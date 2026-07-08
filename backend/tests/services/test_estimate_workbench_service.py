"""Phase 1 tests: shop-data loaders + workbench persist/recalc."""

from __future__ import annotations

import pytest

from app.models.quote_config import MaterialCategory, QuoteMaterial, QuoteSettings
from app.models.rfq_quote import RfqPackage
from app.services.estimate_workbench_service import (
    StaleVersionError,
    create_blank_estimate,
    get_estimate_tree,
    load_shop_data,
    recalc_payload,
    save_estimate_tree,
    seed_cut_bend_defaults,
)
from app.services.fab_calc_engine import DEFAULT_RATES


@pytest.fixture
def rfq_package(db_session):
    pkg = RfqPackage(
        rfq_number="RFQ-EW-TEST-001",
        customer_name="Test Customer",
        status="uploaded",
        company_id=1,
    )
    db_session.add(pkg)
    db_session.commit()
    db_session.refresh(pkg)
    return pkg


class TestShopDataLoaders:
    def test_seed_and_load_returns_db_source(self, db_session):
        n = seed_cut_bend_defaults(db_session, company_id=1)
        db_session.commit()
        assert n == 5

        shop = load_shop_data(db_session, company_id=1)
        assert shop.source == "db"
        assert len(shop.cut_speed_table) >= 10
        assert len(shop.pierce_time_table) >= 5
        assert len(shop.brake_time_table) >= 5
        # 0.075 mild → 430 from Excel defaults
        mild_075 = next(r for r in shop.cut_speed_table if r.thickness == 0.075)
        assert mild_075.mild == 430

    def test_ensure_auto_seeds_on_load(self, db_session):
        shop = load_shop_data(db_session, company_id=1)
        assert shop.source == "db"
        assert len(shop.cut_speed_table) > 0

    def test_custom_rate_setting_overrides_default(self, db_session):
        db_session.add(
            QuoteSettings(
                company_id=1,
                setting_key="ew_laser_rate",
                setting_value="200",
                setting_type="number",
            )
        )
        db_session.commit()
        shop = load_shop_data(db_session, company_id=1)
        assert shop.rates.laser_rate == 200.0
        assert shop.rates.brake_rate == DEFAULT_RATES.brake_rate


class TestWorkbenchPersist:
    def test_create_blank_estimate(self, db_session, rfq_package):
        estimate = create_blank_estimate(
            db_session,
            rfq_package_id=rfq_package.id,
            company_id=1,
            user_id=None,
            audit=None,
        )
        assert estimate.id is not None
        assert estimate.version == 1
        assert len(estimate.assemblies) == 1
        assert estimate.assemblies[0].name == "Assembly 1"

    def test_save_and_recompute_fab_line(self, db_session, rfq_package):
        db_session.add(
            QuoteMaterial(
                name="A36 Mild Steel",
                category=MaterialCategory.STEEL,
                stock_price_per_pound=0.90,
                density_lb_per_cubic_inch=0.284,
                is_active=True,
                company_id=1,
            )
        )
        db_session.commit()

        estimate = create_blank_estimate(
            db_session,
            rfq_package_id=rfq_package.id,
            company_id=1,
            user_id=None,
        )
        payload = {
            "assemblies": [
                {
                    "name": "TC-2-EXT",
                    "assembly_labor_hrs": 1.0,
                    "electrical_labor_hrs": 0.0,
                    "fab_lines": [
                        {
                            "detail_name": "Detail-001 (Bottom)",
                            "material": "A36 Mild Steel",
                            "qty": 10,
                            "thickness_in": 0.075,
                            "width_in": 12.0,
                            "length_in": 24.0,
                            "cut_length_in": 72.0,
                            "pierce_count": 2,
                            "bend_count": 4,
                        }
                    ],
                    "buyout_lines": [
                        {
                            "description": "PEM nut",
                            "qty": 20,
                            "unit_cost": 0.45,
                            "confidence": "confirmed",
                        }
                    ],
                }
            ],
            "machined_parts": [],
        }
        saved = save_estimate_tree(
            db_session,
            estimate,
            payload,
            expected_version=1,
            company_id=1,
        )
        assert saved.version == 2
        assert saved.grand_total > 0
        asm = next(a for a in saved.assemblies if not a.is_deleted)
        assert asm.name == "TC-2-EXT"
        fab = next(f for f in asm.fab_line_items if not f.is_deleted)
        assert fab.brake_hours == pytest.approx(0.2)
        assert fab.brake_cost == pytest.approx(19.0)
        assert fab.material_cost > 0
        assert fab.laser_cost > 0
        buy = next(b for b in asm.buyout_line_items if not b.is_deleted)
        assert buy.extended_cost == pytest.approx(9.0)
        assert saved.internal_breakdown["brake_hours"] == pytest.approx(0.2)

    def test_stale_version_raises(self, db_session, rfq_package):
        estimate = create_blank_estimate(
            db_session,
            rfq_package_id=rfq_package.id,
            company_id=1,
            user_id=None,
        )
        with pytest.raises(StaleVersionError) as exc:
            save_estimate_tree(
                db_session,
                estimate,
                {"assemblies": [], "machined_parts": []},
                expected_version=0,
                company_id=1,
            )
        assert exc.value.current_version == 1

    def test_bend_only_partial_scope_persists(self, db_session, rfq_package):
        estimate = create_blank_estimate(
            db_session,
            rfq_package_id=rfq_package.id,
            company_id=1,
            user_id=None,
        )
        payload = {
            "assemblies": [
                {
                    "name": "Bend Only",
                    "fab_lines": [
                        {
                            "detail_name": "Bracket",
                            "material": "A36",
                            "qty": 5,
                            "thickness_in": 0.250,
                            "bend_count": 6,
                            "include_material": False,
                            "include_laser": False,
                            "include_weld": False,
                            "include_brake": True,
                        }
                    ],
                }
            ],
            "machined_parts": [],
        }
        saved = save_estimate_tree(
            db_session,
            estimate,
            payload,
            expected_version=1,
            company_id=1,
        )
        fab = next(f for a in saved.assemblies if not a.is_deleted for f in a.fab_line_items if not f.is_deleted)
        assert fab.material_cost == 0.0
        assert fab.laser_cost == 0.0
        assert fab.weld_cost == 0.0
        assert fab.brake_cost > 0
        assert fab.line_total == fab.brake_cost


class TestRecalcPayload:
    def test_recalc_uses_seeded_tables(self, db_session):
        seed_cut_bend_defaults(db_session, company_id=1)
        db_session.commit()

        class Asm:
            assembly_labor_hrs = 0
            electrical_labor_hrs = 0
            buyout_lines = []
            fab_lines = [
                type(
                    "FL",
                    (),
                    {
                        "material": "A36 Mild Steel",
                        "qty": 1,
                        "thickness_in": 0.075,
                        "width_in": 10.0,
                        "length_in": 10.0,
                        "cut_length_in": 40.0,
                        "pierce_count": 0,
                        "bend_count": 2,
                        "weld_length_in": None,
                        "weld_minutes_ea": None,
                        "material_family_override": None,
                        "include_material": True,
                        "include_laser": True,
                        "include_brake": True,
                        "include_weld": True,
                        "price_per_lb": 0.90,
                        "density_lb_per_in3": 0.284,
                        "detail_name": "Panel",
                        "part_number": None,
                    },
                )()
            ]

        result = recalc_payload(db_session, 1, [Asm()], [])
        assert result["shop_data_source"] == "db"
        assert result["summary"].brake_hours > 0
        assert result["summary"].sell_price > result["summary"].cogs
