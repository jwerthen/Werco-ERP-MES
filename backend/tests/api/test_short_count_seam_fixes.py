"""Seam fixes on the stacked Wave 2 tree (short count + floor gates), each pinned on the real route.

* ``TestOperationPutStartGateAnswersFirst`` (F1) -- ``PUT /work-orders/operations/{id}`` on a draft
  or held job: a ``quantity_complete`` raise on a not-started operation, or a hand-set start, gets
  the start gate's "release first" / "Resume Work Order" text -- not the open-operation count text,
  whose doors (clock-out, Report production, the office Complete) that job keeps shut. On a released
  job the count text is unchanged, and so is a COMPLETE operation's completion-record lock.
* ``TestApiTokenOrderLoweringReadsLabor`` (F2) -- a standing API token lowering ``quantity_ordered``
  is refused once any operation has recorded good, scrap, a start OR LABOR: the one per-operation
  rule (``operation_has_recorded_work``) the nest lowering already used, not an inline twin that
  ignored labor.
* ``TestApiTokenNestReimportOverRecordedWork`` (F3) -- a standing API token may not re-import a nest
  package over nest operations with recorded work: IMPORT REPLACES EVERYTHING deletes them and zeroes
  the laser work order's counts, erasing a person's good, scrap and any declared short. This PR's
  extension of the 2026-11-07 decision, not part of it. A person's re-import, and a token's import
  over un-worked nests, are byte-identical to before. Review fixes pinned there too: the refusal has
  its own text (``API_TOKEN_NEST_REIMPORT``); an office start (``actual_start``) and the header counts
  the import zeroes are recorded work; the re-check reads the database, not the first check's loaded
  rows; a row changed mid-import -- the laser work order's own header included, which is flushed,
  version-checked and row-locked before the first audit row -- answers the app-wide 409 text; a
  person's import never resolves the laser child for the token check. The import locks in the
  floor's SFI-1 order -- the operations it replaces, then the work order, then the audit chain --
  so the token re-check judges the locked rows. It takes the work order only when free: a writer
  holding it (one that locks the work order before its operations) makes the import release the
  operations and wait, never queue on it with them held. When it is replacing existing operations it
  takes the audit-chain lock as its own statement before the first audit row, so a lock race it
  loses on Postgres (40P01 / 40001) -- at the chain lock included -- answers the same 409 rather
  than the 400 for a conflicting nest row; an import that replaces nothing (standalone, a parent's
  first import) leaves the chain lock to its first audit row, after the rebuild.

Every refusal is pinned as writing NOTHING.
"""

import base64
import io
import json
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

import app.api.endpoints.work_orders as work_orders_endpoint
from app.db.locks import is_lost_lock_race
from app.db.mixins import STALE_WRITE_CONFLICT_DETAIL
from app.main import app
from app.mcp.catalog import build_catalog
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.operational_event import OperationalEvent
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services import laser_nest_service
from app.services.audit_service import AuditService
from app.services.laser_nest_service import import_would_wipe_recorded_work, laser_import_wipe_operation_ids
from app.services.operation_action_gates import (
    API_TOKEN_COUNT_JUDGMENT,
    API_TOKEN_NEST_REIMPORT,
    MSG_WORK_ORDER_NOT_RELEASED,
    MSG_WORK_ORDER_ON_HOLD,
    completed_counts_locked_message,
    open_count_above_ceiling_message,
    open_count_above_recorded_message,
    operation_has_recorded_work,
)
from app.services.work_order_state_service import operation_accounting
from tests.api.kiosk_test_helpers import COMPANY_A, make_user, make_work_center, user_headers
from tests.api.short_count_helpers import add_entry, fresh, make_part, make_routing, set_op
from tests.api.test_short_count_cross_review_fixes import _refused, _token, _world
from tests.test_mcp_server import call, make_server

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OPERATION_PUT_URL = "/api/v1/work-orders/operations/{op}"
WORK_ORDER_URL = "/api/v1/work-orders/{wo}"
WO_IMPORT_URL = "/api/v1/work-orders/{wo}/laser-nest-packages/import"
STANDALONE_IMPORT_URL = "/api/v1/work-orders/laser-nest-packages/standalone/import"

DONE = OperationStatus.COMPLETE
OPEN = OperationStatus.IN_PROGRESS
READY = OperationStatus.READY
PENDING = OperationStatus.PENDING
HELD = OperationStatus.ON_HOLD


@pytest.fixture(autouse=True)
def _no_completion_signal_enqueue(monkeypatch):
    """Skip the post-commit ARQ enqueue (it retries a missing Redis for seconds)."""
    monkeypatch.setattr("app.api.endpoints.work_orders.enqueue_work_order_completion_signals", lambda **_: None)
    monkeypatch.setattr("app.api.endpoints.shop_floor.enqueue_work_order_completion_signals", lambda **_: None)


@pytest.fixture
def supervisor(db_session: Session) -> User:
    return make_user(db_session, role=UserRole.SUPERVISOR, first_name="Sue", last_name="Visor")


def _put_op(client: TestClient, op: WorkOrderOperation, headers: dict, **fields):
    return client.put(OPERATION_PUT_URL.format(op=op.id), json={"version": op.version, **fields}, headers=headers)


def _with_release_record(db: Session, wo: WorkOrder, releaser: User) -> WorkOrder:
    """A held job that WAS released -- the only kind of hold the PUT can create since ruling 2."""
    wo.released_at = datetime.utcnow() - timedelta(days=1)
    wo.released_by = releaser.id
    db.commit()
    db.refresh(wo)
    return wo


# --------------------------------------------------------------------------- #
# F1 -- the operation PUT answers the start gate before the open-operation count checks
# --------------------------------------------------------------------------- #
class TestOperationPutStartGateAnswersFirst:
    def _counted(self, db: Session, *, wo_status: WorkOrderStatus, op_status=READY, releaser=None):
        """One operation, not started, carrying Ann's closed entry of 3 good and quantity_complete 3."""
        ann = make_user(db, first_name="Ann")
        wo, (op,) = make_routing(db, work_centers=[make_work_center(db)], statuses=[op_status], wo_status=wo_status)
        if releaser is not None:
            wo = _with_release_record(db, wo, releaser)
        add_entry(db, ann, op, produced=3)
        set_op(db, op, quantity_complete=3)
        return wo, op

    @pytest.mark.parametrize("op_status", [READY, PENDING])
    def test_a_held_job_gets_resume_not_the_recorded_count_text(self, client, db_session, supervisor, op_status):
        _, op = self._counted(db_session, wo_status=WorkOrderStatus.ON_HOLD, op_status=op_status, releaser=supervisor)
        before = _world(db_session)

        resp = _put_op(client, op, user_headers(supervisor), quantity_complete=5)
        _refused(db_session, resp, before, status=409, detail=MSG_WORK_ORDER_ON_HOLD)

    @pytest.mark.parametrize("op_status", [READY, PENDING])
    def test_a_draft_job_gets_release_first_not_the_recorded_count_text(
        self, client, db_session, supervisor, op_status
    ):
        _, op = self._counted(db_session, wo_status=WorkOrderStatus.DRAFT, op_status=op_status)
        before = _world(db_session)

        resp = _put_op(client, op, user_headers(supervisor), quantity_complete=5)
        _refused(db_session, resp, before, status=409, detail=MSG_WORK_ORDER_NOT_RELEASED)

    def test_a_hand_set_start_with_a_raise_on_a_held_job_gets_the_start_gate(self, client, db_session, supervisor):
        _, op = self._counted(db_session, wo_status=WorkOrderStatus.ON_HOLD, releaser=supervisor)
        before = _world(db_session)

        resp = _put_op(client, op, user_headers(supervisor), status="in_progress", quantity_complete=5)
        _refused(db_session, resp, before, status=409, detail=MSG_WORK_ORDER_ON_HOLD)

    def test_on_a_released_job_the_same_raise_keeps_the_recorded_count_text(self, client, db_session, supervisor):
        _, op = self._counted(db_session, wo_status=WorkOrderStatus.RELEASED)
        before = _world(db_session)

        resp = _put_op(client, op, user_headers(supervisor), quantity_complete=5)
        _refused(
            db_session,
            resp,
            before,
            status=409,
            detail=open_count_above_recorded_message(fresh(db_session, WorkOrderOperation, op.id), 5, 3),
        )

    def _short_upstream(self, db: Session, *, wo_status: WorkOrderStatus, releaser=None):
        """op10 closed 8 good + 2 scrap with 2 declared short; op20 READY, nothing recorded (ceiling 8)."""
        wo, (op10, op20) = make_routing(
            db,
            work_centers=[make_work_center(db), make_work_center(db)],
            statuses=[DONE, READY],
            wo_status=wo_status,
        )
        if releaser is not None:
            wo = _with_release_record(db, wo, releaser)
        set_op(db, op10, quantity_complete=8, quantity_scrapped=2, scrap_reason="burr", quantity_short_declared=2)
        return wo, op20

    def test_a_held_job_gets_resume_not_the_ceiling_text(self, client, db_session, supervisor):
        _, op20 = self._short_upstream(db_session, wo_status=WorkOrderStatus.ON_HOLD, releaser=supervisor)
        before = _world(db_session)

        resp = _put_op(client, op20, user_headers(supervisor), quantity_complete=9)
        _refused(db_session, resp, before, status=409, detail=MSG_WORK_ORDER_ON_HOLD)

    def test_on_a_released_job_the_ceiling_text_is_unchanged(self, client, db_session, supervisor):
        wo, op20 = self._short_upstream(db_session, wo_status=WorkOrderStatus.RELEASED)
        acct = operation_accounting(fresh(db_session, WorkOrder, wo.id), op20, good=9)
        assert acct.exceeds_ceiling
        before = _world(db_session)

        resp = _put_op(client, op20, user_headers(supervisor), quantity_complete=9)
        _refused(db_session, resp, before, status=409, detail=open_count_above_ceiling_message(op20, 9, acct))

    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.RELEASED, WorkOrderStatus.ON_HOLD])
    def test_a_complete_operations_lock_text_is_unchanged(self, client, db_session, supervisor, wo_status):
        wo, (op,) = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[DONE], wo_status=wo_status
        )
        if wo_status == WorkOrderStatus.ON_HOLD:
            _with_release_record(db_session, wo, supervisor)
        set_op(db_session, op, quantity_complete=10)
        before = _world(db_session)

        resp = _put_op(client, op, user_headers(supervisor), quantity_complete=11)
        _refused(db_session, resp, before, status=409, detail=completed_counts_locked_message(op))


