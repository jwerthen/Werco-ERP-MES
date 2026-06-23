"""Manual laser-nest entry, PDF attach/preview, edit, and soft-delete.

Covers the standalone manual creation path (POST
``/work-orders/{id}/laser-nests/manual``) and the per-nest routes mounted at
``/laser-nests`` -- edit, attach/detach/preview PDF, soft-delete -- plus the
RBAC, tenant-isolation, and soft-delete serialization invariants.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.laser_nest import LaserNest
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder

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

    def test_second_nest_is_pending_and_rolls_up_quantity(self, client, db_session, laser_setup):
        headers = headers_for(laser_setup["admin"])
        parent_id = laser_setup["parent"].id
        first = _create_manual_nest(client, headers, parent_id, {"cnc_number": "A", "planned_runs": 3})
        assert first.status_code == status.HTTP_201_CREATED
        second = _create_manual_nest(client, headers, parent_id, {"cnc_number": "B", "planned_runs": 4})
        assert second.status_code == status.HTTP_201_CREATED
        assert second.json()["operation_status"] == "pending"  # not the first ready-able op

        nest = db_session.query(LaserNest).filter(LaserNest.id == second.json()["id"]).first()
        child = db_session.query(WorkOrder).filter(WorkOrder.id == nest.operation.work_order_id).first()
        assert float(child.quantity_ordered) == 7.0  # 3 + 4


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
        db_session.expire_all()
        reloaded = db_session.query(LaserNest).filter(LaserNest.id == nest_id).first()
        assert reloaded is not None
        assert reloaded.is_deleted is True
        assert reloaded.work_order_operation_id == op_id, (
            "soft-deleted nest lost its operation FK after the WorkOrderResponse render -- "
            "the in-memory dissociation guard corrupted traceability"
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
