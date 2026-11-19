"""
Analytics & Business Intelligence API Endpoints
"""

import csv
import io
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.audit_guard import required_audit
from app.api.date_window import company_period
from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role

# The custom-report export shares the bulk-export row cap with /exports/* (one number for
# "too big to hand over as one file"). Imported as a module global so tests can patch it here.
from app.api.endpoints.exports import MAX_EXPORT_ROWS
from app.db.database import get_db
from app.models.user import User, UserRole
from app.schemas.analytics import (
    REPORT_DEFAULT_LIMIT,
    REPORT_MAX_LIMIT,
    AdoptionMetricsResponse,
    CapacityForecastResponse,
    CostAnalysisResponse,
    CustomReportRequest,
    CustomReportResult,
    DateGranularity,
    DeliveryPrediction,
    FlowMetricsResponse,
    FPYResponse,
    InventoryAnalyticsResponse,
    InventoryDemandResponse,
    KPIDashboard,
    OEEResponse,
    ProductionTrendsResponse,
    QualityMetricsResponse,
    ReportDataSourceInfo,
    ReportTemplateCreate,
    ReportTemplateResponse,
    ReportTemplateUpdate,
    ScrapParetoResponse,
    WIPAgingResponse,
)
from app.services import report_template_service
from app.services.adoption_metrics_service import get_adoption_metrics
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService
from app.services.export_audit import log_export, refuse_file_export_to_narrowed_token
from app.services.export_safety import attachment_content_disposition, sanitize_csv_mapping, sanitize_csv_row
from app.services.flow_metrics_service import get_flow_metrics, get_wip_aging
from app.services.prediction_service import PredictionService
from app.services.quality_yield_service import get_fpy_rty, get_scrap_pareto
from app.services.report_builder import ReportBuilderService, data_source_catalog

router = APIRouter()


# ============ KPI DASHBOARD ============


