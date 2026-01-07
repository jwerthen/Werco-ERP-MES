from decimal import Decimal
from typing import Annotated
import re
from pydantic import Field, field_validator, model_validator, BaseModel
from enum import Enum


# ============================================================================
# ENUMS
# ============================================================================

class MakeBuy(str, Enum):
    MAKE = "MAKE"
    BUY = "BUY"


# ============================================================================
# ANNOTATED TYPES (Reusable validators)
# ============================================================================

PartNumber = Annotated[str, Field(
    min_length=3,
    max_length=50,
    pattern=r'^[A-Z0-9\-]+$',
    description="Part number: 3-50 chars, alphanumeric + dashes, uppercase"
)]

Revision = Annotated[str, Field(
    min_length=1,
    max_length=5,
    pattern=r'^[A-Z0-9]+$',
    description="Revision: 1-5 chars, uppercase alphanumeric"
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
    max_digits=8,
    decimal_places=2,
    description="Currency: 0-999999.99"
)]

OptionalMoney = Annotated[Decimal, Field(
    ge=0,
    max_digits=8,
    decimal_places=2,
    default=None,
    description="Optional currency: 0-999999.99"
)]

MoneySmall = Annotated[Decimal, Field(
    ge=0,
    max_digits=6,
    decimal_places=4,
    description="Small currency: 0-9999.9999"
)]

Percentage = Annotated[Decimal, Field(
    ge=0,
    le=100,
    decimal_places=2,
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


# ============================================================================
# BASE VALIDATORS
# ============================================================================

class UppercaseValidator:
    @field_validator('*', mode='before')
    @classmethod
    def uppercase_strings(cls, v, info):
        """Uppercase specific string fields"""
        field_name = info.field_name
        if field_name in ['part_number', 'revision', 'part_type']:
            if isinstance(v, str):
                return v.upper().strip()
        return v


class UUIDValidator:
    @field_validator('*_id', mode='before')
    @classmethod
    def validate_uuid(cls, v, info):
        """Validate UUID format for any *_id field"""
        if isinstance(v, str):
            pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
            if not pattern.match(str(v)):
                raise ValueError('Invalid ID format')
        return v


# ============================================================================
# VALIDATION ERROR RESPONSE
# ============================================================================

class ValidationErrorDetail(BaseModel):
    field: str
    message: str
    type: str


class ValidationErrorResponse(BaseModel):
    error: str = "VALIDATION_ERROR"
    message: str = "Input validation failed"
    details: list[ValidationErrorDetail]


def format_validation_error(exc: Exception, field: str, message: str, error_type: str) -> ValidationErrorDetail:
    """Helper to create validation error detail"""
    return ValidationErrorDetail(field=field, message=message, type=error_type)
