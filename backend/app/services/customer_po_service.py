"""Customer PO groupings from persisted work-order facts; no reconciliation writes."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from app.models.laser_nest import LaserNest
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)
from app.schemas.customer_po import CustomerPOWorkOrder
from app.services.dispatch_service import cancelled_nest_exists
from app.services.work_order_browse_service import summary

TERMINAL = (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED)
COMPLETED = (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED)


def _base_query(db, company_id):
    return db.query(WorkOrder).filter(WorkOrder.company_id == company_id, WorkOrder.is_deleted.is_(False))


def customer_po_suggestions(db, company_id, *, search="", customer_name=None, limit=10):
    """Bounded prefix matches for entry; selecting a suggestion preserves its exact identity."""
    if not search.strip():
        return {"items": [], "has_more": False}

    customer = func.nullif(WorkOrder.customer_name, "")
    prefix = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    query = _base_query(db, company_id).filter(
        WorkOrder.customer_po.is_not(None),
        func.length(func.trim(WorkOrder.customer_po, " \t\n\r\f\v")) > 0,
        WorkOrder.customer_po.ilike(prefix, escape="\\"),
    )
    if customer_name is not None:
        query = query.filter(WorkOrder.customer_name == customer_name if customer_name else customer.is_(None))
    # Group only by persisted identity; NULL/empty customer are one unnamed group.
    # Read one extra group to report truncation without scanning/counting all matches.
    rows = (
        query.with_entities(customer.label("customer_name"), WorkOrder.customer_po)
        .group_by(customer, WorkOrder.customer_po)
        .order_by(
            func.lower(WorkOrder.customer_po),
            WorkOrder.customer_po,
            customer.asc().nulls_last(),
        )
        .limit(limit + 1)
        .all()
    )
    return {
        "items": [dict(row._mapping) for row in rows[:limit]],
        "has_more": len(rows) > limit,
    }


def _stat_columns(today, company_id):
    active = WorkOrder.status.not_in(TERMINAL)
    promise_date = func.coalesce(WorkOrder.must_ship_by, WorkOrder.due_date)
    held_operation = (
        select(WorkOrderOperation.id)
        .where(
            WorkOrderOperation.work_order_id == WorkOrder.id,
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.status == OperationStatus.ON_HOLD,
            ~cancelled_nest_exists(company_id),
        )
        .correlate(WorkOrder)
        .exists()
    )

    def count_where(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    return (
        func.count(WorkOrder.id).label("total_work_orders"),
        count_where(active).label("active_work_orders"),
        count_where(WorkOrder.status.in_(COMPLETED)).label("completed_work_orders"),
        count_where(WorkOrder.status == WorkOrderStatus.CANCELLED).label("cancelled_work_orders"),
        count_where(WorkOrder.status == WorkOrderStatus.IN_PROGRESS).label("in_progress_work_orders"),
        count_where(active & or_(WorkOrder.status == WorkOrderStatus.ON_HOLD, held_operation)).label(
            "on_hold_work_orders"
        ),
        count_where(active & (promise_date < today)).label("overdue_work_orders"),
        count_where(active & (promise_date == today)).label("due_today_work_orders"),
        func.min(case((active, promise_date), else_=None)).label("next_due_date"),
    )


def _stats(row):
    values = dict(row._mapping)
    denominator = values["total_work_orders"] - values["cancelled_work_orders"]
    values["completion_percent"] = round(values["completed_work_orders"] / denominator * 100, 1) if denominator else 0.0
    return values


def _page(total, skip, limit):
    return min(skip, max(0, ((total - 1) // limit) * limit))


def list_customer_pos(db, company_id, *, skip=0, limit=50, search=None, today=None):
    today = today or datetime.now(ZoneInfo("America/Chicago")).date()
    # NULL and empty customer both mean that no customer was recorded. All other
    # values remain exact: similar PO numbers and different customers never merge.
    customer = func.nullif(WorkOrder.customer_name, "")
    query = _base_query(db, company_id).filter(
        WorkOrder.customer_po.is_not(None),
        func.length(func.trim(WorkOrder.customer_po, " \t\n\r\f\v")) > 0,
    )
    if search and search.strip():
        pattern = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(
            or_(
                WorkOrder.customer_po.ilike(pattern, escape="\\"),
                WorkOrder.customer_name.ilike(pattern, escape="\\"),
            )
        )
    groups = query.with_entities(
        customer.label("customer_name"),
        WorkOrder.customer_po,
        *_stat_columns(today, company_id),
    ).group_by(customer, WorkOrder.customer_po)
    total = groups.count()
    skip = _page(total, skip, limit)
    rows = groups.order_by(customer.asc().nulls_last(), WorkOrder.customer_po.asc()).offset(skip).limit(limit).all()
    items = []
    for row in rows:
        values = _stats(row)
        name = values.pop("customer_name")
        po = values.pop("customer_po")
        items.append({"customer_name": name, "customer_po": po, "stats": values})
    return {
        "items": items,
        "total": total,
        "skip": skip,
        "limit": limit,
        "has_next": skip + len(items) < total,
        "as_of_date": today,
    }


def _current_operations(wo, cancelled_operation_ids):
    if wo.status in TERMINAL:
        return []
    operations = sorted(
        (op for op in wo.operations if op.id not in cancelled_operation_ids),
        key=lambda op: (op.sequence, op.id),
    )
    selected = [op for op in operations if op.status in (OperationStatus.IN_PROGRESS, OperationStatus.ON_HOLD)]
    if not selected:
        selected = [op for op in operations if op.status == OperationStatus.READY]
    if not selected:
        selected = [op for op in operations if op.status == OperationStatus.PENDING][:1]
    return [
        {
            "id": op.id,
            "operation_number": op.operation_number,
            "name": op.name,
            "status": op.status,
            "work_center_name": op.work_center.name if op.work_center else None,
        }
        for op in selected
    ]


def customer_po_dashboard(db, company_id, *, customer_name, customer_po, skip=0, limit=50, today=None):
    today = today or datetime.now(ZoneInfo("America/Chicago")).date()
    query = _base_query(db, company_id).filter(WorkOrder.customer_po == customer_po)
    query = (
        query.filter(WorkOrder.customer_name == customer_name)
        if customer_name
        else query.filter(or_(WorkOrder.customer_name.is_(None), WorkOrder.customer_name == ""))
    )
    stats = _stats(query.with_entities(*_stat_columns(today, company_id)).one())
    total = stats["total_work_orders"]
    skip = _page(total, skip, limit)
    rows = (
        query.options(
            joinedload(WorkOrder.part.and_(Part.company_id == company_id)),
            selectinload(WorkOrder.operations.and_(WorkOrderOperation.company_id == company_id)).joinedload(
                WorkOrderOperation.work_center.and_(WorkCenter.company_id == company_id)
            ),
            # Route progress reads each laser operation's retained nest to exclude
            # cancellations. Batch that read and fence inconsistent tenant links.
            selectinload(WorkOrder.operations.and_(WorkOrderOperation.company_id == company_id)).joinedload(
                WorkOrderOperation.laser_nest.and_(LaserNest.company_id == company_id)
            ),
        )
        .order_by(
            case((WorkOrder.status.in_(TERMINAL), 1), else_=0),
            func.coalesce(WorkOrder.must_ship_by, WorkOrder.due_date).asc().nulls_last(),
            WorkOrder.work_order_number,
            WorkOrder.id,
        )
        .offset(skip)
        .limit(limit)
        .all()
    )
    # Reuse dispatch's tenant-scoped tombstone predicate; a cancelled laser nest
    # retains an ON_HOLD operation for traceability, but is no longer current work.
    cancelled_operation_ids = (
        {
            operation_id
            for (operation_id,) in db.query(WorkOrderOperation.id).filter(
                WorkOrderOperation.company_id == company_id,
                WorkOrderOperation.work_order_id.in_([wo.id for wo in rows]),
                cancelled_nest_exists(company_id),
            )
        }
        if rows
        else set()
    )
    items = []
    for wo in rows:
        values = summary(wo).model_dump()
        current = _current_operations(wo, cancelled_operation_ids)
        promise_date = wo.must_ship_by or wo.due_date
        values["current_operation"] = ", ".join(op["name"] for op in current) or None
        items.append(
            CustomerPOWorkOrder(
                **values,
                po_date=wo.po_date,
                must_ship_by=wo.must_ship_by,
                quantity_scrapped=float(wo.quantity_scrapped or 0),
                is_overdue=wo.status not in TERMINAL and promise_date is not None and promise_date < today,
                current_operations=current,
            )
        )
    return {
        "customer_name": customer_name or None,
        "customer_po": customer_po,
        "stats": stats,
        "items": items,
        "total": total,
        "skip": skip,
        "limit": limit,
        "has_next": skip + len(items) < total,
        "as_of_date": today,
    }
