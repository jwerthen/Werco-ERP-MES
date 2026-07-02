"""Shared factories for the crew-station kiosk test files.

The five ``test_kiosk_*.py`` files share the same fixture vocabulary (companies,
badge users, work centers, PIN stations, work orders with a queued operation),
mirroring the self-contained helper style of ``tests/test_visitor_logs.py`` but
factored out once because five files consume it.

Every natural key routes through a module-level counter so rows stay globally
unique across companies and across tests sharing a worker DB under ``-n auto``.
"""

from datetime import datetime
from typing import Optional, Tuple

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token, create_kiosk_token, get_password_hash
from app.models.company import Company
from app.models.kiosk_station import KioskStation
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

COMPANY_A = 1
COMPANY_B = 2

BADGE_TOKEN_URL = "/api/v1/auth/kiosk-badge-token"
STATION_LOGIN_URL = "/api/v1/shop-floor/kiosk-stations/station-login"
STATIONS_URL = "/api/v1/shop-floor/kiosk-stations"


def queue_url(work_center_id: int) -> str:
    return f"/api/v1/shop-floor/work-center-queue/{work_center_id}"


# Module-level counter so every fixture row gets a globally unique natural key.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def ensure_company(db: Session, company_id: int) -> Company:
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


def make_user(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    role: UserRole = UserRole.OPERATOR,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    is_active: bool = True,
    locked_until: Optional[datetime] = None,
    employee_id: Optional[str] = None,
) -> User:
    ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"kiosk-user-{n}@co{company_id}.test",
        employee_id=employee_id or f"KB-{n:05d}",
        first_name=first_name or "Kiosk",
        last_name=last_name or f"User{n}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens minted directly; never used for login
        role=role,
        is_active=is_active,
        is_superuser=False,
        locked_until=locked_until,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_work_center(db: Session, *, company_id: int = COMPANY_A, name: Optional[str] = None) -> WorkCenter:
    ensure_company(db, company_id)
    n = _next()
    work_center = WorkCenter(
        name=name or f"Weld Bay {n}",
        code=f"WLD-{n:04d}",
        work_center_type="welding",
        hourly_rate=95.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(work_center)
    db.commit()
    db.refresh(work_center)
    return work_center


def make_kiosk_station(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    work_center: Optional[WorkCenter] = None,
    pin: str = "1234",
    label: Optional[str] = None,
    revoked: bool = False,
) -> KioskStation:
    ensure_company(db, company_id)
    if work_center is None:
        work_center = make_work_center(db, company_id=company_id)
    n = _next()
    station = KioskStation(
        label=label or f"Crew Kiosk {n}",
        work_center_id=work_center.id,
        pin_hash=get_password_hash(pin),
        revoked=revoked,
        company_id=company_id,
    )
    db.add(station)
    db.commit()
    db.refresh(station)
    return station


def kiosk_token_for(station: KioskStation, *, company_id: Optional[int] = None) -> str:
    """Mint a station kiosk token directly (bypassing station-login)."""
    return create_kiosk_token(
        station_id=station.id,
        company_id=company_id if company_id is not None else station.company_id,
        label=station.label,
    )


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def user_headers(user: User, *, active_company_id: Optional[int] = None) -> dict:
    cid = active_company_id if active_company_id is not None else user.company_id
    return bearer(create_access_token(subject=user.id, company_id=cid))


def make_wo_with_operation(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    work_center: WorkCenter,
    quantity_ordered: float = 10,
    op_status: OperationStatus = OperationStatus.READY,
    wo_status: WorkOrderStatus = WorkOrderStatus.RELEASED,
) -> Tuple[WorkOrder, WorkOrderOperation]:
    ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"KP-{n:05d}",
        name=f"Kiosk part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    work_order = WorkOrder(
        work_order_number=f"KWO-{n:05d}",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=wo_status,
        priority=2,
        company_id=company_id,
    )
    db.add(work_order)
    db.flush()
    operation = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=work_center.id,
        sequence=10,
        operation_number="OP10",
        name="Weld out",
        status=op_status,
        company_id=company_id,
    )
    db.add(operation)
    db.commit()
    db.refresh(work_order)
    db.refresh(operation)
    return work_order, operation


def mint_badge_token(client: TestClient, station_token: str, employee_id: str):
    """POST /auth/kiosk-badge-token with a station bearer; returns the raw response."""
    return client.post(
        BADGE_TOKEN_URL,
        headers=bearer(station_token),
        json={"employee_id": employee_id},
    )
