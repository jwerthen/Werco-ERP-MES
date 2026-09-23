"""Deterministic, read-only receiving suggestions from retained PDF evidence.

Extraction is evidence, never authorization to receive or accept material. The
ordinary reviewed receive_delivery command remains the only mutation boundary.
"""

import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from app.db.tenant_filter import tenant_query
from app.models.hank import HankTask
from app.models.hank_intake import HankIntakeFile
from app.models.part import Part, uom_label
from app.models.purchasing import POReceipt, POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.schemas.hank_intake import (
    IntakeExtraction,
    IntakeLine,
    IntakeReceivingCandidate,
    IntakeReceivingDraft,
    IntakeReceivingLine,
    IntakeReceivingPurchaseOrder,
)
from app.services.hank_intake_service import HankIntakeService
from app.services.hank_task_service import PLAN_ROW_LIMIT, HankTaskService

READY_STATUSES = {'awaiting_review', 'planned', 'completed'}
OPEN_STATUSES = (POStatus.SENT, POStatus.PARTIAL)
_QUANTITY = re.compile(r'^(?:[0-9]+(?:\.[0-9]{1,4})?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,4})?)$')
_UNITS = {
    'ea': 'each',
    'pc': 'each',
    'pcs': 'each',
    'piece': 'each',
    'pieces': 'each',
    'ft': 'feet',
    'foot': 'feet',
    'in': 'inches',
    'inch': 'inches',
    'lb': 'pounds',
    'lbs': 'pounds',
    'pound': 'pounds',
    'kg': 'kilograms',
    'kilogram': 'kilograms',
    'sheet': 'sheets',
    'gal': 'gallons',
    'gallon': 'gallons',
    'l': 'liters',
    'liter': 'liters',
}


def _identifier(value):
    # Do not discard punctuation: materially different part numbers must not merge.
    return (value or '').strip().lower()


def _unit(value):
    value = uom_label(value)
    return _UNITS.get(value, value)


def _quantity(value):
    """Accept unambiguous printed positive decimals, never infer units or fractions."""
    text = (value or '').strip()
    if not _QUANTITY.fullmatch(text):
        return None
    try:
        number = Decimal(text.replace(',', ''))
        return float(number) if 0 < number < Decimal('10000000000') else None
    except InvalidOperation:
        return None