@router.get("/kpis", response_model=KPIDashboard)
def get_kpi_dashboard(
    period: str = Query("30d", description="Period: today, 7d, 30d, 90d, ytd, custom"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Get all KPIs for the executive dashboard."""
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    service = AnalyticsService(db, company_id, zone=calendar.tz, calendar=calendar)
    return service.get_kpi_dashboard(start, end, work_center_id)


# ============ OEE ============


@router.get("/oee", response_model=OEEResponse)
def get_oee_details(
    period: str = Query("30d"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    work_center_id: Optional[int] = None,
    granularity: DateGranularity = DateGranularity.DAY,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Get detailed OEE breakdown with time series."""
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    service = AnalyticsService(db, company_id, zone=calendar.tz, calendar=calendar)
    return service.get_oee_details(start, end, work_center_id, granularity)


# ============ PRODUCTION TRENDS ============


@router.get("/production-trends", response_model=ProductionTrendsResponse)
def get_production_trends(
    period: str = Query("30d"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    group_by: Optional[str] = Query(None, description="Group by: work_center, part, customer"),
    granularity: DateGranularity = DateGranularity.DAY,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Get production trend data for charts."""
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    service = AnalyticsService(db, company_id, zone=calendar.tz, calendar=calendar)
    return service.get_production_trends(start, end, group_by, granularity)


# ============ FLOW & QUALITY METRICS (Lean Phase 1, issue #88) ============


@router.get("/flow", response_model=FlowMetricsResponse)
def get_flow_metrics_endpoint(
    period: str = Query("30d", description="Period: today, 7d, 30d, 90d, ytd, custom"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Measured flow: lead times, queue times, Little's Law throughput, PCE."""
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    return get_flow_metrics(db, company_id, start, end)


@router.get("/wip-aging", response_model=WIPAgingResponse)
def get_wip_aging_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """WIP aging snapshot: open WOs with days since release / in current operation."""
    return get_wip_aging(db, company_id)


@router.get("/fpy", response_model=FPYResponse)
def get_fpy_endpoint(
    period: str = Query("30d", description="Period: today, 7d, 30d, 90d, ytd, custom"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    work_center_id: Optional[int] = None,
    part_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
    ),
    company_id: int = Depends(get_current_company_id),
):
    """First-pass yield / rolled throughput yield per part and work center.

    RTY is omitted when work_center_id is set (it is a full-route metric).
    """
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    return get_fpy_rty(db, company_id, start, end, work_center_id=work_center_id, part_id=part_id)


@router.get("/scrap-pareto", response_model=ScrapParetoResponse)
def get_scrap_pareto_endpoint(
    period: str = Query("30d", description="Period: today, 7d, 30d, 90d, ytd, custom"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    work_center_id: Optional[int] = None,
    part_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
    ),
    company_id: int = Depends(get_current_company_id),
):
    """Scrap quantity/cost Pareto by reason code (uncoded = 'unspecified')."""
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    return get_scrap_pareto(db, company_id, start, end, work_center_id=work_center_id, part_id=part_id)


@router.get("/adoption", response_model=AdoptionMetricsResponse)
def get_adoption_endpoint(
    period: str = Query("30d", description="Period: today, 7d, 30d, 90d, ytd, custom"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Digital adoption (completion channel, clock-in coverage, backfill rate,
    weekly trend) + hidden-factory metrics (rework share, maintenance mix,
    MTBF/MTTR per work center)."""
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    return get_adoption_metrics(db, company_id, start, end)


# ============ COST ANALYSIS ============


@router.get("/cost-analysis", response_model=CostAnalysisResponse)
def get_cost_analysis(
    period: str = Query("30d"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    work_order_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Get cost analysis for completed jobs."""
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    service = AnalyticsService(db, company_id, zone=calendar.tz, calendar=calendar)
    return service.get_cost_analysis(start, end, work_order_id)


# ============ QUALITY METRICS ============


@router.get("/quality-metrics", response_model=QualityMetricsResponse)
def get_quality_metrics(
    period: str = Query("30d"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    metric_type: str = Query("all", description="Metric type: defects, ncrs, yield, all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
    ),
    company_id: int = Depends(get_current_company_id),
):
    """Get quality metrics and Pareto data."""
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    service = AnalyticsService(db, company_id, zone=calendar.tz, calendar=calendar)
    return service.get_quality_metrics(start, end, metric_type)


# ============ INVENTORY ANALYTICS ============


@router.get("/inventory-turnover", response_model=InventoryAnalyticsResponse)
def get_inventory_analytics(
    period: str = Query("90d"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Inventory turnover and value for the company's OWN stock (Admin / Manager / Supervisor).

    Per-part average inventory value, turnover (cost of goods consumed over the period against
    that average) and ``summary.total_inventory_value``. Customer- and supplier-owned property
    held here (AS9100D 8.5.3) is not the company's inventory, so it is left out of every value
    figure; list it with ``GET /inventory/?ownership=customer`` (or ``supplier``).
    """
    start, end, calendar = company_period(db, company_id, period, start_date, end_date)
    service = AnalyticsService(db, company_id, zone=calendar.tz, calendar=calendar)
    return service.get_inventory_analytics(start, end, category)


# ============ CUSTOM REPORT BUILDER ============
#
# Access rule for templates (enforced in ``report_template_service``): reading, running or
# exporting one is ADMIN/MANAGER and only the caller's own, a shared one, or any for an
# ADMIN; changing or deleting one is ADMIN/MANAGER and only its author or an ADMIN (a
# superuser or Platform Admin counts as an ADMIN in both). Listing stays open to any role,
# as it always has. A template in another company is the same 404 as a missing one.


@router.post("/custom-report", response_model=CustomReportResult)
def run_custom_report(
    request: CustomReportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Run an ad-hoc report over one data source and return its rows.

    Read GET /analytics/data-sources first: it lists every data source and, per field, its
    name, label, type and (for enum fields) the allowed values. A definition names fields
    from one source only. Columns may aggregate (sum/avg on number fields, min/max on
    number/date/datetime/string fields, count on any); once any column aggregates or
    ``group_by`` is set, every other column must appear in ``group_by``. Filter operators
    by type: string eq/ne/in/like, number eq/ne/gt/gte/lt/lte/in/between, date and datetime
    eq/ne/gt/gte/lt/lte/between, enum eq/ne/in; dates are YYYY-MM-DD, datetimes ISO-8601
    (no zone = UTC). A sort names a field or a selected column's output key (alias, else
    field). Anything the definition gets wrong is one 422 listing every problem.

    Returns ``columns`` (key, label, type), ``rows`` keyed by column key, ``row_count``,
    ``truncated`` (true when more than ``limit`` rows matched -- add a filter or raise
    ``limit``, max 10,000) and ``notes`` (e.g. ``labor_not_tracked``: labor-derived work
    order columns read 0 because labor costing is off, which means "not tracked", not $0).
    Deleted records are never included. ADMIN or MANAGER only.
    """
    return ReportBuilderService(db).execute_report(request, company_id)


@router.get("/custom-report/export")
def export_custom_report(
    template_id: int,
    format: Literal["csv"] = Query("csv", description="Export file format. Only csv is supported."),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Download a saved report template's full result as a CSV file.

    The caller must own the template or it must be shared (an ADMIN may export any). Every
    matching row is exported (no 1,000-row default); a result over 10,000 rows is refused
    with 400 rather than cut short -- add a filter to the template and export again. A
    template whose saved definition no longer passes validation is refused with 409 (its
    ``definition_errors`` in GET /analytics/custom-report/templates say why). An empty
    result is a 400. The export is recorded in the audit log. A read-only or AI-external
    API token is refused 403 before anything is read. ADMIN or MANAGER only.
    """
    # First, before the template read and the up-to-10,001-row query: a narrowed token can
    # never receive the file (the response middleware would replace it), so running the
    # query for one only costs a full read -- and the over-cap / empty 400s it could reach
    # first would tell it how many rows the template matches.
    refuse_file_export_to_narrowed_token(current_user)
    template = report_template_service.require_readable(db, template_id, company_id, current_user)
    request = report_template_service.request_from_template(template)

    # G3-scope: the data query is scoped to the active company (the template fetch above
    # already was). The export reads up to the shared export cap, never ``request.limit``.
    result = ReportBuilderService(db).execute_report(request, company_id, limit=MAX_EXPORT_ROWS)
    if result.truncated:
        # Refuse rather than truncate (``exports.fetch_export_rows``): a CSV has no channel
        # to say "this is partial". Nothing was disclosed, so nothing is audited.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This export matches more than {MAX_EXPORT_ROWS:,} rows. Add a filter, then export again.",
        )

    # Build the file first: ``_export_csv`` refuses an empty result set with 400,
    # and a refusal disclosed nothing, so it must not leave an EXPORT row behind.
    response = _export_csv(result.rows, [column.key for column in result.columns], template.name)
    # This is the most general bulk export in the system -- a tenant-authored
    # template over a whole data source -- so the row records the template and the
    # query shape it ran, never the rows. ``columns`` comes from the stored
    # template, not the query string, so no ``known_columns`` fence is needed.
    log_export(
        db,
        audit,
        dataset="custom_report",
        label="custom report",
        row_count=result.row_count,
        export_format=format,
        columns=template.columns or [],
        filters={"data_source": template.data_source, "template_filters": template.filters},
        resource_id=template.id,
        resource_identifier=template.name,
    )
    return response


def _export_csv(rows: List[Dict[str, Any]], fieldnames: List[str], name: str) -> StreamingResponse:
    """Render report rows as a CSV download (header row = the output keys)."""
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No data to export")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    # Neutralize spreadsheet formulas (CWE-1236). Header included: the column
    # list comes from a tenant-authored report template, not a fixed allowlist.
    writer.writerow(dict(zip(fieldnames, sanitize_csv_row(fieldnames))))
    writer.writerows(sanitize_csv_mapping(row) for row in rows)

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": attachment_content_disposition(f"{name}.csv")},
    )


@router.get("/custom-report/templates", response_model=List[ReportTemplateResponse])
def list_report_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List the saved report templates the caller can see: their own plus every shared one.

    Each template carries its full definition and ``definition_errors``: a non-empty list
    means the saved definition no longer passes validation, so running or exporting it
    answers 409 until it is edited (PATCH its ``definition``). Any signed-in role may list.
    """
    templates = report_template_service.list_templates(db, company_id, current_user)
    return [report_template_service.template_response(t) for t in templates]


@router.post("/custom-report/templates", response_model=ReportTemplateResponse)
def create_report_template(
    template: ReportTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Save a report definition as a named template (private unless ``is_shared`` is true).

    The definition (data_source, columns, filters, group_by, sort) follows the same rules
    as POST /analytics/custom-report and is validated the same way (one 422 listing every
    problem). ``name`` is 1-255 characters; ``description`` at most 2,000. The creation is
    recorded in the audit log. ADMIN or MANAGER only.
    """
    created = report_template_service.create_template(db, template, company_id, current_user, audit)
    return report_template_service.template_response(created)


@router.get("/custom-report/templates/{template_id}", response_model=ReportTemplateResponse)
def get_report_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Get one saved report template, with its definition and ``definition_errors``.

    The caller must own the template or it must be shared (403 otherwise; an ADMIN may open
    any); an unknown id is 404. ADMIN or MANAGER only.
    """
    template = report_template_service.require_readable(db, template_id, company_id, current_user)
    return report_template_service.template_response(template)


@router.patch("/custom-report/templates/{template_id}", response_model=ReportTemplateResponse)
def update_report_template(
    template_id: int,
    changes: ReportTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Change a saved report template's name, description, sharing or definition.

    Send only the keys that change. ``definition`` replaces the whole definition
    (data_source, columns, filters, group_by, sort) and is validated like a new one --
    this is how a template with ``definition_errors`` is repaired. ``description`` may be
    null to clear it; ``name``, ``is_shared`` and ``definition`` may not be null, and an
    empty body is a 422. Only the template's author or an ADMIN may change it (403
    otherwise). The change is recorded in the audit log with old and new values. ADMIN or
    MANAGER only.
    """
    template = report_template_service.require_modifiable(db, template_id, company_id, current_user)
    updated = report_template_service.update_template(db, template, changes, audit)
    return report_template_service.template_response(updated)


@router.delete("/custom-report/templates/{template_id}")
def delete_report_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete a saved report template.

    Only the template's author or an ADMIN may delete it (403 otherwise). The audit log
    keeps a snapshot of the deleted definition; that audit row is required, so if it cannot
    be saved nothing is deleted (503). ADMIN or MANAGER only.
    """
    template = report_template_service.require_modifiable(db, template_id, company_id, current_user)
    with required_audit(
        db, "The audit record for this delete could not be saved; the template was not deleted. Retry."
    ):
        report_template_service.delete_template(db, template, company_id, audit)
    return {"message": "Template deleted"}


@router.post("/custom-report/templates/{template_id}/run", response_model=CustomReportResult)
def run_report_template(
    template_id: int,
    limit: int = Query(REPORT_DEFAULT_LIMIT, ge=1, le=REPORT_MAX_LIMIT, description="Most rows to return (1-10,000)."),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Run a saved report template and return its rows (same result shape as
    POST /analytics/custom-report).

    The caller must own the template or it must be shared (403 otherwise; an ADMIN may run
    any). A template whose saved definition no longer passes validation answers 409 naming
    the template and its problems; fix it with PATCH. ``truncated`` is true when more than
    ``limit`` rows matched. ADMIN or MANAGER only.
    """
    template = report_template_service.require_readable(db, template_id, company_id, current_user)
    request = report_template_service.request_from_template(template, limit=limit)
    return ReportBuilderService(db).execute_report(request, company_id)


# ============ PREDICTIVE ANALYTICS ============


@router.get("/predict/delivery/{work_order_id}", response_model=DeliveryPrediction)
def predict_delivery_date(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Predict completion date for a work order.

    A work order belonging to another company 404s exactly as an absent id does: the
    refusal carries no identifier and the two are indistinguishable, so the status code
    cannot be used as an existence oracle (#189 convention).
    """
    service = PredictionService(db, company_id)
    try:
        return service.predict_delivery(work_order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/predict/capacity", response_model=CapacityForecastResponse)
def get_capacity_forecast(
    weeks_ahead: int = Query(4, ge=1, le=12),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Get capacity utilization forecast for upcoming weeks."""
    service = PredictionService(db, company_id)
    return service.forecast_capacity(weeks_ahead)


@router.get("/predict/inventory-demand", response_model=InventoryDemandResponse)
def get_inventory_demand_prediction(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Predict inventory stockout dates."""
    service = PredictionService(db, company_id)
    return service.predict_inventory_demand()


# ============ DATA SOURCES ============


@router.get(
    "/data-sources",
    response_model=Dict[str, ReportDataSourceInfo],
    response_model_exclude_none=True,
)
def get_available_data_sources(current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))):
    """List the custom-report data sources and every field each one offers.

    Keyed by data source (the ``data_source`` value a report definition uses); each entry
    has a ``label`` and ``fields``, each with ``name`` (what a column, filter, group_by or
    sort names), ``label``, ``type`` (string, number, date, datetime or enum; it decides
    the allowed operators and aggregates) and, on enum fields only, ``options`` (the
    values a filter on that field takes). This is the complete list: a field not listed
    here is refused. The catalog is static (the same for every company). ADMIN or MANAGER
    only.
    """
    return data_source_catalog()
