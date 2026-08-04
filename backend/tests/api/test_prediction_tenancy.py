"""The three ``/analytics/predict/*`` endpoints were cross-tenant END TO END.

``PredictionService`` was constructed ``PredictionService(db)`` -- a session and nothing
else -- while every other service in the very same router file is constructed
``AnalyticsService(db, company_id)``. Sixteen reads in the module therefore carried no
``company_id`` predicate, and all three routes are reachable in production by anyone
holding an ADMIN / MANAGER / SUPERVISOR (or superuser / platform-admin) token:

* ``GET /analytics/predict/delivery/{work_order_id}`` looked a work order up **by primary
  key with no ownership check at all**. ``work_order_id`` is a sequential integer, so
  ``for i in 1..N`` walked the entire platform's work orders. Per id the response returned
  the header (number, part number, quantity, due date) *and* ``operations[]`` -- the
  sequenced routing, with the machine name and estimated hours for every step. For a job
  shop that array is the process plan.
* ``GET /analytics/predict/capacity`` returned **every tenant's machine list by name**, and
  summed every tenant's open work orders into ``committed_hours`` /
  ``overall_utilization``. It backs the default ``/analytics`` landing panel.
* ``GET /analytics/predict/inventory-demand`` selected the part set with no scope, so the
  three per-part reads underneath it (on-hand, 90-day usage, open POs) were then executed
  for **every other tenant's parts**, and foreign part numbers/names were rendered into
  ``predictions``.

This file is the two-tenant reproduction. Company B's every user-visible string carries the
literal marker ``ZBRAVO``; the endpoint tests assert on the **raw response body**, not on a
row count, so a leak cannot hide behind a length assertion.

Beyond plain "B's own rows", the fixture deliberately plants the harder shapes: rows whose
FKs cross the tenant boundary in BOTH directions -- a company-B operation on company A's
work center AND on company A's work order; a company-A operation on company B's work order
AND on company B's work center; a company-A work order pointing at a company-B part; a
company-B inventory item / transaction / PO line against company A's part; and PO lines
whose ``company_id`` disagrees with their own header's.

Every one of those columns is a plain FK, and they were cross-tenant-writable until #194, so
"it filters by work_order_id / groups by work_center_id, therefore it is transitively
scoped" is not an argument -- it is the exact reasoning #191 rejected. Those rows are what
make the numeric assertions below (``queue_position == 2``, ``estimated_hours == 2.0``,
``committed_hours == 1.5``, ``current_stock == 100.0``, ``open_po_quantity == 40.0``) fail
against unscoped code even though no marker string would appear in those particular fields.

WHY THE SHAPES ARE THAT ELABORATE. Every tenancy predicate in the module was removed one at
a time and this suite re-run (19 mutants). A predicate whose removal leaves the suite green
is unguarded, however correct it is. The first pass left seven green -- all seven were
fixture gaps, not redundant predicates, and the cross-boundary FK rows above are what closes
them. The second pass is red on all nineteen.

THE TWO ``WorkOrderOperation`` READS ALSO GAINED A HEADER JOIN. ``_get_queue_depths`` and
``_get_historical_cycle_times`` read that table without ever reaching its work order, so
``WorkOrder.is_deleted`` could not be filtered and the caller's own soft-deleted job inflated
its own queue depth and steered its own estimates (invariant 3 -- no cross-tenant component,
but the two sibling reads in the module DO filter it, so the module disagreed with itself).
Each of the four predicates on that join is pinned by a test that fails without it:
``test_queue_depth_excludes_a_soft_deleted_work_orders_operations`` and the parametrized
``test_cycle_times_ignore_history_hanging_off_a_job_that_is_not_open_and_ours``.

WHAT FAILS AGAINST PRE-FIX CODE. Every test in the "cross-tenant", "oracle" and "numbers"
sections, plus the two constructor tests (which fail at ``TypeError``). The single-tenant
control section passes both before and after -- that is its job, and it was additionally
verified by running the pre-fix service and the post-fix service side by side against one
company's data and comparing the payloads field for field.
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.database import Base
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.prediction_service import PredictionService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

# Every company-B string a user could ever see embeds this token. The endpoint tests grep
# the raw response body for it.
MARKER = "ZBRAVO"

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

DELIVERY = "/api/v1/analytics/predict/delivery"
CAPACITY = "/api/v1/analytics/predict/capacity"
DEMAND = "/api/v1/analytics/predict/inventory-demand"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ===========================================================================
# Fixture construction
# ===========================================================================


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"tenancy-{n}@co{company_id}.test",
        employee_id=f"TEN-{n:05d}",
        first_name="Ten",
        last_name="Ancy",
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


def headers_for(user: User, *, active_company_id: int = None) -> dict:
    """Mint a token. ``active_company_id`` models a platform admin who switched context --
    the JWT claim, not ``user.company_id``, is what ``get_current_company_id`` returns."""
    token = create_access_token(
        subject=user.id,
        company_id=active_company_id if active_company_id is not None else user.company_id,
    )
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_work_center(
    db: Session,
    *,
    name: str,
    company_id: int,
    capacity_hours_per_day: float = 8.0,
    efficiency_factor: float = 1.0,
    is_active: bool = True,
) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        code=f"WC{n:04d}",
        name=name,
        work_center_type="machining",
        capacity_hours_per_day=capacity_hours_per_day,
        efficiency_factor=efficiency_factor,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_part(
    db: Session,
    *,
    part_number: str,
    name: str,
    part_type: str = "manufactured",
    company_id: int = COMPANY_A,
    is_active: bool = True,
    is_deleted: bool = False,
) -> Part:
    _ensure_company(db, company_id)
    part = Part(
        part_number=part_number,
        name=name,
        description="prediction-tenancy fixture part",
        part_type=part_type,
        unit_of_measure="each",
        standard_cost=1.0,
        is_active=is_active,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_wo(
    db: Session,
    *,
    number: str,
    part: Part,
    company_id: int,
    ordered: float = 10.0,
    complete: float = 0.0,
    customer_name: str = "Acme",
    wo_status: WorkOrderStatus = WorkOrderStatus.RELEASED,
    is_deleted: bool = False,
    due_in_days: int = 30,
) -> WorkOrder:
    wo = WorkOrder(
        work_order_number=number,
        customer_name=customer_name,
        part_id=part.id,
        quantity_ordered=ordered,
        quantity_complete=complete,
        status=wo_status,
        priority=5,
        due_date=date.today() + timedelta(days=due_in_days),
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_op(
    db: Session,
    *,
    wo: WorkOrder,
    work_center: WorkCenter,
    company_id: int,
    name: str,
    sequence: int = 10,
    setup_time_hours: float = 1.0,
    run_time_per_piece: float = 0.1,
    run_time_hours: float = 1.0,
    op_status: OperationStatus = OperationStatus.PENDING,
    quantity_complete: float = 0.0,
    actual_setup_hours: float = 0.0,
    actual_run_hours: float = 0.0,
    actual_end: datetime = None,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=work_center.id,
        sequence=sequence,
        name=name,
        setup_time_hours=setup_time_hours,
        run_time_hours=run_time_hours,
        run_time_per_piece=run_time_per_piece,
        actual_setup_hours=actual_setup_hours,
        actual_run_hours=actual_run_hours,
        status=op_status,
        quantity_complete=quantity_complete,
        actual_end=actual_end,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def make_inventory(db: Session, *, part: Part, company_id: int, quantity: float, is_active: bool = True) -> None:
    db.add(
        InventoryItem(
            part_id=part.id,
            location="BIN-1",
            warehouse="MAIN",
            quantity_on_hand=quantity,
            is_active=is_active,
            company_id=company_id,
        )
    )
    db.commit()


def make_issue(db: Session, *, part: Part, company_id: int, quantity: float, user: User, days_ago: int = 5) -> None:
    db.add(
        InventoryTransaction(
            part_id=part.id,
            transaction_type=TransactionType.ISSUE,
            quantity=quantity,
            created_by=user.id,
            created_at=datetime.utcnow() - timedelta(days=days_ago),
            company_id=company_id,
        )
    )
    db.commit()


def make_po_line(
    db: Session,
    *,
    part: Part,
    company_id: int,
    ordered: float,
    received: float = 0.0,
    po_status: POStatus = POStatus.SENT,
    required_in_days: int = 10,
    po_is_deleted: bool = False,
    is_closed: bool = False,
    with_required_date: bool = True,
) -> PurchaseOrderLine:
    n = _next()
    vendor = Vendor(code=f"V{n:04d}", name=f"Vendor {n}", company_id=company_id)
    db.add(vendor)
    db.flush()
    po = PurchaseOrder(
        po_number=f"PO-{n:05d}",
        vendor_id=vendor.id,
        status=po_status,
        expected_date=date.today() + timedelta(days=required_in_days + 5),
        is_deleted=po_is_deleted,
        company_id=company_id,
    )
    db.add(po)
    db.flush()
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=ordered,
        quantity_received=received,
        unit_price=1.0,
        required_date=(date.today() + timedelta(days=required_in_days)) if with_required_date else None,
        is_closed=is_closed,
        company_id=company_id,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def make_misaligned_po_line(
    db: Session,
    *,
    part: Part,
    po_company: int,
    line_company: int,
    ordered: float,
    required_in_days: int,
) -> PurchaseOrderLine:
    """A PO line whose ``company_id`` DISAGREES with its header's.

    ``_get_open_po_info`` carries a tenant predicate on both the line and the joined
    header. For an aligned pair either predicate alone would exclude a foreign row, so only
    a misaligned pair proves each one is individually load-bearing. ``purchase_order_id`` is
    a plain FK and both models set ``company_id`` at write time, so a cross-tenant write bug
    on either side produces exactly this.
    """
    n = _next()
    vendor = Vendor(code=f"VX{n:04d}", name=f"Vendor X {n}", company_id=po_company)
    db.add(vendor)
    db.flush()
    po = PurchaseOrder(
        po_number=f"POX-{n:05d}",
        vendor_id=vendor.id,
        status=POStatus.SENT,
        expected_date=date.today() + timedelta(days=required_in_days + 5),
        is_deleted=False,
        company_id=po_company,
    )
    db.add(po)
    db.flush()
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=ordered,
        quantity_received=0.0,
        unit_price=1.0,
        required_date=date.today() + timedelta(days=required_in_days),
        is_closed=False,
        company_id=line_company,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


class World:
    """Namespace for the two-tenant fixture."""


@pytest.fixture
def world(db_session: Session) -> World:
    """Two complete tenants.

    Company A is small and hand-computable. Company B is large, marker-named, and
    deliberately mis-parented into A's objects wherever a plain FK allows it.
    """
    w = World()
    db = db_session

    w.a_manager = make_user(db, role=UserRole.MANAGER, company_id=COMPANY_A)
    w.b_manager = make_user(db, role=UserRole.MANAGER, company_id=COMPANY_B)

    # ---- work centers -------------------------------------------------
    w.a_wc = make_work_center(db, name="Alpha Cell One", company_id=COMPANY_A)
    w.b_wc = make_work_center(db, name=f"{MARKER} Mill Cell", company_id=COMPANY_B)

    # ---- parts --------------------------------------------------------
    w.a_raw = make_part(
        db, part_number="ALPHA-RAW-1", name="Alpha Sheet", part_type="raw_material", company_id=COMPANY_A
    )
    w.a_asm = make_part(db, part_number="ALPHA-ASM-1", name="Alpha Weldment", company_id=COMPANY_A)
    # An A part that must NOT be forecast: soft-deleted (invariant 3).
    w.a_retired = make_part(
        db,
        part_number="ALPHA-RAW-RETIRED",
        name="Alpha Retired Sheet",
        part_type="raw_material",
        company_id=COMPANY_A,
        is_deleted=True,
    )
    w.b_raw = make_part(
        db, part_number=f"{MARKER}-RAW-1", name=f"{MARKER} Sheet", part_type="raw_material", company_id=COMPANY_B
    )
    w.b_purchased = make_part(
        db, part_number=f"{MARKER}-BUY-1", name=f"{MARKER} Bearing", part_type="purchased", company_id=COMPANY_B
    )
    w.b_asm = make_part(db, part_number=f"{MARKER}-ASM-1", name=f"{MARKER} Weldment", company_id=COMPANY_B)

    # ---- work orders + routing ----------------------------------------
    # A: one open job, two pending operations, 10 pieces.
    #   op 10 -> 1.0 setup + 0.1/pc * 10 = 2.0 h
    #   op 20 -> 2.0 setup + 0.2/pc * 10 = 4.0 h   => 6.0 h committed at A's cell
    w.a_wo = make_wo(db, number="ALPHA-WO-0001", part=w.a_asm, company_id=COMPANY_A, ordered=10)
    w.a_op10 = make_op(db, wo=w.a_wo, work_center=w.a_wc, company_id=COMPANY_A, name="Alpha Saw", sequence=10)
    w.a_op20 = make_op(
        db,
        wo=w.a_wo,
        work_center=w.a_wc,
        company_id=COMPANY_A,
        name="Alpha Mill",
        sequence=20,
        setup_time_hours=2.0,
        run_time_per_piece=0.2,
    )
    # A soft-deleted A job: not open demand, not committed capacity, and -- because the two
    # ``WorkOrderOperation`` reads now join their header -- not queued work and not cycle-time
    # history either (invariant 3). Its two operations are what make each half of that join
    # load-bearing: the PENDING one for ``_get_queue_depths``, the COMPLETE one (10 h actual
    # against a 2 h plan, ratio 5.0) for ``_get_historical_cycle_times``.
    w.a_wo_deleted = make_wo(
        db, number="ALPHA-WO-DEAD", part=w.a_asm, company_id=COMPANY_A, ordered=1000, is_deleted=True
    )
    w.a_dead_op_pending = make_op(
        db,
        wo=w.a_wo_deleted,
        work_center=w.a_wc,
        company_id=COMPANY_A,
        name="Alpha Dead Op",
        setup_time_hours=100.0,
        run_time_per_piece=1.0,
    )
    w.a_dead_op_done = make_op(
        db,
        wo=w.a_wo_deleted,
        work_center=w.a_wc,
        company_id=COMPANY_A,
        name="Alpha Dead History",
        sequence=20,
        setup_time_hours=1.0,
        run_time_hours=1.0,
        op_status=OperationStatus.COMPLETE,
        actual_setup_hours=2.0,
        actual_run_hours=8.0,
        actual_end=datetime.utcnow() - timedelta(days=1),
    )

    # B: its own big job on its own cell.
    w.b_wo = make_wo(
        db,
        number=f"{MARKER}-WO-0001",
        part=w.b_asm,
        company_id=COMPANY_B,
        ordered=500,
        customer_name=f"{MARKER} Aerospace",
    )
    w.b_op = make_op(
        db,
        wo=w.b_wo,
        work_center=w.b_wc,
        company_id=COMPANY_B,
        name=f"{MARKER} Broach",
        setup_time_hours=50.0,
        run_time_per_piece=2.0,
    )

    # B rows MIS-PARENTED onto A's work center. These are the rows that defeat
    # "grouping by work_center_id makes it transitively scoped".
    w.b_op_on_a_cell_pending = make_op(
        db,
        wo=w.b_wo,
        work_center=w.a_wc,
        company_id=COMPANY_B,
        name=f"{MARKER} Queue Filler",
        sequence=30,
    )
    # Completed, with a wildly different actual/estimate ratio (10h actual vs 2h planned
    # => ratio 5.0). Unscoped, this multiplies A's estimated_hours by 5 and quintuples
    # A's queue-wait basis.
    w.b_op_on_a_cell_done = make_op(
        db,
        wo=w.b_wo,
        work_center=w.a_wc,
        company_id=COMPANY_B,
        name=f"{MARKER} History",
        sequence=40,
        setup_time_hours=1.0,
        run_time_hours=1.0,
        op_status=OperationStatus.COMPLETE,
        actual_setup_hours=2.0,
        actual_run_hours=8.0,
        actual_end=datetime.utcnow() - timedelta(days=1),
    )

    # B rows MIS-PARENTED onto A's WORK ORDER. ``WorkOrderOperation.work_order_id`` is a
    # plain FK too, so a foreign operation can hang off this caller's job -- which is what
    # makes the tenant predicate on the ROUTING read (and on the batched capacity op read)
    # load-bearing rather than implied by ``work_order_id``.
    w.b_op_on_a_wo = make_op(
        db,
        wo=w.a_wo,
        work_center=w.a_wc,
        company_id=COMPANY_B,
        name=f"{MARKER} Interloper",
        sequence=15,
        setup_time_hours=9.0,
        run_time_per_piece=0.9,
    )

    # The mirror image: an operation THIS caller owns, hanging off company B's job. The
    # capacity forecast reads the open work-order set first and the operations second, so
    # only a predicate on the WORK ORDER stops company B's ``quantity_ordered`` (500) from
    # being multiplied into this caller's committed hours via an operation A does own.
    # IN_PROGRESS keeps it out of the queue-depth and cycle-time reads, isolating the
    # capacity effect.
    w.a_op_on_b_wo = make_op(
        db,
        wo=w.b_wo,
        work_center=w.a_wc,
        company_id=COMPANY_A,
        name="Alpha Stray",
        sequence=25,
        setup_time_hours=7.0,
        run_time_per_piece=0.0,
        op_status=OperationStatus.IN_PROGRESS,
    )
    # Two more of the same shape -- an operation company A owns on company B's job -- whose
    # statuses put them in the OTHER two ``WorkOrderOperation`` reads. The operation-level
    # tenant predicate cannot exclude either (A really does own them); only the predicate on
    # the JOINED header can, which is what makes ``WorkOrder.company_id`` load-bearing on
    # both queue depth and cycle times rather than merely correct.
    w.a_op_on_b_wo_pending = make_op(
        db,
        wo=w.b_wo,
        work_center=w.a_wc,
        company_id=COMPANY_A,
        name="Alpha Stray Queued",
        sequence=26,
    )
    w.a_op_on_b_wo_done = make_op(
        db,
        wo=w.b_wo,
        work_center=w.a_wc,
        company_id=COMPANY_A,
        name="Alpha Stray History",
        sequence=27,
        setup_time_hours=1.0,
        run_time_hours=1.0,
        op_status=OperationStatus.COMPLETE,
        actual_setup_hours=2.0,
        actual_run_hours=8.0,
        actual_end=datetime.utcnow() - timedelta(days=1),
    )

    # A work order company A owns whose PART and WORK CENTER both belong to company B --
    # the two remaining plain FKs that get turned into strings in the delivery response.
    # ON_HOLD so it never enters the capacity forecast and cannot perturb those numbers.
    w.a_wo_foreign_refs = make_wo(
        db,
        number="ALPHA-WO-XREF",
        part=w.b_asm,
        company_id=COMPANY_A,
        ordered=4,
        wo_status=WorkOrderStatus.ON_HOLD,
    )
    make_op(
        db,
        wo=w.a_wo_foreign_refs,
        work_center=w.b_wc,
        company_id=COMPANY_A,
        name="Alpha Xref Op",
    )

    # ---- BOMs ---------------------------------------------------------
    for assembly, component, qty, company in (
        (w.a_asm, w.a_raw, 2.0, COMPANY_A),
        (w.b_asm, w.b_raw, 3.0, COMPANY_B),
    ):
        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=company)
        db.add(bom)
        db.flush()
        db.add(
            BOMItem(
                bom_id=bom.id,
                component_part_id=component.id,
                item_number=10,
                quantity=qty,
                item_type="buy",
                line_type="component",
                unit_of_measure="each",
                company_id=company,
            )
        )
    db.commit()

    # ---- inventory ----------------------------------------------------
    make_inventory(db, part=w.a_raw, company_id=COMPANY_A, quantity=100.0)
    make_inventory(db, part=w.b_raw, company_id=COMPANY_B, quantity=7777.0)
    # Mis-parented: a company-B stock row against company A's part.
    make_inventory(db, part=w.a_raw, company_id=COMPANY_B, quantity=500.0)

    # ---- usage history (90-day window) --------------------------------
    make_issue(db, part=w.a_raw, company_id=COMPANY_A, quantity=90.0, user=w.a_manager)  # -> 1.0/day
    make_issue(db, part=w.b_raw, company_id=COMPANY_B, quantity=9000.0, user=w.b_manager)
    # Mis-parented: company-B issue against company A's part.
    make_issue(db, part=w.a_raw, company_id=COMPANY_B, quantity=900.0, user=w.b_manager)

    # ---- purchase orders ----------------------------------------------
    # A: 50 ordered / 10 received => 40 open, due in 10 days.
    make_po_line(db, part=w.a_raw, company_id=COMPANY_A, ordered=50.0, received=10.0, required_in_days=10)
    # A soft-deleted PO must not count as inbound supply (invariant 3).
    make_po_line(db, part=w.a_raw, company_id=COMPANY_A, ordered=999.0, required_in_days=1, po_is_deleted=True)
    # Mis-parented: a company-B PO line against company A's part.
    make_po_line(db, part=w.a_raw, company_id=COMPANY_B, ordered=7000.0, required_in_days=2)
    make_po_line(db, part=w.b_raw, company_id=COMPANY_B, ordered=8000.0, required_in_days=3)
    # The two misaligned shapes -- one for each side of the join. Neither may be counted.
    make_misaligned_po_line(
        db, part=w.a_raw, po_company=COMPANY_A, line_company=COMPANY_B, ordered=600.0, required_in_days=4
    )
    make_misaligned_po_line(
        db, part=w.a_raw, po_company=COMPANY_B, line_company=COMPANY_A, ordered=300.0, required_in_days=6
    )

    return w


# ===========================================================================
# Helpers
# ===========================================================================


def _row_counts(db: Session) -> dict:
    return {t.name: db.execute(select(func.count()).select_from(t)).scalar() for t in Base.metadata.sorted_tables}


@contextmanager
def asserts_nothing_was_written(db: Session):
    """A prediction endpoint is a POLL, not an actor (CLAUDE.md invariant 2).

    Guards both halves: no ``Session.commit()`` at all, and no row-count delta in ANY
    table -- audit_log and inventory_transactions included. #199 found two GETs that
    committed, which is why this is asserted rather than assumed.
    """
    commits = []
    flushes = []

    def _on_commit(_session):
        commits.append(1)

    def _on_flush(_session, _ctx, _instances):
        flushes.append(1)

    before = _row_counts(db)
    event.listen(db, "after_commit", _on_commit)
    event.listen(db, "before_flush", _on_flush)
    try:
        yield
    finally:
        event.remove(db, "after_commit", _on_commit)
        event.remove(db, "before_flush", _on_flush)

    assert commits == [], f"a read endpoint committed {len(commits)} time(s)"
    assert flushes == [], f"a read endpoint flushed pending writes {len(flushes)} time(s)"
    after = _row_counts(db)
    delta = {name: (before[name], after[name]) for name in before if before[name] != after[name]}
    assert delta == {}, f"a read endpoint changed row counts: {delta}"


# ===========================================================================
# 1. Cross-tenant leakage -- assert on the RAW BODY, not a count
# ===========================================================================


def test_capacity_forecast_body_contains_no_company_b_marker(client: TestClient, world: World):
    """WOULD FAIL PRE-FIX. ``forecast_capacity`` listed every tenant's active work centers
    by name, so company B's cell name was rendered verbatim into company A's default
    ``/analytics`` panel."""
    response = client.get(CAPACITY, headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert MARKER not in response.text, response.text

    names = {wc["work_center_name"] for week in response.json()["weeks"] for wc in week["work_centers"]}
    assert names == {"Alpha Cell One"}


def test_inventory_demand_body_contains_no_company_b_marker(client: TestClient, world: World):
    """WOULD FAIL PRE-FIX. The part set was selected with no scope at all -- foreign part
    numbers and names were rendered straight into ``predictions``."""
    response = client.get(DEMAND, headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert MARKER not in response.text, response.text

    numbers = {p["part_number"] for p in response.json()["predictions"]}
    assert numbers == {"ALPHA-RAW-1"}, "only company A's live purchased/raw parts"


def test_inventory_demand_omits_a_soft_deleted_part(client: TestClient, world: World):
    """Invariant 3. ``Part`` carries ``SoftDeleteMixin`` and the part set did not filter it,
    so retired parts were still being forecast and reordered."""
    response = client.get(DEMAND, headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert "ALPHA-RAW-RETIRED" not in response.text


def test_delivery_prediction_body_contains_no_company_b_marker(client: TestClient, world: World):
    """WOULD FAIL PRE-FIX. Even reading its OWN work order, company A saw company B's
    machine name -- ``op.work_center`` lazy-loaded off a plain, historically
    cross-tenant-writable FK, and B's mis-parented operations sat on A's cell."""
    response = client.get(f"{DELIVERY}/{world.a_wo.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert MARKER not in response.text, response.text

    body = response.json()
    assert body["work_order_number"] == "ALPHA-WO-0001"
    assert body["part_number"] == "ALPHA-ASM-1"
    assert [op["operation_name"] for op in body["operations"]] == ["Alpha Saw", "Alpha Mill"]
    assert body["bottleneck_work_center"] == "Alpha Cell One"


def test_a_foreign_operation_hanging_off_our_own_work_order_is_not_in_our_routing(client: TestClient, world: World):
    """``WorkOrderOperation.work_order_id`` is a plain FK, so filtering the routing read by
    work order alone is not ownership. A company-B operation attached to company A's own job
    would otherwise be rendered into A's process plan -- by name, with its machine and its
    hours -- and would shift every downstream operation's predicted dates."""
    response = client.get(f"{DELIVERY}/{world.a_wo.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert MARKER not in response.text, response.text
    assert [op["operation_id"] for op in response.json()["operations"]] == [world.a_op10.id, world.a_op20.id]
    assert world.b_op_on_a_wo.id not in [op["operation_id"] for op in response.json()["operations"]]


def test_a_foreign_part_id_on_our_own_work_order_renders_as_unknown(client: TestClient, world: World):
    """``WorkOrder.part_id`` is a plain FK and the part number is rendered into the response.
    Unscoped, a mis-parented part id discloses another tenant's part number on a work order
    the caller legitimately owns. "Unknown" is the correct refusal -- the caller may read its
    own job, not resolve a foreign identifier through it."""
    response = client.get(f"{DELIVERY}/{world.a_wo_foreign_refs.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["part_number"] == "Unknown"
    assert MARKER not in response.text, response.text


def test_a_foreign_work_center_id_on_our_own_operation_renders_as_unknown(client: TestClient, world: World):
    """Same shape on the other FK. ``_work_center_names`` exists precisely so this resolves
    through a scoped read instead of the ``op.work_center`` relationship; unscoped it puts
    another shop's machine name in ``work_center_name`` and ``bottleneck_work_center``."""
    response = client.get(f"{DELIVERY}/{world.a_wo_foreign_refs.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert [op["work_center_name"] for op in body["operations"]] == ["Unknown"]
    assert body["bottleneck_work_center"] in (None, "Unknown")
    assert MARKER not in response.text, response.text


def test_company_b_symmetrically_sees_only_its_own(client: TestClient, world: World):
    """The leak is not one-directional, and a fix that hard-coded company 1 would pass every
    test above. Company B must see B and only B."""
    response = client.get(CAPACITY, headers=headers_for(world.b_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    names = {wc["work_center_name"] for week in response.json()["weeks"] for wc in week["work_centers"]}
    assert names == {f"{MARKER} Mill Cell"}
    assert "Alpha Cell One" not in response.text

    demand = client.get(DEMAND, headers=headers_for(world.b_manager))
    assert demand.status_code == status.HTTP_200_OK, demand.text
    assert {p["part_number"] for p in demand.json()["predictions"]} == {f"{MARKER}-RAW-1", f"{MARKER}-BUY-1"}
    assert "ALPHA-" not in demand.text


# ===========================================================================
# 2. predict_delivery by foreign primary key -- the enumeration oracle
# ===========================================================================


def test_delivery_prediction_refuses_another_tenants_work_order_id(client: TestClient, world: World):
    """THE headline defect. Pre-fix this returned **200** with company B's work-order
    number, part number, due date and full routing. It is now a flat 404."""
    response = client.get(f"{DELIVERY}/{world.b_wo.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {"detail": "Work order not found"}
    # Nothing about company B may appear -- not its number, not its id, not its routing.
    assert MARKER not in response.text
    assert str(world.b_wo.id) not in response.text
    assert "operations" not in response.text


def test_a_foreign_id_is_indistinguishable_from_an_absent_one(client: TestClient, world: World):
    """The 404 must not be an existence oracle (#189). Pre-fix the two refusals were
    ``"Work order {id} not found"`` and ``"No operations found for work order {id}"``, so an
    op-less FOREIGN work order was still confirmed to exist -- and both echoed the id."""
    absent_id = 10_000_000
    foreign = client.get(f"{DELIVERY}/{world.b_wo.id}", headers=headers_for(world.a_manager))
    absent = client.get(f"{DELIVERY}/{absent_id}", headers=headers_for(world.a_manager))

    assert foreign.status_code == absent.status_code == status.HTTP_404_NOT_FOUND
    assert foreign.json() == absent.json(), "the two refusals must be byte-identical"
    assert str(absent_id) not in absent.text, "the refusal must not echo the identifier"


def test_a_foreign_work_order_with_no_routing_is_also_indistinguishable(
    client: TestClient, db_session: Session, world: World
):
    """The second half of the oracle. A foreign work order with NO operations used to fall
    through to the ``"No operations found"`` branch, confirming its existence. It must now
    fail at the ownership check first and return the same body as an absent id."""
    bare = make_wo(db_session, number=f"{MARKER}-WO-BARE", part=world.b_asm, company_id=COMPANY_B)

    foreign = client.get(f"{DELIVERY}/{bare.id}", headers=headers_for(world.a_manager))
    absent = client.get(f"{DELIVERY}/10000001", headers=headers_for(world.a_manager))

    assert foreign.status_code == status.HTTP_404_NOT_FOUND
    assert foreign.json() == absent.json()


def test_own_work_order_without_routing_still_gets_its_own_refusal(
    client: TestClient, db_session: Session, world: World
):
    """The counterweight: collapsing the oracle must not collapse a genuinely useful message
    for a work order the caller OWNS. That branch is now only reachable for own rows, so it
    stays distinct and discloses nothing."""
    bare = make_wo(db_session, number="ALPHA-WO-BARE", part=world.a_asm, company_id=COMPANY_A)

    response = client.get(f"{DELIVERY}/{bare.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {"detail": "Work order has no routing operations"}


def test_delivery_prediction_refuses_a_soft_deleted_own_work_order(client: TestClient, world: World):
    """Invariant 3: a deleted job is not a job to forecast."""
    response = client.get(f"{DELIVERY}/{world.a_wo_deleted.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {"detail": "Work order not found"}


# ===========================================================================
# 3. The numbers -- where the marker string cannot reach
# ===========================================================================


def test_queue_position_counts_only_this_tenants_live_queued_work(client: TestClient, world: World):
    """``_get_queue_depths`` groups by ``work_center_id`` and is surfaced verbatim as
    ``queue_position``. Four PENDING operations sit on company A's cell and only **2** are
    A's live queued work. No marker string appears in this field; only the number betrays
    the other three:

    * A's own two operations on its own open job -- the answer, 2.
    * A company-B operation mis-parented onto A's cell (excluded by the predicate on the
      OPERATION). Grouping by ``work_center_id`` is not transitive scoping (#191).
    * An operation A genuinely OWNS, hanging off company B's job (excluded only by the
      predicate on the JOINED header -- A owns the row, so the operation-level predicate
      admits it).
    * An operation on A's own SOFT-DELETED job (excluded only by ``WorkOrder.is_deleted``).

    Queue depth is not merely displayed: it multiplies into ``queue_wait_hours`` and so into
    every predicted date below it, which is why the count being right matters twice.
    """
    response = client.get(f"{DELIVERY}/{world.a_wo.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert [op["queue_position"] for op in response.json()["operations"]] == [2, 2]


def test_queue_depth_excludes_a_soft_deleted_work_orders_operations(
    client: TestClient, db_session: Session, world: World
):
    """Isolates the ``WorkOrder.is_deleted`` half of the join added to ``_get_queue_depths``.

    Toggling ONLY ``WorkOrder.is_deleted`` moves the number and nothing else does: restoring
    the job raises the depth by exactly one (so the operation is genuinely eligible queued
    work -- right status, right company, right cell -- and the predicate is what was
    excluding it), and re-deleting it puts the number back. Physically removing the row from
    the deleted job then leaves the depth UNCHANGED, because it was never being counted.
    """

    def depth() -> int:
        response = client.get(f"{DELIVERY}/{world.a_wo.id}", headers=headers_for(world.a_manager))
        assert response.status_code == status.HTTP_200_OK, response.text
        return response.json()["operations"][0]["queue_position"]

    assert depth() == 2

    dead_wo = db_session.query(WorkOrder).filter(WorkOrder.id == world.a_wo_deleted.id).one()
    dead_wo.is_deleted = False
    db_session.commit()
    assert depth() == 3, "restoring the job makes its queued op count -- so the row is otherwise eligible"

    dead_wo.is_deleted = True
    db_session.commit()
    assert depth() == 2, "and re-deleting it takes the op back out"

    dead_op = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == world.a_dead_op_pending.id).one()
    db_session.delete(dead_op)
    db_session.commit()
    assert depth() == 2, "removing it physically changes nothing -- it was already not counted"


def test_estimated_hours_are_not_steered_by_another_tenants_cycle_times(client: TestClient, world: World):
    """``_get_historical_cycle_times`` averages actual-vs-planned into ``avg_ratio`` and
    multiplies this caller's estimate by it. Company B's completed operation on A's cell ran
    10 h against a 2 h plan (ratio 5.0), so unscoped A's 2.0 h operation is quoted at 10.0 h.
    Grouping by ``work_center_id`` is NOT transitive scoping (#191)."""
    response = client.get(f"{DELIVERY}/{world.a_wo.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    hours = [op["estimated_hours"] for op in response.json()["operations"]]
    assert hours == pytest.approx([2.0, 4.0])  # 1.0 + 0.1*10  and  2.0 + 0.2*10


@pytest.mark.parametrize(
    "op_attr,label",
    [
        ("a_dead_op_done", "an operation on this tenant's own soft-deleted job"),
        ("a_op_on_b_wo_done", "an operation this tenant owns, hanging off another tenant's job"),
    ],
)
def test_cycle_times_ignore_history_hanging_off_a_job_that_is_not_open_and_ours(
    client: TestClient, db_session: Session, world: World, op_attr: str, label: str
):
    """The two halves of the join added to ``_get_historical_cycle_times``, isolated.

    Both rows are COMPLETE, inside the 90-day window, on company A's cell, and ran 10 h
    against a 2 h plan (ratio 5.0). Neither is excluded by the operation-level tenant
    predicate -- the first is A's own row, and so is the second. Only the predicate on the
    JOINED work-order header excludes them, one half each. Counted, either would multiply
    A's estimates by 5.0; the assertion is that the estimates stay at plan.

    The second half of each case re-points the row at A's own OPEN job and shows the ratio
    then DOES apply -- so the row is genuinely eligible history and it is the header
    predicate, not the cutoff or the status filter, doing the excluding.
    """

    def planned_hours(response) -> dict:
        return {op["operation_name"]: op["estimated_hours"] for op in response.json()["operations"]}

    at_plan = client.get(f"{DELIVERY}/{world.a_wo.id}", headers=headers_for(world.a_manager))
    assert at_plan.status_code == status.HTTP_200_OK, at_plan.text
    hours = planned_hours(at_plan)
    assert (hours["Alpha Saw"], hours["Alpha Mill"]) == pytest.approx((2.0, 4.0)), label

    row = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == getattr(world, op_attr).id).one()
    row.work_order_id = world.a_wo.id
    row.company_id = COMPANY_A
    db_session.commit()

    steered = planned_hours(client.get(f"{DELIVERY}/{world.a_wo.id}", headers=headers_for(world.a_manager)))
    assert (steered["Alpha Saw"], steered["Alpha Mill"]) == pytest.approx((10.0, 20.0)), label


def test_committed_hours_count_only_this_tenants_open_jobs(client: TestClient, world: World):
    """``forecast_capacity`` summed every tenant's open work orders into ``committed_hours``.
    A's own open job is 6.0 h; B's mis-parented operations on A's cell add 51.0 h + 1.0 h
    unscoped, and A's own soft-deleted job would add 1100 h more."""
    response = client.get(f"{CAPACITY}?weeks_ahead=4", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    week = response.json()["weeks"][0]
    assert len(week["work_centers"]) == 1
    row = week["work_centers"][0]
    assert row["committed_hours"] == pytest.approx(1.5)  # 6.0 h spread over 4 weeks
    assert row["available_hours"] == pytest.approx(40.0)  # 8 h/day * 5 * 1.0 efficiency
    assert row["utilization_pct"] == pytest.approx(3.8)
    assert week["total_committed"] == pytest.approx(1.5)
    assert week["total_available"] == pytest.approx(40.0)


def test_committed_hours_ignore_our_own_operation_hanging_off_a_foreign_job(client: TestClient, world: World):
    """The capacity forecast reads the open WORK-ORDER set first and the operations second,
    so the two predicates guard different things. This fixture holds an operation company A
    genuinely owns, attached to company B's 500-piece job: without the tenant predicate on
    the WORK ORDER, B's ``quantity_ordered`` is multiplied into A's committed hours through
    an operation A does own -- and no marker string is anywhere near the number."""
    response = client.get(f"{CAPACITY}?weeks_ahead=4", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["weeks"][0]["work_centers"][0]["committed_hours"] == pytest.approx(1.5)


def test_committed_hours_ignore_a_foreign_operation_hanging_off_our_own_job(client: TestClient, world: World):
    """And the mirror: company B's operation on company A's OPEN job passes the work-order
    filter (the job is A's), so only the predicate on the OPERATION excludes it."""
    response = client.get(f"{CAPACITY}?weeks_ahead=4", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    row = response.json()["weeks"][0]["work_centers"][0]
    # 9.0 setup + 0.9/pc * 10 = 18.0 h would be added, i.e. 24.0/4 = 6.0 committed.
    assert row["committed_hours"] == pytest.approx(1.5)
    assert row["utilization_pct"] == pytest.approx(3.8)


def test_on_hand_stock_counts_only_this_tenants_inventory(client: TestClient, world: World):
    """A company-B ``InventoryItem`` row points at company A's part (``part_id`` is a plain
    FK). Unscoped, A's on-hand reads 600 instead of 100 -- an inflated stock figure
    SUPPRESSES a stockout warning, so this leak is a safety defect as well as a privacy
    one."""
    response = client.get(DEMAND, headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    (row,) = [p for p in response.json()["predictions"] if p["part_number"] == "ALPHA-RAW-1"]
    assert row["current_stock"] == pytest.approx(100.0)


def test_daily_usage_counts_only_this_tenants_issues(client: TestClient, world: World):
    """``_calculate_daily_usage`` drives reorder points. #200 documented this exact gap in
    code and deferred it to this change. A issued 90 over the window (1.0/day); a
    company-B issue against A's part adds 900 more, so unscoped the rate reads 11.0/day and
    the shop re-buys material it does not need."""
    response = client.get(DEMAND, headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    (row,) = [p for p in response.json()["predictions"] if p["part_number"] == "ALPHA-RAW-1"]
    assert row["daily_usage_rate"] == pytest.approx(1.0)
    assert row["days_until_stockout"] == 100


def test_open_po_quantity_counts_only_this_tenants_supply(client: TestClient, world: World):
    """``_get_open_po_info`` was unscoped on BOTH the line and its header, and did not filter
    the header's soft delete. A's real open supply is 40 (50 ordered - 10 received) due in
    10 days. Unscoped it reads 40 + 7000 (company B) + 999 (A's own deleted PO) and pulls
    ``next_po_due`` forward to the deleted PO's date."""
    response = client.get(DEMAND, headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    (row,) = [p for p in response.json()["predictions"] if p["part_number"] == "ALPHA-RAW-1"]
    assert row["open_po_quantity"] == pytest.approx(40.0)
    assert row["next_po_due"] == (date.today() + timedelta(days=10)).isoformat()


def test_the_po_header_date_fallback_reads_the_eagerly_loaded_header(
    client: TestClient, db_session: Session, world: World
):
    """``_get_open_po_info`` ends ``line.required_date or line.purchase_order.expected_date``
    -- the only place the joined header is dereferenced. This PR replaced the implicit join
    with an explicit one plus ``contains_eager`` so that attribute reuses the FILTERED header
    instead of lazy-loading an unscoped one, and every other test here sets ``required_date``,
    so the fallback branch would otherwise never execute. A dedicated part keeps this
    isolated from the ``open_po_quantity == 40.0`` pins above."""
    lonely = make_part(
        db_session,
        part_number="ALPHA-RAW-NODATE",
        name="Alpha Undated Sheet",
        part_type="raw_material",
        company_id=COMPANY_A,
    )
    make_po_line(
        db_session,
        part=lonely,
        company_id=COMPANY_A,
        ordered=25.0,
        required_in_days=20,
        with_required_date=False,
    )

    response = client.get(DEMAND, headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    (row,) = [p for p in response.json()["predictions"] if p["part_number"] == "ALPHA-RAW-NODATE"]
    assert row["open_po_quantity"] == pytest.approx(25.0)
    # expected_date is required_in_days + 5 -- i.e. the value came from the HEADER, not the line
    assert row["next_po_due"] == (date.today() + timedelta(days=25)).isoformat()


def test_open_po_supply_excludes_both_misaligned_line_header_pairs(client: TestClient, world: World):
    """``_get_open_po_info`` carries the tenant predicate on the line AND on the joined
    header, and for an ALIGNED foreign pair either one alone would be enough. These two rows
    are the misaligned pairs -- a company-B line on a company-A PO (600) and a company-A line
    on a company-B PO (300) -- so each predicate is the only thing excluding one of them.
    Drop either and this caller's inbound supply is overstated, which suppresses a real
    stockout warning."""
    response = client.get(DEMAND, headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    (row,) = [p for p in response.json()["predictions"] if p["part_number"] == "ALPHA-RAW-1"]
    assert row["open_po_quantity"] == pytest.approx(40.0), "40 only -- neither 640, 340 nor 940"
    # Both misaligned rows are due sooner than A's real PO, so a leak also moves this date.
    assert row["next_po_due"] == (date.today() + timedelta(days=10)).isoformat()


# ===========================================================================
# 4. Single-tenant control -- the numbers must be UNCHANGED
# ===========================================================================


@pytest.fixture
def solo(db_session: Session) -> World:
    """Exactly the company-A half of ``world``, with no company B at all. Every figure
    asserted here is what a single-tenant install saw BEFORE the scoping change; the fix
    must be a no-op for them."""
    w = World()
    db = db_session
    w.manager = make_user(db, role=UserRole.MANAGER, company_id=COMPANY_A)
    w.wc = make_work_center(db, name="Solo Cell", company_id=COMPANY_A)
    w.raw = make_part(db, part_number="SOLO-RAW-1", name="Solo Sheet", part_type="raw_material")
    w.asm = make_part(db, part_number="SOLO-ASM-1", name="Solo Weldment")
    w.wo = make_wo(db, number="SOLO-WO-0001", part=w.asm, company_id=COMPANY_A, ordered=10)
    make_op(db, wo=w.wo, work_center=w.wc, company_id=COMPANY_A, name="Solo Saw", sequence=10)
    make_op(
        db,
        wo=w.wo,
        work_center=w.wc,
        company_id=COMPANY_A,
        name="Solo Mill",
        sequence=20,
        setup_time_hours=2.0,
        run_time_per_piece=0.2,
    )
    make_inventory(db, part=w.raw, company_id=COMPANY_A, quantity=100.0)
    make_issue(db, part=w.raw, company_id=COMPANY_A, quantity=90.0, user=w.manager)
    make_po_line(db, part=w.raw, company_id=COMPANY_A, ordered=50.0, received=10.0, required_in_days=10)
    return w


def test_single_tenant_capacity_is_unchanged(client: TestClient, solo: World):
    """PASSES BOTH BEFORE AND AFTER -- that is the point. If this drifts, the new predicates
    are filtering something they should not."""
    response = client.get(CAPACITY, headers=headers_for(solo.manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    week = response.json()["weeks"][0]
    (row,) = week["work_centers"]
    assert row["work_center_name"] == "Solo Cell"
    assert row["committed_hours"] == pytest.approx(1.5)
    assert row["available_hours"] == pytest.approx(40.0)
    assert row["utilization_pct"] == pytest.approx(3.8)
    assert row["is_overloaded"] is False
    assert response.json()["alerts"] == []
    assert len(response.json()["weeks"]) == 4


def test_single_tenant_delivery_prediction_is_unchanged(client: TestClient, solo: World):
    response = client.get(f"{DELIVERY}/{solo.wo.id}", headers=headers_for(solo.manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["work_order_number"] == "SOLO-WO-0001"
    assert body["part_number"] == "SOLO-ASM-1"
    assert body["quantity"] == pytest.approx(10.0)
    assert body["confidence"] == pytest.approx(0.5)
    assert body["on_time_probability"] == pytest.approx(0.95)
    assert body["bottleneck_work_center"] == "Solo Cell"
    assert [(op["operation_name"], op["queue_position"], op["estimated_hours"]) for op in body["operations"]] == [
        ("Solo Saw", 2, pytest.approx(2.0)),
        ("Solo Mill", 2, pytest.approx(4.0)),
    ]


def test_single_tenant_inventory_demand_is_unchanged(client: TestClient, solo: World):
    response = client.get(DEMAND, headers=headers_for(solo.manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    (row,) = body["predictions"]
    assert row["part_number"] == "SOLO-RAW-1"
    assert row["part_name"] == "Solo Sheet"
    assert row["current_stock"] == pytest.approx(100.0)
    assert row["daily_usage_rate"] == pytest.approx(1.0)
    assert row["days_until_stockout"] == 100
    assert row["open_po_quantity"] == pytest.approx(40.0)
    assert row["next_po_due"] == (date.today() + timedelta(days=10)).isoformat()
    assert row["urgency"] == "ok"
    assert body["critical_count"] == 0
    assert body["warning_count"] == 0


def test_a_work_center_with_no_work_still_reports_zero_not_absent(client: TestClient, db_session: Session, solo: World):
    """An idle machine must still appear at 0% -- the scoped ``WorkCenter`` read must not
    have become an inner join to work."""
    idle = make_work_center(db_session, name="Solo Idle Cell", company_id=COMPANY_A)

    response = client.get(CAPACITY, headers=headers_for(solo.manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    rows = {wc["work_center_name"]: wc for wc in response.json()["weeks"][0]["work_centers"]}
    assert set(rows) == {"Solo Cell", "Solo Idle Cell"}
    assert rows["Solo Idle Cell"]["committed_hours"] == pytest.approx(0.0)
    assert idle.is_active is True


# ===========================================================================
# 5. Invariant 2 -- a poll is not an actor
# ===========================================================================


@pytest.mark.parametrize("path", ["capacity", "demand", "delivery"])
def test_prediction_endpoints_write_nothing(client: TestClient, db_session: Session, world: World, path: str):
    """CLAUDE.md invariant 2 + the "a poll is not an actor" rule. #199 found two GETs that
    committed, so this is asserted rather than assumed: no commit, no flush, and no
    row-count delta in ANY table."""
    url = {"capacity": CAPACITY, "demand": DEMAND, "delivery": f"{DELIVERY}/{world.a_wo.id}"}[path]
    headers = headers_for(world.a_manager)

    with asserts_nothing_was_written(db_session):
        response = client.get(url, headers=headers)

    assert response.status_code == status.HTTP_200_OK, response.text


def test_a_refused_delivery_prediction_writes_nothing_either(client: TestClient, db_session: Session, world: World):
    """The refusal path too: a cross-tenant probe must not leave a row behind (and, notably,
    must not write an audit row -- ``AuditService`` is for state changes, and a refused read
    is not one)."""
    before_audit = db_session.query(AuditLog).count()

    with asserts_nothing_was_written(db_session):
        response = client.get(f"{DELIVERY}/{world.b_wo.id}", headers=headers_for(world.a_manager))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert db_session.query(AuditLog).count() == before_audit


# ===========================================================================
# 6. Role gating -- must match the rest of the analytics router
# ===========================================================================


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
@pytest.mark.parametrize("path", ["capacity", "demand", "delivery"])
def test_the_three_office_roles_are_admitted(
    client: TestClient, db_session: Session, world: World, role: UserRole, path: str
):
    """``require_role([ADMIN, MANAGER, SUPERVISOR])`` -- the same triple used by
    ``/kpis``, ``/oee``, ``/production-trends``, ``/flow``, ``/wip-aging``, ``/adoption``
    and ``/inventory-turnover``. Unchanged by this PR; pinned so it cannot drift."""
    user = make_user(db_session, role=role, company_id=COMPANY_A)
    url = {"capacity": CAPACITY, "demand": DEMAND, "delivery": f"{DELIVERY}/{world.a_wo.id}"}[path]

    assert client.get(url, headers=headers_for(user)).status_code == status.HTTP_200_OK


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER])
@pytest.mark.parametrize("path", ["capacity", "demand", "delivery"])
def test_the_shop_floor_roles_are_refused(
    client: TestClient, db_session: Session, world: World, role: UserRole, path: str
):
    """403 BEFORE any tenant scoping is even reached -- role gating is the outer fence."""
    user = make_user(db_session, role=role, company_id=COMPANY_A)
    url = {"capacity": CAPACITY, "demand": DEMAND, "delivery": f"{DELIVERY}/{world.a_wo.id}"}[path]

    assert client.get(url, headers=headers_for(user)).status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("path", ["capacity", "demand", "delivery"])
def test_anonymous_callers_are_refused(client: TestClient, world: World, path: str):
    url = {"capacity": CAPACITY, "demand": DEMAND, "delivery": f"{DELIVERY}/{world.a_wo.id}"}[path]

    assert client.get(url).status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)


# ===========================================================================
# 7. The ACTIVE company, not the user's home company
# ===========================================================================


def test_a_platform_admin_sees_the_company_it_switched_into(client: TestClient, db_session: Session, world: World):
    """The scope must come from ``get_current_company_id`` (the JWT's active company), never
    ``current_user.company_id``. A platform admin homed in company A but viewing company B
    must see B's machines -- a ``current_user.company_id`` implementation would silently
    show A's and would pass every other test in this file."""
    admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=COMPANY_A)

    response = client.get(CAPACITY, headers=headers_for(admin, active_company_id=COMPANY_B))

    assert response.status_code == status.HTTP_200_OK, response.text
    names = {wc["work_center_name"] for week in response.json()["weeks"] for wc in week["work_centers"]}
    assert names == {f"{MARKER} Mill Cell"}
    assert "Alpha Cell One" not in response.text


def test_a_platform_admin_in_its_home_context_sees_its_home_company(
    client: TestClient, db_session: Session, world: World
):
    """The other half: with no switch, the active company IS the home company."""
    admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=COMPANY_A)

    response = client.get(CAPACITY, headers=headers_for(admin))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert MARKER not in response.text


# ===========================================================================
# 8. The constructor is the fence
# ===========================================================================


def test_the_service_cannot_be_constructed_without_a_company(db_session: Session):
    """Constructor injection is the structural half of this fix: an unscoped construction is
    now a ``TypeError`` at the call site rather than a silent platform-wide read. That is
    what stops the next helper added to this module from repeating the defect."""
    with pytest.raises(TypeError):
        PredictionService(db_session)  # type: ignore[call-arg]


def test_every_public_method_is_scoped_by_the_constructor_argument(db_session: Session, world: World):
    """Belt-and-braces at the unit level, below the HTTP layer: the same service instance,
    pointed at each company in turn, must return disjoint answers."""
    a = PredictionService(db_session, COMPANY_A)
    b = PredictionService(db_session, COMPANY_B)

    assert {wc.work_center_name for wc in a.forecast_capacity(1).weeks[0].work_centers} == {"Alpha Cell One"}
    assert {wc.work_center_name for wc in b.forecast_capacity(1).weeks[0].work_centers} == {f"{MARKER} Mill Cell"}

    assert {p.part_number for p in a.predict_inventory_demand().predictions} == {"ALPHA-RAW-1"}
    assert {p.part_number for p in b.predict_inventory_demand().predictions} == {
        f"{MARKER}-RAW-1",
        f"{MARKER}-BUY-1",
    }

    assert a.predict_delivery(world.a_wo.id).work_order_number == "ALPHA-WO-0001"
    with pytest.raises(ValueError):
        a.predict_delivery(world.b_wo.id)
    with pytest.raises(ValueError):
        b.predict_delivery(world.a_wo.id)
