from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.customer import Customer
from app.services.audit_service import AuditService
from app.models.part import Part, PartType
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
    base = "".join(c for c in name.upper() if c.isalnum())[:3]
    if len(base) < 3:
        base = base.ljust(3, "X")

    # Find next number
    existing = db.query(Customer).filter(Customer.code.like(f"{base}%")).count()
    return f"{base}{existing + 1:03d}"


@router.get("/", response_model=List[CustomerResponse])
def list_customers(
    active_only: bool = True,
    search: Optional[str] = None,
    include_deleted: bool = Query(
        False, description="Include soft-deleted customers (admin only)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Get simple list of customer names for dropdowns"""
    customers = (
        db.query(Customer.id, Customer.name)
        .filter(Customer.is_active == True)
        .order_by(Customer.name)
        .all()
    )
    return [{"id": c.id, "name": c.name} for c in customers]


@router.get("/{customer_id}", response_model=CustomerResponse)
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
):
    """Get customer statistics including work order counts"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    customer_parts = (
        db.query(Part)
        .filter(
            Part.customer_name == customer.name,
            Part.is_deleted == False,  # noqa: E712
        )
        .order_by(Part.part_number)
        .all()
    )
    part_ids = [part.id for part in customer_parts]

    work_orders_query = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(WorkOrder.is_deleted == False)  # noqa: E712
    )

    if part_ids:
        work_orders_query = work_orders_query.filter(
            or_(
                WorkOrder.part_id.in_(part_ids),
                WorkOrder.customer_name == customer.name,
            )
        )
    else:
        work_orders_query = work_orders_query.filter(
            WorkOrder.customer_name == customer.name
        )

    work_orders = work_orders_query.order_by(WorkOrder.created_at.desc()).all()

    def _normalize_part_type(value: object) -> str:
        if hasattr(value, "value"):
            return str(value.value)
        return str(value or "")

    def _serialize_part(part: Part) -> dict:
        return {
            "id": part.id,
            "part_number": part.part_number,
            "name": part.name,
            "revision": part.revision,
            "part_type": _normalize_part_type(part.part_type),
            "customer_part_number": part.customer_part_number,
            "is_active": part.is_active,
        }

    def _serialize_work_order(wo: WorkOrder) -> dict:
        status_value = (
            wo.status.value if hasattr(wo.status, "value") else str(wo.status)
        )
        return {
            "id": wo.id,
            "work_order_number": wo.work_order_number,
            "status": status_value,
            "due_date": wo.due_date.isoformat() if wo.due_date else None,
            "quantity_ordered": float(wo.quantity_ordered),
            "created_at": wo.created_at.isoformat() if wo.created_at else None,
            "part_id": wo.part_id,
            "part_number": wo.part.part_number if wo.part else None,
            "part_name": wo.part.name if wo.part else None,
            "customer_name": wo.customer_name,
            "customer_po": wo.customer_po,
        }

    serialized_parts = [_serialize_part(part) for part in customer_parts]
    assemblies = [
        part
        for part in serialized_parts
        if part["part_type"] == PartType.ASSEMBLY.value
    ]
    non_assemblies = [
        part
        for part in serialized_parts
        if part["part_type"] != PartType.ASSEMBLY.value
    ]

    serialized_work_orders = [_serialize_work_order(wo) for wo in work_orders]
    status_counts: dict[str, int] = {}
    for wo in serialized_work_orders:
        status = wo["status"]
        status_counts[status] = status_counts.get(status, 0) + 1

    current_statuses = {
        WorkOrderStatus.DRAFT.value,
        WorkOrderStatus.RELEASED.value,
        WorkOrderStatus.IN_PROGRESS.value,
        WorkOrderStatus.ON_HOLD.value,
    }
    past_statuses = {
        WorkOrderStatus.COMPLETE.value,
        WorkOrderStatus.CLOSED.value,
        WorkOrderStatus.CANCELLED.value,
    }

    current_work_orders = [
        wo for wo in serialized_work_orders if wo["status"] in current_statuses
    ]
    past_work_orders = [
        wo for wo in serialized_work_orders if wo["status"] in past_statuses
    ]
    part_count = len(serialized_parts)

    return {
        "customer_id": customer_id,
        "customer_name": customer.name,
        "part_count": part_count,
        "work_order_counts": {
            "total": len(serialized_work_orders),
            "by_status": status_counts,
        },
        "parts": non_assemblies,
        "assemblies": assemblies,
        "current_work_orders": current_work_orders,
        "past_work_orders": past_work_orders,
        "recent_work_orders": serialized_work_orders[:10],
    }


@router.post("/", response_model=CustomerResponse)
def create_customer(
    customer_in: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new customer"""
    # Check name uniqueness
    if db.query(Customer).filter(Customer.name == customer_in.name).first():
        raise HTTPException(status_code=400, detail="Customer name already exists")

    code = customer_in.code or generate_customer_code(db, customer_in.name)

    customer = Customer(**customer_in.model_dump(exclude={"code"}), code=code)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.put("/{customer_id}", response_model=CustomerResponse)
def update_customer(
    customer_id: int,
    customer_in: CustomerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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
    hard_delete: bool = Query(
        False, description="Permanently delete (use with caution)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
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
                detail=f"Cannot hard delete: Customer has {part_count} associated parts",
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
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
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

    audit.log_update(
        "customer",
        customer.id,
        customer.name,
        old_values={"is_deleted": True},
        new_values={"is_deleted": False},
        action="restore",
    )

    return {"message": f"Customer {customer.name} restored"}
