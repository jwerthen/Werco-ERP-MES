"""Print-friendly report endpoints that return all data needed for printing."""
from typing import Optional, List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel

from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.quote import Quote, QuoteLine
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.models.part import Part

router = APIRouter()


def format_date(d) -> Optional[str]:
    """Format date for display."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.strftime("%m/%d/%Y %I:%M %p")
    return d.strftime("%m/%d/%Y")


def format_currency(value) -> str:
    """Format number as currency."""
    if value is None:
        return "$0.00"
    return f"${float(value):,.2f}"


def format_number(value, decimals: int = 2) -> str:
    """Format number with commas."""
    if value is None:
        return "0"
    return f"{float(value):,.{decimals}f}"


class OperationPrintData(BaseModel):
    sequence: int
    operation_number: Optional[str] = None
    name: str
    description: Optional[str] = None
    work_center_name: Optional[str] = None
    operation_group: Optional[str] = None
    setup_time_hours: float
    run_time_hours: float
    status: str
    component_part_number: Optional[str] = None
    component_quantity: Optional[float] = None


class WorkOrderPrintData(BaseModel):
    work_order_number: str
    part_number: str
    part_name: str
    part_description: Optional[str] = None
    revision: Optional[str] = None
    status: str
    priority: int
    quantity_ordered: str
    quantity_complete: str
    quantity_scrapped: str
    customer_name: Optional[str] = None
    customer_po: Optional[str] = None
    lot_number: Optional[str] = None
    due_date: Optional[str] = None
    must_ship_by: Optional[str] = None
    scheduled_start: Optional[str] = None
    scheduled_end: Optional[str] = None
    actual_start: Optional[str] = None
    actual_end: Optional[str] = None
    notes: Optional[str] = None
    special_instructions: Optional[str] = None
    estimated_hours: str
    actual_hours: str
    operations: List[OperationPrintData]
    total_setup_hours: str
    total_run_hours: str
    created_at: str
    printed_at: str


@router.get("/work-orders/{work_order_id}/print-data", response_model=WorkOrderPrintData)
def get_work_order_print_data(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all data needed to print a work order."""
    wo = db.query(WorkOrder).options(
        joinedload(WorkOrder.part),
        joinedload(WorkOrder.operations).joinedload(WorkOrderOperation.work_center),
        joinedload(WorkOrder.operations).joinedload(WorkOrderOperation.component_part)
    ).filter(WorkOrder.id == work_order_id).first()
    
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    operations = []
    total_setup = 0.0
    total_run = 0.0
    
    for op in sorted(wo.operations, key=lambda x: x.sequence):
        setup = float(op.setup_time_hours or 0)
        run = float(op.run_time_hours or 0)
        total_setup += setup
        total_run += run
        
        operations.append(OperationPrintData(
            sequence=op.sequence,
            operation_number=op.operation_number,
            name=op.name,
            description=op.description,
            work_center_name=op.work_center.name if op.work_center else None,
            operation_group=op.operation_group,
            setup_time_hours=setup,
            run_time_hours=run,
            status=op.status.value if hasattr(op.status, 'value') else op.status,
            component_part_number=op.component_part.part_number if op.component_part else None,
            component_quantity=float(op.component_quantity) if op.component_quantity else None
        ))
    
    return WorkOrderPrintData(
        work_order_number=wo.work_order_number,
        part_number=wo.part.part_number if wo.part else "",
        part_name=wo.part.name if wo.part else "",
        part_description=wo.part.description if wo.part else None,
        revision=wo.part.revision if wo.part else None,
        status=wo.status.value if hasattr(wo.status, 'value') else wo.status,
        priority=wo.priority,
        quantity_ordered=format_number(wo.quantity_ordered, 0),
        quantity_complete=format_number(wo.quantity_complete, 0),
        quantity_scrapped=format_number(wo.quantity_scrapped, 0),
        customer_name=wo.customer_name,
        customer_po=wo.customer_po,
        lot_number=wo.lot_number,
        due_date=format_date(wo.due_date),
        must_ship_by=format_date(wo.must_ship_by),
        scheduled_start=format_date(wo.scheduled_start),
        scheduled_end=format_date(wo.scheduled_end),
        actual_start=format_date(wo.actual_start),
        actual_end=format_date(wo.actual_end),
        notes=wo.notes,
        special_instructions=wo.special_instructions,
        estimated_hours=format_number(wo.estimated_hours),
        actual_hours=format_number(wo.actual_hours),
        operations=operations,
        total_setup_hours=format_number(total_setup),
        total_run_hours=format_number(total_run),
        created_at=format_date(wo.created_at),
        printed_at=format_date(datetime.now())
    )


class QuoteLinePrintData(BaseModel):
    line_number: int
    part_number: Optional[str] = None
    description: str
    quantity: str
    unit_price: str
    line_total: str


class QuotePrintData(BaseModel):
    quote_number: str
    revision: str
    status: str
    customer_name: str
    customer_contact: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_po: Optional[str] = None
    quote_date: str
    valid_until: Optional[str] = None
    lead_time_days: Optional[int] = None
    payment_terms: Optional[str] = None
    subtotal: str
    tax: str
    total: str
    notes: Optional[str] = None
    lines: List[QuoteLinePrintData]
    printed_at: str


