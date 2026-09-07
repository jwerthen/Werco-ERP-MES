"""Review and atomically create unissued supply drafts from current MRP shortages."""

import hashlib
import json
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.db.locks import acquire_generator_lock
from app.models.mrp import MRPAction, MRPRun, MRPRunStatus, MRPSupplyLink, PlanningAction
from app.models.part import Part, is_material_supply_part_type
from app.models.process_sheet import ProcessSheet
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.routing import Routing
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.mrp import MRPSupplyDraftRequest, MRPSupplyDraftResponse
from app.schemas.purchasing import POCreate, POLineCreate
from app.schemas.work_order import WorkOrderCreate, WorkOrderOperationCreate
from app.services.audit_service import AuditService
from app.services.mrp_service import MRPService
from app.services.operational_event_service import OperationalEventService


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


class MRPSupplyService:
    def __init__(self, db: Session, company_id: int):
        self.db = db
        self.company_id = company_id

    def _action(self, action_id: int, lock: bool = False) -> MRPAction:
        query = self.db.query(MRPAction).filter(MRPAction.id == action_id, MRPAction.company_id == self.company_id)
        action = (query.with_for_update() if lock else query).first()
        if not action:
            raise HTTPException(404, 'MRP recommendation not found')
        return action

    def _link(self, action_id: int):
        return (
            self.db.query(MRPSupplyLink)
            .filter(MRPSupplyLink.company_id == self.company_id, MRPSupplyLink.action_id == action_id)
            .first()
        )

    def result(self, link: MRPSupplyLink, replayed: bool = False) -> MRPSupplyDraftResponse:
        po = link.purchase_order
        wo = link.work_order
        record = po or wo
        return MRPSupplyDraftResponse(
            action_id=link.action_id,
            mrp_run_id=link.action.mrp_run_id,
            kind='purchase_order' if po else 'work_order',
            id=record.id,
            number=po.po_number if po else wo.work_order_number,
            url=f'/purchasing?po={po.id}' if po else f'/work-orders/{wo.id}',
            quantity=link.quantity,
            status=record.status.value,
            replayed=replayed,
        )

    def review(self, action_id: int) -> dict:
        action = self._action(action_id)
        run = self.db.query(MRPRun).filter(MRPRun.id == action.mrp_run_id, MRPRun.company_id == self.company_id).first()
        if not run:
            raise HTTPException(404, 'MRP run not found')
        latest = (
            self.db.query(MRPRun)
            .filter(MRPRun.company_id == self.company_id, MRPRun.status == MRPRunStatus.COMPLETE)
            .order_by(MRPRun.completed_at.desc(), MRPRun.id.desc())
            .first()
        )
        part = self.db.query(Part).filter(Part.id == action.part_id, Part.company_id == self.company_id).first()
        if not part or part.is_deleted or not part.is_active:
            raise HTTPException(409, 'This part is no longer available for supply planning. Refresh MRP.')
        link = self._link(action_id)
        if action.action_type not in (PlanningAction.ORDER, PlanningAction.MANUFACTURE, PlanningAction.EXPEDITE):
            raise HTTPException(409, 'This recommendation requires a schedule/review action, not a new supply draft.')
        kind = 'purchase_order' if is_material_supply_part_type(part.part_type) else 'work_order'
        planner = MRPService(self.db, self.company_id)
        horizon = run.created_at.date() + timedelta(days=run.planning_horizon_days)
        requirements = [
            r for r in planner.get_work_order_requirements(horizon, run.include_allocated) if r['part_id'] == part.id
        ]
        aggregated = planner.aggregate_requirements(requirements)
        _, fresh_actions = planner.calculate_shortages_and_actions(aggregated, run.include_safety_stock)
        current = next((row for row in fresh_actions if row.required_date == action.required_date), None)
        quantity = float(current.quantity) if current else 0.0
        # Use the same BOM traversal as normal WO creation; preview every copied
        # component routing as well as the parent's released routing.
        from app.api.endpoints.work_orders import _collect_bom_components, _get_active_bom

        bom = _get_active_bom(self.db, part.id, self.company_id) if kind == 'work_order' else None
        components = _collect_bom_components(self.db, bom, self.company_id) if bom else []
        route_part_ids = list(dict.fromkeys([component.id for _, component, _ in components] + [part.id]))
        routings = (
            self.db.query(Routing)
            .options(joinedload(Routing.operations))
            .filter(
                Routing.company_id == self.company_id,
                Routing.part_id.in_(route_part_ids),
                Routing.is_active == True,
                Routing.status == 'released',
            )
            .order_by(Routing.id)
            .all()
        )
        routing_by_part = {route.part_id: route for route in routings}
        routing_ops = [
            op
            for pid in route_part_ids
            if pid in routing_by_part
            for op in sorted(routing_by_part[pid].operations, key=lambda op: op.sequence)
            if op.is_active
        ]
        centers = (
            self.db.query(WorkCenter)
            .filter(WorkCenter.company_id == self.company_id, WorkCenter.is_active == True)
            .order_by(WorkCenter.name)
            .all()
        )
        center_by_id = {center.id: center for center in centers}
        routing_detail = [
            dict(
                id=op.id,
                sequence=op.sequence,
                name=op.name,
                work_center_id=op.work_center_id,
                work_center_name=(
                    center_by_id[op.work_center_id].name if op.work_center_id in center_by_id else 'Unavailable'
                ),
                setup_hours=op.setup_hours,
                run_hours_per_unit=op.run_hours_per_unit,
                process_sheet_id=op.process_sheet_id,
            )
            for op in routing_ops
        ]
        vendors = (
            self.db.query(Vendor)
            .filter(Vendor.company_id == self.company_id, Vendor.is_active == True, Vendor.is_deleted == False)
            .order_by(Vendor.name)
            .all()
        )
        vendor_ids = {vendor.id for vendor in vendors}
        vendor_id = part.primary_supplier_id if part.primary_supplier_id in vendor_ids else None
        if vendor_id is None:
            from app.models.supplier_part import SupplierPartMapping

            mapping = (
                self.db.query(SupplierPartMapping)
                .filter(
                    SupplierPartMapping.company_id == self.company_id,
                    SupplierPartMapping.part_id == part.id,
                    SupplierPartMapping.is_active == True,
                    SupplierPartMapping.vendor_id.in_(vendor_ids),
                )
                .order_by(SupplierPartMapping.id)
                .first()
            )
            vendor_id = mapping.vendor_id if mapping else None
        sheet_ids = [op.process_sheet_id for op in routing_ops if op.process_sheet_id]
        sheet_families = self.db.query(ProcessSheet.sheet_number).filter(
            ProcessSheet.company_id == self.company_id, ProcessSheet.id.in_(sheet_ids)
        )
        sheets = (
            self.db.query(ProcessSheet)
            .filter(ProcessSheet.company_id == self.company_id, ProcessSheet.sheet_number.in_(sheet_families))
            .order_by(ProcessSheet.id)
            .all()
        )
        blocked_reason = None
        if not latest or latest.id != run.id:
            blocked_reason = 'A newer MRP run is available. Review its current recommendation before creating supply.'
        elif quantity <= 0:
            blocked_reason = (
                'Current inventory and planned supply cover this shortage. Refresh MRP before creating more supply.'
            )
        elif kind == 'work_order' and any(r.is_deleted for r in routings):
            blocked_reason = 'A released routing is deleted. Correct the routing before creating this draft.'
        elif kind == 'work_order' and any(op.work_center_id not in center_by_id for op in routing_ops):
            blocked_reason = 'The released routing contains an unavailable work center. Correct the routing first.'
        fingerprint = dict(
            action=action.id,
            run=run.id,
            latest=latest.id if latest else None,
            part=[part.id, planner._enum_value(part.part_type), part.revision, str(part.updated_at)],
            required_date=action.required_date,
            quantity=quantity,
            inventory=planner.get_inventory_summary(part.id),
            requirements=requirements,
            routing=routing_detail,
            routing_source=[
                {column.name: getattr(op, column.name) for column in op.__table__.columns} for op in routing_ops
            ],
            process_sheets=[
                dict(
                    id=sheet.id,
                    version=sheet.version,
                    status=sheet.status,
                    active=sheet.is_active,
                    deleted=sheet.is_deleted,
                )
                for sheet in sheets
            ],
            routing_versions=[
                dict(id=r.id, revision=r.revision, updated=str(r.updated_at), deleted=r.is_deleted) for r in routings
            ],
            bom_components=[dict(item=item.id, part=component.id, quantity=qty) for item, component, qty in components],
        )
        return dict(
            action_id=action.id,
            mrp_run_id=run.id,
            mrp_run_number=run.run_number,
            source_url=f'/mrp?run={run.id}&action={action.id}',
            kind=kind,
            part_id=part.id,
            part_number=part.part_number,
            part_name=part.name,
            source_quantity=float(action.quantity),
            quantity=quantity,
            required_date=action.required_date,
            due_date=max(date.today(), action.required_date),
            review_token=_digest(fingerprint),
            blocked_reason=blocked_reason,
            vendor_id=vendor_id,
            unit_price=float(part.standard_cost or 0),
            vendors=[dict(id=v.id, code=v.code, name=v.name) for v in vendors],
            work_center_id=routing_ops[0].work_center_id if routing_ops else None,
            work_centers=[dict(id=c.id, code=c.code, name=c.name) for c in centers],
            routing=routing_detail,
            routing_source=[
                {column.name: getattr(op, column.name) for column in op.__table__.columns} for op in routing_ops
            ],
            process_sheets=[
                dict(
                    id=sheet.id,
                    version=sheet.version,
                    status=sheet.status,
                    active=sheet.is_active,
                    deleted=sheet.is_deleted,
                )
                for sheet in sheets
            ],
            existing_draft=self.result(link).model_dump() if link else None,
        )

    def create(self, action_id: int, payload: MRPSupplyDraftRequest, user: User, audit: AuditService):
        acquire_generator_lock(self.db, 'mrp_planning', self.company_id)
        action = self._action(action_id, lock=True)
        self.db.query(Part.id).filter(
            Part.id == action.part_id, Part.company_id == self.company_id
        ).with_for_update().first()
        canonical = payload.model_dump(mode='json', exclude={'request_key', 'review_token'})
        canonical['action_id'] = action_id
        request_hash = _digest(canonical)
        existing = (
            self.db.query(MRPSupplyLink)
            .filter(
                MRPSupplyLink.company_id == self.company_id,
                or_(MRPSupplyLink.action_id == action_id, MRPSupplyLink.request_key == payload.request_key),
            )
            .first()
        )
        if existing:
            if existing.action_id == action_id and existing.request_hash == request_hash:
                return self.result(existing, replayed=True)
            result = self.result(existing)
            raise HTTPException(
                409,
                dict(
                    message='A supply draft already exists for this recommendation or retry key. Open it before creating more supply.',
                    existing_draft=result.model_dump(),
                ),
            )
        # Recover links made by the legacy auto-draft job, which predates retry keys.
        if action.result_po_id or action.result_wo_id:
            raise HTTPException(409, 'This recommendation already has a supply document. Refresh the run to open it.')
        review = self.review(action_id)
        if review['blocked_reason'] or payload.review_token != review['review_token']:
            raise HTTPException(
                409,
                dict(
                    message=review['blocked_reason']
                    or 'The shortage or routing changed. Reload the review before creating a draft.',
                    code='MRP_REVIEW_STALE',
                ),
            )
        if float(payload.quantity) > review['quantity'] + 1e-9:
            raise HTTPException(409, 'Draft quantity exceeds the current shortage. Reload the review.')
        if payload.due_date < date.today():
            raise HTTPException(
                422, 'Draft due date cannot be in the past. Select a new date for this overdue requirement.'
            )
        notes = f"MRP {review['mrp_run_number']} / action {action.id} / {review['source_url']}\n{payload.notes}".strip()
        part = self.db.get(Part, action.part_id)
        if review['kind'] == 'purchase_order':
            record = self._purchase(payload, part, notes, user, audit)
            action.result_po_id = record.id
        else:
            record = self._manufacture(payload, part, notes, review, user, audit)
            action.result_wo_id = record.id
        link = MRPSupplyLink(
            company_id=self.company_id,
            action_id=action.id,
            request_key=payload.request_key,
            request_hash=request_hash,
            quantity=float(payload.quantity),
            created_by=user.id,
            purchase_order_id=record.id if review['kind'] == 'purchase_order' else None,
            work_order_id=record.id if review['kind'] == 'work_order' else None,
        )
        self.db.add(link)
        self.db.flush()
        audit.log_create(
            'mrp_supply_link',
            link.id,
            f'MRP action {action.id}',
            new_values=link,
            extra_data={'mrp_run_id': action.mrp_run_id, 'draft_kind': review['kind'], 'draft_id': record.id},
        )
        return self.result(link)

    def _purchase(self, payload, part, notes, user, audit):
        from app.api.endpoints.purchasing import generate_po_number

        vendor = (
            self.db.query(Vendor)
            .filter(
                Vendor.id == payload.vendor_id,
                Vendor.company_id == self.company_id,
                Vendor.is_active == True,
                Vendor.is_deleted == False,
            )
            .first()
        )
        if not vendor:
            raise HTTPException(422, 'Choose an active supplier in this workspace.')
        data = POCreate(
            vendor_id=vendor.id,
            required_date=payload.due_date,
            notes=notes,
            lines=[
                POLineCreate(
                    part_id=part.id,
                    quantity_ordered=payload.quantity,
                    unit_price=payload.unit_price,
                    required_date=payload.due_date,
                )
            ],
        )
        po = PurchaseOrder(
            company_id=self.company_id,
            po_number=generate_po_number(self.db, self.company_id),
            vendor_id=data.vendor_id,
            required_date=data.required_date,
            notes=data.notes,
            created_by=user.id,
            status=POStatus.DRAFT,
            tax=0,
            shipping=0,
        )
        self.db.add(po)
        self.db.flush()
        amount = float(data.lines[0].quantity_ordered) * float(data.lines[0].unit_price)
        self.db.add(
            PurchaseOrderLine(
                company_id=self.company_id,
                purchase_order_id=po.id,
                line_number=1,
                part_id=part.id,
                quantity_ordered=float(payload.quantity),
                unit_price=float(payload.unit_price),
                line_total=amount,
                required_date=payload.due_date,
            )
        )
        po.subtotal = po.total = amount
        self.db.flush()
        audit.log_create(
            'purchase_order',
            po.id,
            po.po_number,
            new_values=po,
            extra_data={'vendor_code': vendor.code, 'line_count': 1, 'source': 'mrp_review'},
        )
        OperationalEventService(self.db).emit_best_effort(
            company_id=self.company_id,
            event_type='purchase_order_created',
            source_module='purchasing',
            entity_type='purchase_order',
            entity_id=po.id,
            user_id=user.id,
            severity='info',
            event_payload=dict(
                po_number=po.po_number,
                vendor_id=vendor.id,
                vendor_name=vendor.name,
                line_count=1,
                required_date=po.required_date.isoformat(),
                total=float(po.total),
                source='mrp_review',
            ),
        )
        return po

    def _manufacture(self, payload, part, notes, review, user, audit):
        from app.api.endpoints.work_orders import create_routing_operations_for_work_order, generate_work_order_number

        operations = []
        if not review['routing'] and not payload.work_center_id:
            raise HTTPException(422, 'No released routing is available. Choose a work center for the draft operation.')
        if not review['routing'] and payload.work_center_id:
            center = (
                self.db.query(WorkCenter)
                .filter(
                    WorkCenter.id == payload.work_center_id,
                    WorkCenter.company_id == self.company_id,
                    WorkCenter.is_active == True,
                )
                .first()
            )
            if not center:
                raise HTTPException(422, 'Choose an active work center in this workspace.')
            operations = [
                WorkOrderOperationCreate(
                    sequence=10, operation_number='10', name='Supply production', work_center_id=center.id
                )
            ]
        if review['routing'] and payload.work_center_id != review['work_center_id']:
            raise HTTPException(
                409, 'This draft uses the reviewed released routing. Reload its work-center assignments.'
            )
        try:
            data = WorkOrderCreate(
                part_id=part.id,
                quantity_ordered=payload.quantity,
                due_date=payload.due_date,
                notes=notes,
                operations=operations,
            )
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        wo = WorkOrder(
            company_id=self.company_id,
            work_order_number=generate_work_order_number(self.db, self.company_id),
            created_by=user.id,
            status=WorkOrderStatus.DRAFT,
            **data.model_dump(exclude={'operations', 'serial_numbers'}),
        )
        self.db.add(wo)
        self.db.flush()
        snapshots = []
        if operations:
            for operation in operations:
                self.db.add(
                    WorkOrderOperation(company_id=self.company_id, work_order_id=wo.id, **operation.model_dump())
                )
        else:
            snapshots = create_routing_operations_for_work_order(
                self.db, wo, part, float(payload.quantity), self.company_id
            )
        self.db.flush()
        audit.log_create(
            'work_order',
            wo.id,
            wo.work_order_number,
            new_values=wo,
            extra_data={
                'part_number': part.part_number,
                'quantity': float(payload.quantity),
                'auto_routing': not bool(operations),
                'process_sheet_snapshot': snapshots,
                'source': 'mrp_review',
            },
        )
        return wo
