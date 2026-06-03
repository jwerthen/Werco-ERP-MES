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

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.bom import BOM
from app.models.customer import Customer
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quote import Quote
from app.models.routing import Routing
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder
from app.models.work_order import WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerStatus

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


class NaturalLanguageSearchRequest(BaseModel):
    query: str
    limit: int = 20


class NaturalLanguageSearchResult(SearchResult):
    explanation: str
    matched_filters: List[str] = []


class NaturalLanguageSearchResponse(BaseModel):
    query: str
    confidence: float
    interpreted_filters: dict
    used_fallback: bool
    results: List[NaturalLanguageSearchResult]


@router.get("/", response_model=SearchResponse)
def global_search(
    q: str = Query(..., min_length=1, max_length=100, description="Search query"),
    limit: int = Query(default=20, le=50, description="Maximum results"),
    types: Optional[str] = Query(default=None, description="Comma-separated types to search"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
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
        parts = (
            db.query(Part)
            .filter(
                Part.company_id == company_id,
                Part.is_active == True,
                or_(
                    func.lower(Part.part_number).like(search_term),
                    func.lower(Part.name).like(search_term),
                    func.lower(Part.description).like(search_term),
                    func.lower(Part.customer_part_number).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for part in parts:
            results.append(
                SearchResult(
                    id=part.id,
                    type="part",
                    title=part.part_number,
                    subtitle=part.name,
                    url=f"/parts/{part.id}",
                    icon="cube",
                )
            )
        categories["part"] = len(parts)

    # Search Work Orders
    if should_search("work_order"):
        work_orders = (
            db.query(WorkOrder)
            .filter(
                WorkOrder.company_id == company_id,
                or_(
                    func.lower(WorkOrder.work_order_number).like(search_term),
                    func.lower(WorkOrder.customer_po).like(search_term),
                    func.lower(WorkOrder.lot_number).like(search_term),
                    func.lower(WorkOrder.customer_name).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for wo in work_orders:
            results.append(
                SearchResult(
                    id=wo.id,
                    type="work_order",
                    title=wo.work_order_number,
                    subtitle=f"{wo.customer_name or ''} - {wo.status.value}".strip(" -"),
                    url=f"/work-orders/{wo.id}",
                    icon="clipboard",
                )
            )
        categories["work_order"] = len(work_orders)

    # Search Customers
    if should_search("customer"):
        customers = (
            db.query(Customer)
            .filter(
                Customer.company_id == company_id,
                Customer.is_active == True,
                or_(
                    func.lower(Customer.name).like(search_term),
                    func.lower(Customer.code).like(search_term),
                    func.lower(Customer.email).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for customer in customers:
            results.append(
                SearchResult(
                    id=customer.id,
                    type="customer",
                    title=customer.name,
                    subtitle=customer.code,
                    url=f"/customers?id={customer.id}",
                    icon="building",
                )
            )
        categories["customer"] = len(customers)

    # Search BOMs
    if should_search("bom"):
        boms = (
            db.query(BOM)
            .join(Part, BOM.part_id == Part.id)
            .filter(
                BOM.company_id == company_id,
                BOM.is_active == True,
                or_(
                    func.lower(Part.part_number).like(search_term),
                    func.lower(Part.name).like(search_term),
                    func.lower(BOM.description).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for bom in boms:
            results.append(
                SearchResult(
                    id=bom.id,
                    type="bom",
                    title=bom.part.part_number if bom.part else f"BOM #{bom.id}",
                    subtitle=f"{bom.part.name if bom.part else 'BOM'} - Rev {bom.revision}".strip(" -"),
                    url=f"/bom?id={bom.id}",
                    icon="document",
                )
            )
        categories["bom"] = len(boms)

    # Search Routings
    if should_search("routing"):
        routings = (
            db.query(Routing)
            .join(Part, Routing.part_id == Part.id)
            .filter(
                Routing.company_id == company_id,
                Routing.is_active == True,
                or_(
                    func.lower(Part.part_number).like(search_term),
                    func.lower(Part.name).like(search_term),
                    func.lower(Routing.description).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for routing in routings:
            results.append(
                SearchResult(
                    id=routing.id,
                    type="routing",
                    title=routing.part.part_number if routing.part else f"Routing #{routing.id}",
                    subtitle=f"{routing.part.name if routing.part else 'Routing'} - Rev {routing.revision}".strip(" -"),
                    url=f"/routing?id={routing.id}",
                    icon="list",
                )
            )
        categories["routing"] = len(routings)

    # Search Users (admin/manager only can see all users)
    if should_search("user") and current_user.role in [UserRole.ADMIN, UserRole.MANAGER]:
        users = (
            db.query(User)
            .filter(
                User.company_id == company_id,
                User.is_active == True,
                or_(
                    func.lower(User.first_name).like(search_term),
                    func.lower(User.last_name).like(search_term),
                    func.lower(User.email).like(search_term),
                    func.lower(User.employee_id).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for user in users:
            results.append(
                SearchResult(
                    id=user.id,
                    type="user",
                    title=user.full_name,
                    subtitle=user.email,
                    url=f"/users?id={user.id}",
                    icon="user",
                )
            )
        categories["user"] = len(users)

    # Search Vendors
    if should_search("vendor"):
        vendors = (
            db.query(Vendor)
            .filter(
                Vendor.company_id == company_id,
                Vendor.is_active == True,
                or_(
                    func.lower(Vendor.name).like(search_term),
                    func.lower(Vendor.code).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for vendor in vendors:
            results.append(
                SearchResult(
                    id=vendor.id,
                    type="vendor",
                    title=vendor.name,
                    subtitle=vendor.code,
                    url=f"/purchasing?vendor={vendor.id}",
                    icon="truck",
                )
            )
        categories["vendor"] = len(vendors)

    # Search Purchase Orders
    if should_search("purchase_order"):
        pos = (
            db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.company_id == company_id,
                or_(
                    func.lower(PurchaseOrder.po_number).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for po in pos:
            results.append(
                SearchResult(
                    id=po.id,
                    type="purchase_order",
                    title=po.po_number,
                    subtitle=po.status,
                    url=f"/purchasing?po={po.id}",
                    icon="document",
                )
            )
        categories["purchase_order"] = len(pos)

    # Search Quotes
    if should_search("quote"):
        quotes = (
            db.query(Quote)
            .filter(
                Quote.company_id == company_id,
                or_(
                    func.lower(Quote.quote_number).like(search_term),
                    func.lower(Quote.customer_name).like(search_term),
                    func.lower(Quote.project_name).like(search_term),
                ),
            )
            .limit(limit)
            .all()
        )

        for quote in quotes:
            results.append(
                SearchResult(
                    id=quote.id,
                    type="quote",
                    title=quote.quote_number,
                    subtitle=quote.customer_name or quote.project_name,
                    url=f"/quotes?id={quote.id}",
                    icon="currency",
                )
            )
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

    return SearchResponse(query=q, total=len(results), results=results, categories=categories)


def _contains_any(query: str, terms: List[str]) -> bool:
    return any(term in query for term in terms)


def _parse_nl_search(query: str) -> dict:
    normalized = " ".join(query.lower().strip().split())
    work_center_terms = []
    for term in ["laser", "weld", "welding", "brake", "press brake", "bend", "saw", "machining", "paint"]:
        if term in normalized:
            work_center_terms.append(term)

    filters = {
        "late": _contains_any(normalized, ["late", "overdue", "past due", "behind"]),
        "blocked": _contains_any(normalized, ["blocked", "waiting", "stuck", "hold", "on hold"]),
        "material_missing": _contains_any(
            normalized,
            ["waiting on material", "no material", "missing material", "material missing", "short material"],
        ),
        "hot": _contains_any(normalized, ["hot", "expedite", "rush", "critical"]),
        "work_center_terms": work_center_terms,
        "active_jobs": _contains_any(normalized, ["job", "jobs", "work order", "work orders", "wo"]),
    }
    filter_count = sum(1 for key, value in filters.items() if key != "work_center_terms" and value) + len(
        work_center_terms
    )
    filters["filter_count"] = filter_count
    return filters


def _literal_work_order_fallback(
    *,
    db: Session,
    company_id: int,
    query: str,
    limit: int,
) -> List[NaturalLanguageSearchResult]:
    search_term = f"%{query.lower()}%"
    rows = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .outerjoin(Part, WorkOrder.part_id == Part.id)
        .filter(
            WorkOrder.company_id == company_id,
            or_(
                func.lower(WorkOrder.work_order_number).like(search_term),
                func.lower(WorkOrder.customer_po).like(search_term),
                func.lower(WorkOrder.lot_number).like(search_term),
                func.lower(WorkOrder.customer_name).like(search_term),
                func.lower(Part.part_number).like(search_term),
                func.lower(Part.name).like(search_term),
            ),
        )
        .order_by(WorkOrder.priority, WorkOrder.due_date)
        .limit(limit)
        .all()
    )
    return [
        NaturalLanguageSearchResult(
            id=wo.id,
            type="work_order",
            title=wo.work_order_number,
            subtitle=f"{wo.part.part_number if wo.part else ''} - {wo.status.value}".strip(" -"),
            url=f"/work-orders/{wo.id}",
            icon="clipboard",
            explanation="Matched literal work-order, customer, or part text.",
            matched_filters=["literal_text"],
        )
        for wo in rows
    ]


@router.post("/nl", response_model=NaturalLanguageSearchResponse)
def natural_language_search(
    request: NaturalLanguageSearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Interpret natural-language operational searches into explainable ERP filters."""
    limit = max(1, min(request.limit or 20, 50))
    filters = _parse_nl_search(request.query)
    if filters["filter_count"] == 0:
        return NaturalLanguageSearchResponse(
            query=request.query,
            confidence=0.35,
            interpreted_filters=filters,
            used_fallback=True,
            results=_literal_work_order_fallback(db=db, company_id=company_id, query=request.query, limit=limit),
        )

    query = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]),
        )
    )

    matched_filters: List[str] = []
    if filters["late"]:
        query = query.filter(WorkOrder.due_date < date.today())
        matched_filters.append("late")
    if filters["hot"]:
        query = query.filter(WorkOrder.priority <= 2)
        matched_filters.append("hot_priority")
    if filters["work_center_terms"]:
        query = query.join(WorkOrderOperation, WorkOrderOperation.work_order_id == WorkOrder.id).join(
            WorkCenter, WorkOrderOperation.work_center_id == WorkCenter.id
        )
        wc_clauses = []
        for term in filters["work_center_terms"]:
            term_filter = f"%{term}%"
            wc_clauses.extend(
                [
                    func.lower(WorkCenter.name).like(term_filter),
                    func.lower(WorkCenter.code).like(term_filter),
                    func.lower(WorkCenter.work_center_type).like(term_filter),
                    func.lower(WorkOrderOperation.name).like(term_filter),
                    func.lower(WorkOrderOperation.operation_group).like(term_filter),
                ]
            )
        query = query.filter(or_(*wc_clauses))
        matched_filters.append("work_center:" + ",".join(filters["work_center_terms"]))

    if filters["material_missing"]:
        query = query.join(
            WorkOrderBlocker,
            and_(
                WorkOrderBlocker.work_order_id == WorkOrder.id,
                WorkOrderBlocker.company_id == company_id,
            ),
        ).filter(
            WorkOrderBlocker.status.in_([WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]),
            WorkOrderBlocker.category == WorkOrderBlockerCategory.MATERIAL_MISSING.value,
        )
        matched_filters.append("material_missing_blocker")
    elif filters["blocked"]:
        query = query.outerjoin(
            WorkOrderBlocker,
            and_(
                WorkOrderBlocker.work_order_id == WorkOrder.id,
                WorkOrderBlocker.company_id == company_id,
                WorkOrderBlocker.status.in_(
                    [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]
                ),
            ),
        ).filter(or_(WorkOrder.status == WorkOrderStatus.ON_HOLD, WorkOrderBlocker.id.isnot(None)))
        matched_filters.append("blocked")

    rows = query.distinct().order_by(WorkOrder.priority, WorkOrder.due_date).limit(limit).all()
    confidence = min(0.95, 0.35 + (0.15 * filters["filter_count"]))
    results = [
        NaturalLanguageSearchResult(
            id=wo.id,
            type="work_order",
            title=wo.work_order_number,
            subtitle=f"{wo.part.part_number if wo.part else ''} - {wo.status.value}".strip(" -"),
            url=f"/work-orders/{wo.id}",
            icon="clipboard",
            explanation=f"Matched operational filters: {', '.join(matched_filters)}.",
            matched_filters=matched_filters,
        )
        for wo in rows
    ]

    return NaturalLanguageSearchResponse(
        query=request.query,
        confidence=round(confidence, 2),
        interpreted_filters=filters,
        used_fallback=False,
        results=results,
    )


@router.get("/recent")
def get_recent_items(
    limit: int = Query(default=10, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get recently accessed/created items for quick access."""
    results = []

    # Recent work orders (last 5)
    recent_wos = (
        db.query(WorkOrder)
        .filter(WorkOrder.company_id == company_id)
        .order_by(WorkOrder.updated_at.desc())
        .limit(5)
        .all()
    )

    for wo in recent_wos:
        results.append(
            SearchResult(
                id=wo.id,
                type="work_order",
                title=wo.work_order_number,
                subtitle=wo.customer_name,
                url=f"/work-orders/{wo.id}",
                icon="clipboard",
            )
        )

    # Recent parts (last 5)
    recent_parts = (
        db.query(Part)
        .filter(Part.company_id == company_id, Part.is_active == True)
        .order_by(Part.updated_at.desc())
        .limit(5)
        .all()
    )

    for part in recent_parts:
        results.append(
            SearchResult(
                id=part.id,
                type="part",
                title=part.part_number,
                subtitle=part.name,
                url=f"/parts/{part.id}",
                icon="cube",
            )
        )

    return results[:limit]
