"""Behavior locks for the Batch-7 labor-hour + cost rollup (Rank 10).

Covered findings:
- COST-3/COST-4: labor hours roll up into op/WO actuals on completion, summed across
  ALL operators on an operation (multi-welder case), monotonic-up.
- COST-1: WorkOrder.actual_cost = labor + issued material + overhead (flag ON).
- COST-2: a linked JobCost is synced (actuals + status COMPLETED) on completion.
- COST-5: one shared labor rate (WorkCenter.hourly_rate) used by the rollup AND by
  analytics.get_cost_analysis.
- OPT-IN gating: flag OFF preserves pre-Batch-7 behavior (no auto cost/hours/JobCost on
  ANY path -- live or reconcile -- and no computed labor/overhead in the cost-analysis
  report); flag ON auto-rolls everything and the report computes labor at the WC rate.
  (Flag-consistency change: the reconcile path's Batch-7 HOUR rollup and the analytics
  labor/overhead legs are now flag-gated too, so the OPT-IN flag governs ALL Batch-7 cost
  surfacing uniformly. Previously the reconcile path rolled hours flag-OFF and the
  analytics report computed labor flag-OFF; both are now gated.)
- no_labor_recorded data-quality signal fires REGARDLESS of the cost flag.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.inventory import InventoryTransaction, TransactionType
from app.models.job_costing import CostEntry, CostEntrySource, JobCost, JobCostStatus
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    n = _next()
    user = User(
        email=f"b7-{n}@co{company_id}.test",
        employee_id=f"B7-{n:05d}",
        first_name="B7",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session, company_id: int = COMPANY_A, *, standard_cost: float = 0.0) -> Part:
    n = _next()
    part = Part(
        part_number=f"B7-P-{n}",
        name=f"Part {n}",
        description="batch7 fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        standard_cost=standard_cost,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, company_id: int = COMPANY_A, *, hourly_rate: float = 100.0) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        name=f"B7-WC-{n}",
        code=f"B7-WC-{n}",
        work_center_type="welding",
        description="batch7 fixture work center",
        hourly_rate=hourly_rate,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session,
    part: Part,
    *,
    status_: WorkOrderStatus,
    quantity_ordered: float = 10,
    company_id: int = COMPANY_A,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B7-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int,
    status_: OperationStatus,
    quantity_complete: float = 0,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        quantity_complete=quantity_complete,
        company_id=company_id,
    )
    db.add(op)
    db.flush()
    return op


def make_closed_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    duration_hours: float,
    entry_type: TimeEntryType = TimeEntryType.RUN,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    """A CLOSED time entry (clock_out set, duration populated) — durable labor evidence."""
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=entry_type,
        clock_in=datetime.utcnow() - timedelta(hours=duration_hours),
        clock_out=datetime.utcnow(),
        duration_hours=duration_hours,
        company_id=company_id,
    )
    db.add(entry)
    db.flush()
    return entry


def make_open_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    hours_ago: float,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    """An OPEN time entry (clocked in ``hours_ago``, not yet clocked out)."""
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=hours_ago),
        clock_out=None,
        company_id=company_id,
    )
    db.add(entry)
    db.flush()
    return entry


def make_issue_txn(
    db: Session,
    wo: WorkOrder,
    part: Part,
    user: User,
    *,
    total_cost: float,
    company_id: int = COMPANY_A,
) -> InventoryTransaction:
    """An ISSUE InventoryTransaction referencing the WO (the Batch-6 backflush/issue txn).

    ISSUE quantities/costs are stored NEGATIVE (material leaving stock), so we book
    ``total_cost`` negative; the rollup/analytics read the magnitude. ``reference_type``
    must be the literal ``"work_order"`` and ``reference_id`` the WO id.
    """
    txn = InventoryTransaction(
        part_id=part.id,
        transaction_type=TransactionType.ISSUE,
        quantity=-1.0,
        reference_type="work_order",
        reference_id=wo.id,
        unit_cost=total_cost,
        total_cost=-abs(total_cost),
        created_by=user.id,
        company_id=company_id,
    )
    db.add(txn)
    db.flush()
    return txn


def _enable_rollup(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", True)


def _disable_rollup(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", False)


def _set_default_labor_rate(monkeypatch, rate: float) -> None:
    monkeypatch.setattr(settings, "DEFAULT_LABOR_RATE", rate)


def _set_overhead_rate(monkeypatch, rate: float) -> None:
    monkeypatch.setattr(settings, "DEFAULT_OVERHEAD_RATE", rate)


# ---------------------------------------------------------------------------
# COST-3/COST-4: multi-operator hours SUM (the multi-welder invariant)
# ---------------------------------------------------------------------------


def test_hours_sum_across_multiple_operators_on_one_operation(client, db_session, monkeypatch):
    """Two welders on ONE operation: their hours SUM (never deduped by operation)."""
    _enable_rollup(monkeypatch)
    admin = make_user(db_session)
    welder_b = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    # Two distinct operators each logged closed labor on the SAME operation.
    make_closed_entry(db_session, admin, wo, op, duration_hours=2.0)
    make_closed_entry(db_session, welder_b, wo, op, duration_hours=3.0)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op.id)
    wo = db_session.get(WorkOrder, wo.id)
    # 2.0 + 3.0 hours summed across both operators (NOT 2.0 or 3.0 alone).
    assert op.actual_run_hours == pytest.approx(5.0)
    assert wo.actual_hours == pytest.approx(5.0)
    # COST-1/COST-5: actual_cost = 5 hr x $100 WC rate (+ 0 material + 0 overhead).
    assert wo.actual_cost == pytest.approx(500.0)


def test_setup_and_run_hours_split_by_entry_type(client, db_session, monkeypatch):
    """SETUP entries credit actual_setup_hours; RUN entries credit actual_run_hours."""
    _enable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=3)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=1.5, entry_type=TimeEntryType.SETUP)
    make_closed_entry(db_session, admin, wo, op, duration_hours=2.5, entry_type=TimeEntryType.RUN)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=3",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op.id)
    wo = db_session.get(WorkOrder, wo.id)
    assert op.actual_setup_hours == pytest.approx(1.5)
    assert op.actual_run_hours == pytest.approx(2.5)
    assert wo.actual_hours == pytest.approx(4.0)
    assert wo.actual_cost == pytest.approx(400.0)  # 4 hr x $100


# ---------------------------------------------------------------------------
# COST-3: shop-floor complete auto-closes OPEN entries -> their hours roll up
# ---------------------------------------------------------------------------


def test_shop_floor_complete_rolls_auto_closed_entry_hours(client, db_session, monkeypatch):
    """The shop-floor /complete auto-closes open entries; their hours roll into actuals.

    Two operators are still clocked in (no explicit clock-out) when an operator hits
    'complete'. Their open entries are auto-closed AND their durations rolled into the
    op/WO actuals (the COST-3 data-loss this batch fixes), summed across both operators.
    """
    _enable_rollup(monkeypatch)
    admin = make_user(db_session)
    welder_b = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_open_entry(db_session, admin, wo, op, hours_ago=2.0)
    make_open_entry(db_session, welder_b, wo, op, hours_ago=3.0)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        json={"quantity_complete": 5},
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op.id)
    wo = db_session.get(WorkOrder, wo.id)
    # ~2 + ~3 hours from the two auto-closed entries (allow float drift on the clock-out).
    assert op.actual_run_hours == pytest.approx(5.0, abs=0.05)
    assert wo.actual_hours == pytest.approx(5.0, abs=0.05)
    assert wo.actual_cost == pytest.approx(500.0, abs=5.0)


# ---------------------------------------------------------------------------
# OPT-IN gating: flag OFF preserves pre-Batch-7 behavior
# ---------------------------------------------------------------------------


def test_flag_off_does_not_populate_cost_or_hours(client, db_session, monkeypatch):
    """Flag OFF (default): completion does NOT auto-populate actual_cost/actual_hours."""
    _disable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=4.0)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    # Pre-Batch-7 behavior preserved: no auto rollup of cost/hours.
    assert wo.actual_cost == pytest.approx(0.0)
    assert wo.actual_hours == pytest.approx(0.0)


def test_flag_off_does_not_sync_job_cost(client, db_session, monkeypatch):
    """Flag OFF: a linked JobCost stays IN_PROGRESS with stale actuals (on-demand only)."""
    _disable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=4.0)
    jc = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(jc)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    jc = db_session.get(JobCost, jc.id)
    assert jc.status == JobCostStatus.IN_PROGRESS
    assert jc.actual_labor_cost == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# COST-2: flag ON syncs the linked JobCost (labor + status COMPLETED)
# ---------------------------------------------------------------------------


def test_flag_on_syncs_linked_job_cost(client, db_session, monkeypatch):
    """Flag ON: completion regenerates JobCost labor at the WC rate and flips COMPLETED."""
    _enable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=4.0)
    jc = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(jc)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    jc = db_session.get(JobCost, jc.id)
    assert jc.status == JobCostStatus.COMPLETED
    # 4 closed labor hours x $100 WC rate (COST-5 shared rate, NOT the old $45).
    assert jc.actual_labor_cost == pytest.approx(400.0)
    assert jc.actual_total_cost == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# no_labor_recorded data-quality signal — fires regardless of the cost flag
# ---------------------------------------------------------------------------


def _no_labor_audit(db: Session) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == "COMPLETED_WITH_QUALITY_EXCEPTION",
        )
        .all()
    )


def test_no_labor_recorded_signal_fires_with_flag_off(client, db_session, monkeypatch):
    """A WO completing with a ZERO-labor operation raises no_labor_recorded even flag-OFF."""
    _disable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    # NO time entry on the op -> zero labor recorded.
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "no_labor_recorded" in codes
    # And it left a tamper-evident audit row (reused Batch-4 mechanism).
    assert len(_no_labor_audit(db_session)) >= 1


def test_no_labor_recorded_signal_absent_when_labor_present(client, db_session, monkeypatch):
    """An op with real closed labor does NOT raise no_labor_recorded."""
    _disable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=1.0)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "no_labor_recorded" not in codes


# ---------------------------------------------------------------------------
# COST-5: on-demand /job-costs/{id}/calculate uses the SHARED WC rate (not $45)
# and is tenant-scoped.
# ---------------------------------------------------------------------------


def test_on_demand_calculate_uses_shared_rate(client, db_session, monkeypatch):
    """The /calculate endpoint costs labor at the WC rate (COST-5), not the old $45."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=80.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=2.0)
    jc = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(jc)
    db_session.commit()

    resp = client.post(f"/api/v1/job-costs/{jc.id}/calculate", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # 2 hr x $80 WC rate = $160 (was $90 under the hardcoded $45).
    assert body["actual_labor_cost"] == pytest.approx(160.0)


def test_on_demand_calculate_is_tenant_scoped(client, db_session, monkeypatch):
    """A user from company A cannot /calculate a company-B job cost (404)."""
    admin_a = make_user(db_session, company_id=COMPANY_A)
    part_b = make_part(db_session, company_id=2)
    wc_b = make_work_center(db_session, company_id=2)
    wo_b = make_wo(db_session, part_b, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5, company_id=2)
    make_op(db_session, wo_b, wc_b, sequence=10, status_=OperationStatus.IN_PROGRESS, company_id=2)
    jc_b = JobCost(work_order_id=wo_b.id, status=JobCostStatus.IN_PROGRESS, company_id=2)
    db_session.add(jc_b)
    db_session.commit()

    resp = client.post(f"/api/v1/job-costs/{jc_b.id}/calculate", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


# ===========================================================================
# Matrix gap fills (the comprehensive Batch-7 test matrix). Everything below
# extends the original 10 locks above; it does not replace them.
# ===========================================================================


# ---------------------------------------------------------------------------
# Matrix #1 (flag OFF, full): the LIVE op-complete path does NOT auto-populate
# WorkOrder.actual_cost and does NOT flip a linked JobCost; on-demand /calculate
# still works flag-OFF. (Locks the EXACT live-path flag-off behavior: NO hours
# rollup either -- the live rollup is gated, unlike the reconcile path below.)
# ---------------------------------------------------------------------------


def test_flag_off_live_complete_no_cost_no_jobcost_but_calculate_still_works(client, db_session, monkeypatch):
    """Flag OFF on the LIVE path: no actual_cost, no actual_hours, JobCost stays
    IN_PROGRESS -- yet the on-demand /calculate still recomputes it on request."""
    _disable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=3.0)
    jc = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(jc)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    jc = db_session.get(JobCost, jc.id)
    # LIVE flag-OFF: no auto rollup of cost/hours; JobCost untouched.
    assert wo.actual_cost == pytest.approx(0.0)
    assert wo.actual_hours == pytest.approx(0.0)
    assert jc.status == JobCostStatus.IN_PROGRESS
    assert jc.actual_labor_cost == pytest.approx(0.0)

    # The on-demand path still works flag-OFF: 3 hr x $100 WC rate = $300.
    calc = client.post(f"/api/v1/job-costs/{jc.id}/calculate", headers=headers_for(admin))
    assert calc.status_code == status.HTTP_200_OK, calc.text
    assert calc.json()["actual_labor_cost"] == pytest.approx(300.0)


def test_flag_off_reconcile_rolls_no_hours_no_cost_no_jobcost(client, db_session, monkeypatch):
    """Flag OFF on the RECONCILE-on-read path: NO Batch-7 rollup at all.

    FLAG-CONSISTENCY CHANGE: the reconcile path's Batch-7 hour rollup is now flag-gated
    just like the cost/JobCost rollup (and like the live completion paths). Previously
    this test asserted that hours rolled up flag-OFF on the reconcile path (path-dependent
    behavior: live=no hours, reconcile=hours-only). The product decision is that the
    OPT-IN flag must govern ALL Batch-7 cost/hours surfacing uniformly, so flag-OFF a
    reconcile completion now surfaces ZERO computed hours, cost, AND a stale JobCost --
    matching the live path exactly. (The WO still reconciles to COMPLETE from durable
    evidence; only the Batch-7 hour/cost SURFACING is gated. The pre-existing clock_out
    accumulation is a separate mechanism, not exercised here.)"""
    _disable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    # A closed entry whose produced qty >= target drives the op/WO to COMPLETE on a GET.
    entry = make_closed_entry(db_session, admin, wo, op, duration_hours=6.0)
    entry.quantity_produced = 4.0
    jc = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(jc)
    db_session.commit()

    # GET the WO detail -> reconcile-on-read drives it COMPLETE but, flag-OFF, rolls NO
    # Batch-7 hours/cost.
    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    op = db_session.get(WorkOrderOperation, op.id)
    jc = db_session.get(JobCost, jc.id)
    assert wo.status == WorkOrderStatus.COMPLETE
    # Flag-OFF: NO Batch-7 hour rollup on the reconcile path (now gated, was unconditional).
    assert op.actual_run_hours == pytest.approx(0.0)
    assert wo.actual_hours == pytest.approx(0.0)
    # ...and cost and the JobCost are likewise untouched (gated behind the flag).
    assert wo.actual_cost == pytest.approx(0.0)
    assert jc.status == JobCostStatus.IN_PROGRESS
    assert jc.actual_labor_cost == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Matrix #2 (flag ON, full): actual_cost = labor + issued-material + overhead,
# actual_hours populated, linked JobCost -> COMPLETED with recomputed actuals.
# ---------------------------------------------------------------------------


def test_flag_on_actual_cost_is_labor_plus_material_plus_overhead(client, db_session, monkeypatch):
    """Flag ON: actual_cost = Σ(op hours × WC rate) + Σ|ISSUE total_cost| + Σ(hours ×
    overhead rate), with actual_hours populated, asserted with KNOWN numbers."""
    _enable_rollup(monkeypatch)
    _set_overhead_rate(monkeypatch, 20.0)  # $20/hr overhead so the overhead leg is non-zero
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=4.0)
    # $400 of ISSUE'd material on the WO (one ISSUE txn -- the SQLite test DB ignores the
    # partial-index predicate and treats uq_wo_inventory_issue/receipt as full unique
    # indexes, so a single txn keeps the fixture index-clean while still proving the
    # material leg is summed via Σ|ISSUE total_cost|).
    comp = make_part(db_session)
    make_issue_txn(db_session, wo, comp, admin, total_cost=400.0)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    # labor   = 4 hr x $100 = 400
    # material= |−150| + |−250| = 400
    # overhead= 4 hr x $20 = 80
    # total   = 880
    assert wo.actual_hours == pytest.approx(4.0)
    assert wo.actual_cost == pytest.approx(880.0)


