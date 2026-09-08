"""Reviewed purchase batches: one draft per vendor, one durable link per action.

The whole batch commits atomically. Existing link request keys carry a hashed batch
identity; every link stores the complete canonical request hash. This allows retries
without adding a header table or relying on process-local cache state.
"""

from collections import defaultdict
from datetime import date

from fastapi import HTTPException

from app.db.locks import acquire_generator_lock
from app.models.mrp import MRPSupplyLink
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.schemas.mrp import MRPPurchaseBatchResponse
from app.schemas.purchasing import POCreate, POLineCreate
from app.services.mrp_supply_service import MRPSupplyService, _digest
from app.services.operational_event_service import OperationalEventService


class MRPPurchaseBatchService(MRPSupplyService):
    @staticmethod
    def validate_ids(action_ids):
        if not 1 <= len(action_ids) <= 25 or len(set(action_ids)) != len(action_ids) or any(i <= 0 for i in action_ids):
            raise HTTPException(422, 'Select 1 to 25 distinct purchase recommendations.')

    def review_batch(self, action_ids):
        self.validate_ids(action_ids)
        reviews = [self.review(action_id) for action_id in sorted(action_ids)]
        if any(row['kind'] != 'purchase_order' for row in reviews):
            raise HTTPException(422, 'Only purchased material recommendations can be combined by supplier.')
        return dict(lines=reviews, max_lines=25)

    def batch_result(self, links, replayed=False):
        drafts = [self.result(link, replayed=replayed) for link in sorted(links, key=lambda row: row.action_id)]
        orders = {}
        for link in links:
            po = link.purchase_order
            if po is None or po.company_id != self.company_id:
                raise HTTPException(409, 'A batch supply association is unavailable. Review MRP before continuing.')
            if po.id not in orders:
                orders[po.id] = dict(
                    id=po.id,
                    number=po.po_number,
                    url=f'/purchasing?po={po.id}',
                    vendor_id=po.vendor_id,
                    action_ids=[],
                    total=float(po.total),
                    status=po.status.value,
                )
            orders[po.id]['action_ids'].append(link.action_id)
        return MRPPurchaseBatchResponse(drafts=drafts, purchase_orders=list(orders.values()), replayed=replayed)

    def create_batch(self, payload, user, audit):
        ids = [line.action_id for line in payload.lines]
        self.validate_ids(ids)
        lines = sorted(payload.lines, key=lambda line: line.action_id)
        # Shared by manual single drafts, auto drafts and new MRP runs. The order of
        # all subsequent row locks is stable across batch requests.
        acquire_generator_lock(self.db, 'mrp_planning', self.company_id)
        actions = {action_id: self._action(action_id, lock=True) for action_id in sorted(ids)}
        prefix = 'mrpb-' + _digest(payload.request_key) + '-'
        request_hash = _digest([line.model_dump(mode='json', exclude={'review_token'}) for line in lines])
        prior = (
            self.db.query(MRPSupplyLink)
            .filter(MRPSupplyLink.company_id == self.company_id, MRPSupplyLink.request_key.startswith(prefix))
            .all()
        )
        if prior:
            if {link.action_id for link in prior} == set(ids) and all(
                link.request_hash == request_hash for link in prior
            ):
                return self.batch_result(prior, replayed=True)
            raise HTTPException(
                409, 'This batch retry key already belongs to a different reviewed selection. Refresh MRP.'
            )
        overlaps = (
            self.db.query(MRPSupplyLink)
            .filter(MRPSupplyLink.company_id == self.company_id, MRPSupplyLink.action_id.in_(ids))
            .all()
        )
        if overlaps or any(action.result_po_id or action.result_wo_id for action in actions.values()):
            raise HTTPException(
                409,
                dict(
                    message='A selected recommendation already has supply. Refresh MRP and open its linked draft; no batch was created.',
                    existing_drafts=[self.result(link).model_dump() for link in overlaps],
                ),
            )
        parts = (
            self.db.query(Part)
            .filter(Part.company_id == self.company_id, Part.id.in_({a.part_id for a in actions.values()}))
            .order_by(Part.id)
            .with_for_update()
            .populate_existing()
            .all()
        )
        parts_by_id = {part.id: part for part in parts}
        vendors = (
            self.db.query(Vendor)
            .filter(
                Vendor.company_id == self.company_id,
                Vendor.id.in_({line.vendor_id for line in lines}),
                Vendor.is_active == True,
                Vendor.is_deleted == False,
            )
            .order_by(Vendor.id)
            .with_for_update()
            .populate_existing()
            .all()
        )
        vendor_by_id = {vendor.id: vendor for vendor in vendors}
        if set(vendor_by_id) != {line.vendor_id for line in lines}:
            raise HTTPException(422, 'Every line needs an active supplier in this workspace.')
        # Validate ALL live shortages before adding any supply, otherwise an early
        # group changes the planner inputs used to validate a later group.
        reviews = {review['action_id']: review for review in self.review_batch(ids)['lines']}
        by_vendor = defaultdict(list)
        quantities = defaultdict(float)
        for line in lines:
            review = reviews[line.action_id]
            if review['blocked_reason'] or line.review_token != review['review_token']:
                raise HTTPException(
                    409,
                    dict(
                        message=review['blocked_reason'] or 'A selected shortage changed. Reload the batch review.',
                        code='MRP_REVIEW_STALE',
                    ),
                )
            if float(line.quantity) > review['quantity'] + 1e-9:
                raise HTTPException(409, 'A draft quantity exceeds its current shortage. Reload the batch review.')
            if line.due_date < date.today():
                raise HTTPException(422, 'Draft due dates cannot be in the past.')
            quantities[review['part_id']] += float(line.quantity)
            if quantities[review['part_id']] > review['quantity'] + 1e-9:
                raise HTTPException(
                    409,
                    'Selected recommendations overlap the same material shortage. Review one current recommendation per part.',
                )
            by_vendor[line.vendor_id].append(line)
        links = []
        for vendor_id in sorted(by_vendor):
            vendor = vendor_by_id[vendor_id]
            group = by_vendor[vendor_id]
            data = POCreate(
                vendor_id=vendor_id,
                required_date=min(line.due_date for line in group),
                notes='Reviewed MRP purchase batch. Individual source recommendations are recorded on each line.',
                lines=[
                    POLineCreate(
                        part_id=actions[line.action_id].part_id,
                        quantity_ordered=line.quantity,
                        unit_price=line.unit_price,
                        required_date=line.due_date,
                        notes=f"MRP {reviews[line.action_id]['mrp_run_number']} / action {line.action_id}\n{line.notes}".strip(),
                    )
                    for line in group
                ],
            )
            from app.api.endpoints.purchasing import generate_po_number

            po = PurchaseOrder(
                company_id=self.company_id,
                po_number=generate_po_number(self.db, self.company_id),
                vendor_id=vendor_id,
                required_date=data.required_date,
                notes=data.notes,
                created_by=user.id,
                status=POStatus.DRAFT,
                tax=0,
                shipping=0,
            )
            self.db.add(po)
            self.db.flush()
            subtotal = 0.0
            for number, (line, validated) in enumerate(zip(group, data.lines), 1):
                amount = float(validated.quantity_ordered * validated.unit_price)
                subtotal += amount
                self.db.add(
                    PurchaseOrderLine(
                        company_id=self.company_id,
                        purchase_order_id=po.id,
                        line_number=number,
                        part_id=parts_by_id[validated.part_id].id,
                        quantity_ordered=float(validated.quantity_ordered),
                        unit_price=float(validated.unit_price),
                        line_total=amount,
                        required_date=validated.required_date,
                        notes=validated.notes,
                    )
                )
                action = actions[line.action_id]
                action.result_po_id = po.id
                link = MRPSupplyLink(
                    company_id=self.company_id,
                    action_id=action.id,
                    request_key=prefix + str(action.id),
                    request_hash=request_hash,
                    quantity=float(validated.quantity_ordered),
                    created_by=user.id,
                    purchase_order_id=po.id,
                )
                self.db.add(link)
                links.append(link)
            po.subtotal = po.total = subtotal
            self.db.flush()
            audit.log_create(
                'purchase_order',
                po.id,
                po.po_number,
                new_values=po,
                extra_data={
                    'vendor_code': vendor.code,
                    'line_count': len(group),
                    'source': 'mrp_batch_review',
                    'mrp_action_ids': [line.action_id for line in group],
                },
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
                    line_count=len(group),
                    required_date=po.required_date.isoformat(),
                    total=float(po.total),
                    source='mrp_batch_review',
                ),
            )
        self.db.flush()
        for link in links:
            audit.log_create(
                'mrp_supply_link',
                link.id,
                f'MRP action {link.action_id}',
                new_values=link,
                extra_data={'source': 'mrp_batch_review', 'draft_id': link.purchase_order_id},
            )
        return self.batch_result(links)
