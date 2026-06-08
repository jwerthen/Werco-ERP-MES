"""Tenant-isolation regression coverage for the work-order completion paths.

Locks in the Batch-1 hardening (branch qa/full-pass-2026-06-04) that scoped the
shop-floor and office completion endpoints to the *active* company. Before the
fix a company-A user could drive a company-B operation / work order / time entry
IN_PROGRESS or COMPLETE just by guessing a foreign integer id -- a CMMC AC /
tenant-isolation defect (a write across the tenant boundary).

Every test asserts the headline invariant: a cross-tenant call returns HTTP 404
*and leaves the foreign rows untouched* (status unchanged, no clock-out written).
The 404 must happen BEFORE any mutation -- so we re-read the company-B rows from
the DB after the rejected call and assert their pre-call state survived.

Endpoints covered (gap items TEN-1..TEN-4):
- POST /api/v1/shop-floor/clock-in              (foreign operation_id)        TEN-2
- POST /api/v1/shop-floor/clock-out/{id}        (foreign time_entry_id)       TEN-1
- PUT  /api/v1/shop-floor/operations/{id}/start (foreign op)                  TEN-3
- POST /api/v1/shop-floor/operations/{id}/complete (foreign op)              TEN-3
- PUT  /api/v1/work-orders/operations/{id}      (foreign op, update)          TEN-4
- POST /api/v1/work-orders/operations/{id}/start (foreign op)                 TEN-4
- POST /api/v1/work-orders/operations/{id}/complete (foreign op)             TEN-4

Company-A and company-B data are created directly in the DB (the shared
``db_session``); requests are made with a company-A token. Company B is a second
tenant (id=2); the default seeded company is id=1 (tests/conftest.py).
"""

from datetime import date, datetime, timedelta

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
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
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


def make_user(db: Session, *, company_id: int, role: UserRole = UserRole.OPERATOR) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"comp-iso-{n}@co{company_id}.test",
        employee_id=f"CISO-{n:05d}",
        first_name="Iso",
        last_name=f"C{company_id}",
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


def make_part(db: Session, *, company_id: int) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"CISO-P-{n}",
        name=f"Part {n}",
        description="completion-isolation fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, *, company_id: int) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"CISO-WC-{n}",
        code=f"CISO-WC-{n}",
        work_center_type="welding",
        description="completion-isolation fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_work_order_with_operation(
    db: Session,
    *,
    company_id: int,
    wo_status: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    op_status: OperationStatus = OperationStatus.IN_PROGRESS,
) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    part = make_part(db, company_id=company_id)
    wc = make_work_center(db, company_id=company_id)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"CISO-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=wo_status,
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
        name="Iso Op",
        status=op_status,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(wo)
    db.refresh(op)
    return wo, op, wc


