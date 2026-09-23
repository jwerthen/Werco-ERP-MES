"""Source-bound previews and execution of canonical operational commands."""

from fastapi import HTTPException

from app.db.locks import acquire_generator_lock
from app.models.document import Document
from app.models.inventory import InventoryLocation
from app.models.part import Part, uom_label
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine
from app.models.scrap_reason import ScrapReasonCode
from app.models.shipping import Shipment
from app.models.time_entry import TimeEntry, TimeEntrySource
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from app.schemas.hank_operations import DraftShipmentInput, ReceiveDeliveryInput, ReportProductionInput
from app.schemas.hank_tasks import HankTaskPreview, HankTaskResult
from app.schemas.purchasing import DeliveryReceiptCreate
from app.schemas.shop_floor_commands import ProductionReportRequest
from app.services.hank_task_service import PLAN_ROW_LIMIT, _digest, _reference, _row_values
from app.services.receiving_delivery_service import post_delivery_command
from app.services.shipment_commands import _allocated_quantity, create_shipment_command
from app.services.shop_floor_commands import hold_operation_command, report_production_command

KINDS = {'receive_delivery', 'report_production', 'draft_shipment'}


def _fields(data):
    """Every persisted instruction/trace field is visible before employee execution."""
    return '; '.join(f'{key.replace("_", " ")}: {value}' for key, value in data.items() if value is not None)


