"""Tenant-scoped count workspaces and short-lived, exact-stock adjustment reviews."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.inventory import CycleCount, CycleCountItem, CycleCountStatus, InventoryItem, InventoryLocation
from app.models.part import Part
from app.models.user import User, UserRole


def _signing_key() -> str:
    return hashlib.sha256((settings.SECRET_KEY + ":cycle-count-review-v1").encode()).hexdigest()


def count_or_404(db: Session, company_id: int, count_id: int, *, lock: bool = False) -> CycleCount:
    query = db.query(CycleCount).filter(CycleCount.company_id == company_id, CycleCount.id == count_id)
    if lock:
        query = query.populate_existing().with_for_update()
    count = query.first()
    if not count:
        raise HTTPException(404, "Cycle count not found")
    return count


def eligible_assignee(db: Session, company_id: int, user_id: int) -> User:
    user = (
        db.query(User)
        .filter(User.company_id == company_id, User.id == user_id, User.is_active == True, User.role != UserRole.VIEWER)
        .first()
    )
    if not user:
        raise HTTPException(404, "Active counter not found")
    return user


def count_summary(count: CycleCount, names: dict, locations: dict) -> dict:
    return {
        "id": count.id,
        "count_number": count.count_number,
        "status": count.status.value if count.status else "unknown",
        "scheduled_date": count.scheduled_date,
        "started_at": count.started_at,
        "completed_at": count.completed_at,
        "warehouse": count.warehouse,
        "location_code": locations.get(count.location_id),
        "part_id": count.part_id,
        "assigned_to": count.assigned_to,
        "assigned_to_name": names.get(count.assigned_to),
        "total_items": count.total_items or 0,
        "items_counted": count.items_counted or 0,
        "items_adjusted": count.items_adjusted or 0,
        "total_variance_value": count.total_variance_value or 0,
        "notes": count.notes,
    }


def workspace_list(db: Session, company_id: int, *, status, assigned_to, offset: int, limit: int) -> dict:
    query = db.query(CycleCount).filter(CycleCount.company_id == company_id)
    if status:
        query = query.filter(CycleCount.status == status)
    if assigned_to:
        query = query.filter(CycleCount.assigned_to == assigned_to)
    total = query.count()
    counts = query.order_by(CycleCount.scheduled_date.desc(), CycleCount.id.desc()).offset(offset).limit(limit).all()
    names = {
        row.id: row.full_name
        for row in db.query(User)
        .filter(User.company_id == company_id, User.id.in_([c.assigned_to for c in counts]))
        .all()
    }
    locations = dict(
        db.query(InventoryLocation.id, InventoryLocation.code)
        .filter(InventoryLocation.company_id == company_id, InventoryLocation.id.in_([c.location_id for c in counts]))
        .all()
    )
    return {
        "items": [count_summary(c, names, locations) for c in counts],
        "total": total,
        "has_more": offset + len(counts) < total,
    }


def workspace_detail(db: Session, company_id: int, count_id: int) -> dict:
    count = count_or_404(db, company_id, count_id)
    names = {
        user.id: user.full_name
        for user in db.query(User).filter(User.company_id == company_id, User.id == count.assigned_to).all()
    }
    locations = dict(
        db.query(InventoryLocation.id, InventoryLocation.code)
        .filter(InventoryLocation.company_id == company_id, InventoryLocation.id == count.location_id)
        .all()
    )
    # Historical inventory and part identities remain readable when deactivated;
    # the count records the stock row as enrolled, never a new catalog selection.
    rows = (
        db.query(CycleCountItem, InventoryItem, Part)
        .outerjoin(
            InventoryItem,
            and_(InventoryItem.id == CycleCountItem.inventory_item_id, InventoryItem.company_id == company_id),
        )
        .outerjoin(Part, and_(Part.id == InventoryItem.part_id, Part.company_id == company_id))
        .filter(CycleCountItem.company_id == company_id, CycleCountItem.cycle_count_id == count_id)
        .order_by(InventoryItem.location, Part.part_number, CycleCountItem.id)
        .all()
    )
    details = []
    for item, stock, part in rows:
        details.append(
            {
                "id": item.id,
                "inventory_item_id": item.inventory_item_id,
                "part_id": stock.part_id if stock else None,
                "part_number": part.part_number if part else "Unavailable stock row",
                "part_name": part.name if part else "",
                "unit_of_measure": part.unit_of_measure if part else "",
                "location": stock.location if stock else None,
                "lot_number": stock.lot_number if stock else None,
                "serial_number": stock.serial_number if stock else None,
                "system_quantity": item.system_quantity,
                "current_quantity": stock.quantity_on_hand if stock else None,
                "counted_quantity": item.counted_quantity,
                "variance": item.variance,
                "variance_value": item.variance_value,
                "posting_delta": (
                    item.counted_quantity - stock.quantity_on_hand
                    if stock and item.is_counted and item.counted_quantity is not None
                    else 0
                ),
                "stock_changed": stock is not None and stock.quantity_on_hand != item.system_quantity,
                "is_counted": bool(item.is_counted),
                "requires_recount": bool(item.requires_recount),
                "counted_at": item.counted_at,
                "notes": item.notes,
            }
        )
    return {**count_summary(count, names, locations), "items": details}


def _snapshot(db: Session, company_id: int, count_id: int, *, lock_stock: bool = False) -> dict:
    count = count_or_404(db, company_id, count_id)
    items = (
        db.query(CycleCountItem)
        .filter(CycleCountItem.company_id == company_id, CycleCountItem.cycle_count_id == count_id)
        .populate_existing()
        .order_by(CycleCountItem.id)
        .all()
    )
    stock_ids = sorted({item.inventory_item_id for item in items})
    stock = []
    for start in range(0, len(stock_ids), 500):
        query = (
            db.query(InventoryItem)
            .filter(InventoryItem.company_id == company_id, InventoryItem.id.in_(stock_ids[start : start + 500]))
            .populate_existing()
            .order_by(InventoryItem.id)
        )
        if lock_stock:
            query = query.with_for_update()
        stock.extend(query.all())
    ready = (
        count.status == CycleCountStatus.IN_PROGRESS
        and bool(items)
        and all(i.is_counted and i.counted_quantity is not None and not i.requires_recount for i in items)
        and len(stock) == len(stock_ids)
        and all(row.is_active for row in stock)
    )
    return {
        "ready": ready,
        "status": count.status.value if count.status else None,
        "items": [
            [
                i.id,
                i.inventory_item_id,
                i.system_quantity,
                i.counted_quantity,
                i.variance,
                i.unit_cost,
                i.counted_at.isoformat() if i.counted_at else None,
                i.notes,
                bool(i.requires_recount),
            ]
            for i in items
        ],
        "stock": [
            [
                s.id,
                s.quantity_on_hand,
                s.quantity_allocated,
                s.unit_cost,
                s.location,
                s.lot_number,
                s.status,
                s.is_active,
            ]
            for s in stock
        ],
    }


def _digest(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def review_count(db: Session, company_id: int, user_id: int, count_id: int) -> dict:
    count_or_404(db, company_id, count_id, lock=True)
    snapshot = _snapshot(db, company_id, count_id, lock_stock=True)
    if not snapshot["ready"]:
        raise HTTPException(
            409, "Count every item and resolve unavailable stock or recounts before reviewing adjustments"
        )
    detail = workspace_detail(db, company_id, count_id)
    claims = {
        "company_id": company_id,
        "user_id": user_id,
        "count_id": count_id,
        "snapshot": _digest(snapshot),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    return {**detail, "review_token": jwt.encode(claims, _signing_key(), algorithm="HS256")}


def validate_review(db: Session, company_id: int, user_id: int, count_id: int, token: str) -> None:
    try:
        claims = jwt.decode(token, _signing_key(), algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(409, "Adjustment review expired or is invalid. Review the count again.") from exc
    if (claims.get("company_id"), claims.get("user_id"), claims.get("count_id")) != (company_id, user_id, count_id):
        raise HTTPException(409, "This adjustment review belongs to another count or reviewer")
    snapshot = _snapshot(db, company_id, count_id, lock_stock=True)
    if not snapshot["ready"] or claims.get("snapshot") != _digest(snapshot):
        raise HTTPException(
            409, "Stock or counts changed after review. Review the current quantities again before posting."
        )
