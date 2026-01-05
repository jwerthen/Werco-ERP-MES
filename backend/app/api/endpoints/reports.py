from typing import Optional
from datetime import datetime, timedelta, date
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, case
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.time_entry import TimeEntry
from app.models.quality import NonConformanceReport, NCRStatus
from app.models.purchasing import PurchaseOrder, POStatus, POReceipt
from app.models.inventory import InventoryItem, InventoryTransaction

router = APIRouter()


@router.get("/production-summary")
def get_production_summary(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get production metrics for dashboard"""
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Work order counts by status
    wo_stats = db.query(
        WorkOrder.status,
        func.count(WorkOrder.id)
    ).filter(
        WorkOrder.created_at >= cutoff
    ).group_by(WorkOrder.status).all()
    
    wo_by_status = {str(s.value if hasattr(s, 'value') else s): c for s, c in wo_stats}
    
    # Completed work orders
    completed_wos = db.query(WorkOrder).filter(
        WorkOrder.status == WorkOrderStatus.COMPLETE,
        WorkOrder.actual_end >= cutoff
    ).all()
    
    total_completed = len(completed_wos)
    on_time = sum(1 for wo in completed_wos if wo.due_date and wo.actual_end and wo.actual_end.date() <= wo.due_date)
    
    # Hours worked
    time_entries = db.query(
        func.sum(TimeEntry.duration_hours)
    ).filter(
        TimeEntry.clock_in >= cutoff,
        TimeEntry.duration_hours != None
    ).scalar() or 0
    
    # Scrap quantity
    scrap_qty = db.query(
        func.sum(WorkOrderOperation.quantity_scrapped)
    ).filter(
        WorkOrderOperation.updated_at >= cutoff
    ).scalar() or 0
    
    produced_qty = db.query(
        func.sum(WorkOrderOperation.quantity_complete)
    ).filter(
        WorkOrderOperation.updated_at >= cutoff
    ).scalar() or 0
    
    return {
        "period_days": days,
        "work_orders_by_status": wo_by_status,
        "total_completed": total_completed,
        "on_time_delivery_count": on_time,
        "on_time_delivery_pct": (on_time / total_completed * 100) if total_completed > 0 else 0,
        "total_hours_worked": round(time_entries, 1),
        "total_produced": produced_qty,
        "total_scrapped": scrap_qty,
        "scrap_rate_pct": (scrap_qty / (produced_qty + scrap_qty) * 100) if (produced_qty + scrap_qty) > 0 else 0
    }


@router.get("/quality-metrics")
def get_quality_metrics(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get quality metrics"""
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # NCR counts
    ncrs = db.query(NonConformanceReport).filter(
        NonConformanceReport.created_at >= cutoff
    ).all()
    
    ncr_by_status = {}
    ncr_by_source = {}
    for ncr in ncrs:
        status = ncr.status.value if hasattr(ncr.status, 'value') else str(ncr.status)
        source = ncr.source.value if hasattr(ncr.source, 'value') else str(ncr.source)
        ncr_by_status[status] = ncr_by_status.get(status, 0) + 1
        ncr_by_source[source] = ncr_by_source.get(source, 0) + 1
    
    # Receiving inspection
    receipts = db.query(POReceipt).filter(
        POReceipt.received_at >= cutoff
    ).all()
    
    total_received_qty = sum(r.quantity_received for r in receipts)
    rejected_qty = sum(r.quantity_rejected for r in receipts)
    
    return {
        "period_days": days,
        "total_ncrs": len(ncrs),
        "open_ncrs": ncr_by_status.get("open", 0),
        "ncr_by_status": ncr_by_status,
        "ncr_by_source": ncr_by_source,
        "receiving_total_qty": total_received_qty,
        "receiving_rejected_qty": rejected_qty,
        "receiving_reject_rate_pct": (rejected_qty / total_received_qty * 100) if total_received_qty > 0 else 0
    }


@router.get("/inventory-value")
def get_inventory_value(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get inventory value summary"""
    from app.models.part import Part
    
    items = db.query(
        InventoryItem.part_id,
        func.sum(InventoryItem.quantity_on_hand).label("qty"),
        Part.standard_cost
    ).join(Part).filter(
        InventoryItem.is_active == True,
        InventoryItem.quantity_on_hand > 0
    ).group_by(InventoryItem.part_id, Part.standard_cost).all()
    
    total_value = sum((item.qty or 0) * (item.standard_cost or 0) for item in items)
    total_qty = sum(item.qty or 0 for item in items)
    unique_parts = len(items)
    
    return {
        "total_value": round(total_value, 2),
        "total_quantity": total_qty,
        "unique_parts": unique_parts
    }


@router.get("/vendor-performance")
def get_vendor_performance(
    days: int = 90,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get vendor performance metrics"""
    from app.models.purchasing import Vendor, PurchaseOrderLine
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    vendors = db.query(Vendor).filter(Vendor.is_active == True).all()
    
    result = []
    for vendor in vendors:
        # Get PO lines for vendor
        lines = db.query(PurchaseOrderLine).join(PurchaseOrder).filter(
            PurchaseOrder.vendor_id == vendor.id,
            PurchaseOrder.created_at >= cutoff
        ).all()
        
        if not lines:
            continue
        
        total_ordered = sum(l.quantity_ordered for l in lines)
        total_received = sum(l.quantity_received for l in lines)
        
        # Get receipts for reject rate
        receipts = db.query(POReceipt).join(PurchaseOrderLine).join(PurchaseOrder).filter(
            PurchaseOrder.vendor_id == vendor.id,
            POReceipt.received_at >= cutoff
        ).all()
        
        received_qty = sum(r.quantity_received for r in receipts)
        rejected_qty = sum(r.quantity_rejected for r in receipts)
        
        result.append({
            "vendor_id": vendor.id,
            "vendor_code": vendor.code,
            "vendor_name": vendor.name,
            "total_ordered": total_ordered,
            "total_received": total_received,
            "fill_rate_pct": (total_received / total_ordered * 100) if total_ordered > 0 else 0,
            "reject_rate_pct": (rejected_qty / received_qty * 100) if received_qty > 0 else 0,
            "po_count": len(set(l.purchase_order_id for l in lines))
        })
    
    return sorted(result, key=lambda x: x["total_ordered"], reverse=True)


@router.get("/work-center-utilization")
def get_work_center_utilization(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get work center utilization"""
    from app.models.work_center import WorkCenter
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    work_centers = db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
    
    result = []
    for wc in work_centers:
        # Get time entries for work center
        hours = db.query(
            func.sum(TimeEntry.duration_hours)
        ).filter(
            TimeEntry.work_center_id == wc.id,
            TimeEntry.clock_in >= cutoff,
            TimeEntry.duration_hours != None
        ).scalar() or 0
        
        # Assume 8 hours/day available
        available_hours = days * 8
        
        result.append({
            "work_center_id": wc.id,
            "work_center_code": wc.code,
            "work_center_name": wc.name,
            "hours_worked": round(hours, 1),
            "available_hours": available_hours,
            "utilization_pct": round((hours / available_hours * 100) if available_hours > 0 else 0, 1)
        })
    
    return sorted(result, key=lambda x: x["utilization_pct"], reverse=True)


@router.get("/daily-output")
def get_daily_output(
    days: int = 14,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get daily production output"""
    result = []
    
    for i in range(days - 1, -1, -1):
        day = date.today() - timedelta(days=i)
        day_start = datetime.combine(day, datetime.min.time())
        day_end = datetime.combine(day, datetime.max.time())
        
        # Completed operations for the day
        completed = db.query(
            func.sum(WorkOrderOperation.quantity_complete)
        ).filter(
            WorkOrderOperation.actual_end >= day_start,
            WorkOrderOperation.actual_end <= day_end
        ).scalar() or 0
        
        scrapped = db.query(
            func.sum(WorkOrderOperation.quantity_scrapped)
        ).filter(
            WorkOrderOperation.actual_end >= day_start,
            WorkOrderOperation.actual_end <= day_end
        ).scalar() or 0
        
        result.append({
            "date": day.isoformat(),
            "completed": completed,
            "scrapped": scrapped
        })
    
    return result


@router.get("/work-order-costing")
def get_work_order_costing(
    work_order_id: Optional[int] = None,
    days: int = 90,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get work order costing details"""
    from app.models.part import Part
    
    query = db.query(WorkOrder).filter(
        WorkOrder.status.in_([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.IN_PROGRESS])
    )
    
    if work_order_id:
        query = query.filter(WorkOrder.id == work_order_id)
    else:
        cutoff = datetime.utcnow() - timedelta(days=days)
        query = query.filter(WorkOrder.created_at >= cutoff)
    
    work_orders = query.order_by(WorkOrder.created_at.desc()).limit(100).all()
    
    result = []
    for wo in work_orders:
        # Get part info for material cost estimate
        part = db.query(Part).filter(Part.id == wo.part_id).first()
        material_cost = (part.unit_cost or 0) * wo.quantity_ordered if part else 0
        
        # Calculate labor cost from time entries (assume $50/hr standard rate)
        labor_hours = db.query(
            func.sum(TimeEntry.duration_hours)
        ).filter(
            TimeEntry.work_order_id == wo.id,
            TimeEntry.duration_hours != None
        ).scalar() or 0
        
        labor_rate = 50.0  # Could make this configurable per work center
        labor_cost = labor_hours * labor_rate
        
        # Overhead (typically 100-150% of labor for manufacturing)
        overhead_rate = 1.0
        overhead_cost = labor_cost * overhead_rate
        
        # Total actual cost
        actual_total = material_cost + labor_cost + overhead_cost
        
        # Estimated costs
        estimated_labor = (wo.estimated_hours or 0) * labor_rate
        estimated_overhead = estimated_labor * overhead_rate
        estimated_total = material_cost + estimated_labor + estimated_overhead
        
        result.append({
            "work_order_id": wo.id,
            "work_order_number": wo.work_order_number,
            "part_number": part.part_number if part else None,
            "part_name": part.name if part else None,
            "quantity": wo.quantity_ordered,
            "status": wo.status.value,
            "customer_name": wo.customer_name,
            # Estimated
            "estimated_hours": wo.estimated_hours or 0,
            "estimated_material": material_cost,
            "estimated_labor": estimated_labor,
            "estimated_overhead": estimated_overhead,
            "estimated_total": estimated_total,
            # Actual
            "actual_hours": labor_hours,
            "actual_material": material_cost,  # Same as estimated for now
            "actual_labor": labor_cost,
            "actual_overhead": overhead_cost,
            "actual_total": actual_total,
            # Variance
            "hours_variance": labor_hours - (wo.estimated_hours or 0),
            "cost_variance": actual_total - estimated_total,
            "variance_pct": ((actual_total - estimated_total) / estimated_total * 100) if estimated_total > 0 else 0
        })
    
    return result


@router.get("/employee-time")
def get_employee_time_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get employee time report"""
    if not start_date:
        start_date = date.today() - timedelta(days=7)
    if not end_date:
        end_date = date.today()
    
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())
    
    query = db.query(TimeEntry).filter(
        TimeEntry.clock_in >= start_dt,
        TimeEntry.clock_in <= end_dt
    )
    
    if user_id:
        query = query.filter(TimeEntry.user_id == user_id)
    
    entries = query.order_by(TimeEntry.clock_in.desc()).all()
    
    # Group by employee
    by_employee = {}
    for entry in entries:
        uid = entry.user_id
        if uid not in by_employee:
            user = db.query(User).filter(User.id == uid).first()
            by_employee[uid] = {
                "user_id": uid,
                "employee_name": user.full_name if user else f"User {uid}",
                "total_hours": 0,
                "entries": []
            }
        
        hours = entry.duration_hours or 0
        by_employee[uid]["total_hours"] += hours
        by_employee[uid]["entries"].append({
            "date": entry.clock_in.date().isoformat() if entry.clock_in else None,
            "clock_in": entry.clock_in.isoformat() if entry.clock_in else None,
            "clock_out": entry.clock_out.isoformat() if entry.clock_out else None,
            "hours": hours,
            "work_order_number": entry.work_order.work_order_number if entry.work_order else None,
            "operation": entry.operation.name if entry.operation else None,
            "work_center": entry.work_center.name if entry.work_center else None
        })
    
    result = list(by_employee.values())
    for emp in result:
        emp["total_hours"] = round(emp["total_hours"], 2)
    
    return sorted(result, key=lambda x: x["total_hours"], reverse=True)
