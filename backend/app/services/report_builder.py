"""
Report Builder Service - Dynamic query execution for custom reports
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import asc, desc, func
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_filter
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.purchasing import PurchaseOrder
from app.models.quality import NonConformanceReport
from app.models.quote import Quote
from app.models.work_order import WorkOrder
from app.schemas.analytics import (
    AggregateFunction,
    CustomReportRequest,
    ReportDataSource,
)
from app.services.labor_cost_service import is_labor_cost_rollup_enabled

logger = logging.getLogger(__name__)

# WORK_ORDERS columns that are populated ONLY by the labor-cost rollup. When
# ``LABOR_COST_ROLLUP_ENABLED`` is OFF (the default) these render a literal 0 that means
# "not tracked", NOT a genuine $0/0-hours. ``labor_tracking_note`` annotates a report so
# a consumer can tell the two apart (G3-content), resolved via the SAME flag chokepoint
# (labor_cost_service.is_labor_cost_rollup_enabled) the completion paths + cost analysis use.
LABOR_DERIVED_WORK_ORDER_FIELDS = ("actual_hours", "actual_cost", "estimated_cost")

# Model mapping for data sources
DATA_SOURCE_MODELS = {
    ReportDataSource.WORK_ORDERS: WorkOrder,
    ReportDataSource.PARTS: Part,
    ReportDataSource.INVENTORY: InventoryItem,
    ReportDataSource.QUALITY: NonConformanceReport,
    ReportDataSource.PURCHASING: PurchaseOrder,
    ReportDataSource.QUOTES: Quote,
}

# Field mappings for each data source
FIELD_MAPPINGS = {
    ReportDataSource.WORK_ORDERS: {
        "work_order_number": WorkOrder.work_order_number,
        "status": WorkOrder.status,
        "quantity_ordered": WorkOrder.quantity_ordered,
        "quantity_complete": WorkOrder.quantity_complete,
        "quantity_scrapped": WorkOrder.quantity_scrapped,
        "due_date": WorkOrder.due_date,
        "actual_start": WorkOrder.actual_start,
        "actual_end": WorkOrder.actual_end,
        "customer_name": WorkOrder.customer_name,
        "customer_po": WorkOrder.customer_po,
        # G3-content: ``estimated_hours`` intentionally NOT exposed -- it has no writer
        # anywhere in the system (structurally 0 in every tenant), so a report column for
        # it only renders a misleading literal 0. Computing it needs a routing/operations
        # join (out of scope). Dropped rather than surfaced as a phantom column.
        "actual_hours": WorkOrder.actual_hours,
        "estimated_cost": WorkOrder.estimated_cost,
        "actual_cost": WorkOrder.actual_cost,
        "created_at": WorkOrder.created_at,
    },
    ReportDataSource.PARTS: {
        "part_number": Part.part_number,
        "name": Part.name,
        "part_type": Part.part_type,
        "standard_cost": Part.standard_cost,
        "material_cost": Part.material_cost,
        "labor_cost": Part.labor_cost,
        "lead_time_days": Part.lead_time_days,
        "safety_stock": Part.safety_stock,
        "reorder_point": Part.reorder_point,
    },
    ReportDataSource.INVENTORY: {
        "quantity_on_hand": InventoryItem.quantity_on_hand,
        "quantity_allocated": InventoryItem.quantity_allocated,
        "quantity_available": InventoryItem.quantity_available,
        "location": InventoryItem.location,
        "lot_number": InventoryItem.lot_number,
        "unit_cost": InventoryItem.unit_cost,
        "status": InventoryItem.status,
    },
    ReportDataSource.QUALITY: {
        "ncr_number": NonConformanceReport.ncr_number,
        "status": NonConformanceReport.status,
        "source": NonConformanceReport.source,
        "disposition": NonConformanceReport.disposition,
        "quantity_affected": NonConformanceReport.quantity_affected,
        "quantity_rejected": NonConformanceReport.quantity_rejected,
        "detected_date": NonConformanceReport.detected_date,
        "closed_date": NonConformanceReport.closed_date,
        "estimated_cost": NonConformanceReport.estimated_cost,
        "actual_cost": NonConformanceReport.actual_cost,
    },
    ReportDataSource.PURCHASING: {
        "po_number": PurchaseOrder.po_number,
        "status": PurchaseOrder.status,
        "order_date": PurchaseOrder.order_date,
        "required_date": PurchaseOrder.required_date,
        "subtotal": PurchaseOrder.subtotal,
        "total": PurchaseOrder.total,
    },
    ReportDataSource.QUOTES: {
        "quote_number": Quote.quote_number,
        "customer_name": Quote.customer_name,
        "status": Quote.status,
        "quote_date": Quote.quote_date,
        "subtotal": Quote.subtotal,
        "total": Quote.total,
        "lead_time_days": Quote.lead_time_days,
    },
}


class ReportBuilderService:
    def __init__(self, db: Session):
        self.db = db

    def labor_tracking_note(self, request: CustomReportRequest, company_id: int) -> Optional[Dict[str, Any]]:
        """Flag-OFF annotation for labor-derived report columns (G3-content).

        When ``LABOR_COST_ROLLUP_ENABLED`` is OFF (default) for this company AND the
        report selects any of the WORK_ORDERS labor-derived columns
        (``actual_hours`` / ``actual_cost`` / ``estimated_cost``), those columns render a
        literal 0 that means "not tracked", not a genuine zero. Returns a small metadata
        dict the endpoint can surface (e.g. as a response header) so a consumer can tell
        the two apart -- mirroring the documented flag-OFF stance of
        ``analytics_service.get_cost_analysis``. Returns ``None`` when the flag is ON, the
        data source isn't WORK_ORDERS, or no labor-derived column was selected (so the
        bare-list report contract is otherwise unchanged). Resolves the flag via the SAME
        chokepoint (``labor_cost_service.is_labor_cost_rollup_enabled``).
        """
        if request.data_source != ReportDataSource.WORK_ORDERS:
            return None
        if is_labor_cost_rollup_enabled(company_id):
            return None
        selected = {col.field for col in request.columns}
        not_tracked = [f for f in LABOR_DERIVED_WORK_ORDER_FIELDS if f in selected]
        if not_tracked:
            return {
                "labor_cost_rollup_enabled": False,
                "not_tracked_fields": not_tracked,
                "note": (
                    "Labor-cost rollup is disabled for this company, so "
                    f"{', '.join(not_tracked)} are reported as 0 (not tracked), not a measured $0."
                ),
            }
        return None

    def execute_report(self, request: CustomReportRequest, company_id: int) -> List[Dict[str, Any]]:
        """Execute a custom report query and return results, scoped to ``company_id``.

        Tenant isolation (G3-scope): the report runs ``self.db.query(*columns)`` over a
        single base model selected by ``request.data_source``. Every supported base
        model (WorkOrder, Part, InventoryItem, NonConformanceReport, PurchaseOrder,
        Quote) carries ``company_id`` via ``TenantMixin``, so we ALWAYS apply a
        ``model.company_id == company_id`` filter before any user-supplied filters.
        Without it the report returned every tenant's rows.
        """
        model = DATA_SOURCE_MODELS.get(request.data_source)
        if not model:
            raise ValueError(f"Unknown data source: {request.data_source}")

        field_map = FIELD_MAPPINGS.get(request.data_source, {})

        # Build column list
        columns = []
        for col in request.columns:
            if col.field not in field_map:
                continue

            db_field = field_map[col.field]

            if col.aggregate:
                if col.aggregate == AggregateFunction.SUM:
                    db_field = func.sum(db_field)
                elif col.aggregate == AggregateFunction.AVG:
                    db_field = func.avg(db_field)
                elif col.aggregate == AggregateFunction.COUNT:
                    db_field = func.count(db_field)
                elif col.aggregate == AggregateFunction.MIN:
                    db_field = func.min(db_field)
                elif col.aggregate == AggregateFunction.MAX:
                    db_field = func.max(db_field)

            alias = col.alias or col.field
            columns.append(db_field.label(alias))

        if not columns:
            raise ValueError("No valid columns specified")

        # Build query
        query = self.db.query(*columns)

        # G3-scope: tenant isolation -- restrict to the active company BEFORE any
        # user-supplied filters/group-by/sort. The base model carries company_id
        # (TenantMixin); a report must never return another tenant's rows.
        query = tenant_filter(query, model, company_id)

        # Apply filters
        for f in request.filters:
            if f.field not in field_map:
                continue

            db_field = field_map[f.field]

            if f.operator == "eq":
                query = query.filter(db_field == f.value)
            elif f.operator == "ne":
                query = query.filter(db_field != f.value)
            elif f.operator == "gt":
                query = query.filter(db_field > f.value)
            elif f.operator == "gte":
                query = query.filter(db_field >= f.value)
            elif f.operator == "lt":
                query = query.filter(db_field < f.value)
            elif f.operator == "lte":
                query = query.filter(db_field <= f.value)
            elif f.operator == "in":
                query = query.filter(db_field.in_(f.value))
            elif f.operator == "like":
                query = query.filter(db_field.ilike(f"%{f.value}%"))
            elif f.operator == "between" and f.value2:
                query = query.filter(db_field.between(f.value, f.value2))

        # Apply group by
        group_fields = []
        for g in request.group_by:
            if g.field not in field_map:
                continue
            group_fields.append(field_map[g.field])

        if group_fields:
            query = query.group_by(*group_fields)

        # Apply sorting
        for s in request.sort:
            if s.field not in field_map:
                continue

            db_field = field_map[s.field]
            if s.direction.lower() == "desc":
                query = query.order_by(desc(db_field))
            else:
                query = query.order_by(asc(db_field))

        # Apply limit
        if request.limit:
            query = query.limit(request.limit)

        # Execute and format results
        results = query.all()

        # Convert to list of dicts
        column_names = [col.alias or col.field for col in request.columns if col.field in field_map]

        return [{column_names[i]: self._format_value(row[i]) for i in range(len(column_names))} for row in results]

    def _format_value(self, value: Any) -> Any:
        """Format a value for JSON serialization."""
        if value is None:
            return None
        if hasattr(value, 'value'):  # Enum
            return value.value
        if hasattr(value, 'isoformat'):  # Date/datetime
            return value.isoformat()
        if isinstance(value, float):
            return round(value, 2)
        return value
