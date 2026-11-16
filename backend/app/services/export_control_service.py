"""Export control for AI-external API tokens (Claude-plugin brief P0.3), and the customer-contract writes behind it.

Three jobs, one module:

1. :func:`build_controlled_index` -- which of a company's records an AI-external token
   must not see in full, and why. The owner's rules (2026-11-13), applied per request with
   no cache:

   * A customer is CONTROLLED when ANY ``customer_contracts`` row of the company for it --
     any ``active``, any dates: ITAR obligations outlive the contract -- is
     ``itar_controlled`` / ``export_controlled`` / ``contains_cui`` or classified
     ``itar_export_controlled`` / ``cui``, or any ``customer_handling_instructions`` row
     (via ``customer_id``, or via ``contract_id`` -> the contract's customer) is classified
     so. Soft-deleted customers still count: their historical records stay controlled.
   * POSITIVE CLEARANCE, fail closed. A record linked to a customer only by free-text
     ``customer_name`` is served in full ONLY when that name (casefolded, stripped,
     whitespace collapsed) is the name or code of a known customer row -- deleted ones
     included -- with no controlling contract. A blank, unresolvable or controlled name is
     withheld, and so is any name or code a controlled customer has EVER carried (its audit
     history, :func:`customer_former_names`): a rename frees nothing, even when another
     customer later takes the old name. Records the ERP cannot tie to any customer
     (standalone laser work orders, NCRs with no part / work order / receipt, CARs with no
     part or customer of their own that no NCR or complaint references,
     raw-material purchase orders and receipts, engineering parts with no customer) are
     withheld as UNLINKABLE. A material-supply part (purchased / raw material / hardware /
     consumable) with a blank customer is generic stock and passes.
   * The linkage map (docs/EXPORT_CONTROL_AI_EGRESS.md): work orders via their name, part,
     component parts and parent; operations, time entries and downtime via the work order;
     routings via the part; FAI via part and work order; NCRs via part, work order and
     receipt; CARs via their own part, customer and customer-name snapshot AND the NCRs and
     complaints that reference them; quotes via name, work
     order, line parts and the fabrication quote's customer; purchase orders and receipts
     via their line parts.

2. :func:`apply_export_control` / :func:`record_refusal` -- what
   ``app.middleware.export_control`` runs (in a worker thread, in its own short-lived
   session): build the index, redact (``export_control_redaction``), and write the
   ``controlled_access_events`` ledger -- one row per withheld record per request, or one
   per refusal -- BEFORE a byte is sent. Any failure propagates, and the middleware answers
   503 with nothing sent.

3. The customer-contract write path -- the only writer of ``customer_contracts`` in the
   app, ADMIN-only at the route, required audit (``log_required``) in the same transaction.
   There is no delete: un-marking a customer is a ``PUT`` that clears the flags, with a reason.

Every query is tenant-scoped (``tenant_filter`` on column-only queries). Soft-deleted rows
are read DELIBERATELY throughout the index (invariant 3's historical-record exception): a
deleted customer's contracts still control its records, and a response can still name a
deleted record, which must therefore still be classifiable.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.core.export_control import (
    ACTION_LOOKUP_MISS,
    ACTION_REDACTED,
    ACTION_REFUSED,
    AI_EXTERNAL_MASKED_LOOKUPS,
    CLASSIFICATION_CUI,
    CLASSIFICATION_ITAR,
    CLASSIFICATION_UNKNOWN,
    REASON_EXPORT_CONTROLLED_CUSTOMER,
    REASON_NOT_FOUND,
    ExportControlMarker,
)
from app.db import database as _database
from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.customer_complaint import CustomerComplaint
from app.models.fabrication_quote import FabricationQuote
from app.models.governance import (
    ControlledAccessEvent,
    CustomerContract,
    CustomerHandlingInstruction,
    DataClassification,
)
from app.models.part import Part, is_material_supply_part_type
from app.models.part_number_alias import PartNumberAlias
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine
from app.models.quality import CorrectiveActionRequest, FirstArticleInspection, NonConformanceReport
from app.models.quote import Quote, QuoteLine
from app.models.routing import Routing
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.services.audit_service import AuditService
from app.services.export_control_redaction import (
    UNLINKABLE,
    Control,
    ControlledIndex,
    RedactionHit,
    ai_external_error_body,
    customer_control,
    is_masked_lookup,
    normalize_key,
    redact_payload,
    worst,
)

logger = logging.getLogger(__name__)

# The two classifications that make a customer controlled (the brief: ITAR_EXPORT_CONTROLLED / CUI).
CONTROLLED_CLASSIFICATIONS = frozenset({DataClassification.ITAR_EXPORT_CONTROLLED.value, DataClassification.CUI.value})

# controlled_access_events column widths (governance.py)
_REQUEST_ID_MAX = 100
_SOURCE_IP_MAX = 45


def normalize_customer_key(value: Any) -> str:
    """The name-join key: casefold, strip, collapse whitespace (``"  ACME  Aero "`` -> ``"acme aero"``)."""
    return normalize_key(value)


# --------------------------------------------------------------------------- controlled customers


def contract_classification(
    *, itar_controlled: Any, export_controlled: Any, contains_cui: Any, default_data_classification: Any
) -> Optional[str]:
    """The classification a contract imposes on its customer, or None when it imposes none.

    ITAR / export control wins over CUI. ``contains_fci``, ``restricted_customer`` and a
    handling instruction's ``requires_export_review`` do not control (owner, 2026-11-13).
    """
    classification = getattr(default_data_classification, "value", default_data_classification)
    if itar_controlled or export_controlled or classification == DataClassification.ITAR_EXPORT_CONTROLLED.value:
        return CLASSIFICATION_ITAR
    if contains_cui or classification == DataClassification.CUI.value:
        return CLASSIFICATION_CUI
    return None


def controlled_customers(db: Session, company_id: int) -> Dict[int, Control]:
    """customer id -> the control its contracts / handling instructions impose (controlled customers only).

    ANY contract row counts -- inactive, expired, not yet effective (owner, 2026-11-13).
    Ties between equally severe contracts go to the lowest contract id, so the answer is stable.
    """
    contracts = (
        tenant_filter(
            db.query(
                CustomerContract.id,
                CustomerContract.customer_id,
                CustomerContract.itar_controlled,
                CustomerContract.export_controlled,
                CustomerContract.contains_cui,
                CustomerContract.default_data_classification,
            ),
            CustomerContract,
            company_id,
        )
        .order_by(CustomerContract.id)
        .all()
    )
    result: Dict[int, Control] = {}
    contract_customer: Dict[int, Optional[int]] = {}
    for row in contracts:
        contract_customer[row.id] = row.customer_id
        if row.customer_id is None:
            continue
        classification = contract_classification(
            itar_controlled=row.itar_controlled,
            export_controlled=row.export_controlled,
            contains_cui=row.contains_cui,
            default_data_classification=row.default_data_classification,
        )
        if classification is not None:
            control = customer_control(classification, row.customer_id, row.id)
            result[row.customer_id] = worst(result.get(row.customer_id), control) or control

    instructions = (
        tenant_filter(
            db.query(
                CustomerHandlingInstruction.id,
                CustomerHandlingInstruction.customer_id,
                CustomerHandlingInstruction.contract_id,
                CustomerHandlingInstruction.default_data_classification,
            ),
            CustomerHandlingInstruction,
            company_id,
        )
        .filter(CustomerHandlingInstruction.default_data_classification.in_(sorted(CONTROLLED_CLASSIFICATIONS)))
        .order_by(CustomerHandlingInstruction.id)
        .all()
    )
    for row in instructions:
        customer_id = row.customer_id
        if customer_id is None and row.contract_id is not None:
            customer_id = contract_customer.get(row.contract_id)
        if customer_id is None:
            continue
        classification = (
            CLASSIFICATION_ITAR
            if row.default_data_classification == DataClassification.ITAR_EXPORT_CONTROLLED.value
            else CLASSIFICATION_CUI
        )
        control = customer_control(classification, customer_id, row.contract_id)
        result[customer_id] = worst(result.get(customer_id), control) or control
    return result


# --------------------------------------------------------------------------- the index

_BLANK = Control(reason="blank", classification=CLASSIFICATION_UNKNOWN)  # internal sentinel, never emitted


def _customer_controlled(control: Optional[Control]) -> bool:
    return control is not None and control.reason == REASON_EXPORT_CONTROLLED_CUSTOMER


def customer_former_names(db: Session, company_id: int, customer_ids: Iterable[int]) -> Dict[int, Set[str]]:
    """customer id -> every name / code the customer's own audit history says it has carried.

    The free-text join (a record's ``customer_name``) knows only the customer master's CURRENT
    names, and a rename cascades to nothing. So renaming a controlled customer, then creating
    (or renaming) another customer into the old name, made every historical record filed
    under that name resolve to the NEW, cleared customer -- served in full to an AI-external
    token, with no record edited and no Admin un-marking anything (code review P0 round 2,
    finding 5). The customer master's ``audit_log`` rows carry the history: every rename's
    UPDATE row has the old and new name (``update_customer`` writes it as REQUIRED evidence),
    a CREATE row the first name and code, a hard delete's row the whole record. The table is
    append-only (the 008/060 triggers refuse UPDATE and DELETE, and archival keeps the live
    rows), so the history cannot be edited away.

    Only ``customer_ids`` are read -- the caller passes the controlled customers: a former
    name CONTROLS (it is withheld like a current one) but never CLEARS, so the owner's
    positive-clearance rule (a name clears only as a known customer's current name or code)
    is unchanged.
    """
    ids = sorted(set(customer_ids))
    if not ids:
        return {}
    rows = (
        tenant_filter(db.query(AuditLog.resource_id, AuditLog.old_values, AuditLog.new_values), AuditLog, company_id)
        .filter(AuditLog.resource_type == "customer", AuditLog.resource_id.in_(ids))
        .all()
    )
    former: Dict[int, Set[str]] = defaultdict(set)
    for row in rows:
        for values in (row.old_values, row.new_values):
            if not isinstance(values, dict):
                continue
            for field_name in ("name", "code"):
                value = values.get(field_name)
                if isinstance(value, str) and value.strip():
                    former[row.resource_id].add(value.strip())
    return dict(former)


class _NameResolver:
    """Positive clearance for a free-text ``customer_name``."""

    def __init__(
        self,
        customers: Sequence[Any],
        controlled: Mapping[int, Control],
        former_names: Optional[Mapping[int, Iterable[str]]] = None,
    ) -> None:
        self.status: Dict[str, Optional[Control]] = {}
        self.controlled_names: Dict[str, Tuple[int, Control]] = {}
        for customer in customers:
            control = controlled.get(customer.id)
            for raw in (customer.name, customer.code):
                key = normalize_key(raw)
                if not key:
                    continue
                if control is not None:
                    self._control(key, customer.id, control)
                elif key not in self.status:
                    self.status[key] = None
        # A name a controlled customer used to carry stays controlled, whoever carries it now.
        for customer_id, names in (former_names or {}).items():
            control = controlled.get(customer_id)
            if control is None:
                continue
            for raw in names:
                key = normalize_key(raw)
                if key:
                    self._control(key, customer_id, control)

    def _control(self, key: str, customer_id: int, control: Control) -> None:
        # A key shared by a cleared and a controlled customer is controlled: fail closed.
        self.status[key] = worst(self.status.get(key), control)
        existing = self.controlled_names.get(key)
        if existing is None or control.rank > existing[1].rank:
            self.controlled_names[key] = (customer_id, control)

    def resolve(self, raw: Any) -> Optional[Control]:
        """``_BLANK`` for no name, UNLINKABLE for a name no customer carries, else the customer's control (None = cleared)."""
        key = normalize_key(raw)
        if not key:
            return _BLANK
        if key not in self.status:
            return UNLINKABLE
        return self.status[key]


def _tokens(*values: Any) -> List[str]:
    return [str(value).strip() for value in values if isinstance(value, str) and value.strip()]


def build_controlled_index(db: Session, company_id: int) -> ControlledIndex:
    """The company's export-control index (see the module docstring for every rule)."""
    controlled = controlled_customers(db, company_id)
    # Soft-deleted customers deliberately included: a deleted customer still names its history.
    customers = tenant_filter(db.query(Customer.id, Customer.name, Customer.code), Customer, company_id).all()
    former = customer_former_names(db, company_id, controlled)
    names = _NameResolver(customers, controlled, former)
    sensitive: Set[str] = set()
    for customer in customers:
        if customer.id in controlled:
            sensitive.update(_tokens(customer.name, customer.code))
    for held in former.values():
        sensitive.update(_tokens(*held))

    # ---- parts (soft-deleted included: a historical record can still name one)
    parts = tenant_filter(
        db.query(
            Part.id,
            Part.part_number,
            Part.part_type,
            Part.customer_name,
            Part.customer_part_number,
            Part.drawing_number,
        ),
        Part,
        company_id,
    ).all()
    part_control: Dict[int, Optional[Control]] = {}
    material_blank: Set[int] = set()
    for part in parts:
        material = is_material_supply_part_type(part.part_type)
        named = names.resolve(part.customer_name)
        if named is _BLANK:
            if material:
                material_blank.add(part.id)
                part_control[part.id] = None
            else:
                part_control[part.id] = UNLINKABLE
        else:
            part_control[part.id] = named
            if named is UNLINKABLE or _customer_controlled(named):
                # An unresolvable name may be a controlled customer misspelled: withhold it too.
                sensitive.update(_tokens(part.customer_name))

    # ---- work orders and their operations (soft-deleted work orders included)
    work_orders = tenant_filter(
        db.query(
            WorkOrder.id,
            WorkOrder.work_order_number,
            WorkOrder.part_id,
            WorkOrder.parent_work_order_id,
            WorkOrder.customer_name,
        ),
        WorkOrder,
        company_id,
    ).all()
    operations = tenant_filter(
        db.query(WorkOrderOperation.id, WorkOrderOperation.work_order_id, WorkOrderOperation.component_part_id),
        WorkOrderOperation,
        company_id,
    ).all()
    components: Dict[int, List[int]] = defaultdict(list)
    for op in operations:
        if op.component_part_id is not None:
            components[op.work_order_id].append(op.component_part_id)

    wo_name = {wo.id: names.resolve(wo.customer_name) for wo in work_orders}
    for wo in work_orders:
        if wo_name[wo.id] is UNLINKABLE or _customer_controlled(wo_name[wo.id]):
            sensitive.update(_tokens(wo.customer_name))

    # Closure, ONE step (the linkage map): every part a directly name-controlled work order
    # builds or consumes is controlled with it. Not iterated -- a fixpoint would spread one
    # ITAR job's common component across the whole shop.
    for wo in work_orders:
        control = wo_name[wo.id]
        if not _customer_controlled(control):
            continue
        for part_id in [wo.part_id, *components.get(wo.id, [])]:
            if part_id is not None:
                part_control[part_id] = worst(part_control.get(part_id), control)

    wo_control: Dict[int, Optional[Control]] = {}
    pending: List[Any] = []
    for wo in work_orders:
        found = [wo_name[wo.id]] if _customer_controlled(wo_name[wo.id]) else []
        for part_id in [wo.part_id, *components.get(wo.id, [])]:
            if part_id is not None and _customer_controlled(part_control.get(part_id)):
                found.append(part_control[part_id])
        if found:
            wo_control[wo.id] = worst(*found)
        elif wo_name[wo.id] is not _BLANK:
            wo_control[wo.id] = wo_name[wo.id]  # cleared, or UNLINKABLE (unresolvable name)
        elif wo.part_id is not None:
            # No name of its own: the work order is its part's customer's.
            wo_control[wo.id] = part_control[wo.part_id] if wo.part_id in part_control else UNLINKABLE
        else:
            pending.append(wo)  # no name, no part: only a parent can place it (a laser child)

    # A controlled parent controls its children, transitively.
    parents = {wo.id: wo.parent_work_order_id for wo in work_orders}
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if parent is None:
                continue
            parent_control = wo_control.get(parent)
            if _customer_controlled(parent_control) and not _customer_controlled(wo_control.get(child)):
                wo_control[child] = parent_control
                changed = True
    # Unplaced work orders inherit their parent's placement, else they are unlinkable
    # (a standalone laser work order: no part, no customer, no parent).
    for _ in range(len(pending) + 1):
        progressed = False
        for wo in list(pending):
            parent = wo.parent_work_order_id
            if parent is not None and parent in wo_control:
                wo_control[wo.id] = wo_control[parent]
                pending.remove(wo)
                progressed = True
        if not progressed:
            break
    for wo in pending:
        wo_control.setdefault(wo.id, UNLINKABLE)

    op_control: Dict[int, Optional[Control]] = {}
    for op in operations:
        component = None
        if op.component_part_id is not None:
            component = part_control.get(op.component_part_id, UNLINKABLE)
        op_control[op.id] = worst(wo_control.get(op.work_order_id, UNLINKABLE), component)

    # ---- routings (soft-deleted included: a released job's routing history still names its part)
    routings = tenant_filter(db.query(Routing.id, Routing.part_id), Routing, company_id).all()
    routing_control = {row.id: part_control.get(row.part_id, UNLINKABLE) for row in routings}

    # ---- purchase orders and receipts (soft-deleted included)
    lines = tenant_filter(
        db.query(PurchaseOrderLine.id, PurchaseOrderLine.purchase_order_id, PurchaseOrderLine.part_id),
        PurchaseOrderLine,
        company_id,
    ).all()

    def line_control(part_id: Optional[int]) -> Optional[Control]:
        if part_id is None or part_id not in part_control:
            return UNLINKABLE
        control = part_control[part_id]
        if control is not None:
            return control
        # Raw material / hardware bought with no customer on the part: the buy cannot be tied
        # to the job it was for, so it is unlinkable (the part record itself still passes).
        return UNLINKABLE if part_id in material_blank else None

    line_part = {line.id: line.part_id for line in lines}
    po_lines: Dict[int, List[Optional[Control]]] = defaultdict(list)
    for line in lines:
        po_lines[line.purchase_order_id].append(line_control(line.part_id))
    purchase_orders = tenant_filter(
        db.query(PurchaseOrder.id, PurchaseOrder.po_number), PurchaseOrder, company_id
    ).all()
    po_control = {po.id: (worst(*po_lines[po.id]) if po_lines.get(po.id) else UNLINKABLE) for po in purchase_orders}
    receipts = tenant_filter(
        db.query(POReceipt.id, POReceipt.receipt_number, POReceipt.po_line_id), POReceipt, company_id
    ).all()
    receipt_control = {
        row.id: (line_control(line_part[row.po_line_id]) if row.po_line_id in line_part else UNLINKABLE)
        for row in receipts
    }

    # ---- NCRs (soft-deleted / voided included)
    ncrs = tenant_filter(
        db.query(
            NonConformanceReport.id,
            NonConformanceReport.ncr_number,
            NonConformanceReport.part_id,
            NonConformanceReport.work_order_id,
            NonConformanceReport.receipt_id,
            NonConformanceReport.car_id,
        ),
        NonConformanceReport,
        company_id,
    ).all()
    ncr_control: Dict[int, Optional[Control]] = {}
    car_referrers: Dict[int, List[Optional[Control]]] = defaultdict(list)
    for ncr in ncrs:
        links: List[Optional[Control]] = []
        if ncr.part_id is not None:
            links.append(part_control.get(ncr.part_id, UNLINKABLE))
        if ncr.work_order_id is not None:
            links.append(wo_control.get(ncr.work_order_id, UNLINKABLE))
        if ncr.receipt_id is not None:
            links.append(receipt_control.get(ncr.receipt_id, UNLINKABLE))
        ncr_control[ncr.id] = worst(*links) if links else UNLINKABLE
        if ncr.car_id is not None:
            car_referrers[ncr.car_id].append(ncr_control[ncr.id])

    # ---- CARs: placed by the NCRs and complaints that reference them ...
    known_customer_ids = {customer.id for customer in customers}
    complaints = (
        tenant_filter(
            db.query(
                CustomerComplaint.customer_id,
                CustomerComplaint.customer_name,
                CustomerComplaint.part_id,
                CustomerComplaint.work_order_id,
                CustomerComplaint.car_id,
            ),
            CustomerComplaint,
            company_id,
        )
        .filter(CustomerComplaint.car_id.isnot(None))
        .all()
    )
    for complaint in complaints:
        if complaint.customer_id is not None and complaint.customer_id in controlled:
            by_customer: Optional[Control] = controlled[complaint.customer_id]
        elif complaint.customer_id is not None and complaint.customer_id in known_customer_ids:
            by_customer = None
        else:
            resolved = names.resolve(complaint.customer_name)
            by_customer = UNLINKABLE if resolved is _BLANK else resolved
            if resolved is UNLINKABLE:
                sensitive.update(_tokens(complaint.customer_name))
        links = [by_customer]
        if complaint.part_id is not None:
            links.append(part_control.get(complaint.part_id, UNLINKABLE))
        if complaint.work_order_id is not None:
            links.append(wo_control.get(complaint.work_order_id, UNLINKABLE))
        car_referrers[complaint.car_id].append(worst(*links))
    # ---- and by the CAR's OWN links (migration 122_car_form_quote_rev): the CAR form's part,
    # customer and the customer-name snapshot taken when the customer was chosen. A CAR is
    # cleared only when EVERY link -- referrers and direct -- resolves to a known, uncontrolled
    # customer (or generic stock); any controlled or unresolvable link withholds it. So a CAR
    # escalated from a cleared NCR is withheld the moment its customer is set to a controlled
    # one. The snapshot is judged on its own as well (positive clearance, the former-names rule
    # included): it names the customer as the CAR recorded it, and it outlives a rename.
    # Soft-deleted / voided CARs, parts and customers are included (invariant 3's
    # historical-record exception), as everywhere in this index.
    cars = tenant_filter(
        db.query(
            CorrectiveActionRequest.id,
            CorrectiveActionRequest.car_number,
            CorrectiveActionRequest.part_id,
            CorrectiveActionRequest.customer_id,
            CorrectiveActionRequest.customer_name,
        ),
        CorrectiveActionRequest,
        company_id,
    ).all()
    car_control: Dict[int, Optional[Control]] = {}
    for car in cars:
        car_links: List[Optional[Control]] = list(car_referrers.get(car.id, []))
        if car.part_id is not None:
            car_links.append(part_control.get(car.part_id, UNLINKABLE))
        if car.customer_id is not None:
            if car.customer_id in controlled:
                car_links.append(controlled[car.customer_id])
            elif car.customer_id in known_customer_ids:
                car_links.append(None)
            else:
                car_links.append(UNLINKABLE)  # an id the index never enumerated: never "fine"
        snapshot = names.resolve(car.customer_name)
        if snapshot is not _BLANK:
            car_links.append(snapshot)
            if snapshot is UNLINKABLE or _customer_controlled(snapshot):
                sensitive.update(_tokens(car.customer_name))
        car_control[car.id] = worst(*car_links) if car_links else UNLINKABLE

    # ---- FAIs
    fais = tenant_filter(
        db.query(
            FirstArticleInspection.id,
            FirstArticleInspection.fai_number,
            FirstArticleInspection.part_id,
            FirstArticleInspection.work_order_id,
        ),
        FirstArticleInspection,
        company_id,
    ).all()
    fai_control = {
        fai.id: worst(
            part_control.get(fai.part_id, UNLINKABLE),
            wo_control.get(fai.work_order_id, UNLINKABLE) if fai.work_order_id is not None else None,
        )
        for fai in fais
    }

    # ---- quotes
    quotes = tenant_filter(
        db.query(Quote.id, Quote.quote_number, Quote.customer_name, Quote.work_order_id), Quote, company_id
    ).all()
    quote_links: Dict[int, List[Optional[Control]]] = defaultdict(list)
    quote_lines = tenant_filter(
        db.query(QuoteLine.quote_id, QuoteLine.part_id, QuoteLine.work_order_id), QuoteLine, company_id
    ).all()
    for line in quote_lines:
        if line.part_id is not None:
            quote_links[line.quote_id].append(part_control.get(line.part_id, UNLINKABLE))
        if line.work_order_id is not None:
            quote_links[line.quote_id].append(wo_control.get(line.work_order_id, UNLINKABLE))
    fab_quotes = (
        tenant_filter(
            db.query(FabricationQuote.erp_quote_id, FabricationQuote.customer_id), FabricationQuote, company_id
        )
        .filter(FabricationQuote.erp_quote_id.isnot(None))
        .all()
    )
    for fab in fab_quotes:
        if fab.customer_id is not None and fab.customer_id in controlled:
            quote_links[fab.erp_quote_id].append(controlled[fab.customer_id])
    quote_control: Dict[int, Optional[Control]] = {}
    for quote in quotes:
        named = names.resolve(quote.customer_name)
        if named is UNLINKABLE or _customer_controlled(named):
            sensitive.update(_tokens(quote.customer_name))
        links = [UNLINKABLE if named is _BLANK else named, *quote_links.get(quote.id, [])]
        if quote.work_order_id is not None:
            links.append(wo_control.get(quote.work_order_id, UNLINKABLE))
        quote_control[quote.id] = worst(*links)

    # ---- assemble
    by_type: Dict[str, Dict[int, Optional[Control]]] = {
        "customer": {customer.id: controlled.get(customer.id) for customer in customers},
        "part": part_control,
        "work_order": wo_control,
        "operation": op_control,
        "routing": routing_control,
        "purchase_order": po_control,
        "receipt": receipt_control,
        "ncr": ncr_control,
        "car": car_control,
        "fai": fai_control,
        "quote": quote_control,
    }
    numbered: Dict[str, Iterable[Tuple[int, Any]]] = {
        "part": [(part.id, part.part_number) for part in parts],
        "work_order": [(wo.id, wo.work_order_number) for wo in work_orders],
        "purchase_order": [(po.id, po.po_number) for po in purchase_orders],
        "receipt": [(row.id, row.receipt_number) for row in receipts],
        "ncr": [(ncr.id, ncr.ncr_number) for ncr in ncrs],
        "car": [(car.id, car.car_number) for car in cars],
        "fai": [(fai.id, fai.fai_number) for fai in fais],
        "quote": [(quote.id, quote.quote_number) for quote in quotes],
    }
    controlled_map = {
        record_type: {record_id: control for record_id, control in controls.items() if control is not None}
        for record_type, controls in by_type.items()
    }
    numbers: Dict[str, Dict[str, int]] = {}
    for record_type, rows in numbered.items():
        withheld = controlled_map[record_type]
        numbers[record_type] = {
            normalize_key(number): record_id for record_id, number in rows if record_id in withheld and number
        }

    # Part aliases: a withheld part's alternate numbers are withheld too.
    aliases = tenant_filter(
        db.query(PartNumberAlias.part_id, PartNumberAlias.alias_number), PartNumberAlias, company_id
    ).all()
    withheld_parts = controlled_map["part"]
    for alias in aliases:
        if alias.part_id in withheld_parts and alias.alias_number:
            numbers["part"].setdefault(normalize_key(alias.alias_number), alias.part_id)
            sensitive.update(_tokens(alias.alias_number))
    for part in parts:
        if part.id in withheld_parts:
            sensitive.update(_tokens(part.part_number, part.customer_part_number, part.drawing_number))

    return ControlledIndex(
        company_id=company_id,
        controlled=controlled_map,
        known={record_type: frozenset(controls) for record_type, controls in by_type.items()},
        numbers=numbers,
        customer_names=dict(names.controlled_names),
        sensitive_tokens=tuple(sorted(sensitive)),
    )


# --------------------------------------------------------------------------- the ledger


def _event_row(
    marker: ExportControlMarker,
    *,
    record_type: str,
    record_id: Optional[int],
    action: str,
    reason: str,
    classification: str,
    customer_id: Optional[int],
    contract_id: Optional[int],
    request_id: Optional[str],
    source_ip: Optional[str],
    route: Optional[str],
    occurred_at: datetime,
) -> Dict[str, Any]:
    return {
        "company_id": marker.company_id,
        "record_type": record_type,
        "record_id": record_id,
        "action": action,
        "allowed": False,
        "denial_reason": reason,
        "user_id": marker.user_id,
        "occurred_at": occurred_at,
        "request_id": (request_id or None) and str(request_id)[:_REQUEST_ID_MAX],
        "source_ip": (source_ip or None) and str(source_ip)[:_SOURCE_IP_MAX],
        "data_classification": classification,
        "extra_data": {
            "credential": marker.credential(),
            "route": route or marker.route,
            "customer_id": customer_id,
            "contract_id": contract_id,
        },
    }


def insert_controlled_access_events(db: Session, rows: Sequence[Mapping[str, Any]]) -> None:
    """ONE bulk insert of ledger rows into the caller's transaction (the seam tests break to prove 503)."""
    if rows:
        db.execute(insert(ControlledAccessEvent), [dict(row) for row in rows])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def redaction_event_rows(
    marker: ExportControlMarker,
    hits: Sequence[RedactionHit],
    *,
    request_id: Optional[str],
    source_ip: Optional[str],
    route: Optional[str],
) -> List[Dict[str, Any]]:
    """One ``ai_external_redacted`` row per withheld (record_type, record_id) -- already deduped by the engine."""
    moment = _utcnow()
    return [
        _event_row(
            marker,
            record_type=hit.record_type,
            record_id=hit.record_id,
            action=ACTION_REDACTED,
            reason=hit.control.reason,
            classification=hit.control.classification,
            customer_id=hit.control.customer_id,
            contract_id=hit.control.contract_id,
            request_id=request_id,
            source_ip=source_ip,
            route=route,
            occurred_at=moment,
        )
        for hit in hits
    ]


SessionFactory = Callable[[], Session]


def _session(factory: Optional[SessionFactory]) -> Session:
    return (factory or _database.SessionLocal)()


def record_refusal(
    marker: ExportControlMarker,
    reason: str,
    *,
    request_id: Optional[str],
    source_ip: Optional[str],
    route: Optional[str],
    session_factory: Optional[SessionFactory] = None,
) -> None:
    """Write ONE ``ai_external_refused`` row (record_id NULL) in a short-lived session, and commit."""
    db = _session(session_factory)
    try:
        insert_controlled_access_events(
            db,
            [
                _event_row(
                    marker,
                    record_type="request",
                    record_id=None,
                    action=ACTION_REFUSED,
                    reason=reason,
                    classification=CLASSIFICATION_UNKNOWN,
                    customer_id=None,
                    contract_id=None,
                    request_id=request_id,
                    source_ip=source_ip,
                    route=route,
                    occurred_at=_utcnow(),
                )
            ],
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@dataclass(frozen=True)
class ExportControlOutcome:
    """What the middleware sends instead of the route's body.

    ``rebuilt`` -- the response is a NEW one (a route error's fixed text, or a masked
    lookup's 404): send ``status_code`` with fresh headers, none of the route's. Otherwise
    send the route's status and headers with this ``payload`` as the body.
    """

    status_code: int
    payload: Any
    rebuilt: bool = False


def apply_export_control(
    marker: ExportControlMarker,
    payload: Any,
    *,
    status_code: int,
    route_path: Optional[str],
    path_params: Optional[Mapping[str, Any]],
    request_id: Optional[str],
    source_ip: Optional[str],
    route: Optional[str] = None,
    session_factory: Optional[SessionFactory] = None,
) -> ExportControlOutcome:
    """The AI-external view of one decoded JSON response, with its ledger rows COMMITTED before it returns.

    A 2xx body gets both passes and one ``ai_external_redacted`` row per withheld record --
    and a by-number lookup that found a withheld record answers the not-found 404 instead
    (``is_masked_lookup``). A route's own error answers a fixed text per status code
    (``ai_external_error_body``): its text could echo the request, and scrubbing an echo
    would confirm a probe. Blocking (the middleware runs it in a worker thread). Raises on
    any failure -- the caller then sends 503 and nothing else.

    A masked lookup's OWN 404 (the number matched nothing) does the same work a withheld
    hit does before it is answered: the index is built and one ``ai_external_lookup_miss``
    row is written and committed (code review P0 round 2, finding 1). The two 404s are
    byte-identical, and a miss that skipped the index build and the commit answered several
    times faster -- latency alone confirmed that a guessed part number exists.
    """
    lookup = route_path is not None and route_path in AI_EXTERNAL_MASKED_LOOKUPS
    if not 200 <= status_code < 300:
        if lookup and status_code == 404:
            _record_masked_lookup_miss(
                marker,
                route_path=route_path,
                request_id=request_id,
                source_ip=source_ip,
                route=route or marker.route,
                session_factory=session_factory,
            )
        return ExportControlOutcome(status_code, ai_external_error_body(status_code, payload), rebuilt=True)
    db = _session(session_factory)
    try:
        index = build_controlled_index(db, marker.company_id)
        result = redact_payload(payload, index, route_path=route_path, path_params=path_params)
        rows = redaction_event_rows(
            marker, result.hits, request_id=request_id, source_ip=source_ip, route=route or marker.route
        )
        if rows:
            insert_controlled_access_events(db, rows)
        db.commit()
        if is_masked_lookup(route_path, result):
            return ExportControlOutcome(404, ai_external_error_body(404, None), rebuilt=True)
        return ExportControlOutcome(status_code, result.payload)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _record_masked_lookup_miss(
    marker: ExportControlMarker,
    *,
    route_path: str,
    request_id: Optional[str],
    source_ip: Optional[str],
    route: Optional[str],
    session_factory: Optional[SessionFactory],
) -> None:
    """The withheld-hit path's work, for a masked lookup that matched nothing: index, one ledger row, commit.

    The index is built and discarded on purpose: it is the cost being matched, not a
    result anything reads. Raises on any failure, exactly like the hit path (the caller
    then answers 503), so a miss and a hit also fail alike.
    """
    _param, record_type = AI_EXTERNAL_MASKED_LOOKUPS[route_path]
    db = _session(session_factory)
    try:
        build_controlled_index(db, marker.company_id)
        insert_controlled_access_events(
            db,
            [
                _event_row(
                    marker,
                    record_type=record_type,
                    record_id=None,
                    action=ACTION_LOOKUP_MISS,
                    reason=REASON_NOT_FOUND,
                    classification=CLASSIFICATION_UNKNOWN,
                    customer_id=None,
                    contract_id=None,
                    request_id=request_id,
                    source_ip=source_ip,
                    route=route,
                    occurred_at=_utcnow(),
                )
            ],
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# --------------------------------------------------------------------------- customer-contract writes

CONTRACT_FIELDS = (
    "contract_number",
    "contract_name",
    "itar_controlled",
    "export_controlled",
    "contains_cui",
    "default_data_classification",
    "effective_date",
    "expiration_date",
    "active",
    "handling_instructions",
)


class CustomerNotFound(LookupError):
    """The customer (or the contract under it) is not in the active company."""


def customer_for_export_control(db: Session, company_id: int, customer_id: int) -> Customer:
    """The company's customer by id -- a soft-deleted one INCLUDED, on purpose.

    Historical records stay controlled after a customer is deleted, so an Admin must still be
    able to read and mark that customer's contracts (the records-not-selection exception of
    invariant 3). Raises :class:`CustomerNotFound` for another tenant's id or no such row.
    """
    customer = tenant_query(db, Customer, company_id).filter(Customer.id == customer_id).first()
    if customer is None:
        raise CustomerNotFound("Customer not found")
    return customer


def customer_has_export_control_records(db: Session, company_id: int, customer_id: int) -> bool:
    """Does any contract or handling instruction of the company name this customer (any flags, any state)?"""
    contract = (
        tenant_filter(db.query(CustomerContract.id), CustomerContract, company_id)
        .filter(CustomerContract.customer_id == customer_id)
        .first()
    )
    if contract is not None:
        return True
    instruction = (
        tenant_filter(db.query(CustomerHandlingInstruction.id), CustomerHandlingInstruction, company_id)
        .filter(CustomerHandlingInstruction.customer_id == customer_id)
        .first()
    )
    return instruction is not None


def customer_contracts(db: Session, company_id: int, customer_id: int) -> List[CustomerContract]:
    return (
        tenant_query(db, CustomerContract, company_id)
        .filter(CustomerContract.customer_id == customer_id)
        .order_by(CustomerContract.id)
        .all()
    )


def contract_is_controlling(contract: CustomerContract) -> bool:
    return (
        contract_classification(
            itar_controlled=contract.itar_controlled,
            export_controlled=contract.export_controlled,
            contains_cui=contract.contains_cui,
            default_data_classification=contract.default_data_classification,
        )
        is not None
    )


def customer_control_of(db: Session, company_id: int, customer_id: int) -> Optional[Control]:
    """The customer's derived export control (contracts + handling instructions), or None."""
    return controlled_customers(db, company_id).get(customer_id)


def _snapshot(contract: CustomerContract) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in CONTRACT_FIELDS:
        value = getattr(contract, name)
        out[name] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _naive_utc(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _audit_contract(
    audit: AuditService,
    *,
    action: str,
    contract: CustomerContract,
    customer: Customer,
    old: Optional[Dict[str, Any]],
    new: Dict[str, Any],
    reason: str,
    before: Optional[Control],
    after: Optional[Control],
) -> None:
    identifier = contract.contract_number or contract.contract_name
    audit.log_required(
        action,
        "customer_contract",
        resource_id=contract.id,
        resource_identifier=identifier,
        description=f"{'Created' if action == 'CREATE' else 'Updated'} export-control contract {identifier} "
        f"for customer {customer.name}",
        old_values=old,
        new_values=new,
        extra_data={
            "reason": reason,
            "customer_id": customer.id,
            "customer_controlled_before": before is not None,
            "customer_controlled_after": after is not None,
            "classification_after": after.classification if after is not None else None,
        },
        company_id=contract.company_id,
    )
    if (before is None) != (after is None) or (before and after and before.classification != after.classification):
        audit.log_required(
            "STATUS_CHANGE",
            "customer",
            resource_id=customer.id,
            resource_identifier=customer.name,
            description=f"Customer {customer.name} export control: "
            f"{before.classification if before else 'not controlled'} -> "
            f"{after.classification if after else 'not controlled'}",
            old_values={"export_control": before.classification if before else None},
            new_values={"export_control": after.classification if after else None},
            extra_data={"reason": reason, "contract_id": contract.id},
            company_id=contract.company_id,
        )


def create_customer_contract(
    db: Session,
    audit: AuditService,
    *,
    company_id: int,
    customer: Customer,
    values: Mapping[str, Any],
    reason: str,
    actor_id: Optional[int],
) -> CustomerContract:
    """Add a contract under ``customer``; REQUIRED audit rows in the caller's transaction (flush, no commit).

    Raises ``sqlalchemy.exc.IntegrityError`` for a duplicate ``contract_number`` in the company
    and ``AuditWriteError`` when the audit cannot be written -- the caller rolls back either way.
    """
    before = customer_control_of(db, company_id, customer.id)
    contract = CustomerContract(
        company_id=company_id, customer_id=customer.id, created_by=actor_id, updated_by=actor_id
    )
    for name in CONTRACT_FIELDS:
        if name in values:
            setattr(contract, name, _naive_utc(values[name]))
    db.add(contract)
    db.flush()
    after = customer_control_of(db, company_id, customer.id)
    _audit_contract(
        audit,
        action="CREATE",
        contract=contract,
        customer=customer,
        old=None,
        new=_snapshot(contract),
        reason=reason,
        before=before,
        after=after,
    )
    return contract


def update_customer_contract(
    db: Session,
    audit: AuditService,
    *,
    company_id: int,
    customer: Customer,
    contract_id: int,
    changes: Mapping[str, Any],
    reason: str,
    actor_id: Optional[int],
) -> CustomerContract:
    """Change a contract of ``customer`` (un-marking = clearing the flags); REQUIRED audit, flush, no commit.

    Raises :class:`CustomerNotFound` when the contract is not this customer's in this company.
    A request that changes nothing writes nothing (no audit row either).
    """
    contract = (
        tenant_query(db, CustomerContract, company_id)
        .filter(CustomerContract.id == contract_id, CustomerContract.customer_id == customer.id)
        .with_for_update()
        .first()
    )
    if contract is None:
        raise CustomerNotFound("Contract not found")
    before = customer_control_of(db, company_id, customer.id)
    old = _snapshot(contract)
    for name in CONTRACT_FIELDS:
        if name in changes:
            setattr(contract, name, _naive_utc(changes[name]))
    new = _snapshot(contract)
    if new == old:
        return contract
    contract.updated_by = actor_id
    db.flush()
    after = customer_control_of(db, company_id, customer.id)
    changed_old = {key: old[key] for key in new if old[key] != new[key]}
    changed_new = {key: new[key] for key in new if old[key] != new[key]}
    _audit_contract(
        audit,
        action="UPDATE",
        contract=contract,
        customer=customer,
        old=changed_old,
        new=changed_new,
        reason=reason,
        before=before,
        after=after,
    )
    return contract
