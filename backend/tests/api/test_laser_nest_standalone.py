"""Standalone laser-nest work orders: part-less, parent-less nest WOs.

Covers the new standalone endpoints and the ``{work_order_id}`` generalization:
  - ``POST /work-orders/laser-nest-packages/standalone/preview`` -- same package
    parsing as the parent-addressed preview, no WO id required.
  - ``POST /work-orders/laser-nest-packages/standalone/import`` -- creates a
    FRESH RELEASED ``laser_cutting`` WO with ``part_id NULL`` and no parent;
    ``quantity_ordered`` = total planned sheet runs; the package carries
    ``parent_work_order_id NULL`` + ``child_work_order_id`` = the new WO; nest
    PDF Documents attach to the created WO itself; the WO creation and each
    nest are audited.
  - Re-import and manual nest-add addressed AT the standalone laser WO operate
    on it directly (no child WO is nested under it).
  - The model-level CHECK ``ck_work_orders_part_required_unless_laser`` rejects
    a part-less WO of any other type (enforced by SQLite create_all too).
  - Read paths (list summary + detail response) serialize ``part_id=None``
    instead of 500ing.

Offline by contract: only CNC-file packages (filename inference, no AI) and the
PDF confirm-and-commit path (no extractor call) are used; the extractor is
patched to fail the test if ever invoked.
"""

import io
import zipfile
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.services.scheduling_service import SchedulingService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"salone-{n}@co{company_id}.test",
        employee_id=f"SALONE-{n:05d}",
        first_name="Standalone",
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
        code=f"LASER-SA-{n}",
        work_center_type="laser",
        description="laser fixture",
        hourly_rate=120,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _cnc_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, "M30")
    return buf.getvalue()


