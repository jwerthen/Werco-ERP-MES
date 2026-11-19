"""Customer- and supplier-owned property (AS9100D 8.5.3) -- the shared server rules.

Migration 129 gives ``inventory_items`` and ``tools`` an ``ownership`` kind (``company`` /
``customer`` / ``supplier``) plus the owner reference. ``company`` is the server default and
what every earlier row is, and THE rule of the feature is that company-owned stock behaves
byte-identically to how it behaved before. Everything else in this module serves the two
places owned property must behave differently:

* **Nothing automatic spends it.** The unpinned FIFO draw takes company stock only
  (``material_consumption_service.AUTO_DRAW_ITEM_CLAUSES``); an explicit pin still does.
* **Nothing merges across owners.** Every write that adds into an existing stock row matched
  on part / location / lot also matches the ownership triple (``ownership_match_clauses``),
  so a customer's lot never lands in a company row, nor the reverse. The five merge sites:
  ``/inventory/receive``, the PO receipt (``receiving_commands._add_to_inventory``),
  ``/inventory/transfer``'s destination, the finished-goods receipt, and the combine's
  landing rows.

Owner references resolve **tenant-scoped, before the first mutation**
(``resolve_owner``): a customer must be live (not soft-deleted) in the caller's company, a
vendor must be in the company and not soft-deleted (an INACTIVE vendor is allowed -- its
tooling can sit in the crib after the relationship ends). Anything else is a flat 404, so the
status code is no oracle over another tenant's customers or vendors.

Owner NAMES shown beside a row (``owner_names``) are a historical record, not a selection: they
read through tombstones (a soft-deleted customer still owns the material it owned), always
tenant-scoped, and are batched -- at most one query per owner kind for a whole page.

Changing a stock row's ownership afterwards is its own reasoned verb
(``change_inventory_ownership``, behind ``POST /inventory/items/{item_id}/ownership``): row
lock, required audit with the reason, no ledger row (ownership is not a quantity movement).
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.ledger_filter import LEDGER_QUANTITY_EPSILON
from app.models.customer import Customer
from app.models.inventory import InventoryItem, OwnershipType
from app.models.purchasing import Vendor
from app.schemas.owned_property import ownership_problem, ownership_value
from app.services.audit_service import AuditService

COMPANY = OwnershipType.COMPANY.value
CUSTOMER = OwnershipType.CUSTOMER.value
SUPPLIER = OwnershipType.SUPPLIER.value

# (ownership, owner_customer_id, owner_vendor_id) -- the part of a merge key that names who
# owns the material.
OwnershipKey = Tuple[str, Optional[int], Optional[int]]
COMPANY_OWNERSHIP_KEY: OwnershipKey = (COMPANY, None, None)

# The text ``POST /inventory/items/{id}/ownership`` answers when its required audit row cannot
# be written (``app.api.audit_guard.required_audit``).
OWNERSHIP_AUDIT_UNAVAILABLE_DETAIL = (
    "The audit record for this ownership change could not be saved; nothing was changed. Retry."
)


@dataclass(frozen=True)
class ResolvedOwner:
    """An ownership triple whose owner reference was resolved in the caller's company."""

    ownership: str = COMPANY
    owner_customer_id: Optional[int] = None
    owner_vendor_id: Optional[int] = None
    owner_name: Optional[str] = None

    @property
    def key(self) -> OwnershipKey:
        return (self.ownership, self.owner_customer_id, self.owner_vendor_id)

    @property
    def is_company(self) -> bool:
        return self.ownership == COMPANY

    def columns(self) -> Dict[str, Any]:
        """The three column values, ready to pass to a model constructor."""
        return {
            "ownership": self.ownership,
            "owner_customer_id": self.owner_customer_id,
            "owner_vendor_id": self.owner_vendor_id,
        }


COMPANY_OWNER = ResolvedOwner()


def ownership_key(row: Any) -> OwnershipKey:
    """The ownership triple of a stock row, tool, or any object carrying the three columns.

    An unflushed instance whose ``ownership`` is still ``None`` reads as ``company``, the
    column's own default.
    """
    return (
        ownership_value(getattr(row, "ownership", None) or COMPANY),
        getattr(row, "owner_customer_id", None),
        getattr(row, "owner_vendor_id", None),
    )


