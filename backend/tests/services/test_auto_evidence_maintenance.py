"""Preventive-maintenance auto-evidence verdict (``_query_maintenance``).

The QMS auto-evidence rule for any clause mentioning maintenance / infrastructure used to
count EVERY ``MaintenanceSchedule`` (no ``is_active`` filter) and derive "overdue" only from
maintenance work orders. So a shop holding thirty PM schedules that had never been performed
read "healthy / compliant" -- and still did with every schedule deactivated. A false
"compliant" in front of a registrar is the failure this file exists to prevent.

The rule now judges the active PM schedules first, in precedence order:

1. no active schedules and no maintenance WOs           -> no_data  / not_assessed
2. any active schedule or open WO past due (< today)    -> critical / non_compliant
3. no active schedules but some maintenance WOs         -> warning  / partial
4. every active schedule unperformed                    -> warning  / not_assessed
5. some unperformed, or some active schedule undated    -> warning  / partial
6. otherwise                                             -> healthy  / compliant

"Performed" = ``last_completed_date`` set, or a COMPLETED maintenance WO of the SAME company
whose ``schedule_id`` is that schedule. Every direct call passes an explicit ``today`` so no
assertion depends on the wall clock; the one test of the default seam patches
``company_today`` instead.

Each test gets fresh tables (``db_session`` drops them at teardown), so the exact counts
asserted for companies 1 and 2 are not polluted by other tests.
"""

from datetime import date, datetime, timedelta
from typing import Optional

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.maintenance import (
    MaintenanceFrequency,
    MaintenanceSchedule,
    MaintenanceStatus,
    MaintenanceType,
    MaintenanceWorkOrder,
)
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.work_center import WorkCenter
from app.services import auto_evidence_service
from app.services.auto_evidence_service import (
    _query_maintenance,
    compute_overall_compliance,
    discover_evidence_for_clause,
)
from app.services.company_time import company_today

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

# Fixed "today" for every direct call: the verdict must never depend on the real clock.
TODAY = date(2026, 3, 10)

# Module-level counter so every row gets a globally unique natural key (wo_number is
# globally unique, work-center codes are unique per company) across tests in a worker.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=True,
        )
        db.add(company)
        db.commit()
    return company


def _make_work_center(db: Session, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        company_id=company_id,
        code=f"PMWC-{n:05d}",
        name=f"Test Press {n}",
        work_center_type="press_brake",
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _make_schedule(
    db: Session,
    work_center: WorkCenter,
    *,
    next_due: Optional[date],
    last_completed: Optional[date] = None,
    is_active: bool = True,
) -> MaintenanceSchedule:
    n = _next()
    schedule = MaintenanceSchedule(
        company_id=work_center.company_id,
        work_center_id=work_center.id,
        name=f"Lube and inspect {n}",
        maintenance_type=MaintenanceType.PREVENTIVE,
        frequency=MaintenanceFrequency.MONTHLY,
        next_due_date=next_due,
        last_completed_date=last_completed,
        is_active=is_active,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def _make_work_order(
    db: Session,
    work_center: WorkCenter,
    *,
    status: MaintenanceStatus,
    due_date: Optional[date] = None,
    schedule_id: Optional[int] = None,
    company_id: Optional[int] = None,
) -> MaintenanceWorkOrder:
    n = _next()
    wo = MaintenanceWorkOrder(
        company_id=company_id if company_id is not None else work_center.company_id,
        work_center_id=work_center.id,
        schedule_id=schedule_id,
        wo_number=f"MWO-EVD-{n:06d}",
        maintenance_type=MaintenanceType.PREVENTIVE,
        status=status,
        title=f"PM task {n}",
        scheduled_date=due_date,
        due_date=due_date,
        completed_at=datetime(2026, 2, 1, 15, 0) if status == MaintenanceStatus.COMPLETED else None,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _make_clause(db: Session, *, title: str, description: str = "", company_id: int = COMPANY_A) -> QMSClause:
    _ensure_company(db, company_id)
    n = _next()
    standard = QMSStandard(name=f"STD-PM-{n}", version="2016", is_active=True, company_id=company_id)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        standard_id=standard.id,
        company_id=company_id,
        clause_number=f"7.1.{n}",
        title=title,
        description=description,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _verdict(result: dict) -> tuple:
    return result["health_status"], result["suggested_compliance"]


# ---------------------------------------------------------------------------
# 1. No data
# ---------------------------------------------------------------------------


def test_no_schedules_and_no_work_orders_is_no_data(db_session: Session):
    _ensure_company(db_session, COMPANY_A)

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("no_data", "not_assessed")
    assert result["recent_count"] == 0
    assert result["total_count"] == 0
    assert result["health_detail"] == "No active PM schedules or maintenance records found"
    assert result["examples"] == []
    # Response shape is unchanged.
    assert result["evidence_type"] == "module"
    assert result["title"] == "Preventive Maintenance & Infrastructure"
    assert result["module_reference"] == "/maintenance"


# ---------------------------------------------------------------------------
# 2. The production scenario: schedules exist, none ever performed
# ---------------------------------------------------------------------------


def test_never_performed_schedules_due_in_future_are_not_compliant(db_session: Session):
    """Three PM schedules first due in two weeks, no work orders, nothing completed: the old
    rule called this healthy / compliant. Nothing has been performed, so it is not assessed."""
    wc = _make_work_center(db_session)
    for _ in range(3):
        _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=15))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert result["suggested_compliance"] != "compliant"
    assert result["health_status"] != "healthy"
    assert _verdict(result) == ("warning", "not_assessed")
    assert result["recent_count"] == 3
    assert result["total_count"] == 0
    assert "No PM performed yet" in result["health_detail"]
    assert "3 active PM schedules" in result["health_detail"]
    assert "3 never performed" in result["description"]


def test_single_never_performed_schedule_uses_singular_wording(db_session: Session):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=30))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("warning", "not_assessed")
    assert "1 active PM schedule," in result["health_detail"]


