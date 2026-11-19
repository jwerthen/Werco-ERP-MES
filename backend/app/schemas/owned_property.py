"""Request contracts for customer- and supplier-owned property (AS9100D 8.5.3).

A stock row (``inventory_items``) or a tool (``tools``) is owned by the company (the
default, and what every row created before migration 129 is), by a customer, or by a
supplier. The kind and the owner reference travel together, and this module is the one
place their pairing is checked at the request boundary, so every creator answers a
mismatch with the same 422 text:

* ``company``  -- no owner reference at all;
* ``customer`` -- ``owner_customer_id`` and no ``owner_vendor_id``;
* ``supplier`` -- ``owner_vendor_id`` and no ``owner_customer_id``.

That is the request-side twin of the ``ck_<table>_owner_matches`` CHECK
(``app.models.inventory.OWNER_MATCHES_CHECK``). Whether the referenced customer or
vendor exists in the caller's company is a database question and is answered by
``app.services.owned_property_service.resolve_owner`` (404), before any write.
"""

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.inventory import OwnershipType

OWNERSHIP_DESCRIPTION = (
    "Who owns this material: 'company' (the default -- the shop's own stock), 'customer' "
    "(customer-supplied property; name the customer in owner_customer_id) or 'supplier' "
    "(supplier-owned property such as consignment stock or tooling; name the vendor in "
    "owner_vendor_id). Customer- and supplier-owned stock is never drawn by automatic "
    "(unpinned) consumption and is left out of inventory valuation."
)
OWNER_CUSTOMER_DESCRIPTION = "The owning customer's id. Required when ownership is 'customer', otherwise omit it."
OWNER_VENDOR_DESCRIPTION = "The owning vendor's id. Required when ownership is 'supplier', otherwise omit it."


def ownership_value(ownership: object) -> str:
    """``ownership`` as its stored string: an ``OwnershipType`` member or its value."""
    return str(getattr(ownership, "value", ownership))


def ownership_problem(
    ownership: object, owner_customer_id: Optional[int], owner_vendor_id: Optional[int]
) -> Optional[str]:
    """Why this kind/owner pair cannot be stored (the 422 text), or ``None`` when it can."""
    kind = ownership_value(ownership)
    if kind == OwnershipType.COMPANY.value:
        if owner_customer_id is not None or owner_vendor_id is not None:
            return "Company-owned material takes no owner_customer_id or owner_vendor_id; omit both."
        return None
    if kind == OwnershipType.CUSTOMER.value:
        if owner_customer_id is None:
            return "Customer-owned material needs owner_customer_id naming the customer."
        if owner_vendor_id is not None:
            return "Customer-owned material takes owner_customer_id only; omit owner_vendor_id."
        return None
    if kind == OwnershipType.SUPPLIER.value:
        if owner_vendor_id is None:
            return "Supplier-owned material needs owner_vendor_id naming the vendor."
        if owner_customer_id is not None:
            return "Supplier-owned material takes owner_vendor_id only; omit owner_customer_id."
        return None
    return "ownership must be one of: company, customer, supplier."


def _normalize_ownership(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


class OwnershipFields(BaseModel):
    """The three ownership fields with company defaults, for a request that CREATES a row.

    Omitting all three is company stock, byte-for-byte what the request meant before
    migration 129. A kind/owner mismatch is a 422 from the validator below.
    """

    ownership: OwnershipType = Field(OwnershipType.COMPANY, description=OWNERSHIP_DESCRIPTION)
    owner_customer_id: Optional[int] = Field(None, gt=0, description=OWNER_CUSTOMER_DESCRIPTION)
    owner_vendor_id: Optional[int] = Field(None, gt=0, description=OWNER_VENDOR_DESCRIPTION)

    @field_validator("ownership", mode="before")
    @classmethod
    def _normalize_kind(cls, value: object) -> object:
        return _normalize_ownership(value)

    @model_validator(mode="after")
    def _owner_matches_kind(self) -> "OwnershipFields":
        problem = ownership_problem(self.ownership, self.owner_customer_id, self.owner_vendor_id)
        if problem:
            raise ValueError(problem)
        return self


class InventoryOwnershipChangeRequest(OwnershipFields):
    """``POST /inventory/items/{item_id}/ownership``: the new owner and why.

    All three ownership fields state the row's NEW ownership in full (an omitted owner id
    is ``None``), so changing customer-owned stock back to the company is
    ``{"ownership": "company", "reason": ...}``.
    """

    ownership: OwnershipType = Field(..., description=OWNERSHIP_DESCRIPTION)  # type: ignore[assignment]
    reason: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Why the ownership is changing (required; recorded on the audit trail).",
    )

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("A reason is required")
        return stripped
