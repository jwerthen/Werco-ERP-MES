"""Behavior locks for the AS9100D scrap-reason enforcement invariant.

A scrapped quantity is a quality/defect event: AS9100D defect-traceability requires
it to carry a reason. The UIs (kiosk/desktop) already block reasonless scrap, but the
rule is now enforced at the *data boundary* so a scripted/API client can't slip past
the UI. Two write endpoints carry a scrap quantity and are locked here:

1. ``POST /shop-floor/clock-out/{time_entry_id}`` -- ``ClockOut`` schema rejects when
   ``quantity_scrapped > 0`` and ``scrap_reason`` is missing/blank.
2. ``POST /shop-floor/operations/{operation_id}/production`` -- ``ProductionReportRequest``
   schema rejects when ``quantity_scrapped_delta > 0`` and ``scrap_reason`` is missing/blank.

Both are Pydantic ``model_validator(mode="after")`` checks on the request body, so a
violation is a **422** (not a 400), and -- because validation runs before the handler --
a rejected write mutates nothing.

Contracts locked here, for BOTH endpoints:
  * scrap > 0 with NO reason            -> 422, no scrap persisted.
  * scrap > 0 with a blank/whitespace reason -> 422 (blank counts as missing).
  * scrap > 0 WITH a real reason        -> 2xx, the reason is persisted on the TimeEntry.
  * scrap == 0 with NO reason           -> 2xx (regression guard: the kiosk COMPLETE flow
                                          clocks out zero scrap with no reason).
"""

from datetime import date, datetime, timedelta
from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
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


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole = UserRole.OPERATOR, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"scrap-req-{n}@co{company_id}.test",
        employee_id=f"SCRAPREQ-{n:05d}",
        first_name="Scrap",
        last_name="Reason",
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


def make_wo_op(db: Session, *, company_id: int = COMPANY_A) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    """One IN_PROGRESS work order with a single IN_PROGRESS operation (qty 10)."""
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"SCRAPREQ-P-{n}",
        name=f"Part {n}",
        description="scrap-reason enforcement fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wc = WorkCenter(
        name=f"SCRAPREQ-WC-{n}",
        code=f"SCRAPREQ-WC-{n}",
        work_center_type="welding",
        description="scrap-reason enforcement fixture work center",
        hourly_rate=100.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"SCRAPREQ-WO-{n:05d}",
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
    db.commit()
    return wo, op, wc


def make_open_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    scrap_reason: Optional[str] = None,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        scrap_reason=scrap_reason,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


# ===========================================================================
# Clock-out endpoint:  POST /shop-floor/clock-out/{time_entry_id}
# ===========================================================================


def test_clock_out_scrap_without_reason_is_422_and_persists_nothing(client: TestClient, db_session: Session):
    """Positive scrap with NO reason is a 422; the entry must stay open and un-scrapped."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 1, "quantity_scrapped": 2},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.clock_out is None, "a 422 clock-out must not close the entry"
    assert entry.quantity_scrapped == 0, "a 422 clock-out must not persist scrap"
    assert entry.scrap_reason is None
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS


def test_clock_out_scrap_with_blank_reason_is_422(client: TestClient, db_session: Session):
    """A whitespace-only reason counts as missing -> 422; nothing is written."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 0, "quantity_scrapped": 3, "scrap_reason": "   "},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.clock_out is None and entry.quantity_scrapped == 0
    assert entry.scrap_reason is None


def test_clock_out_scrap_with_reason_succeeds_and_persists_reason(client: TestClient, db_session: Session):
    """Positive scrap WITH a real reason succeeds and stamps the reason on the entry."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 5, "quantity_scrapped": 2, "scrap_reason": "Porosity in weld"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["scrap_reason"] == "Porosity in weld"

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.clock_out is not None, "the clock-out must land"
    assert entry.quantity_scrapped == 2
    assert entry.scrap_reason == "Porosity in weld"


def test_clock_out_zero_scrap_no_reason_succeeds(client: TestClient, db_session: Session):
    """Regression guard: the kiosk COMPLETE flow clocks out zero scrap with NO reason.
    That must still succeed -- the rule only fires for a *positive* scrap quantity."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 10, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.clock_out is not None, "a reason-less zero-scrap clock-out must still land"
    assert entry.quantity_scrapped == 0
    assert entry.scrap_reason is None


# ===========================================================================
# Production-report endpoint:  POST /shop-floor/operations/{operation_id}/production
# ===========================================================================


def test_production_scrap_without_reason_is_422_and_persists_nothing(client: TestClient, db_session: Session):
    """A positive scrap delta with NO reason is a 422; the active entry stays clean."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 1.0, "quantity_scrapped_delta": 2.0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.scrap_reason is None
    assert entry.quantity_scrapped == 0, "a 422 report must not persist scrap on the entry"
    op = db_session.get(WorkOrderOperation, op.id)
    assert (op.quantity_scrapped or 0) == 0, "a 422 report must not roll scrap onto the operation"
    assert (op.quantity_complete or 0) == 0, "a 422 report must not advance completed qty"


def test_production_scrap_with_blank_reason_is_422(client: TestClient, db_session: Session):
    """A whitespace-only reason counts as missing -> 422; nothing is written."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 0.0, "quantity_scrapped_delta": 1.0, "scrap_reason": "   "},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.scrap_reason is None and entry.quantity_scrapped == 0


def test_production_scrap_with_reason_succeeds_and_persists_reason(client: TestClient, db_session: Session):
    """A positive scrap delta WITH a real reason succeeds and stamps it on the active entry."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 1.0, "quantity_scrapped_delta": 2.0, "scrap_reason": "Material defect"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.scrap_reason == "Material defect"
    assert entry.quantity_scrapped == 2


def test_production_zero_scrap_no_reason_succeeds(client: TestClient, db_session: Session):
    """Regression guard: reporting good qty only (zero scrap, no reason) must succeed --
    the rule only fires for a *positive* scrap delta."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 3.0, "quantity_scrapped_delta": 0.0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op.id)
    assert (op.quantity_complete or 0) == 3, "a reason-less zero-scrap report must still land"
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.scrap_reason is None
