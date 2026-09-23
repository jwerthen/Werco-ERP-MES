"""Deterministic PO suggestions and the reviewed import boundary.

The retained document supplies evidence. Only an employee-reviewed Hank task
creates an order; marking it sent records an existing order without emailing it.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.hank import HankTask
from app.models.hank_intake import HankIntakeFile
from app.models.part import Part, uom_label
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.user import UserRole
from app.schemas.hank_intake import IntakeExtraction
from app.schemas.hank_purchase_order import (
    IntakeExistingPurchaseOrder,
    IntakePurchaseOrderDraft,
    IntakePurchaseOrderLine,
    IntakePurchaseOrderPart,
    IntakePurchaseOrderVendor,
)
from app.services.hank_intake_receiving_service import READY_STATUSES, _identifier, _quantity, _unit
from app.services.hank_intake_service import HankIntakeService
from app.services.hank_task_service import PLAN_ROW_LIMIT, HankTaskService, _digest, _row_values

_PRICE = re.compile(r"^(?:[0-9]+(?:\.[0-9]{1,6})?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,6})?)$")


def _price(value):
    text = (value or "").strip()
    if text.startswith("$"):
        text = text[1:].strip()
    if not _PRICE.fullmatch(text):
        return None
    try:
        number = Decimal(text.replace(",", ""))
        return float(number) if number < Decimal("10000000000") else None
    except InvalidOperation:
        return None


def _date(value):
    # Ambiguous day/month formats need employee review, never a locale guess.
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T00:00:00(?:\.0{1,6})?)?", value or ""):
            return date.fromisoformat(value[:10])
        return None
    except ValueError:
        return None


def _supported(value, evidence):
    if not value:
        return False
    excerpt = ' '.join(' '.join(proof.excerpt.split()).casefold() for proof in evidence)
    return bool(re.search(r'(?<!\w)' + re.escape(' '.join(value.split()).casefold()) + r'(?!\w)', excerpt))


def _financial_warnings(analysis, subtotal):
    warnings = []
    currencies = {field.value.strip().upper() for field in analysis.fields if field.name == 'currency' and field.value}
    if currencies - {'USD', 'US DOLLAR', 'US DOLLARS', '$'}:
        warnings.append(
            'Verify every reviewed price in ERP dollars; currency conversion is not performed automatically.'
        )
    totals = {
        _price(field.value)
        for field in analysis.fields
        if field.name == 'total' and field.confidence == 'high' and _supported(field.value, field.evidence)
    } - {None}
    if len(totals) == 1 and subtotal is not None and abs(next(iter(totals)) - subtotal) > 0.005:
        warnings.append(
            f'The printed total ({next(iter(totals)):.2f}) differs from the ERP line subtotal ({subtotal:.2f}). '
            'Tax, freight and other extra charges are not imported automatically; review them in Purchasing.'
        )
    return warnings


class HankIntakePurchaseOrderService:
    def __init__(self, db, user, company_id):
        self.db, self.user, self.company_id = db, user, company_id
        self.intake = HankIntakeService(db, user, company_id)
        self.tasks = HankTaskService(db, user, company_id)

    def can_ready_for_receiving(self):
        elevated = self.user.is_superuser or self.user.role == UserRole.PLATFORM_ADMIN
        return bool(
            (elevated or self.user.role in {UserRole.ADMIN, UserRole.MANAGER})
            and "purchasing:approve" in self.tasks._permissions()
            and not getattr(self.user, "_read_only_company_context", False)
        )

    def source(self, file_id, *, version=None, locked=False):
        row = self.intake.file(file_id, locked=locked)
        self.tasks._require_action("draft_purchase_order")
        if row.status not in READY_STATUSES or not row.analysis_json:
            raise HTTPException(409, "Wait for document analysis before preparing a purchase order.")
        if version is not None and row.version != version:
            raise HTTPException(409, "The intake source changed. Refresh the document and review the order again.")
        analysis = IntakeExtraction.model_validate(row.analysis_json)
        if analysis.classification != "purchase_order":
            raise HTTPException(409, "This source is not classified as a purchase order. Review the document first.")
        if getattr(analysis, "has_more_lines", False):
            raise HTTPException(409, "This order has unextracted lines. Analyze a complete order before importing.")
        return row

    def prior_tasks(self, source):
        file_ids = select(HankIntakeFile.id).where(
            HankIntakeFile.company_id == self.company_id, HankIntakeFile.content_sha256 == source.content_sha256
        )
        return (
            tenant_query(self.db, HankTask, self.company_id)
            .filter(
                HankTask.kind == "draft_purchase_order",
                HankTask.status == "completed",
                HankTask.input_json["source_intake_file_id"].as_integer().in_(file_ids),
            )
            .order_by(HankTask.id)
        )

    def matching_orders(self, po_number):
        if not po_number:
            return []
        # Include deleted POs: deleting an imported order must not make its
        # document eligible to create another financial commitment.
        return (
            tenant_query(self.db, PurchaseOrder, self.company_id)
            .filter(func.lower(func.trim(PurchaseOrder.po_number)) == _identifier(po_number))
            .order_by(PurchaseOrder.id)
            .limit(PLAN_ROW_LIMIT + 1)
            .all()
        )

    def _prior(self, source):
        rows = self.prior_tasks(source).limit(PLAN_ROW_LIMIT + 1).all()
        if len(rows) > PLAN_ROW_LIMIT:
            raise HTTPException(409, "Too many prior imports to review safely. Use Purchasing to review this order.")
        return rows

    def validate_import(self, data, *, locked=False):
        if locked:
            # Same generator lock as ordinary PO creation. Lock before all
            # source/vendor/part rows so distinct-source duplicate numbers race
            # through one serialized check, including case-only differences.
            acquire_generator_lock(self.db, "po_number", self.company_id)
        source = self.source(data["source_intake_file_id"], version=data["source_intake_version"], locked=locked)
        if data.get("ready_for_receiving") and not self.can_ready_for_receiving():
            raise HTTPException(403, "Recording an imported PO as ready for receiving requires purchasing approval.")
        if self.matching_orders(data["po_number"]):
            raise HTTPException(409, "This purchase order number already exists. Open the existing PO in Purchasing.")
        if self._prior(source):
            raise HTTPException(409, "This document has already created a purchase order. Open the existing order.")
        analysis = IntakeExtraction.model_validate(source.analysis_json)
        if len(data["lines"]) != len(analysis.lines) or not analysis.lines:
            raise HTTPException(409, "Review every source line before importing the complete purchase order.")
        if {line["source_line_index"] for line in data["lines"]} != set(range(len(analysis.lines))):
            raise HTTPException(409, "Each extracted source line must appear exactly once in the reviewed order.")
        if any(
            Decimal(str(line[key])) >= Decimal('10000000000')
            for line in data['lines']
            for key in ('quantity_ordered', 'unit_price')
        ):
            raise HTTPException(422, 'An imported quantity or unit price exceeds the supported review limit.')
        return source, {"intake_source_sha256": _digest(_row_values(source))}

    def review_warnings(self, source, data):
        subtotal = sum(float(line['quantity_ordered']) * float(line['unit_price']) for line in data['lines'])
        return _financial_warnings(IntakeExtraction.model_validate(source.analysis_json), subtotal)

    def draft(self, file_id):
        source = self.source(file_id)
        analysis = IntakeExtraction.model_validate(source.analysis_json)
        warnings = list(analysis.warnings)
        fields = {}
        for field in analysis.fields:
            if field.value:
                if field.name in fields and (fields[field.name] is None or fields[field.name].value != field.value):
                    fields[field.name] = None
                    warnings.append(f'Conflicting {field.name.replace("_", " ")} values require review.')
                elif field.name not in fields:
                    fields[field.name] = field

        def value(name):
            field = fields.get(name)
            if not field or field.confidence != "high" or not _supported(field.value, field.evidence):
                return None
            return field.value.strip() if field.value else None

        po_number = value("po_number") if "po_number" in fields else value("document_number")
        if not po_number or len(po_number) > 50:
            po_number = None
            warnings.append("Review and enter the original purchase order number.")
        supplier = value("vendor_name")
        vendors = (
            tenant_query(self.db, Vendor, self.company_id)
            .filter(
                Vendor.is_active.is_(True),
                Vendor.is_deleted.is_(False),
                or_(
                    func.lower(func.trim(Vendor.name)) == _identifier(supplier),
                    func.lower(func.trim(Vendor.code)) == _identifier(supplier),
                ),
            )
            .order_by(Vendor.id)
            .limit(26)
            .all()
            if supplier
            else []
        )
        vendor_id = vendors[0].id if len(vendors) == 1 else None
        if vendor_id is None:
            warnings.append("Select the supplier from existing active vendors; the printed name is not a unique match.")
        lines = []
        for index, line in enumerate(analysis.lines):
            line_warnings = []
            matches = (
                tenant_query(self.db, Part, self.company_id)
                .filter(
                    Part.is_active.is_(True),
                    Part.is_deleted.is_(False),
                    func.lower(func.trim(Part.part_number)) == _identifier(line.part_number),
                )
                .order_by(Part.id)
                .limit(26)
                .all()
                if line.part_number
                else []
            )
            trusted = line.confidence == "high" and bool(line.evidence)
            selected = (
                matches[0] if trusted and _supported(line.part_number, line.evidence) and len(matches) == 1 else None
            )
            quantity = _quantity(line.quantity) if trusted and _supported(line.quantity, line.evidence) else None
            price = _price(line.unit_price) if trusted and _supported(line.unit_price, line.evidence) else None
            if not selected:
                line_warnings.append("Select an existing active part; the source does not provide one certain match.")
            if not _supported(line.unit_of_measure, line.evidence) or (
                selected and _unit(line.unit_of_measure) != _unit(selected.unit_of_measure)
            ):
                quantity = None
                price = None
                line_warnings.append(
                    "Verify both quantity and unit price in the selected part’s stocking units; no unit conversion is inferred."
                )
            if quantity is None:
                line_warnings.append("Review and enter the ordered quantity.")
            if price is None:
                line_warnings.append("Review and enter the unit price.")
            lines.append(
                IntakePurchaseOrderLine(
                    **line.model_dump(),
                    source_line_index=index,
                    part_id=selected.id if selected else None,
                    candidates=[
                        IntakePurchaseOrderPart(
                            id=part.id,
                            part_number=part.part_number,
                            name=part.name,
                            unit_of_measure=uom_label(part.unit_of_measure),
                        )
                        for part in matches[:25]
                    ],
                    quantity_ordered=quantity,
                    unit_price_amount=price,
                    warnings=line_warnings,
                )
            )
        order_date, required_date = _date(value("date")), _date(value("due_date"))
        for field, parsed in [("date", order_date), ("due_date", required_date)]:
            if fields.get(field) and parsed is None:
                warnings.append(f'Review the printed {field.replace("_", " ")}; its date is uncertain or ambiguous.')
        financial_warnings = _financial_warnings(
            analysis,
            (
                sum(line.quantity_ordered * line.unit_price_amount for line in lines)
                if all(line.quantity_ordered is not None and line.unit_price_amount is not None for line in lines)
                else None
            ),
        )
        warnings.extend(financial_warnings)
        if any('currency' in warning for warning in financial_warnings):
            for proposed in lines:
                proposed.unit_price_amount = None
        existing = self.matching_orders(po_number)
        prior = self._prior(source)
        if prior:
            # PO references are tenant-visible purchasing records. Never return
            # private intake/task IDs from another employee's upload.
            ids = {
                ref["id"]
                for task in prior
                for ref in (task.result_json or {}).get("references", [])
                if ref.get("type") == "purchase_order"
            }
            existing_by_id = {row.id: row for row in existing}
            for row in tenant_query(self.db, PurchaseOrder, self.company_id).filter(PurchaseOrder.id.in_(ids)).all():
                existing_by_id[row.id] = row
            existing = list(existing_by_id.values())
        _, _, duplicate_upload = self.intake._duplicates(source)
        blocked = None
        if existing or prior:
            blocked = "This PO number or source document has already been imported. Open the existing order."
        elif not lines:
            blocked = "No complete line items were extracted. Review and analyze the complete purchase order."
        return IntakePurchaseOrderDraft(
            file_id=source.id,
            file_version=source.version,
            company_id=self.company_id,
            filename=source.filename,
            po_number=po_number,
            order_date=order_date,
            required_date=required_date,
            vendor_id=vendor_id,
            vendors=[
                IntakePurchaseOrderVendor(
                    id=row.id, code=row.code, name=row.name, reason="Exact printed supplier name or code."
                )
                for row in vendors[:25]
            ],
            lines=lines,
            warnings=warnings,
            has_duplicates=duplicate_upload or bool(existing or prior),
            blocked_reason=blocked,
            existing_purchase_orders=[
                IntakeExistingPurchaseOrder(id=row.id, po_number=row.po_number, href=f"/purchasing?po={row.id}")
                for row in existing[:25]
            ],
            can_ready_for_receiving=self.can_ready_for_receiving(),
        )
