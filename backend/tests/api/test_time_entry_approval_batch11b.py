"""Behavior locks for the Batch-11B TimeEntry approval workflow + opt-in cost filter (G5-A).

Two contracts:

1. Approve / unapprove endpoints (``POST /shop-floor/time-entries/{id}/approve`` and
   ``/unapprove``):
   - approve by a SUPERVISOR / QUALITY / ADMIN / MANAGER stamps ``approved`` (timestamp) +
     ``approved_by`` and writes a tamper-evident audit row; unapprove clears them.
   - self-approval is forbidden (the user who owns the entry approving it -> 403, even
     with an approver role) -- segregation of duties for the labor-cost gate.
   - RBAC: an OPERATOR calling approve -> 403.
   - tenant isolation: approving a TimeEntry from another company -> 404.

2. Opt-in cost filter (``REQUIRE_APPROVED_LABOR_FOR_COST``, default OFF), asserted at the
   unit level on ``job_costing_service.recompute_from_time_entries`` -- the cleanest
   chokepoint, since it sums per-TimeEntry labor straight into ``JobCost.actual_labor_cost``:
   - flag OFF (default): an UN-approved closed entry IS counted (existing behavior).
   - flag ON (monkeypatched on the settings object): an un-approved entry is EXCLUDED;
     only approved labor feeds actual_labor_cost.

The flag is toggled via the SAME chokepoint the production code resolves it through
(``settings.REQUIRE_APPROVED_LABOR_FOR_COST``, read by
``labor_cost_service.is_approved_labor_required``).
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.job_costing import JobCost, JobCostStatus
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.job_costing_service import recompute_from_time_entries

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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"b11b-g5a-{n}@co{company_id}.test",
        employee_id=f"B11BG5A-{n:05d}",
        first_name="B11B",
        last_name="G5A",
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


def make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"B11BG5A-P-{n}",
        name=f"Part {n}",
        description="batch11b G5A fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, *, hourly_rate: float = 100.0, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"B11BG5A-WC-{n}",
        code=f"B11BG5A-WC-{n}",
        work_center_type="welding",
        description="batch11b G5A fixture work center",
        hourly_rate=hourly_rate,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(db: Session, part: Part, *, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B11BG5A-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    return wo


def make_op(db: Session, wo: WorkOrder, wc: WorkCenter, *, company_id: int = COMPANY_A) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="OP10",
        name="Op 10",
        status=OperationStatus.IN_PROGRESS,
        quantity_complete=0,
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
    duration_hours: float = 2.0,
    approved: bool = False,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=duration_hours),
        clock_out=datetime.utcnow(),
        duration_hours=duration_hours,
        approved=datetime.utcnow() if approved else None,
        company_id=company_id,
    )
    db.add(entry)
    db.flush()
    return entry


# ===========================================================================
# Approve / unapprove endpoint behavior
# ===========================================================================


@pytest.mark.parametrize("approver_role", [UserRole.SUPERVISOR, UserRole.QUALITY, UserRole.ADMIN, UserRole.MANAGER])
def test_approve_stamps_fields_and_audits(client: TestClient, db_session: Session, approver_role: UserRole):
    """A SUPERVISOR / QUALITY / ADMIN / MANAGER approving another user's TimeEntry stamps
    ``approved`` + ``approved_by`` and writes a tamper-evident audit row.

    MANAGER was added to ``_TIME_ENTRY_APPROVAL_ROLES`` for consistency with the documented
    approver set (Batch-11B compliance follow-up)."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    approver = make_user(db_session, role=approver_role)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part)
    op = make_op(db_session, wo, wc)
    entry = make_closed_entry(db_session, operator, wo, op)
    db_session.commit()
    entry_id = entry.id

    resp = client.post(f"/api/v1/shop-floor/time-entries/{entry_id}/approve", headers=headers_for(approver))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["approved"] is not None, "approve must stamp the approved timestamp"
    assert body["approved_by"] == approver.id, "approved_by must be the approver"

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.approved is not None
    assert entry.approved_by == approver.id

    # Tamper-evident audit row for the approval flip.
    audits = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == "TIME_ENTRY_APPROVE",
            AuditLog.resource_type == "time_entry",
            AuditLog.resource_id == entry_id,
        )
        .all()
    )
    assert len(audits) == 1, "approval must write exactly one TIME_ENTRY_APPROVE audit row"


