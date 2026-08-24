"""Request/response contracts for taking a part out of use, and putting it back.

WHY THESE ARE DEDICATED VERBS AND NOT FIELDS ON ``PartUpdate``
--------------------------------------------------------------
``parts.is_active`` doubles as the SOFT-DELETE MASK. ``delete_part`` sets
``is_deleted`` AND ``is_active=False`` AND ``status="obsolete"`` together, which
is exactly the shape invariant 3 records after the 2026-08-16 ``Vendor`` sweep:
the flag a delete happens to clear is a *mask*, not a filter, and anything that
can set it back to ``True`` on a tombstoned row is clearing a delete mask.

``PUT /parts/{id}`` and ``PUT /materials/{id}`` are blind ``setattr`` loops over
this schema's sibling ``PartUpdate``, and neither filters ``is_deleted`` on its
lookup (a deliberate, documented decision on that pair). So adding ``is_active``
or ``status`` to ``PartUpdate`` would hand every Supervisor a way to un-mask a
deleted part through a form save. These two verbs exist instead: ADMIN/MANAGER
only, reason captured, audited, and both refuse a soft-deleted part outright.
"""

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.base import UTCModel


class PartDeactivateRequest(BaseModel):
    """Take a part out of use without deleting it.

    ``reason`` is REQUIRED. Deactivation removes the part from every picker,
    search and purchasing signal in the app, so "why" is the only thing that makes
    the change reviewable later -- the same rule receiving void, NCR void, vendor
    delete and part renumber all follow.

    ``acknowledge_remaining_stock`` exists because deactivating a part that still
    has stock on the shelf is legitimate (a superseded SKU being wound down) but
    is never what someone means by accident: without it the verb refuses **409**
    and names the quantity.
    """

    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Why this part is being taken out of use. Recorded on the audit row.",
    )
    acknowledge_remaining_stock: bool = Field(
        default=False,
        description="Confirm deactivating a part that still has stock on hand.",
    )

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        """``min_length=1`` alone passes ``"   "``. Copied from ``ReceiptVoidRequest``."""
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("reason cannot be blank")
        return cleaned


class PartActivateRequest(BaseModel):
    """Put a part back into use.

    ``reason`` is OPTIONAL here, deliberately asymmetric with deactivate.
    Restoring something to use is the permissive direction and is fully visible
    the moment it happens (the part reappears in every list); taking it out of use
    is the one that has to be explainable months later.
    """

    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional note recorded on the audit row.",
    )

    @field_validator("reason")
    @classmethod
    def _blank_reason_is_none(cls, value: Optional[str]) -> Optional[str]:
        cleaned = (value or "").strip()
        return cleaned or None


class PartActivationResponse(UTCModel):
    """The part's activation state after the verb ran.

    ``no_op`` is ``True`` when the part was already in the requested state and
    nothing was written -- not an error. A request that changes nothing must not
    fail (the same rule ``assert_backflush_change_allowed`` and ``renumber_part``
    follow), because a double-click and a retry are both ordinary.
    """

    part_id: int
    part_number: str
    is_active: bool
    status: str
    no_op: bool = False
