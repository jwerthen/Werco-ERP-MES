"""Read-only PO cost history; aggregate and paginate in SQL, never load the PO book.

One observation represents one part on one committed purchase order. Multiple
lines are quantity-weighted before comparing to the preceding order, so a split
line does not masquerade as a new purchase. Comparisons never cross part IDs.

PO lines currently have no currency or unit-of-measure snapshot. The response
therefore exposes currency as unknown and identifies the current catalog UOM;
it does not claim to convert or reconstruct historical units/currencies.
"""

from datetime import date

from fastapi import HTTPException
from sqlalchemy import Numeric, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models.part import Part, PartType
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.schemas.material_price_history import (
    PriceHistoryChartPoint,
    PriceHistoryCounts,
    PriceHistoryDetailResponse,
    PriceHistoryListResponse,
    PriceHistoryObservation,
    PriceHistoryPoint,
    PriceHistorySort,
    PriceHistoryStats,
    PriceHistorySummary,
    PriceHistoryTrend,
    PriceHistoryVendor,
)

COMMITTED_STATUSES = (
    POStatus.APPROVED,
    POStatus.SENT,
    POStatus.PARTIAL,
    POStatus.RECEIVED,
    POStatus.CLOSED,
)
SPARKLINE_LIMIT = 12
CHART_LIMIT = 500
HISTORY_NOTES = [
    "Costs come from approved, sent, partially received, received and closed purchase orders; "
    "draft, pending approval, cancelled and deleted orders are excluded.",
    "Each point is one purchase order. Repeated item lines are combined using quantity-weighted unit prices. "
    "Extended costs use ordered quantity × unit price and exclude freight and tax; they are ordered costs, not invoices.",
    "Units use the item's current catalog unit of measure; historical PO units and currencies were not recorded. "
    "No unit or currency conversion is performed.",
    "Order date is used when available, otherwise the PO creation date. Orders with neither date are excluded. "
    "Orders on the same date are "
    "ordered by creation time and PO ID. Price changes compare consecutive purchases within the selected filters.",
    "Lines with invalid or unrepresentable quantities or unit prices are excluded from price history.",
]


def _observations(
    company_id: int,
    *,
    part_id: int | None = None,
    search: str | None = None,
    part_type: PartType | None = None,
    vendor_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
):
    """A scoped, grouped source shared by list, detail, options, and statistics.

    Scope EVERY joined table, not only the PO header: legacy foreign keys do
    not enforce company equality. Keep archived vendors as historical evidence.
    Inactive inventory is retained; deleted parts and orders are hidden.
    """
    quantity = cast(PurchaseOrderLine.quantity_ordered, Numeric(28, 10))
    extended = func.sum(quantity * cast(PurchaseOrderLine.unit_price, Numeric(28, 10)))
    ordered_quantity = func.sum(quantity)
    order_date = func.coalesce(PurchaseOrder.order_date, func.date(PurchaseOrder.created_at))
    fields = [
        Part.id.label("part_id"),
        Part.part_number,
        Part.name.label("part_name"),
        Part.part_type,
        Part.unit_of_measure,
        PurchaseOrder.id.label("purchase_order_id"),
        PurchaseOrder.po_number,
        order_date.label("order_date"),
        PurchaseOrder.created_at.label("created_at"),
        PurchaseOrder.status,
        Vendor.id.label("vendor_id"),
        Vendor.name.label("vendor_name"),
    ]
    query = (
        select(
            *fields,
            ordered_quantity.label("quantity_ordered"),
            extended.label("extended_price"),
            (extended / func.nullif(ordered_quantity, 0)).label("unit_price"),
            func.count(PurchaseOrderLine.id).label("line_count"),
        )
        .select_from(PurchaseOrderLine)
        .join(
            PurchaseOrder,
            and_(
                PurchaseOrder.id == PurchaseOrderLine.purchase_order_id,
                PurchaseOrder.company_id == company_id,
                PurchaseOrder.is_deleted.is_(False),
            ),
        )
        .join(
            Part,
            and_(
                Part.id == PurchaseOrderLine.part_id,
                Part.company_id == company_id,
                Part.is_deleted.is_(False),
            ),
        )
        .join(
            Vendor,
            and_(Vendor.id == PurchaseOrder.vendor_id, Vendor.company_id == company_id),
        )
        .where(
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrder.status.in_(COMMITTED_STATUSES),
            order_date.is_not(None),
            # The source columns are Float. Bounds reject Infinity and Postgres
            # NaN (which sorts above finite numbers), as well as quantities
            # that would round to zero or overflow the Numeric(28, 10) cast.
            PurchaseOrderLine.quantity_ordered >= 1e-10,
            PurchaseOrderLine.quantity_ordered < 1e18,
            PurchaseOrderLine.unit_price >= 0,
            PurchaseOrderLine.unit_price < 1e18,
        )
        .group_by(*fields)
    )
    if part_id is not None:
        query = query.where(Part.id == part_id)
    if search and search.strip():
        # Escape LIKE metacharacters so an inventory number containing % or _
        # has the same literal search meaning as it does in the UI.
        value = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{value}%"
        query = query.where(
            or_(
                Part.part_number.ilike(pattern, escape="\\"),
                Part.name.ilike(pattern, escape="\\"),
                Part.description.ilike(pattern, escape="\\"),
            )
        )
    if part_type is not None:
        query = query.where(Part.part_type == part_type)
    if vendor_id is not None:
        query = query.where(Vendor.id == vendor_id)
    if start_date is not None:
        query = query.where(order_date >= start_date)
    if end_date is not None:
        query = query.where(order_date <= end_date)
    return query.cte("price_observations")


