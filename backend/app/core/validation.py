from decimal import Decimal
from typing import Annotated, Optional

from pydantic import AfterValidator, Field

# ============================================================================
# ANNOTATED TYPES (Reusable validators)
# ============================================================================

PartNumber = Annotated[
    str,
    Field(
        min_length=3,
        max_length=50,
        pattern=r'^[A-Za-z0-9\-_\.#]+$',
        description="Part number: 3-50 chars, alphanumeric + dashes/underscores/dots/#",
    ),
]

Revision = Annotated[
    str, Field(min_length=1, max_length=5, pattern=r'^[A-Za-z0-9]+$', description="Revision: 1-5 chars, alphanumeric")
]

DescriptionShort = Annotated[
    str, Field(min_length=5, max_length=500, description="Short description: 5-500 characters")
]

DescriptionLong = Annotated[
    str, Field(min_length=20, max_length=5000, description="Long description: 20-5000 characters")
]

# Operation IDENTIFIER (WorkOrderOperation.operation_number / RoutingOperation.operation_number).
# The 20 is the COLUMN WIDTH -- both are String(20) -- not a style preference. Declared once here
# because it was previously declared three times and disagreed each time (50 on the work-order
# schema, unbounded on both routing schemas), so a 21-50 char value validated at the API boundary
# and then raised StringDataRightTruncation on Postgres: a 500 for what is a caller input error.
# Widening this REQUIRES widening both columns in the same migration.
OperationNumber = Annotated[
    str, Field(max_length=20, description="Operation identifier: max 20 chars (the String(20) column width)")
]


def _unit_number_or_none(value: str) -> Optional[str]:
    """Trim a Unit #, and turn a blank one into ``None`` rather than storing ``""``.

    ``PUT /work-orders/{id}`` applies its body through a blind ``setattr`` loop, so
    without this a planner clearing the field in the UI (which sends ``""``, not
    ``null``) persisted an EMPTY STRING where every reader expects NULL. That row then
    reads as "has a unit number" to anything doing a presence test, and as "" to
    anything rendering it -- which is why the wallboard already carries a defensive
    ``wo.unit_number or None`` and search a ``.strip()``. Normalising at the write
    door is the fix those two are working around.

    Blank must collapse to NULL rather than be REFUSED, because clearing the field is a
    legitimate correction: a unit typed onto the wrong work order is already on the
    kiosk and the TV wall, so it has to be removable, not merely overwritable.
    """
    trimmed = value.strip()
    return trimmed or None


# Build identity of a one-unit-per-work-order job (``work_orders.unit_number``, migration
# 083). The 50 is the COLUMN WIDTH -- ``String(50)`` -- not a style preference, and it is
# declared here once because it was previously restated at every schema that carries the
# field; a third restatement (the template BATCH list, one unit number per created draft)
# is what made consolidating it worth doing. Widening this REQUIRES widening the column in
# the same migration.
#
# ``AfterValidator`` rather than ``BeforeValidator`` is load-bearing at every use site,
# all of which are ``Optional[UnitNumber]``: ``Optional[X]`` compiles to a NULLABLE schema
# that checks for ``None`` BEFORE delegating to the inner str schema, so a before-validator
# returning ``None`` would then be re-validated as a string and fail. After-validator
# output is not re-validated, which is what lets blank collapse to NULL. The consequence to
# know: ``max_length`` is checked against the UNTRIMMED value, so a 51-character string
# that would trim to 50 is refused rather than silently stored.
UnitNumber = Annotated[
    str,
    Field(max_length=50, description="Unit #: max 50 chars (the String(50) column width); blank stores as NULL"),
    AfterValidator(_unit_number_or_none),
]

Money = Annotated[Decimal, Field(ge=0, description="Currency: non-negative decimal")]

OptionalMoney = Annotated[Decimal, Field(ge=0, default=None, description="Optional currency: non-negative decimal")]

MoneySmall = Annotated[Decimal, Field(ge=0, description="Small currency: non-negative decimal")]

Percentage = Annotated[Decimal, Field(ge=0, le=100, description="Percentage: 0-100")]

NonNegativeInteger = Annotated[int, Field(ge=0, description="Non-negative integer")]

PositiveInteger = Annotated[int, Field(gt=0, description="Positive integer")]

SafeString = Annotated[str, Field(pattern=r'^[^<>{}]*$', description="String without HTML/script injection")]

Phone = Annotated[
    str,
    Field(pattern=r'^\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}$', description="Phone number in various formats"),
]

Email = Annotated[str, Field(description="Email address")]

UUID = Annotated[
    str, Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', description="UUID v4 format")
]
