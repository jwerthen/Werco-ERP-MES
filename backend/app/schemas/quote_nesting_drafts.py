"""Bounded client estimate storage, deliberately not a geometry approval schema."""

import math
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

MAX_ESTIMATE_BYTES = 5 * 1024 * 1024
MAX_DRAFT_REQUEST_BYTES = MAX_ESTIMATE_BYTES + 16 * 1024
MAX_INCHES = 20000 / 25.4
Name = Annotated[str, Field(min_length=1, max_length=199, pattern=r"\S")]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Text = Annotated[str, Field(max_length=1000)]


def finite_decimal(value: str) -> str:
    if not math.isfinite(float(value)):
        raise ValueError("Decimal values must be finite")
    return value


DecimalText = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^[0-9]+(\.[0-9]+)?$"), AfterValidator(finite_decimal)
]
Axis = Literal["x", "y"]
Basis = Literal["per_lb", "per_cubic_inch", "per_square_foot"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    nullable_optional: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def reject_null_optional_fields(cls, value: Any):
        # Optional client fields are omitted by projectToFile; explicit null is
        # reserved for required, nullable catalog/price fields, as in the UI schema.
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"version", "schema_version"} and type(item) is not int:
                    raise ValueError("Schema versions must be integers")
                if key in {"reviewed", "confirmed"} and type(item) is not bool:
                    raise ValueError("Review fields must be booleans")
                field = cls.model_fields.get(key)
                if item is None and field and not field.is_required() and key not in cls.nullable_optional:
                    raise ValueError("Omit optional fields instead of setting them to null")
        return value


class SavedPoint(InputModel):
    x: float = Field(ge=-MAX_INCHES, le=MAX_INCHES)
    y: float = Field(ge=-MAX_INCHES, le=MAX_INCHES)


class SavedPolygon(InputModel):
    type: Literal["poly"]
    points: list[SavedPoint] = Field(min_length=3, max_length=2000)


class SavedCircle(InputModel):
    type: Literal["circle"]
    cx: float = Field(ge=-MAX_INCHES, le=MAX_INCHES)
    cy: float = Field(ge=-MAX_INCHES, le=MAX_INCHES)
    r: float = Field(gt=0, le=MAX_INCHES)


SavedLoop = Annotated[SavedPolygon | SavedCircle, Field(discriminator="type")]


class SavedProvenance(InputModel):
    version: Literal[1]
    sourceName: str = Field(min_length=1, max_length=1024)
    sourceSha256: Hash
    sourceHashBasis: Literal["original-bytes", "utf8-text"]
    geometrySha256: Hash
    geometryVersion: Literal["werco-geometry-v1"]
    sourceUnits: Literal["in", "mm", "unitless"]
    resolvedUnits: Literal["in", "mm"]
    unitDecision: Literal["declared", "assigned"]
    importerVersion: Literal["werco-dxf-v2"]
    warnings: list[Annotated[str, Field(max_length=2000)]] = Field(max_length=100)

    @model_validator(mode="after")
    def units_agree(self):
        if self.sourceUnits == "unitless":
            if self.unitDecision != "assigned":
                raise ValueError("Unitless source units must be assigned")
        elif self.unitDecision != "declared" or self.sourceUnits != self.resolvedUnits:
            raise ValueError("Declared source units must agree")
        return self


class SavedPart(InputModel):
    id: Identifier
    name: Name
    quantity: int = Field(ge=1, le=300)
    rotate: bool
    color: int = Field(ge=0, le=3)
    loops: list[SavedLoop] = Field(min_length=1, max_length=100)
    referencePaths: Optional[list[Annotated[list[SavedPoint], Field(min_length=2, max_length=2000)]]] = Field(
        default=None, max_length=2000
    )
    geometryTolerance: Optional[float] = Field(default=None, ge=0, le=0.001)
    rotationMode: Optional[Literal["fixed", "half-turn", "quarter-turn"]] = None
    grainAxis: Optional[Axis] = None
    importMode: Optional[Literal["drawing-bounds"]] = None
    revision: Optional[Annotated[str, Field(max_length=100)]] = None
    provenance: Optional[SavedProvenance] = None


class SavedPriceOption(InputModel):
    price_basis: Basis
    price_key: Optional[Text]
    source_field: Text
    unit_price: Optional[DecimalText]


class SavedCatalog(InputModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=80)
    source_updated_at: Optional[Text]
    catalog_hash: Hash
    density_lb_per_cubic_inch: Optional[DecimalText]
    price_options: list[SavedPriceOption] = Field(max_length=200)
    missing_metadata: list[Text] = Field(max_length=100)


class SavedResolvedStock(InputModel):
    id: Identifier
    width_in: DecimalText
    length_in: DecimalText
    area_sq_in: DecimalText
    area_sq_ft: DecimalText
    volume_cu_in: DecimalText
    weight_lb: Optional[DecimalText]
    sheet_cost: Optional[DecimalText]


class SavedResolutionIssue(InputModel):
    code: Text
    field: Text
    message: Text


class SavedResolution(InputModel):
    schema_version: Literal[1]
    company_id: int = Field(gt=0)
    catalog_material: SavedCatalog
    thickness_in: DecimalText
    price_basis: Basis
    price_key: Optional[Text]
    currency: None
    status: Literal["review_required", "unresolved"]
    calculable: bool
    confirmed: Literal[False]
    stocks: list[SavedResolvedStock] = Field(max_length=20)
    issues: list[SavedResolutionIssue] = Field(max_length=200)
    content_hash: Hash