def test_flag_on_jobcost_completed_with_recomputed_actuals(client, db_session, monkeypatch):
    """Flag ON: a linked JobCost flips to COMPLETED with TIME_ENTRY labor regenerated
    at the shared WC rate (a TIME_ENTRY-sourced CostEntry exists with the right total)."""
    _enable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=120.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=2.5)
    jc = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(jc)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    jc = db_session.get(JobCost, jc.id)
    assert jc.status == JobCostStatus.COMPLETED
    # 2.5 hr x $120 = $300 regenerated labor.
    assert jc.actual_labor_cost == pytest.approx(300.0)
    assert jc.actual_total_cost == pytest.approx(300.0)
    # And the regenerated entry is sourced from the TimeEntry (COST-2 shared recompute).
    auto_entries = (
        db_session.query(CostEntry)
        .filter(CostEntry.job_cost_id == jc.id, CostEntry.source == CostEntrySource.TIME_ENTRY)
        .all()
    )
    assert len(auto_entries) == 1
    assert auto_entries[0].total_cost == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# Matrix #3 (rate source COST-5): WC.hourly_rate else DEFAULT_LABOR_RATE; the
# rollup AND analytics.get_cost_analysis use the SAME rate; two WCs cost each
# operation's hours at its own WC rate.
# ---------------------------------------------------------------------------


