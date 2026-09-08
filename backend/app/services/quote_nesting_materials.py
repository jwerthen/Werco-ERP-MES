"""Pure selection/conversion of explicit catalog inputs for quote-only nesting.

This service never seeds settings, posts audit rows, commits, or changes inventory.
QuoteMaterial has no approved inventory mapping, currency or price effectivity;
numeric calculability must not be presented as confirmed material compatibility.
"""

import hashlib
import json
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.time_utils import to_utc_iso
from app.db.tenant_filter import tenant_query
from app.models.quote_config import QuoteMaterial
from app.schemas.quote_nesting_materials import (
    CatalogMaterial,
    CatalogPriceOption,
    MaterialCatalogResponse,
    MaterialResolutionRequest,
    MaterialResolutionResponse,
    ResolutionIssue,
    ResolvedStock,
)

MISSING_METADATA = {
    "currency": "The quote catalog does not record a currency; no currency has been inferred.",
    "grade": "The catalog name is not an approved structured material grade or specification.",
    "coating": "An authoritative coating or finish requirement is not recorded.",
    "certification": "Authoritative certification requirements and evidence are not recorded.",
    "inventory_mapping": "There is no approved mapping from this catalog row to physical inventory.",
    "price_effective_date": "The catalog update timestamp is not an approved price effective date.",
    "price_expiry": "Price expiry is not recorded, so current price validity is unconfirmed.",
    "approved_revision": "The catalog has no approved material or pricing revision.",
    "authoritative_thickness": "Thickness and sheet dimensions are estimator inputs, not verified catalog specifications.",
}


