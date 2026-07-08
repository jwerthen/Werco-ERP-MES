"""Unit tests for fab_calc_engine — banded lookups and 4-bucket costing."""

import math

import pytest

from app.services.fab_calc_engine import (
    DEFAULT_BRAKE_TIME_ROWS,
    DEFAULT_GAUGE_ROWS,
    DEFAULT_LASER_SPEED_ROWS,
    DEFAULT_PIERCE_TIME_ROWS,
    DEFAULT_RATES,
    DEFAULT_WELD_REF_ROWS,
    BandRow,
    CalcErrorCode,
    FabLineInput,
    FabRates,
    LaserSpeedRow,
    MaterialFamily,
    calc_bid_summary,
    calc_brake_cost,
    calc_fab_line_item,
    calc_laser_cost,
    calc_machined_part,
    calc_material_cost,
    calc_part_weight,
    calc_sell_price,
    detect_material_family,
    lookup_banded,
    lookup_laser_speed,
    suggest_gauge_snap,
)


# ---------------------------------------------------------------------------
# lookup_banded
# ---------------------------------------------------------------------------


class TestLookupBanded:
    table = [
        BandRow(0.075, 18),
        BandRow(0.135, 22),
        BandRow(0.250, 30),
        BandRow(0.375, 40),
    ]

    def test_exact_match(self):
        assert lookup_banded(0.250, self.table, 15) == 30

    def test_between_bands_rounds_down(self):
        # 0.200 is between 0.135 and 0.250 → use 0.135 band
        assert lookup_banded(0.200, self.table, 15) == 22

    def test_below_smallest_uses_fallback(self):
        assert lookup_banded(0.030, self.table, 15) == 15

    def test_above_largest_uses_largest(self):
        assert lookup_banded(1.000, self.table, 15) == 40

    def test_none_thickness_uses_fallback(self):
        assert lookup_banded(None, self.table, 15) == 15

    def test_empty_table_uses_fallback(self):
        assert lookup_banded(0.250, [], 15) == 15

    def test_unsorted_table_still_works(self):
        shuffled = [self.table[2], self.table[0], self.table[3], self.table[1]]
        assert lookup_banded(0.200, shuffled, 15) == 22


# ---------------------------------------------------------------------------
# Material family + laser past capacity
# ---------------------------------------------------------------------------


class TestMaterialFamily:
    def test_stainless(self):
        assert detect_material_family("304 Stainless Steel") == MaterialFamily.STAINLESS

    def test_aluminum(self):
        assert detect_material_family("5052 Aluminum") == MaterialFamily.ALUMINUM

    def test_mild_default(self):
        assert detect_material_family("A36 Mild Steel") == MaterialFamily.MILD
        assert detect_material_family("1018 CD Bar") == MaterialFamily.MILD
        assert detect_material_family("CRS") == MaterialFamily.MILD


class TestLaserSpeed:
    def test_exact_band_mild(self):
        speed, err = lookup_laser_speed(
            0.075, MaterialFamily.MILD, DEFAULT_LASER_SPEED_ROWS, 100
        )
        assert err is None
        assert speed == 430

    def test_between_bands_rounds_down(self):
        # 0.090 → 0.075 band
        speed, err = lookup_laser_speed(
            0.090, MaterialFamily.MILD, DEFAULT_LASER_SPEED_ROWS, 100
        )
        assert err is None
        assert speed == 430

    def test_past_capacity_aluminum(self):
        speed, err = lookup_laser_speed(
            0.625, MaterialFamily.ALUMINUM, DEFAULT_LASER_SPEED_ROWS, 100
        )
        assert speed is None
        assert err is not None
        assert err.code == CalcErrorCode.PAST_CAPACITY

    def test_past_capacity_stainless_thick(self):
        speed, err = lookup_laser_speed(
            0.750, MaterialFamily.STAINLESS, DEFAULT_LASER_SPEED_ROWS, 100
        )
        assert err is not None
        assert err.code == CalcErrorCode.PAST_CAPACITY

    def test_below_first_band_fallback(self):
        speed, err = lookup_laser_speed(
            0.010, MaterialFamily.MILD, DEFAULT_LASER_SPEED_ROWS, 99
        )
        assert err is None
        assert speed == 99


