"""Pure fab estimating calc engine — thickness-banded machine physics.

No SQLAlchemy, FastAPI, or React imports. Pass rates and table rows as args
so unit tests are trivial and formulas stay auditable.

Design rules (see docs/ESTIMATE_WORKBENCH.md):
- Banded lookup: largest thickness <= input; below first band → fallback
- Past-capacity cells (None) → typed error, never silent cost
- Partial scope: blank optional geometry → $0 for that bucket
- Sell price = cost / (1 - target_margin)  (margin on sell, not markup on cost)
- Always return hours alongside $ for labor-driven buckets
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class MaterialFamily(str, Enum):
    MILD = "mild"
    STAINLESS = "stainless"
    ALUMINUM = "aluminum"


class CalcErrorCode(str, Enum):
    PAST_CAPACITY = "past_capacity"
    MISSING_THICKNESS = "missing_thickness"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True)
class CalcError:
    code: CalcErrorCode
    message: str
    field: Optional[str] = None


@dataclass(frozen=True)
class CalcWarning:
    code: str
    message: str
    field: Optional[str] = None
    suggested_value: Optional[float] = None


@dataclass(frozen=True)
class BandRow:
    """Single thickness band for pierce / brake (one value column)."""

    thickness: float
    value: float


@dataclass(frozen=True)
class LaserSpeedRow:
    """Laser cut speed band; None in a family column = past capacity."""

    thickness: float
    mild: Optional[float]
    stainless: Optional[float]
    aluminum: Optional[float]


@dataclass(frozen=True)
class GaugeRow:
    gauge: int
    mild: float
    stainless: float
    aluminum: float


@dataclass(frozen=True)
class WeldRefRow:
    fillet_leg_in: float
    arc_in_per_min: float
    min_per_in: float


@dataclass
class FabLineInput:
    material: str
    qty: int = 1
    thickness_in: Optional[float] = None
    width_in: Optional[float] = None
    length_in: Optional[float] = None
    cut_length_in: Optional[float] = None
    pierce_count: int = 0
    bend_count: int = 0
    weld_length_in: Optional[float] = None
    weld_minutes_ea: Optional[float] = None  # user override
    material_family_override: Optional[MaterialFamily] = None
    # Explicit scope toggles (default all on). Disabled ops force $0.
    include_material: bool = True
    include_laser: bool = True
    include_brake: bool = True
    include_weld: bool = True
    price_per_lb: float = 0.0
    density_lb_per_in3: float = 0.284


@dataclass
class FabRates:
    laser_rate: float
    brake_rate: float
    weld_rate: float
    scrap_factor: float
    laser_speed_fallback: float
    pierce_time_fallback: float
    target_margin: float = 0.30
    overhead_markup: float = 0.15
    buyout_markup: float = 0.20
    consumables_per_job: float = 25.0


@dataclass
class FabLineBreakdown:
    weight_ea_lb: float = 0.0
    material_cost: float = 0.0
    laser_cost: float = 0.0
    laser_minutes: float = 0.0
    laser_hours: float = 0.0
    brake_cost: float = 0.0
    brake_hours: float = 0.0
    weld_cost: float = 0.0
    weld_minutes_ea: float = 0.0
    weld_hours: float = 0.0
    line_total: float = 0.0
    material_family: MaterialFamily = MaterialFamily.MILD
    cut_length_used: float = 0.0
    errors: List[CalcError] = field(default_factory=list)
    warnings: List[CalcWarning] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class BidSummary:
    fab_material: float = 0.0
    fab_laser: float = 0.0
    fab_brake: float = 0.0
    fab_weld: float = 0.0
    fab_subtotal: float = 0.0
    buyout_subtotal: float = 0.0
    buyout_marked_up: float = 0.0
    assembly_labor_cost: float = 0.0
    electrical_labor_cost: float = 0.0
    machined_subtotal: float = 0.0
    laser_hours: float = 0.0
    brake_hours: float = 0.0
    weld_hours: float = 0.0
    assembly_hours: float = 0.0
    electrical_hours: float = 0.0
    subtotal_before_oh: float = 0.0
    overhead: float = 0.0
    consumables: float = 0.0
    cogs: float = 0.0
    sell_price: float = 0.0
    target_margin: float = 0.0
    errors: List[CalcError] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def lookup_banded(
    thickness: Optional[float],
    table: Sequence[BandRow],
    fallback: float,
) -> float:
    """Approximate-match: value for largest table.thickness <= thickness.

    If thickness is None or below the smallest band, return fallback.
    Table may be unsorted; we sort ascending for the scan.
    """
    if thickness is None or thickness <= 0:
        return fallback
    if not table:
        return fallback
    rows = sorted(table, key=lambda r: r.thickness)
    if thickness < rows[0].thickness:
        return fallback
    chosen = rows[0]
    for row in rows:
        if row.thickness <= thickness:
            chosen = row
        else:
            break
    return chosen.value


def detect_material_family(material_text: str) -> MaterialFamily:
    """Case-insensitive substring: stain → stainless, alum → aluminum, else mild."""
    text = (material_text or "").lower()
    if "stain" in text:
        return MaterialFamily.STAINLESS
    if "alum" in text:
        return MaterialFamily.ALUMINUM
    return MaterialFamily.MILD


def lookup_laser_speed(
    thickness: Optional[float],
    family: MaterialFamily,
    table: Sequence[LaserSpeedRow],
    fallback: float,
) -> Tuple[Optional[float], Optional[CalcError]]:
    """Banded laser speed. Returns (speed, error). Past-capacity → error."""
    if thickness is None or thickness <= 0:
        return fallback, None
    if not table:
        return fallback, None
    rows = sorted(table, key=lambda r: r.thickness)
    if thickness < rows[0].thickness:
        return fallback, None
    chosen = rows[0]
    for row in rows:
        if row.thickness <= thickness:
            chosen = row
        else:
            break
    speed = {
        MaterialFamily.MILD: chosen.mild,
        MaterialFamily.STAINLESS: chosen.stainless,
        MaterialFamily.ALUMINUM: chosen.aluminum,
    }[family]
    if speed is None:
        return None, CalcError(
            code=CalcErrorCode.PAST_CAPACITY,
            message=(
                f"Thickness {thickness}\" is past laser capacity for "
                f"{family.value} (band {chosen.thickness}\")"
            ),
            field="thickness_in",
        )
    return float(speed), None


def suggest_gauge_snap(
    thickness: Optional[float],
    family: MaterialFamily,
    gauge_table: Sequence[GaugeRow],
    tolerance_pct: float = 0.02,
) -> Optional[CalcWarning]:
    """If thickness is within ~2% of a canonical gauge decimal, suggest snap."""
    if thickness is None or thickness <= 0 or not gauge_table:
        return None
    best: Optional[Tuple[float, int, float]] = None  # (delta_pct, gauge, canonical)
    for row in gauge_table:
        canonical = {
            MaterialFamily.MILD: row.mild,
            MaterialFamily.STAINLESS: row.stainless,
            MaterialFamily.ALUMINUM: row.aluminum,
        }[family]
        if canonical <= 0:
            continue
        delta = abs(thickness - canonical) / canonical
        if delta <= tolerance_pct and delta > 1e-9:
            if best is None or delta < best[0]:
                best = (delta, row.gauge, canonical)
    if best is None:
        return None
    _, gauge, canonical = best
    return CalcWarning(
        code="gauge_snap",
        message=(
            f"Thickness {thickness}\" is within {tolerance_pct:.0%} of "
            f"{gauge} ga canonical {canonical}\" — snap to avoid wrong band"
        ),
        field="thickness_in",
        suggested_value=canonical,
    )


def estimate_weld_minutes(
    weld_length_in: float,
    fillet_leg_in: float,
    weld_table: Sequence[WeldRefRow],
) -> float:
    """First-pass weld minutes from reference table (min/in already includes OF)."""
    if weld_length_in <= 0 or not weld_table:
        return 0.0
    rows = sorted(weld_table, key=lambda r: r.fillet_leg_in)
    chosen = rows[0]
    for row in rows:
        if row.fillet_leg_in <= fillet_leg_in:
            chosen = row
        else:
            break
    return weld_length_in * chosen.min_per_in


# ---------------------------------------------------------------------------
# Cost primitives
# ---------------------------------------------------------------------------


def calc_part_weight(
    thickness_in: float,
    width_in: float,
    length_in: float,
    density_lb_per_in3: float = 0.284,
) -> float:
    return max(thickness_in, 0.0) * max(width_in, 0.0) * max(length_in, 0.0) * max(
        density_lb_per_in3, 0.0
    )


def calc_material_cost(
    weight_lb: float,
    qty: int,
    scrap_factor: float,
    price_per_lb: float,
) -> float:
    return max(qty, 0) * max(weight_lb, 0.0) * (1.0 + max(scrap_factor, 0.0)) * max(
        price_per_lb, 0.0
    )


def calc_laser_cost(
    cut_length_in: float,
    pierce_count: int,
    thickness_in: Optional[float],
    family: MaterialFamily,
    cut_speed_table: Sequence[LaserSpeedRow],
    pierce_time_table: Sequence[BandRow],
    laser_rate: float,
    fallback_speed: float,
    fallback_pierce: float,
) -> Tuple[float, float, Optional[CalcError]]:
    """Returns (cost, minutes, error)."""
    if cut_length_in <= 0 and pierce_count <= 0:
        return 0.0, 0.0, None
    speed, err = lookup_laser_speed(thickness_in, family, cut_speed_table, fallback_speed)
    if err:
        return 0.0, 0.0, err
    assert speed is not None and speed > 0
    pierce_sec = lookup_banded(thickness_in, pierce_time_table, fallback_pierce)
    minutes = (max(cut_length_in, 0.0) / speed) + (max(pierce_count, 0) * pierce_sec / 60.0)
    cost = (minutes / 60.0) * max(laser_rate, 0.0)
    return cost, minutes, None


def calc_brake_cost(
    bend_count: int,
    qty: int,
    thickness_in: Optional[float],
    brake_time_table: Sequence[BandRow],
    brake_rate: float,
    fallback_sec: float = 15.0,
) -> Tuple[float, float]:
    """Returns (cost, hours)."""
    if bend_count <= 0 or qty <= 0:
        return 0.0, 0.0
    sec_per_bend = lookup_banded(thickness_in, brake_time_table, fallback_sec)
    seconds = bend_count * qty * sec_per_bend
    hours = seconds / 3600.0
    cost = hours * max(brake_rate, 0.0)
    return cost, hours


def calc_weld_cost(weld_minutes_ea: float, qty: int, weld_rate: float) -> Tuple[float, float]:
    """Returns (cost, hours)."""
    if weld_minutes_ea <= 0 or qty <= 0:
        return 0.0, 0.0
    hours = (weld_minutes_ea * qty) / 60.0
    cost = hours * max(weld_rate, 0.0)
    return cost, hours


def calc_sell_price(cogs: float, target_margin: float) -> float:
    """sell = cost / (1 - margin). Margin applied to sell price, not cost."""
    if cogs <= 0:
        return 0.0
    m = max(min(target_margin, 0.99), 0.0)
    if m >= 1.0:
        return cogs
    return cogs / (1.0 - m)


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------


def calc_fab_line_item(
    line: FabLineInput,
    rates: FabRates,
    cut_speed_table: Sequence[LaserSpeedRow],
    pierce_time_table: Sequence[BandRow],
    brake_time_table: Sequence[BandRow],
    gauge_table: Sequence[GaugeRow] = (),
    weld_table: Sequence[WeldRefRow] = (),
    default_fillet_leg_in: float = 0.1875,
) -> FabLineBreakdown:
    """Compute 4-bucket breakdown for one fab line. Partial scope → $0 buckets."""
    out = FabLineBreakdown()
    family = line.material_family_override or detect_material_family(line.material)
    out.material_family = family

    needs_thickness = (
        (line.include_laser and (line.cut_length_in or line.pierce_count))
        or (line.include_brake and line.bend_count > 0)
    )
    if needs_thickness and (line.thickness_in is None or line.thickness_in <= 0):
        out.warnings.append(
            CalcWarning(
                code="missing_thickness",
                message="Thickness required for laser/brake lookups",
                field="thickness_in",
            )
        )

    snap = suggest_gauge_snap(line.thickness_in, family, gauge_table)
    if snap:
        out.warnings.append(snap)

    # --- Material ---
    if line.include_material and line.thickness_in and line.width_in and line.length_in:
        out.weight_ea_lb = calc_part_weight(
            line.thickness_in, line.width_in, line.length_in, line.density_lb_per_in3
        )
        out.material_cost = calc_material_cost(
            out.weight_ea_lb, line.qty, rates.scrap_factor, line.price_per_lb
        )

    # --- Laser ---
    if line.include_laser:
        cut_len = line.cut_length_in
        if cut_len is None and line.width_in and line.length_in:
            cut_len = 2.0 * (line.width_in + line.length_in)
            out.warnings.append(
                CalcWarning(
                    code="cut_length_suggested",
                    message=f"Cut length auto-suggested as 2*(W+L) = {cut_len}\"",
                    field="cut_length_in",
                    suggested_value=cut_len,
                )
            )
        cut_len = cut_len or 0.0
        out.cut_length_used = cut_len
        if cut_len > 0 or line.pierce_count > 0:
            cost, minutes, err = calc_laser_cost(
                cut_len,
                line.pierce_count,
                line.thickness_in,
                family,
                cut_speed_table,
                pierce_time_table,
                rates.laser_rate,
                rates.laser_speed_fallback,
                rates.pierce_time_fallback,
            )
            if err:
                out.errors.append(err)
            else:
                out.laser_cost = cost
                out.laser_minutes = minutes
                out.laser_hours = minutes / 60.0

    # --- Brake ---
    if line.include_brake and line.bend_count > 0:
        cost, hours = calc_brake_cost(
            line.bend_count,
            line.qty,
            line.thickness_in,
            brake_time_table,
            rates.brake_rate,
        )
        out.brake_cost = cost
        out.brake_hours = hours

    # --- Weld ---
    if line.include_weld:
        weld_min = line.weld_minutes_ea
        if weld_min is None and line.weld_length_in and line.weld_length_in > 0:
            weld_min = estimate_weld_minutes(
                line.weld_length_in, default_fillet_leg_in, weld_table
            )
        weld_min = weld_min or 0.0
        out.weld_minutes_ea = weld_min
        cost, hours = calc_weld_cost(weld_min, line.qty, rates.weld_rate)
        out.weld_cost = cost
        out.weld_hours = hours

    out.line_total = (
        out.material_cost + out.laser_cost + out.brake_cost + out.weld_cost
    )
    return out


def calc_machined_part(
    stock_dia_in: float,
    stock_length_in: float,
    qty: int,
    price_per_lb: float,
    scrap_factor: float,
    turning_minutes: float,
    milling_minutes: float,
    cnc_turning_rate: float,
    cnc_mill_rate: float,
    density_lb_per_in3: float = 0.284,
) -> Dict[str, float]:
    """Cylindrical blank weight + CNC minutes."""
    import math

    radius = max(stock_dia_in, 0.0) / 2.0
    volume = math.pi * radius * radius * max(stock_length_in, 0.0)
    weight_ea = volume * max(density_lb_per_in3, 0.0)
    material_cost = calc_material_cost(weight_ea, qty, scrap_factor, price_per_lb)
    turn_hours = (max(turning_minutes, 0.0) * max(qty, 0)) / 60.0
    mill_hours = (max(milling_minutes, 0.0) * max(qty, 0)) / 60.0
    turning_cost = turn_hours * max(cnc_turning_rate, 0.0)
    milling_cost = mill_hours * max(cnc_mill_rate, 0.0)
    return {
        "weight_ea_lb": weight_ea,
        "material_cost": material_cost,
        "turning_cost": turning_cost,
        "turning_hours": turn_hours,
        "milling_cost": milling_cost,
        "milling_hours": mill_hours,
        "line_total": material_cost + turning_cost + milling_cost,
    }


def calc_bid_summary(
    fab_lines: Sequence[FabLineBreakdown],
    buyout_extended_total: float,
    assembly_labor_hrs: float,
    electrical_labor_hrs: float,
    assembly_labor_rate: float,
    electrical_labor_rate: float,
    machined_subtotal: float,
    rates: FabRates,
) -> BidSummary:
    s = BidSummary(target_margin=rates.target_margin)
    for line in fab_lines:
        if line.errors:
            s.errors.extend(line.errors)
            continue
        s.fab_material += line.material_cost
        s.fab_laser += line.laser_cost
        s.fab_brake += line.brake_cost
        s.fab_weld += line.weld_cost
        s.laser_hours += line.laser_hours
        s.brake_hours += line.brake_hours
        s.weld_hours += line.weld_hours
    s.fab_subtotal = s.fab_material + s.fab_laser + s.fab_brake + s.fab_weld
    s.buyout_subtotal = max(buyout_extended_total, 0.0)
    s.buyout_marked_up = s.buyout_subtotal * (1.0 + max(rates.buyout_markup, 0.0))
    s.assembly_hours = max(assembly_labor_hrs, 0.0)
    s.electrical_hours = max(electrical_labor_hrs, 0.0)
    s.assembly_labor_cost = s.assembly_hours * max(assembly_labor_rate, 0.0)
    s.electrical_labor_cost = s.electrical_hours * max(electrical_labor_rate, 0.0)
    s.machined_subtotal = max(machined_subtotal, 0.0)
    s.subtotal_before_oh = (
        s.fab_subtotal
        + s.buyout_marked_up
        + s.assembly_labor_cost
        + s.electrical_labor_cost
        + s.machined_subtotal
    )
    s.overhead = s.subtotal_before_oh * max(rates.overhead_markup, 0.0)
    s.consumables = max(rates.consumables_per_job, 0.0) if s.subtotal_before_oh > 0 else 0.0
    s.cogs = s.subtotal_before_oh + s.overhead + s.consumables
    s.sell_price = calc_sell_price(s.cogs, rates.target_margin)
    return s


# ---------------------------------------------------------------------------
# Default seed tables (Excel workbook defaults — also used by seed script)
# ---------------------------------------------------------------------------

DEFAULT_LASER_SPEED_ROWS: List[LaserSpeedRow] = [
    LaserSpeedRow(0.030, 900, 1000, 800),
    LaserSpeedRow(0.048, 700, 850, 650),
    LaserSpeedRow(0.060, 560, 650, 520),
    LaserSpeedRow(0.075, 430, 480, 400),
    LaserSpeedRow(0.105, 330, 380, 330),
    LaserSpeedRow(0.135, 260, 300, 270),
    LaserSpeedRow(0.187, 150, 200, 180),
    LaserSpeedRow(0.250, 95, 105, 85),
    LaserSpeedRow(0.313, 55, 70, 40),
    LaserSpeedRow(0.375, 46, 38, 26),
    LaserSpeedRow(0.500, 42, 26, 16),
    LaserSpeedRow(0.625, 30, 16, None),
    LaserSpeedRow(0.750, 22, None, None),
    LaserSpeedRow(1.000, 11, None, None),
]

DEFAULT_PIERCE_TIME_ROWS: List[BandRow] = [
    BandRow(0.030, 0.5),
    BandRow(0.075, 0.7),
    BandRow(0.135, 1.0),
    BandRow(0.250, 1.5),
    BandRow(0.375, 2.0),
    BandRow(0.500, 3.0),
    BandRow(0.625, 5.0),
    BandRow(0.750, 6.0),
    BandRow(1.000, 10.0),
]

DEFAULT_BRAKE_TIME_ROWS: List[BandRow] = [
    BandRow(0.030, 15),
    BandRow(0.075, 18),
    BandRow(0.135, 22),
    BandRow(0.250, 30),
    BandRow(0.375, 40),
    BandRow(0.500, 55),
    BandRow(0.750, 75),
    BandRow(1.000, 95),
]

DEFAULT_GAUGE_ROWS: List[GaugeRow] = [
    GaugeRow(7, 0.1793, 0.1875, 0.1443),
    GaugeRow(8, 0.1644, 0.1719, 0.1285),
    GaugeRow(10, 0.1345, 0.1406, 0.1019),
    GaugeRow(11, 0.1196, 0.1250, 0.0907),
    GaugeRow(12, 0.1046, 0.1094, 0.0808),
    GaugeRow(14, 0.0747, 0.0781, 0.0641),
    GaugeRow(16, 0.0598, 0.0625, 0.0508),
    GaugeRow(18, 0.0478, 0.0500, 0.0403),
    GaugeRow(20, 0.0359, 0.0375, 0.0320),
    GaugeRow(22, 0.0299, 0.0313, 0.0253),
]

# Canonical gauge decimals used for cost banding (snap targets for mild steel)
CANONICAL_GAUGE_MILD: Dict[int, float] = {
    14: 0.075,  # use band key, not mill-tolerance 0.0747
    16: 0.060,
    18: 0.048,
    12: 0.105,
    10: 0.135,
}

DEFAULT_WELD_REF_ROWS: List[WeldRefRow] = [
    WeldRefRow(0.125, 18, 0.185),
    WeldRefRow(0.1875, 14, 0.238),
    WeldRefRow(0.250, 11, 0.303),
    WeldRefRow(0.375, 7, 0.476),
    WeldRefRow(0.500, 5, 0.667),
]

DEFAULT_RATES = FabRates(
    laser_rate=185.0,
    brake_rate=95.0,
    weld_rate=110.0,
    scrap_factor=0.20,
    laser_speed_fallback=100.0,
    pierce_time_fallback=1.0,
    target_margin=0.30,
    overhead_markup=0.15,
    buyout_markup=0.20,
    consumables_per_job=25.0,
)

DEFAULT_MATERIAL_PRICES: Dict[str, float] = {
    "A36 Mild Steel": 0.90,
    "304 Stainless": 2.30,
    "316 Stainless": 3.05,
    "5052 Aluminum": 2.10,
    "6061 Aluminum": 2.25,
    "1018 CD Bar": 1.25,
}