def _pdf_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"%PDF-1.4\n%stub nest report\n")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    """Keep storage + laser package roots hermetic (same as the PDF-import tests)."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    """Every path exercised here is AI-free; any extractor call is a bug."""
    monkeypatch.setattr(
        work_orders_endpoint,
        "extract_nest_fields_from_pdf",
        lambda *a, **k: pytest.fail("standalone laser-nest tests must not call the AI extractor"),
    )


def _standalone_preview(client, headers, zip_bytes, *, name="nests.zip"):
    return client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/preview",
        headers=headers,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _standalone_import(client, headers, zip_bytes, *, rows=None, work_center_id=None, name="nests.zip"):
    import json as _json

    data = {}
    if rows is not None:
        data["rows"] = _json.dumps(rows)
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    return client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        headers=headers,
        data=data,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _wo_import(client, headers, wo_id, zip_bytes, *, rows=None, work_center_id=None, name="nests.zip"):
    import json as _json

    data = {}
    if rows is not None:
        data["rows"] = _json.dumps(rows)
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    return client.post(
        f"/api/v1/work-orders/{wo_id}/laser-nest-packages/import",
        headers=headers,
        data=data,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _import_standalone_wo(client, db_session, admin, wc) -> dict:
    """Create one standalone nest WO via the CNC-zip import; return the response WO dict."""
    resp = _standalone_import(
        client,
        headers_for(admin),
        _cnc_zip("NEST-A_A36_10ga_60x120_QTY3.nc", "NEST-B_304SS_0.25in_48x96_x2.tap"),
        work_center_id=wc.id,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["child_work_order"]


# --------------------------------------------------------------------------- #
# Standalone preview
# --------------------------------------------------------------------------- #
class TestStandalonePreview:
    def test_preview_cnc_zip_returns_rows_without_a_work_order(self, client, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)

        resp = _standalone_preview(
            client, headers_for(admin), _cnc_zip("NEST-A_A36_10ga_60x120_QTY3.nc", "NEST-B_304SS_0.25in_48x96_x2.tap")
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        assert data["nest_count"] == 2
        assert data["total_planned_runs"] == 5  # 3 + 2 from filenames
        assert {row["planned_runs"] for row in data["nests"]} == {3, 2}
        # No work order was created by a preview.
        assert db_session.query(WorkOrder).count() == 0

    def test_preview_requires_privileged_role(self, client, db_session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        resp = _standalone_preview(client, headers_for(operator), _cnc_zip("NEST-A_QTY1.nc"))
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Standalone import
# --------------------------------------------------------------------------- #
class TestStandaloneImport:
    def test_import_creates_partless_released_laser_wo(self, client, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)

        child = _import_standalone_wo(client, db_session, admin, wc)

        # Response contract: fresh RELEASED laser WO, no part, no parent, runs qty.
        assert child["part_id"] is None
        assert child["parent_work_order_id"] is None
        assert child["work_order_type"] == "laser_cutting"
        assert child["status"] == "released"
        assert child["quantity_ordered"] == 5  # 3 + 2 sheet runs
        assert child["work_order_number"].startswith("WO-")
        assert len(child["operations"]) == 2

        # DB: exactly one WO exists (no parent assembly, no nested child).
        wos = db_session.query(WorkOrder).all()
        assert [wo.id for wo in wos] == [child["id"]]
        wo = wos[0]
        assert wo.part_id is None
        assert wo.parent_work_order_id is None
        assert wo.work_order_type == WorkOrderType.LASER_CUTTING.value
        assert wo.status == WorkOrderStatus.RELEASED

        # Package: parent NULL, child = the created WO.
        package = db_session.query(LaserNestPackage).one()
        assert package.parent_work_order_id is None
        assert package.child_work_order_id == wo.id
        assert db_session.query(LaserNest).filter_by(package_id=package.id).count() == 2

    def test_import_audits_wo_creation_and_each_nest(self, client, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)

        child = _import_standalone_wo(client, db_session, admin, wc)

        wo_creates = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "work_order", AuditLog.action == "CREATE").all()
        )
        assert len(wo_creates) == 1
        assert wo_creates[0].resource_id == child["id"]
        assert wo_creates[0].extra_data.get("source") == "laser_nest_standalone_import"
        # Snapshot taken AFTER the build: quantity is the total planned runs.
        assert wo_creates[0].extra_data.get("quantity") == 5.0
        assert wo_creates[0].company_id == admin.company_id

        nest_creates = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest", AuditLog.action == "CREATE").all()
        )
        assert len(nest_creates) == 2
        assert all(row.extra_data.get("parent_work_order_id") is None for row in nest_creates)
        assert all(row.extra_data.get("child_work_order_id") == child["id"] for row in nest_creates)
        assert all(row.extra_data.get("source") == "cnc_file_import" for row in nest_creates)

    def test_pdf_rows_import_attaches_documents_to_created_wo(self, client, db_session):
        """Design item 3: nest-PDF Documents attach to the CREATED standalone WO
        (there is no parent to attach them to)."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)

        rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 4, "material": "A36"},
            {"source_file": "05750.pdf", "cnc_number": "05750", "planned_runs": 2, "material": "304SS"},
        ]
        resp = _standalone_import(
            client, headers_for(admin), _pdf_zip("05749.pdf", "05750.pdf"), rows=rows, work_center_id=wc.id
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        assert child["part_id"] is None
        assert child["quantity_ordered"] == 6

        nests = db_session.query(LaserNest).all()
        assert len(nests) == 2
        for nest in nests:
            assert nest.document_id is not None
            doc = db_session.query(Document).filter_by(id=nest.document_id).one()
            assert doc.work_order_id == child["id"]  # attached to the standalone WO itself
            assert doc.mime_type == "application/pdf"

    def test_import_requires_privileged_role(self, client, db_session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        resp = _standalone_import(client, headers_for(operator), _cnc_zip("NEST-A_QTY1.nc"))
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert db_session.query(WorkOrder).count() == 0


# --------------------------------------------------------------------------- #
# Generalized {work_order_id} endpoints on a standalone laser WO
# --------------------------------------------------------------------------- #
class TestReimportOntoStandaloneWo:
    def test_reimport_replaces_nests_on_the_same_wo_without_creating_a_child(self, client, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        # Re-import a DIFFERENT package targeting the standalone WO itself.
        resp = _wo_import(
            client,
            headers_for(admin),
            child["id"],
            _cnc_zip("NEST-C_A36_10ga_48x96_QTY7.nc"),
            work_center_id=wc.id,
            name="cnc2.zip",
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rebuilt = resp.json()["child_work_order"]

        # Operated on the SAME WO directly -- no child WO nested under it.
        assert rebuilt["id"] == child["id"]
        assert rebuilt["parent_work_order_id"] is None
        assert rebuilt["part_id"] is None
        assert rebuilt["quantity_ordered"] == 7
        assert len(rebuilt["operations"]) == 1
        assert db_session.query(WorkOrder).count() == 1

        # Import-replaces-everything: one package, one nest, supersession audited.
        packages = db_session.query(LaserNestPackage).all()
        assert len(packages) == 1
        assert packages[0].parent_work_order_id is None
        assert packages[0].child_work_order_id == child["id"]
        assert db_session.query(LaserNest).count() == 1

        deletes = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest", AuditLog.action == "DELETE").all()
        )
        assert len(deletes) == 2  # both first-import nests superseded
        assert all(row.extra_data.get("reason") == "superseded_by_reimport" for row in deletes)
        assert all(row.extra_data.get("parent_work_order_id") is None for row in deletes)

    def test_reimport_takes_the_per_wo_advisory_lock(self, client, db_session, monkeypatch):
        """The direct-target branch keeps the advisory-lock discipline: the laser
        WO's own key is locked (a no-op on SQLite, spied here)."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        calls = []
        real_lock = work_orders_endpoint.acquire_generator_lock

        def _spy(db, namespace, company=None):
            calls.append((namespace, company))
            return real_lock(db, namespace, company)

        monkeypatch.setattr(work_orders_endpoint, "acquire_generator_lock", _spy)

        resp = _wo_import(
            client, headers_for(admin), child["id"], _cnc_zip("NEST-D_QTY1.nc"), work_center_id=wc.id, name="cnc3.zip"
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert (f"laser_child_work_order:{child['id']}", admin.company_id) in calls

    def test_classic_parent_flow_still_creates_a_child(self, client, db_session):
        """Regression: a NON-laser parent addressed by id still gets a laser child."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        n = _next()
        part = Part(
            part_number=f"ASM-SA-{n}",
            name="Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=COMPANY_A,
        )
        db_session.add(part)
        db_session.flush()
        parent = WorkOrder(
            work_order_number=f"WO-SA-{n}",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=3,
            company_id=COMPANY_A,
        )
        db_session.add(parent)
        db_session.commit()

        resp = _wo_import(client, headers_for(admin), parent.id, _cnc_zip("NEST-E_QTY2.nc"), work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        assert child["id"] != parent.id
        assert child["parent_work_order_id"] == parent.id
        assert child["part_id"] == part.id  # classic child still inherits the parent's part
        package = db_session.query(LaserNestPackage).one()
        assert package.parent_work_order_id == parent.id
        assert package.child_work_order_id == child["id"]

    def test_reimport_audits_wo_level_status_and_quantity_reset(self, client, db_session):
        """Invariant 2: re-importing onto an EXISTING laser WO force-sets
        RELEASED, zeroes quantity_complete/quantity_scrapped, and re-derives
        quantity_ordered -- that WO-level change must land in the audit log
        (the per-nest DELETE/CREATE rows alone don't record it)."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        # Simulate in-flight production on the standalone WO.
        wo = db_session.query(WorkOrder).filter_by(id=child["id"]).one()
        wo.status = WorkOrderStatus.IN_PROGRESS
        wo.quantity_complete = 2
        db_session.commit()

        resp = _wo_import(
            client, headers_for(admin), child["id"], _cnc_zip("NEST-R_QTY7.nc"), work_center_id=wc.id, name="cnc-r.zip"
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order",
                AuditLog.action == "UPDATE",
                AuditLog.resource_id == child["id"],
            )
            .one()
        )
        assert row.old_values["status"] == "in_progress"
        assert row.new_values["status"] == "released"
        assert row.old_values["quantity_complete"] == 2.0
        assert row.new_values["quantity_complete"] == 0.0
        assert row.old_values["quantity_ordered"] == 5.0
        assert row.new_values["quantity_ordered"] == 7.0  # re-derived to the new package's runs
        assert row.extra_data.get("reason") == "laser_nest_package_import"
        assert row.extra_data.get("parent_work_order_id") is None
        assert row.company_id == admin.company_id

    def test_first_standalone_import_writes_no_wo_update_row(self, client, db_session):
        """The fresh-WO path is covered by log_create alone -- no spurious
        UPDATE row for the WO the same request just created."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        updates = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order",
                AuditLog.action == "UPDATE",
                AuditLog.resource_id == child["id"],
            )
            .all()
        )
        assert updates == []

    def test_soft_deleted_laser_wo_is_404_for_import_and_manual(self, client, db_session):
        """Invariant 3: a soft-deleted WO must not be resolvable by the
        ``{work_order_id}`` nest endpoints (which would rebuild + RELEASE it)."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        wo = db_session.query(WorkOrder).filter_by(id=child["id"]).one()
        wo.soft_delete(admin.id)
        db_session.commit()

        resp = _wo_import(client, headers_for(admin), child["id"], _cnc_zip("NEST-Z_QTY1.nc"), work_center_id=wc.id)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

        resp = client.post(
            f"/api/v1/work-orders/{child['id']}/laser-nests/manual",
            headers=headers_for(admin),
            json={"cnc_number": "88001", "planned_runs": 2},
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

        # The deleted WO was not resurrected and gained no new nests.
        db_session.expire_all()
        wo = db_session.query(WorkOrder).filter_by(id=child["id"]).one()
        assert wo.is_deleted is True
        assert db_session.query(LaserNest).count() == 2  # the two first-import nests only

    def test_cross_tenant_wo_id_is_404(self, client, db_session):
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        wc_a = make_laser_work_center(db_session, company_id=COMPANY_A)
        child = _import_standalone_wo(client, db_session, admin_a, wc_a)

        admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)
        resp = _wo_import(client, headers_for(admin_b), child["id"], _cnc_zip("NEST-F_QTY1.nc"))
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_reimport_addressed_at_classic_laser_child_operates_on_it_directly(self, client, db_session, monkeypatch):
        """Generalization branch with a PARENT: a classic laser child addressed
        by its own id is rebuilt in place (no grandchild WO), the package keeps
        the parent linkage, and BOTH advisory-lock keys are taken parent-first
        (matching the classic flow's single parent-key acquisition, so a
        parent-addressed import racing a child-addressed one cannot deadlock)."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        n = _next()
        part = Part(
            part_number=f"ASM-CH-{n}",
            name="Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=COMPANY_A,
        )
        db_session.add(part)
        db_session.flush()
        parent = WorkOrder(
            work_order_number=f"WO-CH-{n}",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=3,
            company_id=COMPANY_A,
        )
        db_session.add(parent)
        db_session.commit()

        first = _wo_import(client, headers_for(admin), parent.id, _cnc_zip("NEST-G_QTY2.nc"), work_center_id=wc.id)
        assert first.status_code == status.HTTP_200_OK, first.text
        child = first.json()["child_work_order"]
        assert child["parent_work_order_id"] == parent.id

        calls = []
        real_lock = work_orders_endpoint.acquire_generator_lock

        def _spy(db, namespace, company=None):
            calls.append((namespace, company))
            return real_lock(db, namespace, company)

        monkeypatch.setattr(work_orders_endpoint, "acquire_generator_lock", _spy)

        resp = _wo_import(
            client, headers_for(admin), child["id"], _cnc_zip("NEST-H_QTY5.nc"), work_center_id=wc.id, name="cnc4.zip"
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rebuilt = resp.json()["child_work_order"]

        # Rebuilt IN PLACE: same WO, parent linkage + inherited part preserved,
        # quantity re-derived, and no grandchild WO appeared.
        assert rebuilt["id"] == child["id"]
        assert rebuilt["parent_work_order_id"] == parent.id
        assert rebuilt["part_id"] == part.id
        assert rebuilt["quantity_ordered"] == 5
        assert db_session.query(WorkOrder).count() == 2  # parent + child only

        package = db_session.query(LaserNestPackage).one()
        assert package.parent_work_order_id == parent.id  # parent linkage kept on the package
        assert package.child_work_order_id == child["id"]

        # Lock discipline: parent key FIRST, then the child's own key.
        parent_key = (f"laser_child_work_order:{parent.id}", admin.company_id)
        own_key = (f"laser_child_work_order:{child['id']}", admin.company_id)
        assert parent_key in calls and own_key in calls
        assert calls.index(parent_key) < calls.index(own_key)


class TestManualNestOnStandaloneWo:
    def test_manual_add_appends_to_the_standalone_wo(self, client, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        resp = client.post(
            f"/api/v1/work-orders/{child['id']}/laser-nests/manual",
            headers=headers_for(admin),
            json={"cnc_number": "77001", "planned_runs": 4, "material": "A36"},
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        nest_id = resp.json()["id"]

        # No child WO was nested under the standalone WO.
        assert db_session.query(WorkOrder).count() == 1

        nest = db_session.query(LaserNest).filter_by(id=nest_id).one()
        operation = db_session.query(WorkOrderOperation).filter_by(id=nest.work_order_operation_id).one()
        assert operation.work_order_id == child["id"]

        # Manual nests live under the reusable parent-less "Manual entry" package.
        manual_package = db_session.query(LaserNestPackage).filter_by(id=nest.package_id).one()
        assert manual_package.package_name == "Manual entry"
        assert manual_package.parent_work_order_id is None
        assert manual_package.child_work_order_id == child["id"]

        # Ordered quantity re-derived over all non-deleted nests: 3 + 2 + 4.
        wo = db_session.query(WorkOrder).filter_by(id=child["id"]).one()
        assert float(wo.quantity_ordered) == 9.0

        # Audit row records the parent-less context.
        create_row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "laser_nest",
                AuditLog.action == "CREATE",
                AuditLog.resource_id == nest_id,
            )
            .one()
        )
        assert create_row.extra_data.get("source") == "manual"
        assert create_row.extra_data.get("parent_work_order_id") is None
        assert create_row.extra_data.get("child_work_order_id") == child["id"]

        # Invariant 2: the WO-level quantity re-derivation (5 -> 9 runs) is
        # audited on the work order itself, not just the nest CREATE row.
        update_row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order",
                AuditLog.action == "UPDATE",
                AuditLog.resource_id == child["id"],
            )
            .one()
        )
        assert update_row.old_values["quantity_ordered"] == 5.0
        assert update_row.new_values["quantity_ordered"] == 9.0
        assert update_row.extra_data.get("reason") == "manual_laser_nest_added"


# --------------------------------------------------------------------------- #
# CHECK constraint: part required unless laser_cutting
# --------------------------------------------------------------------------- #
class TestPartRequiredUnlessLaserCheck:
    def test_partless_production_wo_is_rejected(self, db_session):
        """The model-level CHECK is live on the create_all (SQLite) test DB: a
        part-less WO whose type defaults to 'production' must fail at flush."""
        n = _next()
        db_session.add(
            WorkOrder(
                work_order_number=f"WO-BAD-{n}",
                part_id=None,
                quantity_ordered=1,
                status=WorkOrderStatus.DRAFT,
                company_id=COMPANY_A,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_partless_laser_cutting_wo_persists(self, db_session):
        n = _next()
        wo = WorkOrder(
            work_order_number=f"WO-OK-{n}",
            part_id=None,
            work_order_type=WorkOrderType.LASER_CUTTING.value,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            company_id=COMPANY_A,
        )
        db_session.add(wo)
        db_session.commit()
        assert wo.id is not None
        assert wo.part_id is None

    def test_create_endpoint_still_requires_part_id(self, client, db_session):
        """The relaxation is READ-side only: ``WorkOrderCreate`` keeps part_id
        required, so POST /work-orders/ cannot mint a part-less WO -- not even a
        laser_cutting one. Part-less WOs are born ONLY via the standalone import."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        resp = client.post(
            "/api/v1/work-orders/",
            headers=headers_for(admin),
            json={"work_order_type": "laser_cutting", "quantity_ordered": 3, "priority": 5},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert db_session.query(WorkOrder).count() == 0


# --------------------------------------------------------------------------- #
# Null-part read paths (the schema relaxations)
# --------------------------------------------------------------------------- #
class TestNullPartSerialization:
    def test_list_and_detail_render_a_partless_wo(self, client, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        listing = client.get("/api/v1/work-orders/", headers=headers_for(admin))
        assert listing.status_code == status.HTTP_200_OK, listing.text
        rows = [row for row in listing.json() if row["id"] == child["id"]]
        assert len(rows) == 1
        assert rows[0]["part_id"] is None
        assert rows[0]["part_number"] is None
        assert rows[0]["part_name"] is None
        assert rows[0]["work_order_type"] == "laser_cutting"

        detail = client.get(f"/api/v1/work-orders/{child['id']}", headers=headers_for(admin))
        assert detail.status_code == status.HTTP_200_OK, detail.text
        assert detail.json()["part_id"] is None

    def test_shop_floor_queue_renders_partless_nest_operations(self, client, db_session):
        """The operator work-center queue must surface the standalone WO's nest
        operations with part fields as None (not 500)."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        resp = client.get(f"/api/v1/shop-floor/work-center-queue/{wc.id}", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        queue = resp.json()["queue"]
        mine = [item for item in queue if item["work_order_id"] == child["id"]]
        assert mine, f"expected the standalone WO's ready nest op in the queue, got {queue}"
        assert all(item["part_number"] is None for item in mine)
        assert all(item["part_name"] is None for item in mine)

    def test_shop_floor_operations_search_matches_partless_wo_number(self, client, db_session):
        """GET /shop-floor/operations?search= must not drop part-less WOs: the
        Part join is an OUTER join, so searching the standalone WO's number
        still returns its nest ops (with part fields None)."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)

        resp = client.get(
            "/api/v1/shop-floor/operations",
            params={"search": child["work_order_number"]},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        ops = resp.json()["operations"]
        mine = [op for op in ops if op["work_order_id"] == child["id"]]
        assert len(mine) == 2, f"expected both nest ops for the searched WO number, got {ops}"
        assert all(op["part_number"] is None for op in mine)
        assert all(op["part_name"] is None for op in mine)

    def test_shop_floor_operations_search_still_matches_part_number(self, client, db_session):
        """Regression for the INNER->OUTER Part join in get_all_operations: a
        part-NUMBER search must keep matching parted WOs' ops exactly as before
        (and a coexisting part-less standalone WO must not leak into it)."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        standalone = _import_standalone_wo(client, db_session, admin, wc)

        n = _next()
        part = Part(
            part_number=f"ASM-SEARCH-{n}",
            name="Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=COMPANY_A,
        )
        db_session.add(part)
        db_session.flush()
        parent = WorkOrder(
            work_order_number=f"WO-SEARCH-{n}",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=3,
            company_id=COMPANY_A,
        )
        db_session.add(parent)
        db_session.commit()
        # Classic flow: the laser child inherits the parent's part, giving us
        # parted nest ops to search for.
        resp = _wo_import(client, headers_for(admin), parent.id, _cnc_zip("NEST-S_QTY2.nc"), work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]

        resp = client.get(
            "/api/v1/shop-floor/operations",
            params={"search": part.part_number},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        ops = resp.json()["operations"]
        assert ops, "expected the parted child WO's ops to match a part-number search"
        assert {op["work_order_id"] for op in ops} == {child["id"]}
        assert standalone["id"] not in {op["work_order_id"] for op in ops}
        assert all(op["part_number"] == part.part_number for op in ops)

    def test_operator_can_start_a_nest_operation_on_the_partless_wo(self, client, db_session):
        """Downstream of the born-RELEASED state: the shop-floor START gate
        (terminal-status check, predecessor gate, qualification warn-gate, time
        entry) runs clean on an operation whose WO has no part -- no 500, WO
        moves to in_progress."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        child = _import_standalone_wo(client, db_session, admin, wc)
        operator = make_user(db_session, role=UserRole.OPERATOR)

        first_op = min(child["operations"], key=lambda op: op["sequence"])
        resp = client.put(f"/api/v1/shop-floor/operations/{first_op['id']}/start", headers=headers_for(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        wo = db_session.query(WorkOrder).filter_by(id=child["id"]).one()
        assert wo.status == WorkOrderStatus.IN_PROGRESS
        assert wo.part_id is None

    def test_wo_email_templates_render_partless_wo_with_nest_labeling(self):
        """Email surface for part-less laser WOs.

        CUI-safe redesign (§11.1), NOT a regression: ``work_order_late`` was rewritten to
        carry ONLY the WO identifier + days-late + critical flag -- no part/nest cells, it
        never touches ``work_order.part`` (the detail lives behind login). ``wo_released``
        still renders real part fields, or the app's 'Nest package' label for a part-less
        laser WO -- and must render either without raising.
        """
        from app.services.email_service import EmailService

        service = EmailService()

        # --- work_order_late: CUI-safe (identifier + days-late only) ---
        late_html = service._render_template(
            "work_order_late",
            {"work_order_number": "WO-000123", "days_late": 3, "critical": False, "base_url": "http://erp.test"},
        )
        assert "WO-000123" in late_html
        assert "3 day" in late_html
        # No part / nest detail leaks into the CUI-safe late email.
        assert "Nest package" not in late_html
        assert "PN-77" not in late_html

        # critical=True renders the CRITICAL banner (still no part detail).
        critical_html = service._render_template(
            "work_order_late",
            {"work_order_number": "WO-000999", "days_late": 10, "critical": True, "base_url": "http://erp.test"},
        )
        assert "CRITICAL" in critical_html
        assert "WO-000999" in critical_html

        # --- wo_released: part-less laser WO renders the 'Nest package' label (no raise) ---
        partless_wo = SimpleNamespace(
            id=1,
            wo_number="WO-000123",
            work_order_number="WO-000123",
            part=None,
            quantity=5,
            due_date="2026-07-01",
            status="released",
            priority=5,
        )
        released_partless = service._render_template(
            "wo_released", {"work_order": partless_wo, "base_url": "http://erp.test"}
        )
        assert "Nest package" in released_partless

        # A parted WO still renders its real part fields through wo_released.
        parted_wo = SimpleNamespace(
            id=2,
            wo_number="WO-000124",
            work_order_number="WO-000124",
            part=SimpleNamespace(part_number="PN-77", description="Bracket"),
            quantity=5,
            due_date="2026-07-01",
            status="released",
            priority=5,
        )
        released_parted = service._render_template(
            "wo_released", {"work_order": parted_wo, "base_url": "http://erp.test"}
        )
        assert "PN-77" in released_parted
        assert "Nest package" not in released_parted

    def test_scheduling_prioritize_tolerates_null_part_id(self, db_session):
        """Regression for the optimize_setup sort: two ops tied on priority +
        due date, one from a part-less laser WO, must not TypeError on the
        None-vs-int tuple compare."""
        service = SchedulingService(db_session)
        tied_due = date(2026, 8, 1)
        laser_op = SimpleNamespace(sequence=10, work_order=SimpleNamespace(priority=3, due_date=tied_due, part_id=None))
        part_op = SimpleNamespace(sequence=10, work_order=SimpleNamespace(priority=3, due_date=tied_due, part_id=42))
        ordered = service._prioritize_operations([part_op, laser_op], optimize_setup=True)
        # Part-less WOs coalesce to 0 and group first; no TypeError.
        assert ordered[0] is laser_op
        assert ordered[1] is part_op
