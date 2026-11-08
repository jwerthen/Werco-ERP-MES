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
  person's import never resolves the laser child for the token check.

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
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
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

    def _classic(self, client, db: Session, admin: User) -> tuple[WorkOrder, WorkOrder]:
        """A production parent and the laser child a person's import created under it."""
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
        """A nest operation the wipe deletes is changed by someone else after this request loaded
        it: the version check on the delete is a concurrent edit, so the answer is the app's 409
        "refresh and retry" -- not the 400 that sends the caller looking for a conflicting row."""
        laser = self._standalone(client, db_session, admin_user)
        target_id = _nest_ops(db_session, laser.id)[0].id
        original = work_orders_endpoint.extract_laser_nest_zip
        held: list = []  # keeps the request's loaded row in the weak-referencing identity map

        def _someone_edits_during_extraction(*args, **kwargs):
            result = original(*args, **kwargs)
            held.append(db_session.get(WorkOrderOperation, target_id))
            assert held[0].version is not None
            db_session.execute(
                text("UPDATE work_order_operations SET version = version + 1 WHERE id = :id"), {"id": target_id}
            )
            return result

        monkeypatch.setattr(work_orders_endpoint, "extract_laser_nest_zip", _someone_edits_during_extraction)
        before = _nest_world(db_session)

        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
        assert _nest_world(db_session) == before

    def _header_bumped_during_extraction(self, db: Session, monkeypatch, work_order_id: int) -> list:
        """Someone else saves the laser work order's header while the upload is being extracted.
        (The request holds its loaded work order throughout, so it stays stale in the identity map.)
        Returns the audit rows the import then asks for: the conflict must be found before the first."""
        original = work_orders_endpoint.extract_laser_nest_zip

        def _someone_saves_the_header(*args, **kwargs):
            result = original(*args, **kwargs)
            db.execute(text("UPDATE work_orders SET version = version + 1 WHERE id = :id"), {"id": work_order_id})
            return result

        monkeypatch.setattr(work_orders_endpoint, "extract_laser_nest_zip", _someone_saves_the_header)
        audit_calls: list = []
        original_log = AuditService.log

        def _recording_log(service, *args, **kwargs):
            audit_calls.append((kwargs.get("action"), kwargs.get("resource_type")))
            return original_log(service, *args, **kwargs)

        monkeypatch.setattr(AuditService, "log", _recording_log)
        return audit_calls

    @pytest.mark.parametrize("header", ["counts", "draft", "rederived"])
    def test_a_header_changed_under_a_persons_import_answers_409_refresh(
        self, client, db_session, admin_user, header, no_ai, monkeypatch
    ):
        """The laser WORK ORDER itself is saved by someone else mid-import. Every shape of the
        import's header write must reach the version check outside the audit service, whose
        savepoint swallowed the StaleDataError and left the next statement to fail as the 400 for
        a conflicting nest row: zeroing a hand-set count or releasing a draft (written before the
        first audit row), and re-deriving ``quantity_ordered`` (written after the rebuild, inside
        the work-order audit row's flush) when the header was otherwise unchanged."""
        laser = self._standalone(client, db_session, admin_user)  # 2 nests, 5 planned runs
        if header == "counts":
            laser.quantity_complete = 2.0
        elif header == "draft":
            laser.status = WorkOrderStatus.DRAFT
        db_session.commit()
        audit_calls = self._header_bumped_during_extraction(db_session, monkeypatch, laser.id)
        before = _nest_world(db_session)

        # One nest of 1 run: the import re-derives quantity_ordered from 5 to 1 in every shape.
        resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
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

    @pytest.mark.parametrize("header", ["counts", "clean"])
    def test_the_header_is_flushed_and_locked_before_the_first_audit_row(
        self, client, db_session, admin_user, header, no_ai, monkeypatch
    ):
        """Invariant 4's order, no race needed: a header with a write pending (a hand-set count to
        zero) is flushed before the import's first audit row, and the ``work_orders`` row lock
        comes before the global audit-chain lock even on a header with nothing pending until after
        the rebuild (released, stamped, zero counts), where the flush sends no UPDATE. The lock is
        pinned by DIALECT-COMPILING the statements the import sends: SQLite renders no FOR UPDATE."""
        laser = self._standalone(client, db_session, admin_user)
        if header == "counts":
            laser.quantity_complete = 2.0
            db_session.commit()
        order: list = []

        def _record_header_locks(state):
            if state.is_select:
                sql = str(state.statement.compile(dialect=postgresql.dialect()))
                if "FOR UPDATE" in sql and "FROM work_orders" in sql:
                    order.append("header_lock")

        def _record_header_writes(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE WORK_ORDERS "):
                order.append("header_write")

        original_log = AuditService.log

        def _recording_log(service, *args, **kwargs):
            order.append("audit")
            return original_log(service, *args, **kwargs)

        monkeypatch.setattr(AuditService, "log", _recording_log)
        engine = db_session.get_bind()
        event.listen(db_session, "do_orm_execute", _record_header_locks)
        event.listen(engine, "before_cursor_execute", _record_header_writes)
        try:
            resp = _import(client, user_headers(admin_user), WO_IMPORT_URL.format(wo=laser.id), _cnc_zip("N9.nc"))
        finally:
            event.remove(db_session, "do_orm_execute", _record_header_locks)
            event.remove(engine, "before_cursor_execute", _record_header_writes)
        assert resp.status_code == 200, resp.text
        first_audit = order.index("audit")
        assert "header_lock" in order[:first_audit], order
        if header == "counts":
            assert "header_write" in order[:first_audit], order
        else:
            assert "header_write" not in order[:first_audit], "nothing was pending to flush"
            assert "header_write" in order[first_audit:], "quantity_ordered is re-derived after the rebuild"

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