def test_open_linked_work_order_is_not_performance_evidence(db_session: Session):
    """Only a COMPLETED work order evidences a PM. A scheduled or in-progress one linked to
    a schedule must not turn a never-performed schedule into a performed one.

    One OPEN work order per schedule: a PM schedule takes one at a time, and since migration 132
    the database refuses a second (``uq_maintenance_work_orders_open_schedule``). So the scheduled
    one and the in-progress one sit on two schedules; the first also carries a cancelled sibling,
    which is no evidence either."""
    wc = _make_work_center(db_session)
    scheduled_pm = _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=15))
    in_progress_pm = _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=15))
    _make_work_order(
        db_session,
        wc,
        status=MaintenanceStatus.SCHEDULED,
        due_date=TODAY + timedelta(days=15),
        schedule_id=scheduled_pm.id,
    )
    _make_work_order(db_session, wc, status=MaintenanceStatus.CANCELLED, schedule_id=scheduled_pm.id)
    _make_work_order(
        db_session,
        wc,
        status=MaintenanceStatus.IN_PROGRESS,
        due_date=TODAY + timedelta(days=15),
        schedule_id=in_progress_pm.id,
    )

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    # Both schedules still read never performed: neither open work order counts.
    assert _verdict(result) == ("warning", "not_assessed")
    assert result["recent_count"] == 2
    assert result["total_count"] == 3
    assert "2 never performed" in result["description"]


# ---------------------------------------------------------------------------
# 3. Overdue schedules
# ---------------------------------------------------------------------------


def test_schedule_past_due_is_non_compliant(db_session: Session):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY - timedelta(days=1), last_completed=TODAY - timedelta(days=31))
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=10), last_completed=TODAY - timedelta(days=20))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("critical", "non_compliant")
    assert result["recent_count"] == 2
    assert result["health_detail"] == "1 active PM schedule past due"
    assert "(1 past due, 0 never performed)" in result["description"]


def test_overdue_outranks_never_performed(db_session: Session):
    """A never-performed schedule that is already past due is critical, not 'not assessed'."""
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY - timedelta(days=5))
    _make_schedule(db_session, wc, next_due=TODAY - timedelta(days=2))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("critical", "non_compliant")
    assert result["health_detail"] == "2 active PM schedules past due"


