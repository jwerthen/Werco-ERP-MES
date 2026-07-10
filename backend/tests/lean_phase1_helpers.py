"""Shared factories for the Lean Phase 1 (issue #88) metric/scrap-code tests.

Several new test files (flow metrics, quality yield, ship OTD, adoption,
scrap-code CRUD/write paths, OEE service+cron, wallboard KPI strip) build the
same fixture vocabulary: companies, users+headers, parts with a standard cost,
work centers, WOs with routed operations, closed TimeEntries with provenance
sources, shipments, downtime, and scrap reason codes. Factored out once,
mirroring ``tests/api/kiosk_test_helpers.py``.

Every natural key routes through a module-level counter so rows stay globally
unique across companies and across tests sharing a worker DB under ``-n auto``.
All timestamps are naive UTC (matching the models' naive DateTime columns).
"""

from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimePlannedType
from app.models.part import Part
from app.models.scrap_reason import ScrapReasonCode
from app.models.shipping import Shipment, ShipmentStatus
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

COMPANY_A = 1
COMPANY_B = 2

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A) -> User:
    ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"lean1-{n}@co{company_id}.test",
        employee_id=f"LEAN1-{n:05d}",
        first_name="Lean",
        last_name=f"User{n}",
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


def make_part(db: Session, *, company_id: int = COMPANY_A, standard_cost: float = 0.0) -> Part:
    ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"LEAN1-P-{n:05d}",
        name=f"Lean part {n}",
        description="lean phase 1 fixture part",
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


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"LEAN1-WC-{n}",
        code=f"LEAN1-WC-{n:04d}",
        work_center_type="machining",
        description="lean phase 1 fixture work center",
        hourly_rate=100.0,
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
    company_id: int = COMPANY_A,
    status_: WorkOrderStatus = WorkOrderStatus.RELEASED,
    quantity_ordered: float = 10,
    customer_name: str = "Acme",
    due_date: Optional[date] = None,
    must_ship_by: Optional[date] = None,
    released_at: Optional[datetime] = None,
    actual_end: Optional[datetime] = None,
    quantity_scrapped: float = 0.0,
    scrap_reason_code_id: Optional[int] = None,
) -> WorkOrder:
    ensure_company(db, company_id)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"LEAN1-WO-{n:05d}",
        customer_name=customer_name,
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=status_,
        priority=5,
        due_date=due_date,
        must_ship_by=must_ship_by,
        released_at=released_at,
        actual_end=actual_end,
        quantity_scrapped=quantity_scrapped,
        scrap_reason_code_id=scrap_reason_code_id,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: Optional[WorkCenter],
    *,
    company_id: int = COMPANY_A,
    sequence: int = 10,
    status_: OperationStatus = OperationStatus.PENDING,
    run_time_per_piece: float = 0.0,
    quantity_complete: float = 0.0,
    quantity_scrapped: float = 0.0,
    quantity_reworked: float = 0.0,
    scrap_reason_code_id: Optional[int] = None,
    actual_start: Optional[datetime] = None,
    actual_end: Optional[datetime] = None,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id if wc else None,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        run_time_per_piece=run_time_per_piece,
        quantity_complete=quantity_complete,
        quantity_scrapped=quantity_scrapped,
        quantity_reworked=quantity_reworked,
        scrap_reason_code_id=scrap_reason_code_id,
        actual_start=actual_start,
        actual_end=actual_end,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def make_wo_with_op(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    wo_status: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    op_status: OperationStatus = OperationStatus.IN_PROGRESS,
    quantity_ordered: float = 10,
) -> Tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    """One WO with a single routed operation (the scrap write-path shape)."""
    part = make_part(db, company_id=company_id)
    wc = make_work_center(db, company_id=company_id)
    wo = make_wo(db, part, company_id=company_id, status_=wo_status, quantity_ordered=quantity_ordered)
    op = make_op(db, wo, wc, company_id=company_id, status_=op_status)
    return wo, op, wc


def make_entry(
    db: Session,
    user: User,
    wo: Optional[WorkOrder],
    op: Optional[WorkOrderOperation],
    wc: Optional[WorkCenter],
    *,
    company_id: int = COMPANY_A,
    entry_type: TimeEntryType = TimeEntryType.RUN,
    clock_in: Optional[datetime] = None,
    duration_hours: Optional[float] = 1.0,
    open_entry: bool = False,
    quantity_produced: float = 0.0,
    quantity_scrapped: float = 0.0,
    scrap_reason: Optional[str] = None,
    scrap_reason_code_id: Optional[int] = None,
    source: Optional[str] = None,
) -> TimeEntry:
    """A TimeEntry; closed with ``duration_hours`` unless ``open_entry=True``."""
    clock_in = clock_in or (datetime.utcnow() - timedelta(hours=2))
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id if wo else None,
        operation_id=op.id if op else None,
        work_center_id=wc.id if wc else None,
        entry_type=entry_type,
        clock_in=clock_in,
        clock_out=None if open_entry else clock_in + timedelta(hours=duration_hours or 0),
        duration_hours=None if open_entry else duration_hours,
        quantity_produced=quantity_produced,
        quantity_scrapped=quantity_scrapped,
        scrap_reason=scrap_reason,
        scrap_reason_code_id=scrap_reason_code_id,
        source=source,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_shipment(
    db: Session,
    wo: WorkOrder,
    *,
    company_id: int = COMPANY_A,
    ship_date: Optional[date] = None,
    quantity_shipped: float = 0.0,
    status: ShipmentStatus = ShipmentStatus.SHIPPED,
    is_deleted: bool = False,
) -> Shipment:
    n = _next()
    shipment = Shipment(
        shipment_number=f"LEAN1-SHP-{n:05d}",
        work_order_id=wo.id,
        status=status,
        quantity_shipped=quantity_shipped,
        ship_date=ship_date,
        company_id=company_id,
    )
    if is_deleted:
        shipment.is_deleted = True
        shipment.deleted_at = datetime.utcnow()
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    return shipment


def make_scrap_code(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    code: Optional[str] = None,
    name: Optional[str] = None,
    category: str = "other",
    is_active: bool = True,
    display_order: int = 0,
) -> ScrapReasonCode:
    ensure_company(db, company_id)
    n = _next()
    reason = ScrapReasonCode(
        code=code or f"SC{n:04d}",
        name=name or f"Scrap reason {n}",
        category=category,
        is_active=is_active,
        display_order=display_order,
        company_id=company_id,
    )
    db.add(reason)
    db.commit()
    db.refresh(reason)
    return reason


def make_downtime(
    db: Session,
    user: User,
    wc: WorkCenter,
    *,
    company_id: int = COMPANY_A,
    start_time: Optional[datetime] = None,
    duration_minutes: float = 30.0,
    planned: bool = False,
) -> DowntimeEvent:
    start = start_time or (datetime.utcnow() - timedelta(hours=3))
    event = DowntimeEvent(
        work_center_id=wc.id,
        start_time=start,
        end_time=start + timedelta(minutes=duration_minutes),
        duration_minutes=duration_minutes,
        category=DowntimeCategory.MECHANICAL,
        planned_type=DowntimePlannedType.PLANNED if planned else DowntimePlannedType.UNPLANNED,
        reported_by=user.id,
        company_id=company_id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
