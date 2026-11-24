"""The completion gate judges the LATEST first article per (work order, part).

A failed FAI is frozen and answered by raising a new one, and CONDITIONAL is a final
status a customer can approve, so judging every FAI on the job kept raising
``fai_not_passed`` on every later completion with no way to clear it. Only the newest
FAI per part speaks for the job now: PASSED clears, CONDITIONAL with the customer's
approval on record clears, anything else (including the newest still being open)
warns. An FAI against a component part is judged apart from the job's own part.
"""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.part import Part
from app.models.quality import FAIStatus, FirstArticleInspection
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.quality_gate_service import (
    evaluate_completion_quality_exceptions,
    latest_fai_exceptions_by_work_order,
)

pytestmark = [pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _company(db: Session, company_id: int) -> None:
    if db.get(Company, company_id) is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.flush()


def _part(db: Session, company_id: int = COMPANY_A) -> Part:
    _company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"LFAI-P-{n}",
        name=f"Bracket {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    return part


def _wo(db: Session, part: Part, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"LFAI-WO-{n:05d}",
        customer_name="Fictional Aero",
        part_id=part.id,
        quantity_ordered=5,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=10),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    return wo


def _fai(
    db: Session,
    wo: WorkOrder,
    part: Part,
    status_: FAIStatus,
    *,
    minutes: int = 0,
    customer_approved: bool = False,
) -> FirstArticleInspection:
    n = _next()
    fai = FirstArticleInspection(
        fai_number=f"LFAI-{n}",
        part_id=part.id,
        work_order_id=wo.id,
        status=status_,
        customer_approved=customer_approved,
        created_at=datetime(2026, 11, 1, 8, 0) + timedelta(minutes=minutes),
        company_id=wo.company_id,
    )
    db.add(fai)
    db.flush()
    return fai


def _fai_refs(db: Session, wo: WorkOrder) -> list[int]:
    return [
        e.reference_id
        for e in evaluate_completion_quality_exceptions(db, wo, None, wo.company_id)
        if e.code == "fai_not_passed"
    ]


def test_failed_then_passed_redo_clears(db_session: Session):
    part = _part(db_session)
    wo = _wo(db_session, part)
    _fai(db_session, wo, part, FAIStatus.FAILED, minutes=0)
    _fai(db_session, wo, part, FAIStatus.PASSED, minutes=5)
    assert _fai_refs(db_session, wo) == []


def test_failed_then_open_redo_warns_on_the_redo_only(db_session: Session):
    part = _part(db_session)
    wo = _wo(db_session, part)
    _fai(db_session, wo, part, FAIStatus.FAILED, minutes=0)
    redo = _fai(db_session, wo, part, FAIStatus.IN_PROGRESS, minutes=5)
    assert _fai_refs(db_session, wo) == [redo.id]


def test_conditional_with_customer_approval_clears(db_session: Session):
    part = _part(db_session)
    wo = _wo(db_session, part)
    _fai(db_session, wo, part, FAIStatus.CONDITIONAL, customer_approved=True)
    assert _fai_refs(db_session, wo) == []


def test_conditional_without_customer_approval_still_warns(db_session: Session):
    part = _part(db_session)
    wo = _wo(db_session, part)
    fai = _fai(db_session, wo, part, FAIStatus.CONDITIONAL)
    excs = latest_fai_exceptions_by_work_order(db_session, COMPANY_A, [wo.id])[wo.id]
    assert [e.reference_id for e in excs] == [fai.id]
    assert "customer approval not recorded" in excs[0].message


def test_latest_still_failed_warns(db_session: Session):
    part = _part(db_session)
    wo = _wo(db_session, part)
    _fai(db_session, wo, part, FAIStatus.PASSED, minutes=0)
    newest = _fai(db_session, wo, part, FAIStatus.FAILED, minutes=5)
    assert _fai_refs(db_session, wo) == [newest.id]


def test_component_part_judged_apart_from_job_part(db_session: Session):
    part = _part(db_session)
    component = _part(db_session)
    wo = _wo(db_session, part)
    _fai(db_session, wo, part, FAIStatus.PASSED, minutes=10)
    component_fai = _fai(db_session, wo, component, FAIStatus.FAILED, minutes=0)
    assert _fai_refs(db_session, wo) == [component_fai.id]


def test_batched_lookup_is_tenant_scoped(db_session: Session):
    part_a = _part(db_session, COMPANY_A)
    wo_a = _wo(db_session, part_a, COMPANY_A)
    part_b = _part(db_session, COMPANY_B)
    wo_b = _wo(db_session, part_b, COMPANY_B)
    _fai(db_session, wo_b, part_b, FAIStatus.FAILED)
    result = latest_fai_exceptions_by_work_order(db_session, COMPANY_A, [wo_a.id, wo_b.id])
    assert result == {}
