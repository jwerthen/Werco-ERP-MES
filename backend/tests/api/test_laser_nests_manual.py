"""Manual laser-nest entry, PDF attach/preview, edit, and soft-delete.

Covers the standalone manual creation path (POST
``/work-orders/{id}/laser-nests/manual``) and the per-nest routes mounted at
``/laser-nests`` -- edit, attach/detach/preview PDF, soft-delete -- plus the
RBAC, tenant-isolation, and soft-delete serialization invariants.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
from app.models.company import Company
from app.models.laser_nest import LaserNest
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from app.services.work_center_type_service import get_work_center_group

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"nest-{n}@co{company_id}.test",
        employee_id=f"NEST-{n:05d}",
        first_name="Nest",
        last_name=f"Co{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_laser_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"Laser Cutter {n}",
        code=f"LASER-{n}",
        work_center_type="laser",
        description="laser fixture work center",
        hourly_rate=120,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    n = _next()
    part = Part(
        part_number=f"NESTP-{n}",
        name="Nest assembly part",
        description="manual nest fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_parent_work_order(db: Session, part: Part, *, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"WO-NEST-{n}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status="released",
        priority=2,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


@pytest.fixture
def laser_setup(db_session: Session):
    """A parent assembly WO + laser work center + an admin caller for company A."""
    _ensure_company(db_session, COMPANY_A)
    wc = make_laser_work_center(db_session)
    part = make_part(db_session)
    parent = make_parent_work_order(db_session, part)
    admin = make_user(db_session, role=UserRole.ADMIN)
    return {"wc": wc, "part": part, "parent": parent, "admin": admin}


def _create_manual_nest(client: TestClient, headers: dict, parent_id: int, body: dict) -> dict:
    resp = client.post(
        f"/api/v1/work-orders/{parent_id}/laser-nests/manual",
        headers=headers,
        json=body,
    )
    return resp


def _upload_pdf(client: TestClient, headers: dict, *, name: str = "drawing.pdf", mime: str = "application/pdf") -> int:
    resp = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"title": "Nest Drawing", "document_type": "drawing", "revision": "A"},
        files={"file": (name, b"%PDF-1.4\n", mime)},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["id"]


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    from app.api.endpoints import documents as documents_endpoint

    monkeypatch.setattr(documents_endpoint, "UPLOAD_DIR", str(tmp_path))


# --------------------------------------------------------------------------- #
# Manual create
# --------------------------------------------------------------------------- #
class TestManualCreate:
    def test_create_makes_clock_in_able_operation(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        resp = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "PRG-100", "planned_runs": 5, "material": "A36", "thickness": "10ga"},
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        body = resp.json()

        assert body["cnc_number"] == "PRG-100"
        assert body["planned_runs"] == 5
        assert body["operation_status"] == "ready"  # first nest is READY -> clock-in-able
        assert body["work_order_operation_id"] is not None
        assert body["has_document"] is False

        # The backing operation is a READY LASER op at the laser work center with
        # component_quantity == planned_runs, on the child laser WO.
        nest = db_session.query(LaserNest).filter(LaserNest.id == body["id"]).first()
        op = nest.operation
        assert op.operation_group == "LASER"
        assert op.status.value == "ready"
        assert float(op.component_quantity) == 5.0
        assert op.work_center_id == laser_setup["wc"].id

        child = db_session.query(WorkOrder).filter(WorkOrder.id == op.work_order_id).first()
        assert child.parent_work_order_id == laser_setup["parent"].id
        assert child.work_order_type == "laser_cutting"
        assert float(child.quantity_ordered) == 5.0

    def test_second_nest_is_ready_and_rolls_up_quantity(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        parent_id = laser_setup["parent"].id
        first = _create_manual_nest(client, headers, parent_id, {"cnc_number": "A", "planned_runs": 3})
        assert first.status_code == status.HTTP_201_CREATED
        second = _create_manual_nest(client, headers, parent_id, {"cnc_number": "B", "planned_runs": 4})
        assert second.status_code == status.HTTP_201_CREATED
        # Whole-package-ready: laser WOs are dispatch pools, so EVERY nest op is
        # born READY (was "pending" when only the first nest was ready-able).
        assert second.json()["operation_status"] == "ready"

        nest = db_session.query(LaserNest).filter(LaserNest.id == second.json()["id"]).first()
        child = db_session.query(WorkOrder).filter(WorkOrder.id == nest.operation.work_order_id).first()
        assert float(child.quantity_ordered) == 7.0  # 3 + 4


# --------------------------------------------------------------------------- #
# Concurrency: the laser-child-WO advisory lock is taken on the manual path
# --------------------------------------------------------------------------- #
class TestLaserChildWorkOrderLock:
    """``_ensure_laser_child_work_order`` takes a per-parent advisory lock to
    serialize creation of the LASER_CUTTING child WO so two concurrent manual
    adds (or a manual-add racing an import) can't double-create the child.

    The lock is a no-op on SQLite (the test DB), so real serialization can't be
    asserted here -- instead we spy on ``acquire_generator_lock`` (still letting
    the real, harmless function run) and prove it was invoked with the correct
    per-parent namespace + company id on the manual-create path.
    """

    def test_manual_create_acquires_per_parent_lock(self, client, db_session, laser_setup, monkeypatch):
        parent_id = laser_setup["parent"].id
        company_id = laser_setup["admin"].company_id
        calls = []

        real_lock = work_orders_endpoint.acquire_generator_lock

        def _spy(db, namespace, company=None):
            calls.append((namespace, company))
            return real_lock(db, namespace, company)

        monkeypatch.setattr(work_orders_endpoint, "acquire_generator_lock", _spy)

        headers = headers_for(laser_setup["admin"])
        resp = _create_manual_nest(client, headers, parent_id, {"cnc_number": "LOCK-1", "planned_runs": 2})
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        # The same generator-lock helper is used for other namespaces too (e.g.
        # "work_order_number"), so filter for the laser-child namespace rather
        # than asserting a total call count.
        laser_calls = [c for c in calls if c[0].startswith("laser_child_work_order:")]
        assert laser_calls, f"expected a laser_child_work_order lock acquisition, got {calls}"
        assert (f"laser_child_work_order:{parent_id}", company_id) in laser_calls

    def test_second_manual_create_reuses_existing_child(self, client, db_session, laser_setup):
        """A second manual add onto the SAME parent re-uses the existing child laser
        WO (find-or-create) -- exactly one LASER_CUTTING child exists afterward.

        This guards the behavior the advisory lock protects: the second call's
        SELECT must find the committed child rather than INSERT a duplicate.
        """
        headers = headers_for(laser_setup["admin"])
        parent_id = laser_setup["parent"].id

        first = _create_manual_nest(client, headers, parent_id, {"cnc_number": "REUSE-1", "planned_runs": 2})
        assert first.status_code == status.HTTP_201_CREATED, first.text
        second = _create_manual_nest(client, headers, parent_id, {"cnc_number": "REUSE-2", "planned_runs": 3})
        assert second.status_code == status.HTTP_201_CREATED, second.text

        children = (
            db_session.query(WorkOrder)
            .filter(
                WorkOrder.parent_work_order_id == parent_id,
                WorkOrder.work_order_type == "laser_cutting",
                WorkOrder.is_deleted.is_(False),
            )
            .all()
        )
        assert len(children) == 1, f"expected exactly one laser child WO, got {len(children)}"

        # Both nests' operations live on that single child WO.
        first_nest = db_session.query(LaserNest).filter(LaserNest.id == first.json()["id"]).first()
        second_nest = db_session.query(LaserNest).filter(LaserNest.id == second.json()["id"]).first()
        assert first_nest.operation.work_order_id == children[0].id
        assert second_nest.operation.work_order_id == children[0].id


# --------------------------------------------------------------------------- #
# Edit / reverse-sync
# --------------------------------------------------------------------------- #
class TestEditNest:
    def test_patch_planned_runs_reverse_syncs(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        created = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-200", "planned_runs": 2}
        ).json()
        nest_id = created["id"]

        resp = client.patch(f"/api/v1/laser-nests/{nest_id}", headers=headers, json={"planned_runs": 9})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["planned_runs"] == 9

        nest = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        db_session.refresh(nest.operation)
        assert float(nest.operation.component_quantity) == 9.0
        child = db_session.query(WorkOrder).filter(WorkOrder.id == nest.operation.work_order_id).first()
        db_session.refresh(child)
        assert float(child.quantity_ordered) == 9.0

    def test_patch_other_fields(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        created = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-300", "planned_runs": 1}
        ).json()
        resp = client.patch(
            f"/api/v1/laser-nests/{created['id']}",
            headers=headers,
            json={"material": "SS304", "nest_name": "Bracket nest"},
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["material"] == "SS304"
        assert resp.json()["nest_name"] == "Bracket nest"

    def test_lower_planned_runs_below_completed_runs_allowed(self, client, db_session, laser_setup):
        """Over-run is acceptable: planned_runs may be set below completed_runs.

        Only the schema's ``ge=1`` floor constrains the edit -- there is no
        "cannot drop below progress" rule. Start at planned=5, mark progress so
        completed_runs > 0, then drop planned to 2 (still >= 1 but < completed).
        Must be a 200 with planned_runs == 2, NOT a 400.
        """
        headers = headers_for(laser_setup["admin"])
        created = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-310", "planned_runs": 5}
        ).json()
        nest_id = created["id"]

        # Simulate shop-floor progress: 4 runs complete out of a planned 5,
        # mirroring how the suite manipulates state directly in the session.
        nest = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        nest.completed_runs = 4.0
        nest.operation.quantity_complete = 4
        db_session.commit()

        resp = client.patch(f"/api/v1/laser-nests/{nest_id}", headers=headers, json={"planned_runs": 2})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["planned_runs"] == 2

        db_session.expire_all()
        nest = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        assert nest.planned_runs == 2  # 2 < completed (4) is allowed


# --------------------------------------------------------------------------- #
# Document attach / detach / inline preview
# --------------------------------------------------------------------------- #
class TestDocument:
    def test_attach_non_pdf_rejected(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-400", "planned_runs": 1}
        ).json()
        doc_id = _upload_pdf(client, headers, name="notes.txt", mime="text/plain")

        resp = client.post(
            f"/api/v1/laser-nests/{nest['id']}/attach-document",
            headers=headers,
            json={"document_id": doc_id},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "PDF" in resp.json()["detail"]

    def test_attach_pdf_and_inline_serve(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-500", "planned_runs": 1}
        ).json()
        doc_id = _upload_pdf(client, headers)

        attach = client.post(
            f"/api/v1/laser-nests/{nest['id']}/attach-document",
            headers=headers,
            json={"document_id": doc_id},
        )
        assert attach.status_code == status.HTTP_200_OK, attach.text
        assert attach.json()["has_document"] is True
        assert attach.json()["document_id"] == doc_id

        # Inline preview -- readable by an operator (any authenticated user).
        operator_headers = headers_for(make_user(db_session, role=UserRole.OPERATOR))
        serve = client.get(f"/api/v1/laser-nests/{nest['id']}/document", headers=operator_headers)
        assert serve.status_code == status.HTTP_200_OK
        assert serve.headers["content-type"].startswith("application/pdf")
        assert serve.headers["content-disposition"].startswith("inline")

    def test_serve_404_when_no_document(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-510", "planned_runs": 1}
        ).json()
        resp = client.get(f"/api/v1/laser-nests/{nest['id']}/document", headers=headers)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_detach_clears_fk(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-520", "planned_runs": 1}
        ).json()
        doc_id = _upload_pdf(client, headers)
        client.post(f"/api/v1/laser-nests/{nest['id']}/attach-document", headers=headers, json={"document_id": doc_id})
        resp = client.delete(f"/api/v1/laser-nests/{nest['id']}/document", headers=headers)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["has_document"] is False
        # The Document row itself survives (only the FK was cleared).
        from app.models.document import Document

        assert db_session.query(Document).filter(Document.id == doc_id).first() is not None

    def test_detach_then_inline_serve_404(self, client, db_session, laser_setup):
        """After detach, the inline-preview route 404s -- no document is attached.

        Guards the detach->preview transition: once the FK is cleared the GET
        document route must fall into the "No document attached" branch rather
        than serving stale bytes from the (still-existing) Document row.
        """
        headers = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-530", "planned_runs": 1}
        ).json()
        doc_id = _upload_pdf(client, headers)
        client.post(f"/api/v1/laser-nests/{nest['id']}/attach-document", headers=headers, json={"document_id": doc_id})

        # Sanity: served while attached.
        assert (
            client.get(f"/api/v1/laser-nests/{nest['id']}/document", headers=headers).status_code == status.HTTP_200_OK
        )

        detach = client.delete(f"/api/v1/laser-nests/{nest['id']}/document", headers=headers)
        assert detach.status_code == status.HTTP_200_OK

        resp = client.get(f"/api/v1/laser-nests/{nest['id']}/document", headers=headers)
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# Soft-delete hides the nest everywhere
# --------------------------------------------------------------------------- #
class TestSoftDelete:
    def test_delete_hides_from_responses_and_queue(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        parent_id = laser_setup["parent"].id
        nest = _create_manual_nest(client, headers, parent_id, {"cnc_number": "PRG-600", "planned_runs": 6}).json()
        nest_id = nest["id"]
        op_id = nest["work_order_operation_id"]

        # Find the child laser WO id from the operation.
        db_nest = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        child_id = db_nest.operation.work_order_id
        wc_id = laser_setup["wc"].id

        # Before delete: nest visible on the child WorkOrderResponse + in the queue.
        wo_resp = client.get(f"/api/v1/work-orders/{child_id}", headers=headers)
        assert wo_resp.status_code == status.HTTP_200_OK
        ops = wo_resp.json()["operations"]
        assert any(o.get("laser_nest") and o["laser_nest"]["id"] == nest_id for o in ops)

        queue = client.get(f"/api/v1/shop-floor/work-center-queue/{wc_id}", headers=headers)
        assert queue.status_code == status.HTTP_200_OK
        assert any(item["operation_id"] == op_id for item in queue.json()["queue"])

        # Delete (soft).
        delete = client.delete(f"/api/v1/laser-nests/{nest_id}", headers=headers)
        assert delete.status_code == status.HTTP_200_OK

        db_session.expire_all()

        # After delete: absent from the WorkOrderResponse operations' laser_nest,
        # absent from the work-center queue, and child quantity_ordered floors at 1.
        wo_resp2 = client.get(f"/api/v1/work-orders/{child_id}", headers=headers)
        ops2 = wo_resp2.json()["operations"]
        assert all(not (o.get("laser_nest") and o["laser_nest"]["id"] == nest_id) for o in ops2)

        queue2 = client.get(f"/api/v1/shop-floor/work-center-queue/{wc_id}", headers=headers)
        assert all(item["operation_id"] != op_id for item in queue2.json()["queue"])

        child = db_session.query(WorkOrder).filter(WorkOrder.id == child_id).first()
        assert float(child.quantity_ordered) == 1.0  # only nest gone -> floored at 1

    def test_per_nest_routes_404_after_delete(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-610", "planned_runs": 1}
        ).json()
        client.delete(f"/api/v1/laser-nests/{nest['id']}", headers=headers)
        assert (
            client.patch(f"/api/v1/laser-nests/{nest['id']}", headers=headers, json={"planned_runs": 2}).status_code
            == status.HTTP_404_NOT_FOUND
        )


# --------------------------------------------------------------------------- #
# Soft-delete must not corrupt the deleted nest's operation FK
# --------------------------------------------------------------------------- #
class TestSoftDeleteFKIntegrity:
    def test_render_after_delete_keeps_operation_fk_on_deleted_nest(self, client, db_session, laser_setup):
        """Rendering a child WO after soft-delete must NOT null the deleted nest's FK.

        ``_enrich_work_order_operations`` dissociates a soft-deleted nest from the
        response by doing ``op.laser_nest = None`` *in memory*. But
        ``WorkOrderOperation.laser_nest`` is bidirectional
        (``back_populates="operation"``), so that assignment also clears
        ``nest.operation`` on the session-attached soft-deleted row -- which on
        flush would NULL ``laser_nests.work_order_operation_id`` and sever the
        traceability link between the deleted nest and the operation it ran on.

        This is a traceability-corruption guard: after the GET, the soft-deleted
        nest must STILL carry its ``work_order_operation_id`` (NOT NULL) and
        remain ``is_deleted == True``. If this fails it is a real bug in the
        implementation, not the test.
        """
        headers = headers_for(laser_setup["admin"])
        parent_id = laser_setup["parent"].id
        nest = _create_manual_nest(client, headers, parent_id, {"cnc_number": "PRG-650", "planned_runs": 3}).json()
        nest_id = nest["id"]
        op_id = nest["work_order_operation_id"]
        assert op_id is not None

        db_nest = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        child_id = db_nest.operation.work_order_id

        # Soft-delete the nest (operation -> ON_HOLD).
        assert client.delete(f"/api/v1/laser-nests/{nest_id}", headers=headers).status_code == status.HTTP_200_OK

        # Render the child WO -- this runs _enrich_work_order_operations and the
        # in-memory dissociation guard (op.laser_nest = None).
        wo_resp = client.get(f"/api/v1/work-orders/{child_id}", headers=headers)
        assert wo_resp.status_code == status.HTTP_200_OK

        # Re-read the nest straight from the DB; the FK must be intact.
        # FLUSH FIRST, and this line is the whole guard.
        #
        # ``expire_all()`` on its own made this assertion VACUOUS: expiring an attribute
        # DISCARDS any pending in-memory change to it, so the dissociation the guard
        # exists to catch was thrown away before the re-read, and what got measured was
        # the untouched DB row. Verified 2026-08-31 -- swapping ``set_committed_value``
        # back to ``op.laser_nest = None`` (the exact bug) left this test GREEN.
        #
        # Flushing first is what a real regression does for us: ``op.laser_nest = None``
        # back-populates ``nest.operation = None``, which sits pending until ANY flush in
        # the same session -- a commit later in the request, or the autoflush from a lazy
        # load during response serialization -- writes ``work_order_operation_id = NULL``.
        # Do not delete this line, and never put ``expire_all()`` above it.
        db_session.flush()
        db_session.expire_all()
        reloaded = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        assert reloaded is not None
        assert reloaded.is_deleted is True
        assert reloaded.work_order_operation_id == op_id, (
            "soft-deleted nest lost its operation FK after the WorkOrderResponse render -- "
            "the in-memory dissociation guard corrupted traceability"
        )

    def test_hold_context_on_a_deleted_nests_operation_costs_the_nest_neither_its_row_nor_its_fk(
        self, client, db_session, laser_setup
    ):
        """The two guards inside ``_enrich_work_order_operations`` must COEXIST.

        Deleting a nest parks its operation at ``ON_HOLD``, so a soft-deleted nest's
        operation is now EXACTLY the row the hold-provenance lookup targets -- the one
        query this render step did not used to issue at all. Three things have to hold
        on that single row at once, and only the combination is interesting:

        1. the hold reason IS served (the office page can say why the nest stopped);
        2. the soft-deleted nest is STILL hidden from the response (invariant 3 -- a
           tombstone must never surface on a ``WorkOrderResponse``);
        3. the deleted nest STILL owns its ``work_order_operation_id``.

        (3) is the one the new query put at risk. ``set_committed_value`` is what keeps
        the dissociation from dirtying ``nest.operation``; a plain ``op.laser_nest = None``
        would leave that pending, and ANY flush afterwards -- an autoflush from a query
        issued later in this same function is now one -- NULLs the FK and severs the
        traceability link between the deleted nest and the operation it ran on.
        """
        headers = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-660", "planned_runs": 2}
        ).json()
        nest_id = nest["id"]
        op_id = nest["work_order_operation_id"]
        assert op_id is not None

        db_nest = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        child_id = db_nest.operation.work_order_id

        assert client.delete(f"/api/v1/laser-nests/{nest_id}", headers=headers).status_code == status.HTTP_200_OK

        # Somebody then files a blocker against the parked operation -- the reason the
        # office page exists to disclose.
        operation = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == op_id).first()
        assert operation.status == OperationStatus.ON_HOLD
        db_session.add(
            WorkOrderBlocker(
                company_id=COMPANY_A,
                work_order_id=child_id,
                operation_id=op_id,
                category=WorkOrderBlockerCategory.MATERIAL_MISSING.value,
                severity=WorkOrderBlockerSeverity.HIGH.value,
                status=WorkOrderBlockerStatus.OPEN.value,
                title="Material Missing: cancelled nest",
                note="sheet pulled for another job",
                reported_by=laser_setup["admin"].id,
                reported_at=datetime.utcnow(),
            )
        )
        db_session.commit()

        wo_resp = client.get(f"/api/v1/work-orders/{child_id}", headers=headers)
        assert wo_resp.status_code == status.HTTP_200_OK
        held = next(op for op in wo_resp.json()["operations"] if op["id"] == op_id)

        # (1) the reason is served on the tombstone's operation...
        assert held["hold_context"] is not None
        assert held["hold_context"]["blocker"]["note"] == "sheet pulled for another job"
        # (2) ...while the soft-deleted nest itself stays off the response.
        assert held.get("laser_nest") is None

        # (3) and the FK the whole guard exists for is untouched. The query below is
        # itself an autoflush point, which is what makes this assertion load-bearing.
        # FLUSH FIRST, and this line is the whole guard.
        #
        # ``expire_all()`` on its own made this assertion VACUOUS: expiring an attribute
        # DISCARDS any pending in-memory change to it, so the dissociation the guard
        # exists to catch was thrown away before the re-read, and what got measured was
        # the untouched DB row. Verified 2026-08-31 -- swapping ``set_committed_value``
        # back to ``op.laser_nest = None`` (the exact bug) left this test GREEN.
        #
        # Flushing first is what a real regression does for us: ``op.laser_nest = None``
        # back-populates ``nest.operation = None``, which sits pending until ANY flush in
        # the same session -- a commit later in the request, or the autoflush from a lazy
        # load during response serialization -- writes ``work_order_operation_id = NULL``.
        # Do not delete this line, and never put ``expire_all()`` above it.
        db_session.flush()
        db_session.expire_all()
        reloaded = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        assert reloaded.is_deleted is True
        assert reloaded.work_order_operation_id == op_id, (
            "the hold-provenance read cost the soft-deleted nest its operation FK -- "
            "the dissociation guard and the new query are not safe together"
        )


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
class TestRBAC:
    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
    def test_non_privileged_roles_forbidden_on_create(self, client, db_session, laser_setup, role):
        headers = headers_for(make_user(db_session, role=role))
        resp = _create_manual_nest(client, headers, laser_setup["parent"].id, {"cnc_number": "X", "planned_runs": 1})
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
    def test_non_privileged_roles_forbidden_on_mutations(self, client, db_session, laser_setup, role):
        admin_headers = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, admin_headers, laser_setup["parent"].id, {"cnc_number": "PRG-700", "planned_runs": 1}
        ).json()
        headers = headers_for(make_user(db_session, role=role))

        assert (
            client.patch(f"/api/v1/laser-nests/{nest['id']}", headers=headers, json={"planned_runs": 2}).status_code
            == status.HTTP_403_FORBIDDEN
        )
        assert (
            client.post(
                f"/api/v1/laser-nests/{nest['id']}/attach-document", headers=headers, json={"document_id": 1}
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )
        assert (
            client.delete(f"/api/v1/laser-nests/{nest['id']}", headers=headers).status_code == status.HTTP_403_FORBIDDEN
        )

    def test_supervisor_allowed(self, client, db_session, laser_setup):
        headers = headers_for(make_user(db_session, role=UserRole.SUPERVISOR))
        resp = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "SUP-1", "planned_runs": 1}
        )
        assert resp.status_code == status.HTTP_201_CREATED


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #
class TestTenantIsolation:
    def test_cross_tenant_access_404(self, client, db_session, laser_setup):
        admin_a = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, admin_a, laser_setup["parent"].id, {"cnc_number": "PRG-800", "planned_runs": 1}
        ).json()
        nest_id = nest["id"]

        admin_b = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B))

        assert (
            client.patch(f"/api/v1/laser-nests/{nest_id}", headers=admin_b, json={"planned_runs": 2}).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert (
            client.get(f"/api/v1/laser-nests/{nest_id}/document", headers=admin_b).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert client.delete(f"/api/v1/laser-nests/{nest_id}", headers=admin_b).status_code == status.HTTP_404_NOT_FOUND

    def test_cross_tenant_inline_document_404_when_attached(self, client, db_session, laser_setup):
        """Company B cannot serve Company A's nest PDF -- even when one is attached.

        The base cross-tenant test hits the GET-document route on a nest with no
        document, so a 404 there could come from the "No document attached"
        branch rather than the tenant filter. This attaches a real PDF first, so
        the only thing standing between Company B and the bytes is the
        company_id scoping in ``_load_nest`` -- it must still 404.
        """
        admin_a = headers_for(laser_setup["admin"])
        nest = _create_manual_nest(
            client, admin_a, laser_setup["parent"].id, {"cnc_number": "PRG-810", "planned_runs": 1}
        ).json()
        doc_id = _upload_pdf(client, admin_a)
        attach = client.post(
            f"/api/v1/laser-nests/{nest['id']}/attach-document", headers=admin_a, json={"document_id": doc_id}
        )
        assert attach.status_code == status.HTTP_200_OK, attach.text
        # Company A genuinely can serve it -- proves the 404 below is tenancy, not absence.
        assert (
            client.get(f"/api/v1/laser-nests/{nest['id']}/document", headers=admin_a).status_code == status.HTTP_200_OK
        )

        admin_b = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B))
        assert (
            client.get(f"/api/v1/laser-nests/{nest['id']}/document", headers=admin_b).status_code
            == status.HTTP_404_NOT_FOUND
        )


# --------------------------------------------------------------------------- #
# Sheet-descriptor normalization (services/laser_nest_text)
# --------------------------------------------------------------------------- #
class TestSheetDescriptorNormalization:
    """A nest cannot be born with an uncanonical sheet descriptor.

    ``material`` / ``thickness`` / ``sheet_size`` carry no ``Part`` FK, so the
    STRING is the only grouping key anything has. A 2026-08-06 production
    reconciliation found the same physical sheet split across two rows on
    whitespace alone (``144x60`` vs ``144 x 60``), which made every group
    under-report. These lock the two WRITE seams reachable over HTTP -- create
    and edit -- rather than only the pure helper, because it is the seams that
    regress when someone adds a third one.
    """

    def test_manual_create_canonicalizes_the_descriptors(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        resp = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {
                "cnc_number": "PRG-NORM-1",
                "planned_runs": 1,
                "material": "  a36 ",
                "thickness": "16 GA",
                "sheet_size": "144x60",
            },
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        nest = db_session.query(LaserNest).filter(LaserNest.id == resp.json()["id"]).first()
        assert nest.material == "A36"
        assert nest.thickness == "16ga"
        assert nest.sheet_size == "144 x 60"

    def test_two_spellings_of_one_sheet_land_on_the_same_key(self, client, db_session, laser_setup):
        """THE regression, end to end: the exact pair that fragmented in prod."""
        headers = headers_for(laser_setup["admin"])
        parent_id = laser_setup["parent"].id
        first = _create_manual_nest(
            client,
            headers,
            parent_id,
            {
                "cnc_number": "PRG-NORM-2",
                "planned_runs": 1,
                "material": "A36",
                "thickness": "0.25",
                "sheet_size": "144x60",
            },
        ).json()
        second = _create_manual_nest(
            client,
            headers,
            parent_id,
            {
                "cnc_number": "PRG-NORM-3",
                "planned_runs": 1,
                "material": "A36",
                "thickness": "0.25",
                "sheet_size": "144 x 60",
            },
        ).json()

        rows = db_session.query(LaserNest).filter(LaserNest.id.in_([first["id"], second["id"]])).all()
        assert len({(r.material, r.thickness, r.sheet_size) for r in rows}) == 1

    def test_patch_canonicalizes_too(self, client, db_session, laser_setup):
        """An edit is another way a nest is written; a hand-typed '144x60' here
        would reintroduce exactly what import normalization removes."""
        headers = headers_for(laser_setup["admin"])
        created = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "PRG-NORM-4", "planned_runs": 1}
        ).json()

        resp = client.patch(
            f"/api/v1/laser-nests/{created['id']}",
            headers=headers,
            json={"material": "ss", "thickness": ".125", "sheet_size": "120×48"},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["material"] == "SS"
        assert resp.json()["thickness"] == "0.125"
        assert resp.json()["sheet_size"] == "120 x 48"

    def test_unrecognized_descriptor_survives_verbatim(self, client, db_session, laser_setup):
        """Normalization must never mangle what it cannot parse -- a descriptor
        it does not understand is collapsed on whitespace and nothing else."""
        headers = headers_for(laser_setup["admin"])
        resp = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "PRG-NORM-5", "planned_runs": 1, "sheet_size": "remnant   drop"},
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        nest = db_session.query(LaserNest).filter(LaserNest.id == resp.json()["id"]).first()
        assert nest.sheet_size == "remnant drop"


# --------------------------------------------------------------------------- #
# Which LASER a nest lands on
#
# The reported bug: adding a nest to a job whose nests already run on the
# Ermaksan silently dispatched the new one to the HSG tube laser. ``POST
# /work-orders/{id}/laser-nests/manual`` shipped in #55 (2026-06-23) a month
# before #136 (2026-07-20) taught the IMPORT path about work-center assignment,
# and the manual path was never converged: it re-asked the shop-wide question
# ("which laser does this shop prefer?") on every call instead of the job-level
# one ("which laser is this job on?"), and offered no way to say.
#
# None of the 114 tests above pinned the machine, which is exactly why the
# defect survived. These do.
# --------------------------------------------------------------------------- #
def _make_wc(
    db: Session,
    *,
    name: str,
    code: str,
    wc_type: str = "laser_cutting",
    is_active: bool = True,
    company_id: int = COMPANY_A,
) -> WorkCenter:
    """A work center with FULL control of the three fields the resolver reads."""
    _ensure_company(db, company_id)
    wc = WorkCenter(
        name=name,
        code=code,
        work_center_type=wc_type,
        description="work-center-resolution fixture",
        hourly_rate=120,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _op_work_center(db: Session, nest_body: dict) -> int:
    return db.get(WorkOrderOperation, nest_body["work_order_operation_id"]).work_center_id


class TestManualNestWorkCenterResolution:
    def test_second_nest_inherits_the_machine_the_first_one_runs_on(self, client, db_session, laser_setup):
        """The job's own laser beats the shop's preferred laser."""
        headers = headers_for(laser_setup["admin"])
        first = _create_manual_nest(client, headers, laser_setup["parent"].id, {"cnc_number": "A-1", "planned_runs": 1})
        assert first.status_code == status.HTTP_201_CREATED, first.text
        assert _op_work_center(db_session, first.json()) == laser_setup["wc"].id

        # A machine the SHOP-WIDE auto-detect ranks strictly higher (tier 0,
        # "ermaksan") appears after the job is already running. Auto-detect would
        # now return it; the job is not on it.
        preferred = _make_wc(db_session, name="Ermaksan Fiber Laser", code="ERM-1")

        second = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "A-2", "planned_runs": 1}
        )
        assert second.status_code == status.HTTP_201_CREATED, second.text
        assert _op_work_center(db_session, second.json()) == laser_setup["wc"].id
        assert _op_work_center(db_session, second.json()) != preferred.id

    def test_inherits_the_jobs_laser_even_when_autodetect_would_pick_the_tube(self, client, db_session, laser_setup):
        """The owner's exact report, in one test.

        Nests already on the Ermaksan; the only machine auto-detect can still see
        is the HSG tube laser. The new nest must stay on the Ermaksan.
        """
        headers = headers_for(laser_setup["admin"])
        ermaksan = _make_wc(db_session, name="Ermaksan Fiber Laser 6KW Bay 2", code="ERM-6K")
        first = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "TUBE-1", "planned_runs": 2, "work_center_id": ermaksan.id},
        )
        assert first.status_code == status.HTTP_201_CREATED, first.text
        assert _op_work_center(db_session, first.json()) == ermaksan.id

        # Take every OTHER laser out of auto-detect's reach, so the shop-wide
        # answer is unambiguously the tube laser.
        tube = _make_wc(db_session, name="HSG Tube Laser", code="HSG-1")
        laser_setup["wc"].is_active = False
        ermaksan.is_active = False
        db_session.commit()
        assert work_orders_endpoint._find_laser_work_center(db_session, COMPANY_A).id == tube.id

        second = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "TUBE-2", "planned_runs": 2}
        )
        assert second.status_code == status.HTTP_201_CREATED, second.text
        # Inherited, NOT auto-detected. is_active is deliberately not filtered on
        # the inherit path: "the same machine as the rest of the job" is the whole
        # point, and a work center holding live work cannot be deactivated anyway.
        assert _op_work_center(db_session, second.json()) == ermaksan.id
        assert _op_work_center(db_session, second.json()) != tube.id

    def test_inherits_the_packages_modal_machine_not_its_last_one(self, client, db_session, laser_setup):
        """A nest package may legitimately span two lasers; one outlier must not drag the rest."""
        headers = headers_for(laser_setup["admin"])
        other = _make_wc(db_session, name="Second Laser", code="LSR-2")
        for cnc in ("MODE-1", "MODE-2"):
            resp = _create_manual_nest(
                client, headers, laser_setup["parent"].id, {"cnc_number": cnc, "planned_runs": 1}
            )
            assert resp.status_code == status.HTTP_201_CREATED, resp.text
        # One nest deliberately moved to the other machine, and it is the HIGHEST
        # sequence — a "last nest wins" rule would follow it.
        outlier = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "MODE-3", "planned_runs": 1, "work_center_id": other.id},
        )
        assert outlier.status_code == status.HTTP_201_CREATED, outlier.text
        assert _op_work_center(db_session, outlier.json()) == other.id

        # A machine the shop-wide auto-detect ranks above BOTH, so landing on the
        # modal machine cannot be a coincidence of the fallback agreeing.
        preferred = _make_wc(db_session, name="Ermaksan Fiber Laser", code="ERM-MODE")
        assert work_orders_endpoint._find_laser_work_center(db_session, COMPANY_A).id == preferred.id

        added = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "MODE-4", "planned_runs": 1}
        )
        assert added.status_code == status.HTTP_201_CREATED, added.text
        landed = _op_work_center(db_session, added.json())
        assert landed == laser_setup["wc"].id  # the MODE, not...
        assert landed != other.id  # ...the last nest's machine, and not...
        assert landed != preferred.id  # ...the shop-wide preference.

    def test_explicit_work_center_id_wins_over_the_incumbent(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        first = _create_manual_nest(client, headers, laser_setup["parent"].id, {"cnc_number": "X-1", "planned_runs": 1})
        assert first.status_code == status.HTTP_201_CREATED, first.text
        chosen = _make_wc(db_session, name="Ermaksan Fiber Laser", code="ERM-2")

        moved = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "X-2", "planned_runs": 1, "work_center_id": chosen.id},
        )
        assert moved.status_code == status.HTTP_201_CREATED, moved.text
        assert _op_work_center(db_session, moved.json()) == chosen.id

    def test_unknown_and_cross_tenant_work_center_ids_are_404_with_nothing_created(
        self, client, db_session, laser_setup
    ):
        headers = headers_for(laser_setup["admin"])
        before = db_session.query(LaserNest).count()
        foreign = _make_wc(db_session, name="Other Co Laser", code="OC-1", company_id=COMPANY_B)

        for work_center_id in (999_999, foreign.id):
            resp = _create_manual_nest(
                client,
                headers,
                laser_setup["parent"].id,
                {"cnc_number": "NOPE", "planned_runs": 1, "work_center_id": work_center_id},
            )
            assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert db_session.query(LaserNest).count() == before

    def test_inactive_work_center_id_is_refused(self, client, db_session, laser_setup):
        """An explicit pick still has to be a machine you can dispatch to."""
        headers = headers_for(laser_setup["admin"])
        retired = _make_wc(db_session, name="Retired Laser", code="RET-1", is_active=False)
        resp = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "RET", "planned_runs": 1, "work_center_id": retired.id},
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    def test_first_nest_on_a_fresh_job_still_prefers_the_ermaksan_over_the_tube(self, client, db_session, laser_setup):
        """No incumbent to inherit: the shop-wide preference applies, tube last."""
        headers = headers_for(laser_setup["admin"])
        laser_setup["wc"].is_active = False
        db_session.commit()
        tube = _make_wc(db_session, name="HSG Tube Laser", code="HSG-2")
        ermaksan = _make_wc(db_session, name="Ermaksan Fiber Laser", code="ERM-3")
        assert tube.id < ermaksan.id  # the id tiebreak must NOT be what decides this

        resp = _create_manual_nest(client, headers, laser_setup["parent"].id, {"cnc_number": "F-1", "planned_runs": 1})
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        assert _op_work_center(db_session, resp.json()) == ermaksan.id

    def test_autodetect_sees_an_ermaksan_that_never_says_laser(self, client, db_session, laser_setup):
        """The candidate set is keyed on the same tokens the tiering is.

        A prefilter demanding the literal "laser" dropped an Ermaksan row spelled
        "Ermaksan 6KW Bay 2" / "ERM-6K" under a non-laser type, while a tube laser
        typed ``laser_cutting`` sailed through -- so the auto-detect returned the
        one machine the rule exists to avoid.
        """
        laser_setup["wc"].is_active = False
        db_session.commit()
        tube = _make_wc(db_session, name="HSG Tube Laser", code="HSG-3", wc_type="laser_cutting")
        ermaksan = _make_wc(db_session, name="Ermaksan 6KW Bay 2", code="ERM-6KB", wc_type="fabrication")
        assert tube.id < ermaksan.id

        resolved = work_orders_endpoint._find_laser_work_center(db_session, COMPANY_A)
        assert resolved.id == ermaksan.id

        resp = _create_manual_nest(
            client,
            headers_for(laser_setup["admin"]),
            laser_setup["parent"].id,
            {"cnc_number": "NL-1", "planned_runs": 1},
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        assert _op_work_center(db_session, resp.json()) == ermaksan.id

    def test_inherits_from_nests_on_a_machine_whose_name_never_says_laser(self, client, db_session, laser_setup):
        """The shape that defeats an ``operation_group``-keyed incumbent probe.

        A nest operation derives its group from ITS work center via
        ``get_work_center_group``, which keys on the literal token "LASER" in the
        machine's type or name. Nests on an Ermaksan row spelled "Ermaksan 6KW Bay 2"
        / type ``fabrication`` therefore carry group "OTHER" -- and that is exactly
        the row shape that makes the shop-wide auto-detect wrong in the first place.
        Reading the incumbent off ``operation_group`` would miss the case the fix
        exists for; it is read off the nests themselves.
        """
        headers = headers_for(laser_setup["admin"])
        ermaksan = _make_wc(db_session, name="Ermaksan 6KW Bay 2", code="ERM-NG", wc_type="fabrication")
        first = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "NG-1", "planned_runs": 1, "work_center_id": ermaksan.id},
        )
        assert first.status_code == status.HTTP_201_CREATED, first.text
        first_op = db_session.get(WorkOrderOperation, first.json()["work_order_operation_id"])
        assert first_op.work_center_id == ermaksan.id

        # Re-stamp the group the way an IMPORT would have. `create_manual_laser_nest`
        # hard-codes "LASER", but `import_nest_package` derives it per nest from the
        # nest's own work center (`get_work_center_group`) -- and nest packages are
        # normally imported, so this is the shape a real job carries. Hand-built for
        # the same reason `_legacy_tombstone` is: the manual endpoint cannot produce it.
        assert get_work_center_group(ermaksan) != "LASER"
        first_op.operation_group = get_work_center_group(ermaksan)
        db_session.commit()

        second = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "NG-2", "planned_runs": 1}
        )
        assert second.status_code == status.HTTP_201_CREATED, second.text
        second_op = db_session.get(WorkOrderOperation, second.json()["work_order_operation_id"])
        assert second_op.work_center_id == ermaksan.id
        # ...and the sequence/label probe has the same blind spot: without the same
        # remedy the second nest lands back at sequence 10 as a duplicate "Nest 1".
        assert second_op.sequence > first_op.sequence
        assert second_op.operation_number != first_op.operation_number

    def test_a_legacy_tombstone_cannot_out_vote_the_live_nests(self, client, db_session, laser_setup):
        """Soft-deleted nests are excluded from the incumbent tally.

        Every delete made before the removal verb shipped left an ON_HOLD operation
        with its nest soft-deleted and the FK intact, and those rows are in
        production. Counting them would let two dead nests on one machine outvote
        the live one on another and re-home the job by arithmetic.
        """
        headers = headers_for(laser_setup["admin"])
        live = _make_wc(db_session, name="Live Laser", code="LIVE-1")
        abandoned = _make_wc(db_session, name="Abandoned Laser", code="DEAD-1")

        keep = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "TB-KEEP", "planned_runs": 1, "work_center_id": live.id},
        )
        assert keep.status_code == status.HTTP_201_CREATED, keep.text
        for cnc in ("TB-DEAD-1", "TB-DEAD-2"):
            dead = _create_manual_nest(
                client,
                headers,
                laser_setup["parent"].id,
                {"cnc_number": cnc, "planned_runs": 1, "work_center_id": abandoned.id},
            )
            assert dead.status_code == status.HTTP_201_CREATED, dead.text
            # The legacy tombstone shape: nest soft-deleted, operation still attached.
            db_session.get(LaserNest, dead.json()["id"]).is_deleted = True
        db_session.commit()

        added = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "TB-NEW", "planned_runs": 1}
        )
        assert added.status_code == status.HTTP_201_CREATED, added.text
        # 1 live nest beats 2 tombstones.
        assert _op_work_center(db_session, added.json()) == live.id

    def test_equal_counts_break_on_the_earliest_sequence(self, client, db_session, laser_setup):
        """The COUNT DESC leg ties; `MIN(sequence) ASC` is what makes the answer deterministic."""
        headers = headers_for(laser_setup["admin"])
        second = _make_wc(db_session, name="Runner Up Laser", code="TIE-2")
        first = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "TIE-A", "planned_runs": 1}
        )
        assert first.status_code == status.HTTP_201_CREATED, first.text
        later = _create_manual_nest(
            client,
            headers,
            laser_setup["parent"].id,
            {"cnc_number": "TIE-B", "planned_runs": 1, "work_center_id": second.id},
        )
        assert later.status_code == status.HTTP_201_CREATED, later.text
        # One nest each — the tally is tied.
        first_op = db_session.get(WorkOrderOperation, first.json()["work_order_operation_id"])
        later_op = db_session.get(WorkOrderOperation, later.json()["work_order_operation_id"])
        assert first_op.sequence < later_op.sequence

        added = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "TIE-C", "planned_runs": 1}
        )
        assert added.status_code == status.HTTP_201_CREATED, added.text
        assert _op_work_center(db_session, added.json()) == laser_setup["wc"].id

    def test_a_job_whose_nests_were_all_removed_falls_back_instead_of_failing(self, client, db_session, laser_setup):
        """No incumbent left: auto-detect, not a 500."""
        headers = headers_for(laser_setup["admin"])
        gone = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "GONE-1", "planned_runs": 1}
        )
        assert gone.status_code == status.HTTP_201_CREATED, gone.text
        # What `remove_laser_nest` leaves behind: the nest soft-deleted AND detached
        # from an operation row that is itself gone.
        nest = db_session.get(LaserNest, gone.json()["id"])
        operation = nest.operation
        nest.operation = None
        db_session.flush()
        db_session.delete(operation)
        nest.is_deleted = True
        db_session.commit()

        added = _create_manual_nest(
            client, headers, laser_setup["parent"].id, {"cnc_number": "GONE-2", "planned_runs": 1}
        )
        assert added.status_code == status.HTTP_201_CREATED, added.text
        assert _op_work_center(db_session, added.json()) == laser_setup["wc"].id