def _ranked(observations):
    c = observations.c
    window = {
        "partition_by": c.part_id,
        "order_by": (
            c.order_date.desc(),
            c.created_at.desc(),
            c.purchase_order_id.desc(),
        ),
    }
    return select(
        *c,
        func.row_number().over(**window).label("position"),
        func.lead(c.unit_price).over(**window).label("previous_unit_price"),
        func.count().over(partition_by=c.part_id).label("order_count"),
        func.sum(c.quantity_ordered).over(partition_by=c.part_id).label("total_quantity"),
        func.sum(c.extended_price).over(partition_by=c.part_id).label("total_spend"),
    ).cte("ranked_prices")


def _latest(ranked):
    c = ranked.c
    change = func.round(cast(c.unit_price - c.previous_unit_price, Numeric(28, 10)), 6)
    return select(*c, change.label("price_change")).where(c.position == 1).cte("latest_prices")


def _number(value, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None else None


def _changes(latest, previous) -> dict:
    latest_value, previous_value = _number(latest), _number(previous)
    change = _number(float(latest) - float(previous)) if latest is not None and previous is not None else None
    percentage = (
        round((float(latest) - float(previous)) / float(previous) * 100, 2) if previous and latest is not None else None
    )
    return {
        "latest_unit_price": latest_value,
        "previous_unit_price": previous_value,
        "price_change": change,
        "price_change_percent": percentage,
    }


def _enum_value(value):
    return getattr(value, "value", value)


def _point(row) -> PriceHistoryPoint:
    return PriceHistoryPoint(
        purchase_order_id=row["purchase_order_id"],
        order_date=row["order_date"],
        unit_price=_number(row["unit_price"]),
    )


def _summary(row, sparkline: list[PriceHistoryPoint]) -> PriceHistorySummary:
    return PriceHistorySummary(
        part_id=row["part_id"],
        part_number=row["part_number"],
        part_name=row["part_name"],
        part_type=_enum_value(row["part_type"]),
        unit_of_measure=_enum_value(row["unit_of_measure"]),
        **_changes(row["unit_price"], row["previous_unit_price"]),
        last_order_date=row["order_date"],
        latest_po_id=row["purchase_order_id"],
        latest_po_number=row["po_number"],
        latest_vendor_id=row["vendor_id"],
        latest_vendor_name=row["vendor_name"],
        order_count=row["order_count"],
        total_quantity=_number(row["total_quantity"]),
        total_spend=_number(row["total_spend"]),
        sparkline=sparkline,
    )


def list_price_history(
    db: Session,
    company_id: int,
    *,
    search: str | None,
    part_type: PartType | None,
    trend: PriceHistoryTrend,
    sort: PriceHistorySort,
    page: int,
    page_size: int,
) -> PriceHistoryListResponse:
    ranked = _ranked(_observations(company_id, search=search, part_type=part_type))
    latest = _latest(ranked)
    c = latest.c
    predicates = {
        "up": c.price_change > 0,
        "down": c.price_change < 0,
        "unchanged": c.price_change == 0,
        "new": c.previous_unit_price.is_(None),
    }
    counts = (
        db.execute(
            select(
                func.count().label("all"),
                *(func.count(case((condition, 1))).label(key) for key, condition in predicates.items()),
            ).select_from(latest)
        )
        .mappings()
        .one()
    )
    query = select(latest)
    if trend != "all":
        query = query.where(predicates[trend])
    percentage = c.price_change / func.nullif(c.previous_unit_price, 0)
    ordering = {
        "recent": [
            c.order_date.desc(),
            c.created_at.desc(),
            c.purchase_order_id.desc(),
        ],
        "increase": [percentage.desc().nullslast(), c.price_change.desc().nullslast()],
        "decrease": [percentage.asc().nullslast(), c.price_change.asc().nullslast()],
        "name": [func.lower(c.part_number).asc()],
    }
    rows = (
        db.execute(query.order_by(*ordering[sort], c.part_id).offset((page - 1) * page_size).limit(page_size))
        .mappings()
        .all()
    )
    # Only the current page's last 12 observations enter Python. No per-item
    # queries, and no full-history materialization for overview sparklines.
    sparks: dict[int, list[PriceHistoryPoint]] = {row["part_id"]: [] for row in rows}
    if sparks:
        points = db.execute(
            select(ranked)
            .where(ranked.c.part_id.in_(sparks), ranked.c.position <= SPARKLINE_LIMIT)
            .order_by(ranked.c.part_id, ranked.c.position.desc())
        ).mappings()
        for point in points:
            sparks[point["part_id"]].append(_point(point))
    return PriceHistoryListResponse(
        items=[_summary(row, sparks[row["part_id"]]) for row in rows],
        total=counts[trend],
        page=page,
        page_size=page_size,
        summary=PriceHistoryCounts(
            tracked_parts=counts["all"],
            price_increases=counts["up"],
            price_decreases=counts["down"],
            unchanged_parts=counts["unchanged"],
            new_parts=counts["new"],
        ),
    )


def get_price_history(
    db: Session,
    company_id: int,
    part_id: int,
    *,
    vendor_id: int | None,
    start_date: date | None,
    end_date: date | None,
    page: int,
    page_size: int,
) -> PriceHistoryDetailResponse:
    all_observations = _observations(company_id, part_id=part_id)
    all_ranked = _ranked(all_observations)
    original = db.execute(select(all_ranked).where(all_ranked.c.position == 1)).mappings().first()
    if original is None:
        raise HTTPException(404, "No purchase price history found for this item")
    sparkline = [
        _point(row)
        for row in db.execute(
            select(all_ranked).where(all_ranked.c.position <= SPARKLINE_LIMIT).order_by(all_ranked.c.position.desc())
        ).mappings()
    ]
    vendors = db.execute(
        select(all_observations.c.vendor_id, all_observations.c.vendor_name)
        .distinct()
        .order_by(all_observations.c.vendor_name, all_observations.c.vendor_id)
    ).mappings()
    vendor_options = [PriceHistoryVendor(id=row["vendor_id"], name=row["vendor_name"]) for row in vendors]

    observations = _observations(
        company_id,
        part_id=part_id,
        vendor_id=vendor_id,
        start_date=start_date,
        end_date=end_date,
    )
    ranked = _ranked(observations)
    filtered_latest = db.execute(select(ranked).where(ranked.c.position == 1)).mappings().first()
    totals = (
        db.execute(
            select(
                func.min(observations.c.unit_price).label("lowest"),
                func.max(observations.c.unit_price).label("highest"),
                (
                    func.sum(observations.c.extended_price) / func.nullif(func.sum(observations.c.quantity_ordered), 0)
                ).label("weighted"),
            )
        )
        .mappings()
        .one()
    )
    stats = PriceHistoryStats()
    if filtered_latest is not None:
        stats = PriceHistoryStats(
            **_changes(filtered_latest["unit_price"], filtered_latest["previous_unit_price"]),
            lowest_unit_price=_number(totals["lowest"]),
            highest_unit_price=_number(totals["highest"]),
            weighted_average_unit_price=_number(totals["weighted"]),
            total_quantity=_number(filtered_latest["total_quantity"]),
            total_spend=_number(filtered_latest["total_spend"]),
            order_count=filtered_latest["order_count"],
        )
    history = []
    rows = db.execute(
        select(ranked).order_by(ranked.c.position).offset((page - 1) * page_size).limit(page_size)
    ).mappings()
    for row in rows:
        changes = _changes(row["unit_price"], row["previous_unit_price"])
        history.append(
            PriceHistoryObservation(
                purchase_order_id=row["purchase_order_id"],
                po_number=row["po_number"],
                order_date=row["order_date"],
                status=_enum_value(row["status"]),
                vendor_id=row["vendor_id"],
                vendor_name=row["vendor_name"],
                quantity_ordered=_number(row["quantity_ordered"]),
                unit_price=changes.pop("latest_unit_price"),
                extended_price=_number(row["extended_price"]),
                line_count=row["line_count"],
                unit_of_measure=_enum_value(row["unit_of_measure"]),
                **changes,
            )
        )
    chart = [
        PriceHistoryChartPoint(
            **_point(row).model_dump(),
            vendor_name=row["vendor_name"],
            quantity_ordered=_number(row["quantity_ordered"]),
            po_number=row["po_number"],
        )
        for row in db.execute(
            select(ranked).where(ranked.c.position <= CHART_LIMIT).order_by(ranked.c.position.desc())
        ).mappings()
    ]
    return PriceHistoryDetailResponse(
        part=_summary(original, sparkline),
        history=history,
        total=stats.order_count,
        page=page,
        page_size=page_size,
        stats=stats,
        chart=chart,
        chart_truncated=stats.order_count > CHART_LIMIT,
        vendor_options=vendor_options,
        notes=HISTORY_NOTES,
    )
