"""Seed data for the export-control (AI-external API token) tests -- Claude-plugin brief P0.3.

``seed_world`` builds, in one company, an ITAR customer whose every record carries the
CANARY string in each text field it has (part number, description, customer, notes,
instructions, specs ...), a cleared customer whose records carry none, and the few records
the ERP cannot tie to any customer. A test then proves the canary never leaves the API in
an AI-external token's response bytes, while a person's session still sees it.

Identifiers the redaction KEEPS (work order / NCR / CAR / FAI / quote / PO / receipt / lot
numbers, operation numbers, work-center names and codes) deliberately do not carry the
canary: they are business identifiers the owner allowed through.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.downtime import DowntimeEvent
from app.models.governance import CustomerContract
from app.models.maintenance import MaintenanceWorkOrder
from app.models.oee import OEERecord
from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias, normalize_alias_key
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quality import CorrectiveActionRequest, FAICharacteristic, FirstArticleInspection, NonConformanceReport
from app.models.quote import Quote, QuoteLine
from app.models.routing import Routing, RoutingOperation
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.company_time import company_today
from tests.api.kiosk_test_helpers import ensure_company

CANARY = "ITARCANARY"
API_TOKENS_URL = "/api/v1/api-tokens/"

ITAR_CUSTOMER = f"{CANARY} Defense Systems"
ITAR_CUSTOMER_CODE = f"{CANARY}-CODE"
CLEAR_CUSTOMER = "Open Commerce Co"
CLEAR_CUSTOMER_CODE = "OPENCO"
CLEAR_TEXT = "OPENTEXT"  # appears in the cleared customer's records; must survive in full
# Dimensional / measured values on the ITAR FAI and NCR: NUMERIC strings, which a text
# canary cannot catch. Exponent form so no timestamp or id can contain the marker by chance.
# (Compliance review P0, finding 1: a numeric string under a non-text key used to pass.)
NUMERIC_CANARIES = (b"329150e", b"329151e")
NOMINAL_CANARY = "7.329150e0"
MEASURED_CANARY = "7.329151e0"
TOLERANCE_CANARY = "0.329150e-2"


def bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def issue_token(client: TestClient, admin_headers: dict, user: User, **extra) -> Tuple[int, str]:
    response = client.post(
        API_TOKENS_URL,
        json={"user_id": user.id, "label": f"assistant for {user.email}", **extra},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], response.json()["token"]


def make_viewer(db: Session, *, company_id: int = 1, email: str = "werco-assistant@werco.com") -> User:
    user = User(
        email=email,
        employee_id=f"EMP-{email.split('@')[0][:20]}",
        first_name="Werco",
        last_name="Assistant",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.VIEWER,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@dataclass
class World:
    company_id: int
    reporter: User
    work_center: WorkCenter
    itar_customer: Customer
    clear_customer: Customer
    contract: CustomerContract
    itar_part: Part
    clear_part: Part
    material_part: Part
    itar_routing: Routing
    clear_routing: Routing
    itar_wo: WorkOrder
    itar_op: WorkOrderOperation
    clear_wo: WorkOrder
    clear_op: WorkOrderOperation
    laser_wo: WorkOrder
    itar_time_entry: TimeEntry
    itar_downtime: DowntimeEvent
    clear_downtime: DowntimeEvent
    itar_ncr: NonConformanceReport
    clear_ncr: NonConformanceReport
    orphan_ncr: NonConformanceReport
    itar_car: CorrectiveActionRequest
    orphan_car: CorrectiveActionRequest
    itar_fai: FirstArticleInspection
    clear_fai: FirstArticleInspection
    itar_quote: Quote
    clear_quote: Quote
    vendor: Vendor
    itar_po: PurchaseOrder
    clear_po: PurchaseOrder
    itar_receipt: POReceipt
    clear_receipt: POReceipt
    oee_record: OEERecord
    maintenance_wo: MaintenanceWorkOrder


def _canary(label: str) -> str:
    return f"{CANARY} {label}"


def seed_world(db: Session, *, company_id: int = 1, controlled: bool = True, prefix: str = "") -> World:
    """Seed the whole fixture world in ``company_id`` (see the module docstring).

    ``controlled=False`` records the ITAR customer's contract with no export-control flag,
    so the same customer is an ordinary cleared one (the control case).
    """
    p = prefix
    ensure_company(db, company_id)
    # The company's own calendar day, the one the routes judge dates by (overdue, due-soon,
    # re-evaluation). A bare date.today() is the CI runner's UTC date, which is already
    # tomorrow from 6pm Central, so a canary seeded "due yesterday" stopped being overdue
    # every evening and its case read as vacuous.
    today = company_today(db, company_id)
    now = datetime.utcnow().replace(microsecond=0)

    reporter = User(
        email=f"{p}reporter-{company_id}@werco.com",
        employee_id=f"{p}EMP-REP-{company_id}",
        first_name="Quality",
        last_name="Reporter",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.QUALITY,
        is_active=True,
        company_id=company_id,
    )
    db.add(reporter)
    work_center = WorkCenter(
        name=f"{p}Brake Press",
        code=f"{p}BRK-1",
        work_center_type="press_brake",
        hourly_rate=90.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(work_center)
    itar_customer = Customer(name=ITAR_CUSTOMER, code=ITAR_CUSTOMER_CODE, company_id=company_id, notes=_canary("cust"))
    clear_customer = Customer(name=CLEAR_CUSTOMER, code=CLEAR_CUSTOMER_CODE, company_id=company_id)
    db.add_all([itar_customer, clear_customer])
    db.flush()

    contract = CustomerContract(
        company_id=company_id,
        customer_id=itar_customer.id,
        contract_number=f"{p}CTR-ITAR-1",
        contract_name="Program contract",
        itar_controlled=controlled,
        default_data_classification="itar_export_controlled" if controlled else "internal",
        # Expired and inactive on purpose: ITAR obligations outlive the contract (owner, 2026-11-13).
        active=False,
        expiration_date=now - timedelta(days=400),
    )
    db.add(contract)

    itar_part = Part(
        part_number=f"{CANARY}-PN-{p}001",
        name=_canary("bracket"),
        description=_canary("description of the controlled bracket"),
        part_type="manufactured",
        customer_name=ITAR_CUSTOMER,
        customer_part_number=f"{CANARY}-CPN-{p}1",
        drawing_number=f"{CANARY}-DWG-{p}1",
        company_id=company_id,
    )
    clear_part = Part(
        part_number=f"{p}OPEN-PN-001",
        name=f"{CLEAR_TEXT} bracket",
        description=f"{CLEAR_TEXT} description",
        part_type="manufactured",
        customer_name=CLEAR_CUSTOMER,
        company_id=company_id,
    )
    material_part = Part(
        part_number=f"{p}SHEET-6061-125",
        name="6061-T6 sheet 0.125",
        part_type="raw_material",
        company_id=company_id,
    )
    db.add_all([itar_part, clear_part, material_part])
    db.flush()
    alias_number = f"{CANARY}-ALIAS-{p}1"
    db.add(
        PartNumberAlias(
            part_id=itar_part.id,
            alias_number=alias_number,
            alias_number_key=normalize_alias_key(alias_number),
            reason="legacy number",
            company_id=company_id,
        )
    )

    itar_routing = Routing(part_id=itar_part.id, description=_canary("routing"), company_id=company_id)
    clear_routing = Routing(part_id=clear_part.id, description=f"{CLEAR_TEXT} routing", company_id=company_id)
    db.add_all([itar_routing, clear_routing])
    db.flush()
    db.add_all(
        [
            RoutingOperation(
                routing_id=itar_routing.id,
                sequence=10,
                operation_number="10",
                name=_canary("form"),
                description=_canary("routing op"),
                setup_instructions=_canary("setup"),
                work_instructions=_canary("work"),
                work_center_id=work_center.id,
                company_id=company_id,
            ),
            RoutingOperation(
                routing_id=clear_routing.id,
                sequence=10,
                operation_number="10",
                name="Form",
                setup_instructions=f"{CLEAR_TEXT} setup",
                work_center_id=work_center.id,
                company_id=company_id,
            ),
        ]
    )

    itar_wo = WorkOrder(
        work_order_number=f"{p}WO-ITAR-0001",
        part_id=itar_part.id,
        quantity_ordered=7,
        status=WorkOrderStatus.RELEASED,
        priority=2,
        due_date=today + timedelta(days=10),
        customer_name=ITAR_CUSTOMER,
        customer_po=f"{CANARY}-CPO",
        notes=_canary("work order notes"),
        special_instructions=_canary("special instructions"),
        company_id=company_id,
    )
    clear_wo = WorkOrder(
        work_order_number=f"{p}WO-OPEN-0001",
        part_id=clear_part.id,
        quantity_ordered=5,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        due_date=today + timedelta(days=12),
        customer_name=CLEAR_CUSTOMER,
        notes=f"{CLEAR_TEXT} notes",
        company_id=company_id,
    )
    # Standalone laser work order: no part, no customer -- the ERP cannot tie it to anyone.
    laser_wo = WorkOrder(
        work_order_number=f"{p}WO-LASER-0001",
        part_id=None,
        work_order_type="laser_cutting",
        quantity_ordered=3,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        notes="LASERTEXT nest notes",
        company_id=company_id,
    )
    db.add_all([itar_wo, clear_wo, laser_wo])
    db.flush()
    itar_op = WorkOrderOperation(
        work_order_id=itar_wo.id,
        work_center_id=work_center.id,
        sequence=10,
        operation_number="10",
        name=_canary("brake op"),
        description=_canary("operation description"),
        setup_instructions=_canary("op setup"),
        run_instructions=_canary("op run"),
        status=OperationStatus.READY,
        company_id=company_id,
    )
    clear_op = WorkOrderOperation(
        work_order_id=clear_wo.id,
        work_center_id=work_center.id,
        sequence=10,
        operation_number="10",
        name="Brake",
        run_instructions=f"{CLEAR_TEXT} run",
        status=OperationStatus.READY,
        company_id=company_id,
    )
    db.add_all([itar_op, clear_op])
    db.flush()

    itar_time_entry = TimeEntry(
        user_id=reporter.id,
        work_order_id=itar_wo.id,
        operation_id=itar_op.id,
        work_center_id=work_center.id,
        clock_in=now - timedelta(hours=3),
        clock_out=now - timedelta(hours=1),
        duration_hours=2.0,
        quantity_produced=2,
        notes=_canary("labor notes"),
        company_id=company_id,
    )
    db.add(itar_time_entry)
    itar_downtime = DowntimeEvent(
        work_center_id=work_center.id,
        work_order_id=itar_wo.id,
        start_time=now - timedelta(hours=2),
        end_time=now - timedelta(hours=1),
        duration_minutes=60,
        category="mechanical",
        reason_code="JAM",
        description=_canary("downtime description"),
        resolution=_canary("downtime resolution"),
        reported_by=reporter.id,
        company_id=company_id,
    )
    clear_downtime = DowntimeEvent(
        work_center_id=work_center.id,
        work_order_id=clear_wo.id,
        start_time=now - timedelta(hours=5),
        end_time=now - timedelta(hours=4),
        duration_minutes=60,
        category="mechanical",
        description=f"{CLEAR_TEXT} downtime",
        reported_by=reporter.id,
        company_id=company_id,
    )
    db.add_all([itar_downtime, clear_downtime])

    itar_car = CorrectiveActionRequest(
        car_number=f"{p}CAR-0001",
        title=_canary("car title"),
        problem_description=_canary("car problem"),
        root_cause=_canary("car root cause"),
        due_date=today + timedelta(days=30),
        # The CAR form's fields (migration 122): every text one carries the canary, the cost
        # buckets are money and must stay true inside the withheld CAR.
        initiation_reason="other",
        initiation_reason_other=_canary("car reason other"),
        rma_number=_canary("RMA"),
        vendor_po_number=_canary("vendor PO"),
        management_action=_canary("management action"),
        vendor_approval_name=_canary("vendor approver"),
        vendor_approval_title=_canary("vendor title"),
        customer_approval_name=_canary("customer approver"),
        cost_labor=Decimal("125.50"),
        cost_material=Decimal("40.00"),
        company_id=company_id,
    )
    # Referenced by no NCR and no complaint: cannot be tied to any customer.
    orphan_car = CorrectiveActionRequest(
        car_number=f"{p}CAR-0002",
        title="ORPHANTEXT car title",
        problem_description="ORPHANTEXT problem",
        company_id=company_id,
    )
    db.add_all([itar_car, orphan_car])
    db.flush()
    itar_ncr = NonConformanceReport(
        ncr_number=f"{p}NCR-0001",
        part_id=itar_part.id,
        work_order_id=itar_wo.id,
        quantity_affected=2,
        quantity_rejected=1,
        source="in_process",
        title=_canary("ncr title"),
        description=_canary("ncr description"),
        specification=_canary("spec"),
        actual_value=MEASURED_CANARY,
        required_value=NOMINAL_CANARY,
        detected_date=today,
        car_id=itar_car.id,
        company_id=company_id,
    )
    clear_ncr = NonConformanceReport(
        ncr_number=f"{p}NCR-0002",
        part_id=clear_part.id,
        work_order_id=clear_wo.id,
        quantity_affected=1,
        source="in_process",
        title=f"{CLEAR_TEXT} ncr title",
        description=f"{CLEAR_TEXT} ncr description",
        detected_date=today,
        company_id=company_id,
    )
    # An internal-audit NCR naming no part, work order or receipt: unlinkable.
    orphan_ncr = NonConformanceReport(
        ncr_number=f"{p}NCR-0003",
        source="internal_audit",
        title="ORPHANTEXT audit finding",
        description="ORPHANTEXT description",
        detected_date=today,
        company_id=company_id,
    )
    db.add_all([itar_ncr, clear_ncr, orphan_ncr])

    itar_fai = FirstArticleInspection(
        fai_number=f"{p}FAI-0001",
        part_id=itar_part.id,
        part_revision=f"{CANARY}-R",
        work_order_id=itar_wo.id,
        notes=_canary("fai notes"),
        due_date=today + timedelta(days=5),
        company_id=company_id,
    )
    clear_fai = FirstArticleInspection(
        fai_number=f"{p}FAI-0002",
        part_id=clear_part.id,
        work_order_id=clear_wo.id,
        notes=f"{CLEAR_TEXT} fai notes",
        company_id=company_id,
    )
    db.add_all([itar_fai, clear_fai])
    db.flush()
    db.add(
        FAICharacteristic(
            fai_id=itar_fai.id,
            char_number=1,
            characteristic=_canary("hole diameter"),
            nominal=NOMINAL_CANARY,
            tolerance_plus=TOLERANCE_CANARY,
            tolerance_minus=TOLERANCE_CANARY,
            specification=_canary("char spec"),
            actual_value=MEASURED_CANARY,
            company_id=company_id,
        )
    )

    itar_quote = Quote(
        quote_number=f"{p}Q-{company_id}-0001",
        customer_name=ITAR_CUSTOMER,
        customer_contact=_canary("contact"),
        notes=_canary("quote notes"),
        internal_notes=_canary("internal"),
        quote_date=today,
        valid_until=today + timedelta(days=30),
        total=1234.5,
        company_id=company_id,
    )
    clear_quote = Quote(
        quote_number=f"{p}Q-{company_id}-0002",
        customer_name=CLEAR_CUSTOMER,
        notes=f"{CLEAR_TEXT} quote notes",
        quote_date=today,
        total=99.0,
        company_id=company_id,
    )
    db.add_all([itar_quote, clear_quote])
    db.flush()
    db.add_all(
        [
            QuoteLine(
                quote_id=itar_quote.id,
                line_number=1,
                part_id=itar_part.id,
                part_revision=_canary("rev"),  # the drawing revision quoted (P2.4)
                description=_canary("quote line"),
                quantity=7,
                unit_price=176.36,
                company_id=company_id,
            ),
            QuoteLine(
                quote_id=clear_quote.id,
                line_number=1,
                part_id=clear_part.id,
                description=f"{CLEAR_TEXT} quote line",
                quantity=5,
                unit_price=19.8,
                company_id=company_id,
            ),
        ]
    )

    vendor = Vendor(code=f"{p}VEND-1", name="Plain Plating Inc", is_approved=True, company_id=company_id)
    db.add(vendor)
    db.flush()
    itar_po = PurchaseOrder(
        po_number=f"{p}PO-0001",
        vendor_id=vendor.id,
        notes=_canary("po notes"),
        order_date=today,
        company_id=company_id,
    )
    clear_po = PurchaseOrder(
        po_number=f"{p}PO-0002",
        vendor_id=vendor.id,
        notes=f"{CLEAR_TEXT} po notes",
        order_date=today,
        company_id=company_id,
    )
    db.add_all([itar_po, clear_po])
    db.flush()
    itar_line = PurchaseOrderLine(
        purchase_order_id=itar_po.id,
        line_number=1,
        part_id=itar_part.id,
        quantity_ordered=7,
        quantity_received=7,
        unit_price=12.0,
        notes=_canary("outside processing"),
        company_id=company_id,
    )
    clear_line = PurchaseOrderLine(
        purchase_order_id=clear_po.id,
        line_number=1,
        part_id=clear_part.id,
        quantity_ordered=5,
        quantity_received=5,
        unit_price=3.0,
        company_id=company_id,
    )
    db.add_all([itar_line, clear_line])
    db.flush()
    itar_receipt = POReceipt(
        receipt_number=f"{p}RCV-0001",
        po_line_id=itar_line.id,
        quantity_received=7,
        quantity_accepted=7,
        lot_number=f"{p}LOT-0001",
        heat_number=_canary("heat"),
        inspection_notes=_canary("inspection notes"),
        notes=_canary("receipt notes"),
        received_by=reporter.id,
        received_at=now - timedelta(days=1),
        company_id=company_id,
    )
    clear_receipt = POReceipt(
        receipt_number=f"{p}RCV-0002",
        po_line_id=clear_line.id,
        quantity_received=5,
        quantity_accepted=5,
        lot_number=f"{p}LOT-0002",
        notes=f"{CLEAR_TEXT} receipt notes",
        received_by=reporter.id,
        received_at=now - timedelta(days=1),
        company_id=company_id,
    )
    db.add_all([itar_receipt, clear_receipt])

    # Not customer data, but free text a person typed: it names the controlled part number.
    oee_record = OEERecord(
        work_center_id=work_center.id,
        record_date=today,
        planned_production_time_minutes=480,
        actual_run_time_minutes=400,
        total_parts=10,
        good_parts=9,
        notes=f"ran {itar_part.part_number} all shift",
        company_id=company_id,
    )
    maintenance_wo = MaintenanceWorkOrder(
        work_center_id=work_center.id,
        wo_number=f"{p}MWO-0001",
        title="Replace hydraulic seal",
        description=f"found while running {itar_part.part_number}",
        scheduled_date=today,
        due_date=today - timedelta(days=1),
        company_id=company_id,
    )
    db.add_all([oee_record, maintenance_wo])
    db.commit()
    for row in (
        reporter,
        work_center,
        itar_customer,
        clear_customer,
        contract,
        itar_part,
        clear_part,
        material_part,
        itar_routing,
        clear_routing,
        itar_wo,
        itar_op,
        clear_wo,
        clear_op,
        laser_wo,
        itar_time_entry,
        itar_downtime,
        clear_downtime,
        itar_ncr,
        clear_ncr,
        orphan_ncr,
        itar_car,
        orphan_car,
        itar_fai,
        clear_fai,
        itar_quote,
        clear_quote,
        vendor,
        itar_po,
        clear_po,
        itar_receipt,
        clear_receipt,
        oee_record,
        maintenance_wo,
    ):
        db.refresh(row)
    return World(
        company_id=company_id,
        reporter=reporter,
        work_center=work_center,
        itar_customer=itar_customer,
        clear_customer=clear_customer,
        contract=contract,
        itar_part=itar_part,
        clear_part=clear_part,
        material_part=material_part,
        itar_routing=itar_routing,
        clear_routing=clear_routing,
        itar_wo=itar_wo,
        itar_op=itar_op,
        clear_wo=clear_wo,
        clear_op=clear_op,
        laser_wo=laser_wo,
        itar_time_entry=itar_time_entry,
        itar_downtime=itar_downtime,
        clear_downtime=clear_downtime,
        itar_ncr=itar_ncr,
        clear_ncr=clear_ncr,
        orphan_ncr=orphan_ncr,
        itar_car=itar_car,
        orphan_car=orphan_car,
        itar_fai=itar_fai,
        clear_fai=clear_fai,
        itar_quote=itar_quote,
        clear_quote=clear_quote,
        vendor=vendor,
        itar_po=itar_po,
        clear_po=clear_po,
        itar_receipt=itar_receipt,
        clear_receipt=clear_receipt,
        oee_record=oee_record,
        maintenance_wo=maintenance_wo,
    )


def contains_canary(raw: bytes, canary: str = CANARY) -> bool:
    return canary.lower().encode() in raw.lower()


def contains_numeric_canary(raw: bytes) -> bool:
    return any(marker in raw.lower() for marker in NUMERIC_CANARIES)


# ---------------------------------------------------------------------------
# Absence checks over raw response bytes vs. the clock
# ---------------------------------------------------------------------------
# A check that a literal is ABSENT from a serialized response is only sound when nothing
# else in that response can contain the literal by chance. Every response here carries
# server-clock instants ("2026-11-24T00:51:37.940123Z"), whose digits change on every run.
# On 2026-11-24 the needle b"7.94" matched such an instant (seconds 37, microseconds 94....)
# and failed a redaction test about one run in a thousand although nothing had leaked.
# The two helpers below make that class of chance match impossible rather than unlikely.

# Every digit slot of an ISO-8601 instant with microseconds, and a real instant to fill
# the slots a needle does not cover. A shorter fraction, a fraction-less instant, a space
# separator (``str(datetime)``) or a ``Z`` / offset suffix only removes or renames
# positions a numeric needle could use, so this one template covers them all.
_ISO_TEMPLATE = "dddd-dd-ddTdd:dd:dd.dddddd"
_ISO_BASE = "2026-11-24T15:15:22.227386"
# Month, day, hour, minute, second: two-digit fields with a range a free digit must respect.
_ISO_RANGED_FIELDS = ((5, 7), (8, 10), (11, 13), (14, 16), (17, 19))
_ISO_INSTANT = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?!\d)(?:Z|[+-]\d{2}:\d{2})?")


def iso_timestamp_containing(needle: bytes) -> Optional[str]:
    """A real ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` instant whose text contains ``needle``, or None.

    Constructive: it lays the needle over every offset of the template, fills the digits it
    does not cover (trying all ten values for a ranged field the needle only half covers)
    and returns the first string ``datetime.fromisoformat`` accepts. None therefore means
    no instant of this shape, at any date or time, can contain the needle.
    """
    text = needle.decode("ascii")
    for start in range(len(_ISO_TEMPLATE) - len(text) + 1):
        slots = _ISO_TEMPLATE[start : start + len(text)]
        if not all(ch.isdigit() if slot == "d" else ch == slot for slot, ch in zip(slots, text)):
            continue
        covered = set(range(start, start + len(text)))
        overlaid = _ISO_BASE[:start] + text + _ISO_BASE[start + len(text) :]
        free = [
            pos
            for lo, hi in _ISO_RANGED_FIELDS
            if covered & set(range(lo, hi))
            for pos in range(lo, hi)
            if pos not in covered
        ]
        for digits in itertools.product("0123456789", repeat=len(free)):
            candidate = list(overlaid)
            for pos, digit in zip(free, digits):
                candidate[pos] = digit
            instant = "".join(candidate)
            try:
                datetime.fromisoformat(instant)
            except ValueError:
                continue
            return instant + "Z"
    return None


def could_occur_in_iso_timestamp(needle: bytes) -> bool:
    """True when some real ISO-8601 instant with microseconds contains ``needle``.

    Assert this is False for every needle of an absence check over raw response bytes:
    a needle that fails it would match a timestamp by chance on some run. Seven or more
    fractional digits, or three or more integer digits before a ``.``, can never fit.
    """
    return iso_timestamp_containing(needle) is not None


def strip_iso_instants(text: str) -> str:
    """``text`` with every whole ISO-8601 instant removed, for an absence check whose needle
    cannot be made timestamp-proof (a six-digit PIN is exactly a microsecond field's shape).

    Only complete instants go: a secret written next to one, or a fraction longer than six
    digits (which no clock writes), stays in the text and is still caught.
    """
    return _ISO_INSTANT.sub("<instant>", text)


def find_record(payload: Any, *, key: str, value: Any) -> Optional[Dict[str, Any]]:
    """The first object anywhere in ``payload`` whose ``key`` equals ``value``."""
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if current.get(key) == value:
                return current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return None
