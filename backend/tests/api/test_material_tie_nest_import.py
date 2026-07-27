"""Material ties created SERVER-SIDE by the laser-nest paths (PR 2).

Before PR 2 a tie could only be created by hand through
``POST /work-orders/{id}/material-allocations``. Now the two nest paths create the
tie inside their own transaction, through one shared seam
(``laser_nest_service.create_nest_material_allocation``), so the package import and the
manual single-nest endpoint produce byte-identical rows AND identical hash-chain
entries:

  * ``POST /work-orders/{id}/laser-nest-packages/import`` -- per-row
    ``material_part_id`` / ``qty_per_run`` on the planner-confirmed rows;
  * ``POST /work-orders/{id}/laser-nests/manual`` -- the same two fields on the body.

What is pinned here:

1. **A row WITH ``material_part_id`` creates ONE OPEN, OPERATION-scoped tie** with
   ``source='nest'``, ``qty_planned = qty_per_run x planned_runs``, the part's UoM
   SNAPSHOTTED, no lot pin -- plus its ``work_order_material_allocation`` CREATE audit
   row (invariant 2: creating a tie is a state change on a tenant table).
2. **A row WITHOUT it creates NOTHING** -- no allocation row, no audit row. That is
   invariant 6(d) at the import boundary: an untied nest stays byte-identical to its
   pre-feature self.
3. **A cross-tenant ``material_part_id`` is 404, never 403** (so an id cannot be probed
   across tenants) and NOTHING is persisted -- the part is resolved BEFORE the rebuild
   wipes the prior nests, so a bad id cannot leave a work order half-destroyed.
4. **Re-import replaces ties without colliding with ``uq_wo_material_alloc_open_op``.**
   The superseded tie is CANCELLED and DETACHED (never deleted), so the partial unique
   index only ever sees one OPEN row per (company, operation, part).
5. **FOREIGN KEYS** (see ``sqlite_foreign_keys_enforced``): SQLite runs with
   ``PRAGMA foreign_keys`` OFF while production Postgres always enforces them, and that
   exact blind spot hid a production-only blocker in PR 1. PR 2 makes the IMPORT itself
   a producer of the FK that the re-import wipe then has to delete around, so that path
   gets its own pragma-enabled test with a positive control.
"""

