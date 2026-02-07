from app.models.quote_config import MaterialCategory, QuoteMaterial
from app.models.rfq_quote import PriceSnapshot, RfqPackage
from app.services.rfq_pricing_service import MaterialPriceService
from app.services.sheet_metal_costing_service import (
    calc_bending_cost,
    calc_cutting_cost,
    calc_margin,
    calc_material_cost,
    calc_required_weight_lbs,
    calc_shop_labor_oh,
)


def test_pricing_cache_and_snapshot_creation(db_session):
    package = RfqPackage(rfq_number="RFQ-TEST-001", customer_name="Test Customer", status="uploaded")
    db_session.add(package)
    db_session.flush()

    material = QuoteMaterial(
        name="A36 Steel",
        category=MaterialCategory.STEEL,
        stock_price_per_pound=1.25,
        density_lb_per_cubic_inch=0.284,
        sheet_pricing={"0.125": 6.0},
        is_active=True,
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
