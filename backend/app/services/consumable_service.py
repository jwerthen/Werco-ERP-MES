"""Employee-attributed consumable issues against the existing inventory ledger."""

import csv
import hashlib
import io
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError

from app.db.database import atomic_transaction
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.consumable import ConsumableCheckout
from app.models.inventory import InventoryItem, InventoryLocation, InventoryTransaction, TransactionType
from app.models.part import Part, PartType, uom_label
from app.models.user import User, UserRole
from app.schemas.consumables import ConsumableCheckoutResponse, ConsumableStock
from app.services.audit_service import AuditWriteError
from app.services.export_safety import sanitize_csv_row
from app.services.inventory_combine_service import source_reservations
from app.services.material_consumption_service import is_consumable_item

MANAGE_ROLES = (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR)
EXPORT_ROLES = (UserRole.ADMIN, UserRole.MANAGER)
SHOP_TIMEZONE = ZoneInfo("America/Chicago")
CSV_MAX_ROWS = 10_000


def metadata(db, company_id, user):
    # Inactive past recipients remain filterable, never selectable for a new issue.
    historical_ids = tenant_query(db, ConsumableCheckout, company_id).with_entities(ConsumableCheckout.employee_id)
    people = (
        tenant_query(db, User, company_id)
        .filter(or_(User.is_active.is_(True), User.id.in_(historical_ids)))
        .order_by(User.last_name, User.first_name, User.id)
        .all()
    )
    return {
        "can_manage": bool(
            (user.role in MANAGE_ROLES or user.role == UserRole.PLATFORM_ADMIN or user.is_superuser)
            and not getattr(user, "_read_only_company_context", False)
        ),
        "can_export": bool(user.role in EXPORT_ROLES or user.role == UserRole.PLATFORM_ADMIN or user.is_superuser),
        "people": [
            dict(
                id=p.id,
                name=p.full_name,
                department=p.department,
                is_active=bool(p.is_active),
            )
            for p in people
        ],
    }


def stock_rows_query(db, company_id, part_id, *, for_update=False):
    query = tenant_query(db, InventoryItem, company_id).filter(InventoryItem.part_id == part_id)
    query = query.order_by(InventoryItem.id)
    return query.populate_existing().with_for_update() if for_update else query


def _unavailable_reason(item, locations, now):
    if not is_consumable_item(item):
        return "Stock is inactive" if not item.is_active else f"Stock is {item.status}"
    if item.serial_number:
        return "Serialized stock is not supported by consumable checkout; use the existing inventory/tool workflow"
    if item.expiration_date:
        expires = item.expiration_date
        expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
        if expires <= now:
            return "Stock has expired"
    # Free-form legacy location labels remain supported; configured controls bind.
    location = locations.get(item.location)
    if location and (not location.is_active or not location.is_pickable):
        return "Location is inactive or unavailable for picking"
    return None


def _stock_picture(db, company_id, part, rows):
    reservations = source_reservations(db, company_id, part.id)
    locations = {
        location.code: location
        for location in tenant_query(db, InventoryLocation, company_id)
        .filter(InventoryLocation.code.in_({row.location for row in rows}))
        .all()
    }
    now = datetime.now(timezone.utc)
    eligible_total, withheld_total = 0.0, 0.0
    free_by_id, reasons = {}, {}
    for row in rows:
        available = max(0.0, float(row.quantity_on_hand or 0) - float(row.quantity_allocated or 0))
        withheld = min(available, max(0.0, reservations.pinned_by_item.get(row.id, 0)))
        withheld_total += withheld
        reason = _unavailable_reason(row, locations, now)
        free = max(0.0, available - withheld) if reason is None else 0.0
        free_by_id[row.id], reasons[row.id] = free, reason
        eligible_total += free
    part_cap = max(0.0, eligible_total - reservations.unpinned_total(withheld_total))
    result = {}
    for row in rows:
        free = min(free_by_id[row.id], part_cap)
        if uom_label(part.unit_of_measure) == "each":
            free = float(int(free))
        result[row.id] = (free, reasons[row.id] or ("No unreserved stock available" if free <= 0 else None))
    return result