# --------------------------------------------------------------------------- #
# F2 -- a token's quantity_ordered lowering reads labor, through the one predicate
# --------------------------------------------------------------------------- #
class TestApiTokenOrderLoweringReadsLabor:
    def _labor_only(self, db: Session, op_status: OperationStatus):
        """One operation with a closed entry of ZERO pieces and no counts, left at ``op_status``."""
        ann = make_user(db, first_name="Ann")
        wo, (op,) = make_routing(db, work_centers=[make_work_center(db)], statuses=[op_status])
        add_entry(db, ann, op, produced=0)
        return fresh(db, WorkOrder, wo.id), op

    def _lower(self, client, wo: WorkOrder, headers: dict, to: float = 8):
        return client.put(
            WORK_ORDER_URL.format(wo=wo.id), json={"version": wo.version, "quantity_ordered": to}, headers=headers
        )

    @pytest.mark.parametrize("op_status", [PENDING, HELD])
    def test_a_token_lowering_over_labor_only_is_refused_writing_nothing(
        self, client, db_session, admin_headers, supervisor, op_status
    ):
        wo, op = self._labor_only(db_session, op_status)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = self._lower(client, wo, headers)
        _refused(db_session, resp, before, status=403, detail=API_TOKEN_COUNT_JUDGMENT)
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_ordered) == 10

    @pytest.mark.parametrize("op_status", [PENDING, HELD])
    def test_a_person_may_still_lower_it(self, client, db_session, supervisor, op_status):
        wo, _ = self._labor_only(db_session, op_status)
        resp = self._lower(client, wo, user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_ordered) == 8

    def test_a_person_lowering_below_the_floor_is_still_409(self, client, db_session, supervisor):
        ann = make_user(db_session, first_name="Ann")
        wo, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)
        resp = self._lower(client, fresh(db_session, WorkOrder, wo.id), user_headers(supervisor), to=6)
        assert resp.status_code == 409, resp.text

    def test_a_token_lowering_an_untouched_order_still_works(self, client, db_session, admin_headers, supervisor):
        wo, _ = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[READY])
        resp = self._lower(client, fresh(db_session, WorkOrder, wo.id), _token(client, admin_headers, supervisor))
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_ordered) == 8


# --------------------------------------------------------------------------- #
# F3 -- a token may not re-import a nest package over worked nest operations
# --------------------------------------------------------------------------- #
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


def _import(client, headers, url: str, zip_bytes: bytes, *, rows=None):
    data = {"rows": json.dumps(rows)} if rows is not None else {}
    return client.post(
        url, headers=headers, data=data, files={"file": ("nests.zip", io.BytesIO(zip_bytes), "application/zip")}
    )


def _laser_center(db: Session) -> WorkCenter:
    center = WorkCenter(
        name="Ermaksan Fiber Laser",
        code=f"LSR-SEAM-{db.query(WorkCenter).count() + 1}",
        work_center_type="laser",
        hourly_rate=120,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(center)
    db.commit()
    db.refresh(center)
    return center


def _nest_ops(db: Session, laser_wo_id: int) -> list:
    db.expire_all()
    return (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.work_order_id == laser_wo_id)
        .order_by(WorkOrderOperation.sequence)
        .all()
    )


def _work(db: Session, operation: WorkOrderOperation, recorded: str) -> None:
    """Record ``recorded`` work on a nest operation: a run, labor alone, or scrap alone."""
    if recorded in ("run", "labor"):
        operator = make_user(db, first_name="Olly")
        add_entry(db, operator, operation, produced=1.0 if recorded == "run" else 0.0)
    if recorded == "run":
        set_op(db, operation, status=OPEN, quantity_complete=1.0)
    if recorded == "scrap":
        set_op(db, operation, quantity_scrapped=1.0, scrap_reason="burr")


def _nest_world(db: Session) -> dict:
    """Every row a nest import writes, plus the append-only tables' sizes."""
    db.expire_all()
    return {
        "work_orders": [
            (
                wo.id,
                wo.status,
                float(wo.quantity_ordered or 0),
                float(wo.quantity_complete or 0),
                float(wo.quantity_scrapped or 0),
                wo.released_at,
                wo.released_by,
                wo.version,
            )
            for wo in db.query(WorkOrder).order_by(WorkOrder.id).all()
        ],
        "operations": [
            (
                op.id,
                op.work_order_id,
                op.status,
                float(op.quantity_complete or 0),
                float(op.quantity_scrapped or 0),
                op.scrap_reason,
                op.quantity_short_declared,
                op.version,
            )
            for op in db.query(WorkOrderOperation).order_by(WorkOrderOperation.id).all()
        ],
        "nests": [
            (n.id, n.package_id, n.work_order_operation_id, n.planned_runs, n.is_deleted)
            for n in db.query(LaserNest).order_by(LaserNest.id).all()
        ],
        "packages": [(p.id, p.child_work_order_id) for p in db.query(LaserNestPackage).order_by(LaserNestPackage.id)],
        "entries": [(e.id, e.operation_id) for e in db.query(TimeEntry).order_by(TimeEntry.id).all()],
        "documents": db.query(Document).count(),
        "audit_rows": db.query(AuditLog).count(),
        "events": db.query(OperationalEvent).count(),
    }


NEST_PACKAGE_CONFLICT = (
    "Could not import the nest package; a nest conflicts with an existing record "
    "or a value is invalid. Review the rows and try again."
)


class _DriverError(Exception):
    """A stand-in for a psycopg2 error: SQLAlchemy wraps the driver's exception and keeps it as ``.orig``."""

    def __init__(self, pgcode):
        super().__init__(f"driver error {pgcode}")
        self.pgcode = pgcode


@pytest.mark.parametrize(
    "exc, lost",
    [
        (OperationalError("SELECT 1", {}, _DriverError("40P01")), True),
        (OperationalError("SELECT 1", {}, _DriverError("40001")), True),
        (OperationalError("SELECT 1", {}, _DriverError("57014")), False),
        (OperationalError("SELECT 1", {}, _DriverError(None)), False),
        (OperationalError("SELECT 1", {}, Exception("no pgcode attribute")), False),
        (StaleDataError("version mismatch"), False),
        (ValueError("40P01"), False),
    ],
)
def test_is_lost_lock_race_reads_the_driver_sqlstate_only(exc, lost):
    assert is_lost_lock_race(exc) is lost


@pytest.fixture
def no_ai(monkeypatch):
    monkeypatch.setattr(
        work_orders_endpoint,
        "extract_nest_fields_from_pdf",
        lambda *a, **k: pytest.fail("the nest import commit path must never call the AI extractor"),
    )


