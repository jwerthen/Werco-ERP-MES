"""Tenant-scoped entity search with database ranking and complete, paged counts."""

from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import String, case, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models.bom import BOM
from app.models.customer import Customer
from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias, normalize_alias_key
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quote import Quote
from app.models.routing import Routing
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder

SEARCH_TYPES = ("part", "work_order", "customer", "bom", "routing", "user", "vendor", "purchase_order", "quote")
LINKS = {
    "part": ("/parts/{}", "cube"),
    "work_order": ("/work-orders/{}", "clipboard"),
    "customer": ("/customers?id={}", "building"),
    "bom": ("/bom?id={}", "document"),
    "routing": ("/routing?id={}", "list"),
    "user": ("/users?id={}", "user"),
    "vendor": ("/purchasing?vendor={}", "truck"),
    "purchase_order": ("/purchasing?po={}", "document"),
    "quote": ("/quotes?id={}", "currency"),
}


class SearchResult(BaseModel):
    id: int
    type: str
    title: str
    subtitle: Optional[str] = None
    url: str
    icon: str
    matched_alias: Optional[str] = None

    class Config:
        from_attributes = True


class SearchResponse(BaseModel):
    query: str
    total: int
    results: List[SearchResult]
    categories: dict
    offset: int = 0
    limit: int = 20
    has_more: bool = False


def _work_order_subtitle(work_order) -> str:
    base = f"{work_order.customer_name or ''} - {work_order.status.value}".strip(" -")
    unit = (work_order.unit_number or "").strip()
    return f"{base} - Unit {unit}".strip(" -") if unit else base


def _search_statement(company_id: int, current_user: User, q: str):
    """Build one UNION before ordering/limiting; exact hits cannot be lost to a branch cap."""
    needle = q.lower()
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    contains = f"%{escaped}%"
    prefix = f"{escaped}%"
    statements = []

    def add(kind, model, title, subtitle, fields, conditions=(), join_part=False, alias=None, alias_exact=None):
        exact = or_(*(func.lower(field) == needle for field in fields))
        # A current identifier wins over its retired alias; both beat broad matches.
        ranks = [(exact, 0)]
        if alias_exact is not None:
            ranks.append((alias_exact, 1))
        ranks.append((func.lower(title).like(prefix, escape="\\"), 2))
        matches = [func.lower(field).like(contains, escape="\\") for field in fields]
        if alias is not None:
            matches.append(alias.is_not(None))
        stmt = select(
            model.id.label("id"),
            literal(kind).label("type"),
            title.label("title"),
            cast(subtitle, String).label("subtitle"),
            case(*ranks, else_=3).label("rank"),
            (alias if alias is not None else cast(literal(None), String)).label("matched_alias"),
        ).select_from(model)
        if join_part:
            stmt = stmt.join(Part, model.part_id == Part.id)
        stmt = stmt.where(model.company_id == company_id, *conditions, or_(*matches))
        statements.append(stmt)

    aliases = (
        select(PartNumberAlias.alias_number)
        .where(
            PartNumberAlias.company_id == company_id,
            PartNumberAlias.part_id == Part.id,
            func.lower(PartNumberAlias.alias_number_key).like(contains, escape="\\"),
        )
        .order_by(
            case((PartNumberAlias.alias_number_key == normalize_alias_key(q), 0), else_=1),
            PartNumberAlias.alias_number_key,
            PartNumberAlias.id,
        )
        .limit(1)
        .correlate(Part)
        .scalar_subquery()
    )
    exact_alias = (
        select(PartNumberAlias.id)
        .where(
            PartNumberAlias.company_id == company_id,
            PartNumberAlias.part_id == Part.id,
            PartNumberAlias.alias_number_key == normalize_alias_key(q),
        )
        .correlate(Part)
        .exists()
    )
    add(
        "part",
        Part,
        Part.part_number,
        Part.name,
        [Part.part_number, Part.name, Part.description, Part.customer_part_number],
        [Part.is_active.is_(True), Part.is_deleted.is_(False)],
        alias=aliases,
        alias_exact=exact_alias,
    )
    wo_subtitle = func.trim(func.coalesce(WorkOrder.customer_name, "") + " - " + cast(WorkOrder.status, String), " -")
    wo_subtitle = wo_subtitle + case(
        (
            func.length(func.trim(func.coalesce(WorkOrder.unit_number, ""))) > 0,
            literal(" - Unit ") + func.trim(WorkOrder.unit_number),
        ),
        else_="",
    )
    add(
        "work_order",
        WorkOrder,
        WorkOrder.work_order_number,
        wo_subtitle,
        [
            WorkOrder.work_order_number,
            WorkOrder.customer_po,
            WorkOrder.lot_number,
            WorkOrder.unit_number,
            WorkOrder.customer_name,
        ],
        [WorkOrder.is_deleted.is_(False)],
    )
    add(
        "customer",
        Customer,
        Customer.name,
        Customer.code,
        [Customer.name, Customer.code, Customer.email],
        [Customer.is_active.is_(True), Customer.is_deleted.is_(False)],
    )
    for kind, model in (("bom", BOM), ("routing", Routing)):
        add(
            kind,
            model,
            Part.part_number,
            func.coalesce(Part.name, "") + " - Rev " + func.coalesce(model.revision, ""),
            [Part.part_number, Part.name, model.description],
            [
                model.is_active.is_(True),
                model.is_deleted.is_(False),
                Part.company_id == company_id,
                Part.is_deleted.is_(False),
            ],
            join_part=True,
        )
    if current_user.role in (UserRole.ADMIN, UserRole.MANAGER):
        add(
            "user",
            User,
            func.trim(func.coalesce(User.first_name, "") + " " + func.coalesce(User.last_name, "")),
            User.email,
            [User.first_name, User.last_name, User.email, User.employee_id],
            [User.is_active.is_(True)],
        )
    add(
        "vendor",
        Vendor,
        Vendor.name,
        Vendor.code,
        [Vendor.name, Vendor.code],
        [Vendor.is_active.is_(True), Vendor.is_deleted.is_(False)],
    )
    add(
        "purchase_order",
        PurchaseOrder,
        PurchaseOrder.po_number,
        PurchaseOrder.status,
        [PurchaseOrder.po_number],
        [PurchaseOrder.is_deleted.is_(False)],
    )
    add("quote", Quote, Quote.quote_number, Quote.customer_name, [Quote.quote_number, Quote.customer_name])
    return union_all(*statements).subquery("search_matches")