def ownership_match_clauses(key: OwnershipKey, model: Any = InventoryItem) -> tuple:
    """WHERE clauses that match exactly this ownership triple, NULL-safe.

    Spliced into every merge-key lookup so stock is only ever added into a row with the same
    owner. ``IS NULL`` rather than ``= NULL`` for an absent owner id: the naive comparison
    compiles to ``= NULL``, which never matches, and every company receive would mint a new
    fragment row (the same trap ``_find_stock_row``'s lot clause documents). For the company
    triple this is ``ownership = 'company' AND owner_customer_id IS NULL AND owner_vendor_id
    IS NULL``, which every pre-129 row satisfies -- company merges are unchanged.
    """
    kind, customer_id, vendor_id = key
    return (
        model.ownership == kind,
        model.owner_customer_id.is_(None) if customer_id is None else model.owner_customer_id == customer_id,
        model.owner_vendor_id.is_(None) if vendor_id is None else model.owner_vendor_id == vendor_id,
    )


def resolve_owner(
    db: Session,
    company_id: int,
    ownership: Any,
    owner_customer_id: Optional[int],
    owner_vendor_id: Optional[int],
) -> ResolvedOwner:
    """Validate an ownership triple against the caller's company. Call it before any write.

    422 when the kind and the owner reference do not pair (the same text the request schemas
    use). 404 when the referenced customer is not a live customer of this company, or the
    vendor is not a non-deleted vendor of this company (inactive is allowed). Never another
    tenant's row, and the 404 does not say whether the id exists elsewhere.
    """
    kind = ownership_value(ownership)
    problem = ownership_problem(kind, owner_customer_id, owner_vendor_id)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if kind == CUSTOMER:
        customer = (
            db.query(Customer.id, Customer.name)
            .filter(
                Customer.id == owner_customer_id,
                Customer.company_id == company_id,
                Customer.is_deleted == False,  # noqa: E712 -- a new owner is a SELECTION
            )
            .first()
        )
        if customer is None:
            raise HTTPException(status_code=404, detail="Owning customer not found")
        return ResolvedOwner(CUSTOMER, owner_customer_id, None, customer.name)
    if kind == SUPPLIER:
        vendor = (
            db.query(Vendor.id, Vendor.name)
            .filter(
                Vendor.id == owner_vendor_id,
                Vendor.company_id == company_id,
                Vendor.is_deleted == False,  # noqa: E712 -- inactive is allowed, deleted is not
            )
            .first()
        )
        if vendor is None:
            raise HTTPException(status_code=404, detail="Owning vendor not found")
        return ResolvedOwner(SUPPLIER, None, owner_vendor_id, vendor.name)
    return COMPANY_OWNER


def owner_names(db: Session, company_id: int, rows: Iterable[Any]) -> Dict[Tuple[str, int], str]:
    """``{(kind, owner id): name}`` for every owner the rows name. At most two queries.

    Tenant-scoped (invariant 1), and deliberately NOT filtered on ``is_deleted``: who owns
    the material is a historical record that must keep naming who it named, so a
    soft-deleted customer's name still shows beside its stock. Only ids actually present
    are queried, so a page of company stock costs nothing.
    """
    customer_ids = set()
    vendor_ids = set()
    for row in rows:
        kind, customer_id, vendor_id = ownership_key(row)
        if kind == CUSTOMER and customer_id is not None:
            customer_ids.add(customer_id)
        elif kind == SUPPLIER and vendor_id is not None:
            vendor_ids.add(vendor_id)
    names: Dict[Tuple[str, int], str] = {}
    if customer_ids:
        for customer_id, name in db.query(Customer.id, Customer.name).filter(
            Customer.company_id == company_id, Customer.id.in_(sorted(customer_ids))
        ):
            names[(CUSTOMER, customer_id)] = name
    if vendor_ids:
        for vendor_id, name in db.query(Vendor.id, Vendor.name).filter(
            Vendor.company_id == company_id, Vendor.id.in_(sorted(vendor_ids))
        ):
            names[(SUPPLIER, vendor_id)] = name
    return names


def owner_name_for(row: Any, names: Dict[Tuple[str, int], str]) -> Optional[str]:
    """The owner's name for one row out of an ``owner_names`` map; ``None`` for company stock."""
    kind, customer_id, vendor_id = ownership_key(row)
    if kind == CUSTOMER and customer_id is not None:
        return names.get((CUSTOMER, customer_id))
    if kind == SUPPLIER and vendor_id is not None:
        return names.get((SUPPLIER, vendor_id))
    return None


