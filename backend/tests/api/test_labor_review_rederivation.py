"""Labor Review, second pass: the set-clock-out write paths the first file does not reach.

``tests/api/test_labor_review.py`` covers the flags, the refusals and the basic close /
correct re-derivation. This file pins what is left:

* the required audit really is required: an audit outage answers 503 and NOTHING moves
  (entry, operation hours, work-order hours), and no audit row lands;
* a concurrent writer that bumped the entry's version between the check and the flush
  answers 409 with nothing saved;
* a SETUP entry corrects the setup bucket and leaves run hours alone;
* stored hours BELOW the closed-entry evidence are left alone (``operation_hours_below_evidence``);
* an existing JobCost's TIME_ENTRY labor lines are rebuilt at the corrected duration;
* with the labor-cost rollup on, a COMPLETE work order's ``actual_cost`` is recomputed
  from the lowered hours (and is NOT touched with the rollup off);
* re-sending the stored clock-out is a no-op: ``changed=false``, no audit row, no version bump;
* an entry with no operation moves only the work order's hours;
* a refusal for another company's entry writes nothing (no change, no audit row);
* the list read is pure: it writes nothing, not even on an open, flagged entry;
* the shop-floor dashboard's ``active_assignments`` rows carry ``time_entry_version``.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.job_costing import CostEntry, CostEntrySource, JobCost, JobCostStatus
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services import labor_review_service
from app.services.audit_service import AuditService, AuditWriteError
from app.services.labor_review_service import SET_CLOCK_OUT_AUDIT_DETAIL, STALE_TIME_ENTRY_DETAIL
from tests.api.test_time_entry_approval_batch11b import (
    COMPANY_A,
    COMPANY_B,
    headers_for,
    make_closed_entry,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

LIST_URL = "/api/v1/shop-floor/time-entries"
DASHBOARD_URL = "/api/v1/shop-floor/dashboard"


def _set_url(entry_id: int) -> str:
    return f"/api/v1/shop-floor/time-entries/{entry_id}/set-clock-out"


def _iso(value: datetime) -> str:
    # Microseconds kept, so a computed duration is exact (fixtures stamp utcnow()).
    return value.isoformat() + "Z"


def _world(db: Session, *, company_id: int = COMPANY_A, hourly_rate: float = 100.0):
    operator = make_user(db, role=UserRole.OPERATOR, company_id=company_id)
    supervisor = make_user(db, role=UserRole.SUPERVISOR, company_id=company_id)
    part = make_part(db, company_id=company_id)
    wc = make_work_center(db, company_id=company_id, hourly_rate=hourly_rate)
    wo = make_wo(db, part, company_id=company_id)
    op = make_op(db, wo, wc, company_id=company_id)
    return operator, supervisor, wo, op


def _open_entry(db: Session, user, wo, op, *, hours_ago: float, company_id: int = COMPANY_A, op_id=True):
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id if op_id else None,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=hours_ago),
        company_id=company_id,
    )
    db.add(entry)
    db.flush()
    return entry


def _audit_rows(db: Session, entry_id: int):
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "time_entry",
            AuditLog.resource_id == entry_id,
            AuditLog.action == "TIME_ENTRY_SET_CLOCK_OUT",
        )
        .all()
    )


def _post(client: TestClient, actor, entry: TimeEntry, clock_out: datetime, *, reason: str = "Left clock running"):
    return client.post(
        _set_url(entry.id),
        json={"clock_out": _iso(clock_out), "reason": reason, "version": entry.version},
        headers=headers_for(actor),
    )


# --------------------------------------------------------------------------------------
# Required audit and concurrency
# --------------------------------------------------------------------------------------


def test_audit_outage_answers_503_and_saves_nothing(client: TestClient, db_session: Session, monkeypatch):
    operator, supervisor, wo, op = _world(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=10)
    op.actual_run_hours = 10.0
    wo.actual_hours = 10.0
    db_session.commit()
    entry_id, clock_out_before, version_before = entry.id, entry.clock_out, entry.version

    def refuse(self, *args, **kwargs):
        raise AuditWriteError("simulated audit outage")

    # Patched at log_required: a best-effort log_update (required=False) never reaches it,
    # so this only bites when the route really opted in to required evidence.
    monkeypatch.setattr(AuditService, "log_required", refuse)

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=3))
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == SET_CLOCK_OUT_AUDIT_DETAIL

    db_session.expire_all()
    stored = db_session.get(TimeEntry, entry_id)
    assert stored.clock_out == clock_out_before
    assert stored.duration_hours == pytest.approx(10.0)
    assert stored.version == version_before
    assert float(db_session.get(WorkOrderOperation, op.id).actual_run_hours) == pytest.approx(10.0)
    assert float(db_session.get(WorkOrder, wo.id).actual_hours) == pytest.approx(10.0)
    assert _audit_rows(db_session, entry_id) == []


def test_concurrent_version_bump_answers_409_and_saves_nothing(client: TestClient, db_session: Session, monkeypatch):
    operator, supervisor, wo, op = _world(db_session)
    entry = _open_entry(db_session, operator, wo, op, hours_ago=9)
    db_session.commit()
    entry_id = entry.id

    real_plan = labor_review_service._plan_hours

    def plan_then_race(db, company_id, te, *args, **kwargs):
        # Another writer commits a change to the row after our version check and before
        # our flush: the ORM's version_id_col UPDATE then matches no row.
        db.execute(text("UPDATE time_entries SET version = version + 1 WHERE id = :id"), {"id": te.id})
        return real_plan(db, company_id, te, *args, **kwargs)

    monkeypatch.setattr(labor_review_service, "_plan_hours", plan_then_race)

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=8))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == STALE_TIME_ENTRY_DETAIL

    db_session.expire_all()
    stored = db_session.get(TimeEntry, entry_id)
    assert stored.clock_out is None
    assert float(db_session.get(WorkOrderOperation, op.id).actual_run_hours or 0) == 0
    assert float(db_session.get(WorkOrder, wo.id).actual_hours or 0) == 0
    assert _audit_rows(db_session, entry_id) == []


# --------------------------------------------------------------------------------------
# Which stored hours move
# --------------------------------------------------------------------------------------


def test_setup_entry_corrects_the_setup_bucket_only(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=4)
    entry.entry_type = TimeEntryType.SETUP
    op.actual_setup_hours = 4.0
    op.actual_run_hours = 2.5
    wo.actual_hours = 6.5
    db_session.commit()

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=1))
    assert resp.status_code == 200, resp.text
    hours = resp.json()["hours"]
    assert hours["applied"] is True
    assert hours["operation_bucket"] == "setup"
    assert hours["operation_hours_before"] == pytest.approx(4.0)
    assert hours["operation_hours_after"] == pytest.approx(1.0)

    db_session.expire_all()
    stored_op = db_session.get(WorkOrderOperation, op.id)
    assert float(stored_op.actual_setup_hours) == pytest.approx(1.0)
    assert float(stored_op.actual_run_hours) == pytest.approx(2.5), "run hours are not this entry's bucket"
    assert float(db_session.get(WorkOrder, wo.id).actual_hours) == pytest.approx(3.5)

    audit = _audit_rows(db_session, entry.id)
    assert len(audit) == 1
    assert audit[0].old_values["operation_actual_setup_hours"] == pytest.approx(4.0)
    assert audit[0].new_values["operation_actual_setup_hours"] == pytest.approx(1.0)


def test_stored_hours_below_the_evidence_are_left_alone(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=10)
    # Stored hours lower than the closed entries explain: someone already changed them.
    op.actual_run_hours = 4.0
    wo.actual_hours = 4.0
    db_session.commit()

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=2))
    assert resp.status_code == 200, resp.text
    hours = resp.json()["hours"]
    assert hours["applied"] is False
    assert hours["skipped_reason"] == "operation_hours_below_evidence"

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).duration_hours == pytest.approx(2.0), "the entry itself still moves"
    assert float(db_session.get(WorkOrderOperation, op.id).actual_run_hours) == pytest.approx(4.0)
    assert float(db_session.get(WorkOrder, wo.id).actual_hours) == pytest.approx(4.0)
    audit = _audit_rows(db_session, entry.id)
    assert len(audit) == 1
    assert audit[0].extra_data["hours_rederived"] is False
    assert audit[0].extra_data["hours_skipped_reason"] == "operation_hours_below_evidence"


def test_entry_without_an_operation_moves_only_work_order_hours(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = _open_entry(db_session, operator, wo, op, hours_ago=6, op_id=False)
    db_session.commit()

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=5))
    assert resp.status_code == 200, resp.text
    hours = resp.json()["hours"]
    assert hours["applied"] is True and hours["operation_bucket"] is None

    db_session.expire_all()
    assert float(db_session.get(WorkOrder, wo.id).actual_hours) == pytest.approx(5.0)
    assert float(db_session.get(WorkOrderOperation, op.id).actual_run_hours or 0) == 0


def test_resending_the_stored_clock_out_is_a_no_op(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=3)
    entry.clock_out = entry.clock_out.replace(microsecond=0)
    entry.clock_in = entry.clock_in.replace(microsecond=0)
    op.actual_run_hours = 3.0
    wo.actual_hours = 3.0
    db_session.commit()
    version_before = entry.version

    resp = _post(client, supervisor, entry, entry.clock_out)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is False
    assert body["hours"] is None
    assert body["entry"]["version"] == version_before

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).version == version_before
    assert float(db_session.get(WorkOrderOperation, op.id).actual_run_hours) == pytest.approx(3.0)
    assert _audit_rows(db_session, entry.id) == []


# --------------------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------------------


def test_existing_job_cost_labor_lines_are_rebuilt(client: TestClient, db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_APPROVED_LABOR_FOR_COST", False)
    operator, supervisor, wo, op = _world(db_session, hourly_rate=100.0)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=10)
    op.actual_run_hours = 10.0
    wo.actual_hours = 10.0
    job_cost = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(job_cost)
    db_session.commit()

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=3))
    assert resp.status_code == 200, resp.text
    assert resp.json()["hours"]["job_cost_recomputed"] is True

    db_session.expire_all()
    stored = db_session.get(JobCost, job_cost.id)
    assert stored.actual_labor_cost == pytest.approx(300.0), "3 corrected hours x $100"
    lines = (
        db_session.query(CostEntry)
        .filter(CostEntry.job_cost_id == job_cost.id, CostEntry.source == CostEntrySource.TIME_ENTRY)
        .all()
    )
    assert len(lines) == 1
    audit = _audit_rows(db_session, entry.id)
    assert audit[0].extra_data["job_cost_recomputed"] is True


@pytest.mark.parametrize("rollup", [True, False])
def test_complete_work_order_actual_cost_follows_the_rollup_flag(
    client: TestClient, db_session: Session, monkeypatch, rollup: bool
):
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", rollup)
    operator, supervisor, wo, op = _world(db_session, hourly_rate=100.0)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=10)
    op.actual_run_hours = 10.0
    wo.actual_hours = 10.0
    wo.status = WorkOrderStatus.COMPLETE
    wo.actual_cost = 12345.0
    db_session.commit()

    # Lowering hours on a terminal work order is allowed (only raising is refused).
    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=4))
    assert resp.status_code == 200, resp.text
    hours = resp.json()["hours"]
    assert hours["applied"] is True
    assert hours["actual_cost_recomputed"] is rollup

    db_session.expire_all()
    stored_wo = db_session.get(WorkOrder, wo.id)
    assert float(stored_wo.actual_hours) == pytest.approx(4.0)
    if rollup:
        assert float(stored_wo.actual_cost) != pytest.approx(12345.0)
        assert float(stored_wo.actual_cost) >= 400.0, "4 hours x $100 labor at least"
        audit = _audit_rows(db_session, entry.id)[0]
        assert audit.old_values["work_order_actual_cost"] == pytest.approx(12345.0)
    else:
        assert float(stored_wo.actual_cost) == pytest.approx(12345.0)


def test_terminal_work_order_correction_upward_does_not_raise_hours(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=2)
    entry.clock_in = datetime.utcnow() - timedelta(hours=9)
    entry.clock_out = entry.clock_in + timedelta(hours=2)
    op.actual_run_hours = 2.0
    wo.actual_hours = 2.0
    wo.status = WorkOrderStatus.COMPLETE
    db_session.commit()

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=6))
    assert resp.status_code == 200, resp.text
    assert resp.json()["hours"]["skipped_reason"] == "terminal_work_order"
    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).duration_hours == pytest.approx(6.0)
    assert float(db_session.get(WorkOrder, wo.id).actual_hours) == pytest.approx(2.0)
    assert float(db_session.get(WorkOrderOperation, op.id).actual_run_hours) == pytest.approx(2.0)


# --------------------------------------------------------------------------------------
# Tenant scope and pure reads
# --------------------------------------------------------------------------------------


def test_other_company_entry_is_404_with_no_change_and_no_audit(client: TestClient, db_session: Session):
    _operator, supervisor, _wo, _op = _world(db_session)
    operator_b, _sup_b, wo_b, op_b = _world(db_session, company_id=COMPANY_B)
    foreign = _open_entry(db_session, operator_b, wo_b, op_b, hours_ago=5, company_id=COMPANY_B)
    db_session.commit()
    version_before = foreign.version

    resp = _post(client, supervisor, foreign, foreign.clock_in + timedelta(hours=1))
    assert resp.status_code == 404

    db_session.expire_all()
    stored = db_session.get(TimeEntry, foreign.id)
    assert stored.clock_out is None and stored.version == version_before
    assert float(db_session.get(WorkOrder, wo_b.id).actual_hours or 0) == 0
    assert _audit_rows(db_session, foreign.id) == []


def test_list_read_writes_nothing(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    forgotten = _open_entry(db_session, operator, wo, op, hours_ago=20)
    op.status = OperationStatus.COMPLETE
    closed = make_closed_entry(db_session, operator, wo, op, duration_hours=5)
    db_session.commit()
    before = {
        "audit": db_session.query(AuditLog).count(),
        "forgotten": (forgotten.version, forgotten.clock_out),
        "closed": (closed.version, closed.clock_out, closed.duration_hours),
        "op": (op.version, op.actual_run_hours, op.status),
        "wo": (wo.version, wo.actual_hours, wo.status),
    }

    for params in ({}, {"status": "open"}, {"flagged_only": "true"}, {"flag": "long_entry"}):
        resp = client.get(LIST_URL, params=params, headers=headers_for(supervisor))
        assert resp.status_code == 200, resp.text

    db_session.expire_all()
    forgotten = db_session.get(TimeEntry, forgotten.id)
    closed = db_session.get(TimeEntry, closed.id)
    op = db_session.get(WorkOrderOperation, op.id)
    wo = db_session.get(WorkOrder, wo.id)
    after = {
        "audit": db_session.query(AuditLog).count(),
        "forgotten": (forgotten.version, forgotten.clock_out),
        "closed": (closed.version, closed.clock_out, closed.duration_hours),
        "op": (op.version, op.actual_run_hours, op.status),
        "wo": (wo.version, wo.actual_hours, wo.status),
    }
    assert after == before


def test_dashboard_assignment_carries_the_entry_version(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = _open_entry(db_session, operator, wo, op, hours_ago=1)
    db_session.commit()

    resp = client.get(DASHBOARD_URL, headers=headers_for(supervisor))
    assert resp.status_code == 200, resp.text
    rows = [row for row in resp.json().get("active_assignments", []) if row["time_entry_id"] == entry.id]
    assert len(rows) == 1
    db_session.expire_all()
    assert rows[0]["time_entry_version"] == db_session.get(TimeEntry, entry.id).version


def test_an_open_entry_approved_before_the_rule_can_still_be_unapproved(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = _open_entry(db_session, operator, wo, op, hours_ago=2)
    entry.approved = datetime.utcnow()
    entry.approved_by = supervisor.id
    db_session.commit()

    resp = client.post(f"/api/v1/shop-floor/time-entries/{entry.id}/unapprove", headers=headers_for(supervisor))
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    stored = db_session.get(TimeEntry, entry.id)
    assert stored.approved is None and stored.clock_out is None


def test_job_cost_rebuild_writes_required_recalculate_evidence(client: TestClient, db_session: Session, monkeypatch):
    """The rebuilt labor lines are a job-cost mutation: a RECALCULATE job_cost row records them."""
    monkeypatch.setattr(settings, "REQUIRE_APPROVED_LABOR_FOR_COST", False)
    operator, supervisor, wo, op = _world(db_session, hourly_rate=100.0)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=10)
    op.actual_run_hours = 10.0
    wo.actual_hours = 10.0
    job_cost = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(job_cost)
    db_session.commit()

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=3))
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "job_cost", AuditLog.resource_id == job_cost.id)
        .all()
    )
    assert [row.action for row in rows] == ["RECALCULATE"]
    assert rows[0].extra_data["time_entry_id"] == entry.id
    assert set(rows[0].new_values) == {"job_cost", "entries"}
    assert len(rows[0].new_values["entries"]) == 1


def test_soft_deleted_work_order_job_cost_is_not_rebuilt(client: TestClient, db_session: Session, monkeypatch):
    """The job-cost module refuses to write a deleted work order's job cost; so does this verb."""
    monkeypatch.setattr(settings, "REQUIRE_APPROVED_LABOR_FOR_COST", False)
    operator, supervisor, wo, op = _world(db_session, hourly_rate=100.0)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=10)
    op.actual_run_hours = 10.0
    wo.actual_hours = 10.0
    job_cost = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(job_cost)
    db_session.flush()
    wo.soft_delete(supervisor.id)
    db_session.commit()

    resp = _post(client, supervisor, entry, entry.clock_in + timedelta(hours=3))
    assert resp.status_code == 200, resp.text
    assert resp.json()["hours"]["job_cost_recomputed"] is False

    db_session.expire_all()
    assert (
        db_session.query(CostEntry).filter(CostEntry.job_cost_id == job_cost.id).count() == 0
    ), "no labor lines were rebuilt on the deleted work order's job cost"
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "job_cost", AuditLog.resource_id == job_cost.id)
        .count()
        == 0
    )
    audit = _audit_rows(db_session, entry.id)
    assert audit[0].extra_data["job_cost_skipped_reason"] == "work_order_deleted"