def list_stock(db, company_id, *, q=None, low_stock=False, skip=0, limit=100):
    query = (
        db.query(Part, InventoryItem)
        .outerjoin(InventoryItem, and_(InventoryItem.part_id == Part.id, InventoryItem.company_id == company_id))
        .filter(
            Part.company_id == company_id,
            Part.is_deleted.is_(False),
            Part.is_active.is_(True),
            Part.part_type == PartType.CONSUMABLE,
        )
    )
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Part.name.ilike(term),
                Part.part_number.ilike(term),
                InventoryItem.location.ilike(term),
                InventoryItem.lot_number.ilike(term),
            )
        )
    totals = (
        db.query(InventoryItem.part_id, func.sum(InventoryItem.quantity_on_hand).label("on_hand"))
        .filter(InventoryItem.company_id == company_id, InventoryItem.is_active.is_(True))
        .group_by(InventoryItem.part_id)
        .subquery()
    )
    query = query.outerjoin(totals, totals.c.part_id == Part.id)
    if low_stock:
        query = query.filter(func.coalesce(totals.c.on_hand, 0) <= func.coalesce(Part.reorder_point, 0))
    total = query.count()
    selected = query.order_by(Part.part_number, Part.id, InventoryItem.id).offset(skip).limit(limit).all()
    pictures, part_totals = {}, {}
    for part, _item in selected:
        if part.id not in pictures:
            rows = stock_rows_query(db, company_id, part.id).all()
            pictures[part.id] = _stock_picture(db, company_id, part, rows)
            part_totals[part.id] = sum(float(row.quantity_on_hand or 0) for row in rows if row.is_active)
    items = []
    for part, item in selected:
        available, reason = pictures[part.id][item.id] if item else (0.0, "No stock received")
        on_hand, reorder = part_totals[part.id], float(part.reorder_point or 0)
        items.append(
            ConsumableStock(
                inventory_item_id=item.id if item else None,
                part_id=part.id,
                part_number=part.part_number,
                part_name=part.name,
                unit_of_measure=uom_label(part.unit_of_measure),
                location=item.location if item else None,
                lot_number=item.lot_number if item else None,
                quantity_on_hand=float(item.quantity_on_hand or 0) if item else 0,
                quantity_available=available,
                part_quantity_on_hand=on_hand,
                reorder_point=reorder,
                is_low_stock=on_hand <= reorder,
                can_checkout=available > 0,
                unavailable_reason=reason,
            )
        )
    return {"items": items, "total": total}


