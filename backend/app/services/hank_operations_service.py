"""Live, permission-filtered operational evidence. No reconciliation, mutation or model call."""

from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import func, or_

from app.core.time_utils import CENTRAL_TIME_ZONE, to_utc_iso
from app.db.tenant_filter import tenant_query
from app.models.bom import BOM, BOMItem
from app.models.document import Document, DocumentType
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.process_sheet import WOOperationStep
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine
from app.models.shipping import CertificateOfConformance, Shipment, ShipmentStatus
from app.models.time_entry import TimeEntry
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.schemas.hank_operations import HankOperationalCheck, HankOperationalReport
from app.services.hank_task_service import HankTaskService, _reference
from app.services.material_consumption_service import CONSUMABLE_ITEM_CLAUSES
from app.services.material_readiness_service import material_readiness
from app.services.process_sheet_service import step_counts_for_operations
from app.services.quality_gate_service import evaluate_completion_quality_exceptions
from app.services.shipment_commands import _allocated_quantity
from app.services.traceability_service import trace_lot, trace_serial

LIMIT = 50


def _value(value):
    return getattr(value, 'value', value)


def _text(value, limit=1800):
    text = str(value or '')
    return text if len(text) <= limit else text[:limit] + '…'


class HankOperationsService:
    def __init__(self, db, user, company_id):
        self.db, self.user, self.company_id = db, user, company_id
        self.permissions = HankTaskService(db, user, company_id)._permissions()
        self.now = datetime.now(timezone.utc)
        self.today = self.now.astimezone(CENTRAL_TIME_ZONE).date()

    def require(self, *permissions):
        if not set(permissions) <= self.permissions:
            raise HTTPException(403, 'Your current role cannot view this operational evidence')

    def work_order(self, work_order_id):
        self.require('work_orders:view')
        row = (
            tenant_query(self.db, WorkOrder, self.company_id)
            .filter(WorkOrder.id == work_order_id, WorkOrder.is_deleted.is_(False))
            .first()
        )
        if not row:
            raise HTTPException(404, 'Work order not found')
        return row

    def reference(self, wo):
        return _reference('work_order', wo.id, wo.work_order_number, f'/work-orders/{wo.id}')

    def report(self, title, summary, checks, notes=None, draft=None):
        return HankOperationalReport(
            company_id=self.company_id,
            checked_at=self.now,
            title=title,
            summary=summary,
            checks=checks,
            coverage_notes=notes or [],
            draft_text=draft,
        )

    def check(self, key, title, status, detail, references=None):
        return HankOperationalCheck(
            key=key, title=title, status=status, detail=_text(detail, 6000), references=references or []
        )

    def documents(self, wo):
        return (
            tenant_query(self.db, Document, self.company_id)
            .filter(
                or_(
                    Document.work_order_id == wo.id, (Document.part_id == wo.part_id) & Document.work_order_id.is_(None)
                ),
                Document.status == 'released',
            )
            .order_by(Document.id.desc())
            .limit(LIMIT)
            .all()
        )

    def document_ref(self, doc):
        return _reference(
            'document',
            doc.id,
            f'{doc.document_number} rev {doc.revision}: {doc.title}',
            f'/documents?document={doc.id}',
        )

    def readiness(self, work_order_id):
        wo = self.work_order(work_order_id)
        refs = [self.reference(wo)]
        ops = (
            tenant_query(self.db, WorkOrderOperation, self.company_id)
            .filter(WorkOrderOperation.work_order_id == wo.id)
            .order_by(WorkOrderOperation.id)
            .limit(201)
            .all()
        )
        if len(ops) > 200:
            raise HTTPException(409, 'This job has more than 200 operations; review it in Work Orders')
        checks = [
            self.check(
                'job_state',
                'Job state',
                (
                    'attention'
                    if wo.status in (WorkOrderStatus.DRAFT, WorkOrderStatus.ON_HOLD, WorkOrderStatus.CANCELLED)
                    else 'info'
                ),
                f'{wo.work_order_number}: {_value(wo.status)}; ordered {wo.quantity_ordered:g}; completed {float(wo.quantity_complete or 0):g}.',
                refs,
            )
        ]
        notes = [
            'This is evidence coverage, not authorization to start, complete or ship a job. Refresh before acting.',
            'Customer-specific requirements, physical setup and unrecorded evidence are not verified.',
        ]
        blockers = (
            tenant_query(self.db, WorkOrderBlocker, self.company_id)
            .filter(WorkOrderBlocker.work_order_id == wo.id, WorkOrderBlocker.status.in_(['open', 'acknowledged']))
            .order_by(WorkOrderBlocker.id)
            .limit(LIMIT + 1)
            .all()
        )
        checks.append(
            self.check(
                'blockers',
                'Open blockers',
                'attention' if blockers else 'satisfied',
                '\n'.join(f'{row.title}: {_text(row.note, 400)}' for row in blockers[:LIMIT])
                or 'No open or acknowledged blockers are recorded.',
                refs,
            )
        )
        docs = self.documents(wo)
        controlled = [
            doc
            for doc in docs
            if doc.document_type
            in (
                DocumentType.DRAWING,
                DocumentType.WORK_INSTRUCTION,
                DocumentType.SPECIFICATION,
                DocumentType.INSPECTION_PLAN,
            )
        ]
        steps = (
            tenant_query(self.db, WOOperationStep, self.company_id)
            .filter(WOOperationStep.work_order_operation_id.in_([op.id for op in ops]))
            .order_by(WOOperationStep.id)
            .limit(501)
            .all()
        )
        checks.append(
            self.check(
                'instructions',
                'Released instruction evidence',
                'info' if controlled or steps else 'unknown',
                f'{len(controlled)} released document(s) and {min(len(steps), 500)} traveler step snapshot(s) found. '
                'Metadata does not prove that every required drawing or instruction is present and current.',
                [self.document_ref(doc) for doc in controlled],
            )
        )
        counts = step_counts_for_operations(self.db, self.company_id, ops)
        required = sum(row['steps_total'] for row in counts.values())
        recorded = sum(row['steps_recorded'] for row in counts.values())
        checks.append(
            self.check(
                'traveler_evidence',
                'Required traveler evidence',
                'attention' if recorded < required else 'info' if required else 'unknown',
                f'{recorded} of {required} required traveler steps have current conforming records across required serials. '
                + (
                    'No required steps are recorded; coverage is not established.'
                    if not required
                    else 'Counts use the canonical completion predicate; they do not approve unrelated quality requirements.'
                ),
                refs,
            )
        )
        if 'quality:view' in self.permissions:
            exceptions = evaluate_completion_quality_exceptions(self.db, wo, None, self.company_id)
            checks.append(
                self.check(
                    'quality',
                    'Recorded quality exceptions',
                    'attention' if exceptions else 'info',
                    '\n'.join(row.message for row in exceptions[:LIMIT])
                    or 'No linked open NCR, unpassed FAI or open blocker detected. An absent FAI does not establish that FAI is not required.',
                    [
                        *refs,
                        *[
                            _reference(
                                row.reference_type,
                                row.reference_id,
                                row.message,
                                (
                                    f'/quality?tab=ncr&ncr={row.reference_id}'
                                    if row.reference_type == 'ncr'
                                    else (
                                        f'/quality?tab=fai&fai={row.reference_id}'
                                        if row.reference_type == 'fai'
                                        else f'/work-orders/{wo.id}'
                                    )
                                ),
                            )
                            for row in exceptions[:LIMIT]
                            if row.reference_id is not None
                        ],
                    ],
                )
            )
        else:
            checks.append(
                self.check(
                    'quality', 'Quality evidence', 'unknown', 'Quality source details are unavailable for your role.'
                )
            )
        incomplete = [op for op in ops if op.requires_inspection and not op.inspection_complete]
        checks.append(
            self.check(
                'inspections',
                'Required operation inspections',
                'attention' if incomplete else 'info',
                ', '.join(f'Operation {op.operation_number}: {op.name}' for op in incomplete)
                or 'No incomplete required operation inspection is recorded. This is not a quality sign-off.',
                refs,
            )
        )
        if {'inventory:view', 'purchasing:view'} <= self.permissions:
            data = material_readiness(self.db, self.company_id, [wo], self.today)['jobs'][wo.id]
            checks.append(
                self.check(
                    'materials',
                    'Material coverage',
                    'unknown' if data['status'] in ('unknown', 'not_defined') else 'info',
                    '\n'.join(
                        [
                            data['basis'],
                            f'Planning available date: {data["ready_date"] or "unverified"}.',
                            *[
                                f'{line["part_number"]}: need {line["required_quantity"]:g}; covered {line["covered_quantity"]:g}; '
                                f'short {line["shortage_quantity"]:g}. {line["reason"] or ""}'
                                for line in data['lines'][:LIMIT]
                            ],
                            *data['warnings'],
                        ]
                    ),
                    refs,
                )
            )
        else:
            checks.append(
                self.check(
                    'materials',
                    'Material coverage',
                    'unknown',
                    'The combined stock and supplier evidence requires inventory and purchasing view access.',
                )
            )
        if len(blockers) > LIMIT or len(docs) == LIMIT or len(steps) > 500:
            notes.append('Evidence lists reached their display bound; open the source workspace for full coverage.')
        return self.report(
            f'Readiness: {wo.work_order_number}', 'Known gaps and source evidence for this job.', checks, notes
        )

    def knowledge(self, work_order_id):
        wo = self.work_order(work_order_id)
        checks = [
            self.check(
                'current_notes',
                'Current planning notes',
                'info',
                '\n'.join(filter(None, [wo.notes, wo.special_instructions])) or 'No job planning notes recorded.',
                [self.reference(wo)],
            )
        ]
        prior = (
            tenant_query(self.db, WorkOrder, self.company_id)
            .filter(
                WorkOrder.part_id == wo.part_id,
                WorkOrder.id != wo.id,
                WorkOrder.is_deleted.is_(False),
                WorkOrder.status.in_([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED]),
            )
            .order_by(WorkOrder.updated_at.desc(), WorkOrder.id.desc())
            .limit(5)
            .all()
        )
        for row in prior:
            notes = (
                tenant_query(self.db, TimeEntry, self.company_id)
                .filter(TimeEntry.work_order_id == row.id, TimeEntry.notes.isnot(None))
                .order_by(TimeEntry.id.desc())
                .limit(5)
                .all()
            )
            checks.append(
                self.check(
                    f'prior:{row.id}',
                    f'Prior run {row.work_order_number}',
                    'info',
                    '\n'.join(
                        [
                            f'Completed {float(row.quantity_complete or 0):g}; scrap {float(row.quantity_scrapped or 0):g}.',
                            _text(row.notes),
                            *[_text(entry.notes, 700) for entry in notes],
                        ]
                    ),
                    [self.reference(row)],
                )
            )
        for doc in self.documents(wo):
            checks.append(
                self.check(
                    f'document:{doc.id}',
                    doc.title,
                    'info',
                    f'Released document {doc.document_number}, revision {doc.revision}. File contents are not summarized by this lookup.',
                    [self.document_ref(doc)],
                )
            )
        return self.report(
            f'Shop knowledge: {wo.work_order_number}',
            'Recorded instructions, documents and prior run notes.',
            checks,
            [
                'Prior notes are historical observations, not approved instructions for the current run.',
                'Up to five completed prior runs, five notes per run, and 50 released documents. No unrecorded experience is inferred.',
            ],
        )

    def impact(self, po_id):
        self.require('purchasing:view')
        po = (
            tenant_query(self.db, PurchaseOrder, self.company_id)
            .filter(PurchaseOrder.id == po_id, PurchaseOrder.is_deleted.is_(False))
            .first()
        )
        if not po:
            raise HTTPException(404, 'Purchase order not found')
        lines = (
            tenant_query(self.db, PurchaseOrderLine, self.company_id)
            .filter(
                PurchaseOrderLine.purchase_order_id == po.id,
                PurchaseOrderLine.is_closed.is_(False),
                PurchaseOrderLine.quantity_ordered > PurchaseOrderLine.quantity_received,
            )
            .order_by(PurchaseOrderLine.id)
            .limit(51)
            .all()
        )
        parts = {
            part.id: part
            for part in tenant_query(self.db, Part, self.company_id)
            .filter(Part.id.in_([line.part_id for line in lines]))
            .all()
        }
        reference = _reference('purchase_order', po.id, po.po_number, f'/purchasing?po={po.id}')
        checks = [
            self.check(
                'supplier',
                'Supplier confirmation',
                'attention' if not po.supplier_confirmed_date else 'info',
                f'Status {_value(po.status)}; requested {po.required_date or "not set"}; supplier confirmed '
                f'{po.supplier_confirmed_date or "not recorded"}; follow-up due {po.follow_up_due_date or "not set"}.',
                [reference],
            )
        ]
        for line in lines[:50]:
            part = parts.get(line.part_id)
            checks.append(
                self.check(
                    f'line:{line.id}',
                    f'Outstanding line {line.line_number}',
                    'attention',
                    f'{part.part_number if part else "Unavailable part"}: {line.quantity_ordered-line.quantity_received:g} outstanding; '
                    f'required {line.required_date or po.required_date or "not set"}.',
                    [reference],
                )
            )
        notes = [
            'Part matches identify potentially affected jobs, not reservations or a promised delivery impact.',
            'Direct material ties and direct active BOM components are considered; nested/implicit demand may be missing.',
        ]
        if 'inventory:view' in self.permissions:
            stock = (
                tenant_query(self.db, InventoryItem, self.company_id)
                .join(Part, Part.id == InventoryItem.part_id)
                .filter(
                    InventoryItem.part_id.in_([line.part_id for line in lines]),
                    *CONSUMABLE_ITEM_CLAUSES,
                    Part.company_id == self.company_id,
                    Part.is_deleted.is_(False),
                    Part.is_active.is_(True),
                    InventoryItem.quantity_on_hand > func.coalesce(InventoryItem.quantity_allocated, 0),
                    or_(
                        InventoryItem.expiration_date.is_(None), func.date(InventoryItem.expiration_date) >= self.today
                    ),
                )
                .order_by(InventoryItem.id)
                .limit(51)
                .all()
            )
            for item in stock[:50]:
                available = max(0, float(item.quantity_on_hand or 0) - max(0, float(item.quantity_allocated or 0)))
                part = parts.get(item.part_id)
                checks.append(
                    self.check(
                        f'alternative_stock:{item.id}',
                        'Same-part stock to review',
                        'info',
                        f'{part.part_number if part else "Part"}: {available:g} unallocated usable units at {item.location}; '
                        f'lot {item.lot_number or "not recorded"}; expiry {item.expiration_date or "not set"}. '
                        'This is not a reservation or approval to substitute a lot.',
                        [
                            _reference(
                                'inventory',
                                item.id,
                                f'Inventory record {item.id}: {item.lot_number or item.location}',
                                '/inventory',
                            )
                        ],
                    )
                )
            if len(stock) > 50:
                notes.append('More than 50 matching stock records; alternatives are partial.')
        alternatives = (
            self.db.query(PurchaseOrderLine, PurchaseOrder)
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .filter(
                PurchaseOrderLine.company_id == self.company_id,
                PurchaseOrder.company_id == self.company_id,
                PurchaseOrder.id != po.id,
                PurchaseOrder.is_deleted.is_(False),
                PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL]),
                PurchaseOrderLine.part_id.in_([line.part_id for line in lines]),
                PurchaseOrderLine.is_closed.is_(False),
                PurchaseOrderLine.quantity_ordered > PurchaseOrderLine.quantity_received,
            )
            .order_by(PurchaseOrderLine.id)
            .limit(51)
            .all()
        )
        for line, other in alternatives[:50]:
            part = parts.get(line.part_id)
            checks.append(
                self.check(
                    f'alternative_po:{line.id}',
                    f'Other open supply: {other.po_number}',
                    'info',
                    f'{part.part_number if part else "Part"}: {line.quantity_ordered-line.quantity_received:g} outstanding; '
                    f'supplier confirmed {other.supplier_confirmed_date or "not recorded"}; requested '
                    f'{line.required_date or other.required_date or "not set"}. Incoming quantity is not on-hand stock or reserved for this job.',
                    [_reference('purchase_order', other.id, other.po_number, f'/purchasing?po={other.id}')],
                )
            )
        if len(alternatives) > 50:
            notes.append('More than 50 other outstanding supply lines; alternatives are partial.')
        notes.append(
            'Alternative stock and supply are exact same-part candidates, not approved substitutions or reservations.'
        )
        if {'work_orders:view', 'boms:view', 'inventory:view'} <= self.permissions:
            ids = [line.part_id for line in lines]
            ties = tenant_query(self.db, WorkOrderMaterialAllocation, self.company_id).filter(
                WorkOrderMaterialAllocation.part_id.in_(ids),
                WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
            )
            bom_parts = (
                tenant_query(self.db, BOM, self.company_id)
                .join(BOMItem, BOMItem.bom_id == BOM.id)
                .filter(
                    BOMItem.company_id == self.company_id,
                    BOMItem.component_part_id.in_(ids),
                    BOM.is_deleted.is_(False),
                    BOM.is_active.is_(True),
                    BOM.status == 'released',
                )
                .with_entities(BOM.part_id)
            )
            jobs = (
                tenant_query(self.db, WorkOrder, self.company_id)
                .filter(
                    WorkOrder.is_deleted.is_(False),
                    WorkOrder.status.in_(
                        [
                            WorkOrderStatus.DRAFT,
                            WorkOrderStatus.RELEASED,
                            WorkOrderStatus.IN_PROGRESS,
                            WorkOrderStatus.ON_HOLD,
                        ]
                    ),
                    or_(
                        WorkOrder.id.in_(ties.with_entities(WorkOrderMaterialAllocation.work_order_id)),
                        WorkOrder.part_id.in_(bom_parts),
                    ),
                )
                .order_by(WorkOrder.due_date, WorkOrder.id)
                .limit(26)
                .all()
            )
            for wo in jobs[:25]:
                checks.append(
                    self.check(
                        f'impact:{wo.id}',
                        f'Potential impact: {wo.work_order_number}',
                        'info',
                        f'Active job uses a matching material; due {wo.due_date or "not set"}. Review its material coverage before changing the schedule.',
                        [self.reference(wo)],
                    )
                )
            if len(jobs) > 25:
                notes.append('More than 25 potentially affected jobs; this list is partial.')
        else:
            notes.append('Downstream job details require work order, BOM and inventory view access.')
        draft = (
            f'Subject: Delivery confirmation requested — {po.po_number}\n\n'
            f'Please confirm the delivery date and remaining quantities for {po.po_number}.\n'
            + '\n'.join(
                f'- Line {line.line_number}: {line.quantity_ordered-line.quantity_received:g} outstanding; '
                f'requested {line.required_date or po.required_date or "date not recorded"}.'
                for line in lines[:50]
            )
            + '\nPlease advise of any shortages or partial deliveries. Thank you.'
        )
        if len(lines) > 50:
            notes.append('Only the first 50 open lines are included in this report and draft.')
        notes.append('This follow-up is a draft for your review; no message has been sent or supplier promise changed.')
        return self.report(
            f'Purchasing impact: {po.po_number}',
            'Outstanding supply, possible downstream jobs and a draft follow-up.',
            checks,
            notes,
            draft,
        )

    def shipping_packet(self, work_order_id):
        self.require('shipping:view')
        wo = self.work_order(work_order_id)
        shipments = (
            tenant_query(self.db, Shipment, self.company_id)
            .filter(
                Shipment.work_order_id == wo.id,
                Shipment.is_deleted.is_(False),
                Shipment.status != ShipmentStatus.CANCELLED,
            )
            .order_by(Shipment.id.desc())
            .limit(51)
            .all()
        )
        available = max(0, float(wo.quantity_complete or 0) - _allocated_quantity(self.db, self.company_id, wo.id))
        checks = [
            self.check(
                'quantity',
                'Completed quantity and reservations',
                'info',
                f'Completed {float(wo.quantity_complete or 0):g}; available after all shipment allocations {available:g}. '
                'Creating a pending shipment reserves this quantity; it does not dispatch goods.',
                [self.reference(wo)],
            )
        ]
        for row in shipments[:50]:
            ref = _reference('shipment', row.id, row.shipment_number, '/shipping')
            coc = (
                tenant_query(self.db, CertificateOfConformance, self.company_id)
                .filter(CertificateOfConformance.shipment_id == row.id)
                .first()
            )
            checks.append(
                self.check(
                    f'shipment:{row.id}',
                    row.shipment_number,
                    'info',
                    f'Status {_value(row.status)}; quantity {row.quantity_shipped:g}; '
                    f'CoC {coc.coc_number if coc else "not issued"}; tracking {row.tracking_number or "not recorded"}.',
                    [ref, _reference('packing_slip', row.id, 'Packing slip', f'/print/packing-slip/{row.id}')],
                )
            )
        docs = self.documents(wo)
        checks.append(
            self.check(
                'documents',
                'Linked released documents',
                'info' if docs else 'unknown',
                'Review customer requirements against these linked files. An attachment is not certificate approval.',
                [self.document_ref(doc) for doc in docs],
            )
        )
        checks.append(
            self.check(
                'approval',
                'Final packing and quality review',
                'unknown',
                'An authorized employee must verify quality release, quantities, packaging, destination and customer document requirements.',
            )
        )
        return self.report(
            f'Shipping packet: {wo.work_order_number}',
            'A linked packet checklist of shipment records and existing evidence.',
            checks,
            [
                'No CoC is issued, goods dispatched, label purchased or pickup scheduled by this report.',
                'Documents are linked to their controlled source; no unsigned certificate is invented.',
                'Up to 50 shipments and 50 released documents are displayed; use Shipping for complete allocation totals.',
            ],
        )

    def trace(self, kind, value):
        self.require('inventory:view', 'quality:view', 'work_orders:view', 'purchasing:view')
        if not value.strip() or len(value) > 100:
            raise HTTPException(422, 'Enter a lot or serial number up to 100 characters')
        result = (
            trace_lot(value, self.db, self.company_id)
            if kind == 'lot'
            else trace_serial(value, self.db, self.company_id)
        )
        data = result.model_dump() if hasattr(result, 'model_dump') else result
        source = (
            tenant_query(self.db, InventoryItem, self.company_id)
            .filter(InventoryItem.lot_number == value if kind == 'lot' else InventoryItem.serial_number == value)
            .order_by(InventoryItem.id)
            .first()
        )
        origin = (
            [
                _reference(
                    'inventory',
                    source.id,
                    f'{kind.title()} {value}',
                    '/traceability?' + urlencode({'type': kind, 'number': value}),
                )
            ]
            if source
            else []
        )
        checks = [
            self.check(
                'identity',
                f'{kind.title()} record',
                'info',
                f'Part {data.get("part_number") or "not found"}; location {data.get("current_location") or "not recorded"}; '
                f'status {data.get("status") or "unknown"}; cert {data.get("cert_number") or "not recorded"}.',
                origin,
            )
        ]
        for index, event in enumerate(data.get('history', [])[-50:]):
            checks.append(
                self.check(
                    f'history:{index}',
                    str(event.get('event_type', 'Event')),
                    'info',
                    f'{to_utc_iso(event.get("timestamp"))}: {event.get("description") or event.get("reference") or ""}; '
                    f'quantity {event.get("quantity")}; location {event.get("location") or "not recorded"}.',
                )
            )
        for index, component in enumerate(data.get('consumed_components', [])[:50]):
            checks.append(
                self.check(
                    f'component:{index}',
                    'Consumed component genealogy',
                    'info',
                    f'{component.get("component_part_number")}: lot {component.get("lot_number")}; '
                    f'quantity {component.get("quantity")}; job {component.get("work_order_number")}.',
                )
            )
        return self.report(
            f'{kind.title()} trace: {value}',
            'Recorded inventory, receipt and quality genealogy.',
            checks,
            [
                'Latest 50 history events and first 50 component links. Historical evidence does not certify current acceptance.',
                'The recorded genealogy may be incomplete where source movements or serial identifiers were not captured.',
            ],
        )