def test_schedule_due_today_is_not_overdue(db_session: Session):
    """Boundary: next_due_date == today is due, not past due."""
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY, last_completed=TODAY - timedelta(days=30))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("healthy", "compliant")
    assert "past due" not in result["health_detail"]

    # One day later the same schedule IS past due -- the boundary is exactly `< today`.
    later = _query_maintenance(db_session, COMPANY_A, today=TODAY + timedelta(days=1))
    assert _verdict(later) == ("critical", "non_compliant")


# ---------------------------------------------------------------------------
# 4. Compliant
# ---------------------------------------------------------------------------


def test_all_current_and_performed_is_compliant(db_session: Session):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20), last_completed=TODAY - timedelta(days=10))
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=80), last_completed=TODAY - timedelta(days=10))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("healthy", "compliant")
    assert result["recent_count"] == 2
    assert result["total_count"] == 0
    assert result["health_detail"].startswith("All 2 active PM schedules current and performed")


def test_completed_linked_work_order_counts_as_performance(db_session: Session):
    """A schedule with no last_completed_date is still performed when a COMPLETED work order
    of the same company is linked to it."""
    wc = _make_work_center(db_session)
    schedule = _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=25))
    _make_work_order(
        db_session, wc, status=MaintenanceStatus.COMPLETED, due_date=TODAY - timedelta(days=5), schedule_id=schedule.id
    )

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("healthy", "compliant")
    assert result["recent_count"] == 1
    assert result["total_count"] == 1
    # A completed WO past its due date is not "overdue" -- only SCHEDULED/OVERDUE ones are.
    assert "(1 completed, 0 overdue)" in result["description"]
    assert result["health_detail"].endswith("1/1 maintenance work orders completed")
    assert [e["record_type"] for e in result["examples"]] == ["maintenance_wo"]


# ---------------------------------------------------------------------------
# 5. Inactive schedules are ignored
# ---------------------------------------------------------------------------


def test_inactive_schedules_are_ignored(db_session: Session):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20), last_completed=TODAY - timedelta(days=10))
    # Each of these would drag an active schedule below compliant; all are retired.
    _make_schedule(db_session, wc, next_due=TODAY - timedelta(days=60), is_active=False)  # overdue, never performed
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=5), is_active=False)  # never performed
    _make_schedule(db_session, wc, next_due=None, is_active=False)  # undated

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("healthy", "compliant")
    assert result["recent_count"] == 1
    assert "(0 past due, 0 never performed)" in result["description"]


def test_all_schedules_inactive_and_no_work_orders_is_no_data(db_session: Session):
    """Deactivating every schedule used to leave the verdict unchanged. With no active plan
    and no work orders there is nothing to assess."""
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY - timedelta(days=30), is_active=False)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=30), is_active=False)
    _make_schedule(
        db_session, wc, next_due=TODAY + timedelta(days=30), last_completed=TODAY - timedelta(days=1), is_active=False
    )

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("no_data", "not_assessed")
    assert result["recent_count"] == 0
    assert result["total_count"] == 0


# ---------------------------------------------------------------------------
# 6. Partial
# ---------------------------------------------------------------------------


def test_some_performed_some_never_performed_is_partial(db_session: Session):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20), last_completed=TODAY - timedelta(days=10))
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("warning", "partial")
    assert result["recent_count"] == 2
    assert result["health_detail"] == "1 of 2 active PM schedules never performed"


def test_active_schedule_without_next_due_date_is_partial(db_session: Session):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20), last_completed=TODAY - timedelta(days=10))
    _make_schedule(db_session, wc, next_due=None, last_completed=TODAY - timedelta(days=10))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("warning", "partial")
    assert result["health_detail"] == "1 active PM schedule has no next due date"


def test_unperformed_and_undated_gaps_are_both_reported(db_session: Session):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20), last_completed=TODAY - timedelta(days=10))
    _make_schedule(db_session, wc, next_due=None, last_completed=TODAY - timedelta(days=10))
    _make_schedule(db_session, wc, next_due=None, last_completed=TODAY - timedelta(days=10))
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("warning", "partial")
    assert result["health_detail"] == (
        "1 of 4 active PM schedules never performed; 2 active PM schedules have no next due date"
    )


def test_work_orders_without_any_active_schedule_is_partial(db_session: Session):
    """Maintenance has been done but there is no active preventive plan."""
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20), is_active=False)
    _make_work_order(db_session, wc, status=MaintenanceStatus.COMPLETED, due_date=TODAY - timedelta(days=3))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("warning", "partial")
    assert result["recent_count"] == 0
    assert result["total_count"] == 1
    assert result["health_detail"].startswith("No active PM schedules: 1 maintenance work order on record")


