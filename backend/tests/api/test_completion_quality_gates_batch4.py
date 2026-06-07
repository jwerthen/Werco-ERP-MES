"""Behavior locks for the Batch-4 quality-gate warn-and-record path (Rank 7).

POSTURE: warn + record, do NOT block. When an operation / work order completes
while a quality gate (inspection / NCR / FAI / open blocker) is unsatisfied,
completion STILL SUCCEEDS, but the system writes a tamper-evident audit row
(action ``COMPLETED_WITH_QUALITY_EXCEPTION``), emits a warning OperationalEvent,
and surfaces the exceptions on the API response.

Covered findings:
- QG-1: requires_inspection + not inspection_complete -> inspection_incomplete warning.
- QG-2: the missing inspection_complete WRITER endpoint clears the gate (audited).
- QG-3: open NCR / non-passed FAI -> open_ncr / fai_not_passed warnings, tenant-scoped.
- QG-4: reconcile-on-read completion records inspection_incomplete (partial coverage).
- BLK-2: open WorkOrderBlocker -> open_blocker warning on completion.
- BLK-4: resume surfaces still-open blockers.
- Warn-not-block: completion succeeds (200) in every case.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.quality import (
    FAIStatus,
    FirstArticleInspection,
    NCRDisposition,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    n = _next()
    user = User(
        email=f"b4-{n}@co{company_id}.test",
        employee_id=f"B4-{n:05d}",
        first_name="B4",
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


def make_part(db: Session, company_id: int = COMPANY_A) -> Part:
    n = _next()
    part = Part(
        part_number=f"B4-P-{n}",
        name=f"Part {n}",
        description="batch4 fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, company_id: int = COMPANY_A) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        name=f"B4-WC-{n}",
        code=f"B4-WC-{n}",
        work_center_type="welding",
        description="batch4 fixture work center",
        hourly_rate=100,
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
        work_order_number=f"B4-WO-{n:05d}",
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
    requires_inspection: bool = False,
    inspection_complete: bool = False,
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
        requires_inspection=requires_inspection,
        inspection_complete=inspection_complete,
        company_id=company_id,
    )
    db.add(op)
    db.flush()
    return op


def make_ncr(db: Session, wo: WorkOrder, *, status_: NCRStatus, disposition: NCRDisposition) -> NonConformanceReport:
    n = _next()
    ncr = NonConformanceReport(
        ncr_number=f"B4-NCR-{n}",
        work_order_id=wo.id,
        source=NCRSource.IN_PROCESS,
        status=status_,
        disposition=disposition,
        title="Test NCR",
        description="batch4 fixture NCR",
        company_id=wo.company_id,
    )
    db.add(ncr)
    db.flush()
    return ncr


def make_fai(db: Session, wo: WorkOrder, part: Part, *, status_: FAIStatus) -> FirstArticleInspection:
    n = _next()
    fai = FirstArticleInspection(
        fai_number=f"B4-FAI-{n}",
        part_id=part.id,
        work_order_id=wo.id,
        status=status_,
        company_id=wo.company_id,
    )
    db.add(fai)
    db.flush()
    return fai


def make_blocker(db: Session, wo: WorkOrder, op: WorkOrderOperation, *, status_: str) -> WorkOrderBlocker:
    blocker = WorkOrderBlocker(
        company_id=wo.company_id,
        work_order_id=wo.id,
        operation_id=op.id,
        category="quality_hold",
        severity="high",
        status=status_,
        title="Quality hold",
    )
    db.add(blocker)
    db.flush()
    return blocker


def make_time_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    """An OPEN run time entry (clocked in, not yet clocked out) for the clock-out path."""
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        quantity_produced=0,
        quantity_scrapped=0,
        company_id=company_id,
    )
    db.add(entry)
    db.flush()
    return entry


def _quality_exception_audit(db: Session, company_id: int = COMPANY_A) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "COMPLETED_WITH_QUALITY_EXCEPTION", AuditLog.company_id == company_id)
        .all()
    )


def _audit_codes(audit_rows: list[AuditLog]) -> set[str]:
    """Every quality-exception code recorded across the given audit rows (new_values)."""
    codes: set[str] = set()
    for row in audit_rows:
        codes.update((row.new_values or {}).get("quality_exceptions", []))
    return codes


def _audit_reference_ids(audit_rows: list[AuditLog], code: str) -> set[int]:
    """reference_id values stamped in extra_data for a given exception code."""
    refs: set[int] = set()
    for row in audit_rows:
        for exc in (row.extra_data or {}).get("quality_exceptions", []):
            if exc.get("code") == code and exc.get("reference_id") is not None:
                refs.add(exc["reference_id"])
    return refs


def _warning_events(db: Session, wo: WorkOrder) -> list[OperationalEvent]:
    return (
        db.query(OperationalEvent)
        .filter(
            OperationalEvent.event_type == "quality_exception_on_completion",
            OperationalEvent.work_order_id == wo.id,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# QG-1: inspection gate warns but does NOT block
# ---------------------------------------------------------------------------


def test_shop_floor_complete_with_inspection_incomplete_warns_not_blocks(client: TestClient, db_session: Session):
    user = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        requires_inspection=True,
        inspection_complete=False,
    )
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        json={"quantity_complete": 5},
        headers=headers_for(user),
    )
    # Warn-not-block: completion succeeds.
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["is_fully_complete"] is True
    codes = [e["code"] for e in body["quality_exceptions"]]
    assert "inspection_incomplete" in codes

    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.status == OperationStatus.COMPLETE  # still completed

    # Tamper-evident record written.
    audit = _quality_exception_audit(db_session)
    assert len(audit) >= 1
    assert any("inspection_incomplete" in (a.new_values or {}).get("quality_exceptions", []) for a in audit)
    # Warning OperationalEvent emitted.
    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.event_type == "quality_exception_on_completion",
            OperationalEvent.work_order_id == wo.id,
        )
        .all()
    )
    assert events and events[0].severity == "warning"


def test_office_complete_with_inspection_incomplete_warns_not_blocks(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        requires_inspection=True,
    )
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "inspection_incomplete" in codes


def test_no_quality_exceptions_when_gates_clear(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        requires_inspection=True,
        inspection_complete=True,  # gate satisfied
    )
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["quality_exceptions"] == []
    assert _quality_exception_audit(db_session) == []


# ---------------------------------------------------------------------------
# QG-2: the inspection writer endpoint clears the gate (audited, RBAC-gated)
# ---------------------------------------------------------------------------


def test_mark_inspected_writer_sets_flag_and_audits(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, requires_inspection=True)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/inspection",
        json={"inspection_type": "final", "notes": "all dims in tolerance"},
        headers=headers_for(quality),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["inspection_complete"] is True

    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.inspection_complete is True
    assert refreshed.inspection_type == "final"

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "MARK_OPERATION_INSPECTED", AuditLog.resource_id == op.id)
        .all()
    )
    assert len(audit) == 1


def test_mark_inspected_clears_the_completion_gate(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, requires_inspection=True)
    db_session.commit()

    # Inspect first, then complete -> no inspection_incomplete exception.
    insp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/inspection",
        json={},
        headers=headers_for(quality),
    )
    assert insp.status_code == status.HTTP_200_OK, insp.text

    comp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(quality),
    )
    assert comp.status_code == status.HTTP_200_OK, comp.text
    codes = [e["code"] for e in comp.json()["quality_exceptions"]]
    assert "inspection_incomplete" not in codes


def test_mark_inspected_forbidden_for_operator(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, requires_inspection=True)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/inspection",
        json={},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_mark_inspected_tenant_scoped_404(client: TestClient, db_session: Session):
    quality_b = make_user(db_session, role=UserRole.QUALITY, company_id=COMPANY_B)
    part = make_part(db_session)  # company A
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, requires_inspection=True)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/inspection",
        json={},
        headers=headers_for(quality_b),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


# ---------------------------------------------------------------------------
# QG-3: open NCR / non-passed FAI warn on completion (tenant-scoped)
# ---------------------------------------------------------------------------


def test_open_ncr_warns_on_completion(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_ncr(db_session, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "open_ncr" in codes


def test_closed_ncr_does_not_warn(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_ncr(db_session, wo, status_=NCRStatus.CLOSED, disposition=NCRDisposition.USE_AS_IS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "open_ncr" not in codes


def test_non_passed_fai_warns_passed_fai_clears(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_fai(db_session, wo, part, status_=FAIStatus.FAILED)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "fai_not_passed" in [e["code"] for e in resp.json()["quality_exceptions"]]

    # Flip the SAME FAI to PASSED on a fresh WO/op -> no warning.
    wo2 = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op2 = make_op(db_session, wo2, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_fai(db_session, wo2, part, status_=FAIStatus.PASSED)
    db_session.commit()
    resp2 = client.post(
        f"/api/v1/work-orders/operations/{op2.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp2.status_code == status.HTTP_200_OK, resp2.text
    assert "fai_not_passed" not in [e["code"] for e in resp2.json()["quality_exceptions"]]


def test_other_company_ncr_does_not_warn(client: TestClient, db_session: Session):
    """Tenant isolation: an NCR on company B must not surface on company A's WO."""
    admin_a = make_user(db_session)
    part_a = make_part(db_session)
    wo_a = make_wo(db_session, part_a, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc_a = make_work_center(db_session)
    op_a = make_op(db_session, wo_a, wc_a, sequence=10, status_=OperationStatus.IN_PROGRESS)

    # A company-B NCR that (pathologically) points at company A's WO id must be
    # excluded by the company_id filter in the evaluator.
    n = _next()
    db_session.add(
        NonConformanceReport(
            ncr_number=f"B4-NCR-X-{n}",
            work_order_id=wo_a.id,
            source=NCRSource.IN_PROCESS,
            status=NCRStatus.OPEN,
            disposition=NCRDisposition.PENDING,
            title="cross-tenant NCR",
            description="should be excluded",
            company_id=COMPANY_B,
        )
    )
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_a.id}/complete?quantity_complete=5",
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "open_ncr" not in [e["code"] for e in resp.json()["quality_exceptions"]]