# ---------------------------------------------------------------------------
# Gauge snap
# ---------------------------------------------------------------------------


class TestGaugeSnap:
    def test_suggests_snap_near_14ga(self):
        # mill-tolerance 0.0747 vs canonical mild 0.0747 in gauge table
        warn = suggest_gauge_snap(0.0747, MaterialFamily.MILD, DEFAULT_GAUGE_ROWS)
        # exact match to table → no snap warning (delta ~0)
        assert warn is None

    def test_suggests_snap_near_canonical(self):
        # 0.076 is within 2% of 0.0747
        warn = suggest_gauge_snap(0.076, MaterialFamily.MILD, DEFAULT_GAUGE_ROWS)
        assert warn is not None
        assert warn.code == "gauge_snap"
        assert warn.suggested_value == pytest.approx(0.0747)

    def test_no_snap_when_far(self):
        warn = suggest_gauge_snap(0.100, MaterialFamily.MILD, DEFAULT_GAUGE_ROWS)
        # 0.100 is near 12ga 0.1046 (~4.4%) — outside 2%
        assert warn is None


# ---------------------------------------------------------------------------
# Cost primitives
# ---------------------------------------------------------------------------


class TestCostPrimitives:
    def test_part_weight(self):
        # 0.075 * 12 * 24 * 0.284
        w = calc_part_weight(0.075, 12, 24, 0.284)
        assert w == pytest.approx(0.075 * 12 * 24 * 0.284)

    def test_material_cost_with_scrap(self):
        # qty 10, weight 1 lb, scrap 20%, $0.90/lb
        assert calc_material_cost(1.0, 10, 0.20, 0.90) == pytest.approx(10.8)

    def test_sell_price_margin_on_sell(self):
        # cost 70, margin 30% → sell = 70 / 0.7 = 100
        assert calc_sell_price(70.0, 0.30) == pytest.approx(100.0)

    def test_brake_hours_and_cost(self):
        cost, hours = calc_brake_cost(
            bend_count=4,
            qty=10,
            thickness_in=0.075,
            brake_time_table=DEFAULT_BRAKE_TIME_ROWS,
            brake_rate=95.0,
        )
        # 4 * 10 * 18 sec = 720 sec = 0.2 hr; * 95 = 19
        assert hours == pytest.approx(0.2)
        assert cost == pytest.approx(19.0)


# ---------------------------------------------------------------------------
# Fab line orchestrator
# ---------------------------------------------------------------------------