# ---------------------------------------------------------------------------
# 7. Overdue maintenance work orders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE])
def test_open_work_order_past_due_is_non_compliant(db_session: Session, status: MaintenanceStatus):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20), last_completed=TODAY - timedelta(days=10))
    _make_work_order(db_session, wc, status=status, due_date=TODAY - timedelta(days=1))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("critical", "non_compliant")
    assert result["health_detail"] == "1 maintenance work order overdue"
    assert result["total_count"] == 1


def test_overdue_work_order_with_no_schedules_is_non_compliant(db_session: Session):
    """Overdue outranks 'no active PM schedules' (rule 2 before rule 3)."""
    wc = _make_work_center(db_session)
    _make_work_order(db_session, wc, status=MaintenanceStatus.SCHEDULED, due_date=TODAY - timedelta(days=4))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("critical", "non_compliant")
    assert result["recent_count"] == 0


def test_overdue_schedule_and_work_order_are_both_reported(db_session: Session):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY - timedelta(days=2), last_completed=TODAY - timedelta(days=40))
    _make_work_order(db_session, wc, status=MaintenanceStatus.SCHEDULED, due_date=TODAY - timedelta(days=2))
    _make_work_order(db_session, wc, status=MaintenanceStatus.OVERDUE, due_date=TODAY - timedelta(days=9))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("critical", "non_compliant")
    assert result["health_detail"] == "1 active PM schedule past due; 2 maintenance work orders overdue"


@pytest.mark.parametrize(
    "status, due_offset_days",
    [
        (MaintenanceStatus.SCHEDULED, 0),  # due today: not yet overdue
        (MaintenanceStatus.COMPLETED, -5),  # done, even though late
        (MaintenanceStatus.CANCELLED, -5),
        (MaintenanceStatus.IN_PROGRESS, -5),  # same status set as GET /maintenance/dashboard
    ],
)
def test_work_order_not_counted_as_overdue(db_session: Session, status: MaintenanceStatus, due_offset_days: int):
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY + timedelta(days=20), last_completed=TODAY - timedelta(days=10))
    _make_work_order(db_session, wc, status=status, due_date=TODAY + timedelta(days=due_offset_days))

    result = _query_maintenance(db_session, COMPANY_A, today=TODAY)

    assert _verdict(result) == ("healthy", "compliant")
    assert "0 overdue)" in result["description"]


# ---------------------------------------------------------------------------
# 8. Tenant scoping
# ---------------------------------------------------------------------------


def test_other_company_completed_work_order_is_not_performance_evidence(db_session: Session):
    """A COMPLETED work order owned by company B whose schedule_id points at company A's
    schedule (a pre-fix cross-tenant row) must not count as A having performed its PM."""
    wc_a = _make_work_center(db_session, COMPANY_A)
    wc_b = _make_work_center(db_session, COMPANY_B)
    schedule_a = _make_schedule(db_session, wc_a, next_due=TODAY + timedelta(days=15))
    _make_work_order(
        db_session,
        wc_b,
        status=MaintenanceStatus.COMPLETED,
        due_date=TODAY - timedelta(days=3),
        schedule_id=schedule_a.id,
    )

    result_a = _query_maintenance(db_session, COMPANY_A, today=TODAY)
    assert _verdict(result_a) == ("warning", "not_assessed")
    assert result_a["recent_count"] == 1
    assert result_a["total_count"] == 0  # B's work order is not A's
    assert result_a["examples"] == []

    # B holds the work order but no active schedule of its own.
    result_b = _query_maintenance(db_session, COMPANY_B, today=TODAY)
    assert _verdict(result_b) == ("warning", "partial")
    assert result_b["recent_count"] == 0
    assert result_b["total_count"] == 1

    # Positive control: the same link from a company-A work order DOES count, so the
    # assertion above is about tenancy, not about linked work orders being ignored.
    _make_work_order(
        db_session,
        wc_a,
        status=MaintenanceStatus.COMPLETED,
        due_date=TODAY - timedelta(days=3),
        schedule_id=schedule_a.id,
    )
    assert _verdict(_query_maintenance(db_session, COMPANY_A, today=TODAY)) == ("healthy", "compliant")