import io
import json
import zipfile
from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.laser_nest_service import ParsedLaserNest, build_laser_nest_child_work_order
from tests.api.fk_test_helpers import sqlite_foreign_keys_enforced

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"mtnest-{n}@co{company_id}.test",
        employee_id=f"MTNEST-{n:05d}",
        first_name="Nest",
        last_name=f"Tie{n}",
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
        name=f"Laser {n}",
        code=f"MT-LASER-{n}",
        work_center_type="laser",
        hourly_rate=120,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_sheet_part(db: Session, *, company_id: int = COMPANY_A, uom: str = "sheets", is_deleted: bool = False) -> Part:
    """The MATERIAL part a nest consumes -- never the part being produced."""
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"MT-SHEET-{n:05d}",
        name=f"Sheet stock {n}",
        part_type="raw_material",
        unit_of_measure=uom,
        is_active=True,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_parent_work_order(db: Session, *, company_id: int = COMPANY_A) -> WorkOrder:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"MT-ASM-{n:05d}",
        name=f"Assembly {n}",
        part_type="assembly",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"MT-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _pdf_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"%PDF-1.4\n%stub nest report\n")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    """Local storage + laser-package roots under a tmp dir (both read UPLOAD_DIR)."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    """The confirm-and-commit import path must never re-run the AI extractor."""
    monkeypatch.setattr(
        work_orders_endpoint,
        "extract_nest_fields_from_pdf",
        lambda *a, **k: pytest.fail("the import path must not call the AI extractor"),
    )


def _import(client: TestClient, headers: dict, wo_id: int, zip_bytes: bytes, *, rows, work_center_id=None):
    data = {"rows": json.dumps(rows)}
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    return client.post(
        f"/api/v1/work-orders/{wo_id}/laser-nest-packages/import",
        headers=headers,
        data=data,
        files={"file": ("nests.zip", io.BytesIO(zip_bytes), "application/zip")},
    )


def _ties(db: Session, *, company_id: int = COMPANY_A) -> list[WorkOrderMaterialAllocation]:
    return (
        db.query(WorkOrderMaterialAllocation)
        .filter(WorkOrderMaterialAllocation.company_id == company_id)
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )


def _tie_audit_rows(db: Session) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order_material_allocation")
        .order_by(AuditLog.id)
        .all()
    )


# --------------------------------------------------------------------------- #
# Package import
# --------------------------------------------------------------------------- #
class TestPackageImportCreatesTheTie:
    def test_row_with_a_material_part_creates_an_open_operation_scoped_tie(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session, uom="sheets")

        rows = [
            {
                "source_file": "05749.pdf",
                "cnc_number": "05749",
                "planned_runs": 4,
                "material": "A36",
                "material_part_id": sheet.id,
                "qty_per_run": 2.0,
            }
        ]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        [tie] = _ties(db_session)
        child_id = resp.json()["child_work_order"]["id"]
        nest = (
            db_session.query(LaserNest)
            .join(LaserNestPackage)
            .filter(LaserNestPackage.child_work_order_id == child_id)
            .one()
        )

        assert tie.company_id == COMPANY_A
        assert tie.work_order_id == child_id, "the tie belongs to the CHILD laser WO, not the parent assembly"
        assert tie.work_order_operation_id == nest.work_order_operation_id
        assert tie.part_id == sheet.id
        assert tie.status == AllocationStatus.OPEN
        assert tie.source == AllocationSource.NEST
        assert tie.qty_per_run == 2.0
        # Run-scaled planning demand: 2 sheets/run x 4 planned runs.
        assert tie.qty_planned == 8.0
        assert tie.qty_consumed == 0.0
        # A SNAPSHOT of the part's UoM, so the tie stays readable after the part's
        # unit is changed. Stored as the lowercase enum VALUE, like Part persists it.
        assert tie.unit_of_measure == "sheets"
        # Ships UNPINNED by design -- FIFO picks the lot at consume time.
        assert tie.pinned_inventory_item_id is None
        assert tie.pinned_lot_number is None
        assert tie.created_by == admin.id

    def test_the_tie_is_written_to_the_tamper_evident_chain(self, client: TestClient, db_session: Session):
        """Invariant 2: creating a tie is a state change on a tenant table, so
        ``audit`` is a REQUIRED parameter on the creation seam -- there is no caller
        for whom an unaudited tie is correct."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session)

        rows = [
            {
                "source_file": "05749.pdf",
                "cnc_number": "05749",
                "planned_runs": 3,
                "material_part_id": sheet.id,
            }
        ]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        [tie] = _ties(db_session)
        [row] = _tie_audit_rows(db_session)
        assert row.action == "CREATE"
        assert row.resource_id == tie.id
        assert row.company_id == COMPANY_A
        assert row.extra_data["work_order_id"] == tie.work_order_id
        assert row.extra_data["work_order_operation_id"] == tie.work_order_operation_id
        assert row.extra_data["part_id"] == sheet.id
        assert row.extra_data["source"] == "nest"
        assert row.extra_data["pinned_inventory_item_id"] is None
        assert sheet.part_number in row.description

    def test_qty_per_run_defaults_to_one(self, client: TestClient, db_session: Session):
        """The headline nest case: one sheet per run. A planner who names a part
        without a quantity means 1, not 0 and not "unset"."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session)

        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 6, "material_part_id": sheet.id}]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        [tie] = _ties(db_session)
        assert tie.qty_per_run == 1.0
        assert tie.qty_planned == 6.0

    def test_row_without_a_material_part_creates_no_tie_at_all(self, client: TestClient, db_session: Session):
        """Invariant 6(d) at the import boundary: an untied nest is byte-identical
        to its pre-feature self -- no allocation row, NO AUDIT ROW."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material": "A36"},
            {"source_file": "05750.pdf", "cnc_number": "05750", "planned_runs": 2},
        ]
        resp = _import(
            client, headers_for(admin), parent.id, _pdf_zip("05749.pdf", "05750.pdf"), rows=rows, work_center_id=wc.id
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        assert _ties(db_session) == []
        assert _tie_audit_rows(db_session) == []

    def test_a_mixed_package_ties_only_the_rows_that_asked_for_it(self, client: TestClient, db_session: Session):
        """Per-ROW, not per-package: tying is opt-in one nest at a time."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session)

        rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material_part_id": sheet.id},
            {"source_file": "05750.pdf", "cnc_number": "05750", "planned_runs": 2},
        ]
        resp = _import(
            client, headers_for(admin), parent.id, _pdf_zip("05749.pdf", "05750.pdf"), rows=rows, work_center_id=wc.id
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        ties = _ties(db_session)
        assert len(ties) == 1
        tied_nest = db_session.query(LaserNest).filter(LaserNest.cnc_number == "05749").one()
        assert ties[0].work_order_operation_id == tied_nest.work_order_operation_id

    def test_two_rows_may_share_one_material_part(self, client: TestClient, db_session: Session):
        """``uq_wo_material_alloc_open_op`` is keyed on (company, OPERATION, part),
        so two nests cutting the same sheet are two separate, legitimate ties."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session)

        rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material_part_id": sheet.id},
            {"source_file": "05750.pdf", "cnc_number": "05750", "planned_runs": 2, "material_part_id": sheet.id},
        ]
        resp = _import(
            client, headers_for(admin), parent.id, _pdf_zip("05749.pdf", "05750.pdf"), rows=rows, work_center_id=wc.id
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        ties = _ties(db_session)
        assert len(ties) == 2
        assert {t.part_id for t in ties} == {sheet.id}
        assert len({t.work_order_operation_id for t in ties}) == 2
        assert sorted(t.qty_planned for t in ties) == [2.0, 3.0]


class TestPackageImportRefusals:
    def test_a_material_part_from_another_tenant_is_404_and_persists_nothing(
        self, client: TestClient, db_session: Session
    ):
        """A tie naming another company's material is a security defect, not a 404
        nuisance -- and the refusal is 404, NEVER 403, so a part id cannot be probed
        for existence across tenants.

        The part is resolved BEFORE the atomic rebuild, so nothing is destroyed:
        the work order still has no nests, no package, and no tie afterwards.
        """
        admin_a = make_user(db_session, company_id=COMPANY_A)
        wc = make_laser_work_center(db_session, company_id=COMPANY_A)
        parent = make_parent_work_order(db_session, company_id=COMPANY_A)
        foreign_sheet = make_sheet_part(db_session, company_id=COMPANY_B)

        rows = [
            {
                "source_file": "05749.pdf",
                "cnc_number": "05749",
                "planned_runs": 3,
                "material_part_id": foreign_sheet.id,
            }
        ]
        resp = _import(client, headers_for(admin_a), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert resp.status_code != status.HTTP_403_FORBIDDEN
        assert _ties(db_session, company_id=COMPANY_A) == []
        assert _ties(db_session, company_id=COMPANY_B) == []
        assert db_session.query(LaserNest).count() == 0
        assert db_session.query(LaserNestPackage).count() == 0

    def test_a_nonexistent_material_part_is_404(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material_part_id": 999_999}]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert _ties(db_session) == []

    def test_a_soft_deleted_material_part_is_404(self, client: TestClient, db_session: Session):
        """A tie to a deleted part would advertise demand nothing can satisfy."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        deleted_sheet = make_sheet_part(db_session, is_deleted=True)

        rows = [
            {
                "source_file": "05749.pdf",
                "cnc_number": "05749",
                "planned_runs": 3,
                "material_part_id": deleted_sheet.id,
            }
        ]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert _ties(db_session) == []

    def test_qty_per_run_without_a_material_part_is_refused(self, client: TestClient, db_session: Session):
        """A per-run quantity with nothing to consume is meaningless -- and
        silently dropping it would let a planner believe material will deplete
        when no tie exists at all."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "qty_per_run": 2.0}]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)

        assert resp.status_code in (
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ), resp.text
        assert _ties(db_session) == []


class TestReimportReplacesTies:
    def test_reimport_cancels_and_detaches_the_old_tie_and_creates_a_new_one(
        self, client: TestClient, db_session: Session
    ):
        """The partial index ``uq_wo_material_alloc_open_op`` permits ONE OPEN row
        per (company, operation, part). A re-import must therefore cancel first and
        create second -- and the superseded row is DETACHED
        (``work_order_operation_id`` cleared) so the operation it pointed at can be
        physically deleted.
        """
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session)

        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material_part_id": sheet.id}]
        first = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert first.status_code == status.HTTP_200_OK, first.text
        [original] = _ties(db_session)
        original_id = original.id
        original_operation_id = original.work_order_operation_id

        rebuilt_rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749-REV-B", "planned_runs": 5, "material_part_id": sheet.id}
        ]
        second = _import(
            client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rebuilt_rows, work_center_id=wc.id
        )
        assert second.status_code == status.HTTP_200_OK, second.text

        db_session.expire_all()
        ties = _ties(db_session)
        assert len(ties) == 2, "the superseded tie is CANCELLED, never deleted"
        superseded = db_session.get(WorkOrderMaterialAllocation, original_id)
        assert superseded.status == AllocationStatus.CANCELLED
        # Detached: the FK carries no ON DELETE, so the column has to be cleared
        # before the operation row can go.
        assert superseded.work_order_operation_id is None

        [live] = [t for t in ties if t.status == AllocationStatus.OPEN]
        assert live.id != original_id
        # Scoped to the REBUILT nest's operation. Asserted against that operation
        # rather than "!= the old id": SQLite reuses a deleted row's rowid, so the
        # fresh operation can legitimately land on the same integer here even
        # though the row itself is new (Postgres would hand out a new id).
        rebuilt_nest = db_session.query(LaserNest).filter(LaserNest.cnc_number == "05749-REV-B").one()
        assert live.work_order_operation_id == rebuilt_nest.work_order_operation_id
        assert live.work_order_operation_id is not None
        assert original_operation_id is not None
        assert live.qty_planned == 5.0
        # The original scope survives on the hash chain even though the column is
        # cleared, so the tie is not read back as one that was always WO-scoped.
        cancel_rows = [r for r in _tie_audit_rows(db_session) if r.resource_id == original_id and r.action != "CREATE"]
        assert cancel_rows, "the supersede must be recorded on the chain"
        assert any((r.extra_data or {}).get("reason") == "superseded_by_reimport" for r in cancel_rows)

    def test_reimport_of_a_tied_nest_into_an_untied_one_leaves_no_live_tie(
        self, client: TestClient, db_session: Session
    ):
        """Removing the sheet pick on re-import really unties the work: the old tie
        is cancelled and no new one takes its place."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session)

        tied = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material_part_id": sheet.id}]
        assert (
            _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=tied, work_center_id=wc.id)
        ).status_code == status.HTTP_200_OK

        untied = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3}]
        second = _import(
            client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=untied, work_center_id=wc.id
        )
        assert second.status_code == status.HTTP_200_OK, second.text

        db_session.expire_all()
        assert [t.status for t in _ties(db_session)] == [AllocationStatus.CANCELLED]


# --------------------------------------------------------------------------- #
# Manual single-nest endpoint -- the same seam
# --------------------------------------------------------------------------- #
class TestManualNestCreatesTheSameTie:
    def _create(self, client: TestClient, headers: dict, parent_id: int, body: dict):
        return client.post(f"/api/v1/work-orders/{parent_id}/laser-nests/manual", headers=headers, json=body)

    def test_manual_nest_with_a_material_part_creates_the_tie(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        sheet = make_sheet_part(db_session, uom="sheets")

        resp = self._create(
            client,
            headers_for(admin),
            parent.id,
            {"cnc_number": "PRG-500", "planned_runs": 4, "material_part_id": sheet.id, "qty_per_run": 3.0},
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        [tie] = _ties(db_session)
        nest = db_session.query(LaserNest).filter(LaserNest.id == resp.json()["id"]).one()
        assert tie.work_order_operation_id == nest.work_order_operation_id
        assert tie.part_id == sheet.id
        assert tie.source == AllocationSource.NEST
        assert tie.status == AllocationStatus.OPEN
        assert tie.qty_per_run == 3.0
        assert tie.qty_planned == 12.0
        assert tie.unit_of_measure == "sheets"
        # Same seam => same chain entry shape as the import path.
        [row] = _tie_audit_rows(db_session)
        assert row.action == "CREATE"
        assert row.extra_data["source"] == "nest"

    def test_manual_nest_without_a_material_part_creates_no_tie(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        resp = self._create(client, headers_for(admin), parent.id, {"cnc_number": "PRG-501", "planned_runs": 4})
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        assert _ties(db_session) == []
        assert _tie_audit_rows(db_session) == []

    def test_manual_nest_with_another_tenants_part_is_404_and_creates_no_nest(
        self, client: TestClient, db_session: Session
    ):
        """404 (never 403), and the refusal lands BEFORE the transaction, so no
        half-built nest is left behind."""
        admin_a = make_user(db_session, company_id=COMPANY_A)
        make_laser_work_center(db_session, company_id=COMPANY_A)
        parent = make_parent_work_order(db_session, company_id=COMPANY_A)
        foreign_sheet = make_sheet_part(db_session, company_id=COMPANY_B)

        resp = self._create(
            client,
            headers_for(admin_a),
            parent.id,
            {"cnc_number": "PRG-502", "planned_runs": 2, "material_part_id": foreign_sheet.id},
        )

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert _ties(db_session, company_id=COMPANY_A) == []
        assert db_session.query(LaserNest).count() == 0

    def test_manual_nest_qty_per_run_without_a_part_is_422(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        resp = self._create(
            client, headers_for(admin), parent.id, {"cnc_number": "PRG-503", "planned_runs": 2, "qty_per_run": 2.0}
        )

        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
        assert _ties(db_session) == []


# --------------------------------------------------------------------------- #
# Foreign keys -- the re-import wipe, now reachable straight from the import
# --------------------------------------------------------------------------- #


def _child_laser_work_order(db: Session, *, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    part = Part(
        part_number=f"MT-CHILD-{n:05d}",
        name=f"Child {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"MT-CHILD-WO-{n:05d}",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        work_order_type="laser_cutting",
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def test_reimport_of_a_tied_package_survives_foreign_key_enforcement(db_session: Session):
    """The PR-1 blind spot, re-armed for the path PR 2 opened.

    SQLite defaults ``PRAGMA foreign_keys`` OFF while production Postgres always
    enforces them, which is how a nest-re-import FK violation shipped through a
    first review in PR 1. PR 2 makes the IMPORT ITSELF the producer of
    ``work_order_material_allocations.work_order_operation_id`` -- so a planner who
    ties a sheet on import and then re-imports the package is now walking the
    headline flow straight into that FK.

    ``build_laser_nest_child_work_order`` is exercised directly (rather than over
    HTTP) so the pragma applies to the exact statements the rebuild runs. The
    POSITIVE CONTROL below is what makes this non-vacuous: with the tie still
    attached, deleting its operation really does raise -- proving FK enforcement is
    live for this test rather than silently off, which is the suite-wide default.
    """
    user = make_user(db_session)
    wc = make_laser_work_center(db_session)
    parent = make_parent_work_order(db_session)
    child = _child_laser_work_order(db_session)
    sheet = make_sheet_part(db_session)
    audit = AuditService(db_session, user)

    def _build(cnc: str, runs: int) -> LaserNestPackage:
        package = build_laser_nest_child_work_order(
            db_session,
            parent_work_order=parent,
            child_work_order=child,
            package_name=f"Package {cnc}",
            package_source_path=None,
            nests=[
                ParsedLaserNest(
                    nest_name=cnc,
                    cnc_file_name=f"{cnc}.nc",
                    cnc_file_path=None,
                    cnc_number=cnc,
                    planned_runs=runs,
                    material="A36",
                    material_part_id=sheet.id,
                    qty_per_run=1.0,
                )
            ],
            laser_work_center=wc,
            company_id=COMPANY_A,
            created_by=user.id,
            row_material_parts={sheet.id: sheet},
            audit=audit,
        )
        db_session.commit()
        return package

    _build("CNC-001", 3)
    [first_tie] = _ties(db_session)
    first_tie_id = first_tie.id
    first_operation_id = first_tie.work_order_operation_id
    assert first_operation_id is not None

    with sqlite_foreign_keys_enforced(db_session):
        assert (
            db_session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        ), "FK enforcement must be live for this test to mean anything"

        # POSITIVE CONTROL: the import-created tie really does hold the FK, so
        # deleting its operation without detaching first FK-violates.
        nested = db_session.begin_nested()
        with pytest.raises(IntegrityError):
            db_session.delete(db_session.get(WorkOrderOperation, first_operation_id))
            db_session.flush()
        nested.rollback()

        # THE REAL PATH: the re-import cancels + detaches, wipes the operations,
        # and creates the replacement tie -- all under enforced foreign keys.
        _build("CNC-001-REV-B", 5)

        db_session.expire_all()
        superseded = db_session.get(WorkOrderMaterialAllocation, first_tie_id)
        assert superseded is not None, "the tie row must survive the operation delete"
        assert superseded.status == AllocationStatus.CANCELLED
        assert superseded.work_order_operation_id is None

        # The wipe really replaced the operation set -- asserted by COUNT rather
        # than by "the old id is gone", because SQLite hands a deleted row's rowid
        # straight back to the next INSERT (Postgres would not).
        laser_ops = (
            db_session.query(WorkOrderOperation)
            .filter(
                WorkOrderOperation.work_order_id == child.id,
                WorkOrderOperation.operation_group == "LASER",
            )
            .all()
        )
        assert len(laser_ops) == 1
        rebuilt_nest = db_session.query(LaserNest).filter(LaserNest.cnc_number == "CNC-001-REV-B").one()
        assert rebuilt_nest.work_order_operation_id == laser_ops[0].id

        [live] = [t for t in _ties(db_session) if t.status == AllocationStatus.OPEN]
        assert live.qty_planned == 5.0
        assert live.work_order_operation_id == laser_ops[0].id
