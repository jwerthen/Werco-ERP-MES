"""Precision and malformed-source regressions for material provenance."""

from decimal import ROUND_DOWN, Decimal, localcontext
from sys import float_info

import pytest

from app.models.quote_config import MaterialCategory, QuoteMaterial
from app.schemas.quote_nesting_materials import MaterialResolutionRequest
from app.services.quote_nesting_materials import catalog_material, resolve_material

pytestmark = pytest.mark.unit


@pytest.fixture
def material(db_session):
    row = QuoteMaterial(
        company_id=1,
        name="A36",
        category=MaterialCategory.STEEL,
        stock_price_per_pound=0.9,
        density_lb_per_cubic_inch=0.284,
        sheet_pricing={"chosen": "2.005"},
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def request(row, **changes):
    data = dict(
        catalog_material_id=row.id,
        thickness_in="0.125",
        price_basis="per_square_foot",
        price_key="chosen",
        stock_options=[dict(id="s", width_in="12", length_in="12")],
    )
    data.update(changes)
    return MaterialResolutionRequest(**data)


def test_decimal_conversion_ignores_callers_precision_and_rounding(db_session, material):
    normal = resolve_material(db_session, 1, request(material))
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        limited = resolve_material(db_session, 1, request(material))
        assert context.prec == 3 and context.rounding == ROUND_DOWN
    assert limited == normal
    assert limited.stocks[0].sheet_cost == "2.01"


def test_max_finite_float_price_and_density_remain_calculable(db_session, material):
    material.stock_price_per_pound = float_info.max
    material.density_lb_per_cubic_inch = float_info.max
    db_session.commit()
    result = resolve_material(db_session, 1, request(material, price_basis="per_lb", price_key=None))
    with localcontext() as context:
        context.prec = 1024
        expected = (Decimal(str(float_info.max)) ** 2 * Decimal(18)).quantize(Decimal("0.01"))
    assert result.calculable is True
    assert result.stocks[0].sheet_cost == format(expected, "f")
    assert result.confirmed is False and result.currency is None


def test_malformed_legacy_sheet_keys_are_distinct_filtered_and_reported(db_session, material):
    # Do not pass these through JSON's coercion of integer keys into strings.
    material.sheet_pricing = {1: 10, "1": 11, "": 12, " ": 13, "x" * 101: 14}
    first = catalog_material(material)
    assert [option.price_key for option in first.price_options if option.price_basis == "per_square_foot"] == ["1"]
    result = resolve_material(db_session, 1, request(material, price_key="1"))
    assert result.stocks[0].sheet_cost == "11.00"
    assert "invalid_sheet_price_keys" in {issue.code for issue in result.issues}
    material.sheet_pricing = {"1": 11, "": 12, " ": 13, "x" * 101: 14}
    assert catalog_material(material).catalog_hash != first.catalog_hash


def test_source_digest_canonical_numbers_and_company_identity(material):
    material.sheet_pricing = {"chosen": 2}
    initial = catalog_material(material).catalog_hash
    material.sheet_pricing = {"chosen": 2.0}
    assert catalog_material(material).catalog_hash == initial
    material.company_id = 2
    assert catalog_material(material).catalog_hash != initial


def test_null_source_metadata_and_invalid_lifecycle_are_not_filled(db_session, material):
    material.updated_at = None
    material.stock_price_per_pound = None
    material.stock_price_per_cubic_inch = None
    material.density_lb_per_cubic_inch = None
    material.sheet_pricing = None
    db_session.commit()
    result = resolve_material(db_session, 1, request(material, price_basis="per_lb", price_key=None))
    assert result.catalog_material.source_updated_at is None
    assert result.catalog_material.density_lb_per_cubic_inch is None
    assert all(option.unit_price is None for option in result.catalog_material.price_options)
    assert result.calculable is False
    material.is_active = None
    db_session.commit()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        resolve_material(db_session, 1, request(material))
    assert exc.value.status_code == 404