def _request_hash(user_id, payload):
    values = payload.model_dump(mode="json", exclude={"request_id"})
    raw = json.dumps([user_id, values], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _replay(db, company_id, user_id, payload):
    existing = (
        tenant_query(db, ConsumableCheckout, company_id)
        .filter(ConsumableCheckout.request_id == str(payload.request_id))
        .first()
    )
    if existing is None:
        return None
    if existing.issued_by != user_id or existing.request_hash != _request_hash(user_id, payload):
        raise HTTPException(409, "This request ID already records a different checkout. Review the saved history.")
    return ConsumableCheckoutResponse.model_validate(existing).model_copy(update={"replayed": True})


def checkout(db, company_id, user, audit, payload):
    try:
        with atomic_transaction(db):
            acquire_generator_lock(db, f"consumable_checkout:{payload.request_id}", company_id)
            replay = _replay(db, company_id, user.id, payload)
            if replay:
                return replay
            employee = (
                tenant_query(db, User, company_id)
                .filter(User.id == payload.employee_id, User.is_active.is_(True))
                .first()
            )
            if employee is None:
                raise HTTPException(404, "Active employee not found")
            # Only the part id is resolved before the fresh stock lock read.
            part_id = (
                tenant_query(db, InventoryItem, company_id)
                .filter(InventoryItem.id == payload.inventory_item_id)
                .with_entities(InventoryItem.part_id)
                .scalar()
            )
            part = (
                tenant_query(db, Part, company_id)
                .filter(
                    Part.id == part_id,
                    Part.is_deleted.is_(False),
                    Part.is_active.is_(True),
                    Part.part_type == PartType.CONSUMABLE,
                )
                .first()
            )
            if part is None:
                raise HTTPException(404, "Active consumable stock not found")
            if uom_label(part.unit_of_measure) == "each" and not payload.quantity.is_integer():
                raise HTTPException(422, "Consumables counted as each require a whole-number quantity")
            rows = stock_rows_query(db, company_id, part.id, for_update=True).all()
            item = next((row for row in rows if row.id == payload.inventory_item_id), None)
            if item is None:
                raise HTTPException(404, "Consumable stock not found")
            available, reason = _stock_picture(db, company_id, part, rows)[item.id]
            if reason or payload.quantity > available:
                raise HTTPException(409, reason or f"Insufficient unreserved stock. Available: {available:g}")
            old_quantity = float(item.quantity_on_hand or 0)
            item.quantity_on_hand = old_quantity - payload.quantity
            item.quantity_available = item.quantity_on_hand - float(item.quantity_allocated or 0)
            issued_at = datetime.now(timezone.utc)
            transaction = InventoryTransaction(
                company_id=company_id,
                inventory_item_id=item.id,
                part_id=part.id,
                transaction_type=TransactionType.ISSUE,
                quantity=-payload.quantity,
                reference_type="consumable_checkout",
                from_location=item.location,
                lot_number=item.lot_number,
                serial_number=item.serial_number,
                unit_cost=float(item.unit_cost or 0),
                total_cost=payload.quantity * float(item.unit_cost or 0),
                notes=payload.notes,
                reason_code="consumable_checkout",
                created_at=issued_at.replace(tzinfo=None),
                created_by=user.id,
            )
            db.add(transaction)
            db.flush()
            record = ConsumableCheckout(
                company_id=company_id,
                inventory_item_id=item.id,
                inventory_transaction_id=transaction.id,
                part_id=part.id,
                part_number=part.part_number,
                part_name=part.name,
                unit_of_measure=uom_label(part.unit_of_measure),
                location=item.location,
                lot_number=item.lot_number,
                employee_id=employee.id,
                employee_name=employee.full_name,
                employee_code=employee.employee_id,
                department=employee.department,
                issued_by=user.id,
                issued_by_name=user.full_name,
                issued_at=issued_at,
                quantity=payload.quantity,
                notes=payload.notes,
                request_id=str(payload.request_id),
                request_hash=_request_hash(user.id, payload),
            )
            db.add(record)
            db.flush()
            transaction.reference_id, transaction.reference_number = record.id, f"CC-{record.id:06d}"
            response = ConsumableCheckoutResponse.model_validate(record)
            audit.log_required(
                "CREATE",
                "consumable_checkout",
                company_id=company_id,
                resource_id=record.id,
                resource_identifier=transaction.reference_number,
                description=f"Issued {payload.quantity:g} {record.unit_of_measure} to {record.employee_name}",
                new_values={**response.model_dump(mode="json"), "inventory_transaction_id": transaction.id},
            )
            audit.log_required(
                "CREATE",
                "inventory",
                company_id=company_id,
                resource_id=transaction.id,
                resource_identifier=str(transaction.id),
                description="Consumable checkout inventory issue",
                new_values={"checkout_id": record.id, "quantity": -payload.quantity, "inventory_item_id": item.id},
            )
            audit.log_required(
                "UPDATE",
                "inventory",
                company_id=company_id,
                resource_id=item.id,
                resource_identifier=f"inventory_item {item.id}",
                description="Consumable checkout stock decrease",
                old_values={"quantity_on_hand": old_quantity},
                new_values={"quantity_on_hand": item.quantity_on_hand},
            )
            return response
    except AuditWriteError as exc:
        raise HTTPException(503, "Unable to save audit record. No checkout was committed.") from exc
    except IntegrityError as exc:
        # The unique index remains the final guard after the production advisory lock.
        replay = _replay(db, company_id, user.id, payload)
        if replay:
            return replay
        raise HTTPException(
            409, "Checkout could not be saved. Refresh stock and retry with the same request ID."
        ) from exc


def history_query(db, company_id, *, q=None, employee_id=None, part_id=None, date_from=None, date_to=None):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "Start date must be on or before end date")
    query = tenant_query(db, ConsumableCheckout, company_id)
    if employee_id is not None:
        query = query.filter(ConsumableCheckout.employee_id == employee_id)
    if part_id is not None:
        query = query.filter(ConsumableCheckout.part_id == part_id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ConsumableCheckout.part_number.ilike(term),
                ConsumableCheckout.part_name.ilike(term),
                ConsumableCheckout.employee_name.ilike(term),
                ConsumableCheckout.issued_by_name.ilike(term),
                ConsumableCheckout.location.ilike(term),
                ConsumableCheckout.lot_number.ilike(term),
                ConsumableCheckout.department.ilike(term),
                ConsumableCheckout.notes.ilike(term),
            )
        )
    if date_from:
        start = datetime.combine(date_from, time.min, SHOP_TIMEZONE).astimezone(timezone.utc)
        query = query.filter(ConsumableCheckout.issued_at >= start)
    if date_to:
        if date_to == date.max:
            raise HTTPException(422, "End date is outside the supported range")
        end = datetime.combine(date_to + timedelta(days=1), time.min, SHOP_TIMEZONE).astimezone(timezone.utc)
        query = query.filter(ConsumableCheckout.issued_at < end)
    return query