# ---------------------------------------------------------------------------
# BLK-2: open blocker warns on completion
# ---------------------------------------------------------------------------


def test_open_blocker_warns_on_completion(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_blocker(db_session, wo, op, status_=WorkOrderBlockerStatus.OPEN.value)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "open_blocker" in [e["code"] for e in resp.json()["quality_exceptions"]]


# ---------------------------------------------------------------------------
# complete_work_order: warn-and-record at the WO grain (NCR + per-op inspection)
# ---------------------------------------------------------------------------


def test_complete_work_order_records_quality_exceptions(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        requires_inspection=True,
    )
    make_ncr(db_session, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "inspection_incomplete" in codes
    assert "open_ncr" in codes

    db_session.expire_all()
    refreshed = db_session.get(WorkOrder, wo.id)
    assert refreshed.status == WorkOrderStatus.COMPLETE  # warn-not-block


# ---------------------------------------------------------------------------
# QG-4: reconcile-on-read completion records inspection_incomplete (partial)
# ---------------------------------------------------------------------------


def test_reconcile_on_read_records_inspection_exception(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        quantity_complete=5,
        requires_inspection=True,
    )
    # Durable closed evidence so reconcile flips the op (and WO) to COMPLETE on a GET.
    db_session.add(
        TimeEntry(
            user_id=admin.id,
            work_order_id=wo.id,
            operation_id=op.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow() - timedelta(hours=2),
            clock_out=datetime.utcnow() - timedelta(hours=1),
            duration_hours=1.0,
            quantity_produced=5,
            quantity_scrapped=0,
            company_id=COMPANY_A,
        )
    )
    db_session.commit()

    resp = client.get(f"/api/v1/shop-floor/operations/{op.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.status == OperationStatus.COMPLETE  # reconcile completed it on the read

    audit = _quality_exception_audit(db_session)
    assert any(
        (a.extra_data or {}).get("source") == "reconcile_on_read" for a in audit
    ), "reconcile-driven completion must record inspection_incomplete"


# ---------------------------------------------------------------------------
# BLK-4: resume surfaces still-open blockers
# ---------------------------------------------------------------------------


def test_resume_surfaces_open_blockers(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.ON_HOLD)
    blocker = make_blocker(db_session, wo, op, status_=WorkOrderBlockerStatus.OPEN.value)
    db_session.commit()

    resp = client.put(f"/api/v1/shop-floor/operations/{op.id}/resume", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    open_blockers = resp.json()["open_blockers"]
    assert [b["id"] for b in open_blockers] == [blocker.id]


# ===========================================================================
# MATRIX GAP-FILL: complete the gate x path coverage required by Batch 4.
#
# For each gate code the matrix demands four assertions on a single completion:
#   (i)   completion returns 200 and the op/WO reaches COMPLETE (NOT blocked),
#   (ii)  the ``quality_exceptions`` response field carries the code,
#   (iii) a committed ``audit_log`` row (action COMPLETED_WITH_QUALITY_EXCEPTION)
#         exists carrying the code + the offending record's reference, and
#   (iv)  a warning ``OperationalEvent`` exists.
# The tests above already lock several individual facets; the tests below close
# the remaining (gate x path) cells and tighten the audit/reference/event proof.
# ===========================================================================


# ---------------------------------------------------------------------------
# QG-1 inspection_incomplete -- third completion path: clock_out
# (shop_floor complete_operation + office complete_operation are covered above)
# ---------------------------------------------------------------------------


def test_clock_out_completion_with_inspection_incomplete_warns_not_blocks(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        requires_inspection=True,
        inspection_complete=False,
    )
    entry = make_time_entry(db_session, operator, wo, op)
    db_session.commit()

    # Clock out producing the full ordered quantity -> the op (and WO) complete.
    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        json={"quantity_produced": 5, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    # (i) warn-not-block: clock-out succeeds.
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # (ii) the code rides the TimeEntryResponse.quality_exceptions field.
    codes = [e["code"] for e in body["quality_exceptions"]]
    assert "inspection_incomplete" in codes

    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.status == OperationStatus.COMPLETE  # (i) still completed via clock-out

    # (iii) tamper-evident audit row carries the code + the operation reference,
    # tagged with the clock_out source.
    audit = _quality_exception_audit(db_session)
    assert "inspection_incomplete" in _audit_codes(audit)
    assert op.id in _audit_reference_ids(audit, "inspection_incomplete")
    assert any((a.extra_data or {}).get("source") == "clock_out" for a in audit)

    # (iv) warning OperationalEvent emitted.
    events = _warning_events(db_session, wo)
    assert events and all(e.severity == "warning" for e in events)


# ---------------------------------------------------------------------------
# QG-3 open_ncr -- full matrix on a WO/op completion (audit reference + event)
# ---------------------------------------------------------------------------


def test_open_ncr_full_matrix_on_completion(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    ncr = make_ncr(db_session, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    db_session.commit()
    ncr_id = ncr.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    # (i) + (ii)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "open_ncr" in [e["code"] for e in resp.json()["quality_exceptions"]]

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE

    # (iii) audit row carries the code AND points at the offending NCR.
    audit = _quality_exception_audit(db_session)
    assert "open_ncr" in _audit_codes(audit)
    assert ncr_id in _audit_reference_ids(audit, "open_ncr")

    # (iv) warning event.
    assert _warning_events(db_session, wo)


def test_open_ncr_warns_via_work_order_level_completion(client: TestClient, db_session: Session):
    """open_ncr surfaces on the WO-grain force-completion path too."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    ncr = make_ncr(db_session, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    db_session.commit()
    ncr_id = ncr.id

    resp = client.post(f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=5", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "open_ncr" in [e["code"] for e in resp.json()["quality_exceptions"]]

    db_session.expire_all()
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE  # warn-not-block

    audit = _quality_exception_audit(db_session)
    assert ncr_id in _audit_reference_ids(audit, "open_ncr")
    assert _warning_events(db_session, wo)


# ---------------------------------------------------------------------------
# QG-3 fai_not_passed -- full matrix (audit reference + event).
# NOTE (documented limitation): a missing-but-required FAI is NOT detectable --
# the FAI model has no "required" flag and no operation_id, so the gate fires
# ONLY when an FAI row EXISTS and is not PASSED. This test exercises the
# detectable case; the missing case is intentionally not covered.
# ---------------------------------------------------------------------------


def test_fai_not_passed_full_matrix_on_completion(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    fai = make_fai(db_session, wo, part, status_=FAIStatus.IN_PROGRESS)  # exists, not PASSED
    db_session.commit()
    fai_id = fai.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "fai_not_passed" in [e["code"] for e in resp.json()["quality_exceptions"]]

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE

    audit = _quality_exception_audit(db_session)
    assert "fai_not_passed" in _audit_codes(audit)
    assert fai_id in _audit_reference_ids(audit, "fai_not_passed")
    assert _warning_events(db_session, wo)


def test_missing_required_fai_is_not_detectable(client: TestClient, db_session: Session):
    """Documented limitation: with NO FAI row at all, the gate cannot fire.

    The data model carries no 'FAI required' marker, so an absent-but-required
    FAI produces no fai_not_passed exception. This locks that known partial
    coverage so a future schema change that adds the marker forces a revisit.
    """
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()  # no FAI created

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "fai_not_passed" not in [e["code"] for e in resp.json()["quality_exceptions"]]


# ---------------------------------------------------------------------------
# BLK-2 open_blocker -- full matrix (audit reference + event) + op-level scope
# ---------------------------------------------------------------------------


def test_open_blocker_full_matrix_on_completion(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    blocker = make_blocker(db_session, wo, op, status_=WorkOrderBlockerStatus.OPEN.value)
    db_session.commit()
    blocker_id = blocker.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "open_blocker" in [e["code"] for e in resp.json()["quality_exceptions"]]

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE  # not blocked

    audit = _quality_exception_audit(db_session)
    assert "open_blocker" in _audit_codes(audit)
    assert blocker_id in _audit_reference_ids(audit, "open_blocker")
    assert _warning_events(db_session, wo)


def test_acknowledged_blocker_warns_on_completion(client: TestClient, db_session: Session):
    """An ACKNOWLEDGED (not just OPEN) blocker is still an unsatisfied gate."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_blocker(db_session, wo, op, status_=WorkOrderBlockerStatus.ACKNOWLEDGED.value)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "open_blocker" in [e["code"] for e in resp.json()["quality_exceptions"]]


def test_resolved_blocker_does_not_warn(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_blocker(db_session, wo, op, status_=WorkOrderBlockerStatus.RESOLVED.value)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "open_blocker" not in [e["code"] for e in resp.json()["quality_exceptions"]]


# ---------------------------------------------------------------------------
# No false positives: ALL gates satisfied at once -> empty + NO audit/event
# (the existing test only clears the inspection gate; this clears all four)
# ---------------------------------------------------------------------------


def test_all_gates_satisfied_no_exception_recorded(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        requires_inspection=True,
        inspection_complete=True,  # inspection gate satisfied
    )
    make_ncr(db_session, wo, status_=NCRStatus.CLOSED, disposition=NCRDisposition.USE_AS_IS)  # NCR resolved
    make_fai(db_session, wo, part, status_=FAIStatus.PASSED)  # FAI passed
    make_blocker(db_session, wo, op, status_=WorkOrderBlockerStatus.RESOLVED.value)  # blocker cleared
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    # No exceptions surfaced, no exception audit row, no warning event.
    assert resp.json()["quality_exceptions"] == []
    assert _quality_exception_audit(db_session) == []
    assert _warning_events(db_session, wo) == []


# ---------------------------------------------------------------------------
# QG-4 partial coverage: the reconcile-on-read path records ONLY
# inspection_incomplete. NCR / FAI / blocker are intentionally NOT evaluated on
# the read path -- they are caught on the next live completion. This is the
# documented partial coverage, asserted (not treated as a bug). The GET still
# returns 200 the whole time.
# ---------------------------------------------------------------------------


def test_reconcile_on_read_does_not_evaluate_ncr_fai_blocker(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        quantity_complete=5,
        requires_inspection=True,  # cheap gate: WILL be recorded on read
    )
    # All the heavy gates are unsatisfied too -- but must NOT be evaluated on read.
    make_ncr(db_session, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    make_fai(db_session, wo, part, status_=FAIStatus.FAILED)
    make_blocker(db_session, wo, op, status_=WorkOrderBlockerStatus.OPEN.value)
    db_session.add(
        TimeEntry(
            user_id=admin.id,
            work_order_id=wo.id,
            operation_id=op.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow() - timedelta(hours=2),
            clock_out=datetime.utcnow() - timedelta(hours=1),
            duration_hours=1.0,
            quantity_produced=5,
            quantity_scrapped=0,
            company_id=COMPANY_A,
        )
    )
    db_session.commit()

    # The GET drives the op to COMPLETE via reconcile -- and stays 200.
    resp = client.get(f"/api/v1/shop-floor/operations/{op.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE

    reconcile_audit = [
        a for a in _quality_exception_audit(db_session) if (a.extra_data or {}).get("source") == "reconcile_on_read"
    ]
    assert reconcile_audit, "reconcile-driven completion must record inspection_incomplete"
    recorded = _audit_codes(reconcile_audit)
    # ONLY the cheap inspection gate is recorded on the read path.
    assert recorded == {"inspection_incomplete"}, recorded
    # The heavy gates are explicitly NOT on the read path (documented partial coverage).
    assert "open_ncr" not in recorded
    assert "fai_not_passed" not in recorded
    assert "open_blocker" not in recorded