def decimal_text(value: Decimal) -> str:
    """Canonical non-exponent decimal text without context-dependent normalize()."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _source_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    if len(str(value)) > 128:
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    if not result.is_finite() or result <= 0:
        return None
    # Legacy price storage is Float/JSON numeric. Bound unusual string/Decimal
    # values too, before fixed-point formatting can expand an enormous exponent.
    if not -324 <= result.adjusted() <= 308 or len(result.as_tuple().digits) > 40:
        return None
    return result


def _price_text(value: Any) -> Optional[str]:
    result = _source_decimal(value)
    return decimal_text(result) if result is not None else None


def _canonical_source(value: Any) -> Any:
    """Preserve invalid legacy values in the source digest without emitting JSON NaN."""
    if isinstance(value, dict):
        # JSON storage normally guarantees string keys. Preserve malformed legacy
        # mappings distinctly too; str(key) alone can collapse 1 and "1".
        return {
            "entries": [
                [type(key).__name__, str(key), _canonical_source(item)]
                for key, item in sorted(value.items(), key=lambda pair: (type(pair[0]).__name__, str(pair[0])))
            ]
        }
    if isinstance(value, list):
        return [_canonical_source(item) for item in value]
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = Decimal(str(value))
        return decimal_text(number) if number.is_finite() else str(number)
    return value


def _digest(snapshot: dict) -> str:
    serialized = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _catalog_snapshot(material: QuoteMaterial) -> dict:
    return {
        "schema_version": 1,
        "company_id": material.company_id,
        "id": material.id,
        "name": material.name,
        "category": getattr(material.category, "value", material.category),
        "source_updated_at": (to_utc_iso(material.updated_at) if material.updated_at is not None else None),
        "stock_price_per_pound": _canonical_source(material.stock_price_per_pound),
        "stock_price_per_cubic_inch": _canonical_source(material.stock_price_per_cubic_inch),
        "density_lb_per_cubic_inch": _canonical_source(material.density_lb_per_cubic_inch),
        "sheet_pricing": _canonical_source(material.sheet_pricing),
    }


def _sheet_prices(material: QuoteMaterial) -> dict:
    if not isinstance(material.sheet_pricing, dict):
        return {}
    return {
        key: value
        for key, value in material.sheet_pricing.items()
        if isinstance(key, str) and key.strip() and len(key) <= 100
    }


def catalog_material(material: QuoteMaterial) -> CatalogMaterial:
    snapshot = _catalog_snapshot(material)
    options = [
        CatalogPriceOption(
            price_basis="per_lb",
            source_field="stock_price_per_pound",
            unit_price=_price_text(material.stock_price_per_pound),
        ),
        CatalogPriceOption(
            price_basis="per_cubic_inch",
            source_field="stock_price_per_cubic_inch",
            unit_price=_price_text(material.stock_price_per_cubic_inch),
        ),
    ]
    sheet_prices = _sheet_prices(material)
    if sheet_prices:
        options.extend(
            CatalogPriceOption(
                price_basis="per_square_foot",
                price_key=key,
                source_field="sheet_pricing",
                unit_price=_price_text(sheet_prices[key]),
            )
            for key in sorted(sheet_prices)
        )
    return CatalogMaterial(
        id=material.id,
        name=material.name,
        category=snapshot["category"],
        source_updated_at=snapshot["source_updated_at"],
        catalog_hash=_digest(snapshot),
        density_lb_per_cubic_inch=_price_text(material.density_lb_per_cubic_inch),
        price_options=options,
        missing_metadata=list(MISSING_METADATA),
    )


def list_catalog(db: Session, company_id: int, *, offset: int, limit: int) -> MaterialCatalogResponse:
    # QuoteMaterial uses is_active as its lifecycle flag; it has no SoftDeleteMixin.
    query = tenant_query(db, QuoteMaterial, company_id).filter(QuoteMaterial.is_active.is_(True))
    return MaterialCatalogResponse(
        items=[catalog_material(row) for row in query.order_by(QuoteMaterial.id).offset(offset).limit(limit).all()],
        total=query.count(),
        offset=offset,
        limit=limit,
    )


def resolve_material(db: Session, company_id: int, request: MaterialResolutionRequest) -> MaterialResolutionResponse:
    material = (
        tenant_query(db, QuoteMaterial, company_id)
        .filter(
            QuoteMaterial.id == request.catalog_material_id,
            QuoteMaterial.is_active.is_(True),
        )
        .first()
    )
    if material is None:
        raise HTTPException(404, "Active quote material not found")
    catalog = catalog_material(material)
    if request.expected_catalog_hash is not None and request.expected_catalog_hash != catalog.catalog_hash:
        raise HTTPException(
            409,
            "The selected catalog material changed. Reload it before resolving its price.",
        )

    issues = [
        ResolutionIssue(code=f"missing_{key}", field=key, message=message) for key, message in MISSING_METADATA.items()
    ]
    density = _source_decimal(material.density_lb_per_cubic_inch)
    if density is None:
        issues.append(
            ResolutionIssue(
                code="density_missing_or_invalid",
                field="density_lb_per_cubic_inch",
                message="A positive finite catalog density is required for weight and per-pound conversion.",
            )
        )
    selected_option = next(
        (
            option
            for option in catalog.price_options
            if option.price_basis == request.price_basis and option.price_key == request.price_key
        ),
        None,
    )
    # Options already validated the raw source. Their canonical fixed-point text
    # may be longer than the raw-value bound for a valid extreme Float exponent.
    price = (
        Decimal(selected_option.unit_price)
        if selected_option is not None and selected_option.unit_price is not None
        else None
    )
    if price is None:
        issues.append(
            ResolutionIssue(
                code="selected_price_missing_or_invalid",
                field=("price_key" if request.price_basis == "per_square_foot" else "price_basis"),
                message="The explicitly selected catalog price is missing, zero, negative or invalid; no fallback was used.",
            )
        )
    if request.price_basis == "per_square_foot":
        issues.append(
            ResolutionIssue(
                code="sheet_price_thickness_unverified",
                field="price_key",
                message="The exact sheet-price key was selected; its relationship to the supplied thickness is unverified.",
            )
        )
        if not isinstance(material.sheet_pricing, dict) or len(_sheet_prices(material)) != len(material.sheet_pricing):
            issues.append(
                ResolutionIssue(
                    code="invalid_sheet_price_keys",
                    field="sheet_pricing",
                    message="Some catalog sheet-price keys are malformed and cannot be selected.",
                )
            )
    calculable = price is not None and (request.price_basis != "per_lb" or density is not None)
    stocks = []
    # Bounded decimal input lengths plus legacy Float prices fit this context, even
    # at Float's extreme exponent. No binary arithmetic or global context mutation.
    with localcontext(Context(prec=1024, rounding=ROUND_HALF_UP)):
        thickness = Decimal(request.thickness_in)
        for stock in sorted(request.stock_options, key=lambda item: item.id):
            width, length = Decimal(stock.width_in), Decimal(stock.length_in)
            area = width * length
            volume = area * thickness
            weight = volume * density if density is not None else None
            amount = None
            if calculable and price is not None:
                if request.price_basis == "per_lb" and weight is not None:
                    amount = weight * price
                elif request.price_basis == "per_cubic_inch":
                    amount = volume * price
                else:
                    amount = area * price / Decimal(144)
            stocks.append(
                ResolvedStock(
                    id=stock.id,
                    width_in=decimal_text(width),
                    length_in=decimal_text(length),
                    area_sq_in=decimal_text(area),
                    # Display conversions may recur; retain 12 decimal places.
                    # Cost uses the full-precision quotient above, never this text.
                    area_sq_ft=decimal_text((area / Decimal(144)).quantize(Decimal("0.000000000001"))),
                    volume_cu_in=decimal_text(volume),
                    weight_lb=decimal_text(weight) if weight is not None else None,
                    sheet_cost=(
                        format(
                            amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                            "f",
                        )
                        if amount is not None
                        else None
                    ),
                )
            )
    response = MaterialResolutionResponse(
        company_id=company_id,
        catalog_material=catalog,
        thickness_in=decimal_text(thickness),
        price_basis=request.price_basis,
        price_key=request.price_key,
        status="review_required" if calculable else "unresolved",
        calculable=calculable,
        stocks=stocks,
        issues=issues,
        content_hash="",
    )
    # There is no generated timestamp: equal canonical inputs + source data replay
    # identically. Request ordering and cosmetic decimal zeros are not identity.
    response.content_hash = _digest(response.model_dump(exclude={"content_hash"}))
    return response