def test_unapprove_clears_fields_and_audits(client: TestClient, db_session: Session):
    """Unapprove clears ``approved`` + ``approved_by`` and writes an audit row."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part)
    op = make_op(db_session, wo, wc)
    entry = make_closed_entry(db_session, operator, wo, op, approved=True)
    entry.approved_by = supervisor.id
    db_session.commit()
    entry_id = entry.id

    resp = client.post(f"/api/v1/shop-floor/time-entries/{entry_id}/unapprove", headers=headers_for(supervisor))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["approved"] is None, "unapprove must clear approved"
    assert body["approved_by"] is None, "unapprove must clear approved_by"

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.approved is None
    assert entry.approved_by is None

    audits = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == "TIME_ENTRY_UNAPPROVE",
            AuditLog.resource_type == "time_entry",
            AuditLog.resource_id == entry_id,
        )
        .all()
    )
    assert len(audits) == 1, "unapprove must write exactly one TIME_ENTRY_UNAPPROVE audit row"


def test_self_approval_is_forbidden(client: TestClient, db_session: Session):
    """The user who OWNS the TimeEntry cannot approve it -> 403, even holding an
    approver role (segregation of duties)."""
    # A supervisor who is ALSO the owner of the entry: an approver role, but self.
    owner_supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part)
    op = make_op(db_session, wo, wc)
    entry = make_closed_entry(db_session, owner_supervisor, wo, op)
    db_session.commit()
    entry_id = entry.id

    resp = client.post(f"/api/v1/shop-floor/time-entries/{entry_id}/approve", headers=headers_for(owner_supervisor))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.approved is None, "self-approval must NOT stamp the entry"


def test_operator_cannot_approve(client: TestClient, db_session: Session):
    """RBAC: an OPERATOR (no approver role) calling approve -> 403."""
    owner = make_user(db_session, role=UserRole.OPERATOR)
    other_operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part)
    op = make_op(db_session, wo, wc)
    entry = make_closed_entry(db_session, owner, wo, op)
    db_session.commit()
    entry_id = entry.id

    resp = client.post(f"/api/v1/shop-floor/time-entries/{entry_id}/approve", headers=headers_for(other_operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_approve_is_tenant_isolated(client: TestClient, db_session: Session):
    """Approving a TimeEntry that belongs to ANOTHER company -> 404 (tenant scope)."""
    # Company-B entry.
    operator_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    part_b = make_part(db_session, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    wo_b = make_wo(db_session, part_b, company_id=COMPANY_B)
    op_b = make_op(db_session, wo_b, wc_b, company_id=COMPANY_B)
    entry_b = make_closed_entry(db_session, operator_b, wo_b, op_b, company_id=COMPANY_B)
    db_session.commit()
    entry_b_id = entry_b.id

    # A company-A supervisor cannot reach company-B's entry.
    supervisor_a = make_user(db_session, role=UserRole.SUPERVISOR, company_id=COMPANY_A)
    resp = client.post(f"/api/v1/shop-floor/time-entries/{entry_b_id}/approve", headers=headers_for(supervisor_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry_b_id).approved is None, "cross-tenant approval must not stamp the entry"


# ===========================================================================
# Opt-in cost filter (REQUIRE_APPROVED_LABOR_FOR_COST), unit level
# ===========================================================================


def _seed_job_cost_with_one_unapproved_entry(db: Session):
    """A WO + JobCost + ONE closed, UN-approved, 2-hour RUN entry at a $100 WC."""
    admin = make_user(db, role=UserRole.ADMIN)
    part = make_part(db)
    wc = make_work_center(db, hourly_rate=100.0)
    wo = make_wo(db, part)
    op = make_op(db, wo, wc)
    entry = make_closed_entry(db, admin, wo, op, duration_hours=2.0, approved=False)
    jc = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db.add(jc)
    db.commit()
    return admin, jc, entry


def test_cost_filter_off_counts_unapproved_labor(client: TestClient, db_session: Session, monkeypatch):
    """Flag OFF (default): an UN-approved closed TimeEntry IS counted in job cost.

    2 hr x $100 WC rate = $200 even though the entry is un-approved (existing behavior --
    the gate is opt-in)."""
    monkeypatch.setattr(settings, "REQUIRE_APPROVED_LABOR_FOR_COST", False)
    admin, jc, _entry = _seed_job_cost_with_one_unapproved_entry(db_session)

    recompute_from_time_entries(db_session, job_cost=jc, company_id=COMPANY_A, user_id=admin.id)
    db_session.flush()
    db_session.expire_all()
    jc = db_session.get(JobCost, jc.id)
    assert jc.actual_labor_cost == pytest.approx(200.0), "flag OFF: un-approved labor still counts"


def test_cost_filter_on_excludes_unapproved_labor(client: TestClient, db_session: Session, monkeypatch):
    """Flag ON: an UN-approved closed entry is EXCLUDED from job cost (zero labor)."""
    monkeypatch.setattr(settings, "REQUIRE_APPROVED_LABOR_FOR_COST", True)
    admin, jc, _entry = _seed_job_cost_with_one_unapproved_entry(db_session)

    recompute_from_time_entries(db_session, job_cost=jc, company_id=COMPANY_A, user_id=admin.id)
    db_session.flush()
    db_session.expire_all()
    jc = db_session.get(JobCost, jc.id)
    assert jc.actual_labor_cost == pytest.approx(0.0), "flag ON: un-approved labor is excluded from cost"


def test_cost_filter_on_counts_only_approved_labor(client: TestClient, db_session: Session, monkeypatch):
    """Flag ON: with one APPROVED and one UN-approved entry, ONLY the approved labor
    feeds actual_labor_cost."""
    monkeypatch.setattr(settings, "REQUIRE_APPROVED_LABOR_FOR_COST", True)
    admin = make_user(db_session, role=UserRole.ADMIN)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wc = make_work_center(db_session, hourly_rate=100.0)
    wo = make_wo(db_session, part)
    op = make_op(db_session, wo, wc)
    # Approved: 3 hr x $100 = $300 (counts). Un-approved: 5 hr (excluded).
    make_closed_entry(db_session, admin, wo, op, duration_hours=3.0, approved=True)
    make_closed_entry(db_session, operator, wo, op, duration_hours=5.0, approved=False)
    jc = JobCost(work_order_id=wo.id, status=JobCostStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.add(jc)
    db_session.commit()

    recompute_from_time_entries(db_session, job_cost=jc, company_id=COMPANY_A, user_id=admin.id)
    db_session.flush()
    db_session.expire_all()
    jc = db_session.get(JobCost, jc.id)
    assert jc.actual_labor_cost == pytest.approx(300.0), "flag ON: only the approved 3-hr entry counts"