def ownership_label(key: OwnershipKey, owner_name: Optional[str]) -> str:
    """Human words for an ownership triple, for refusals and audit descriptions."""
    kind, customer_id, vendor_id = key
    if kind == CUSTOMER:
        return f"customer-owned ({owner_name or f'customer #{customer_id}'})"
    if kind == SUPPLIER:
        return f"supplier-owned ({owner_name or f'vendor #{vendor_id}'})"
    return "company-owned"


def _ownership_values(key: OwnershipKey, owner_name: Optional[str]) -> Dict[str, Any]:
    kind, customer_id, vendor_id = key
    return {
        "ownership": kind,
        "owner_customer_id": customer_id,
        "owner_vendor_id": vendor_id,
        "owner_name": owner_name,
    }


def change_inventory_ownership(
    db: Session,
    company_id: int,
    item_id: int,
    *,
    ownership: Any,
    owner_customer_id: Optional[int],
    owner_vendor_id: Optional[int],
    reason: str,
    audit: AuditService,
) -> Tuple[InventoryItem, Optional[str]]:
    """Re-own one stock row. Flushes, never commits; returns ``(row, new owner name)``.

    Every refusal is raised before the first mutation:

    * 404 -- the row is not this company's (or does not exist);
    * 409 -- the row is deactivated, or holds no stock (on hand zero or below): ownership is
      recorded for material that is physically there;
    * 404 / 422 -- the new owner does not resolve in this company, or the kind and owner do
      not pair (``resolve_owner``);
    * 409 -- the row is already owned that way. Refused rather than accepted silently so no
      empty audit row is written and the caller learns nothing changed.

    The row is locked ``FOR UPDATE`` (``populate_existing`` so the checks read the row as
    locked, not a copy cached earlier in the session) -- the same lock every quantity
    mutator takes, so an ownership change serializes against a receive, transfer, combine or
    draw on the same row. No ledger row is written: ownership is not a quantity movement.
    The REQUIRED audit row (old -> new, with the reason) is written in the same transaction;
    the caller wraps the call in ``required_audit`` so a lost audit row rolls the change back
    (503).
    """
    item = (
        db.query(InventoryItem)
        .filter(InventoryItem.id == item_id, InventoryItem.company_id == company_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    if not item.is_active:
        raise HTTPException(
            status_code=409,
            detail=f"Inventory item {item.id} is deactivated; ownership is changed only on live stock.",
        )
    on_hand = float(item.quantity_on_hand or 0.0)
    if on_hand <= LEDGER_QUANTITY_EPSILON:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Inventory item {item.id} has no stock on hand ({on_hand:g}); ownership is recorded "
                "only for material that is physically there."
            ),
        )

    new_owner = resolve_owner(db, company_id, ownership, owner_customer_id, owner_vendor_id)
    old_key = ownership_key(item)
    old_name = owner_name_for(item, owner_names(db, company_id, [item]))
    if old_key == new_owner.key:
        raise HTTPException(
            status_code=409,
            detail=f"Inventory item {item.id} is already {ownership_label(old_key, old_name)}; nothing to change.",
        )

    item.ownership = new_owner.ownership
    item.owner_customer_id = new_owner.owner_customer_id
    item.owner_vendor_id = new_owner.owner_vendor_id
    db.flush()

    lot = f" lot {item.lot_number}" if item.lot_number else ""
    audit.log_required(
        AuditService.ACTIONS["UPDATE"],
        "inventory",
        resource_id=item.id,
        resource_identifier=f"inventory_item {item.id} @ {item.location}",
        description=(
            f"Changed ownership of inventory item {item.id} (part {item.part_id}{lot} at {item.location}, "
            f"{on_hand:g} on hand) from {ownership_label(old_key, old_name)} to "
            f"{ownership_label(new_owner.key, new_owner.owner_name)}. Reason: {reason}"
        ),
        old_values=_ownership_values(old_key, old_name),
        new_values=_ownership_values(new_owner.key, new_owner.owner_name),
        extra_data={
            "change": "ownership",
            "reason": reason,
            "part_id": item.part_id,
            "lot_number": item.lot_number,
            "location": item.location,
            "quantity_on_hand": on_hand,
        },
    )
    return item, new_owner.owner_name