class HankIntakeReceivingService:
    def __init__(self, db, user, company_id):
        self.db, self.user, self.company_id = db, user, company_id
        self.intake = HankIntakeService(db, user, company_id)

    def source(self, file_id, *, version=None, locked=False):
        row = self.intake.file(file_id, locked=locked)
        HankTaskService(self.db, self.user, self.company_id)._require_action('receive_delivery')
        if row.status not in READY_STATUSES or not row.analysis_json:
            raise HTTPException(409, 'Wait for document analysis before preparing receiving.')
        if version is not None and row.version != version:
            raise HTTPException(409, 'The intake source changed. Refresh the document and review receiving again.')
        return row

    def prior_tasks(self, row, *, completed_only=False):
        # Query the whole tenant/hash cohort. Disclose only a warning, never another
        # employee's private intake or task identifiers.
        file_ids = select(HankIntakeFile.id).where(
            HankIntakeFile.company_id == self.company_id,
            HankIntakeFile.content_sha256 == row.content_sha256,
        )
        return (
            tenant_query(self.db, HankTask, self.company_id)
            .filter(
                HankTask.kind == 'receive_delivery',
                HankTask.status.in_(('completed',) if completed_only else ('awaiting_review', 'completed')),
                HankTask.input_json['source_intake_file_id'].as_integer().in_(file_ids),
            )
            .order_by(HankTask.id)
        )

    def matching_receipts(self, po_id, packing_slip_number):
        if not packing_slip_number:
            return []
        rows = (
            tenant_query(self.db, POReceipt, self.company_id)
            .join(PurchaseOrderLine, PurchaseOrderLine.id == POReceipt.po_line_id)
            .filter(
                PurchaseOrderLine.company_id == self.company_id,
                PurchaseOrderLine.purchase_order_id == po_id,
                POReceipt.is_deleted.is_(False),
                func.lower(func.trim(POReceipt.packing_slip_number)) == _identifier(packing_slip_number),
            )
            .order_by(POReceipt.id)
            .limit(PLAN_ROW_LIMIT + 1)
            .all()
        )
        if len(rows) > PLAN_ROW_LIMIT:
            raise HTTPException(
                409, 'Too many matching receipts for a bounded Hank review. Use Receiving to review this delivery.'
            )
        return rows

    def _orders(self):
        return (
            tenant_query(self.db, PurchaseOrder, self.company_id)
            .join(Vendor, Vendor.id == PurchaseOrder.vendor_id)
            .filter(
                PurchaseOrder.is_deleted.is_(False),
                PurchaseOrder.status.in_(OPEN_STATUSES),
                Vendor.company_id == self.company_id,
                Vendor.is_deleted.is_(False),
            )
        )

    def _lines(self, po_id):
        rows = (
            self.db.query(PurchaseOrderLine, Part)
            .join(Part, Part.id == PurchaseOrderLine.part_id)
            .filter(
                PurchaseOrderLine.company_id == self.company_id,
                PurchaseOrderLine.purchase_order_id == po_id,
                PurchaseOrderLine.is_closed.is_(False),
                PurchaseOrderLine.quantity_ordered > PurchaseOrderLine.quantity_received,
                Part.company_id == self.company_id,
                Part.is_deleted.is_(False),
            )
            .order_by(PurchaseOrderLine.line_number, PurchaseOrderLine.id)
            .limit(PLAN_ROW_LIMIT + 1)
            .all()
        )
        if len(rows) > PLAN_ROW_LIMIT:
            raise HTTPException(
                409,
                'This order has too many open lines for a bounded Hank review. Use Receiving to review this delivery.',
            )
        return rows

    def draft(self, file_id, purchase_order_id=None):
        row = self.source(file_id)
        analysis = IntakeExtraction.model_validate(row.analysis_json)
        fields = {}
        warnings = list(analysis.warnings)
        for field in analysis.fields:
            if field.value:
                if field.name in fields and (fields[field.name] is None or fields[field.name].value != field.value):
                    warnings.append(f'Conflicting {field.name.replace("_", " ")} values require review.')
                    fields[field.name] = None
                elif field.name not in fields:
                    fields[field.name] = field

        def value(name):
            field = fields.get(name)
            return field.value.strip() if field and field.value else None

        printed_po, supplier = value('po_number'), value('vendor_name')
        slip_field = fields.get('packing_slip_number')
        if not slip_field and analysis.classification == 'packing_slip':
            slip_field = fields.get('document_number')
        printed_slip = slip_field.value.strip() if slip_field and slip_field.value else None
        slip = printed_slip
        if slip_field and (slip_field.confidence != 'high' or not slip_field.evidence):
            warnings.append(
                'The packing slip number is uncertain or lacks page evidence. Verify and enter it manually.'
            )
            slip = None
        if slip and len(slip) > 50:
            warnings.append('The packing slip number exceeds the receipt field limit; review manually.')
            slip = None
        extracted_lines = analysis.lines
        if not extracted_lines and value('part_number'):
            names = ('part_number', 'quantity', 'lot_number', 'heat_number')
            evidence = [entry for name in names for entry in (fields[name].evidence if fields.get(name) else [])]
            header_values = {}
            for name in names:
                header_value = value(name)
                if header_value and len(header_value) > 100:
                    warnings.append(f'The printed {name.replace("_", " ")} is too long; review the PDF manually.')
                    header_value = None
                # Traceability fallback below checks its own confidence/evidence.
                header_values[name] = header_value if name in names[:2] else None
            extracted_lines = [
                IntakeLine(
                    **header_values,
                    confidence=(
                        'high'
                        if all(fields.get(name) and fields[name].confidence == 'high' for name in names[:2])
                        else 'low'
                    ),
                    evidence=evidence[:3],
                )
            ]
        parts = {_identifier(line.part_number) for line in extracted_lines if line.part_number}
        query = self._orders()
        if printed_po:
            query = query.filter(func.lower(func.trim(PurchaseOrder.po_number)) == _identifier(printed_po))
            reason = 'Exact printed purchase order number.'
        else:
            line_orders = (
                select(PurchaseOrderLine.purchase_order_id)
                .join(Part, Part.id == PurchaseOrderLine.part_id)
                .where(
                    PurchaseOrderLine.company_id == self.company_id,
                    PurchaseOrderLine.is_closed.is_(False),
                    PurchaseOrderLine.quantity_ordered > PurchaseOrderLine.quantity_received,
                    Part.company_id == self.company_id,
                    Part.is_deleted.is_(False),
                    func.lower(func.trim(Part.part_number)).in_(parts),
                )
            )
            conditions = [PurchaseOrder.id.in_(line_orders)]
            if supplier:
                conditions.append(func.lower(func.trim(Vendor.name)) == _identifier(supplier))
            query = query.filter(or_(*conditions))
            reason = 'An open order matches a printed part number or supplier; confirm the order.'
        orders = query.order_by(PurchaseOrder.id).limit(51).all()
        if len(orders) > 50:
            warnings.append('More than 50 orders match. Choose the exact purchase order in Receiving.')
        orders = orders[:50]
        selected = None
        if purchase_order_id is not None:
            selected = self._orders().filter(PurchaseOrder.id == purchase_order_id).first()
            if selected is None:
                raise HTTPException(404, 'Open purchase order not found in this company')
            if all(order.id != selected.id for order in orders):
                orders = [selected, *orders[:49]]
            if printed_po and _identifier(selected.po_number) != _identifier(printed_po):
                warnings.append('The selected order differs from the printed purchase order. Confirm the source.')
        elif (
            len(orders) == 1
            and printed_po
            and fields['po_number'].confidence == 'high'
            and fields['po_number'].evidence
        ):
            selected = orders[0]
        if selected is None:
            warnings.append('Choose and confirm an open purchase order before preparing a receiving task.')
        elif supplier and _identifier(selected.vendor.name) != _identifier(supplier):
            warnings.append('The printed supplier differs from the purchase order vendor. Resolve before receiving.')
        if printed_po and not orders:
            warnings.append('The printed purchase order is not an open receiving order in this company.')
        if analysis.classification != 'packing_slip':
            warnings.append(
                'This document is not identified as a packing slip. Confirm quantities physically delivered.'
            )

        available = self._lines(selected.id) if selected else []
        counts = Counter(_identifier(line.part_number) for line in extracted_lines if line.part_number)
        lines = []
        for index, source in enumerate(extracted_lines):
            problems = []
            candidates = [
                IntakeReceivingCandidate(
                    po_line_id=line.id,
                    line_number=line.line_number,
                    part_id=part.id,
                    part_number=part.part_number,
                    description=part.name,
                    quantity_remaining=max(0, float(line.quantity_ordered) - float(line.quantity_received or 0)),
                    unit_of_measure=uom_label(part.unit_of_measure),
                )
                for line, part in available
                if _identifier(source.part_number) == _identifier(part.part_number)
            ]
            matched = candidates[0] if len(candidates) == 1 else None
            if len(candidates) > 1:
                problems.append('This part appears on multiple open order lines. Choose the correct line manually.')
            elif not candidates:
                problems.append('No unique open order line matches this printed part number.')
            if source.confidence != 'high' or not source.evidence:
                problems.append(
                    'Extraction is uncertain or lacks page evidence. Verify against the PDF before entering values.'
                )
                matched = None
            if source.part_number and counts[_identifier(source.part_number)] > 1:
                problems.append(
                    'This part appears more than once in the document. Review separate lots and quantities manually.'
                )
                matched = None
            quantity = _quantity(source.quantity)
            if quantity is None:
                problems.append('The delivered quantity is missing or ambiguous. Enter a verified quantity.')
            if matched and quantity is not None and quantity > matched.quantity_remaining:
                problems.append(
                    f'The printed quantity exceeds the {matched.quantity_remaining:g} remaining on this order line.'
                )
            if matched and source.unit_of_measure and _unit(source.unit_of_measure) != _unit(matched.unit_of_measure):
                problems.append('The printed unit differs from the stocking unit. No unit conversion was inferred.')
                quantity = None
            elif not source.unit_of_measure:
                problems.append('No unit was extracted. Verify that the quantity uses the part stocking unit.')
                quantity = None
            if matched is None:
                quantity = None
            data = source.model_dump()
            if len(extracted_lines) == 1:
                for name in ('lot_number', 'heat_number'):
                    field = fields.get(name)
                    if data[name] or not field or not field.value:
                        continue
                    if field.confidence != 'high' or not field.evidence:
                        problems.append(
                            f'The header {name.replace("_", " ")} is uncertain or lacks page evidence; enter it manually.'
                        )
                    elif len(field.value.strip()) > 50:
                        problems.append(
                            f'The header {name.replace("_", " ")} exceeds the receipt field limit; review manually.'
                        )
                    else:
                        data[name] = field.value.strip()
            for name in ('lot_number', 'heat_number'):
                if data[name] and len(data[name]) > 50:
                    problems.append(
                        f'The printed {name.replace("_", " ")} exceeds the receipt field limit; review manually.'
                    )
            lines.append(
                IntakeReceivingLine(
                    **data,
                    source_line_index=index,
                    candidates=candidates,
                    po_line_id=matched.po_line_id if matched else None,
                    quantity_received=quantity,
                    warnings=problems,
                )
            )
        if not lines:
            warnings.append('No material lines were extracted. Read the PDF and enter the delivery manually.')
        _, _, has_duplicates = self.intake._duplicates(row)
        if has_duplicates:
            warnings.append('An identical PDF was already uploaded. Check for an existing receipt before receiving.')
        completed = self.prior_tasks(row, completed_only=True).first() is not None
        if completed:
            warnings.append(
                'A receiving task already completed from this PDF. Review prior receipts before recording another partial delivery.'
            )
        elif self.prior_tasks(row).first() is not None:
            warnings.append(
                'A receiving task is already awaiting review for this PDF. Check existing tasks before preparing another.'
            )
        receipts = self.matching_receipts(selected.id, printed_slip) if selected else []
        if receipts:
            warnings.append(
                'Receipts already exist on this order with this packing slip number. Verify this is additional material.'
            )
        return IntakeReceivingDraft(
            file_id=row.id,
            file_version=row.version,
            company_id=self.company_id,
            filename=row.filename,
            purchase_order_id=selected.id if selected else None,
            purchase_orders=[
                IntakeReceivingPurchaseOrder(
                    id=order.id,
                    po_number=order.po_number,
                    vendor_name=order.vendor.name,
                    reason='Employee-selected order.' if purchase_order_id == order.id else reason,
                )
                for order in orders
            ],
            packing_slip_number=slip,
            lines=lines,
            warnings=warnings,
            has_duplicates=has_duplicates,
            requires_duplicate_acknowledgement=completed or bool(receipts),
        )
