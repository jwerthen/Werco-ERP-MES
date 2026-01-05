"""
Analytics & Business Intelligence API Endpoints
"""
from typing import Optional, List
from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import csv
import io

from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.analytics import ReportTemplate
from app.services.analytics_service import AnalyticsService, get_date_range
from app.services.prediction_service import PredictionService
from app.schemas.analytics import (
    KPIDashboard, OEEResponse, DateGranularity,
    ProductionTrendsResponse, CostAnalysisResponse,
    QualityMetricsResponse, InventoryAnalyticsResponse,
    CustomReportRequest, ReportTemplateCreate, ReportTemplateResponse,
    DeliveryPrediction, CapacityForecastResponse, InventoryDemandResponse
)

router = APIRouter()


# ============ KPI DASHBOARD ============

@router.get("/kpis", response_model=KPIDashboard)
def get_kpi_dashboard(
    period: str = Query("30d", description="Period: today, 7d, 30d, 90d, ytd, custom"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Get all KPIs for the executive dashboard."""
    start, end = get_date_range(period, start_date, end_date)
    service = AnalyticsService(db)
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
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Get detailed OEE breakdown with time series."""
    start, end = get_date_range(period, start_date, end_date)
    service = AnalyticsService(db)
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
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Get production trend data for charts."""
    start, end = get_date_range(period, start_date, end_date)
    service = AnalyticsService(db)
    return service.get_production_trends(start, end, group_by, granularity)


# ============ COST ANALYSIS ============

@router.get("/cost-analysis", response_model=CostAnalysisResponse)
def get_cost_analysis(
    period: str = Query("30d"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    work_order_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Get cost analysis for completed jobs."""
    start, end = get_date_range(period, start_date, end_date)
    service = AnalyticsService(db)
    return service.get_cost_analysis(start, end, work_order_id)


# ============ QUALITY METRICS ============

@router.get("/quality-metrics", response_model=QualityMetricsResponse)
def get_quality_metrics(
    period: str = Query("30d"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    metric_type: str = Query("all", description="Metric type: defects, ncrs, yield, all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY]))
):
    """Get quality metrics and Pareto data."""
    start, end = get_date_range(period, start_date, end_date)
    service = AnalyticsService(db)
    return service.get_quality_metrics(start, end, metric_type)


# ============ INVENTORY ANALYTICS ============

@router.get("/inventory-turnover", response_model=InventoryAnalyticsResponse)
def get_inventory_analytics(
    period: str = Query("90d"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Get inventory turnover and analytics."""
    start, end = get_date_range(period, start_date, end_date)
    service = AnalyticsService(db)
    return service.get_inventory_analytics(start, end, category)


# ============ CUSTOM REPORT BUILDER ============

@router.post("/custom-report")
def run_custom_report(
    request: CustomReportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Execute a custom report query."""
    # Build dynamic query based on data source
    from app.services.report_builder import ReportBuilderService
    service = ReportBuilderService(db)
    return service.execute_report(request)


@router.get("/custom-report/export")
def export_custom_report(
    template_id: int,
    format: str = Query("csv", description="Export format: csv, xlsx, pdf"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Export a custom report to file."""
    template = db.query(ReportTemplate).filter(ReportTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Report template not found")
    
    # Check access
    if not template.is_shared and template.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied to this report")
    
    from app.services.report_builder import ReportBuilderService
    service = ReportBuilderService(db)
    
    # Build request from template
    request = CustomReportRequest(
        data_source=template.data_source,
        columns=template.columns,
        filters=template.filters,
        group_by=template.group_by,
        sort=template.sort
    )
    
    data = service.execute_report(request)
    
    if format == "csv":
        return _export_csv(data, template.name)
    else:
        raise HTTPException(status_code=400, detail=f"Format {format} not yet supported")


def _export_csv(data: List[dict], filename: str) -> StreamingResponse:
    """Export data to CSV."""
    if not data:
        raise HTTPException(status_code=400, detail="No data to export")
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=data[0].keys())
    writer.writeheader()
    writer.writerows(data)
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
    )


@router.get("/custom-report/templates", response_model=List[ReportTemplateResponse])
def list_report_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List available report templates."""
    # User's own templates + shared templates
    templates = db.query(ReportTemplate).filter(
        (ReportTemplate.created_by == current_user.id) |
        (ReportTemplate.is_shared == True)
    ).order_by(ReportTemplate.name).all()
    
    return templates


@router.post("/custom-report/templates", response_model=ReportTemplateResponse)
def create_report_template(
    template: ReportTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Save a custom report template."""
    db_template = ReportTemplate(
        name=template.name,
        description=template.description,
        data_source=template.data_source.value,
        columns=[c.model_dump() for c in template.columns],
        filters=[f.model_dump() for f in template.filters],
        group_by=[g.model_dump() for g in template.group_by],
        sort=[s.model_dump() for s in template.sort],
        is_shared=template.is_shared,
        created_by=current_user.id
    )
    db.add(db_template)
    db.commit()
    db.refresh(db_template)
    return db_template


@router.delete("/custom-report/templates/{template_id}")
def delete_report_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Delete a report template."""
    template = db.query(ReportTemplate).filter(ReportTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    if template.created_by != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Cannot delete another user's template")
    
    db.delete(template)
    db.commit()
    return {"message": "Template deleted"}


# ============ PREDICTIVE ANALYTICS ============

@router.get("/predict/delivery/{work_order_id}", response_model=DeliveryPrediction)
def predict_delivery_date(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Predict completion date for a work order."""
    service = PredictionService(db)
    try:
        return service.predict_delivery(work_order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/predict/capacity", response_model=CapacityForecastResponse)
def get_capacity_forecast(
    weeks_ahead: int = Query(4, ge=1, le=12),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Get capacity utilization forecast for upcoming weeks."""
    service = PredictionService(db)
    return service.forecast_capacity(weeks_ahead)


@router.get("/predict/inventory-demand", response_model=InventoryDemandResponse)
def get_inventory_demand_prediction(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Predict inventory stockout dates."""
    service = PredictionService(db)
    return service.predict_inventory_demand()


# ============ DATA SOURCES ============

@router.get("/data-sources")
def get_available_data_sources(
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Get available data sources and their fields for report builder."""
    return {
        "work_orders": {
            "label": "Work Orders",
            "fields": [
                {"name": "work_order_number", "type": "string", "label": "WO Number"},
                {"name": "status", "type": "enum", "label": "Status"},
                {"name": "quantity_ordered", "type": "number", "label": "Qty Ordered"},
                {"name": "quantity_complete", "type": "number", "label": "Qty Complete"},
                {"name": "due_date", "type": "date", "label": "Due Date"},
                {"name": "actual_start", "type": "datetime", "label": "Start Date"},
                {"name": "actual_end", "type": "datetime", "label": "End Date"},
                {"name": "customer_name", "type": "string", "label": "Customer"},
                {"name": "estimated_hours", "type": "number", "label": "Est. Hours"},
                {"name": "actual_hours", "type": "number", "label": "Actual Hours"},
                {"name": "estimated_cost", "type": "number", "label": "Est. Cost"},
                {"name": "actual_cost", "type": "number", "label": "Actual Cost"},
            ]
        },
        "parts": {
            "label": "Parts",
            "fields": [
                {"name": "part_number", "type": "string", "label": "Part Number"},
                {"name": "name", "type": "string", "label": "Name"},
                {"name": "part_type", "type": "enum", "label": "Type"},
                {"name": "standard_cost", "type": "number", "label": "Std Cost"},
                {"name": "lead_time_days", "type": "number", "label": "Lead Time"},
            ]
        },
        "inventory": {
            "label": "Inventory",
            "fields": [
                {"name": "part_number", "type": "string", "label": "Part Number"},
                {"name": "quantity_on_hand", "type": "number", "label": "Qty On Hand"},
                {"name": "quantity_allocated", "type": "number", "label": "Qty Allocated"},
                {"name": "location", "type": "string", "label": "Location"},
                {"name": "lot_number", "type": "string", "label": "Lot Number"},
                {"name": "unit_cost", "type": "number", "label": "Unit Cost"},
            ]
        },
        "quality": {
            "label": "Quality (NCRs)",
            "fields": [
                {"name": "ncr_number", "type": "string", "label": "NCR Number"},
                {"name": "status", "type": "enum", "label": "Status"},
                {"name": "source", "type": "enum", "label": "Source"},
                {"name": "disposition", "type": "enum", "label": "Disposition"},
                {"name": "quantity_affected", "type": "number", "label": "Qty Affected"},
                {"name": "detected_date", "type": "date", "label": "Detected Date"},
                {"name": "estimated_cost", "type": "number", "label": "Est. Cost"},
            ]
        },
        "purchasing": {
            "label": "Purchase Orders",
            "fields": [
                {"name": "po_number", "type": "string", "label": "PO Number"},
                {"name": "vendor_name", "type": "string", "label": "Vendor"},
                {"name": "status", "type": "enum", "label": "Status"},
                {"name": "order_date", "type": "date", "label": "Order Date"},
                {"name": "total", "type": "number", "label": "Total"},
            ]
        },
        "quotes": {
            "label": "Quotes",
            "fields": [
                {"name": "quote_number", "type": "string", "label": "Quote Number"},
                {"name": "customer_name", "type": "string", "label": "Customer"},
                {"name": "status", "type": "enum", "label": "Status"},
                {"name": "quote_date", "type": "date", "label": "Quote Date"},
                {"name": "total", "type": "number", "label": "Total"},
            ]
        }
    }
