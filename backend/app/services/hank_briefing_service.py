"""Live, read-only shift briefings. No model call, inferred assignment or writes."""

from datetime import timedelta

from sqlalchemy import func

from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.shipping import Shipment, ShipmentStatus
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.schemas.hank import HankBriefingItem, HankBriefingResponse, HankBriefingSection
from app.services.hank_preference_service import get_hank_preference_values
from app.services.operations_inbox_service import OperationalInboxService

ITEM_LIMIT = 5
ACTIVE_WORK = [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]
SECTION_SOURCES = {
    'shop': ('blocker', 'late_work_order'),
    'quality': ('quality_ncr',),
    'purchasing': ('supplier_follow_up', 'overdue_po_line'),
    'inventory': ('mrp_shortage', 'low_stock'),
}
SECTION_LABELS = {
    'shop': ('Shop priorities', 'Shared open blockers and late jobs. Items assigned to you come first.'),
    'quality': ('Quality attention', 'Open NCRs, with your assigned reviews first.'),
    'purchasing': ('Supplier follow-through', 'Supplier responses and overdue PO lines, with your follow-ups first.'),
    'inventory': (
        'Material attention',
        'Low stock and the latest completed MRP snapshot; verify supply before acting.',
    ),
}
ROLE_ORDER = {
    UserRole.OPERATOR: ('my_work', 'shop', 'quality', 'inventory', 'purchasing', 'shipping'),
    UserRole.QUALITY: ('quality', 'my_work', 'shop', 'inventory', 'purchasing', 'shipping'),
    UserRole.SHIPPING: ('shipping', 'shop', 'inventory', 'quality', 'purchasing', 'my_work'),
}
OVERVIEW_ORDER = ('shop', 'quality', 'purchasing', 'inventory', 'shipping', 'my_work')


def _bounded(value, limit):
    value = value or ''
    return value if len(value) <= limit else value[: limit - 1] + '…'