def test_rate_falls_back_to_default_when_work_center_has_no_rate(client, db_session, monkeypatch):
    """A work center with a zero/unset hourly_rate falls back to settings.DEFAULT_LABOR_RATE."""
    _enable_rollup(monkeypatch)
    _set_default_labor_rate(monkeypatch, 90.0)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=0.0)  # no positive WC rate -> default
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=2.0)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    # 2 hr x $90 DEFAULT_LABOR_RATE (the WC rate was 0 -> fallback).
    assert wo.actual_cost == pytest.approx(180.0)


def test_two_work_centers_cost_each_operation_at_its_own_rate(client, db_session, monkeypatch):
    """A WO across two work centers with different rates costs each operation's hours
    at ITS OWN work-center rate (labor cost reflects WHERE the work happened)."""
    _enable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc_a = make_work_center(db_session, hourly_rate=100.0)
    wc_b = make_work_center(db_session, hourly_rate=60.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op_a = make_op(db_session, wo, wc_a, sequence=10, status_=OperationStatus.IN_PROGRESS)
    op_b = make_op(db_session, wo, wc_b, sequence=20, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op_a, duration_hours=2.0)  # 2 x $100 = 200
    make_closed_entry(db_session, admin, wo, op_b, duration_hours=3.0)  # 3 x $60  = 180
    db_session.commit()

    # Complete op_a, then op_b (the second completion drives the WO to COMPLETE).
    r1 = client.post(
        f"/api/v1/work-orders/operations/{op_a.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert r1.status_code == status.HTTP_200_OK, r1.text
    r2 = client.post(
        f"/api/v1/work-orders/operations/{op_b.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert r2.status_code == status.HTTP_200_OK, r2.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.actual_hours == pytest.approx(5.0)
    # 200 (op_a @ $100) + 180 (op_b @ $60) = 380 -- NOT 5 x either single rate.
    assert wo.actual_cost == pytest.approx(380.0)


def test_get_cost_analysis_uses_same_rate_and_is_not_structurally_zero(client, db_session, monkeypatch):
    """analytics.get_cost_analysis charges the SAME shared WC rate as the rollup, so a
    flag-ON completed WO surfaces a non-zero labor cost (the old hardcoded *50 path is
    gone). It also no longer reports a structurally-zero labor leg."""
    _enable_rollup(monkeypatch)
    admin = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=4.0)
    db_session.commit()

    done = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert done.status_code == status.HTTP_200_OK, done.text

    # The WO is now COMPLETE with actual_cost = 4 x $100 = $400; analytics must agree.
    resp = client.get(
        f"/api/v1/analytics/cost-analysis?work_order_id={wo.id}",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    jobs = body["jobs"]
    assert len(jobs) == 1, body
    job = jobs[0]
    # Same shared rate the rollup used: labor leg = 4 hr x $100 = 400 (NOT 4 x $50 = 200,
    # and NOT a structural zero).
    assert job["cost_breakdown"]["labor_cost"] == pytest.approx(400.0)
    assert job["actual_cost"] == pytest.approx(400.0)
    assert body["summary"]["total_actual"] == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# Matrix #5 (reconcile COST-4): a reconcile-on-read that drives a WO to COMPLETE
# rolls hours from durable evidence, monotonic-up, read-safe (200).
# ---------------------------------------------------------------------------


def test_reconcile_on_read_rolls_hours_monotonic_up_and_is_read_safe(client, db_session, monkeypatch):
    """A reconcile-driven completion (on a GET) rolls op/WO hours from durable closed
    TimeEntry evidence; a SECOND GET does not reduce them; both GETs return 200."""
    _enable_rollup(monkeypatch)
    admin = make_user(db_session)
    welder_b = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    # Two operators' closed entries; combined produced qty (4) >= target (4) -> COMPLETE.
    e1 = make_closed_entry(db_session, admin, wo, op, duration_hours=2.0)
    e1.quantity_produced = 2.0
    e2 = make_closed_entry(db_session, welder_b, wo, op, duration_hours=3.0)
    e2.quantity_produced = 2.0
    db_session.commit()

    r1 = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert r1.status_code == status.HTTP_200_OK, r1.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    op = db_session.get(WorkOrderOperation, op.id)
    assert wo.status == WorkOrderStatus.COMPLETE
    # 2 + 3 hours summed across BOTH operators on the one operation (COST-4 rollup).
    assert op.actual_run_hours == pytest.approx(5.0)
    assert wo.actual_hours == pytest.approx(5.0)

    # A SECOND reconcile read must not reduce the rolled-up hours (monotonic-up).
    r2 = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert r2.status_code == status.HTTP_200_OK, r2.text
    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op.id)
    wo = db_session.get(WorkOrder, wo.id)
    assert op.actual_run_hours == pytest.approx(5.0)
    assert wo.actual_hours == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Matrix #6 (no_labor_recorded): fires flag-ON too (it is a process signal, not a
# cost figure) -- complements the existing flag-OFF + labor-present locks.
# ---------------------------------------------------------------------------


def test_no_labor_recorded_signal_fires_with_flag_on(client, db_session, monkeypatch):
    """The no_labor_recorded signal fires even when the cost-rollup flag is ON: a
    zero-labor operation still raises it (audit row + response field)."""
    _enable_rollup(monkeypatch)
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    # NO time entry on the op -> zero labor recorded.
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "no_labor_recorded" in codes
    assert len(_no_labor_audit(db_session)) >= 1


# ---------------------------------------------------------------------------
# Matrix #7 (tenant scoping): the issued-material read and the JobCost sync are
# company-scoped -- a company-A completion never reads company-B material or a
# company-B JobCost.
# ---------------------------------------------------------------------------


def test_completion_ignores_other_tenant_issued_material(client, db_session, monkeypatch):
    """A company-B ISSUE txn that happens to reference company-A's WO id is NOT read
    into company-A's actual_cost (the material read is company-scoped)."""
    _enable_rollup(monkeypatch)
    admin_a = make_user(db_session, company_id=COMPANY_A)
    part_a = make_part(db_session, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A, hourly_rate=100.0)
    wo_a = make_wo(db_session, part_a, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op_a = make_op(db_session, wo_a, wc_a, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin_a, wo_a, op_a, duration_hours=2.0)  # 2 x $100 = 200
    # A foreign-company ISSUE txn with the SAME reference_id as wo_a but company_id=2.
    part_b = make_part(db_session, company_id=2)
    foreign_user = make_user(db_session, company_id=2)
    foreign_txn = InventoryTransaction(
        part_id=part_b.id,
        transaction_type=TransactionType.ISSUE,
        quantity=-1.0,
        reference_type="work_order",
        reference_id=wo_a.id,  # collides on id, but belongs to company B
        unit_cost=9999.0,
        total_cost=-9999.0,
        created_by=foreign_user.id,
        company_id=2,
    )
    db_session.add(foreign_txn)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_a.id}/complete?quantity_complete=5",
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo_a = db_session.get(WorkOrder, wo_a.id)
    # Only company-A labor ($200), NONE of the $9999 company-B "material".
    assert wo_a.actual_cost == pytest.approx(200.0)


def test_completion_does_not_sync_other_tenant_job_cost(client, db_session, monkeypatch):
    """A company-B JobCost linked to a colliding WO id is NEVER synced/flipped by a
    company-A completion (the JobCost sync is company-scoped)."""
    _enable_rollup(monkeypatch)
    admin_a = make_user(db_session, company_id=COMPANY_A)
    part_a = make_part(db_session, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A, hourly_rate=100.0)
    wo_a = make_wo(db_session, part_a, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op_a = make_op(db_session, wo_a, wc_a, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin_a, wo_a, op_a, duration_hours=2.0)
    # A company-B JobCost referencing the same WO id -- must stay IN_PROGRESS/untouched.
    jc_b = JobCost(work_order_id=wo_a.id, status=JobCostStatus.IN_PROGRESS, company_id=2)
    db_session.add(jc_b)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_a.id}/complete?quantity_complete=5",
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    jc_b = db_session.get(JobCost, jc_b.id)
    # The foreign JobCost is untouched: still IN_PROGRESS, zero labor.
    assert jc_b.status == JobCostStatus.IN_PROGRESS
    assert jc_b.actual_labor_cost == pytest.approx(0.0)


# ===========================================================================
# Flag-consistency locks (the should-fix from the Batch-7 review): the OPT-IN
# cost flag must govern ALL Batch-7 cost surfacing uniformly. Flag-OFF, NO path
# (live, reconcile, or the cost-analysis report) surfaces a computed labor/cost
# figure; flag-ON, every path populates. Material cost is NOT gated (it is real
# issued-material from inventory, not a labor estimate).
# ===========================================================================


def test_get_cost_analysis_reports_zero_labor_flag_off_live_completed(client, db_session, monkeypatch):
    """Flag OFF: get_cost_analysis reports $0 labor/overhead for a live-completed WO that
    has accumulated actual hours.

    This is the analytics half of the flag-consistency fix. Even when a WO carries
    non-zero actual_setup/run hours, the cost-analysis report surfaces NO computed labor
    or overhead while the flag is OFF (it is reported as 0 / not-tracked). Material is
    still surfaced (real issued-material). Previously the report computed labor from
    actual_hours x rate with no flag check, so a WO with hours leaked a non-zero labor
    figure flag-OFF; that is now gated."""
    _disable_rollup(monkeypatch)
    admin = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE)
    # Directly seed accumulated actual hours on the op/WO (as if a prior flag-ON rollup or
    # the pre-existing clock_out accumulation had populated them) so we prove the ANALYTICS
    # gate, independent of how the hours got there. Also give the WO a completion window so
    # the date-ranged report would pick it up.
    op.actual_run_hours = 4.0
    wo.actual_hours = 4.0
    wo.actual_end = datetime.utcnow()
    # Real issued material ($250) -- must still surface flag-OFF.
    comp = make_part(db_session)
    make_issue_txn(db_session, wo, comp, admin, total_cost=250.0)
    db_session.commit()

    resp = client.get(
        f"/api/v1/analytics/cost-analysis?work_order_id={wo.id}",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    jobs = resp.json()["jobs"]
    assert len(jobs) == 1, resp.json()
    breakdown = jobs[0]["cost_breakdown"]
    # Flag-OFF: NO computed labor/overhead even though the WO has 4 actual hours.
    assert breakdown["labor_cost"] == pytest.approx(0.0)
    assert breakdown["overhead_cost"] == pytest.approx(0.0)
    # ...but real issued material IS still surfaced (it is not labor-estimate-dependent).
    assert breakdown["material_cost"] == pytest.approx(250.0)


def test_get_cost_analysis_zero_labor_flag_off_for_reconcile_completed_wo(client, db_session, monkeypatch):
    """Flag OFF: a reconcile-COMPLETED WO also reports $0 labor in get_cost_analysis.

    The bug this closes: flag-OFF, a reconcile-completed WO used to show a non-zero labor
    cost in the report (because the reconcile path rolled hours unconditionally AND
    analytics computed labor from those hours with no flag check) while a live-completed WO
    showed $0 -- an inconsistency. With both halves gated, a reconcile-completed WO now
    shows $0 labor too, matching the live path."""
    _disable_rollup(monkeypatch)
    admin = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    # Durable evidence that drives the op/WO to COMPLETE on a GET (produced qty >= target).
    entry = make_closed_entry(db_session, admin, wo, op, duration_hours=5.0)
    entry.quantity_produced = 4.0
    db_session.commit()

    # Reconcile-on-read drives the WO to COMPLETE (flag-OFF: rolls NO Batch-7 hours).
    r = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert r.status_code == status.HTTP_200_OK, r.text
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.status == WorkOrderStatus.COMPLETE
    assert wo.actual_hours == pytest.approx(0.0)  # hours NOT rolled flag-OFF

    resp = client.get(
        f"/api/v1/analytics/cost-analysis?work_order_id={wo.id}",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    jobs = resp.json()["jobs"]
    assert len(jobs) == 1, resp.json()
    # The report is consistently $0 labor flag-OFF for a reconcile-completed WO, just like
    # a live-completed WO (the inconsistency this fix closes).
    assert jobs[0]["cost_breakdown"]["labor_cost"] == pytest.approx(0.0)
    assert jobs[0]["cost_breakdown"]["overhead_cost"] == pytest.approx(0.0)


def test_get_cost_analysis_computes_labor_flag_on(client, db_session, monkeypatch):
    """Flag ON: get_cost_analysis computes labor + overhead at the shared WC rate.

    The flag-ON complement of the gate above (a focused lock that the gating is not a
    permanent zero): a flag-ON completed WO surfaces labor = hours x WC rate and overhead
    = hours x overhead rate. Pairs with the existing
    test_get_cost_analysis_uses_same_rate_and_is_not_structurally_zero."""
    _enable_rollup(monkeypatch)
    _set_overhead_rate(monkeypatch, 20.0)
    admin = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_entry(db_session, admin, wo, op, duration_hours=4.0)
    db_session.commit()

    done = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert done.status_code == status.HTTP_200_OK, done.text

    resp = client.get(
        f"/api/v1/analytics/cost-analysis?work_order_id={wo.id}",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    breakdown = resp.json()["jobs"][0]["cost_breakdown"]
    # labor = 4 hr x $100, overhead = 4 hr x $20 (computed only because the flag is ON).
    assert breakdown["labor_cost"] == pytest.approx(400.0)
    assert breakdown["overhead_cost"] == pytest.approx(80.0)