@router.get("/quotes/{quote_id}/print-data", response_model=QuotePrintData)
def get_quote_print_data(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all data needed to print a quote."""
    quote = db.query(Quote).options(
        joinedload(Quote.lines).joinedload(QuoteLine.part)
    ).filter(Quote.id == quote_id).first()
    
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    lines = []
    for line in sorted(quote.lines, key=lambda x: x.line_number):
        lines.append(QuoteLinePrintData(
            line_number=line.line_number,
            part_number=line.part.part_number if line.part else None,
            description=line.description,
            quantity=format_number(line.quantity, 0),
            unit_price=format_currency(line.unit_price),
            line_total=format_currency(line.line_total)
        ))
    
    return QuotePrintData(
        quote_number=quote.quote_number,
        revision=quote.revision or "A",
        status=quote.status.value if hasattr(quote.status, 'value') else quote.status,
        customer_name=quote.customer_name,
        customer_contact=quote.customer_contact,
        customer_email=quote.customer_email,
        customer_phone=quote.customer_phone,
        customer_po=quote.customer_po,
        quote_date=format_date(quote.quote_date),
        valid_until=format_date(quote.valid_until),
        lead_time_days=quote.lead_time_days,
        payment_terms=quote.payment_terms,
        subtotal=format_currency(quote.subtotal),
        tax=format_currency(quote.tax),
        total=format_currency(quote.total),
        notes=quote.notes,
        lines=lines,
        printed_at=format_date(datetime.now())
    )


class POLinePrintData(BaseModel):
    line_number: int
    part_number: str
    part_name: str
    quantity_ordered: str
    quantity_received: str
    unit_price: str
    line_total: str
    required_date: Optional[str] = None


class PurchaseOrderPrintData(BaseModel):
    po_number: str
    status: str
    vendor_name: str
    vendor_code: str
    vendor_contact: Optional[str] = None
    vendor_email: Optional[str] = None
    vendor_phone: Optional[str] = None
    vendor_address: Optional[str] = None
    buyer_name: Optional[str] = None
    buyer_email: Optional[str] = None
    order_date: Optional[str] = None
    required_date: Optional[str] = None
    expected_date: Optional[str] = None
    ship_to: Optional[str] = None
    shipping_method: Optional[str] = None
    subtotal: str
    tax: str
    shipping: str
    total: str
    notes: Optional[str] = None
    lines: List[POLinePrintData]
    printed_at: str


@router.get("/purchase-orders/{po_id}/print-data", response_model=PurchaseOrderPrintData)
def get_purchase_order_print_data(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all data needed to print a purchase order."""
    po = db.query(PurchaseOrder).options(
        joinedload(PurchaseOrder.vendor),
        joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part)
    ).filter(PurchaseOrder.id == po_id).first()
    
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    
    vendor_address_parts = []
    if po.vendor:
        if po.vendor.address_line1:
            vendor_address_parts.append(po.vendor.address_line1)
        if po.vendor.address_line2:
            vendor_address_parts.append(po.vendor.address_line2)
        city_state_zip = []
        if po.vendor.city:
            city_state_zip.append(po.vendor.city)
        if po.vendor.state:
            city_state_zip.append(po.vendor.state)
        if po.vendor.postal_code:
            city_state_zip.append(po.vendor.postal_code)
        if city_state_zip:
            vendor_address_parts.append(", ".join(city_state_zip))
    
    lines = []
    for line in sorted(po.lines, key=lambda x: x.line_number):
        lines.append(POLinePrintData(
            line_number=line.line_number,
            part_number=line.part.part_number if line.part else "",
            part_name=line.part.name if line.part else "",
            quantity_ordered=format_number(line.quantity_ordered, 0),
            quantity_received=format_number(line.quantity_received, 0),
            unit_price=format_currency(line.unit_price),
            line_total=format_currency(line.line_total),
            required_date=format_date(line.required_date)
        ))
    
    buyer = None
    if po.created_by:
        buyer = db.query(User).filter(User.id == po.created_by).first()

    return PurchaseOrderPrintData(
        po_number=po.po_number,
        status=po.status.value if hasattr(po.status, 'value') else po.status,
        vendor_name=po.vendor.name if po.vendor else "",
        vendor_code=po.vendor.code if po.vendor else "",
        vendor_contact=po.vendor.contact_name if po.vendor else None,
        vendor_email=po.vendor.email if po.vendor else None,
        vendor_phone=po.vendor.phone if po.vendor else None,
        vendor_address="\n".join(vendor_address_parts) if vendor_address_parts else None,
        buyer_name=buyer.full_name if buyer else None,
        buyer_email=buyer.email if buyer else None,
        order_date=format_date(po.order_date),
        required_date=format_date(po.required_date),
        expected_date=format_date(po.expected_date),
        ship_to=po.ship_to,
        shipping_method=po.shipping_method,
        subtotal=format_currency(po.subtotal),
        tax=format_currency(po.tax),
        shipping=format_currency(po.shipping),
        total=format_currency(po.total),
        notes=po.notes,
        lines=lines,
        printed_at=format_date(datetime.now())
    )
