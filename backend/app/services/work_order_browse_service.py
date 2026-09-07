"""Bounded, read-only work-order browsing; legacy reconciliation reads are unchanged."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import joinedload, selectinload

from app.models.part import Part, PartType
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.schemas.work_order import WorkOrderSummary
from app.services.work_order_state_service import work_order_operation_progress

TERMINAL = (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED)
EXCLUDED_TYPES = (PartType.PURCHASED, PartType.HARDWARE, PartType.RAW_MATERIAL)


def summary(wo):
    metrics = work_order_operation_progress(wo)
    return WorkOrderSummary(
        id=wo.id,
        version=wo.version,
        work_order_number=wo.work_order_number,
        part_id=wo.part_id,
        parent_work_order_id=wo.parent_work_order_id,
        work_order_type=wo.work_order_type,
        sequential_operations=wo.sequential_operations,
        unit_number=wo.unit_number,
        part_number=wo.part.part_number if wo.part else None,
        part_name=wo.part.name if wo.part else None,
        part_type=wo.part.part_type.value if wo.part and wo.part.part_type else None,
        status=wo.status,
        priority=wo.priority,
        quantity_ordered=wo.quantity_ordered,
        quantity_complete=wo.quantity_complete,
        due_date=wo.due_date,
        customer_name=wo.customer_name,
        **metrics,
    )


def browse_work_orders(
    db,
    company_id,
    *,
    skip=0,
    limit=50,
    status=None,
    search=None,
    customer=None,
    hide_cots=True,
    scope=None,
    sort='priority',
    direction='asc',
    group='none',
    today=None,
):
    today = today or datetime.now(ZoneInfo('America/Chicago')).date()
    query = (
        db.query(WorkOrder)
        .outerjoin(Part, and_(Part.id == WorkOrder.part_id, Part.company_id == company_id))
        .filter(WorkOrder.company_id == company_id, WorkOrder.is_deleted.is_(False))
    )
    query = query.filter(WorkOrder.status == status) if status else query.filter(WorkOrder.status.not_in(TERMINAL))
    if search and search.strip():
        # Treat user search as literal text, not SQL wildcard syntax.
        pattern = '%' + search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        query = query.filter(
            or_(
                *[
                    column.ilike(pattern, escape='\\')
                    for column in (
                        WorkOrder.work_order_number,
                        WorkOrder.customer_name,
                        WorkOrder.customer_po,
                        WorkOrder.lot_number,
                        WorkOrder.unit_number,
                        Part.part_number,
                        Part.name,
                    )
                ]
            )
        )
    if hide_cots:
        query = query.filter(or_(Part.part_type.is_(None), Part.part_type.not_in(EXCLUDED_TYPES)))
    active = WorkOrder.status.not_in(TERMINAL)
    overdue = and_(active, WorkOrder.due_date < today)
    due_today = and_(active, WorkOrder.due_date == today)
    if scope == 'overdue':
        query = query.filter(overdue)
    elif scope == 'due_today':
        query = query.filter(due_today)
    customer_rows = (
        query.with_entities(WorkOrder.customer_name)
        .filter(WorkOrder.customer_name.is_not(None), WorkOrder.customer_name != '')
        .distinct()
        .order_by(WorkOrder.customer_name)
        .limit(1001)
        .all()
    )
    if customer:
        query = query.filter(WorkOrder.customer_name == customer)
    total, overdue_count, in_progress_count, today_count = query.with_entities(
        func.count(WorkOrder.id),
        func.sum(case((overdue, 1), else_=0)),
        func.sum(case((WorkOrder.status == WorkOrderStatus.IN_PROGRESS, 1), else_=0)),
        func.sum(case((due_today, 1), else_=0)),
    ).one()
    # Clamp an obsolete page after deletion/filter changes, so the UI can recover.
    skip = min(skip, max(0, ((total - 1) // limit) * limit))
    group_expr = {
        'customer': func.coalesce(func.nullif(WorkOrder.customer_name, ''), 'No Customer'),
        'part': func.coalesce(
            Part.part_number, case((WorkOrder.work_order_type == 'laser_cutting', 'Nest Packages'), else_='No Part')
        ),
        'status': WorkOrder.status,
    }.get(group)
    columns = {
        'work_order_number': WorkOrder.work_order_number,
        'part': Part.part_number,
        'customer': WorkOrder.customer_name,
        'due_date': WorkOrder.due_date,
        'priority': WorkOrder.priority,
        'status': WorkOrder.status,
    }
    column = columns[sort]
    ordering = [group_expr.asc()] if group_expr is not None else []
    ordering.append((column.desc() if direction == 'desc' else column.asc()).nulls_last())
    if sort == 'priority':
        ordering.append(WorkOrder.due_date.asc().nulls_last())
    ordering.append(WorkOrder.id.asc())
    rows = (
        query.options(
            joinedload(WorkOrder.part.and_(Part.company_id == company_id)), selectinload(WorkOrder.operations)
        )
        .order_by(*ordering)
        .offset(skip)
        .limit(limit)
        .all()
    )
    groups = {}
    if group_expr is not None and rows:
        visible_groups = (
            query.with_entities(group_expr).filter(WorkOrder.id.in_([wo.id for wo in rows])).distinct().all()
        )
        groups = {
            getattr(name, 'value', name): count
            for name, count in query.with_entities(group_expr, func.count(WorkOrder.id))
            .filter(group_expr.in_([value[0] for value in visible_groups]))
            .group_by(group_expr)
            .all()
        }
    return {
        'items': [summary(wo) for wo in rows],
        'total': total,
        'skip': skip,
        'limit': limit,
        'has_next': skip + len(rows) < total,
        'stats': {'overdue': overdue_count or 0, 'in_progress': in_progress_count or 0, 'due_today': today_count or 0},
        'customers': [row[0] for row in customer_rows[:1000]],
        'customers_truncated': len(customer_rows) > 1000,
        'group_totals': groups,
    }