def make_time_entry(
    db: Session,
    *,
    company_id: int,
    user: User,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    work_center: WorkCenter,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=work_order.id,
        operation_id=operation.id,
        work_center_id=work_center.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _reload(db: Session, model, pk: int):
    """Re-read a row fresh from the DB so we observe the committed state."""
    db.expire_all()
    return db.query(model).filter(model.id == pk).first()


# ---------------------------------------------------------------------------
# TEN-2: shop-floor clock-in with a foreign operation_id
# ---------------------------------------------------------------------------


def test_clock_in_foreign_operation_is_404_and_no_mutation(client: TestClient, db_session: Session):
    """A company-A user clocking in to a company-B operation gets 404; the foreign
    operation and its work order are left untouched (no IN_PROGRESS flip, no time
    entry created)."""
    a_user = make_user(db_session, company_id=COMPANY_A)
    wo_b, op_b, wc_b = make_work_order_with_operation(
        db_session,
        company_id=COMPANY_B,
        wo_status=WorkOrderStatus.RELEASED,
        op_status=OperationStatus.READY,
    )

    resp = client.post(
        "/api/v1/shop-floor/clock-in",
        headers=headers_for(a_user),
        json={
            "work_order_id": wo_b.id,
            "operation_id": op_b.id,
            "work_center_id": wc_b.id,
            "entry_type": "run",
        },
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Operation not found"

    # Foreign rows unchanged.
    op_after = _reload(db_session, WorkOrderOperation, op_b.id)
    wo_after = _reload(db_session, WorkOrder, wo_b.id)
    assert op_after.status == OperationStatus.READY
    assert wo_after.status == WorkOrderStatus.RELEASED
    # No time entry leaked into company B.
    assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op_b.id).count() == 0


# ---------------------------------------------------------------------------
# TEN-1: shop-floor clock-out with a foreign time_entry_id
# ---------------------------------------------------------------------------


def test_clock_out_foreign_time_entry_is_404_and_no_mutation(client: TestClient, db_session: Session):
    """A company-A user clocking out a company-B time entry gets 404; the foreign
    time entry stays open (clock_out is still null) and the op/WO are unchanged."""
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    wo_b, op_b, wc_b = make_work_order_with_operation(db_session, company_id=COMPANY_B)
    entry_b = make_time_entry(
        db_session, company_id=COMPANY_B, user=b_user, work_order=wo_b, operation=op_b, work_center=wc_b
    )

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_b.id}",
        headers=headers_for(a_user),
        json={"quantity_produced": 10, "quantity_scrapped": 0},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Time entry not found"

    entry_after = _reload(db_session, TimeEntry, entry_b.id)
    op_after = _reload(db_session, WorkOrderOperation, op_b.id)
    wo_after = _reload(db_session, WorkOrder, wo_b.id)
    assert entry_after.clock_out is None, "foreign time entry must not be clocked out"
    assert float(entry_after.quantity_produced or 0) == 0.0
    assert op_after.status == OperationStatus.IN_PROGRESS
    assert wo_after.status == WorkOrderStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# TEN-3: shop-floor operation start / complete with a foreign op
# ---------------------------------------------------------------------------


def test_shop_floor_start_operation_foreign_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    wo_b, op_b, _wc_b = make_work_order_with_operation(
        db_session,
        company_id=COMPANY_B,
        wo_status=WorkOrderStatus.RELEASED,
        op_status=OperationStatus.READY,
    )

    resp = client.put(
        f"/api/v1/shop-floor/operations/{op_b.id}/start",
        headers=headers_for(a_user),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Operation not found"

    op_after = _reload(db_session, WorkOrderOperation, op_b.id)
    wo_after = _reload(db_session, WorkOrder, wo_b.id)
    assert op_after.status == OperationStatus.READY
    assert op_after.actual_start is None
    assert wo_after.status == WorkOrderStatus.RELEASED


def test_shop_floor_complete_operation_foreign_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    wo_b, op_b, _wc_b = make_work_order_with_operation(db_session, company_id=COMPANY_B)

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op_b.id}/complete",
        headers=headers_for(a_user),
        json={"quantity_complete": 10},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Operation not found"

    op_after = _reload(db_session, WorkOrderOperation, op_b.id)
    wo_after = _reload(db_session, WorkOrder, wo_b.id)
    assert op_after.status == OperationStatus.IN_PROGRESS, "foreign op must not be completed"
    assert op_after.actual_end is None
    assert float(op_after.quantity_complete or 0) == 0.0
    assert wo_after.status == WorkOrderStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# TEN-4: office work-orders operation update / start / complete with a foreign op
# ---------------------------------------------------------------------------


def test_office_update_operation_foreign_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    _wo_b, op_b, _wc_b = make_work_order_with_operation(db_session, company_id=COMPANY_B)

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_b.id}",
        headers=headers_for(a_user),
        json={"version": 0, "name": "HIJACKED"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Operation not found"

    op_after = _reload(db_session, WorkOrderOperation, op_b.id)
    assert op_after.name == "Iso Op", "foreign op name must be unchanged"


def test_office_start_operation_foreign_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    wo_b, op_b, _wc_b = make_work_order_with_operation(
        db_session,
        company_id=COMPANY_B,
        wo_status=WorkOrderStatus.RELEASED,
        op_status=OperationStatus.READY,
    )

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_b.id}/start",
        headers=headers_for(a_user),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Operation not found"

    op_after = _reload(db_session, WorkOrderOperation, op_b.id)
    wo_after = _reload(db_session, WorkOrder, wo_b.id)
    assert op_after.status == OperationStatus.READY
    assert op_after.actual_start is None
    assert wo_after.status == WorkOrderStatus.RELEASED


