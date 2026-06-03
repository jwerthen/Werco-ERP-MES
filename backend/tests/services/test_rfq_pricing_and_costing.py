import pytest

from app.models.company import Company
from app.models.quote_config import MaterialCategory, QuoteMaterial
from app.models.rfq_quote import PriceSnapshot, RfqPackage
from app.services.rfq_pricing_service import MaterialPriceService
from app.services.sheet_metal_costing_service import (
    calc_bending_cost,
    calc_dynamic_scrap_factor,
    calc_cutting_cost,
    calc_finishing_cost,
    calc_margin,
    calc_material_cost,
    calc_required_weight_lbs,
    calc_shop_labor_oh,
    parse_thickness_to_inches,
)


def test_pricing_cache_and_snapshot_creation(db_session):
    package = RfqPackage(rfq_number="RFQ-TEST-001", customer_name="Test Customer", status="uploaded", company_id=1)
    db_session.add(package)
    db_session.flush()

    material = QuoteMaterial(
        name="A36 Steel",
        category=MaterialCategory.STEEL,
        stock_price_per_pound=1.25,
        density_lb_per_cubic_inch=0.284,
        sheet_pricing={"0.125": 6.0},
        is_active=True,
        company_id=1,
    )
    db_session.add(material)
    db_session.commit()

    service = MaterialPriceService()
    first = service.get_material_price(
        db=db_session,
        material="carbon steel",
        thickness="0.125",
        rfq_package_id=package.id,
        quote_estimate_id=None,
    )
    db_session.commit()

    assert first.unit_price > 0
    assert "QuoteMaterial" in first.source_name

    second = service.get_material_price(
        db=db_session,
        material="carbon steel",
        thickness="0.125",
        rfq_package_id=package.id,
        quote_estimate_id=None,
    )
    db_session.commit()
    assert "(cached)" in second.source_name

    snapshots = db_session.query(PriceSnapshot).all()
    assert len(snapshots) >= 3  # cache + estimate snapshots across two calls
    assert any(snapshot.snapshot_scope == "cache" for snapshot in snapshots)
    assert any(snapshot.price_type == "material" for snapshot in snapshots)


def test_material_pricing_normalizes_gauge_sheet_price_and_metadata(db_session):
    package = RfqPackage(rfq_number="RFQ-TEST-002", customer_name="Gauge Customer", status="uploaded", company_id=1)
    db_session.add(package)
    db_session.flush()

    material = QuoteMaterial(
        name="A36 10 Gauge",
        category=MaterialCategory.STEEL,
        stock_price_per_pound=1.25,
        density_lb_per_cubic_inch=0.284,
        machinability_factor=0.6,
        material_markup_pct=18.0,
        sheet_pricing={"10 ga": 7.5},
        is_active=True,
        company_id=1,
    )
    db_session.add(material)
    db_session.commit()

    result = MaterialPriceService().get_material_price(
        db=db_session,
        material="carbon steel",
        thickness="10ga",
        rfq_package_id=package.id,
    )

    expected_price_per_lb = 7.5 / (0.1345 * 144.0 * 0.284)
    assert result.unit_price == pytest.approx(expected_price_per_lb)
    assert result.density_lb_per_cubic_inch == pytest.approx(0.284)
    assert result.machinability_factor == pytest.approx(0.6)
    assert result.material_markup_pct == pytest.approx(18.0)


def test_material_pricing_is_company_scoped(db_session):
    other_company = Company(id=2, name="Other Fabricator", slug="other-fab", is_active=True)
    db_session.add(other_company)
    package = RfqPackage(rfq_number="RFQ-TEST-003", customer_name="Tenant Customer", status="uploaded", company_id=2)
    db_session.add(package)
    db_session.add_all(
        [
            QuoteMaterial(
                name="Company One Steel",
                category=MaterialCategory.STEEL,
                stock_price_per_pound=1.25,
                density_lb_per_cubic_inch=0.284,
                is_active=True,
                company_id=1,
            ),
            QuoteMaterial(
                name="Company Two Steel",
                category=MaterialCategory.STEEL,
                stock_price_per_pound=9.99,
                density_lb_per_cubic_inch=0.284,
                is_active=True,
                company_id=2,
            ),
        ]
    )
    db_session.commit()

    result = MaterialPriceService().get_material_price(
        db=db_session,
        material="carbon steel",
        thickness="0.125",
        rfq_package_id=package.id,
    )

    assert result.unit_price == pytest.approx(9.99)
    assert "Company Two Steel" in result.source_name


def test_sheet_metal_costing_is_deterministic():
    required_weight = calc_required_weight_lbs(
        flat_area_in2=120.0,
        thickness_in=0.125,
        material_key="carbon_steel",
        quantity=5,
    )
    material_cost = calc_material_cost(required_weight, unit_price_per_lb=1.0, scrap_factor=0.1)
    cutting = calc_cutting_cost(
        cut_length_in=180.0,
        quantity=5,
        material_key="carbon_steel",
        machine_rate_per_hour=150.0,
        setup_minutes=10.0,
    )
    bending = calc_bending_cost(
        bend_count=8,
        quantity=5,
        sec_per_bend=30.0,
        setup_minutes=5.0,
        brake_rate_per_hour=85.0,
    )
    labor_oh = calc_shop_labor_oh(cutting["cost"] + bending["cost"], overhead_pct=20.0)
    subtotal = material_cost + labor_oh
    margin = calc_margin(subtotal, margin_pct=25.0)
    total = subtotal + margin

    assert round(required_weight, 3) == 21.3
    assert round(material_cost, 2) == 23.43
    assert round(cutting["cost"], 2) == 35.23
    assert round(bending["cost"], 2) == 35.42
    assert round(labor_oh, 2) == 84.77
    assert round(margin, 2) == 27.05
    assert round(total, 2) == 135.25


def test_sheet_metal_costing_accuracy_enhancements():
    assert parse_thickness_to_inches("1/8 in") == pytest.approx(0.125)
    assert parse_thickness_to_inches("3mm") == pytest.approx(3 / 25.4)
    assert parse_thickness_to_inches("10 gauge") == pytest.approx(0.1345)

    scrap = calc_dynamic_scrap_factor(
        base_scrap_factor=0.10,
        quantity=1,
        flat_area_in2=18.0,
        cut_length_in=42.0,
        bend_count=8,
        hole_count=24,
        geometry_confidence=0.6,
    )
    assert scrap > 0.10

    material_cost = calc_material_cost(
        required_weight_lbs=10.0,
        unit_price_per_lb=2.0,
        scrap_factor=0.10,
        material_markup_pct=20.0,
    )
    assert material_cost == pytest.approx(26.4)

    cutting = calc_cutting_cost(
        cut_length_in=24.0,
        quantity=1,
        material_key="carbon_steel",
        machine_rate_per_hour=150.0,
        setup_minutes=2.0,
        thickness_in=0.25,
        pierce_count=5,
        pierce_time_seconds=1.0,
        min_charge=35.0,
    )
    assert cutting["cost"] == pytest.approx(35.0)
    assert cutting["minimum_charge_applied"] > 0
    assert cutting["speed_ipm"] < 220.0

    passivation = calc_finishing_cost(
        finish="Passivation",
        flat_area_in2=144.0,
        quantity=3,
        finish_rate_per_sqft=0.0,
        price_per_part=5.0,
        minimum_charge=35.0,
    )
    assert passivation == pytest.approx(35.0)
