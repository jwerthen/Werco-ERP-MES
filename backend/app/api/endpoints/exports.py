"""Data export endpoints for CSV and Excel downloads."""
from typing import Optional, List
from datetime import datetime, date
from enum import Enum
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_
import io

from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.part import Part, PartType
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine, POStatus
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.services.export_service import generate_csv, generate_excel

router = APIRouter()


class ExportFormat(str, Enum):
    CSV = "csv"
    XLSX = "xlsx"


def get_content_type(format: ExportFormat) -> str:
    if format == ExportFormat.CSV:
        return "text/csv"
    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def get_file_extension(format: ExportFormat) -> str:
    return format.value


# Work Orders Export
@router.get("/work-orders/export")
def export_work_orders(
    format: ExportFormat = Query(ExportFormat.CSV, description="Export format (csv or xlsx)"),
    start_date: Optional[date] = Query(None, description="Filter by created date from"),
    end_date: Optional[date] = Query(None, description="Filter by created date to"),
    status: Optional[WorkOrderStatus] = Query(None, description="Filter by status"),
    columns: Optional[List[str]] = Query(None, description="Columns to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export work orders to CSV or Excel."""
    query = db.query(WorkOrder).options(joinedload(WorkOrder.part))
    query = query.filter(WorkOrder.is_deleted == False)
    
    if start_date:
        query = query.filter(WorkOrder.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(WorkOrder.created_at <= datetime.combine(end_date, datetime.max.time()))
    if status:
        query = query.filter(WorkOrder.status == status)
    
    work_orders = query.order_by(WorkOrder.created_at.desc()).all()
    
    default_columns = [
        "work_order_number", "part_number", "part_name", "status", "priority",
        "quantity_ordered", "quantity_complete", "quantity_scrapped",
        "customer_name", "customer_po", "lot_number",
        "due_date", "scheduled_start", "actual_start", "actual_end",
        "created_at"
    ]
    
    export_columns = columns if columns else default_columns
    
    data = []
    for wo in work_orders:
        row = {
            "work_order_number": wo.work_order_number,
            "part_number": wo.part.part_number if wo.part else "",
            "part_name": wo.part.name if wo.part else "",
            "status": wo.status.value if hasattr(wo.status, 'value') else wo.status,
            "priority": wo.priority,
            "quantity_ordered": float(wo.quantity_ordered),
            "quantity_complete": float(wo.quantity_complete or 0),
            "quantity_scrapped": float(wo.quantity_scrapped or 0),
            "customer_name": wo.customer_name or "",
            "customer_po": wo.customer_po or "",
            "lot_number": wo.lot_number or "",
            "due_date": wo.due_date,
            "scheduled_start": wo.scheduled_start,
            "actual_start": wo.actual_start,
            "actual_end": wo.actual_end,
            "created_at": wo.created_at
        }
        data.append({k: v for k, v in row.items() if k in export_columns})
    
    filename = f"work_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{get_file_extension(format)}"
    
    if format == ExportFormat.CSV:
        content = generate_csv(data, export_columns)
        return StreamingResponse(
            io.StringIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:
        content = generate_excel(data, export_columns, "Work Orders")
        return StreamingResponse(
            io.BytesIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


# Parts Export
@router.get("/parts/export")
def export_parts(
    format: ExportFormat = Query(ExportFormat.CSV, description="Export format (csv or xlsx)"),
    part_type: Optional[PartType] = Query(None, description="Filter by part type"),
    active_only: bool = Query(True, description="Only export active parts"),
    columns: Optional[List[str]] = Query(None, description="Columns to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export parts to CSV or Excel."""
    query = db.query(Part).filter(Part.is_deleted == False)
    
    if part_type:
        query = query.filter(Part.part_type == part_type)
    if active_only:
        query = query.filter(Part.is_active == True)
    
    parts = query.order_by(Part.part_number).all()
    
    default_columns = [
        "part_number", "name", "description", "part_type", "revision",
        "status", "unit_of_measure", "standard_cost", "lead_time_days",
        "reorder_point", "reorder_quantity", "safety_stock",
        "customer_part_number", "created_at"
    ]
    
    export_columns = columns if columns else default_columns
    
    data = []
    for p in parts:
        row = {
            "part_number": p.part_number,
            "name": p.name,
            "description": p.description or "",
            "part_type": p.part_type.value if hasattr(p.part_type, 'value') else p.part_type,
            "revision": p.revision or "",
            "status": p.status or "",
            "unit_of_measure": p.unit_of_measure.value if hasattr(p.unit_of_measure, 'value') else str(p.unit_of_measure),
            "standard_cost": float(p.standard_cost or 0),
            "lead_time_days": p.lead_time_days,
            "reorder_point": float(p.reorder_point or 0),
            "reorder_quantity": float(p.reorder_quantity or 0),
            "safety_stock": float(p.safety_stock or 0),
            "customer_part_number": p.customer_part_number or "",
            "created_at": p.created_at
        }
        data.append({k: v for k, v in row.items() if k in export_columns})
    
    filename = f"parts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{get_file_extension(format)}"
    
    if format == ExportFormat.CSV:
        content = generate_csv(data, export_columns)
        return StreamingResponse(
            io.StringIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:
        content = generate_excel(data, export_columns, "Parts")
        return StreamingResponse(
            io.BytesIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


# Inventory Export
@router.get("/inventory/export")
def export_inventory(
    format: ExportFormat = Query(ExportFormat.CSV, description="Export format (csv or xlsx)"),
    warehouse: Optional[str] = Query(None, description="Filter by warehouse"),
    has_quantity: bool = Query(True, description="Only items with quantity > 0"),
    columns: Optional[List[str]] = Query(None, description="Columns to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export inventory to CSV or Excel."""
    query = db.query(InventoryItem).options(joinedload(InventoryItem.part))
    query = query.filter(InventoryItem.is_active == True)
    
    if warehouse:
        query = query.filter(InventoryItem.warehouse == warehouse)
    if has_quantity:
        query = query.filter(InventoryItem.quantity_on_hand > 0)
    
    items = query.order_by(InventoryItem.part_id, InventoryItem.location).all()
    
    default_columns = [
        "part_number", "part_name", "location", "warehouse",
        "quantity_on_hand", "quantity_allocated", "quantity_available",
        "lot_number", "serial_number", "unit_cost", "total_value",
        "received_date"
    ]
    
    export_columns = columns if columns else default_columns
    
    data = []
    for item in items:
        qty = float(item.quantity_on_hand or 0)
        cost = float(item.unit_cost or 0)
        row = {
            "part_number": item.part.part_number if item.part else "",
            "part_name": item.part.name if item.part else "",
            "location": item.location or "",
            "warehouse": item.warehouse or "",
            "quantity_on_hand": qty,
            "quantity_allocated": float(item.quantity_allocated or 0),
            "quantity_available": float(item.quantity_available or qty),
            "lot_number": item.lot_number or "",
            "serial_number": item.serial_number or "",
            "unit_cost": cost,
            "total_value": qty * cost,
            "received_date": item.received_date
        }
        data.append({k: v for k, v in row.items() if k in export_columns})
    
    filename = f"inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{get_file_extension(format)}"
    
    if format == ExportFormat.CSV:
        content = generate_csv(data, export_columns)
        return StreamingResponse(
            io.StringIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:
        content = generate_excel(data, export_columns, "Inventory")
        return StreamingResponse(
            io.BytesIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


# Purchase Orders Export
@router.get("/purchase-orders/export")
def export_purchase_orders(
    format: ExportFormat = Query(ExportFormat.CSV, description="Export format (csv or xlsx)"),
    start_date: Optional[date] = Query(None, description="Filter by order date from"),
    end_date: Optional[date] = Query(None, description="Filter by order date to"),
    status: Optional[POStatus] = Query(None, description="Filter by status"),
    vendor_id: Optional[int] = Query(None, description="Filter by vendor"),
    columns: Optional[List[str]] = Query(None, description="Columns to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export purchase orders to CSV or Excel."""
    query = db.query(PurchaseOrder).options(
        joinedload(PurchaseOrder.vendor),
        joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part)
    )
    
    if start_date:
        query = query.filter(PurchaseOrder.order_date >= start_date)
    if end_date:
        query = query.filter(PurchaseOrder.order_date <= end_date)
    if status:
        query = query.filter(PurchaseOrder.status == status)
    if vendor_id:
        query = query.filter(PurchaseOrder.vendor_id == vendor_id)
    
    pos = query.order_by(PurchaseOrder.created_at.desc()).all()
    
    default_columns = [
        "po_number", "vendor_name", "vendor_code", "status",
        "order_date", "required_date", "expected_date",
        "subtotal", "tax", "shipping", "total",
        "line_count", "created_at"
    ]
    
    export_columns = columns if columns else default_columns
    
    data = []
    for po in pos:
        row = {
            "po_number": po.po_number,
            "vendor_name": po.vendor.name if po.vendor else "",
            "vendor_code": po.vendor.code if po.vendor else "",
            "status": po.status.value if hasattr(po.status, 'value') else po.status,
            "order_date": po.order_date,
            "required_date": po.required_date,
            "expected_date": po.expected_date,
            "subtotal": float(po.subtotal or 0),
            "tax": float(po.tax or 0),
            "shipping": float(po.shipping or 0),
            "total": float(po.total or 0),
            "line_count": len(po.lines),
            "created_at": po.created_at
        }
        data.append({k: v for k, v in row.items() if k in export_columns})
    
    filename = f"purchase_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{get_file_extension(format)}"
    
    if format == ExportFormat.CSV:
        content = generate_csv(data, export_columns)
        return StreamingResponse(
            io.StringIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:
        content = generate_excel(data, export_columns, "Purchase Orders")
        return StreamingResponse(
            io.BytesIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


# Purchase Order Lines Export (detailed)
@router.get("/purchase-orders/lines/export")
def export_purchase_order_lines(
    format: ExportFormat = Query(ExportFormat.CSV, description="Export format (csv or xlsx)"),
    start_date: Optional[date] = Query(None, description="Filter by order date from"),
    end_date: Optional[date] = Query(None, description="Filter by order date to"),
    status: Optional[POStatus] = Query(None, description="Filter by PO status"),
    columns: Optional[List[str]] = Query(None, description="Columns to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export purchase order lines (detailed) to CSV or Excel."""
    query = db.query(PurchaseOrderLine).options(
        joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
        joinedload(PurchaseOrderLine.part)
    )
    
    if start_date or end_date or status:
        query = query.join(PurchaseOrder)
        if start_date:
            query = query.filter(PurchaseOrder.order_date >= start_date)
        if end_date:
            query = query.filter(PurchaseOrder.order_date <= end_date)
        if status:
            query = query.filter(PurchaseOrder.status == status)
    
    lines = query.all()
    
    default_columns = [
        "po_number", "line_number", "vendor_name", "part_number", "part_name",
        "quantity_ordered", "quantity_received", "unit_price", "line_total",
        "required_date", "is_closed"
    ]
    
    export_columns = columns if columns else default_columns
    
    data = []
    for line in lines:
        row = {
            "po_number": line.purchase_order.po_number if line.purchase_order else "",
            "line_number": line.line_number,
            "vendor_name": line.purchase_order.vendor.name if line.purchase_order and line.purchase_order.vendor else "",
            "part_number": line.part.part_number if line.part else "",
            "part_name": line.part.name if line.part else "",
            "quantity_ordered": float(line.quantity_ordered or 0),
            "quantity_received": float(line.quantity_received or 0),
            "unit_price": float(line.unit_price or 0),
            "line_total": float(line.line_total or 0),
            "required_date": line.required_date,
            "is_closed": line.is_closed
        }
        data.append({k: v for k, v in row.items() if k in export_columns})
    
    filename = f"po_lines_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{get_file_extension(format)}"
    
    if format == ExportFormat.CSV:
        content = generate_csv(data, export_columns)
        return StreamingResponse(
            io.StringIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:
        content = generate_excel(data, export_columns, "PO Lines")
        return StreamingResponse(
            io.BytesIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


# Quotes Export
@router.get("/quotes/export")
def export_quotes(
    format: ExportFormat = Query(ExportFormat.CSV, description="Export format (csv or xlsx)"),
    start_date: Optional[date] = Query(None, description="Filter by quote date from"),
    end_date: Optional[date] = Query(None, description="Filter by quote date to"),
    status: Optional[QuoteStatus] = Query(None, description="Filter by status"),
    customer: Optional[str] = Query(None, description="Filter by customer name"),
    columns: Optional[List[str]] = Query(None, description="Columns to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export quotes to CSV or Excel."""
    query = db.query(Quote).options(joinedload(Quote.lines))
    
    if start_date:
        query = query.filter(Quote.quote_date >= start_date)
    if end_date:
        query = query.filter(Quote.quote_date <= end_date)
    if status:
        query = query.filter(Quote.status == status)
    if customer:
        query = query.filter(Quote.customer_name.ilike(f"%{customer}%"))
    
    quotes = query.order_by(Quote.created_at.desc()).all()
    
    default_columns = [
        "quote_number", "revision", "customer_name", "customer_contact",
        "customer_email", "status", "quote_date", "valid_until",
        "subtotal", "total", "lead_time_days", "line_count", "created_at"
    ]
    
    export_columns = columns if columns else default_columns
    
    data = []
    for q in quotes:
        row = {
            "quote_number": q.quote_number,
            "revision": q.revision or "",
            "customer_name": q.customer_name or "",
            "customer_contact": q.customer_contact or "",
            "customer_email": q.customer_email or "",
            "status": q.status.value if hasattr(q.status, 'value') else q.status,
            "quote_date": q.quote_date,
            "valid_until": q.valid_until,
            "subtotal": float(q.subtotal or 0),
            "total": float(q.total or 0),
            "lead_time_days": q.lead_time_days,
            "line_count": len(q.lines),
            "created_at": q.created_at
        }
        data.append({k: v for k, v in row.items() if k in export_columns})
    
    filename = f"quotes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{get_file_extension(format)}"
    
    if format == ExportFormat.CSV:
        content = generate_csv(data, export_columns)
        return StreamingResponse(
            io.StringIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:
        content = generate_excel(data, export_columns, "Quotes")
        return StreamingResponse(
            io.BytesIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


# Inventory Transactions Export
@router.get("/inventory/transactions/export")
def export_inventory_transactions(
    format: ExportFormat = Query(ExportFormat.CSV, description="Export format (csv or xlsx)"),
    start_date: Optional[date] = Query(None, description="Filter by transaction date from"),
    end_date: Optional[date] = Query(None, description="Filter by transaction date to"),
    part_id: Optional[int] = Query(None, description="Filter by part"),
    transaction_type: Optional[str] = Query(None, description="Filter by transaction type"),
    columns: Optional[List[str]] = Query(None, description="Columns to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export inventory transactions to CSV or Excel."""
    query = db.query(InventoryTransaction).options(joinedload(InventoryTransaction.part))
    
    if start_date:
        query = query.filter(InventoryTransaction.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(InventoryTransaction.created_at <= datetime.combine(end_date, datetime.max.time()))
    if part_id:
        query = query.filter(InventoryTransaction.part_id == part_id)
    if transaction_type:
        query = query.filter(InventoryTransaction.transaction_type == transaction_type)
    
    transactions = query.order_by(InventoryTransaction.created_at.desc()).limit(10000).all()
    
    default_columns = [
        "part_number", "part_name", "transaction_type", "quantity",
        "from_location", "to_location", "lot_number", "serial_number",
        "reference_type", "reference_number", "unit_cost", "total_cost",
        "reason_code", "notes", "created_at"
    ]
    
    export_columns = columns if columns else default_columns
    
    data = []
    for txn in transactions:
        row = {
            "part_number": txn.part.part_number if txn.part else "",
            "part_name": txn.part.name if txn.part else "",
            "transaction_type": txn.transaction_type.value if hasattr(txn.transaction_type, 'value') else txn.transaction_type,
            "quantity": float(txn.quantity or 0),
            "from_location": txn.from_location or "",
            "to_location": txn.to_location or "",
            "lot_number": txn.lot_number or "",
            "serial_number": txn.serial_number or "",
            "reference_type": txn.reference_type or "",
            "reference_number": txn.reference_number or "",
            "unit_cost": float(txn.unit_cost or 0),
            "total_cost": float(txn.total_cost or 0),
            "reason_code": txn.reason_code or "",
            "notes": txn.notes or "",
            "created_at": txn.created_at
        }
        data.append({k: v for k, v in row.items() if k in export_columns})
    
    filename = f"inventory_transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{get_file_extension(format)}"
    
    if format == ExportFormat.CSV:
        content = generate_csv(data, export_columns)
        return StreamingResponse(
            io.StringIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:
        content = generate_excel(data, export_columns, "Transactions")
        return StreamingResponse(
            io.BytesIO(content),
            media_type=get_content_type(format),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
