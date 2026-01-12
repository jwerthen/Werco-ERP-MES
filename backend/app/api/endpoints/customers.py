from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.customer import Customer
from app.services.audit_service import AuditService
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus
from pydantic import BaseModel, EmailStr
from datetime import datetime

router = APIRouter()


class CustomerCreate(BaseModel):
    name: str
    code: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    country: str = "USA"
    ship_to_name: Optional[str] = None
    ship_address_line1: Optional[str] = None
    ship_city: Optional[str] = None
    ship_state: Optional[str] = None
    ship_zip_code: Optional[str] = None
    payment_terms: str = "Net 30"
    requires_coc: bool = True
    requires_fai: bool = False
    special_requirements: Optional[str] = None
    notes: Optional[str] = None


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    country: Optional[str] = None
    ship_to_name: Optional[str] = None
    ship_address_line1: Optional[str] = None
    ship_city: Optional[str] = None
    ship_state: Optional[str] = None
    ship_zip_code: Optional[str] = None
    payment_terms: Optional[str] = None
    requires_coc: Optional[bool] = None
    requires_fai: Optional[bool] = None
    special_requirements: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class CustomerResponse(BaseModel):
    id: int
    name: str
    code: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    payment_terms: Optional[str] = None
    requires_coc: bool
    requires_fai: bool
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


def generate_customer_code(db: Session, name: str) -> str:
    """Generate a customer code from name"""
    # Take first 3 chars of name, uppercase
    base = ''.join(c for c in name.upper() if c.isalnum())[:3]
    if len(base) < 3:
        base = base.ljust(3, 'X')
    
    # Find next number
    existing = db.query(Customer).filter(Customer.code.like(f"{base}%")).count()
    return f"{base}{existing + 1:03d}"


@router.get("/", response_model=List[CustomerResponse])
def list_customers(
    active_only: bool = True,
    search: Optional[str] = None,
    include_deleted: bool = Query(False, description="Include soft-deleted customers (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all customers"""
    query = db.query(Customer)
    
    # Filter out soft-deleted unless explicitly requested by admin
    if not include_deleted or current_user.role != UserRole.ADMIN:
        query = query.filter(Customer.is_deleted == False)
    
    if active_only:
        query = query.filter(Customer.is_active == True)
    
    if search:
        search_filter = f"%{search}%"
        query = query.filter(Customer.name.ilike(search_filter))
    
    customers = query.order_by(Customer.name).all()
    return customers


@router.get("/names")
def list_customer_names(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get simple list of customer names for dropdowns"""
    customers = db.query(Customer.id, Customer.name).filter(
        Customer.is_active == True
    ).order_by(Customer.name).all()
    return [{"id": c.id, "name": c.name} for c in customers]


@router.get("/{customer_id}", response_model=CustomerResponse)
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get customer by ID"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@router.get("/{customer_id}/stats")
def get_customer_stats(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get customer statistics including work order counts"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    # Get parts for this customer
    part_ids = db.query(Part.id).filter(Part.customer_name == customer.name).subquery()
    
    # Count work orders by status
    wo_counts = db.query(
        WorkOrder.status,
        func.count(WorkOrder.id).label('count')
    ).filter(
        WorkOrder.part_id.in_(part_ids)
    ).group_by(WorkOrder.status).all()
    
    status_counts = {status.value: count for status, count in wo_counts}
    total_wos = sum(status_counts.values())
    
    # Get recent work orders
    recent_wos = db.query(WorkOrder).filter(
        WorkOrder.part_id.in_(part_ids)
    ).order_by(WorkOrder.created_at.desc()).limit(10).all()
    
    # Count parts
    part_count = db.query(func.count(Part.id)).filter(Part.customer_name == customer.name).scalar()
    
    return {
        "customer_id": customer_id,
        "customer_name": customer.name,
        "part_count": part_count,
        "work_order_counts": {
            "total": total_wos,
            "by_status": status_counts
        },
        "recent_work_orders": [
            {
                "id": wo.id,
                "work_order_number": wo.work_order_number,
                "status": wo.status.value if hasattr(wo.status, 'value') else wo.status,
                "due_date": wo.due_date.isoformat() if wo.due_date else None,
                "quantity_ordered": float(wo.quantity_ordered),
                "created_at": wo.created_at.isoformat()
            }
            for wo in recent_wos
        ]
    }


@router.post("/", response_model=CustomerResponse)
def create_customer(
    customer_in: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new customer"""
    # Check name uniqueness
    if db.query(Customer).filter(Customer.name == customer_in.name).first():
        raise HTTPException(status_code=400, detail="Customer name already exists")
    
    code = customer_in.code or generate_customer_code(db, customer_in.name)
    
    customer = Customer(
        **customer_in.model_dump(exclude={'code'}),
        code=code
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.put("/{customer_id}", response_model=CustomerResponse)
def update_customer(
    customer_id: int,
    customer_in: CustomerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a customer"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    update_data = customer_in.model_dump(exclude_unset=True)
    
    # Check name uniqueness if changing
    if "name" in update_data and update_data["name"] != customer.name:
        if db.query(Customer).filter(Customer.name == update_data["name"]).first():
            raise HTTPException(status_code=400, detail="Customer name already exists")
    
    for field, value in update_data.items():
        setattr(customer, field, value)
    
    db.commit()
    db.refresh(customer)
    return customer


@router.delete("/{customer_id}")
def delete_customer(
    customer_id: int,
    request: Request,
    hard_delete: bool = Query(False, description="Permanently delete (use with caution)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Soft delete or permanently delete a customer."""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    audit = AuditService(db, current_user, request)
    
    if hard_delete:
        # Check for dependencies
        part_count = db.query(Part).filter(Part.customer_name == customer.name).count()
        if part_count > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot hard delete: Customer has {part_count} associated parts"
            )
        
        audit.log_delete("customer", customer.id, customer.name)
        db.delete(customer)
        db.commit()
        return {"message": "Customer permanently deleted"}
    
    # Soft delete
    customer.soft_delete(current_user.id)
    customer.is_active = False
    db.commit()
    
    audit.log_delete("customer", customer.id, customer.name, soft_delete=True)
    return {"message": "Customer marked as deleted (soft delete)", "can_restore": True}


@router.post("/{customer_id}/restore", summary="Restore a soft-deleted customer")
def restore_customer(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Restore a soft-deleted customer."""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    if not customer.is_deleted:
        raise HTTPException(status_code=400, detail="Customer is not deleted")
    
    audit = AuditService(db, current_user, request)
    
    customer.restore()
    customer.is_active = True
    db.commit()
    
    audit.log_update("customer", customer.id, customer.name,
                    old_values={"is_deleted": True},
                    new_values={"is_deleted": False},
                    action="restore")
    
    return {"message": f"Customer {customer.name} restored"}
