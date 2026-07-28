from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.validation import (
    Money,
    MoneySmall,
    NonNegativeInteger,
    PartNumber,
    Revision,
)
from app.models.part import PartType, UnitOfMeasure
from app.schemas.base import UTCModel


class PartBase(UTCModel):
    part_number: PartNumber
    revision: Revision = "A"
    name: str = Field(min_length=2, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    part_type: PartType = Field(..., description="Type of part")
    unit_of_measure: UnitOfMeasure = UnitOfMeasure.EACH

    # Costing (all optional and non-negative)
    standard_cost: Money = Field(default=Decimal("0.0"))
    material_cost: Money = Field(default=Decimal("0.0"))
    labor_cost: Money = Field(default=Decimal("0.0"))
    overhead_cost: Money = Field(default=Decimal("0.0"))

    # Lead time
    lead_time_days: NonNegativeInteger = Field(default=0)

    # Inventory (all optional and non-negative)
    safety_stock: MoneySmall = Field(default=Decimal("0.0"))
    reorder_point: MoneySmall = Field(default=Decimal("0.0"))
    reorder_quantity: MoneySmall = Field(default=Decimal("0.0"))

    # Classification
    is_critical: bool = False
    requires_inspection: bool = True
    inspection_requirements: Optional[str] = Field(None, max_length=2000)

    # Customer info
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_part_number: Optional[str] = Field(None, max_length=100)
    drawing_number: Optional[str] = Field(None, max_length=100)

    @field_validator('part_number', mode='before')
    @classmethod
    def uppercase_part_number(cls, v: str) -> str:
        """Ensure part number is uppercase"""
        return v.upper().strip() if isinstance(v, str) else str(v)

    @field_validator('revision', mode='before')
    @classmethod
    def uppercase_revision(cls, v: str) -> str:
        """Ensure revision is uppercase"""
        return v.upper().strip() if isinstance(v, str) else str(v)

    @model_validator(mode='after')
    def validate_consistency(self) -> 'PartBase':
        """Ensure data consistency"""
        # Reorder quantity should be set if reorder point is set
        if self.reorder_point > 0 and self.reorder_quantity == 0:
            raise ValueError('Reorder quantity must be greater than 0 when reorder point is set')

        return self

    @field_validator('part_type', mode='before')
    @classmethod
    def normalize_part_type(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator('unit_of_measure', mode='before')
    @classmethod
    def normalize_unit_of_measure(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v


class PartCreate(PartBase):
    pass


class PartUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    # **DELIBERATELY HERE AND ON ``PartResponse`` ONLY -- never on ``PartBase``.**
    # ``PartCreate`` is a bare subclass of ``PartBase`` and BOTH create endpoints splat
    # ``Part(**data)`` (``parts.py`` / ``materials.py``), as do both CSV importers. A
    # field on ``PartBase`` would therefore become settable on four write paths at once,
    # with no gate and no readiness check -- turning a shop-wide "consume this part's BOM
    # automatically, forever" policy into something a spreadsheet column can switch on.
    #
    # Turning it ON through ``PUT /parts/{id}`` or ``PUT /materials/{id}`` runs the shared
    # refusal gate (``parts.assert_backflush_change_allowed``, 409 on any blocking
    # readiness diagnostic). Turning it OFF is always allowed.
    backflush_components: Optional[bool] = Field(
        None,
        description=(
            "Opt this part into automatic BOM/routing component backflush at work-order "
            "completion. Enabling is refused (409) while the part's backflush readiness "
            "check reports blockers — see GET /parts/{part_id}/backflush-readiness."
        ),
    )
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    revision: Optional[str] = Field(None, min_length=1, max_length=20, pattern=r'^[A-Z0-9]+$')
    description: Optional[str] = Field(None, max_length=2000)
    part_type: Optional[PartType] = Field(
        None, description="Type of part (manufactured, purchased, assembly, raw_material)"
    )
    unit_of_measure: Optional[UnitOfMeasure] = None
    standard_cost: Optional[Decimal] = Field(None, ge=0)
    material_cost: Optional[Decimal] = Field(None, ge=0)
    labor_cost: Optional[Decimal] = Field(None, ge=0)
    overhead_cost: Optional[Decimal] = Field(None, ge=0)
    lead_time_days: Optional[int] = Field(None, ge=0, le=365)
    safety_stock: Optional[Decimal] = Field(None, ge=0)
    reorder_point: Optional[Decimal] = Field(None, ge=0)
    reorder_quantity: Optional[Decimal] = Field(None, ge=0)
    is_critical: Optional[bool] = None
    requires_inspection: Optional[bool] = None
    inspection_requirements: Optional[str] = Field(None, max_length=2000)
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_part_number: Optional[str] = Field(None, max_length=100)
    drawing_number: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = None
    status: Optional[str] = Field(None, max_length=50)

    @field_validator('revision', mode='before')
    @classmethod
    def uppercase_revision(cls, v: Optional[str]) -> Optional[str]:
        """Ensure revision is uppercase"""
        return v.upper().strip() if v else v

    @field_validator('backflush_components')
    @classmethod
    def backflush_components_not_null(cls, v: Optional[bool]) -> Optional[bool]:
        """Reject an EXPLICIT ``null`` — 422 rather than a 500 on a NOT NULL column.

        ``None`` is this schema's "field omitted" sentinel and every other optional field
        treats it that way, but ``parts.backflush_components`` is ``nullable=False``. The
        generic ``setattr`` loop in both update handlers writes whatever survives
        ``exclude_unset=True``, so a client sending an explicit ``null`` would drive an
        ``IntegrityError`` on Postgres (and, worse, silently store ``NULL`` on SQLite).

        Pydantic v2 does not run field validators over DEFAULTS, so this fires only when
        the client actually supplied the key — which is exactly the distinction that makes
        the check possible at all.
        """
        if v is None:
            raise ValueError('backflush_components must be true or false, not null')
        return v


class PartResponse(PartBase):
    id: int
    version: Optional[int] = 0  # Optional for backwards compatibility
    # Read-only here in the sense that ``PartBase`` (and therefore ``PartCreate``) does
    # NOT carry it: a part is always created with the flag off and can only be switched
    # on through the gated update path. Defaulted so ``_part_to_response``'s
    # ``except Exception: return None`` can never make a part VANISH from a list because
    # of it -- a required field with no default would do exactly that.
    backflush_components: bool = False
    is_active: bool
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True