@pytest.fixture
def extraction_calls(monkeypatch):
    """Records every package extraction, so a refusal can be pinned as running before it."""
    calls: list = []
    original = work_orders_endpoint.extract_laser_nest_zip

    def _recording(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(work_orders_endpoint, "extract_laser_nest_zip", _recording)
    return calls


@pytest.fixture(autouse=True)
def _upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


class TestApiTokenNestReimportOverRecordedWork:
    def _standalone(self, client, db: Session, admin: User) -> WorkOrder:
        """A person's standalone import of two nests: the laser WO a re-import is addressed to."""
        _laser_center(db)
        resp = _import(client, user_headers(admin), STANDALONE_IMPORT_URL, _cnc_zip("N1_QTY2.nc", "N2_QTY3.nc"))
        assert resp.status_code == 200, resp.text
        return fresh(db, WorkOrder, resp.json()["child_work_order"]["id"])

    def _parent(self, db: Session) -> WorkOrder:
        """A released production parent with no laser child yet, and a laser work center to import onto."""
        _laser_center(db)
        part = make_part(db)
        parent = WorkOrder(
            work_order_number=f"SEAM-PARENT-{part.id}",
            part_id=part.id,
            quantity_ordered=5,
            status=WorkOrderStatus.RELEASED,
            priority=3,
            company_id=COMPANY_A,
        )
        db.add(parent)
        db.commit()
        return parent

    def _classic(self, client, db: Session, admin: User) -> tuple[WorkOrder, WorkOrder]:
        """A production parent and the laser child a person's import created under it."""
        parent = self._parent(db)
        resp = _import(client, user_headers(admin), WO_IMPORT_URL.format(wo=parent.id), _cnc_zip("N1_QTY2.nc"))
        assert resp.status_code == 200, resp.text
        return fresh(db, WorkOrder, parent.id), fresh(db, WorkOrder, resp.json()["child_work_order"]["id"])

    @pytest.mark.parametrize("recorded", ["run", "labor", "scrap"])
    def test_a_token_reimport_over_a_worked_nest_is_refused_writing_nothing(
        self, client, db_session, admin_user, admin_headers, supervisor, recorded, no_ai, extraction_calls
    ):
        laser = self._standalone(client, db_session, admin_user)
        _work(db_session, _nest_ops(db_session, laser.id)[0], recorded)
        headers = _token(client, admin_headers, supervisor)
        extraction_calls.clear()
        before = _nest_world(db_session)

        resp = _import(client, headers, WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9_QTY1.nc"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NEST_REIMPORT
        assert _nest_world(db_session) == before
        assert extraction_calls == [], "refused before the package was unpacked"

    def test_the_pdf_confirm_and_commit_path_is_refused_too(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai, extraction_calls
    ):
        laser = self._standalone(client, db_session, admin_user)
        _work(db_session, _nest_ops(db_session, laser.id)[1], "labor")
        headers = _token(client, admin_headers, supervisor)
        extraction_calls.clear()
        before = _nest_world(db_session)

        rows = [{"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2}]
        resp = _import(client, headers, WO_IMPORT_URL.format(wo=laser.id), _pdf_zip("n1.pdf"), rows=rows)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NEST_REIMPORT
        assert _nest_world(db_session) == before
        assert extraction_calls == []

    def test_a_parent_addressed_reimport_reads_the_laser_child(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai
    ):
        parent, child = self._classic(client, db_session, admin_user)
        _work(db_session, _nest_ops(db_session, child.id)[0], "run")
        headers = _token(client, admin_headers, supervisor)
        before = _nest_world(db_session)

        resp = _import(client, headers, WO_IMPORT_URL.format(wo=parent.id), _cnc_zip("N7.nc"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NEST_REIMPORT
        assert _nest_world(db_session) == before

    def test_work_recorded_while_the_package_unpacks_is_refused_under_the_lock(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai, monkeypatch
    ):
        """The early check passes on an un-worked job; the floor clocks a nest while the upload is
        being extracted; the re-check inside the transaction refuses before the first write."""
        laser = self._standalone(client, db_session, admin_user)
        target_op = _nest_ops(db_session, laser.id)[0]
        headers = _token(client, admin_headers, supervisor)
        original = work_orders_endpoint.extract_laser_nest_zip

        def _floor_clocks_in_during_extraction(*args, **kwargs):
            result = original(*args, **kwargs)
            _work(db_session, fresh(db_session, WorkOrderOperation, target_op.id), "labor")
            return result

        monkeypatch.setattr(work_orders_endpoint, "extract_laser_nest_zip", _floor_clocks_in_during_extraction)
        before = _nest_world(db_session)

        resp = _import(client, headers, WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9_QTY1.nc"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NEST_REIMPORT
        after = _nest_world(db_session)
        assert len(after["entries"]) == len(before["entries"]) + 1, "only the floor's own clock-in landed"
        after["entries"] = before["entries"]
        assert after["audit_rows"] == before["audit_rows"]
        assert after == before

    def test_a_person_may_still_reimport_over_a_worked_nest(self, client, db_session, admin_user, no_ai):
        laser = self._standalone(client, db_session, admin_user)
        worked = _nest_ops(db_session, laser.id)[0]
        _work(db_session, worked, "run")

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9_QTY4.nc"))
        assert resp.status_code == 200, resp.text
        # (Compared by content, not id: SQLite reuses a deleted row's id.)
        [op] = _nest_ops(db_session, laser.id)
        assert "N9" in op.name and "N1" not in op.name, "IMPORT REPLACES EVERYTHING, for a person"
        assert (op.status, float(op.quantity_complete or 0)) == (READY, 0)
        assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).count() == 0
        child = fresh(db_session, WorkOrder, laser.id)
        assert (float(child.quantity_complete), float(child.quantity_scrapped), float(child.quantity_ordered)) == (
            0,
            0,
            4,
        )

    def test_a_token_reimport_over_unworked_nests_still_replaces(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai
    ):
        laser = self._standalone(client, db_session, admin_user)
        assert len(_nest_ops(db_session, laser.id)) == 2

        resp = _import(
            client, _token(client, admin_headers, supervisor), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc")
        )
        assert resp.status_code == 200, resp.text
        [op] = _nest_ops(db_session, laser.id)
        assert "N9" in op.name

    def test_a_token_standalone_import_wipes_nothing_and_is_never_refused(
        self, client, db_session, admin_headers, supervisor, no_ai
    ):
        _laser_center(db_session)
        resp = _import(client, _token(client, admin_headers, supervisor), STANDALONE_IMPORT_URL, _cnc_zip("N1.nc"))
        assert resp.status_code == 200, resp.text

    def test_the_predicate_judges_exactly_the_set_the_wipe_deletes(self, client, db_session, admin_user, no_ai):
        laser = self._standalone(client, db_session, admin_user)
        ops = _nest_ops(db_session, laser.id)
        assert sorted(laser_import_wipe_operation_ids(db_session, child_work_order=laser, company_id=COMPANY_A)) == (
            sorted(op.id for op in ops)
        )
        assert import_would_wipe_recorded_work(db_session, laser_work_order=laser, company_id=COMPANY_A) is False
        assert import_would_wipe_recorded_work(db_session, laser_work_order=None, company_id=COMPANY_A) is False
        _work(db_session, ops[1], "labor")
        assert import_would_wipe_recorded_work(db_session, laser_work_order=laser, company_id=COMPANY_A) is True
        # The labor read is tenant-scoped: under another tenant's id the wipe set itself is empty.
        assert import_would_wipe_recorded_work(db_session, laser_work_order=laser, company_id=2) is False

    # ----------------------------------------------------------------------- #
    # Review fixes on F3 (each fails with its source change switched off)
    # ----------------------------------------------------------------------- #
    def test_the_refusal_names_the_reimport_and_what_to_do(self):
        """Its own text: the count-judgment text names three actions an importer never attempted."""
        assert API_TOKEN_NEST_REIMPORT != API_TOKEN_COUNT_JUDGMENT
        assert "Re-importing a nest package" in API_TOKEN_NEST_REIMPORT
        assert "Leave the re-import for that person." in API_TOKEN_NEST_REIMPORT

    def test_an_office_start_on_a_nest_later_held_is_recorded_work(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai, extraction_calls
    ):
        """Started from the office (``actual_start`` / ``started_by`` stamped, no labor, no counts),
        then held: the status no longer reads IN_PROGRESS, but the start record is still work a
        person did -- the same signal the sequencing flip and the resume target read."""
        laser = self._standalone(client, db_session, admin_user)
        op = _nest_ops(db_session, laser.id)[0]
        set_op(
            db_session, op, status=HELD, actual_start=datetime.utcnow() - timedelta(hours=1), started_by=supervisor.id
        )
        assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).count() == 0
        assert operation_has_recorded_work(db_session, fresh(db_session, WorkOrderOperation, op.id), COMPANY_A)
        headers = _token(client, admin_headers, supervisor)
        extraction_calls.clear()
        before = _nest_world(db_session)

        resp = _import(client, headers, WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9_QTY1.nc"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NEST_REIMPORT
        assert _nest_world(db_session) == before
        assert extraction_calls == []

    @pytest.mark.parametrize("field", ["quantity_complete", "quantity_scrapped"])
    def test_header_counts_the_import_would_zero_are_recorded_work(
        self, client, db_session, admin_user, admin_headers, supervisor, field, no_ai, extraction_calls
    ):
        """No nest operation has been worked, but the laser work order's header carries a count a
        person set by hand: the import zeroes it unconditionally, so a token is refused too."""
        laser = self._standalone(client, db_session, admin_user)
        setattr(laser, field, 2.0)
        db_session.commit()
        ops = _nest_ops(db_session, laser.id)
        assert not any(operation_has_recorded_work(db_session, op, COMPANY_A) for op in ops)
        assert import_would_wipe_recorded_work(db_session, laser_work_order=laser, company_id=COMPANY_A) is True
        headers = _token(client, admin_headers, supervisor)
        extraction_calls.clear()
        before = _nest_world(db_session)

        resp = _import(client, headers, WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9_QTY1.nc"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NEST_REIMPORT
        assert _nest_world(db_session) == before
        assert extraction_calls == []

    def test_the_predicate_reads_the_database_not_the_loaded_operations(self, client, db_session, admin_user, no_ai):
        """The route asks twice in ONE transaction. A count that reached the database after the
        first ask (here written past the identity map) must be seen by the second."""
        laser = self._standalone(client, db_session, admin_user)
        ops = _nest_ops(db_session, laser.id)
        assert import_would_wipe_recorded_work(db_session, laser_work_order=laser, company_id=COMPANY_A) is False
        db_session.execute(
            text("UPDATE work_order_operations SET quantity_complete = 5, version = version + 1 WHERE id = :id"),
            {"id": ops[0].id},
        )
        assert float(ops[0].quantity_complete or 0) == 0, "the loaded row is stale by construction"
        assert import_would_wipe_recorded_work(db_session, laser_work_order=laser, company_id=COMPANY_A) is True
        db_session.rollback()

    def test_a_desk_count_landing_while_the_package_unpacks_is_refused_under_the_lock(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai, monkeypatch
    ):
        """The early check LOADS the nest operations; a desk count reaches the database while the
        upload is extracted. The re-check must see it and answer the documented 403 -- reusing the
        loaded zero let the wipe run into the version check instead, a conflict naming neither
        cause. (One test session serves the request, so the desk write goes past the identity map.)"""
        laser = self._standalone(client, db_session, admin_user)
        target_id = _nest_ops(db_session, laser.id)[0].id
        headers = _token(client, admin_headers, supervisor)
        original = work_orders_endpoint.extract_laser_nest_zip
        # The identity map is weak-referencing: hold the request's loaded row, as a request that
        # still references it would, so it stays in the map (stale) until the re-check.
        held: list = []

        def _desk_records_during_extraction(*args, **kwargs):
            result = original(*args, **kwargs)
            held.append(db_session.get(WorkOrderOperation, target_id))
            assert float(held[0].quantity_complete or 0) == 0
            db_session.execute(
                text("UPDATE work_order_operations SET quantity_complete = 5, version = version + 1 WHERE id = :id"),
                {"id": target_id},
            )
            return result

        monkeypatch.setattr(work_orders_endpoint, "extract_laser_nest_zip", _desk_records_during_extraction)
        before = _nest_world(db_session)

        resp = _import(client, headers, WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9_QTY1.nc"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NEST_REIMPORT
        assert _nest_world(db_session) == before

    def test_a_row_changed_under_a_persons_import_answers_409_refresh(
        self, client, db_session, admin_user, no_ai, monkeypatch
    ):
        """A nest operation the wipe deletes is changed by someone else after this request LOCKED
        it. On Postgres that write cannot land -- the import row-locks the wipe set first (SFI-1
        order) and the other writer queues until the import ends -- so this pins the backstop
        behind the lock: SQLite renders no FOR UPDATE, which lets the write land past the identity
        map, and the version check on the delete is a concurrent edit, answered with the app's 409
        "refresh and retry" -- not the 400 that sends the caller looking for a conflicting row."""
        laser = self._standalone(client, db_session, admin_user)
        target_id = _nest_ops(db_session, laser.id)[0].id
        original_lock = work_orders_endpoint._lock_laser_import_wipe_operations
        held: list = []  # keeps the locked copies in the weak-referencing identity map until the delete

        def _someone_edits_after_the_lock(db, **kwargs):
            locked = original_lock(db, **kwargs)
            held.extend(locked)
            assert target_id in [op.id for op in locked]
            db.execute(text("UPDATE work_order_operations SET version = version + 1 WHERE id = :id"), {"id": target_id})
            return locked

        monkeypatch.setattr(work_orders_endpoint, "_lock_laser_import_wipe_operations", _someone_edits_after_the_lock)
        before = _nest_world(db_session)

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
        # Nothing of the IMPORT persists. The other writer's version bump on the TARGET is theirs,
        # and whether it survives depends on the test backend: on pysqlite no BEGIN precedes the
        # import's lock savepoint, so that SAVEPOINT opens the connection's transaction and its
        # RELEASE commits the bump, which the import's rollback cannot then undo; on Postgres it is
        # rolled back with the import. So everything is compared exactly except that one version,
        # which may be unchanged or +1 -- and every other operation's version must be unchanged.
        after = _nest_world(db_session)

        def _target_version(world: dict) -> int:
            return next(op[-1] for op in world["operations"] if op[0] == target_id)

        def _without_target_version(world: dict) -> dict:
            return {**world, "operations": [op[:-1] if op[0] == target_id else op for op in world["operations"]]}

        assert _target_version(after) in (_target_version(before), _target_version(before) + 1)
        assert _without_target_version(after) == _without_target_version(before)

    def test_a_row_changed_before_the_lock_is_judged_fresh_and_replaced(
        self, client, db_session, admin_user, no_ai, monkeypatch
    ):
        """The other half of the same race: someone saves a nest operation while the package is
        still unpacking, BEFORE the import's transaction locks it, and this request holds a copy
        it loaded earlier. The lock re-reads the row (``populate_existing``), so the import judges
        and replaces what is stored -- for a person, IMPORT REPLACES EVERYTHING -- instead of
        tripping the delete's version check on the stale copy. (A token is refused over the same
        write when it is recorded work: see the desk-count test above.)"""
        laser = self._standalone(client, db_session, admin_user)
        target_id = _nest_ops(db_session, laser.id)[0].id
        original = work_orders_endpoint.extract_laser_nest_zip
        held: list = []  # keeps the request's loaded row in the weak-referencing identity map

        def _someone_edits_during_extraction(*args, **kwargs):
            result = original(*args, **kwargs)
            held.append(db_session.get(WorkOrderOperation, target_id))
            db_session.execute(
                text("UPDATE work_order_operations SET description = 'desk', version = version + 1 WHERE id = :id"),
                {"id": target_id},
            )
            return result

        monkeypatch.setattr(work_orders_endpoint, "extract_laser_nest_zip", _someone_edits_during_extraction)

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
        assert resp.status_code == 200, resp.text
        [op] = _nest_ops(db_session, laser.id)
        assert "N9" in op.name and op.description != "desk"

    def _header_bumped_during_extraction(
        self, db: Session, monkeypatch, work_order_id: int, *, also_set: str = ""
    ) -> list:
        """Someone else saves the laser work order's header while the upload is being extracted
        (``also_set`` is any column they change with it). The request holds its loaded work order
        throughout, so it stays stale in the identity map. Returns the audit rows the import then
        asks for: the conflict must be found before the first."""
        original = work_orders_endpoint.extract_laser_nest_zip
        assignments = "version = version + 1" + (f", {also_set}" if also_set else "")

        def _someone_saves_the_header(*args, **kwargs):
            result = original(*args, **kwargs)
            db.execute(text(f"UPDATE work_orders SET {assignments} WHERE id = :id"), {"id": work_order_id})
            return result

        monkeypatch.setattr(work_orders_endpoint, "extract_laser_nest_zip", _someone_saves_the_header)
        audit_calls: list = []
        original_log = AuditService.log

        def _recording_log(service, *args, **kwargs):
            audit_calls.append((kwargs.get("action"), kwargs.get("resource_type")))
            return original_log(service, *args, **kwargs)

        monkeypatch.setattr(AuditService, "log", _recording_log)
        return audit_calls

    @pytest.mark.parametrize("header", ["counts", "draft", "rederived", "same_total"])
    def test_a_header_changed_under_a_persons_import_answers_409_refresh(
        self, client, db_session, admin_user, header, no_ai, monkeypatch
    ):
        """The laser WORK ORDER itself is saved by someone else mid-import. Every shape of the
        import's header write must reach the version check outside the audit service, whose
        savepoint swallowed the StaleDataError and left the next statement to fail as the 400 for
        a conflicting nest row: zeroing a hand-set count or releasing a draft (written before the
        first audit row), and re-deriving ``quantity_ordered`` (written after the rebuild, inside
        the work-order audit row's flush) when the header was otherwise unchanged.

        ``same_total`` is the shape only the locked version READ catches, because the import never
        writes the header at all (released, stamped, zero counts, the same 5 planned runs): someone
        saves a count of 2 mid-import, and without the read that count survived the import's
        zeroing with a 200 -- a silent success, not merely the wrong status code."""
        laser = self._standalone(client, db_session, admin_user)  # 2 nests, 5 planned runs
        if header == "counts":
            laser.quantity_complete = 2.0
        elif header == "draft":
            laser.status = WorkOrderStatus.DRAFT
        db_session.commit()
        audit_calls = self._header_bumped_during_extraction(
            db_session, monkeypatch, laser.id, also_set="quantity_complete = 2" if header == "same_total" else ""
        )
        before = _nest_world(db_session)

        # One nest of 1 run re-derives quantity_ordered from 5 to 1; same_total re-imports 5 runs.
        package = _cnc_zip("N8_QTY5.nc") if header == "same_total" else _cnc_zip("N9.nc")
        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), package)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
        assert audit_calls == [], "the header conflict is found before the first audit row"
        assert _nest_world(db_session) == before

    def test_a_header_changed_under_a_tokens_allowed_import_answers_409_refresh(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai, monkeypatch
    ):
        """The same race on an import a standing API token IS allowed (no nest worked, a zero
        header), onto the DRAFT laser work order the MCP tool's default path leaves behind: the
        import writes the release onto that header before its first audit row."""
        laser = self._standalone(client, db_session, admin_user)
        laser.status = WorkOrderStatus.DRAFT
        db_session.commit()
        assert import_would_wipe_recorded_work(db_session, laser_work_order=laser, company_id=COMPANY_A) is False
        headers = _token(client, admin_headers, supervisor)
        audit_calls = self._header_bumped_during_extraction(db_session, monkeypatch, laser.id)
        before = _nest_world(db_session)

        resp = _import(client, headers, WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
        assert audit_calls == [], "the header conflict is found before the first audit row"
        assert _nest_world(db_session) == before

    def _import_statement_order(
        self, client, db: Session, monkeypatch, headers, url: str, package: bytes, *, rows=None, locks_sql=None
    ):
        """Run one import and record, in order, every row lock it takes on ``work_order_operations``
        (``ops_lock``) and ``work_orders`` (``header_trylock`` for the never-waiting ``SKIP LOCKED``
        try, ``header_wait`` for the wait between tries, ``header_lock`` for the header check's read;
        the Postgres SQL of every one appended to ``locks_sql`` when given), every savepoint
        statement (``savepoint`` / ``release`` / ``rollback_to``), every UPDATE it sends to
        ``work_orders`` (``header_write``), every audit-chain lock it asks for (``chain_lock``) and
        every audit row (``audit:<resource_type>``, appended BEFORE ``AuditService.log`` runs, so
        the chain lock and any write that log takes or flushes land after its audit entry). A row
        lock is entered when its statement reaches the cursor, not when the ORM is handed it, so it
        lands after the ``SAVEPOINT`` a fresh savepoint emits lazily for that same statement."""
        order: list = []
        pending: list = []  # the row lock the ORM was handed, entered when its statement executes

        def _record_header_locks(state):
            if state.is_select:
                sql = str(state.statement.compile(dialect=postgresql.dialect()))
                if " FOR " in sql and "FROM work_orders" in sql:
                    if "SKIP LOCKED" in sql:
                        pending.append("header_trylock")
                    elif sql.startswith("SELECT work_orders.id "):
                        pending.append("header_wait")
                    else:
                        pending.append("header_lock")
                    if locks_sql is not None:
                        locks_sql.append(sql)
                if " FOR " in sql and "FROM work_order_operations" in sql:
                    pending.append("ops_lock")
                    if locks_sql is not None:
                        locks_sql.append(sql)

        def _record_header_writes(conn, cursor, statement, parameters, context, executemany):
            upper = statement.lstrip().upper()
            for prefix, entry in (
                ("SAVEPOINT ", "savepoint"),
                ("RELEASE SAVEPOINT ", "release"),
                ("ROLLBACK TO SAVEPOINT ", "rollback_to"),
            ):
                if upper.startswith(prefix):
                    order.append(entry)
                    return
            order.extend(pending)
            pending.clear()
            if upper.startswith("UPDATE WORK_ORDERS "):
                order.append("header_write")

        original_log = AuditService.log
        original_chain_lock = AuditService._acquire_chain_lock

        def _recording_log(service, *args, **kwargs):
            order.append(f"audit:{kwargs.get('resource_type')}")
            return original_log(service, *args, **kwargs)

        def _recording_chain_lock(service):
            order.append("chain_lock")
            return original_chain_lock(service)

        monkeypatch.setattr(AuditService, "log", _recording_log)
        monkeypatch.setattr(AuditService, "_acquire_chain_lock", _recording_chain_lock)
        engine = db.get_bind()
        event.listen(db, "do_orm_execute", _record_header_locks)
        event.listen(engine, "before_cursor_execute", _record_header_writes)
        try:
            resp = _import(client, headers, url, package, rows=rows)
        finally:
            event.remove(db, "do_orm_execute", _record_header_locks)
            event.remove(engine, "before_cursor_execute", _record_header_writes)
        return resp, order

    @pytest.mark.parametrize("header", ["counts", "clean", "same_total"])
    def test_the_header_is_flushed_and_locked_before_the_first_audit_row(
        self, client, db_session, admin_user, header, no_ai, monkeypatch
    ):
        """Invariant 4's order, no race needed: a header with a write pending (a hand-set count to
        zero) is flushed before the import's first audit row, and the ``work_orders`` row lock
        comes before the global audit-chain lock even on a header with nothing pending until after
        the rebuild (released, stamped, zero counts), where the flush sends no UPDATE. The lock is
        pinned by DIALECT-COMPILING the statements the import sends: SQLite renders no FOR UPDATE.

        The rebuild's own header write (the re-derived ``quantity_ordered``) is flushed before the
        work-order audit row too, not inside it. ``same_total`` writes the header NOT AT ALL -- the
        shape only the locked version read guards (see the race test of the same name)."""
        laser = self._standalone(client, db_session, admin_user)  # 5 planned runs
        if header == "counts":
            laser.quantity_complete = 2.0
            db_session.commit()
        package = _cnc_zip("N8_QTY5.nc") if header == "same_total" else _cnc_zip("N9.nc")
        resp, order = self._import_statement_order(
            client, db_session, monkeypatch, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), package
        )
        assert resp.status_code == 200, resp.text
        first_audit = next(i for i, entry in enumerate(order) if entry.startswith("audit:"))
        assert "header_lock" in order[:first_audit], order
        if header == "same_total":
            assert "header_write" not in order, "the import has nothing to write to this header"
            assert "audit:work_order" not in order, "and its work-order audit row skips itself as unchanged"
            return
        work_order_audit = order.index("audit:work_order")
        last_write = max(i for i, entry in enumerate(order) if entry == "header_write")
        assert last_write < work_order_audit, f"the header write is flushed before its audit row: {order}"
        if header == "counts":
            assert "header_write" in order[:first_audit], order
        else:
            assert "header_write" not in order[:first_audit], "nothing was pending to flush"
            assert "header_write" in order[first_audit:], "quantity_ordered is re-derived after the rebuild"

    def test_a_standalone_import_flushes_its_rederived_header_before_the_create_row(
        self, client, db_session, admin_user, no_ai, monkeypatch
    ):
        """The fresh standalone job is inserted at quantity 1, stamped by the header check's flush,
        and re-derived to its 5 planned runs by the rebuild. That last UPDATE is flushed before the
        work order's CREATE audit row, not inside that row's savepoint, where a failure would be
        swallowed and resurface as the 400."""
        _laser_center(db_session)
        resp, order = self._import_statement_order(
            client,
            db_session,
            monkeypatch,
            user_headers(admin_user),
            STANDALONE_IMPORT_URL,
            _cnc_zip("N1_QTY2.nc", "N2_QTY3.nc"),
        )
        assert resp.status_code == 200, resp.text
        assert float(resp.json()["child_work_order"]["quantity_ordered"]) == 5
        assert order.count("header_write") == 2, f"the release stamp, then quantity_ordered: {order}"
        last_write = max(i for i, entry in enumerate(order) if entry == "header_write")
        assert last_write < order.index("audit:work_order"), order

    @pytest.mark.parametrize("shape", ["counts", "clean", "same_total", "pdf_rows", "parent"])
    def test_the_operations_are_locked_before_the_work_order_and_the_first_audit_row(
        self, client, db_session, admin_user, shape, no_ai, monkeypatch
    ):
        """SFI-1, the order every floor completion, production report and clock-out locks in --
        operation, then work order, then the audit chain -- and now the import's too. No race
        needed: the import row-locks every operation its wipe deletes, ascending id, in one
        statement; then tries the ``work_orders`` row WITHOUT waiting (``FOR NO KEY UPDATE SKIP
        LOCKED``), both inside one savepoint it releases once both are held; then the header check
        reads the row under the same ``NO KEY UPDATE`` (never the key-strength ``FOR UPDATE``, which
        would queue on a foreign-key check's ``KEY SHARE``); then takes the global chain lock as its
        own statement, before its first audit row; no operation lock comes after that. The old order
        (work order, chain, then the operations by deleting them) is what deadlocked a nest
        completion landing mid-import on Postgres. Pinned by DIALECT-COMPILING the statements the
        import sends: SQLite renders no lock clause. Every import shape onto an existing laser work
        order shares the path: a hand-counted header, a clean one, one the import never writes, the
        PDF confirm-and-commit rows, and a parent-addressed import onto its classic laser child."""
        if shape == "parent":
            parent, laser = self._classic(client, db_session, admin_user)
            url = WO_IMPORT_URL.format(wo=parent.id)
        else:
            laser = self._standalone(client, db_session, admin_user)  # 2 nests, 5 planned runs
            url = WO_IMPORT_URL.format(wo=laser.id)
        if shape == "counts":
            laser.quantity_complete = 2.0
            db_session.commit()
        wipe_ids = sorted(laser_import_wipe_operation_ids(db_session, child_work_order=laser, company_id=COMPANY_A))
        assert wipe_ids and wipe_ids == sorted(op.id for op in _nest_ops(db_session, laser.id))
        rows = None
        package = _cnc_zip("N9.nc")
        if shape == "same_total":
            package = _cnc_zip("N8_QTY5.nc")
        elif shape == "pdf_rows":
            package = _pdf_zip("n1.pdf")
            rows = [{"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2}]
        locked: list = []
        original_lock = work_orders_endpoint._lock_laser_import_wipe_operations

        def _spy(db, **kwargs):
            result = original_lock(db, **kwargs)
            locked.append([op.id for op in result])
            return result

        monkeypatch.setattr(work_orders_endpoint, "_lock_laser_import_wipe_operations", _spy)
        locks_sql: list = []
        resp, order = self._import_statement_order(
            client, db_session, monkeypatch, user_headers(admin_user), url, package, rows=rows, locks_sql=locks_sql
        )
        assert resp.status_code == 200, resp.text
        assert locked == [wipe_ids], "every operation the wipe deletes, ascending"
        assert order.count("ops_lock") == 1, order
        assert order.count("header_trylock") == 1 and "header_wait" not in order, order
        first_audit = next(i for i, entry in enumerate(order) if entry.startswith("audit:"))
        ops_lock, trylock = order.index("ops_lock"), order.index("header_trylock")
        assert (
            ops_lock < trylock < order.index("header_lock") < order.index("chain_lock") < first_audit
        ), f"operations, then the work order (try, then the header check), then the chain: {order}"
        assert (
            order[ops_lock - 1] == "savepoint" and order[trylock + 1] == "release"
        ), f"the operation lock and the try share one savepoint, released once both are held: {order}"
        assert "ops_lock" not in order[first_audit:], f"no operation row lock after the chain lock: {order}"
        ops_sql, try_sql, header_sql = locks_sql[0], locks_sql[1], locks_sql[2]
        assert "ORDER BY work_order_operations.id ASC" in ops_sql and ops_sql.endswith("FOR UPDATE"), ops_sql
        assert "work_order_operations.company_id = " in ops_sql, "tenant-scoped"
        assert try_sql.endswith("FOR NO KEY UPDATE SKIP LOCKED"), try_sql
        assert header_sql.endswith("FOR NO KEY UPDATE"), header_sql
        assert all("work_orders.company_id = " in sql for sql in (try_sql, header_sql)), "tenant-scoped"

    def test_the_token_recheck_judges_the_rows_the_import_just_locked(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai, monkeypatch
    ):
        """The token re-check runs AFTER the operation lock and on the rows it locked: a desk count
        reaching the database while the package unpacks -- past the copy this request loaded for
        the early check -- is already on that copy when the re-check starts, because the lock
        re-read it. Refused 403 with nothing written, as before; the only change is that what it
        judges can no longer move before the wipe."""
        laser = self._standalone(client, db_session, admin_user)
        wipe_ids = sorted(laser_import_wipe_operation_ids(db_session, child_work_order=laser, company_id=COMPANY_A))
        target_id = wipe_ids[0]
        headers = _token(client, admin_headers, supervisor)
        held: list = []  # keeps the request's loaded row in the weak-referencing identity map
        original_extract = work_orders_endpoint.extract_laser_nest_zip

        def _desk_records_during_extraction(*args, **kwargs):
            result = original_extract(*args, **kwargs)
            held.append(db_session.get(WorkOrderOperation, target_id))
            assert float(held[0].quantity_complete or 0) == 0
            db_session.execute(
                text("UPDATE work_order_operations SET quantity_complete = 5, version = version + 1 WHERE id = :id"),
                {"id": target_id},
            )
            return result

        calls: list = []
        original_lock = work_orders_endpoint._lock_laser_import_wipe_operations
        original_check = work_orders_endpoint.import_would_wipe_recorded_work

        def _spy_lock(db, **kwargs):
            result = original_lock(db, **kwargs)
            calls.append(("lock", [op.id for op in result]))
            return result

        def _spy_check(db, **kwargs):
            # What the request's copy of the worked row says when the check STARTS, before its own read.
            calls.append(("check", float(held[0].quantity_complete or 0) if held else None))
            return original_check(db, **kwargs)

        monkeypatch.setattr(work_orders_endpoint, "extract_laser_nest_zip", _desk_records_during_extraction)
        monkeypatch.setattr(work_orders_endpoint, "_lock_laser_import_wipe_operations", _spy_lock)
        monkeypatch.setattr(work_orders_endpoint, "import_would_wipe_recorded_work", _spy_check)
        before = _nest_world(db_session)

        resp = _import(client, headers, WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9_QTY1.nc"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NEST_REIMPORT
        assert calls == [("check", None), ("lock", wipe_ids), ("check", 5.0)], calls
        assert _nest_world(db_session) == before

    @pytest.mark.parametrize("pgcode, status", [("40P01", 409), ("40001", 409), ("57014", 400), (None, 400)])
    @pytest.mark.parametrize("shape", ["cnc", "pdf_rows"])
    def test_a_lock_race_lost_inside_the_rebuild_answers_409_refresh(
        self, client, db_session, admin_user, shape, pgcode, status, no_ai, monkeypatch, tmp_path
    ):
        """Postgres settles a lock cycle (40P01 ``deadlock_detected``) or a serialization conflict
        (40001) by rolling one transaction back. When that is the import, nothing of it commits and
        the other writer's change stands, so the advice is the app's 409 "refresh and retry" -- not
        the 400 telling the caller their nest rows are wrong. Raised AFTER the rebuild has deleted
        and flushed (and, for PDF rows, stored a blob), so the rollback and the blob reap are both
        pinned. Any other database error keeps the 400: a cancelled statement (57014, also an
        ``OperationalError``) and an error carrying no code at all."""
        laser = self._standalone(client, db_session, admin_user)
        original_build = work_orders_endpoint.build_laser_nest_child_work_order
        blobs_written: list = []

        def _rebuild_then_lose_the_race(db, **kwargs):
            original_build(db, **kwargs)
            blobs_written.append(len(kwargs["saved_storage_keys"]))
            raise OperationalError("DELETE FROM work_order_operations", {}, _DriverError(pgcode))

        monkeypatch.setattr(work_orders_endpoint, "build_laser_nest_child_work_order", _rebuild_then_lose_the_race)
        rows = None
        package = _cnc_zip("N9.nc")
        if shape == "pdf_rows":
            package = _pdf_zip("n1.pdf")
            rows = [{"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2}]
        before = _nest_world(db_session)
        files_before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), package, rows=rows)
        assert resp.status_code == status, resp.text
        assert resp.json()["detail"] == (STALE_WRITE_CONFLICT_DETAIL if status == 409 else NEST_PACKAGE_CONFLICT)
        assert blobs_written == [1 if shape == "pdf_rows" else 0], "the race is lost after the rebuild ran"
        assert _nest_world(db_session) == before
        files_after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
        assert files_after == files_before, "no orphaned blob or package dir"

    def _busy_work_order(self, monkeypatch, busy_tries: int) -> list:
        """The laser work order is held by another writer for the import's first ``busy_tries`` tries:
        each try still sends its ``SKIP LOCKED`` statement (so the recorder sees it), and then reports
        the row as held -- what Postgres answers while a work-order-first writer (force-complete, a
        first clock-in on a released job, ``PUT /work-orders/{id}``) holds it. Returns the trace of
        tries, waits and operation locks, in order."""
        trace: list = []
        original_try = work_orders_endpoint._try_lock_laser_work_order
        original_wait = work_orders_endpoint._wait_for_laser_work_order
        original_lock = work_orders_endpoint._lock_laser_import_wipe_operations

        def _try(db, **kwargs):
            got = original_try(db, **kwargs)
            held = len([entry for entry in trace if entry == "try"]) < busy_tries
            trace.append("try")
            return got and not held

        def _wait(db, **kwargs):
            trace.append("wait")
            return original_wait(db, **kwargs)

        def _lock(db, **kwargs):
            trace.append("ops")
            return original_lock(db, **kwargs)

        monkeypatch.setattr(work_orders_endpoint, "_try_lock_laser_work_order", _try)
        monkeypatch.setattr(work_orders_endpoint, "_wait_for_laser_work_order", _wait)
        monkeypatch.setattr(work_orders_endpoint, "_lock_laser_import_wipe_operations", _lock)
        return trace

    def test_a_busy_work_order_releases_the_operation_locks_before_waiting(
        self, client, db_session, admin_user, no_ai, monkeypatch
    ):
        """A writer that locks the WORK ORDER before its operations -- force-complete, the scheduling
        routes, nest restore, and every writer whose unit of work flushes the work order's UPDATE
        first (a first clock-in or office start on a released job, ``PUT /work-orders/{id}`` back to
        draft, release) -- holds the work order while the import holds the operations. Were the
        import to queue on the work order there, the two would close a lock cycle Postgres breaks
        by rolling one side back: a 500 on the floor or a 409 for the planner, the inversion the
        operations-first order introduced. Instead the try never waits; the import rolls its
        savepoint back -- releasing the operation locks, so the other writer can finish -- BEFORE it
        waits for the work order, then waits holding no row lock, releases that too, and starts
        again from the operations. Nothing else changes: the second try succeeds and the import
        replaces the package as usual. Pinned on the statement order, dialect-compiled."""
        laser = self._standalone(client, db_session, admin_user)
        trace = self._busy_work_order(monkeypatch, busy_tries=1)
        locks_sql: list = []
        resp, order = self._import_statement_order(
            client,
            db_session,
            monkeypatch,
            user_headers(admin_user),
            WO_IMPORT_URL.format(wo=laser.id),
            _cnc_zip("N9_QTY1.nc"),
            locks_sql=locks_sql,
        )
        assert resp.status_code == 200, resp.text
        assert trace == ["ops", "try", "wait", "ops", "try"], trace
        first_audit = next(i for i, entry in enumerate(order) if entry.startswith("audit:"))
        assert order[: order.index("header_lock")] == [
            "savepoint",
            "ops_lock",
            "header_trylock",
            "rollback_to",  # the busy try: the operation locks are released ...
            "savepoint",
            "header_wait",  # ... before the import waits for the work order, holding no row lock,
            "rollback_to",  # ... and lets go of the work order again before re-locking the operations
            "savepoint",
            "ops_lock",
            "header_trylock",
            "release",
        ], order
        assert order.index("header_lock") < order.index("chain_lock") < first_audit, order
        wait_sql = [sql for sql in locks_sql if sql.startswith("SELECT work_orders.id ")]
        assert len(wait_sql) == 1 and wait_sql[0].endswith("FOR NO KEY UPDATE"), wait_sql
        assert "work_orders.company_id = " in wait_sql[0], "tenant-scoped"
        [op] = _nest_ops(db_session, laser.id)
        assert "N9" in op.name

    @pytest.mark.parametrize("package", ["N9.nc", "N8_QTY5.nc"])
    def test_a_work_order_saved_by_the_writer_the_import_waited_for_answers_409_refresh(
        self, client, db_session, admin_user, package, no_ai, monkeypatch
    ):
        """The import found the work order held, let go of its operations and WAITED for that writer
        -- who saved the work order (a count of 2) and committed. The import must judge the job as
        that writer left it: its next try re-locks the operations, and the header check compares the
        work order this request LOADED with the stored version, so the save is the app's 409
        "refresh and try again" -- no audit row, nothing of the import written, the other writer's
        count intact -- never a 200 that zeroes the count just saved. That holds only while nothing
        on the busy path reloads the work order: a refresh after the wait would adopt the new
        version and the import would overwrite the count. ``N8_QTY5`` re-imports the same 5 planned
        runs, the shape whose header the import never writes, so only the version read catches it."""
        laser = self._standalone(client, db_session, admin_user)  # 2 nests, 5 planned runs
        laser_id = laser.id
        trace = self._busy_work_order(monkeypatch, busy_tries=1)
        recording_wait = work_orders_endpoint._wait_for_laser_work_order

        def _the_holder_saves_the_work_order_and_commits(db, **kwargs):
            # The writer the import is about to wait for finishes: it saves the work order and
            # COMMITS. One pysqlite connection serves the request and this writer, and the import's
            # first SAVEPOINT opened that connection's transaction (no BEGIN precedes it, and the
            # busy try's ROLLBACK TO keeps it open), so the save must commit at the driver to
            # survive the import's own rollback -- as another session's commit does on Postgres.
            # That COMMIT carries nothing of the import: it has written nothing before its locks.
            assert not (db.new or db.dirty or db.deleted), "the import has written nothing yet"
            driver = db.connection().connection.driver_connection
            driver.commit()
            driver.execute(
                "UPDATE work_orders SET quantity_complete = 2, version = version + 1 WHERE id = ?", (laser_id,)
            )
            driver.commit()
            return recording_wait(db, **kwargs)

        monkeypatch.setattr(
            work_orders_endpoint, "_wait_for_laser_work_order", _the_holder_saves_the_work_order_and_commits
        )
        audit_calls: list = []
        original_log = AuditService.log

        def _recording_log(service, *args, **kwargs):
            audit_calls.append(kwargs.get("resource_type"))
            return original_log(service, *args, **kwargs)

        monkeypatch.setattr(AuditService, "log", _recording_log)
        before = _nest_world(db_session)

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser_id), _cnc_zip(package))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
        assert trace == ["ops", "try", "wait", "ops", "try"], "the import waited, then tried again"
        assert audit_calls == [], "the conflict is found before the first audit row"
        # The other writer's save stands -- its count and its version bump -- and nothing else moved.
        saved = [(*row[:3], 2.0, *row[4:7], row[7] + 1) if row[0] == laser_id else row for row in before["work_orders"]]
        assert _nest_world(db_session) == {**before, "work_orders": saved}

    def test_a_work_order_busy_through_every_try_answers_409_writing_nothing(
        self, client, db_session, admin_user, no_ai, monkeypatch
    ):
        """The work order is held on every try: after the last one the import gives up with the app's
        409 "refresh and try again" -- having written nothing and asked for no audit row -- rather
        than queueing on the work order with the operations held. Three tries, pinned: docs/API.md
        states the number (the import row and "Package import replaces everything"), so changing
        the constant must fail here until the docs change with it."""
        laser = self._standalone(client, db_session, admin_user)
        assert work_orders_endpoint._LASER_IMPORT_LOCK_ATTEMPTS == 3, "docs/API.md documents three tries"
        trace = self._busy_work_order(monkeypatch, busy_tries=3)
        audit_calls: list = []
        original_log = AuditService.log

        def _recording_log(service, *args, **kwargs):
            audit_calls.append(kwargs.get("resource_type"))
            return original_log(service, *args, **kwargs)

        monkeypatch.setattr(AuditService, "log", _recording_log)
        before = _nest_world(db_session)

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
        assert trace == ["ops", "try", "wait", "ops", "try", "wait", "ops", "try"], trace
        assert audit_calls == []
        assert _nest_world(db_session) == before

    @pytest.mark.parametrize("pgcode, status", [("40P01", 409), ("40001", 409), ("57014", 400)])
    def test_a_lock_race_lost_at_the_audit_chain_lock_answers_409_refresh(
        self, client, db_session, admin_user, pgcode, status, no_ai, monkeypatch
    ):
        """The import holds its operations and the work order and queues on the global audit-chain
        lock behind a floor verb that writes its audit row before its row writes (hold, shop-floor
        start, resume, a clock-in with a qualification exception) and then waits for one of those
        rows: a cycle Postgres settles by rolling one side back. AuditService.log takes that lock
        inside an except that swallows everything, so when it was the import's first audit row
        that lost, the 40P01 was eaten, the transaction stayed aborted, and the import answered the
        400 for a conflicting nest row. The import now takes the chain lock as its own statement
        before its first audit row, so the loss reaches the lost-race 409 -- with nothing written
        and no audit row. Any other database error there keeps the 400."""
        laser = self._standalone(client, db_session, admin_user)
        chain_lock_calls: list = []

        def _the_import_is_the_victim(service):
            chain_lock_calls.append(service)
            raise OperationalError("SELECT pg_advisory_xact_lock(%(k)s)", {}, _DriverError(pgcode))

        monkeypatch.setattr(AuditService, "_acquire_chain_lock", _the_import_is_the_victim)
        before = _nest_world(db_session)

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
        assert resp.status_code == status, resp.text
        assert resp.json()["detail"] == (STALE_WRITE_CONFLICT_DETAIL if status == 409 else NEST_PACKAGE_CONFLICT)
        assert len(chain_lock_calls) == 1, "the import's own chain-lock statement, before any audit row"
        assert _nest_world(db_session) == before

    def test_a_paused_hash_chain_takes_no_chain_lock(self, client, db_session, admin_user, no_ai, monkeypatch):
        """``AUDIT_HASH_CHAIN_ENABLED`` false is the lock-free allocator: no audit write takes the
        chain lock, so the import's explicit one must not either -- it is exactly the lock log()
        would take."""
        laser = self._standalone(client, db_session, admin_user)
        monkeypatch.setattr("app.services.audit_service.settings.AUDIT_HASH_CHAIN_ENABLED", False)
        monkeypatch.setattr(
            AuditService, "_acquire_chain_lock", lambda service: pytest.fail("the paused chain takes no lock")
        )

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
        assert resp.status_code == 200, resp.text

    def _chain_lock_trace(self, monkeypatch) -> list:
        """Record, in order: what ``_lock_laser_import_rows`` returned (``rows_locked:<value>``), the
        import's explicit early chain lock (``early_chain_lock``), every chain-lock statement whoever
        asks for it (``chain_lock`` -- the early one sends one too), every audit row (``audit``,
        entered before ``AuditService.log`` runs, so its own chain lock lands after it), the rebuild
        starting (``build``) and every nest-PDF blob write inside it (``blob``, entered once written)."""
        trace: list = []
        original_rows = work_orders_endpoint._lock_laser_import_rows
        original_early = AuditService.acquire_chain_lock
        original_chain = AuditService._acquire_chain_lock
        original_log = AuditService.log
        original_build = work_orders_endpoint.build_laser_nest_child_work_order
        original_document = laser_nest_service._create_nest_document

        def _rows(db, **kwargs):
            locked = original_rows(db, **kwargs)
            trace.append(f"rows_locked:{locked}")
            return locked

        def _early(service):
            trace.append("early_chain_lock")
            return original_early(service)

        def _chain(service):
            trace.append("chain_lock")
            return original_chain(service)

        def _log(service, *args, **kwargs):
            trace.append("audit")
            return original_log(service, *args, **kwargs)

        def _build(db, **kwargs):
            trace.append("build")
            return original_build(db, **kwargs)

        def _document(*args, **kwargs):
            document = original_document(*args, **kwargs)
            if document is not None:  # a nest with a PDF: its blob has just been written
                trace.append("blob")
            return document

        monkeypatch.setattr(work_orders_endpoint, "_lock_laser_import_rows", _rows)
        monkeypatch.setattr(AuditService, "acquire_chain_lock", _early)
        monkeypatch.setattr(AuditService, "_acquire_chain_lock", _chain)
        monkeypatch.setattr(AuditService, "log", _log)
        monkeypatch.setattr(work_orders_endpoint, "build_laser_nest_child_work_order", _build)
        monkeypatch.setattr(laser_nest_service, "_create_nest_document", _document)
        return trace

    @pytest.mark.parametrize("shape", ["standalone", "standalone_pdf_rows", "first_child", "nothing_to_replace"])
    def test_an_import_replacing_nothing_takes_no_chain_lock_before_the_rebuild(
        self, client, db_session, admin_user, shape, no_ai, monkeypatch
    ):
        """The early chain lock is for one race: the import holding the operations it replaces while a
        writer that holds the chain waits for one of them. An import that replaces nothing holds no
        operation lock -- a standalone import, a classic parent's first import onto the child it
        creates, a laser job with no nest operation yet -- so it takes no chain lock of its own and
        leaves it to its first audit row, after the rebuild. Taking it early there held the global,
        all-tenant lock across every operation insert and every nest PDF's blob write, and put it
        ahead of the work-center ``KEY SHARE`` those inserts take: a cycle with any writer that locks
        the work center and then audits, which ``AuditService.log`` would have swallowed on that
        writer's side as a silent no-op commit."""
        rows = None
        package = _cnc_zip("N1_QTY2.nc", "N2_QTY3.nc")
        if shape in ("standalone", "standalone_pdf_rows"):
            _laser_center(db_session)
            url = STANDALONE_IMPORT_URL
            if shape == "standalone_pdf_rows":
                package = _pdf_zip("n1.pdf", "n2.pdf")
                rows = [
                    {"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2},
                    {"source_file": "n2.pdf", "cnc_number": "N2", "planned_runs": 3},
                ]
        elif shape == "first_child":
            url = WO_IMPORT_URL.format(wo=self._parent(db_session).id)
        else:
            _laser_center(db_session)
            empty = WorkOrder(
                work_order_number="SEAM-LASER-EMPTY",
                part_id=None,
                work_order_type="laser_cutting",
                sequential_operations=False,
                quantity_ordered=1,
                status=WorkOrderStatus.RELEASED,
                priority=5,
                company_id=COMPANY_A,
            )
            db_session.add(empty)
            db_session.commit()
            assert laser_import_wipe_operation_ids(db_session, child_work_order=empty, company_id=COMPANY_A) == []
            url = WO_IMPORT_URL.format(wo=empty.id)
        trace = self._chain_lock_trace(monkeypatch)

        resp = _import(client, user_headers(admin_user), url, package, rows=rows)
        assert resp.status_code == 200, resp.text
        # The standalone route never calls the row lock; the other two find nothing to lock.
        expected_rows = [] if shape.startswith("standalone") else ["rows_locked:False"]
        assert [entry for entry in trace if entry.startswith("rows_locked:")] == expected_rows, trace
        assert "early_chain_lock" not in trace, trace
        assert "chain_lock" in trace, "the audit rows still take the chain lock, as they always did"
        first_chain_lock = trace.index("chain_lock")
        assert trace.index("build") < first_chain_lock, f"no chain lock before the rebuild: {trace}"
        assert trace[first_chain_lock - 1] == "audit", f"the first chain lock is an audit row's: {trace}"
        if shape == "standalone_pdf_rows":
            blobs = [i for i, entry in enumerate(trace) if entry == "blob"]
            assert len(blobs) == 2 and max(blobs) < first_chain_lock, f"no blob write under the chain lock: {trace}"

    @pytest.mark.parametrize("shape", ["laser", "parent", "pdf_rows"])
    def test_an_import_replacing_operations_takes_one_early_chain_lock_before_any_audit_row(
        self, client, db_session, admin_user, shape, no_ai, monkeypatch
    ):
        """The other side of the same rule: an import that replaces operations holds their row locks
        (and the work order's), so it takes the chain lock exactly once as its own statement, before
        its first audit row and before the rebuild -- where a lost lock race reaches the 409 instead
        of being swallowed inside ``AuditService.log``."""
        if shape == "parent":
            parent, _laser = self._classic(client, db_session, admin_user)
            url = WO_IMPORT_URL.format(wo=parent.id)
        else:
            laser = self._standalone(client, db_session, admin_user)
            url = WO_IMPORT_URL.format(wo=laser.id)
        rows = None
        package = _cnc_zip("N9.nc")
        if shape == "pdf_rows":
            package = _pdf_zip("n1.pdf")
            rows = [{"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2}]
        trace = self._chain_lock_trace(monkeypatch)

        resp = _import(client, user_headers(admin_user), url, package, rows=rows)
        assert resp.status_code == 200, resp.text
        assert trace.count("early_chain_lock") == 1, trace
        early = trace.index("early_chain_lock")
        assert trace[:early] == ["rows_locked:True"], f"the row locks first, then the chain lock: {trace}"
        assert trace[early + 1] == "chain_lock", "the early lock is the chain-lock statement itself"
        assert early < trace.index("audit") and early < trace.index("build"), trace

    def test_a_persons_parent_addressed_import_never_resolves_the_laser_child_early(
        self, client, db_session, admin_user, admin_headers, supervisor, no_ai, monkeypatch
    ):
        """The credential is tested before the target is resolved: a person's import does no
        throw-away read of the laser child; a token's still does (the spy is live)."""
        parent, _child = self._classic(client, db_session, admin_user)
        calls: list = []
        original = work_orders_endpoint._existing_laser_target

        def _spy(*args, **kwargs):
            calls.append(kwargs.get("work_order"))
            return original(*args, **kwargs)

        monkeypatch.setattr(work_orders_endpoint, "_existing_laser_target", _spy)

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=parent.id), _cnc_zip("N7.nc"))
        assert resp.status_code == 200, resp.text
        assert calls == []

        token = _token(client, admin_headers, supervisor)
        resp = _import(client, token, WO_IMPORT_URL.format(wo=parent.id), _cnc_zip("N8.nc"))
        assert resp.status_code == 200, resp.text
        assert len(calls) == 1


@pytest.fixture(scope="module")
def mcp_catalog():
    return build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


class TestMcpImportSurfacesTheRefusal:
    async def test_import_laser_nest_package_returns_the_403_as_an_error_result(
        self, client, db_session, admin_user, admin_headers, supervisor, mcp_catalog, no_ai
    ):
        _laser_center(db_session)
        made = _import(client, user_headers(admin_user), STANDALONE_IMPORT_URL, _cnc_zip("N1_QTY2.nc"))
        assert made.status_code == 200, made.text
        laser_id = made.json()["child_work_order"]["id"]
        _work(db_session, _nest_ops(db_session, laser_id)[0], "run")
        # The tool reads the work order first; let that read settle before the snapshot.
        assert client.get(WORK_ORDER_URL.format(wo=laser_id), headers=admin_headers).status_code == 200
        server, _ = make_server(mcp_catalog, headers=_token(client, admin_headers, supervisor))
        before = _nest_world(db_session)

        result = await call(
            server,
            "import_laser_nest_package",
            {
                "work_order_id": laser_id,
                "file": {
                    "filename": "nests.zip",
                    "content_base64": base64.b64encode(_cnc_zip("N9_QTY1.nc")).decode(),
                    "content_type": "application/zip",
                },
            },
        )
        assert result.is_error, result.content
        assert (result.structured_content["status"], result.structured_content["detail"]) == (
            403,
            API_TOKEN_NEST_REIMPORT,
        )
        assert _nest_world(db_session) == before