class SavedAcknowledgement(InputModel):
    contentHash: Hash
    currency: Literal["USD"]
    reviewed: Literal[True]


class SavedBinding(InputModel):
    nullable_optional: ClassVar[frozenset[str]] = frozenset({"priceKey"})
    companyId: int = Field(gt=0)
    catalog: SavedCatalog
    priceBasis: Optional[Basis] = None
    # The writer may retain explicit null for this nullable optional selection.
    priceKey: Optional[Text] = None
    resolution: Optional[SavedResolution] = None
    acknowledgement: Optional[SavedAcknowledgement] = None

    @model_validator(mode="after")
    def source_agrees(self):
        result = self.resolution
        if result and (
            result.company_id != self.companyId
            or result.catalog_material.id != self.catalog.id
            or result.catalog_material.catalog_hash != self.catalog.catalog_hash
            or result.price_basis != self.priceBasis
            or result.price_key != self.priceKey
        ):
            raise ValueError("Resolution does not match the selected source")
        if self.acknowledgement and (result is None or self.acknowledgement.contentHash != result.content_hash):
            raise ValueError("Acknowledgment does not match the resolution")
        return self


class SavedStock(InputModel):
    id: Identifier
    width: float = Field(gt=0, le=MAX_INCHES)
    height: float = Field(gt=0, le=MAX_INCHES)
    enabled: bool
    price: Optional[float] = Field(ge=0)


class SavedQuote(InputModel):
    version: Literal[3, 7]
    units: Literal["in"]
    currency: Literal["USD"]
    name: Name
    material: Literal["Carbon steel", "Stainless steel", "Aluminum"]
    thickness: float = Field(gt=0, le=100 / 25.4)
    margin: float = Field(ge=0, le=MAX_INCHES)
    gap: float = Field(ge=0, le=MAX_INCHES)
    objective: Literal["area", "cost"]
    options: list[SavedStock] = Field(min_length=1, max_length=12)
    parts: list[SavedPart] = Field(max_length=300)
    spacingMode: Optional[Literal["auto", "manual"]] = None
    grainAxis: Optional[Axis] = None
    materialBinding: Optional[SavedBinding] = None


class SavedGroup(InputModel):
    id: Identifier
    quote: SavedQuote


class SavedProject(InputModel):
    version: Literal[4, 5, 6]
    units: Literal["in"]
    currency: Literal["USD"]
    name: Name
    activeGroupId: Identifier
    groups: list[SavedGroup] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def bounded_structure(self):
        ids, part_ids, material_keys = set(), set(), set()
        quantity = vertices = designs = 0
        for group in self.groups:
            if group.id in ids:
                raise ValueError("Duplicate material group ID")
            ids.add(group.id)
            quote = group.quote
            binding = quote.materialBinding
            key = (
                binding.companyId if binding else None,
                binding.catalog.id if binding else None,
                quote.material,
                math.floor(quote.thickness * 25.4 / 1e-6 + 0.5),
            )
            if key in material_keys:
                raise ValueError("Duplicate material/thickness group")
            material_keys.add(key)
            if quote.version == 7 and self.version != 6:
                raise ValueError("Orientation constraints require project version 6")
            if len({stock.id for stock in quote.options}) != len(quote.options):
                raise ValueError("Duplicate stock option IDs")
            if not any(stock.enabled for stock in quote.options):
                raise ValueError("Enable at least one stock option")
            group_quantity = max(1, sum(part.quantity for part in quote.parts))
            if any(
                stock.price is not None and not math.isfinite(stock.price * group_quantity) for stock in quote.options
            ):
                raise ValueError("Sheet price is too large for the requested quantity")
            if (
                binding
                and {"steel": "Carbon steel", "stainless": "Stainless steel", "aluminum": "Aluminum"}.get(
                    binding.catalog.category
                )
                != quote.material
            ):
                raise ValueError("Catalog category does not match material family")
            if quote.version != 7 and (
                quote.grainAxis is not None
                or any(part.rotationMode is not None or part.grainAxis is not None for part in quote.parts)
            ):
                raise ValueError("Orientation constraints require quote version 7")
            for part in quote.parts:
                if part.id in part_ids:
                    raise ValueError("Duplicate part ID")
                part_ids.add(part.id)
                designs += 1
                quantity += part.quantity
                vertices += sum(len(loop.points) if isinstance(loop, SavedPolygon) else 1 for loop in part.loops)
                vertices += sum(len(path) for path in (part.referencePaths or []))
                if designs > 300 or quantity > 300 or vertices > 20000:
                    raise ValueError("Estimate exceeds 300 parts or 20,000 geometry vertices")
        if self.activeGroupId not in ids:
            raise ValueError("Active material group does not exist")
        return self


class DraftReviewIssue(BaseModel):
    code: str
    message: str
    group_id: Optional[str] = None


class DraftRevisionSummary(BaseModel):
    draft_id: int
    company_id: int
    revision_number: int
    draft_version: int
    name: str
    status: Literal["DRAFT"] = "DRAFT"
    content_sha256: str
    payload_schema_version: int
    payload_bytes: int
    created_by: int
    created_at: datetime
    review_issues: list[DraftReviewIssue]


class DraftRevisionResponse(DraftRevisionSummary):
    schema_version: Literal[1] = 1
    estimate: dict[str, Any]


class DraftHistoryResponse(BaseModel):
    schema_version: Literal[1] = 1
    items: list[DraftRevisionSummary]
    total: int
    page: int
    per_page: int