def test_other_company_schedules_and_work_orders_do_not_affect_counts(db_session: Session):
    wc_a = _make_work_center(db_session, COMPANY_A)
    wc_b = _make_work_center(db_session, COMPANY_B)
    _make_schedule(db_session, wc_a, next_due=TODAY + timedelta(days=20), last_completed=TODAY - timedelta(days=10))
    _make_work_order(db_session, wc_a, status=MaintenanceStatus.COMPLETED, due_date=TODAY - timedelta(days=10))

    # Company B: overdue, never-performed and undated schedules plus an overdue work order --
    # each would sink A's verdict if it leaked.
    _make_schedule(db_session, wc_b, next_due=TODAY - timedelta(days=7))
    _make_schedule(db_session, wc_b, next_due=TODAY + timedelta(days=7))
    _make_schedule(db_session, wc_b, next_due=None)
    _make_work_order(db_session, wc_b, status=MaintenanceStatus.SCHEDULED, due_date=TODAY - timedelta(days=7))
    _make_work_order(db_session, wc_b, status=MaintenanceStatus.COMPLETED, due_date=TODAY - timedelta(days=7))

    result_a = _query_maintenance(db_session, COMPANY_A, today=TODAY)
    assert _verdict(result_a) == ("healthy", "compliant")
    assert result_a["recent_count"] == 1
    assert result_a["total_count"] == 1
    assert len(result_a["examples"]) == 1

    result_b = _query_maintenance(db_session, COMPANY_B, today=TODAY)
    assert _verdict(result_b) == ("critical", "non_compliant")
    assert result_b["recent_count"] == 3
    assert result_b["total_count"] == 2


# ---------------------------------------------------------------------------
# 9. ``today`` defaults to the company's local date
# ---------------------------------------------------------------------------


def test_today_defaults_to_company_local_date(db_session: Session, monkeypatch):
    """Without an explicit ``today`` the rule reads ``company_today(db, company_id)``."""
    wc = _make_work_center(db_session)
    _make_schedule(db_session, wc, next_due=TODAY, last_completed=TODAY - timedelta(days=30))

    calls = []

    def fake_company_today(db, company_id):
        calls.append(company_id)
        return TODAY + timedelta(days=1)

    monkeypatch.setattr(auto_evidence_service, "company_today", fake_company_today)

    result = _query_maintenance(db_session, COMPANY_A)

    assert calls == [COMPANY_A]
    assert _verdict(result) == ("critical", "non_compliant")


# ---------------------------------------------------------------------------
# 10. Through the public discovery entry points
# ---------------------------------------------------------------------------


def test_infrastructure_clause_with_never_performed_schedules_is_not_compliant(db_session: Session):
    """``discover_evidence_for_clause`` routes an 'Infrastructure' clause to the maintenance
    rule (no injected ``today`` -- the real company date), and the overall verdict for the
    never-performed scenario is not 'compliant'."""
    real_today = company_today(db_session, COMPANY_A)
    wc = _make_work_center(db_session)
    for _ in range(3):
        _make_schedule(db_session, wc, next_due=real_today + timedelta(days=15))
    clause = _make_clause(db_session, title="Infrastructure")

    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)

    assert [r["_rule_id"] for r in results] == ["maintenance"]
    maintenance = results[0]
    assert _verdict(maintenance) == ("warning", "not_assessed")
    assert maintenance["recent_count"] == 3

    overall = compute_overall_compliance(results)
    assert overall != "compliant"
    assert overall == "not_assessed"


def test_infrastructure_clause_overall_compliant_once_performed(db_session: Session):
    """Control for the test above: the same clause reads compliant once every active
    schedule has a completion on record and is current."""
    real_today = company_today(db_session, COMPANY_A)
    wc = _make_work_center(db_session)
    for _ in range(3):
        _make_schedule(
            db_session, wc, next_due=real_today + timedelta(days=15), last_completed=real_today - timedelta(days=15)
        )
    clause = _make_clause(db_session, title="Infrastructure")

    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)

    assert [r["_rule_id"] for r in results] == ["maintenance"]
    assert compute_overall_compliance(results) == "compliant"
