"""Versioned, JSON-editable fabrication cost inputs. All money uses plan currency.

Decimal strings are preferred on the wire. Null means unknown; explicit zero is
allowed where meaningful. No rates or shop performance parameters are seeded.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _decimal_input(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a decimal")
    if isinstance(value, str) and len(value) > 64:
        raise ValueError("Decimal input is too long")
    return value


Amount = Annotated[
    Decimal,
    BeforeValidator(_decimal_input),
    Field(
        ge=0,
        le=Decimal("1000000000000"),
        max_digits=24,
        decimal_places=9,
        allow_inf_nan=False,
    ),
]
Positive = Annotated[
    Decimal,
    BeforeValidator(_decimal_input),
    Field(
        gt=0,
        le=Decimal("1000000000000"),
        max_digits=24,
        decimal_places=9,
        allow_inf_nan=False,
    ),
]
Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]
Currency = Literal[
    "USD",
    "CAD",
    "EUR",
    "GBP",
    "MXN",
    "JPY",
    "CNY",
    "CHF",
    "AUD",
    "NZD",
    "SEK",
    "NOK",
    "DKK",
    "INR",
    "KRW",
    "SGD",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Evidence(StrictModel):
    reviewed: bool = False
    source: str | None = Field(default=None, min_length=1, max_length=2000)
    status: Literal["assumption", "measured", "validated"] = "assumption"
    note: str | None = Field(default=None, max_length=4000)


class PartDefinition(StrictModel):
    id: Identifier
    name: str = Field(default="", max_length=300)
    make_or_buy: Literal["make", "buy"] = "make"
    # Explicit confirmation that materials and all required work are represented.
    costing_complete: bool = False
    purchase_unit_cost: Amount | None = None
    evidence: Evidence = Field(default_factory=Evidence)


class RootDemand(StrictModel):
    part_id: Identifier
    quantity: Positive


class BomEdge(StrictModel):
    id: Identifier
    parent_id: Identifier
    child_id: Identifier
    quantity: Positive


class MaterialLine(StrictModel):
    id: Identifier
    part_id: Identifier
    description: str = Field(default="", max_length=500)
    quantity_basis: Literal["per_unit", "per_batch"] = "per_unit"
    batch_size: Positive = Decimal("1")
    consumed_quantity: Amount | None = None
    unit: Literal["kg", "lb", "mm2", "m2", "ft2", "sheet", "each"] = "kg"
    unit_cost: Amount | None = None
    evidence: Evidence = Field(default_factory=Evidence)


class ManualRecipe(StrictModel):
    kind: Literal["manual"] = "manual"
    labor_seconds: Amount | None = None
    machine_seconds: Amount | None = None


class LaserCutClass(StrictModel):
    cut_length_mm: Amount
    speed_mm_per_second: Positive | None = None
    pierces: int = Field(default=0, ge=0, le=100000000)
    pierce_seconds: Amount | None = None


class LaserRecipe(StrictModel):
    kind: Literal["laser"] = "laser"
    cuts: list[LaserCutClass] = Field(default_factory=list, max_length=1000)
    noncut_machine_seconds: Amount | None = None
    labor_seconds: Amount | None = None
    # Speeds must explicitly include corner/acceleration behavior or this must
    # have a reviewed allowance. Recipe evidence records the chosen basis.
    speed_includes_dynamics: bool = False
    dynamics_allowance_seconds: Amount | None = None


class BrakeRecipe(StrictModel):
    kind: Literal["brake"] = "brake"
    hits: int = Field(ge=0, le=1000000)
    seconds_per_hit: Amount | None = None
    handling_seconds: Amount | None = Field(
        default=None, description="Elapsed crew-attended time; multiplied by crew_size."
    )
    inspection_seconds: Amount | None = Field(
        default=None, description="Elapsed crew-attended time; multiplied by crew_size."
    )
    crew_size: int = Field(default=1, ge=1, le=100)
    machine_seconds: Amount | None = None
    feasibility_reviewed: bool = False


class WeldRecipe(StrictModel):
    kind: Literal["weld"] = "weld"
    process: Literal["MIG", "TIG", "fiber_laser"]
    weld_length_mm: Amount | None = None
    weld_size_mm: Positive | None = None
    travel_speed_mm_per_second: Positive | None = None
    nonweld_labor_seconds: Amount | None = Field(
        default=None,
        description="Elapsed nonwelding attended time per crew member; crew_size applies to arc and nonwelding time.",
    )
    nonweld_machine_seconds: Amount | None = None
    crew_size: int = Field(default=1, ge=1, le=100)
    procedure_reference: str | None = Field(default=None, max_length=500)


Recipe = Annotated[ManualRecipe | LaserRecipe | BrakeRecipe | WeldRecipe, Field(discriminator="kind")]


class OperationLine(StrictModel):
    id: Identifier
    part_id: Identifier
    name: str = Field(default="", max_length=300)
    process: str = Field(default="manual", min_length=1, max_length=100)
    setup_basis: Literal["per_quote", "per_batch"] = "per_quote"
    run_basis: Literal["per_unit", "per_batch"] = "per_unit"
    batch_size: Positive = Decimal("1")
    setup_labor_seconds: Amount | None = None
    setup_machine_seconds: Amount | None = None
    labor_rate_per_hour: Amount | None = None
    machine_rate_per_hour: Amount | None = None
    consumables_cost_per_run: Amount | None = None
    outside_cost_per_run: Amount | None = None
    recipe: Recipe = Field(default_factory=ManualRecipe)
    evidence: Evidence = Field(default_factory=Evidence)


class PriceBreak(StrictModel):
    minimum_quantity: Amount
    price: Amount


class SupplierOffer(StrictModel):
    id: Identifier
    manufacturer: Identifier
    mpn: Identifier
    supplier: Identifier
    currency: Currency = "USD"
    # Price is for price_unit_quantity EACH, never implicitly for a pack.
    price_unit_quantity: Positive = Decimal("1")
    price_breaks: list[PriceBreak] = Field(default_factory=list, max_length=100)
    pack_quantity: int = Field(default=1, ge=1, le=100000000)
    minimum_order_quantity: int = Field(default=1, ge=1, le=100000000)
    order_multiple: int = Field(default=1, ge=1, le=100000000)
    quoted_on: date | None = None
    valid_until: date | None = None
    max_age_days: int = Field(default=30, ge=0, le=3650)
    applicable: bool = False
    freight: Amount | None = None
    evidence: Evidence = Field(default_factory=Evidence)


class HardwareLine(StrictModel):
    id: Identifier
    part_id: Identifier
    manufacturer: Identifier
    mpn: Identifier
    quantity_per_part: Positive
    stock_available: int = Field(default=0, ge=0, le=100000000)
    stock_unit_value: Amount | None = None
    offer: SupplierOffer | None = None
    evidence: Evidence = Field(default_factory=Evidence)


class Assumption(StrictModel):
    id: Identifier
    description: str = Field(min_length=1, max_length=4000)
    reviewed: bool = False
    source: str | None = Field(default=None, max_length=2000)


class SourceReview(StrictModel):
    file_id: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["reviewed", "excluded"]
    note: str = Field(min_length=1, max_length=4000)


class QuotePlan(StrictModel):
    schema_version: Literal["1"] = "1"
    currency: Currency = "USD"
    parts: list[PartDefinition] = Field(default_factory=list, max_length=2000)
    roots: list[RootDemand] = Field(default_factory=list, max_length=2000)
    bom: list[BomEdge] = Field(default_factory=list, max_length=10000)
    materials: list[MaterialLine] = Field(default_factory=list, max_length=10000)
    operations: list[OperationLine] = Field(default_factory=list, max_length=10000)
    hardware: list[HardwareLine] = Field(default_factory=list, max_length=10000)
    assumptions: list[Assumption] = Field(default_factory=list, max_length=1000)
    source_reviews: list[SourceReview] = Field(default_factory=list, max_length=1000)
    target_margin: (
        Annotated[
            Decimal,
            BeforeValidator(_decimal_input),
            Field(
                ge=0,
                le=Decimal("0.95"),
                max_digits=10,
                decimal_places=9,
                allow_inf_nan=False,
            ),
        ]
        | None
    ) = None