def list_history(db, company_id, *, skip=0, limit=100, **filters):
    query = history_query(db, company_id, **filters)
    total = query.count()
    rows = (
        query.order_by(ConsumableCheckout.issued_at.desc(), ConsumableCheckout.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return {"items": [ConsumableCheckoutResponse.model_validate(row) for row in rows], "total": total}


def export_history(db, company_id, audit, **filters):
    query = history_query(db, company_id, **filters)
    rows = (
        query.order_by(ConsumableCheckout.issued_at.desc(), ConsumableCheckout.id.desc()).limit(CSV_MAX_ROWS + 1).all()
    )
    if len(rows) > CSV_MAX_ROWS:
        raise HTTPException(422, "Export exceeds 10,000 checkouts. Narrow the date range or filters.")
    # The audit commit expires ORM objects. Freeze safe values before it
    # so generating a large CSV does not re-query every checkout after commit.
    snapshots = [ConsumableCheckoutResponse.model_validate(row) for row in rows]
    columns = [
        "Checkout",
        "Issued at (America/Chicago)",
        "Employee",
        "Department",
        "Item number",
        "Consumable",
        "Quantity",
        "Unit",
        "Location",
        "Lot",
        "Issued by",
        "Notes",
    ]
    # Same disclosure metadata as export_audit.log_export, with required
    # evidence for this new endpoint. Legacy exporters retain their own policy.
    try:
        with atomic_transaction(db):
            audit.log_required(
                "EXPORT",
                "consumable_checkout",
                company_id=company_id,
                description=f"Exported {len(snapshots)} consumable checkout record(s) to CSV",
                new_values={
                    "format": "csv",
                    "columns": columns,
                    "filters": {
                        key: value.isoformat() if isinstance(value, date) else value
                        for key, value in filters.items()
                        if value is not None
                    },
                },
            )
    except AuditWriteError as exc:
        raise HTTPException(503, "Unable to save export audit record. No file was released.") from exc
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in snapshots:
        writer.writerow(
            sanitize_csv_row(
                [
                    f"CC-{row.id:06d}",
                    row.issued_at.astimezone(SHOP_TIMEZONE).isoformat(),
                    row.employee_name,
                    row.department,
                    row.part_number,
                    row.part_name,
                    row.quantity,
                    row.unit_of_measure,
                    row.location,
                    row.lot_number,
                    row.issued_by_name,
                    row.notes,
                ]
            )
        )
    return output.getvalue()
