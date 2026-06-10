"""Global entity search core, shared by the /search endpoint and Werco Copilot.

The logic here was extracted from ``app/api/endpoints/search.py`` so the
copilot's read-only ``search_erp`` tool can reuse it without going through
HTTP. Behavior matches the original endpoint:

- every query is tenant-scoped to ``company_id``;
- user results are only included for ADMIN / MANAGER callers (the same RBAC
  the endpoint enforced inline);
- result shape, ordering, and limits are unchanged.

One fix over the original: the quote branch referenced ``Quote.project_name``,
a column that does not exist on the model — any search reaching that branch
raised ``AttributeError`` (HTTP 500). Quotes now match on quote number and
customer name only.
"""

from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.bom import BOM
from app.models.customer import Customer
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quote import Quote
from app.models.routing import Routing
from app.models.user import User, UserRole


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


def run_global_search(
    *,
    db: Session,
    company_id: int,
    current_user: User,
    q: str,
    limit: int = 20,
    types: Optional[str] = None,
) -> SearchResponse:
    """Search all major entities, tenant-scoped to ``company_id``.

    ``types`` is an optional comma-separated allowlist of entity types.
    User results are restricted to ADMIN/MANAGER callers.
    """
    from app.models.work_order import WorkOrder

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
                Part.is_active == True,  # noqa: E712
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
                WorkOrder.is_deleted == False,  # noqa: E712 — WorkOrder is soft-delete
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
                Customer.is_active == True,  # noqa: E712
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
                BOM.is_active == True,  # noqa: E712
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
                Routing.is_active == True,  # noqa: E712
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
                User.is_active == True,  # noqa: E712
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
                Vendor.is_active == True,  # noqa: E712
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
                    subtitle=quote.customer_name,
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
