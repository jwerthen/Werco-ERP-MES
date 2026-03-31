from decimal import Decimal
from typing import Annotated
from pydantic import Field


# ============================================================================
# ANNOTATED TYPES (Reusable validators)
# ============================================================================

PartNumber = Annotated[str, Field(
    min_length=3,
    max_length=50,
    pattern=r'^[A-Za-z0-9\-_\.]+$',
    description="Part number: 3-50 chars, alphanumeric + dashes/underscores/dots"
)]

Revision = Annotated[str, Field(
    min_length=1,
    max_length=5,
    pattern=r'^[A-Za-z0-9]+$',
    description="Revision: 1-5 chars, alphanumeric"
)]

DescriptionShort = Annotated[str, Field(
    min_length=5,
    max_length=500,
    description="Short description: 5-500 characters"
)]

DescriptionLong = Annotated[str, Field(
    min_length=20,
    max_length=5000,
    description="Long description: 20-5000 characters"
)]

Money = Annotated[Decimal, Field(
    ge=0,
    description="Currency: non-negative decimal"
)]

OptionalMoney = Annotated[Decimal, Field(
    ge=0,
    default=None,
    description="Optional currency: non-negative decimal"
)]

MoneySmall = Annotated[Decimal, Field(
    ge=0,
    description="Small currency: non-negative decimal"
)]

Percentage = Annotated[Decimal, Field(
    ge=0,
    le=100,
    description="Percentage: 0-100"
)]

NonNegativeInteger = Annotated[int, Field(
    ge=0,
    description="Non-negative integer"
)]

PositiveInteger = Annotated[int, Field(
    gt=0,
    description="Positive integer"
)]

SafeString = Annotated[str, Field(
    pattern=r'^[^<>{}]*$',
    description="String without HTML/script injection"
)]

Phone = Annotated[str, Field(
    pattern=r'^\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}$',
    description="Phone number in various formats"
)]

Email = Annotated[str, Field(
    description="Email address"
)]

UUID = Annotated[str, Field(
    pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    description="UUID v4 format"
)]
