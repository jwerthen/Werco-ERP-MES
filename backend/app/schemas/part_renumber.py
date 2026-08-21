"""Request/response contracts for renumbering a part in place.

The two part-number fields on ``PartRenumberRequest`` carry DIFFERENT types, and
that is the most important decision in this file. See the class docstring.
"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.validation import PartNumber
from app.schemas.base import UTCModel


class RenumberDiagnosticSchema(UTCModel):
    """One reason a renumber is refused, or one consequence worth disclosing."""

    code: str
    detail: str


class SheetSpecDeltaSchema(UTCModel):
    """What the laser-nest matcher reads out of the old number versus the new one.

    For sheet and plate the part number IS the material spec: thickness, size and
    alloy are parsed out of the string, because ``Part`` carries no such columns.
    Renumbering such a part therefore changes what physical material the matcher
    believes it is -- or stops it recognizing it at all, in which case the sheet
    silently stops being suggested for nests with no error anywhere.

    Surfaced so the confirm screen can show, in the planner's own terms, what the
    system reads today and what it will read afterwards. Disclosed, never refused
    (see ``SheetSpecDelta`` in the service for why refusing would be both wrong and
    unenforceable).
    """

    is_sheet_like_before: bool = False
    is_sheet_like_after: bool = False
    thickness_before: Optional[str] = None
    thickness_after: Optional[str] = None
    sheet_size_before: Optional[str] = None
    sheet_size_after: Optional[str] = None
    alloy_before: Optional[str] = None
    alloy_after: Optional[str] = None


class PartRenumberImpactResponse(UTCModel):
    """What a renumber would do, computed by a pure read.

    ``eligible`` is simply ``blockers == []`` and is **not a durable verdict** --
    every input it reads is mutable by other people afterwards, so the write
    re-runs every probe server-side. A client must never treat a stale
    ``eligible: true`` as authorization. (Same contract as
    ``PartBackflushReadinessResponse``, deliberately.)
    """

    part_id: int
    current_part_number: str
    normalized_new_part_number: Optional[str] = None
    eligible: bool = False
    blockers: List[RenumberDiagnosticSchema] = []
    advisories: List[RenumberDiagnosticSchema] = []

    # Open work orders that could carry this number baked into an operation name --
    # including ones where this part is a COMPONENT of somebody else's assembly.
    open_work_order_count: int = 0
    # TWO counts, not one, and the distinction keeps the screen honest: the baked
    # prefix is consulted only when an operation has no component link, so the raw
    # count includes rows that are already fine. Showing only the raw number would
    # put a large, alarming figure in front of the operator for work needing nothing.
    operations_with_stale_prefix: int = 0
    operations_needing_repair: int = 0

    existing_aliases: List[str] = []
    sheet: SheetSpecDeltaSchema = SheetSpecDeltaSchema()


class PartRenumberRequest(BaseModel):
    """Renumber a part in place.

    THE TWO PART-NUMBER FIELDS ARE TYPED DIFFERENTLY ON PURPOSE.

    ``expected_part_number`` is a plain ``str``. It MUST be, because ``PartNumber``
    rejects ``/``, ``"`` and spaces -- so typing it that way would make this verb
    unable to express the old number of ``1/4" PLATE 48 X 96``, which is exactly
    the flagship case (legacy sheet-stock numbers are the ones most likely to need
    renumbering). A verb that cannot name what it is replacing is useless.

    ``new_part_number`` keeps the strict ``PartNumber``, making the verb a one-way
    ratchet onto the canonical grammar: you can renumber OFF an off-pattern legacy
    string but not ONTO one. That asymmetry is the accepted cost, and it is the
    right direction -- it is also the same split ``PartResponse`` and ``PartCreate``
    already make between describing data that exists and deciding what may be added.

    ``expected_part_number`` is the compare-and-swap precondition, not decoration:
    ``Part`` maps no optimistic-lock version column, so the old number string is the
    ONLY concurrency control available. If it no longer matches, someone renumbered
    this part while the dialog was open and the request is refused 409 rather than
    retiring a number the operator never saw.
    """

    new_part_number: PartNumber
    expected_part_number: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="The part's current number, as the client last read it. Compare-and-swap precondition.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Why this part is being renumbered. Recorded on the audit row and the alias.",
    )

    @field_validator("new_part_number", mode="before")
    @classmethod
    def _uppercase_new_number(cls, value):
        """Mirrors ``PartBase.uppercase_part_number``.

        Without it the verb could mint a lowercase number no create path could
        produce -- a case-variant that the case-SENSITIVE unique constraint would
        happily accept alongside its own twin.
        """
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        """``min_length=1`` alone passes ``"   "``. Copied from ``ReceiptVoidRequest``."""
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("reason cannot be blank")
        return cleaned


class PartRenumberResponse(UTCModel):
    """The result of a renumber.

    ``work_orders_repaired`` is the count of open work orders whose operations were
    re-linked to this part by FK *before* the swap, while the old number still
    matched. That drain is what stops the number change silently breaking the
    operator's quantity target on assembly jobs.

    ``operations_with_stale_prefix`` is what remains carrying the old number as
    TEXT. Those are deliberately not rewritten -- an operation name on a released
    work order is part of the released quality plan.
    """

    part_id: int
    part_number: str
    previous_part_number: str
    alias_id: Optional[int] = None
    alias_created: bool = False
    # True when the part was renamed back INTO a number it previously carried: the
    # now-redundant alias row is reclaimed rather than left pointing at a live number.
    alias_reclaimed: bool = False
    no_op: bool = False
    work_orders_repaired: int = 0
    operations_with_stale_prefix: int = 0
    sheet_spec_changed: bool = False