class TestFabLineItem:
    rates = DEFAULT_RATES

    def _calc(self, **kwargs):
        line = FabLineInput(**kwargs)
        return calc_fab_line_item(
            line,
            self.rates,
            DEFAULT_LASER_SPEED_ROWS,
            DEFAULT_PIERCE_TIME_ROWS,
            DEFAULT_BRAKE_TIME_ROWS,
            DEFAULT_GAUGE_ROWS,
            DEFAULT_WELD_REF_ROWS,
        )

    def test_full_14ga_rectangle(self):
        """Golden: 14 ga A36, 12x24, 4 bends, qty 10, 2 pierces."""
        out = self._calc(
            material="A36 Mild Steel",
            qty=10,
            thickness_in=0.075,
            width_in=12.0,
            length_in=24.0,
            cut_length_in=72.0,  # 2*(12+24)
            pierce_count=2,
            bend_count=4,
            price_per_lb=0.90,
        )
        assert out.ok
        assert out.material_family == MaterialFamily.MILD
        assert out.weight_ea_lb == pytest.approx(0.075 * 12 * 24 * 0.284)
        assert out.material_cost > 0
        assert out.laser_cost > 0
        assert out.brake_cost == pytest.approx(19.0)
        assert out.brake_hours == pytest.approx(0.2)
        assert out.weld_cost == 0.0
        assert out.line_total == pytest.approx(
            out.material_cost + out.laser_cost + out.brake_cost
        )

    def test_bend_only_partial_scope(self):
        """Only brake in scope — material/laser/weld $0."""
        out = self._calc(
            material="A36",
            qty=5,
            thickness_in=0.250,
            bend_count=6,
            include_material=False,
            include_laser=False,
            include_weld=False,
            include_brake=True,
        )
        assert out.ok
        assert out.material_cost == 0.0
        assert out.laser_cost == 0.0
        assert out.weld_cost == 0.0
        assert out.brake_cost > 0
        assert out.brake_hours > 0
        assert out.line_total == out.brake_cost

    def test_blank_geometry_zeros_material_laser(self):
        """Blank W/L/cut with brake only — natural $0 without toggles."""
        out = self._calc(
            material="A36",
            qty=1,
            thickness_in=0.135,
            bend_count=2,
            # no width/length/cut
        )
        assert out.material_cost == 0.0
        assert out.laser_cost == 0.0
        assert out.brake_cost > 0

    def test_past_capacity_hard_error(self):
        out = self._calc(
            material="5052 Aluminum",
            qty=1,
            thickness_in=0.625,
            cut_length_in=100.0,
            price_per_lb=2.10,
        )
        assert not out.ok
        assert any(e.code == CalcErrorCode.PAST_CAPACITY for e in out.errors)
        assert out.laser_cost == 0.0

    def test_family_override(self):
        out = self._calc(
            material="Mystery Alloy",
            qty=1,
            thickness_in=0.075,
            cut_length_in=40.0,
            material_family_override=MaterialFamily.STAINLESS,
            price_per_lb=2.30,
        )
        assert out.material_family == MaterialFamily.STAINLESS
        assert out.ok

    def test_auto_suggest_cut_length(self):
        out = self._calc(
            material="A36",
            qty=1,
            thickness_in=0.075,
            width_in=10.0,
            length_in=10.0,
            # no cut_length_in
            price_per_lb=0.90,
        )
        assert out.cut_length_used == 40.0
        assert any(w.code == "cut_length_suggested" for w in out.warnings)


class TestMachinedAndBid:
    def test_machined_cylinder(self):
        result = calc_machined_part(
            stock_dia_in=2.0,
            stock_length_in=6.0,
            qty=2,
            price_per_lb=1.25,
            scrap_factor=0.20,
            turning_minutes=30,
            milling_minutes=15,
            cnc_turning_rate=95,
            cnc_mill_rate=105,
        )
        expected_vol = math.pi * 1.0 * 1.0 * 6.0
        assert result["weight_ea_lb"] == pytest.approx(expected_vol * 0.284)
        assert result["line_total"] > 0
        assert result["turning_hours"] == pytest.approx(1.0)  # 30min * 2 / 60
        assert result["milling_hours"] == pytest.approx(0.5)

    def test_bid_summary_margin(self):
        line = calc_fab_line_item(
            FabLineInput(
                material="A36",
                qty=1,
                thickness_in=0.075,
                width_in=12,
                length_in=12,
                cut_length_in=48,
                bend_count=2,
                price_per_lb=0.90,
            ),
            DEFAULT_RATES,
            DEFAULT_LASER_SPEED_ROWS,
            DEFAULT_PIERCE_TIME_ROWS,
            DEFAULT_BRAKE_TIME_ROWS,
        )
        summary = calc_bid_summary(
            fab_lines=[line],
            buyout_extended_total=50.0,
            assembly_labor_hrs=2.0,
            electrical_labor_hrs=1.0,
            assembly_labor_rate=75.0,
            electrical_labor_rate=105.0,
            machined_subtotal=0.0,
            rates=DEFAULT_RATES,
        )
        assert summary.buyout_marked_up == pytest.approx(50.0 * 1.20)
        assert summary.assembly_labor_cost == pytest.approx(150.0)
        assert summary.electrical_labor_cost == pytest.approx(105.0)
        assert summary.cogs > summary.subtotal_before_oh
        assert summary.sell_price == pytest.approx(
            summary.cogs / (1.0 - DEFAULT_RATES.target_margin)
        )
        assert summary.brake_hours > 0
        assert summary.laser_hours > 0