def run_global_search(
    *,
    db: Session,
    company_id: int,
    current_user: User,
    q: str,
    limit: int = 20,
    types: Optional[str] = None,
    offset: int = 0,
) -> SearchResponse:
    selected = ({item.strip().lower() for item in types.split(",") if item.strip()} or None) if types else None
    if selected and not selected.issubset(SEARCH_TYPES):
        raise HTTPException(status_code=422, detail="Unknown search type")
    q = q.strip()
    if not q:
        return SearchResponse(query=q, total=0, categories={}, results=[], offset=offset, limit=limit)
    matches = _search_statement(company_id, current_user, q)
    categories = dict(db.execute(select(matches.c.type, func.count()).group_by(matches.c.type)).all())
    if selected is not None:
        # Callers such as Copilot explicitly exclude people. Their response
        # must not disclose even a count for an excluded record type.
        categories = {kind: count for kind, count in categories.items() if kind in selected}
    total = sum(count for kind, count in categories.items() if selected is None or kind in selected)
    stmt = select(matches)
    if selected:
        stmt = stmt.where(matches.c.type.in_(sorted(selected)))
    stmt = stmt.order_by(matches.c.rank, func.lower(matches.c.title), matches.c.type, matches.c.id)
    rows = db.execute(stmt.offset(offset).limit(limit)).mappings().all()
    results = [
        SearchResult(
            id=row["id"],
            type=row["type"],
            title=row["title"],
            subtitle=row["subtitle"],
            matched_alias=row["matched_alias"],
            url=LINKS[row["type"]][0].format(row["id"]),
            icon=LINKS[row["type"]][1],
        )
        for row in rows
    ]
    return SearchResponse(
        query=q,
        total=total,
        categories=categories,
        results=results,
        offset=offset,
        limit=limit,
        has_more=offset + len(results) < total,
    )