class HankOperationalActions:
    def __init__(self, tasks):
        self.tasks = tasks
        self.db, self.user, self.company_id = tasks.db, tasks.user, tasks.company_id

    def rows(self, model, predicate, locked=False):
        return self.tasks._rows(model, predicate, locked=locked)

    def fingerprint(self, **groups):
        return {
            'operational_source': _digest({key: [_row_values(row) for row in rows] for key, rows in groups.items()})
        }

    def preview(self, kind, data, *, locked=False):
        if kind == 'receive_delivery':
            return self.receiving_preview(ReceiveDeliveryInput.model_validate(data), locked)
        if kind == 'report_production':
            return self.production_preview(ReportProductionInput.model_validate(data), locked)
        return self.shipment_preview(DraftShipmentInput.model_validate(data), locked)

    def receiving_preview(self, payload, locked):
        if locked:
            acquire_generator_lock(self.db, 'receipt_number', self.company_id)
        source_rows, prior_source_tasks, prior_receipts = [], [], []
        source_groups = {}
        source_warnings, source_references = [], []
        if payload.source_intake_file_id is not None:
            from app.services.hank_intake_receiving_service import HankIntakeReceivingService

            intake = HankIntakeReceivingService(self.db, self.user, self.company_id)
            source = intake.source(payload.source_intake_file_id, version=payload.source_intake_version, locked=locked)
            source_rows = [source]
            prior_source_tasks = intake.prior_tasks(source, completed_only=True).limit(PLAN_ROW_LIMIT + 1).all()
            if len(prior_source_tasks) > PLAN_ROW_LIMIT:
                raise HTTPException(
                    409, 'This source has too many prior receiving tasks for a bounded Hank review. Use Receiving.'
                )
            receipt_ids = set()
            for slip in {line.packing_slip_number for line in payload.lines if line.packing_slip_number}:
                for receipt in intake.matching_receipts(payload.purchase_order_id, slip):
                    if receipt.id not in receipt_ids:
                        prior_receipts.append(receipt)
                        receipt_ids.add(receipt.id)
            prior_receipts.sort(key=lambda receipt: receipt.id)
            if (prior_source_tasks or prior_receipts) and not payload.acknowledge_duplicate_source:
                raise HTTPException(
                    409,
                    'This PDF or packing slip already has receiving records. Review prior receipts and explicitly '
                    'acknowledge additional material before preparing another receiving task.',
                )
            source_warnings.append(
                'PDF extraction is evidence only. Verify quantities and traceability against delivered material.'
            )
            if payload.acknowledge_duplicate_source:
                source_warnings.append(
                    'Employee acknowledged this source may already have receipts and confirmed additional material.'
                )
            source_references.append(
                _reference('intake_file', source.id, source.filename, f'/?hank_work=intake&hank_id={source.id}')
            )
            source_groups = {
                'intake_source': source_rows,
                'prior_source_tasks': prior_source_tasks,
                'prior_receipts': prior_receipts,
            }
        orders = self.rows(PurchaseOrder, PurchaseOrder.id == payload.purchase_order_id, locked)
        if not orders or orders[0].is_deleted:
            raise HTTPException(404, 'Purchase order not found')
        po = orders[0]
        if po.status not in (POStatus.SENT, POStatus.PARTIAL):
            raise HTTPException(409, 'Receive against a sent or partially received purchase order')
        all_lines = self.rows(PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == po.id, locked)
        lookup = {line.id: line for line in all_lines}
        parts = self.rows(Part, Part.id.in_([line.part_id for line in all_lines]), locked)
        partmap = {part.id: part for part in parts}
        locations = self.rows(
            InventoryLocation,
            InventoryLocation.id.in_([line.location_id for line in payload.lines if line.location_id]),
            locked,
        )
        docs = self.rows(
            Document,
            Document.id.in_([line.certificate_document_id for line in payload.lines if line.certificate_document_id]),
            locked,
        )
        changes = []
        for proposed in payload.lines:
            line = lookup.get(proposed.po_line_id)
            if line is None:
                raise HTTPException(422, 'Every received line must belong to this purchase order')
            if line.is_closed:
                raise HTTPException(409, 'A selected purchase order line is already closed')
            part = partmap.get(line.part_id)
            if not part:
                raise HTTPException(404, 'Received part not found in this company')
            remaining = float(line.quantity_ordered or 0) - float(line.quantity_received or 0)
            if float(proposed.quantity_received) > remaining and not proposed.over_receive_approved:
                raise HTTPException(
                    409,
                    f'Line {line.line_number} exceeds remaining quantity {remaining:g}; review an explicit over-receipt approval',
                )
            if proposed.location_id and proposed.location_id not in {row.id for row in locations}:
                raise HTTPException(404, 'Receiving location not found')
            if proposed.certificate_document_id and proposed.certificate_document_id not in {row.id for row in docs}:
                raise HTTPException(404, 'Certificate not found')
            changes.append(
                f'Line {line.line_number}, {part.part_number}: receive {proposed.quantity_received} '
                f'{uom_label(part.unit_of_measure) or "(stocking unit unknown)"}; '
                f'{remaining:g} outstanding in that stocking unit. '
                + (
                    'Hold in incoming inspection; stock is not accepted.'
                    if proposed.requires_inspection
                    else 'Dock-to-stock: accept into inventory without recording a performed inspection.'
                )
            )
            changes.append(
                _fields(
                    proposed.model_dump(mode='json', exclude={'po_line_id', 'quantity_received', 'requires_inspection'})
                )
            )
        preview = HankTaskPreview(
            summary=f'Record {len(payload.lines)} delivery line(s) against {po.po_number}.',
            changes=changes,
            warnings=source_warnings
            + [
                'Lot numbers left blank are assigned from receipt numbers.',
                'Certificate attachment metadata is not certificate approval.',
                'No physical labels are printed by this Hank action. Use the saved receipt in Receiving to print.',
            ],
            references=source_references
            + [_reference('purchase_order', po.id, po.po_number, f'/purchasing?po={po.id}')],
        )
        return (
            f'Receive delivery for {po.po_number}',
            preview,
            self.fingerprint(
                po=orders,
                lines=all_lines,
                parts=parts,
                locations=locations,
                documents=docs,
                # Preserve fingerprints of manual receipt previews saved before
                # PDF sources were supported; their ERP sources remain sufficient.
                **source_groups,
            ),
        )

    def production_preview(self, payload, locked):
        operations = self.rows(WorkOrderOperation, WorkOrderOperation.id == payload.operation_id, locked)
        if not operations:
            raise HTTPException(404, 'Operation not found')
        operation = operations[0]
        wo = self.tasks._work_order(operation.work_order_id, locked=locked)
        if operation.status != OperationStatus.IN_PROGRESS:
            raise HTTPException(409, 'Report production only on an operation in progress')
        entries = self.rows(TimeEntry, (TimeEntry.operation_id == operation.id) & TimeEntry.clock_out.is_(None), locked)
        if not any(row.user_id == self.user.id for row in entries):
            raise HTTPException(409, 'You must be clocked into this operation before reporting production')
        reasons = (
            self.rows(ScrapReasonCode, ScrapReasonCode.id == payload.scrap_reason_code_id, locked)
            if payload.scrap_reason_code_id
            else []
        )
        if payload.scrap_reason_code_id and (not reasons or not reasons[0].is_active):
            raise HTTPException(422, 'Choose an active scrap reason in this company')
        changes = [
            f'{wo.work_order_number}, operation {operation.operation_number}: add {payload.quantity_complete_delta:g} good and '
            f'{payload.quantity_scrapped_delta:g} scrap.',
            _fields(
                payload.model_dump(
                    mode='json',
                    exclude={
                        'operation_id',
                        'request_id',
                        'source',
                        'hold',
                        'quantity_complete_delta',
                        'quantity_scrapped_delta',
                    },
                )
            ),
        ]
        warnings = ['Production reporting does not complete the operation or job.']
        if payload.hold:
            changes.append(
                'After recording production, place this operation on hold: '
                + _fields(payload.hold.model_dump(mode='json'))
            )
            warnings.append(
                f'The hold closes ALL {len(entries)} current time entries on this operation, including other operators. '
                f'Affected employee IDs: {", ".join(str(row.user_id) for row in entries)}. A blocker records the supplied reason.'
            )
        else:
            warnings.append('Your current time entry remains open.')
        return (
            f'Report production for {wo.work_order_number}',
            HankTaskPreview(
                summary='Record the reviewed production report.',
                changes=changes,
                warnings=warnings,
                references=[_reference('work_order', wo.id, wo.work_order_number, f'/work-orders/{wo.id}')],
            ),
            self.fingerprint(work_order=[wo], operation=operations, crew=entries, reasons=reasons),
        )

    def shipment_preview(self, payload, locked):
        wo = self.tasks._work_order(payload.work_order_id, locked=locked)
        if wo.status not in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED):
            raise HTTPException(409, 'Complete the work order before preparing a shipment')
        shipments = self.rows(Shipment, Shipment.work_order_id == wo.id, locked)
        available = max(0, float(wo.quantity_complete or 0) - _allocated_quantity(self.db, self.company_id, wo.id))
        if payload.quantity_shipped > available + 1e-9:
            raise HTTPException(409, f'Only {available:g} completed units remain available for shipment')
        return (
            f'Prepare shipment for {wo.work_order_number}',
            HankTaskPreview(
                summary=f'Create a pending shipment reserving {payload.quantity_shipped:g} completed units.',
                changes=[
                    _fields(payload.model_dump(mode='json')),
                    f'Effective recipient: {payload.ship_to_name or wo.customer_name or "not set"}.',
                ],
                warnings=[
                    'No goods are dispatched, stock decremented, certificate issued, carrier label purchased or pickup scheduled.',
                    'Review quality release, destination, packaging and the shipping packet before dispatch.',
                ],
                references=[_reference('work_order', wo.id, wo.work_order_number, f'/work-orders/{wo.id}')],
            ),
            self.fingerprint(work_order=[wo], shipments=shipments),
        )

    def execute(self, task, data, audit):
        if task.kind == 'receive_delivery':
            parsed = ReceiveDeliveryInput.model_validate(data)
            payload = DeliveryReceiptCreate(
                idempotency_key=f'hank_{task.request_key}',
                **parsed.model_dump(
                    exclude={'source_intake_file_id', 'source_intake_version', 'acknowledge_duplicate_source'}
                ),
            )
            result = post_delivery_command(self.db, self.user, self.company_id, payload, audit)
            return HankTaskResult(
                summary=f'Recorded {len(result["receipts"])} delivery receipt(s).',
                warnings=[
                    'Labels were not printed. Open Receiving to review the receipts and print labels.',
                    'Inspection and stock status follow each reviewed line’s inspection choice.',
                ],
                references=(
                    [
                        _reference(
                            'intake_file',
                            parsed.source_intake_file_id,
                            'Source PDF',
                            f'/?hank_work=intake&hank_id={parsed.source_intake_file_id}',
                        )
                    ]
                    if parsed.source_intake_file_id
                    else []
                )
                + [
                    _reference('receipt', row['id'], row['receipt_number'], '/receiving?tab=history')
                    for row in result['receipts']
                ],
            )
        if task.kind == 'report_production':
            parsed = ReportProductionInput.model_validate(data)
            payload = ProductionReportRequest.model_validate(
                {
                    **parsed.model_dump(exclude={'operation_id', 'hold', 'request_id', 'source'}),
                    'request_id': f'hank:{task.request_key}',
                    'source': TimeEntrySource.DESKTOP,
                }
            )
            result = report_production_command(self.db, self.user, self.company_id, parsed.operation_id, payload, audit)
            operation = self.db.get(WorkOrderOperation, parsed.operation_id)
            warnings = ['Operation and work-order completion were not requested.']
            if parsed.hold:
                hold_operation_command(self.db, self.user, self.company_id, operation.id, parsed.hold, audit)
                warnings.append('The operation is on hold; all its previously open time entries were closed.')
            else:
                warnings.append('Your time entry remains open.')
            references = [
                _reference(
                    'work_order',
                    operation.work_order_id,
                    operation.work_order.work_order_number,
                    f'/work-orders/{operation.work_order_id}',
                )
            ]
            if result.get('ncr'):
                row = result['ncr']
                references.append(_reference('ncr', row['id'], row['ncr_number'], f'/quality?ncr={row["id"]}'))
            return HankTaskResult(
                summary=f'Recorded {parsed.quantity_complete_delta:g} good and {parsed.quantity_scrapped_delta:g} scrap.',
                warnings=warnings,
                references=references,
            )
        shipment = create_shipment_command(
            self.db, self.user, self.company_id, DraftShipmentInput.model_validate(data), audit
        )
        return HankTaskResult(
            summary=f'Created pending shipment {shipment.shipment_number} for {shipment.quantity_shipped:g} units.',
            warnings=['Not dispatched. No carrier purchase, pickup or CoC issuance was performed.'],
            references=[
                _reference('shipment', shipment.id, shipment.shipment_number, '/shipping'),
                _reference('packing_slip', shipment.id, 'Packing slip', f'/print/packing-slip/{shipment.id}'),
            ],
        )
