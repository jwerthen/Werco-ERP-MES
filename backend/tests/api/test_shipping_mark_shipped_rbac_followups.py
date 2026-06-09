"""Behavior locks for the now-RBAC-gated ``mark_shipped`` endpoint
(fix/wo-followups-round2, FIX 3).

``POST /shipping/{id}/ship`` is the terminal shipping action that CLOSES the work order, so
it is now gated to the documented Shipping-Complete role set
(ADMIN / MANAGER / SUPERVISOR / SHIPPING). Previously ANY authenticated user could close a WO
by shipping it (a privilege gap). The change is intentional: non-privileged tenant users
(OPERATOR / QUALITY / VIEWER) now get 403.

Covered:
  - each privileged role (ADMIN, MANAGER, SUPERVISOR, SHIPPING) can ship -> 200, shipment SHIPPED.
  - each non-privileged role (OPERATOR, QUALITY, VIEWER) is rejected -> 403, shipment unchanged.
  - a cross-tenant shipment id still 404s (tenant scope precedes / is unaffected by RBAC).
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.part import Part
from app.models.shipping import Shipment, ShipmentStatus
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus

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


def make_user(db: Session, *, role: UserRole, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"ship-rbac-{n}@co{company_id}.test",
        employee_id=f"SHIPRBAC-{n:05d}",
        first_name="Ship",
        last_name=f"Co{company_id}",
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
        part_number=f"SHIPRBAC-P-{n}",
        name=f"Part {n}",
        description="ship rbac fixture",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_wo(db: Session, part: Part, *, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"SHIPRBAC-WO-{n:05d}",
        customer_name="No Customer Match Co",  # no Customer row -> no auto-CoC interference
        part_id=part.id,
        quantity_ordered=10,
        quantity_complete=10,
        status=WorkOrderStatus.COMPLETE,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_shipment(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> Shipment:
    n = _next()
    shipment = Shipment(
        shipment_number=f"SHIPRBAC-SHP-{n:05d}",
        work_order_id=wo.id,
        status=ShipmentStatus.PENDING,
        quantity_shipped=10,
        cert_of_conformance=False,
        company_id=company_id,
    )
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    return shipment


PRIVILEGED = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.SHIPPING]
NON_PRIVILEGED = [UserRole.OPERATOR, UserRole.QUALITY, UserRole.VIEWER]


# ---------------------------------------------------------------------------
# Privileged roles can ship (200) and the shipment is marked SHIPPED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", PRIVILEGED)
def test_privileged_role_can_mark_shipped(client: TestClient, db_session: Session, role: UserRole):
    user = make_user(db_session, role=role)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, f"{role}: {resp.text}"

    db_session.expire_all()
    assert db_session.get(Shipment, shipment.id).status == ShipmentStatus.SHIPPED
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.CLOSED


# ---------------------------------------------------------------------------
# Non-privileged roles are rejected (403) and the shipment is unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", NON_PRIVILEGED)
def test_non_privileged_role_forbidden_to_ship(client: TestClient, db_session: Session, role: UserRole):
    user = make_user(db_session, role=role)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(user))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, f"{role}: {resp.text}"

    # Neither the shipment nor the WO was advanced.
    db_session.expire_all()
    assert db_session.get(Shipment, shipment.id).status == ShipmentStatus.PENDING
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE


# ---------------------------------------------------------------------------
# Cross-tenant shipment id still 404s, even for a privileged caller.
# ---------------------------------------------------------------------------


def test_mark_shipped_cross_tenant_404(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    part_b = make_part(db_session, company_id=COMPANY_B)
    wo_b = make_wo(db_session, part_b, company_id=COMPANY_B)
    shipment_b = make_shipment(db_session, wo_b, company_id=COMPANY_B)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment_b.id}/ship", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # Company-B shipment untouched.
    db_session.expire_all()
    assert db_session.get(Shipment, shipment_b.id).status == ShipmentStatus.PENDING
