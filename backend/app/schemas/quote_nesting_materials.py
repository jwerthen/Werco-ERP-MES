"""Read-only catalog resolution for quote nesting; no approval or stock claim."""

from decimal import Decimal
from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _positive_decimal(value: str) -> str:
    if Decimal(value) <= 0:
        raise ValueError("Dimension must be greater than zero")
    return value


DimensionString = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9]{1,12}(\.[0-9]{1,12})?$", max_length=25),
    AfterValidator(_positive_decimal),
]
PriceBasis = Literal["per_lb", "per_cubic_inch", "per_square_foot"]


class StockDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    width_in: DimensionString
    length_in: DimensionString


class MaterialResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_material_id: int = Field(strict=True, gt=0)
    thickness_in: DimensionString
    stock_options: list[StockDimensions] = Field(min_length=1, max_length=20)
    price_basis: PriceBasis
    price_key: Optional[str] = Field(default=None, min_length=1, max_length=100)
    expected_catalog_hash: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_selection(self):
        if len({stock.id for stock in self.stock_options}) != len(self.stock_options):
            raise ValueError("Stock option IDs must be unique")
        if self.price_basis == "per_square_foot" and self.price_key is None:
            raise ValueError("per_square_foot requires an exact catalog price_key")
        if self.price_basis != "per_square_foot" and self.price_key is not None:
            raise ValueError("price_key is only valid for per_square_foot")
        return self


class CatalogPriceOption(BaseModel):
    price_basis: PriceBasis
    price_key: Optional[str] = None
    source_field: str
    unit_price: Optional[str] = None


class CatalogMaterial(BaseModel):
    id: int
    name: str
    category: str
    source_updated_at: Optional[str] = None
    catalog_hash: str
    density_lb_per_cubic_inch: Optional[str] = None
    price_options: list[CatalogPriceOption]
    missing_metadata: list[str]


class MaterialCatalogResponse(BaseModel):
    schema_version: Literal[1] = 1
    items: list[CatalogMaterial]
    total: int
    offset: int
    limit: int


class ResolutionIssue(BaseModel):
    code: str
    field: str
    message: str


class ResolvedStock(BaseModel):
    id: str
    width_in: str
    length_in: str
    area_sq_in: str
    area_sq_ft: str
    volume_cu_in: str
    weight_lb: Optional[str] = None
    sheet_cost: Optional[str] = None


class MaterialResolutionResponse(BaseModel):
    schema_version: Literal[1] = 1
    company_id: int
    catalog_material: CatalogMaterial
    thickness_in: str
    price_basis: PriceBasis
    price_key: Optional[str] = None
    currency: None = None
    status: Literal["review_required", "unresolved"]
    calculable: bool
    confirmed: Literal[False] = False
    stocks: list[ResolvedStock]
    issues: list[ResolutionIssue]
    content_hash: str
