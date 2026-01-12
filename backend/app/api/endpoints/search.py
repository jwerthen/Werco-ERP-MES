"""
Global Search API Endpoint

Searches across all major entities in the system:
- Parts
- Work Orders
- Customers
- BOMs
- Routings
- Users
- Inventory Items
- Purchase Orders
- Quotes
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from pydantic import BaseModel
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User, UserRole
from app.models.part import Part
from app.models.work_order import WorkOrder
from app.models.customer import Customer
from app.models.bom import BOM
from app.models.routing import Routing
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quote import Quote
from app.models.inventory import InventoryItem

router = APIRouter()


class SearchResult(BaseModel):
    """Individual search result"""
    id: int
    type: str  # part, work_order, customer, bom, routing, user, inventory, purchase_order, quote, vendor
    title: str
    subtitle: Optional[str] = None
    url: str
    icon: str  # Icon identifier for frontend
    
    class Config:
        from_attributes = True


class SearchResponse(BaseModel):
    """Search response with categorized results"""
    query: str
    total: int
    results: List[SearchResult]
    categories: dict  # Count by category


@router.get("/", response_model=SearchResponse)
def global_search(
    q: str = Query(..., min_length=1, max_length=100, description="Search query"),
    limit: int = Query(default=20, le=50, description="Maximum results"),
    types: Optional[str] = Query(default=None, description="Comma-separated types to search"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Global search across all entities.
    
    Search types:
    - part: Parts by part number, name, description
    - work_order: Work orders by number, customer PO, lot number
    - customer: Customers by name, code
    - bom: BOMs by name
    - routing: Routings by name
    - user: Users by name, email, employee ID
    - inventory: Inventory items by location, lot
    - purchase_order: POs by number, vendor
    - quote: Quotes by number, customer
    - vendor: Vendors/Suppliers by name, code
    """
    results: List[SearchResult] = []
    categories = {}
    search_term = f"%{q.lower()}%"
    
    # Parse types filter
    allowed_types = None
    if types:
        allowed_types = [t.strip().lower() for t in types.split(",")]
    
    def should_search(type_name: str) -> bool:
        return allowed_types is None or type_name in allowed_types
    
    # Search Parts
    if should_search("part"):
        parts = db.query(Part).filter(
            Part.is_active == True,
            or_(
                func.lower(Part.part_number).like(search_term),
                func.lower(Part.name).like(search_term),
                func.lower(Part.description).like(search_term),
                func.lower(Part.customer_part_number).like(search_term),
            )
        ).limit(limit).all()
        
        for part in parts:
            results.append(SearchResult(
                id=part.id,
                type="part",
                title=part.part_number,
                subtitle=part.name,
                url=f"/parts?id={part.id}",
                icon="cube"
            ))
        categories["part"] = len(parts)
    
    # Search Work Orders
    if should_search("work_order"):
        work_orders = db.query(WorkOrder).filter(
            or_(
                func.lower(WorkOrder.work_order_number).like(search_term),
                func.lower(WorkOrder.customer_po).like(search_term),
                func.lower(WorkOrder.lot_number).like(search_term),
                func.lower(WorkOrder.customer_name).like(search_term),
            )
        ).limit(limit).all()
        
        for wo in work_orders:
            results.append(SearchResult(
                id=wo.id,
                type="work_order",
                title=wo.work_order_number,
                subtitle=f"{wo.customer_name or ''} - {wo.status.value}".strip(" -"),
                url=f"/work-orders/{wo.id}",
                icon="clipboard"
            ))
        categories["work_order"] = len(work_orders)
    
    # Search Customers
    if should_search("customer"):
        customers = db.query(Customer).filter(
            Customer.is_active == True,
            or_(
                func.lower(Customer.name).like(search_term),
                func.lower(Customer.code).like(search_term),
                func.lower(Customer.email).like(search_term),
            )
        ).limit(limit).all()
        
        for customer in customers:
            results.append(SearchResult(
                id=customer.id,
                type="customer",
                title=customer.name,
                subtitle=customer.code,
                url=f"/customers?id={customer.id}",
                icon="building"
            ))
        categories["customer"] = len(customers)
    
    # Search BOMs
    if should_search("bom"):
        boms = db.query(BOM).filter(
            BOM.is_active == True,
            or_(
                func.lower(BOM.name).like(search_term),
                func.lower(BOM.description).like(search_term),
            )
        ).limit(limit).all()
        
        for bom in boms:
            results.append(SearchResult(
                id=bom.id,
                type="bom",
                title=bom.name,
                subtitle=f"Rev {bom.revision}" if bom.revision else None,
                url=f"/bom?id={bom.id}",
                icon="document"
            ))
        categories["bom"] = len(boms)
    
    # Search Routings
    if should_search("routing"):
        routings = db.query(Routing).filter(
            Routing.is_active == True,
            or_(
                func.lower(Routing.name).like(search_term),
                func.lower(Routing.description).like(search_term),
            )
        ).limit(limit).all()
        
        for routing in routings:
            results.append(SearchResult(
                id=routing.id,
                type="routing",
                title=routing.name,
                subtitle=f"Rev {routing.revision}" if routing.revision else None,
                url=f"/routing?id={routing.id}",
                icon="list"
            ))
        categories["routing"] = len(routings)
    
    # Search Users (admin/manager only can see all users)
    if should_search("user") and current_user.role in [UserRole.ADMIN, UserRole.MANAGER]:
        users = db.query(User).filter(
            User.is_active == True,
            or_(
                func.lower(User.first_name).like(search_term),
                func.lower(User.last_name).like(search_term),
                func.lower(User.email).like(search_term),
                func.lower(User.employee_id).like(search_term),
            )
        ).limit(limit).all()
        
        for user in users:
            results.append(SearchResult(
                id=user.id,
                type="user",
                title=user.full_name,
                subtitle=user.email,
                url=f"/users?id={user.id}",
                icon="user"
            ))
        categories["user"] = len(users)
    
    # Search Vendors
    if should_search("vendor"):
        vendors = db.query(Vendor).filter(
            Vendor.is_active == True,
            or_(
                func.lower(Vendor.name).like(search_term),
                func.lower(Vendor.code).like(search_term),
            )
        ).limit(limit).all()
        
        for vendor in vendors:
            results.append(SearchResult(
                id=vendor.id,
                type="vendor",
                title=vendor.name,
                subtitle=vendor.code,
                url=f"/purchasing?vendor={vendor.id}",
                icon="truck"
            ))
        categories["vendor"] = len(vendors)
    
    # Search Purchase Orders
    if should_search("purchase_order"):
        pos = db.query(PurchaseOrder).filter(
            or_(
                func.lower(PurchaseOrder.po_number).like(search_term),
            )
        ).limit(limit).all()
        
        for po in pos:
            results.append(SearchResult(
                id=po.id,
                type="purchase_order",
                title=po.po_number,
                subtitle=po.status,
                url=f"/purchasing?po={po.id}",
                icon="document"
            ))
        categories["purchase_order"] = len(pos)
    
    # Search Quotes
    if should_search("quote"):
        quotes = db.query(Quote).filter(
            or_(
                func.lower(Quote.quote_number).like(search_term),
                func.lower(Quote.customer_name).like(search_term),
                func.lower(Quote.project_name).like(search_term),
            )
        ).limit(limit).all()
        
        for quote in quotes:
            results.append(SearchResult(
                id=quote.id,
                type="quote",
                title=quote.quote_number,
                subtitle=quote.customer_name or quote.project_name,
                url=f"/quotes?id={quote.id}",
                icon="currency"
            ))
        categories["quote"] = len(quotes)
    
    # Sort results by relevance (exact matches first)
    def sort_key(result: SearchResult):
        # Exact match on title gets priority
        if result.title.lower() == q.lower():
            return 0
        # Starts with query
        if result.title.lower().startswith(q.lower()):
            return 1
        return 2
    
    results.sort(key=sort_key)
    
    # Limit total results
    results = results[:limit]
    
    return SearchResponse(
        query=q,
        total=len(results),
        results=results,
        categories=categories
    )


@router.get("/recent")
def get_recent_items(
    limit: int = Query(default=10, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get recently accessed/created items for quick access."""
    results = []
    
    # Recent work orders (last 5)
    recent_wos = db.query(WorkOrder).order_by(
        WorkOrder.updated_at.desc()
    ).limit(5).all()
    
    for wo in recent_wos:
        results.append(SearchResult(
            id=wo.id,
            type="work_order",
            title=wo.work_order_number,
            subtitle=wo.customer_name,
            url=f"/work-orders/{wo.id}",
            icon="clipboard"
        ))
    
    # Recent parts (last 5)
    recent_parts = db.query(Part).filter(
        Part.is_active == True
    ).order_by(
        Part.updated_at.desc()
    ).limit(5).all()
    
    for part in recent_parts:
        results.append(SearchResult(
            id=part.id,
            type="part",
            title=part.part_number,
            subtitle=part.name,
            url=f"/parts?id={part.id}",
            icon="cube"
        ))
    
    return results[:limit]
