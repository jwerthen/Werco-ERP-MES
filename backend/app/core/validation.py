from decimal import Decimal
from typing import Annotated

from pydantic import Field

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