class HankBriefingService:
    def __init__(self, db, user, company_id, now=None):
        self.db, self.user, self.company_id = db, user, company_id
        # This is the same live source/override boundary as Operations Inbox.
        self.inbox = OperationalInboxService(db, user, company_id, now=now)
        self.permissions = self.inbox.permissions(user)
        self.preferences = get_hank_preference_values(db, company_id, user.id)
        self.item_limit = 3 if self.preferences.briefing_detail == 'concise' else ITEM_LIMIT

    def _signal_item(self, item):
        return HankBriefingItem(
            key=item.key,
            source_kind=item.source_kind,
            source_id=item.source_id,
            title=_bounded(item.title, 300),
            detail=_bounded(item.detail, 1200),
            severity=item.severity,
            href=item.href,
            suggested_action=_bounded(item.next_action or item.suggested_action, 500),
            owner_name=_bounded(item.owner_name, 210) if item.owner_name else None,
            is_mine=item.owner_id == self.user.id,
        )

    def _my_work(self):
        # Work orders have no operator assignment field. A still-open time entry
        # is evidence of clocked work only, never evidence of the next assignment.
        active_clock = tenant_query(self.db, TimeEntry, self.company_id).filter(
            TimeEntry.user_id == self.user.id,
            TimeEntry.work_order_id == WorkOrder.id,
            TimeEntry.clock_out.is_(None),
        )
        query = tenant_query(self.db, WorkOrder, self.company_id).filter(
            WorkOrder.is_deleted.is_(False),
            WorkOrder.status.in_(ACTIVE_WORK),
            active_clock.exists(),
        )
        total = query.count()
        rows = query.order_by(WorkOrder.priority, WorkOrder.id).limit(self.item_limit).all()
        return HankBriefingSection(
            key='my_work',
            title='Your clocked jobs',
            description='Jobs with your open time entries. Shared shop issues below are not personal assignments.',
            total=total,
            truncated=total > self.item_limit,
            items=[
                HankBriefingItem(
                    key=f'active_work:{wo.id}',
                    source_kind='active_work',
                    source_id=wo.id,
                    title=_bounded(f'{wo.work_order_number}: clocked in', 300),
                    detail=(
                        f'{wo.status.value.replace("_", " ").capitalize()}; due {wo.due_date or "date not set"}. '
                        f'{float(wo.quantity_complete or 0):g} of {wo.quantity_ordered:g} completed. '
                        'An open time entry may need to be closed if you have moved on.'
                    ),
                    severity='high' if wo.status == WorkOrderStatus.ON_HOLD else 'low',
                    href=f'/work-orders/{wo.id}',
                    suggested_action='Review the job and your current time entry',
                    owner_name=_bounded(self.user.full_name, 210),
                    is_mine=True,
                )
                for wo in rows
            ],
        )

    def _shipping(self):
        # This is due-date planning, not a claim that goods cleared inspection or
        # are ready to ship. Exclude fully dispatched work even before WO closure.
        shipped = tenant_filter(
            self.db.query(Shipment.work_order_id, func.sum(Shipment.quantity_shipped).label('quantity')),
            Shipment,
            self.company_id,
        ).filter(
            Shipment.is_deleted.is_(False),
            Shipment.status.in_([ShipmentStatus.SHIPPED, ShipmentStatus.DELIVERED]),
        )
        shipped = shipped.group_by(Shipment.work_order_id).subquery()
        due = func.coalesce(WorkOrder.must_ship_by, WorkOrder.due_date)
        query = (
            tenant_query(self.db, WorkOrder, self.company_id)
            .outerjoin(shipped, shipped.c.work_order_id == WorkOrder.id)
            .filter(
                WorkOrder.is_deleted.is_(False),
                WorkOrder.status.in_([*ACTIVE_WORK, WorkOrderStatus.COMPLETE]),
                due <= self.inbox.today + timedelta(days=2),
                WorkOrder.quantity_ordered > func.coalesce(shipped.c.quantity, 0),
            )
        )
        total = query.count()
        rows = query.order_by(due, WorkOrder.priority, WorkOrder.id).limit(self.item_limit).all()
        return HankBriefingSection(
            key='shipping',
            title='Due to leave the shop',
            description='Unshipped work due through the next two days, including overdue jobs. Readiness is not verified.',
            total=total,
            truncated=total > self.item_limit,
            items=[
                HankBriefingItem(
                    key=f'shipping_due:{wo.id}',
                    source_kind='shipping_due',
                    source_id=wo.id,
                    title=_bounded(f'{wo.work_order_number}: due {wo.must_ship_by or wo.due_date}', 300),
                    detail=(
                        f'{"Must leave by" if wo.must_ship_by else "Work order due"} '
                        f'{wo.must_ship_by or wo.due_date}; status {wo.status.value.replace("_", " ")}. '
                        'Review completion, quality release, packing and shipping documents before dispatch.'
                    ),
                    severity='high' if (wo.must_ship_by or wo.due_date) < self.inbox.today else 'medium',
                    href=f'/work-orders/{wo.id}',
                    suggested_action='Review this work order, then prepare shipping in the Shipping workspace',
                )
                for wo in rows
            ],
        )

    def briefing(self):
        snapshot = self.inbox.list()
        sections = {}
        for key, sources in SECTION_SOURCES.items():
            if not any(self.inbox.can_view(source) for source in sources):
                continue
            items = [item for item in snapshot.items if item.source_kind in sources and item.snoozed_until is None]
            items.sort(key=lambda item: (item.owner_id != self.user.id, item.severity != 'high', item.key))
            title, description = SECTION_LABELS[key]
            sections[key] = HankBriefingSection(
                key=key,
                title=title,
                description=description,
                total=len(items),
                truncated=len(items) > self.item_limit or any(s in snapshot.truncated_sources for s in sources),
                items=[self._signal_item(item) for item in items[: self.item_limit]],
            )
        if 'work_orders:view' in self.permissions:
            sections['my_work'] = self._my_work()
            if 'shipping:view' in self.permissions:
                sections['shipping'] = self._shipping()
        order = ROLE_ORDER.get(self.user.role, OVERVIEW_ORDER)
        focus = self.preferences.focus_area
        if focus in sections:
            order = (focus, *(key for key in order if key != focus))
        item_count = 'three' if self.item_limit == 3 else 'five'
        notes = [
            'A live snapshot of the sources your role can view; refresh to check for changes.',
            f'Up to {item_count} items per section. Shared snoozed issues are omitted until their snooze expires or facts change.',
            'This briefing does not check every ERP workflow and does not change records.',
        ]
        if focus != 'role_default' and focus not in sections:
            notes.append(
                'Your preferred focus area is unavailable for your current role; the usual authorized section order is shown.'
            )
        if snapshot.truncated_sources:
            notes.append(
                'Some source scans reached their limit; section counts may be lower bounds. Open Operations Inbox for detail.'
            )
        if any(section.truncated for section in sections.values()):
            notes.append(
                'Some sections contain additional items beyond those shown. Open the relevant workspace to review them.'
            )
        if not sections:
            notes.append('Your current role has no access to the available briefing sources.')
        role_label = {
            UserRole.OPERATOR: 'your work on the floor',
            UserRole.QUALITY: 'quality and containment',
            UserRole.SHIPPING: 'shipping priorities',
        }.get(self.user.role, 'the shop')
        return HankBriefingResponse(
            checked_at=snapshot.checked_at,
            role=self.user.role.value,
            headline=f'Let’s get you caught up on {role_label}.',
            summary='Your assigned issues come first within each section. Open a record to review the next step.',
            sections=[sections[key] for key in order if key in sections],
            coverage_notes=notes,
        )
