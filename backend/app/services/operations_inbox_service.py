"""Live operational signals and shared triage; never resolves a business record."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import func, or_, tuple_

from app.db.locks import acquire_generator_lock
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.mrp import MRPAction, MRPRun, MRPRunStatus, PlanningAction
from app.models.operations_inbox import OperationalInboxState
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine
from app.models.quality import NCRStatus, NonConformanceReport
from app.models.role_permission import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.schemas.operations_inbox import InboxAssignee, OperationalInboxItem, OperationalInboxResponse
from app.schemas.work_order_blocker import WorkOrderBlockerUpdate
from app.services.work_order_blocker_service import WorkOrderBlockerService

# Every category is gated by the same module permission used by ERP navigation.
SOURCE_ACCESS = {
    'late_work_order': ('work_orders:view', 'work_orders:edit'),
    'blocker': ('work_orders:view', 'work_orders:edit'),
    'low_stock': ('inventory:view', 'inventory:adjust'),
    'quality_ncr': ('quality:view', 'quality:inspect'),
    'overdue_po_line': ('purchasing:view', 'purchasing:create'),
    'supplier_follow_up': ('purchasing:view', 'purchasing:create'),
    'mrp_shortage': ('inventory:view', 'inventory:adjust'),
}
SOURCE_ROLES = {
    'late_work_order': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'blocker': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'low_stock': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'quality_ncr': {UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY},
    'overdue_po_line': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'supplier_follow_up': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'mrp_shortage': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
}
SOURCE_LIMIT = 1000


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _occurrence(*facts):
    return hashlib.sha256(json.dumps(facts, default=str, sort_keys=True).encode()).hexdigest()


class OperationalInboxService:
    def __init__(self, db, user, company_id, now=None):
        self.db, self.user, self.company_id = db, user, company_id
        self.now = now or datetime.now(timezone.utc)
        self.overrides = {
            row.role: set(row.permissions or [])
            for row in db.query(RolePermission).filter(RolePermission.company_id == company_id).all()
        }
        company = db.query(Company).filter(Company.id == company_id).first()
        try:
            tz = ZoneInfo(company.timezone if company and company.timezone else 'America/Chicago')
        except ZoneInfoNotFoundError:
            tz = timezone.utc
        self.today = self.now.astimezone(tz).date()
        self.users = {
            u.id: u for u in db.query(User).filter(User.company_id == company_id, User.is_active.is_(True)).all()
        }

    def permissions(self, user):
        if user.is_superuser or user.role == UserRole.PLATFORM_ADMIN:
            return set(ALL_PERMISSIONS)
        return self.overrides.get(user.role, set(DEFAULT_ROLE_PERMISSIONS.get(user.role, [])))

    def can_view(self, kind, user=None):
        return SOURCE_ACCESS[kind][0] in self.permissions(user or self.user)

    def can_manage(self, kind):
        user = self.user
        return (
            not getattr(user, '_read_only_company_context', False)
            and self.can_view(kind)
            and SOURCE_ACCESS[kind][1] in self.permissions(user)
            and (user.is_superuser or user.role == UserRole.PLATFORM_ADMIN or user.role in SOURCE_ROLES[kind])
        )

    def _queries(self):
        cid = self.company_id
        active_wo = [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]
        stock = (
            self.db.query(
                InventoryItem.part_id.label('part_id'),
                func.coalesce(func.sum(InventoryItem.quantity_on_hand), 0).label('quantity'),
                func.max(InventoryItem.updated_at).label('updated'),
            )
            .filter(InventoryItem.company_id == cid, InventoryItem.is_active.is_(True))
            .group_by(InventoryItem.part_id)
            .subquery()
        )
        qty = func.coalesce(stock.c.quantity, 0)
        po_due = func.coalesce(
            PurchaseOrder.supplier_confirmed_date,
            PurchaseOrder.expected_date,
            PurchaseOrderLine.required_date,
            PurchaseOrder.required_date,
        )
        latest_mrp = (
            self.db.query(MRPRun.id)
            .filter(
                MRPRun.company_id == cid,
                MRPRun.status == MRPRunStatus.COMPLETE,
            )
            .order_by(MRPRun.completed_at.desc(), MRPRun.id.desc())
            .limit(1)
            .correlate(None)
            .scalar_subquery()
        )
        return {
            'mrp_shortage': (
                MRPAction,
                self.db.query(MRPAction, MRPRun, Part)
                .join(
                    MRPRun,
                    MRPRun.id == MRPAction.mrp_run_id,
                )
                .join(Part, Part.id == MRPAction.part_id)
                .filter(
                    MRPAction.company_id == cid,
                    MRPRun.company_id == cid,
                    Part.company_id == cid,
                    Part.is_deleted.is_(False),
                    Part.is_active.is_(True),
                    MRPAction.mrp_run_id == latest_mrp,
                    MRPAction.is_processed.is_(False),
                    MRPAction.quantity > 0,
                    MRPAction.action_type.in_(
                        [PlanningAction.ORDER, PlanningAction.MANUFACTURE, PlanningAction.EXPEDITE]
                    ),
                ),
            ),
            'late_work_order': (
                WorkOrder,
                self.db.query(WorkOrder).filter(
                    WorkOrder.company_id == cid,
                    WorkOrder.is_deleted.is_(False),
                    WorkOrder.status.in_(active_wo),
                    WorkOrder.due_date < self.today,
                ),
            ),
            'blocker': (
                WorkOrderBlocker,
                self.db.query(WorkOrderBlocker)
                .join(
                    WorkOrder,
                    WorkOrder.id == WorkOrderBlocker.work_order_id,
                )
                .filter(
                    WorkOrderBlocker.company_id == cid,
                    WorkOrder.company_id == cid,
                    WorkOrder.is_deleted.is_(False),
                    WorkOrder.status.in_(active_wo),
                    WorkOrderBlocker.status.in_(['open', 'acknowledged']),
                ),
            ),
            'low_stock': (
                Part,
                self.db.query(Part, qty, stock.c.updated)
                .outerjoin(stock, Part.id == stock.c.part_id)
                .filter(
                    Part.company_id == cid,
                    Part.is_active.is_(True),
                    Part.is_deleted.is_(False),
                    Part.reorder_point > 0,
                    qty < Part.reorder_point,
                ),
            ),
            'quality_ncr': (
                NonConformanceReport,
                self.db.query(NonConformanceReport).filter(
                    NonConformanceReport.company_id == cid,
                    NonConformanceReport.is_deleted.is_(False),
                    NonConformanceReport.status.in_(
                        [NCRStatus.OPEN, NCRStatus.UNDER_REVIEW, NCRStatus.PENDING_DISPOSITION]
                    ),
                ),
            ),
            'supplier_follow_up': (
                PurchaseOrder,
                self.db.query(PurchaseOrder).filter(
                    PurchaseOrder.company_id == cid,
                    PurchaseOrder.is_deleted.is_(False),
                    PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL]),
                    self.db.query(PurchaseOrderLine.id)
                    .filter(
                        PurchaseOrderLine.company_id == cid,
                        PurchaseOrderLine.purchase_order_id == PurchaseOrder.id,
                        PurchaseOrderLine.is_closed.is_(False),
                        PurchaseOrderLine.quantity_received < PurchaseOrderLine.quantity_ordered,
                    )
                    .exists(),
                    or_(
                        PurchaseOrder.supplier_acknowledged_at.is_(None), PurchaseOrder.follow_up_due_date <= self.today
                    ),
                ),
            ),
            'overdue_po_line': (
                PurchaseOrderLine,
                self.db.query(PurchaseOrderLine, PurchaseOrder, po_due)
                .join(
                    PurchaseOrder,
                    PurchaseOrder.id == PurchaseOrderLine.purchase_order_id,
                )
                .filter(
                    PurchaseOrder.company_id == cid,
                    PurchaseOrderLine.company_id == cid,
                    PurchaseOrder.is_deleted.is_(False),
                    PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL]),
                    PurchaseOrderLine.is_closed.is_(False),
                    func.coalesce(PurchaseOrderLine.quantity_received, 0) < PurchaseOrderLine.quantity_ordered,
                    po_due < self.today,
                ),
            ),
        }

    def _item(self, kind, row):
        record = row[0] if kind in ('low_stock', 'overdue_po_line', 'mrp_shortage') else row
        facts = [kind, record.id, getattr(record, 'updated_at', None)]
        owner = getattr(record, 'assigned_to', None) if kind in ('blocker', 'quality_ncr') else None
        severity = 'medium'
        if kind == 'supplier_follow_up':
            owner = record.follow_up_owner_id
            late = bool(record.follow_up_due_date and record.follow_up_due_date < self.today)
            title = f'{record.po_number}: ' + (
                'supplier follow-up overdue'
                if late
                else (
                    'awaiting supplier confirmation'
                    if not record.supplier_acknowledged_at
                    else 'supplier follow-up due'
                )
            )
            detail = f'Requested {record.required_date or "date unknown"}; supplier confirmed {record.supplier_confirmed_date or "date unknown"}. '
            detail += f'Follow up {record.follow_up_due_date or "date not set"}. {record.supplier_confirmation_note or "Record the supplier response and next follow-up."}'
            href, action = f'/purchasing?po={record.id}', 'Record supplier response and next follow-up'
            facts += [
                record.supplier_acknowledged_at,
                record.supplier_confirmed_date,
                record.follow_up_due_date,
                record.follow_up_owner_id,
            ]
            severity = 'high' if late else 'medium'
        elif kind == 'mrp_shortage':
            _, run, part = row
            title = f'{part.part_number}: projected material shortage'
            detail = f'{record.quantity:g} units required by {record.required_date}. Planning snapshot {run.run_number}; verify current supply before acting.'
            href = f'/mrp?run={run.id}&action={record.id}'
            action = 'Review projected shortage and supply options in MRP'
            if record.result_po_id or record.result_wo_id:
                detail += ' A supply draft is already linked.'
                action = 'Follow up the linked supply draft in MRP'
            facts += [
                run.completed_at,
                record.quantity,
                record.required_date,
                record.processed_at,
                record.result_po_id,
                record.result_wo_id,
            ]
            severity = (
                'high'
                if record.required_date < self.today or record.action_type == PlanningAction.EXPEDITE
                else 'medium'
            )
        elif kind == 'late_work_order':
            title = f'{record.work_order_number} is late'
            detail = f'Due {record.due_date}. {float(record.quantity_complete or 0):g} of {record.quantity_ordered:g} completed.'
            href, action = f'/work-orders/{record.id}', 'Review schedule and remaining operations'
            facts += [record.due_date, record.status, record.quantity_complete]
            severity = 'high'
        elif kind == 'blocker':
            title, detail = record.title, record.note or record.category.replace('_', ' ')
            href, action = f'/work-orders/{record.work_order_id}', 'Review blocker and resolve through the work order'
            facts += [record.status, record.reported_at, record.resolved_at]
            severity = 'high' if record.severity in ('critical', 'high') else 'medium'
        elif kind == 'low_stock':
            _, quantity, changed = row
            title = f'{record.part_number} below reorder point'
            detail = f'{float(quantity):g} on hand; reorder point {record.reorder_point:g}. Includes active stock in all inventory statuses.'
            href, action = '/warehouse?tab=inventory&filter=low_stock', 'Review stock, allocations and replenishment'
            facts += [quantity, changed, record.reorder_point]
            severity = 'high' if quantity <= (record.safety_stock or 0) else 'medium'
        elif kind == 'quality_ncr':
            title, detail = f'{record.ncr_number}: {record.title}', record.description
            href, action = f'/quality?tab=ncr&ncr={record.id}', 'Review containment and disposition in Quality'
            facts += [record.status, record.quantity_affected, record.quantity_rejected]
            severity = 'high'
        else:
            _, po, due = row
            title = f'{po.po_number} line {record.line_number} overdue'
            remaining = record.quantity_ordered - (record.quantity_received or 0)
            detail = f'{remaining:g} units not received; due {due}.'
            href, action = f'/purchasing?po={po.id}', 'Review supplier follow-up and receipt status'
            facts += [po.updated_at, po.status, due, record.quantity_received, record.quantity_ordered]
        return (
            OperationalInboxItem(
                key=f'{kind}:{record.id}',
                source_kind=kind,
                source_id=record.id,
                occurrence=_occurrence(*facts),
                title=title,
                detail=detail if len(detail) <= 1200 else detail[:1197] + '…',
                severity=severity,
                href=href,
                suggested_action=action,
                owner_id=owner,
                can_manage=self.can_manage(kind),
                acknowledged=kind == 'blocker' and record.status == 'acknowledged',
            ),
            record,
        )

    def collect(self, only_kind=None, only_id=None, lock=False):
        items, truncated = [], []
        for kind, (model, query) in self._queries().items():
            if (only_kind and kind != only_kind) or not self.can_view(kind):
                continue
            if only_id is not None:
                query = query.filter(model.id == only_id)
            if lock:
                query = query.with_for_update(of=model)
            rows = query.order_by(model.id.desc()).limit(SOURCE_LIMIT + 1).all()
            if len(rows) > SOURCE_LIMIT:
                truncated.append(kind)
            items.extend(self._item(kind, row) for row in rows[:SOURCE_LIMIT])
        return items, truncated

    def _apply_state(self, item, state):
        if state:
            if item.source_kind not in ('blocker', 'quality_ncr', 'supplier_follow_up'):
                item.owner_id = state.owner_id
            item.next_action, item.version = state.next_action, state.version
            item.acknowledged = state.acknowledged_occurrence == item.occurrence
            if (
                state.snoozed_occurrence == item.occurrence
                and _utc(state.snoozed_until)
                and _utc(state.snoozed_until) > self.now
            ):
                item.snoozed_until = _utc(state.snoozed_until)
        owner = self.users.get(item.owner_id)
        if owner and self.can_view(item.source_kind, owner):
            item.owner_name = owner.full_name
        else:
            item.owner_id = None
        return item

    def list(self):
        records, truncated = self.collect()
        states = {
            (s.source_kind, s.source_id): s
            for s in self.db.query(OperationalInboxState)
            .filter(
                OperationalInboxState.company_id == self.company_id,
                tuple_(OperationalInboxState.source_kind, OperationalInboxState.source_id).in_(
                    [(item.source_kind, item.source_id) for item, _ in records]
                ),
            )
            .all()
        }
        items = [self._apply_state(item, states.get((item.source_kind, item.source_id))) for item, _ in records]
        items.sort(key=lambda item: (item.severity != 'high', item.source_kind, -item.source_id))
        return OperationalInboxResponse(
            items=items,
            checked_at=self.now,
            truncated_sources=truncated,
            assignees=[
                InboxAssignee(id=u.id, name=u.full_name, sources=sources)
                for u in self.users.values()
                if (sources := [kind for kind in SOURCE_ACCESS if self.can_view(kind) and self.can_view(kind, u)])
            ],
        )

    def update(self, kind, source_id, data, audit):
        if kind not in SOURCE_ACCESS or not self.can_view(kind):
            raise HTTPException(404, 'Operational issue not found')
        if not self.can_manage(kind):
            raise HTTPException(403, 'You cannot manage this operational issue')
        # Serializes state creation as well as updates; unique constraint is the final backstop.
        acquire_generator_lock(self.db, f'inbox:{kind}:{source_id}', self.company_id)
        records, _ = self.collect(kind, source_id, lock=True)
        if not records:
            raise HTTPException(404, 'Issue no longer active; refresh the inbox')
        item, record = records[0]
        state = (
            self.db.query(OperationalInboxState)
            .filter(
                OperationalInboxState.company_id == self.company_id,
                OperationalInboxState.source_kind == kind,
                OperationalInboxState.source_id == source_id,
            )
            .with_for_update()
            .first()
        )
        if data.expected_version != (state.version if state else 0) or data.occurrence != item.occurrence:
            raise HTTPException(409, 'Issue changed. Refresh before updating its action.')
        changes = data.model_dump(exclude_unset=True)
        if 'owner_id' in changes and data.owner_id is not None:
            assignee = self.users.get(data.owner_id)
            if not assignee or not self.can_view(kind, assignee):
                raise HTTPException(422, 'Choose an active assignee in this company with access to this workflow')
        previous = self._apply_state(item.model_copy(), state).model_dump(mode='json')
        if state is None:
            state = OperationalInboxState(
                company_id=self.company_id,
                source_kind=kind,
                source_id=source_id,
                updated_by=self.user.id,
                version=0,
                next_action='',
            )
            self.db.add(state)
        if 'owner_id' in changes:
            if kind in ('blocker', 'quality_ncr'):
                audit.log_update(
                    'work_order_blocker' if kind == 'blocker' else 'ncr',
                    record.id,
                    item.title,
                    {'assigned_to': record.assigned_to},
                    {'assigned_to': data.owner_id},
                )
                record.assigned_to = data.owner_id
                record.updated_at = self.now.replace(tzinfo=None)
            elif kind == 'supplier_follow_up':
                audit.log_update(
                    'purchase_order',
                    record.id,
                    record.po_number,
                    {'follow_up_owner_id': record.follow_up_owner_id},
                    {'follow_up_owner_id': data.owner_id},
                )
                record.follow_up_owner_id = data.owner_id
                record.updated_at = self.now.replace(tzinfo=None)
            else:
                state.owner_id = data.owner_id
        if 'next_action' in changes:
            state.next_action = data.next_action.strip()
        if kind == 'blocker' and data.acknowledge is True and record.status == 'open':
            WorkOrderBlockerService(self.db).update_blocker(
                company_id=self.company_id,
                user=self.user,
                blocker_id=source_id,
                data=WorkOrderBlockerUpdate(status='acknowledged'),
                audit=audit,
            )
        self.db.flush()
        # Assignment/ack can change source.updated_at, so use the post-write occurrence.
        item, _ = self.collect(kind, source_id)[0][0]
        if previous['acknowledged']:
            state.acknowledged_occurrence = item.occurrence
        if previous['snoozed_until']:
            state.snoozed_occurrence = item.occurrence
        if data.acknowledge is not None:
            state.acknowledged_occurrence = item.occurrence if data.acknowledge else None
        if data.snooze_hours is not None:
            state.snoozed_occurrence = item.occurrence if data.snooze_hours else None
            state.snoozed_until = self.now + timedelta(hours=data.snooze_hours) if data.snooze_hours else None
        state.version += 1
        state.updated_by, state.updated_at = self.user.id, self.now
        self.db.flush()
        result = self._apply_state(item, state)
        audit.log_update('operational_inbox', state.id, result.key, previous, result.model_dump(mode='json'))
        return result
