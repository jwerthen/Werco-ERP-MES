"""``POST /work-orders/{id}/start`` (released-only, supervisor tier) and the new
``POST /work-orders/{id}/resume`` ("Resume Work Order").

Ruling 2 (2026-11-05): starting is for RELEASED work and no longer lifts a hold; a work-order
hold is lifted by the resume verb, which never RELEASES a job -- Release is the authorization
step. Its restore target comes from the RELEASE RECORD (``released_at`` / ``released_by``):

* release record + work evidence  -> IN_PROGRESS ("release_record_with_work")
* release record, no work         -> RELEASED    ("release_record")
* no release record               -> DRAFT       ("no_release_record"), un-worked READY
                                     operations (laser nests included) back to PENDING,
                                     one REQUIRED ready->pending audit row each.

The resume writes its evidence through ``log_required`` and fails CLOSED (503, nothing
changed). Every zero-row audit assertion is paired with a positive control.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services.audit_service import AuditService
from app.services.operation_action_gates import MSG_WORK_ORDER_NOT_RELEASED, MSG_WORK_ORDER_ON_HOLD
from app.services.work_order_state_service import (
    _worked_operation_ids,
    demote_operations_for_sequencing,
    demote_ready_operations,
    work_order_hold_restore_target,
)
from tests.api.kiosk_test_helpers import (
    COMPANY_B,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

ALLOWED = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, "superuser", UserRole.PLATFORM_ADMIN]
REFUSED = [UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER]
DRAFT_MESSAGE = (
    "No release is on record for this work order, so it returned to DRAFT — release it to put it back on the floor."
)


def actor(db: Session, role):
    if role == "superuser":
        user = make_user(db, role=UserRole.OPERATOR)
        user.is_superuser = True
        db.commit()
        return user
    return make_user(db, role=role)


def start_url(wo_id: int) -> str:
    return f"/api/v1/work-orders/{wo_id}/start"


def resume_url(wo_id: int) -> str:
    return f"/api/v1/work-orders/{wo_id}/resume"


def wo_status_rows(db: Session, wo_id: int) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order",
            AuditLog.resource_id == wo_id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )


def op_status_rows(db: Session, op_id: int) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op_id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )


def reread(db: Session, model, row_id: int):
    db.expire_all()
    return db.query(model).filter(model.id == row_id).one()


def stamp(db: Session, wo: WorkOrder, user, *, released_at=True, released_by=True) -> None:
    if released_at:
        wo.released_at = datetime.utcnow() - timedelta(days=1)
    if released_by:
        wo.released_by = user.id
    db.commit()


def add_op(db: Session, wo: WorkOrder, wc, *, sequence: int, status: OperationStatus, **fields) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Step {sequence}",
        status=status,
        company_id=wo.company_id,
        **fields,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def labor(db: Session, user, op: WorkOrderOperation) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=op.work_order_id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        company_id=user.company_id,
    )
    db.add(entry)
    db.commit()
    return entry


# =========================================================================== POST /work-orders/{id}/start


class TestStartWorkOrder:
    @pytest.mark.parametrize("role", REFUSED)
    def test_floor_and_read_roles_are_refused(self, client, db_session, role):
        wc = make_work_center(db_session)
        wo, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED)
        resp = client.post(start_url(wo.id), headers=user_headers(actor(db_session, role)))
        assert resp.status_code == 403
        assert reread(db_session, WorkOrder, wo.id).status == WorkOrderStatus.RELEASED

    @pytest.mark.parametrize("role", ALLOWED)
    def test_supervisor_tier_starts_a_released_work_order_with_one_audit_row(self, client, db_session, role):
        wc = make_work_center(db_session)
        wo, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED)

        resp = client.post(start_url(wo.id), headers=user_headers(actor(db_session, role)))

        assert resp.status_code == 200, resp.text
        row = reread(db_session, WorkOrder, wo.id)
        assert row.status == WorkOrderStatus.IN_PROGRESS and row.actual_start is not None
        rows = wo_status_rows(db_session, wo.id)
        assert len(rows) == 1 and rows[0].new_values == {"status": "in_progress"}

    @pytest.mark.parametrize(
        "status_,code,detail",
        [
            (WorkOrderStatus.ON_HOLD, 409, MSG_WORK_ORDER_ON_HOLD),
            (WorkOrderStatus.DRAFT, 409, MSG_WORK_ORDER_NOT_RELEASED),
            (WorkOrderStatus.IN_PROGRESS, 400, "Work order must be released to start"),
        ],
    )
    def test_non_released_work_orders_are_refused_and_untouched(self, client, db_session, status_, code, detail):
        """ON_HOLD used to be accepted and silently lifted, by any authenticated user."""
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wo, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=status_)
        version = wo.version

        resp = client.post(start_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == code
        assert resp.json()["detail"] == detail
        # The app shares this session (autoflush off): a refusal raised after the status
        # write would leave dirty state that reread()'s expire_all() silently discards.
        assert not db_session.dirty and not db_session.new
        row = reread(db_session, WorkOrder, wo.id)
        assert row.status == status_ and row.version == version and row.actual_start is None
        # The identical query finds exactly one row on success (the test above).
        assert wo_status_rows(db_session, wo.id) == []

    def test_soft_deleted_and_cross_tenant_work_orders_are_404(self, client, db_session):
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        deleted, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED)
        deleted.soft_delete(supervisor.id)
        db_session.commit()
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        foreign, _ = make_wo_with_operation(
            db_session, company_id=COMPANY_B, work_center=wc_b, wo_status=WorkOrderStatus.RELEASED
        )

        for wo in (deleted, foreign):
            resp = client.post(start_url(wo.id), headers=user_headers(supervisor))
            assert resp.status_code == 404, resp.text
            assert reread(db_session, WorkOrder, wo.id).status == WorkOrderStatus.RELEASED


# =========================================================================== POST /work-orders/{id}/resume


class TestResumeWorkOrder:
    @pytest.mark.parametrize("role", REFUSED)
    def test_floor_and_read_roles_are_refused(self, client, db_session, role):
        wc = make_work_center(db_session)
        wo, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        resp = client.post(resume_url(wo.id), headers=user_headers(actor(db_session, role)))
        assert resp.status_code == 403
        assert reread(db_session, WorkOrder, wo.id).status == WorkOrderStatus.ON_HOLD

    @pytest.mark.parametrize("role", ALLOWED)
    def test_supervisor_tier_resumes(self, client, db_session, role):
        wc = make_work_center(db_session)
        user = actor(db_session, role)
        wo, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        stamp(db_session, wo, user)

        resp = client.post(resume_url(wo.id), headers=user_headers(user))

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "released"

    def test_not_on_hold_is_400_and_soft_deleted_is_404(self, client, db_session):
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        released, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED)
        resp = client.post(resume_url(released.id), headers=user_headers(supervisor))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Work order is not on hold"

        held, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        held.soft_delete(supervisor.id)
        db_session.commit()
        resp = client.post(resume_url(held.id), headers=user_headers(supervisor))
        assert resp.status_code == 404
        assert reread(db_session, WorkOrder, held.id).status == WorkOrderStatus.ON_HOLD

        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        foreign, _ = make_wo_with_operation(
            db_session, company_id=COMPANY_B, work_center=wc_b, wo_status=WorkOrderStatus.ON_HOLD
        )
        resp = client.post(resume_url(foreign.id), headers=user_headers(supervisor))
        assert resp.status_code == 404
        assert reread(db_session, WorkOrder, foreign.id).status == WorkOrderStatus.ON_HOLD

    def test_released_job_with_labor_resumes_in_progress(self, client, db_session):
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        stamp(db_session, wo, supervisor, released_by=False)
        labor(db_session, operator, op)

        resp = client.post(resume_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "in_progress"
        assert body["restore_basis"] == "release_record_with_work"
        assert body["operations_returned_to_pending"] == 0
        assert body["work_order_id"] == wo.id
        assert reread(db_session, WorkOrder, wo.id).status == WorkOrderStatus.IN_PROGRESS
        [row] = wo_status_rows(db_session, wo.id)
        assert row.old_values == {"status": "on_hold"} and row.new_values == {"status": "in_progress"}
        assert row.extra_data["restore_basis"] == "release_record_with_work"
        assert row.extra_data["transition"] == "work_order_hold_cleared"
        assert row.extra_data["operations_returned_to_pending"] == 0
        # Op holds and READY ops of a resumed released job are untouched.
        assert reread(db_session, WorkOrderOperation, op.id).status == OperationStatus.READY

    def test_a_complete_operation_counts_as_work_even_without_actual_start(self, client, db_session):
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wo, op = make_wo_with_operation(
            db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD, op_status=OperationStatus.COMPLETE
        )
        add_op(db_session, wo, wc, sequence=20, status=OperationStatus.READY)
        stamp(db_session, wo, supervisor)
        assert wo.actual_start is None

        resp = client.post(resume_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "in_progress"
        assert resp.json()["restore_basis"] == "release_record_with_work"

    def test_released_by_alone_with_no_work_resumes_released(self, client, db_session):
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        stamp(db_session, wo, supervisor, released_at=False)

        resp = client.post(resume_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "released"
        assert resp.json()["restore_basis"] == "release_record"
        assert reread(db_session, WorkOrder, wo.id).status == WorkOrderStatus.RELEASED
        assert reread(db_session, WorkOrderOperation, op.id).status == OperationStatus.READY

    def test_unstamped_laser_package_resumes_to_draft_and_demotes_unworked_nests(self, client, db_session):
        """No laser carve-out: an unstamped held package goes back through Release."""
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wo, nest1 = make_wo_with_operation(
            db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD, sequential_operations=False
        )
        wo.work_order_type = WorkOrderType.LASER_CUTTING.value
        db_session.commit()
        nest2 = add_op(db_session, wo, wc, sequence=20, status=OperationStatus.READY)
        running = add_op(
            db_session, wo, wc, sequence=30, status=OperationStatus.IN_PROGRESS, actual_start=datetime.utcnow()
        )
        labor(db_session, operator, running)
        worked_ready = add_op(db_session, wo, wc, sequence=40, status=OperationStatus.READY)
        labor(db_session, operator, worked_ready)

        resp = client.post(resume_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {
            "work_order_id": wo.id,
            "message": DRAFT_MESSAGE,
            "status": "draft",
            "restore_basis": "no_release_record",
            "operations_returned_to_pending": 2,
        }
        assert reread(db_session, WorkOrder, wo.id).status == WorkOrderStatus.DRAFT
        assert reread(db_session, WorkOrderOperation, nest1.id).status == OperationStatus.PENDING
        assert reread(db_session, WorkOrderOperation, nest2.id).status == OperationStatus.PENDING
        assert reread(db_session, WorkOrderOperation, running.id).status == OperationStatus.IN_PROGRESS
        assert reread(db_session, WorkOrderOperation, worked_ready.id).status == OperationStatus.READY
        for demoted in (nest1, nest2):
            [row] = op_status_rows(db_session, demoted.id)
            assert row.old_values == {"status": "ready"} and row.new_values == {"status": "pending"}
            assert row.extra_data["transition"] == "work_order_resumed_to_draft"
            assert row.extra_data["operation_number"] == demoted.operation_number
            assert row.extra_data["sequence"] == demoted.sequence
        assert op_status_rows(db_session, running.id) == []
        assert op_status_rows(db_session, worked_ready.id) == []
        [wo_row] = wo_status_rows(db_session, wo.id)
        assert wo_row.extra_data["restore_basis"] == "no_release_record"
        assert wo_row.extra_data["operations_returned_to_pending"] == 2

    def test_unstamped_job_with_running_labor_still_resumes_to_draft(self, client, db_session):
        """The legacy shape the old PUT-start DRAFT bypass left behind: worked, but never released."""
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wo, op = make_wo_with_operation(
            db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD, op_status=OperationStatus.IN_PROGRESS
        )
        labor(db_session, operator, op)

        resp = client.post(resume_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "draft"
        assert resp.json()["restore_basis"] == "no_release_record"
        assert resp.json()["operations_returned_to_pending"] == 0
        assert reread(db_session, WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS

    @pytest.mark.parametrize("stamped", [False, True], ids=["to_draft", "to_released"])
    def test_operation_level_holds_and_their_blockers_are_untouched(self, client, db_session, stamped):
        """Resume lifts the WORK-ORDER hold only: an operation's own hold (and its open
        blocker) stays exactly as it was, whether the job lands DRAFT or RELEASED."""
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wo, held_op = make_wo_with_operation(
            db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD, op_status=OperationStatus.ON_HOLD
        )
        ready_op = add_op(db_session, wo, wc, sequence=20, status=OperationStatus.READY)
        blocker = WorkOrderBlocker(
            work_order_id=wo.id,
            operation_id=held_op.id,
            title="Gauge out for calibration",
            status=WorkOrderBlockerStatus.OPEN.value,
            company_id=wo.company_id,
        )
        db_session.add(blocker)
        db_session.commit()
        if stamped:
            stamp(db_session, wo, supervisor)

        resp = client.post(resume_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == ("released" if stamped else "draft")
        assert reread(db_session, WorkOrderOperation, held_op.id).status == OperationStatus.ON_HOLD
        assert reread(db_session, WorkOrderBlocker, blocker.id).status == WorkOrderBlockerStatus.OPEN.value
        assert op_status_rows(db_session, held_op.id) == []
        # Control in the same request: the un-worked READY sibling IS demoted on a DRAFT
        # landing (and only then), through the same audited path the held op skipped.
        expected = OperationStatus.READY if stamped else OperationStatus.PENDING
        assert reread(db_session, WorkOrderOperation, ready_op.id).status == expected
        assert len(op_status_rows(db_session, ready_op.id)) == (0 if stamped else 1)
        assert resp.json()["operations_returned_to_pending"] == (0 if stamped else 1)

    def test_audit_failure_fails_closed_with_503_and_changes_nothing(self, client, db_session, monkeypatch):
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        version = wo.version
        monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)

        resp = client.post(resume_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == 503
        assert resp.json()["detail"] == (
            "The audit record for resuming this work order could not be saved; nothing was changed. Retry."
        )
        row = reread(db_session, WorkOrder, wo.id)
        assert row.status == WorkOrderStatus.ON_HOLD and row.version == version
        # Unstamped, so the DRAFT branch would have demoted this READY op -- it must not have.
        assert reread(db_session, WorkOrderOperation, op.id).status == OperationStatus.READY

    def test_a_failed_demotion_audit_rolls_back_the_work_order_row_already_written(
        self, client, db_session, monkeypatch
    ):
        """All-or-nothing: the WO's STATUS_CHANGE row is written FIRST, then one row per
        demoted operation. When a per-operation row fails, the WO row, the status write and
        every demotion roll back together -- no orphan "resumed" evidence for a resume that
        did not happen."""
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        real_log = AuditService.log
        written: list[str] = []

        def flaky_log(self, *args, **kwargs):
            if kwargs.get("resource_type") == "work_order_operation":
                return None  # the demotion row cannot be saved
            entry = real_log(self, *args, **kwargs)
            written.append(kwargs.get("resource_type"))
            return entry

        monkeypatch.setattr(AuditService, "log", flaky_log)

        resp = client.post(resume_url(wo.id), headers=user_headers(supervisor))

        assert resp.status_code == 503, resp.text
        assert written == ["work_order"], "the WO row must have been written before the failing op row"
        assert reread(db_session, WorkOrder, wo.id).status == WorkOrderStatus.ON_HOLD
        assert reread(db_session, WorkOrderOperation, op.id).status == OperationStatus.READY
        assert wo_status_rows(db_session, wo.id) == []
        assert op_status_rows(db_session, op.id) == []

    def test_end_to_end_held_job_refuses_clock_in_until_resumed(self, client, db_session):
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        stamp(db_session, wo, supervisor)
        body = {"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id, "entry_type": "run"}

        held = client.post("/api/v1/shop-floor/clock-in", json=body, headers=user_headers(operator))
        assert held.status_code == 409
        assert held.json()["detail"] == MSG_WORK_ORDER_ON_HOLD

        assert client.post(resume_url(wo.id), headers=user_headers(supervisor)).status_code == 200
        ok = client.post("/api/v1/shop-floor/clock-in", json=body, headers=user_headers(operator))
        assert ok.status_code == 200, ok.text
        assert reread(db_session, WorkOrder, wo.id).status == WorkOrderStatus.IN_PROGRESS


# =========================================================================== the pure helpers


class TestRestoreTargetAndDemotionCore:
    def _wo(self, db_session, **stamp_kwargs):
        wc = make_work_center(db_session)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
        if stamp_kwargs:
            stamp(db_session, wo, supervisor, **stamp_kwargs)
        return wo, op, wc

    def test_restore_target_rules(self, db_session):
        wo, op, _ = self._wo(db_session)
        assert work_order_hold_restore_target(wo, [op], set()) == (WorkOrderStatus.DRAFT, "no_release_record")
        # Work evidence does not rescue a job with no release record.
        assert work_order_hold_restore_target(wo, [op], {op.id}) == (WorkOrderStatus.DRAFT, "no_release_record")

        wo.released_by = 1
        assert work_order_hold_restore_target(wo, [op], set()) == (WorkOrderStatus.RELEASED, "release_record")
        assert work_order_hold_restore_target(wo, [op], {op.id}) == (
            WorkOrderStatus.IN_PROGRESS,
            "release_record_with_work",
        )
        wo.released_by = None
        wo.released_at = datetime.utcnow()
        wo.actual_start = datetime.utcnow()
        assert work_order_hold_restore_target(wo, [op], set())[0] == WorkOrderStatus.IN_PROGRESS
        wo.actual_start = None
        op.quantity_scrapped = 1
        assert work_order_hold_restore_target(wo, [op], set())[0] == WorkOrderStatus.IN_PROGRESS
        db_session.rollback()

    def test_all_unworked_scope_demotes_every_unworked_ready_op_including_laser(self, db_session):
        wo, op, wc = self._wo(db_session)
        wo.work_order_type = WorkOrderType.LASER_CUTTING.value
        db_session.commit()
        worked = add_op(db_session, wo, wc, sequence=20, status=OperationStatus.READY, quantity_complete=1)
        pending = add_op(db_session, wo, wc, sequence=30, status=OperationStatus.PENDING)

        # Laser pools are exempt by default (the sequencing posture)...
        assert demote_ready_operations(db_session, wo, scope="all_unworked") == []
        # ...and demoted when the caller says so (resume to DRAFT).
        demoted = demote_ready_operations(db_session, wo, scope="all_unworked", exempt_laser=False)
        assert [d.id for d in demoted] == [op.id]
        assert op.status == OperationStatus.PENDING
        assert worked.status == OperationStatus.READY and pending.status == OperationStatus.PENDING
        assert _worked_operation_ids(db_session, wo.company_id, [op, worked, pending]) == set()
        db_session.rollback()

    def test_sequencing_wrapper_is_the_core_with_the_sequencing_scope(self, db_session):
        wo, op10, wc = self._wo(db_session)
        wc_b = make_work_center(db_session)
        op20 = add_op(db_session, wo, wc_b, sequence=20, status=OperationStatus.READY)
        wo.sequential_operations = True
        db_session.commit()

        demoted = demote_operations_for_sequencing(db_session, wo)

        # OP10 is the lowest startable op and stays READY; OP20 is blocked by it.
        assert [d.id for d in demoted] == [op20.id]
        assert op10.status == OperationStatus.READY
        db_session.rollback()
        wo.sequential_operations = False
        assert demote_ready_operations(db_session, wo, scope="sequencing") == [], "pooled is a no-op"

    def test_unknown_scope_is_refused(self, db_session):
        wo, _, _ = self._wo(db_session)
        with pytest.raises(ValueError):
            demote_ready_operations(db_session, wo, scope="everything")  # type: ignore[arg-type]