def test_office_complete_operation_foreign_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    wo_b, op_b, _wc_b = make_work_order_with_operation(db_session, company_id=COMPANY_B)

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_b.id}/complete?quantity_complete=10",
        headers=headers_for(a_user),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Operation not found"

    op_after = _reload(db_session, WorkOrderOperation, op_b.id)
    wo_after = _reload(db_session, WorkOrder, wo_b.id)
    assert op_after.status == OperationStatus.IN_PROGRESS, "foreign op must not be completed"
    assert op_after.actual_end is None
    assert float(op_after.quantity_complete or 0) == 0.0
    assert wo_after.status == WorkOrderStatus.IN_PROGRESS


def test_office_complete_work_order_foreign_is_404_and_no_mutation(client: TestClient, db_session: Session):
    """Bonus control on the office WO-complete endpoint itself: a company-A
    manager cannot complete a company-B work order."""
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    wo_b, _op_b, _wc_b = make_work_order_with_operation(db_session, company_id=COMPANY_B)

    resp = client.post(
        f"/api/v1/work-orders/{wo_b.id}/complete?quantity_complete=10",
        headers=headers_for(a_user),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Work order not found"

    wo_after = _reload(db_session, WorkOrder, wo_b.id)
    assert wo_after.status == WorkOrderStatus.IN_PROGRESS, "foreign WO must not be completed"
    assert wo_after.actual_end is None


# ---------------------------------------------------------------------------
# TEN-5: active-operator read paths must not leak another tenant's clock-ins
#
# get_active_shop_users (GET /shop-floor/active-users) and the active_entries
# query feeding shop_floor_dashboard (GET /shop-floor/dashboard) filtered only
# on clock_out IS NULL, with no company scope -- so a company-A reader saw every
# tenant's currently-clocked-in operators. Both now also filter
# TimeEntry.company_id == active company. These tests assert a company-A token
# never observes a company-B operator who is clocked in.
# ---------------------------------------------------------------------------


def _open_time_entry(db: Session, *, company_id: int, user: User) -> TimeEntry:
    """A currently-clocked-in (clock_out IS NULL) time entry for one tenant."""
    wo, op, wc = make_work_order_with_operation(db, company_id=company_id)
    return make_time_entry(db, company_id=company_id, user=user, work_order=wo, operation=op, work_center=wc)


def test_active_users_excludes_other_tenants_clock_ins(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)

    a_entry = _open_time_entry(db_session, company_id=COMPANY_A, user=a_user)
    _open_time_entry(db_session, company_id=COMPANY_B, user=b_user)

    resp = client.get("/api/v1/shop-floor/active-users", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    user_ids = {row["user_id"] for row in resp.json()["active_users"]}
    assert a_user.id in user_ids, "company A reader should see company A's own clock-in"
    assert b_user.id not in user_ids, "company A reader must NOT see company B's clocked-in operator"
    # Sanity: the only entry surfaced for this tenant is company A's.
    assert all(row["user_id"] != b_user.id for row in resp.json()["active_users"])
    _ = a_entry  # keep the fixture referenced for clarity


def test_dashboard_active_assignments_exclude_other_tenants(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)

    _open_time_entry(db_session, company_id=COMPANY_A, user=a_user)
    _open_time_entry(db_session, company_id=COMPANY_B, user=b_user)

    resp = client.get("/api/v1/shop-floor/dashboard", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assignment_user_ids = {a["user"]["id"] for a in resp.json()["active_assignments"]}
    assert a_user.id in assignment_user_ids, "company A dashboard should include its own active operator"
    assert b_user.id not in assignment_user_ids, "company A dashboard must NOT include company B's active operator"
