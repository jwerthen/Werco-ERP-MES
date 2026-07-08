"""Estimate workbench service — DB-backed rates/tables + tree persist/recalc.

Phase 1 of docs/ESTIMATE_WORKBENCH.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models.estimate_workbench import (
    ConfidenceLevel,
    CutBendRow,
    CutBendTable,
    CutBendTableKind,
    QuoteAssembly,
    QuoteBuyoutLineItem,
    QuoteFabLineItem,
    QuoteMachinedLineItem,
)
from app.models.quote_config import LaborRate, QuoteMaterial, QuoteSettings
from app.models.rfq_quote import QuoteEstimate, RfqPackage
from app.services.fab_calc_engine import (
    DEFAULT_BRAKE_TIME_ROWS,
    DEFAULT_GAUGE_ROWS,
    DEFAULT_LASER_SPEED_ROWS,
    DEFAULT_PIERCE_TIME_ROWS,
    DEFAULT_RATES,
    DEFAULT_WELD_REF_ROWS,
    BandRow,
    FabLineBreakdown,
    FabLineInput,
    FabRates,
    GaugeRow,
    LaserSpeedRow,
    MaterialFamily,
    WeldRefRow,
    calc_bid_summary,
    calc_fab_line_item,
    calc_machined_part,
)
from app.services.audit_service import AuditService

# QuoteSettings keys for the estimate workbench (ew_*). Missing → Excel defaults.
SETTING_KEYS = {
    "laser_rate": "ew_laser_rate",
    "brake_rate": "ew_brake_rate",
    "weld_rate": "ew_weld_rate",
    "scrap_factor": "ew_scrap_factor",
    "laser_speed_fallback": "ew_laser_speed_fallback",
    "pierce_time_fallback": "ew_pierce_time_fallback",
    "target_margin": "ew_target_margin",
    "overhead_markup": "ew_overhead_markup",
    "buyout_markup": "ew_buyout_markup",
    "consumables_per_job": "ew_consumables_per_job",
    "assembly_labor_rate": "ew_assembly_labor_rate",
    "electrical_labor_rate": "ew_electrical_labor_rate",
    "cnc_turning_rate": "ew_cnc_turning_rate",
    "cnc_mill_rate": "ew_cnc_mill_rate",
}

# Legacy RFQ keys used as secondary fallbacks where they map cleanly
LEGACY_FALLBACKS = {
    "scrap_factor": "rfq_scrap_factor",
}


@dataclass
class ShopDataBundle:
    rates: FabRates
    assembly_labor_rate: float
    electrical_labor_rate: float
    cnc_turning_rate: float
    cnc_mill_rate: float
    cut_speed_table: List[LaserSpeedRow]
    pierce_time_table: List[BandRow]
    brake_time_table: List[BandRow]
    gauge_table: List[GaugeRow]
    weld_table: List[WeldRefRow]
    source: str  # "db" | "defaults" | "mixed"


def _get_setting_number(
    db: Session, company_id: int, key: str, default: float
) -> float:
    row = (
        db.query(QuoteSettings)
        .filter(
            QuoteSettings.company_id == company_id,
            QuoteSettings.setting_key == key,
        )
        .first()
    )
    if not row or row.setting_value is None:
        return default
    try:
        return float(row.setting_value)
    except (TypeError, ValueError):
        return default


def _labor_rate_by_name(db: Session, company_id: int, name_substr: str) -> Optional[float]:
    rows = (
        db.query(LaborRate)
        .filter(LaborRate.company_id == company_id, LaborRate.is_active.is_(True))
        .all()
    )
    needle = name_substr.lower()
    for row in rows:
        if needle in (row.name or "").lower():
            return float(row.rate_per_hour)
    return None


def load_rates(db: Session, company_id: int) -> Tuple[FabRates, float, float, float, float]:
    """Load FabRates + labor/CNC rates for a company."""

    def pick(field: str, default: float) -> float:
        primary = SETTING_KEYS[field]
        val = _get_setting_number(db, company_id, primary, default)
        if val == default and field in LEGACY_FALLBACKS:
            return _get_setting_number(db, company_id, LEGACY_FALLBACKS[field], default)
        return val

    rates = FabRates(
        laser_rate=pick("laser_rate", DEFAULT_RATES.laser_rate),
        brake_rate=pick("brake_rate", DEFAULT_RATES.brake_rate),
        weld_rate=pick("weld_rate", DEFAULT_RATES.weld_rate),
        scrap_factor=pick("scrap_factor", DEFAULT_RATES.scrap_factor),
        laser_speed_fallback=pick("laser_speed_fallback", DEFAULT_RATES.laser_speed_fallback),
        pierce_time_fallback=pick("pierce_time_fallback", DEFAULT_RATES.pierce_time_fallback),
        target_margin=pick("target_margin", DEFAULT_RATES.target_margin),
        overhead_markup=pick("overhead_markup", DEFAULT_RATES.overhead_markup),
        buyout_markup=pick("buyout_markup", DEFAULT_RATES.buyout_markup),
        consumables_per_job=pick("consumables_per_job", DEFAULT_RATES.consumables_per_job),
    )

    assembly = pick("assembly_labor_rate", 75.0)
    electrical = pick("electrical_labor_rate", 105.0)
    turning = pick("cnc_turning_rate", 95.0)
    mill = pick("cnc_mill_rate", 105.0)

    # Prefer LaborRate catalog when present
    for substr, setter in (
        ("assembl", "assembly"),
        ("weld", None),  # weld already on FabRates
        ("electric", "electrical"),
        ("turn", "turning"),
        ("mill", "mill"),
        ("cnc mill", "mill"),
        ("lathe", "turning"),
    ):
        found = _labor_rate_by_name(db, company_id, substr)
        if found is None:
            continue
        if setter == "assembly":
            assembly = found
        elif setter == "electrical":
            electrical = found
        elif setter == "turning":
            turning = found
        elif setter == "mill":
            mill = found
        elif setter is None:
            rates.weld_rate = found

    return rates, assembly, electrical, turning, mill


def seed_cut_bend_defaults(db: Session, company_id: int, *, force: bool = False) -> int:
    """Insert the five default Cut/Bend tables. Returns number of tables created."""
    existing = (
        db.query(CutBendTable)
        .filter(CutBendTable.company_id == company_id)
        .count()
    )
    if existing and not force:
        return 0
    if existing and force:
        db.query(CutBendRow).filter(CutBendRow.company_id == company_id).delete()
        db.query(CutBendTable).filter(CutBendTable.company_id == company_id).delete()
        db.flush()

    now = datetime.utcnow()
    created = 0

    laser = CutBendTable(
        company_id=company_id,
        kind=CutBendTableKind.LASER_SPEED.value,
        name="Laser Cut Speed",
        description="Cut speed (in/min) by thickness + material family",
        created_at=now,
        updated_at=now,
    )
    db.add(laser)
    db.flush()
    for i, row in enumerate(DEFAULT_LASER_SPEED_ROWS):
        db.add(
            CutBendRow(
                company_id=company_id,
                table_id=laser.id,
                sort_order=i,
                thickness_in=row.thickness,
                mild_steel=row.mild,
                stainless=row.stainless,
                aluminum=row.aluminum,
                created_at=now,
                updated_at=now,
            )
        )
    created += 1

    pierce = CutBendTable(
        company_id=company_id,
        kind=CutBendTableKind.PIERCE_TIME.value,
        name="Pierce Time",
        description="Pierce time (sec each) by thickness",
        created_at=now,
        updated_at=now,
    )
    db.add(pierce)
    db.flush()
    for i, row in enumerate(DEFAULT_PIERCE_TIME_ROWS):
        db.add(
            CutBendRow(
                company_id=company_id,
                table_id=pierce.id,
                sort_order=i,
                thickness_in=row.thickness,
                value=row.value,
                created_at=now,
                updated_at=now,
            )
        )
    created += 1

    brake = CutBendTable(
        company_id=company_id,
        kind=CutBendTableKind.BRAKE_TIME.value,
        name="Press Brake Time",
        description="Brake time (sec/bend) by thickness",
        created_at=now,
        updated_at=now,
    )
    db.add(brake)
    db.flush()
    for i, row in enumerate(DEFAULT_BRAKE_TIME_ROWS):
        db.add(
            CutBendRow(
                company_id=company_id,
                table_id=brake.id,
                sort_order=i,
                thickness_in=row.thickness,
                value=row.value,
                created_at=now,
                updated_at=now,
            )
        )
    created += 1

    gauge = CutBendTable(
        company_id=company_id,
        kind=CutBendTableKind.GAUGE_REFERENCE.value,
        name="Gauge → Decimal Thickness",
        description="Canonical gauge decimals by material family",
        created_at=now,
        updated_at=now,
    )
    db.add(gauge)
    db.flush()
    for i, row in enumerate(DEFAULT_GAUGE_ROWS):
        db.add(
            CutBendRow(
                company_id=company_id,
                table_id=gauge.id,
                sort_order=i,
                gauge=row.gauge,
                mild_steel=row.mild,
                stainless=row.stainless,
                aluminum=row.aluminum,
                created_at=now,
                updated_at=now,
            )
        )
    created += 1

    weld = CutBendTable(
        company_id=company_id,
        kind=CutBendTableKind.WELD_REFERENCE.value,
        name="Weld Estimating Reference",
        description="Fillet leg → arc in/min and min/in (includes 0.30 OF)",
        created_at=now,
        updated_at=now,
    )
    db.add(weld)
    db.flush()
    for i, row in enumerate(DEFAULT_WELD_REF_ROWS):
        db.add(
            CutBendRow(
                company_id=company_id,
                table_id=weld.id,
                sort_order=i,
                fillet_leg_in=row.fillet_leg_in,
                arc_in_per_min=row.arc_in_per_min,
                min_per_in=row.min_per_in,
                created_at=now,
                updated_at=now,
            )
        )
    created += 1

    db.flush()
    return created


def ensure_cut_bend_seeded(db: Session, company_id: int) -> bool:
    """Seed default Cut/Bend tables if company has none. Returns True if seeded."""
    count = (
        db.query(CutBendTable)
        .filter(CutBendTable.company_id == company_id)
        .count()
    )
    if count > 0:
        return False
    seed_cut_bend_defaults(db, company_id, force=False)
    return True


def _rows_for_kind(
    db: Session, company_id: int, kind: CutBendTableKind
) -> List[CutBendRow]:
    table = (
        db.query(CutBendTable)
        .options(joinedload(CutBendTable.rows))
        .filter(
            CutBendTable.company_id == company_id,
            CutBendTable.kind == kind.value,
        )
        .first()
    )
    if not table:
        return []
    return sorted(table.rows, key=lambda r: r.sort_order)


def load_shop_data(db: Session, company_id: int) -> ShopDataBundle:
    """Load rates + all five Cut/Bend tables; fall back to Excel defaults per table."""
    ensure_cut_bend_seeded(db, company_id)
    rates, assembly, electrical, turning, mill = load_rates(db, company_id)

    sources: List[str] = []

    laser_rows = _rows_for_kind(db, company_id, CutBendTableKind.LASER_SPEED)
    if laser_rows:
        cut_speed = [
            LaserSpeedRow(
                thickness=float(r.thickness_in or 0),
                mild=r.mild_steel,
                stainless=r.stainless,
                aluminum=r.aluminum,
            )
            for r in laser_rows
            if r.thickness_in is not None
        ]
        sources.append("db")
    else:
        cut_speed = list(DEFAULT_LASER_SPEED_ROWS)
        sources.append("defaults")

    pierce_rows = _rows_for_kind(db, company_id, CutBendTableKind.PIERCE_TIME)
    if pierce_rows:
        pierce = [
            BandRow(thickness=float(r.thickness_in or 0), value=float(r.value or 0))
            for r in pierce_rows
            if r.thickness_in is not None
        ]
        sources.append("db")
    else:
        pierce = list(DEFAULT_PIERCE_TIME_ROWS)
        sources.append("defaults")

    brake_rows = _rows_for_kind(db, company_id, CutBendTableKind.BRAKE_TIME)
    if brake_rows:
        brake = [
            BandRow(thickness=float(r.thickness_in or 0), value=float(r.value or 0))
            for r in brake_rows
            if r.thickness_in is not None
        ]
        sources.append("db")
    else:
        brake = list(DEFAULT_BRAKE_TIME_ROWS)
        sources.append("defaults")

    gauge_rows = _rows_for_kind(db, company_id, CutBendTableKind.GAUGE_REFERENCE)
    if gauge_rows:
        gauge = [
            GaugeRow(
                gauge=int(r.gauge or 0),
                mild=float(r.mild_steel or 0),
                stainless=float(r.stainless or 0),
                aluminum=float(r.aluminum or 0),
            )
            for r in gauge_rows
            if r.gauge is not None
        ]
        sources.append("db")
    else:
        gauge = list(DEFAULT_GAUGE_ROWS)
        sources.append("defaults")

    weld_rows = _rows_for_kind(db, company_id, CutBendTableKind.WELD_REFERENCE)
    if weld_rows:
        weld = [
            WeldRefRow(
                fillet_leg_in=float(r.fillet_leg_in or 0),
                arc_in_per_min=float(r.arc_in_per_min or 0),
                min_per_in=float(r.min_per_in or 0),
            )
            for r in weld_rows
            if r.fillet_leg_in is not None
        ]
        sources.append("db")
    else:
        weld = list(DEFAULT_WELD_REF_ROWS)
        sources.append("defaults")

    if all(s == "db" for s in sources):
        source = "db"
    elif all(s == "defaults" for s in sources):
        source = "defaults"
    else:
        source = "mixed"

    return ShopDataBundle(
        rates=rates,
        assembly_labor_rate=assembly,
        electrical_labor_rate=electrical,
        cnc_turning_rate=turning,
        cnc_mill_rate=mill,
        cut_speed_table=cut_speed,
        pierce_time_table=pierce,
        brake_time_table=brake,
        gauge_table=gauge,
        weld_table=weld,
        source=source,
    )


def resolve_material_price(
    db: Session, company_id: int, material_name: str, fallback: float = 0.0
) -> Tuple[float, float]:
    """Return (price_per_lb, density) from QuoteMaterial catalog, or fallback."""
    if not material_name:
        return fallback, 0.284
    rows = (
        db.query(QuoteMaterial)
        .filter(QuoteMaterial.company_id == company_id, QuoteMaterial.is_active.is_(True))
        .all()
    )
    needle = material_name.strip().lower()
    best = None
    for row in rows:
        name = (row.name or "").lower()
        if needle == name or needle in name or name in needle:
            best = row
            break
    if not best:
        return fallback, 0.284
    price = float(best.stock_price_per_pound or 0) or fallback
    density = float(best.density_lb_per_cubic_inch or 0) or 0.284
    return price, density


def _parse_family(value: Optional[str]) -> Optional[MaterialFamily]:
    if not value:
        return None
    try:
        return MaterialFamily(value.lower())
    except ValueError:
        return None


def fab_input_from_line(
    line: QuoteFabLineItem,
    price_per_lb: float,
    density: float,
) -> FabLineInput:
    return FabLineInput(
        material=line.material or "",
        qty=int(line.qty or 1),
        thickness_in=line.thickness_in,
        width_in=line.width_in,
        length_in=line.length_in,
        cut_length_in=line.cut_length_in,
        pierce_count=int(line.pierce_count or 0),
        bend_count=int(line.bend_count or 0),
        weld_length_in=line.weld_length_in,
        weld_minutes_ea=line.weld_minutes_ea,
        material_family_override=_parse_family(line.material_family_override),
        include_material=bool(line.include_material),
        include_laser=bool(line.include_laser),
        include_brake=bool(line.include_brake),
        include_weld=bool(line.include_weld),
        price_per_lb=price_per_lb,
        density_lb_per_in3=density,
    )


def apply_breakdown_to_fab(line: QuoteFabLineItem, bd: FabLineBreakdown) -> None:
    line.weight_ea_lb = bd.weight_ea_lb
    line.material_cost = bd.material_cost
    line.laser_cost = bd.laser_cost
    line.laser_hours = bd.laser_hours
    line.brake_cost = bd.brake_cost
    line.brake_hours = bd.brake_hours
    line.weld_cost = bd.weld_cost
    line.weld_hours = bd.weld_hours
    line.line_total = bd.line_total
    line.calc_warnings = [
        {
            "code": w.code,
            "message": w.message,
            "field": w.field,
            "suggested_value": w.suggested_value,
        }
        for w in bd.warnings
    ] or None
    line.calc_errors = [
        {
            "code": e.code.value if hasattr(e.code, "value") else str(e.code),
            "message": e.message,
            "field": e.field,
        }
        for e in bd.errors
    ] or None


def recompute_estimate(
    db: Session,
    estimate: QuoteEstimate,
    shop: ShopDataBundle,
) -> Dict[str, Any]:
    """Recompute all cached costs on an estimate tree; update estimate totals."""
    fab_breakdowns: List[FabLineBreakdown] = []
    buyout_extended = 0.0
    assembly_hrs = 0.0
    electrical_hrs = 0.0
    machined_subtotal = 0.0

    for asm in estimate.assemblies or []:
        if getattr(asm, "is_deleted", False):
            continue
        assembly_hrs += float(asm.assembly_labor_hrs or 0)
        electrical_hrs += float(asm.electrical_labor_hrs or 0)
        for bl in asm.buyout_line_items or []:
            if getattr(bl, "is_deleted", False):
                continue
            bl.extended_cost = float(bl.qty or 0) * float(bl.unit_cost or 0)
            buyout_extended += bl.extended_cost
        for fl in asm.fab_line_items or []:
            if getattr(fl, "is_deleted", False):
                continue
            price, density = resolve_material_price(
                db, estimate.company_id, fl.material or "", fallback=0.0
            )
            # Keep explicit zero if catalog miss and no price stored — don't invent
            inp = fab_input_from_line(fl, price, density)
            bd = calc_fab_line_item(
                inp,
                shop.rates,
                shop.cut_speed_table,
                shop.pierce_time_table,
                shop.brake_time_table,
                shop.gauge_table,
                shop.weld_table,
            )
            apply_breakdown_to_fab(fl, bd)
            fab_breakdowns.append(bd)

    for mp in estimate.machined_line_items or []:
        if getattr(mp, "is_deleted", False):
            continue
        price, density = resolve_material_price(
            db, estimate.company_id, mp.material or "", fallback=0.0
        )
        result = calc_machined_part(
            stock_dia_in=float(mp.stock_dia_in or 0),
            stock_length_in=float(mp.stock_length_in or 0),
            qty=int(mp.qty or 1),
            price_per_lb=price,
            scrap_factor=shop.rates.scrap_factor,
            turning_minutes=float(mp.turning_minutes or 0),
            milling_minutes=float(mp.milling_minutes or 0),
            cnc_turning_rate=shop.cnc_turning_rate,
            cnc_mill_rate=shop.cnc_mill_rate,
            density_lb_per_in3=density,
        )
        mp.weight_ea_lb = result["weight_ea_lb"]
        mp.material_cost = result["material_cost"]
        mp.turning_cost = result["turning_cost"]
        mp.turning_hours = result["turning_hours"]
        mp.milling_cost = result["milling_cost"]
        mp.milling_hours = result["milling_hours"]
        mp.line_total = result["line_total"]
        machined_subtotal += result["line_total"]

    summary = calc_bid_summary(
        fab_lines=fab_breakdowns,
        buyout_extended_total=buyout_extended,
        assembly_labor_hrs=assembly_hrs,
        electrical_labor_hrs=electrical_hrs,
        assembly_labor_rate=shop.assembly_labor_rate,
        electrical_labor_rate=shop.electrical_labor_rate,
        machined_subtotal=machined_subtotal,
        rates=shop.rates,
    )

    estimate.material_total = summary.fab_material + summary.machined_subtotal
    estimate.hardware_consumables_total = summary.buyout_marked_up + summary.consumables
    estimate.shop_labor_oh_total = (
        summary.fab_laser
        + summary.fab_brake
        + summary.fab_weld
        + summary.assembly_labor_cost
        + summary.electrical_labor_cost
        + summary.overhead
    )
    estimate.margin_total = summary.sell_price - summary.cogs
    estimate.grand_total = summary.sell_price
    estimate.internal_breakdown = {
        "shop_data_source": shop.source,
        "fab_material": summary.fab_material,
        "fab_laser": summary.fab_laser,
        "fab_brake": summary.fab_brake,
        "fab_weld": summary.fab_weld,
        "buyout_subtotal": summary.buyout_subtotal,
        "buyout_marked_up": summary.buyout_marked_up,
        "assembly_labor_cost": summary.assembly_labor_cost,
        "electrical_labor_cost": summary.electrical_labor_cost,
        "machined_subtotal": summary.machined_subtotal,
        "overhead": summary.overhead,
        "consumables": summary.consumables,
        "cogs": summary.cogs,
        "sell_price": summary.sell_price,
        "target_margin": summary.target_margin,
        "laser_hours": summary.laser_hours,
        "brake_hours": summary.brake_hours,
        "weld_hours": summary.weld_hours,
        "assembly_hours": summary.assembly_hours,
        "electrical_hours": summary.electrical_hours,
    }
    return estimate.internal_breakdown


def get_estimate_tree(db: Session, estimate_id: int, company_id: int) -> Optional[QuoteEstimate]:
    return (
        db.query(QuoteEstimate)
        .options(
            joinedload(QuoteEstimate.assemblies)
            .joinedload(QuoteAssembly.fab_line_items),
            joinedload(QuoteEstimate.assemblies)
            .joinedload(QuoteAssembly.buyout_line_items),
            joinedload(QuoteEstimate.machined_line_items),
        )
        .filter(
            QuoteEstimate.id == estimate_id,
            QuoteEstimate.company_id == company_id,
        )
        .first()
    )


def create_blank_estimate(
    db: Session,
    *,
    rfq_package_id: int,
    company_id: int,
    user_id: Optional[int],
    audit: Optional[AuditService] = None,
) -> QuoteEstimate:
    pkg = (
        db.query(RfqPackage)
        .filter(RfqPackage.id == rfq_package_id, RfqPackage.company_id == company_id)
        .first()
    )
    if not pkg:
        raise ValueError("RFQ package not found")

    estimate = QuoteEstimate(
        rfq_package_id=rfq_package_id,
        version=1,
        currency="USD",
        created_by=user_id,
        company_id=company_id,
        assumptions=[{"source": "estimate_workbench", "note": "Manual workbench estimate"}],
    )
    db.add(estimate)
    db.flush()

    asm = QuoteAssembly(
        quote_estimate_id=estimate.id,
        name="Assembly 1",
        sort_order=0,
        assembly_labor_hrs=0.0,
        electrical_labor_hrs=0.0,
        company_id=company_id,
    )
    db.add(asm)
    db.flush()

    if audit:
        audit.log_create(
            "quote_estimate",
            estimate.id,
            f"EW-{estimate.id}",
            new_values={"rfq_package_id": rfq_package_id, "source": "estimate_workbench"},
        )

    shop = load_shop_data(db, company_id)
    # Refresh relationships for recompute
    estimate.assemblies = [asm]
    estimate.machined_line_items = []
    recompute_estimate(db, estimate, shop)
    db.commit()
    db.refresh(estimate)
    return get_estimate_tree(db, estimate.id, company_id)  # type: ignore[return-value]


class StaleVersionError(Exception):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"Estimate changed (current version {current_version})")


def save_estimate_tree(
    db: Session,
    estimate: QuoteEstimate,
    payload: Dict[str, Any],
    *,
    expected_version: int,
    company_id: int,
    user_id: Optional[int] = None,
    audit: Optional[AuditService] = None,
) -> QuoteEstimate:
    """Replace workbench children from payload; optimistic lock on estimate.version."""
    current_version = int(estimate.version or 1)
    if expected_version != current_version:
        raise StaleVersionError(current_version)

    now = datetime.utcnow()
    shop = load_shop_data(db, company_id)

    # Soft-delete existing children, then recreate from payload (simple Phase 1 replace)
    for asm in list(estimate.assemblies or []):
        for fl in list(asm.fab_line_items or []):
            fl.soft_delete(user_id)
        for bl in list(asm.buyout_line_items or []):
            bl.soft_delete(user_id)
        asm.soft_delete(user_id)
    for mp in list(estimate.machined_line_items or []):
        mp.soft_delete(user_id)
    db.flush()

    new_assemblies: List[QuoteAssembly] = []
    for i, asm_data in enumerate(payload.get("assemblies") or []):
        asm = QuoteAssembly(
            quote_estimate_id=estimate.id,
            name=asm_data.get("name") or f"Assembly {i + 1}",
            sort_order=asm_data.get("sort_order", i),
            assembly_labor_hrs=float(asm_data.get("assembly_labor_hrs") or 0),
            electrical_labor_hrs=float(asm_data.get("electrical_labor_hrs") or 0),
            notes=asm_data.get("notes"),
            company_id=company_id,
            created_at=now,
        )
        db.add(asm)
        db.flush()

        for j, fl_data in enumerate(asm_data.get("fab_lines") or []):
            fl = QuoteFabLineItem(
                assembly_id=asm.id,
                sort_order=fl_data.get("sort_order", j),
                part_number=fl_data.get("part_number"),
                detail_name=fl_data.get("detail_name") or f"Detail {j + 1}",
                material=fl_data.get("material") or "",
                material_family_override=fl_data.get("material_family_override"),
                qty=int(fl_data.get("qty") or 1),
                thickness_in=fl_data.get("thickness_in"),
                width_in=fl_data.get("width_in"),
                length_in=fl_data.get("length_in"),
                cut_length_in=fl_data.get("cut_length_in"),
                pierce_count=int(fl_data.get("pierce_count") or 0),
                bend_count=int(fl_data.get("bend_count") or 0),
                weld_length_in=fl_data.get("weld_length_in"),
                weld_minutes_ea=fl_data.get("weld_minutes_ea"),
                include_material=bool(fl_data.get("include_material", True)),
                include_laser=bool(fl_data.get("include_laser", True)),
                include_brake=bool(fl_data.get("include_brake", True)),
                include_weld=bool(fl_data.get("include_weld", True)),
                confidence=fl_data.get("confidence") or ConfidenceLevel.REVIEW.value,
                verification_note=fl_data.get("verification_note"),
                company_id=company_id,
                created_at=now,
            )
            db.add(fl)

        for j, bl_data in enumerate(asm_data.get("buyout_lines") or []):
            qty = float(bl_data.get("qty") or 0)
            unit = float(bl_data.get("unit_cost") or 0)
            bl = QuoteBuyoutLineItem(
                assembly_id=asm.id,
                sort_order=bl_data.get("sort_order", j),
                category=bl_data.get("category"),
                vendor=bl_data.get("vendor"),
                part_number=bl_data.get("part_number"),
                part_id=bl_data.get("part_id"),
                description=bl_data.get("description") or "",
                qty=qty,
                unit_cost=unit,
                extended_cost=qty * unit,
                price_source=bl_data.get("price_source"),
                confidence=bl_data.get("confidence") or ConfidenceLevel.REVIEW.value,
                verification_note=bl_data.get("verification_note"),
                company_id=company_id,
                created_at=now,
            )
            db.add(bl)

        new_assemblies.append(asm)

    new_machined: List[QuoteMachinedLineItem] = []
    for i, mp_data in enumerate(payload.get("machined_parts") or []):
        mp = QuoteMachinedLineItem(
            quote_estimate_id=estimate.id,
            sort_order=mp_data.get("sort_order", i),
            part_number=mp_data.get("part_number"),
            description=mp_data.get("description") or f"Machined {i + 1}",
            material=mp_data.get("material") or "",
            qty=int(mp_data.get("qty") or 1),
            stock_dia_in=mp_data.get("stock_dia_in"),
            stock_length_in=mp_data.get("stock_length_in"),
            turning_minutes=float(mp_data.get("turning_minutes") or 0),
            milling_minutes=float(mp_data.get("milling_minutes") or 0),
            confidence=mp_data.get("confidence") or ConfidenceLevel.REVIEW.value,
            verification_note=mp_data.get("verification_note"),
            company_id=company_id,
            created_at=now,
        )
        db.add(mp)
        new_machined.append(mp)

    db.flush()
    estimate.assemblies = new_assemblies
    estimate.machined_line_items = new_machined
    recompute_estimate(db, estimate, shop)
    estimate.version = current_version + 1

    if audit:
        audit.log_update(
            "quote_estimate",
            estimate.id,
            f"EW-{estimate.id}",
            old_values={"version": current_version},
            new_values={
                "version": estimate.version,
                "grand_total": estimate.grand_total,
                "assembly_count": len(new_assemblies),
            },
        )

    db.commit()
    return get_estimate_tree(db, estimate.id, company_id)  # type: ignore[return-value]


def recalc_payload(
    db: Session,
    company_id: int,
    assemblies: Sequence[Any],
    machined_parts: Sequence[Any],
    rates_override: Optional[Any] = None,
) -> Dict[str, Any]:
    """Stateless recalc using DB shop data (+ optional rate overrides)."""
    shop = load_shop_data(db, company_id)
    rates = shop.rates
    assembly_rate = shop.assembly_labor_rate
    electrical_rate = shop.electrical_labor_rate
    turning_rate = shop.cnc_turning_rate
    mill_rate = shop.cnc_mill_rate

    if rates_override is not None:
        for field in (
            "laser_rate",
            "brake_rate",
            "weld_rate",
            "scrap_factor",
            "laser_speed_fallback",
            "pierce_time_fallback",
            "target_margin",
            "overhead_markup",
            "buyout_markup",
            "consumables_per_job",
        ):
            val = getattr(rates_override, field, None)
            if val is not None:
                setattr(rates, field, val)
        if getattr(rates_override, "assembly_labor_rate", None) is not None:
            assembly_rate = rates_override.assembly_labor_rate
        if getattr(rates_override, "electrical_labor_rate", None) is not None:
            electrical_rate = rates_override.electrical_labor_rate
        if getattr(rates_override, "cnc_turning_rate", None) is not None:
            turning_rate = rates_override.cnc_turning_rate
        if getattr(rates_override, "cnc_mill_rate", None) is not None:
            mill_rate = rates_override.cnc_mill_rate

    fab_breakdowns: List[FabLineBreakdown] = []
    fab_outs: List[Dict[str, Any]] = []
    buyout_extended = 0.0
    assembly_hrs = 0.0
    electrical_hrs = 0.0

    for asm in assemblies:
        assembly_hrs += float(getattr(asm, "assembly_labor_hrs", 0) or 0)
        electrical_hrs += float(getattr(asm, "electrical_labor_hrs", 0) or 0)
        for bl in getattr(asm, "buyout_lines", None) or []:
            buyout_extended += float(getattr(bl, "qty", 0) or 0) * float(
                getattr(bl, "unit_cost", 0) or 0
            )
        for fl in getattr(asm, "fab_lines", None) or []:
            material = getattr(fl, "material", "") or ""
            price = float(getattr(fl, "price_per_lb", 0) or 0)
            density = float(getattr(fl, "density_lb_per_in3", 0) or 0) or 0.284
            if price <= 0 and material:
                price, density = resolve_material_price(db, company_id, material, fallback=0.0)
            inp = FabLineInput(
                material=material,
                qty=int(getattr(fl, "qty", 1) or 1),
                thickness_in=getattr(fl, "thickness_in", None),
                width_in=getattr(fl, "width_in", None),
                length_in=getattr(fl, "length_in", None),
                cut_length_in=getattr(fl, "cut_length_in", None),
                pierce_count=int(getattr(fl, "pierce_count", 0) or 0),
                bend_count=int(getattr(fl, "bend_count", 0) or 0),
                weld_length_in=getattr(fl, "weld_length_in", None),
                weld_minutes_ea=getattr(fl, "weld_minutes_ea", None),
                material_family_override=_parse_family(
                    getattr(fl, "material_family_override", None)
                ),
                include_material=bool(getattr(fl, "include_material", True)),
                include_laser=bool(getattr(fl, "include_laser", True)),
                include_brake=bool(getattr(fl, "include_brake", True)),
                include_weld=bool(getattr(fl, "include_weld", True)),
                price_per_lb=price,
                density_lb_per_in3=density,
            )
            bd = calc_fab_line_item(
                inp,
                rates,
                shop.cut_speed_table,
                shop.pierce_time_table,
                shop.brake_time_table,
                shop.gauge_table,
                shop.weld_table,
            )
            fab_breakdowns.append(bd)
            fab_outs.append(
                {
                    "detail_name": getattr(fl, "detail_name", None),
                    "part_number": getattr(fl, "part_number", None),
                    "breakdown": bd,
                }
            )

    machined_outs: List[Dict[str, Any]] = []
    machined_subtotal = 0.0
    for mp in machined_parts:
        material = getattr(mp, "material", "") or ""
        price = float(getattr(mp, "price_per_lb", 0) or 0)
        density = float(getattr(mp, "density_lb_per_in3", 0) or 0) or 0.284
        if price <= 0 and material:
            price, density = resolve_material_price(db, company_id, material, fallback=0.0)
        result = calc_machined_part(
            stock_dia_in=float(getattr(mp, "stock_dia_in", 0) or 0),
            stock_length_in=float(getattr(mp, "stock_length_in", 0) or 0),
            qty=int(getattr(mp, "qty", 1) or 1),
            price_per_lb=price,
            scrap_factor=rates.scrap_factor,
            turning_minutes=float(getattr(mp, "turning_minutes", 0) or 0),
            milling_minutes=float(getattr(mp, "milling_minutes", 0) or 0),
            cnc_turning_rate=turning_rate,
            cnc_mill_rate=mill_rate,
            density_lb_per_in3=density,
        )
        machined_subtotal += result["line_total"]
        machined_outs.append(
            {
                "description": getattr(mp, "description", None),
                **result,
            }
        )

    summary = calc_bid_summary(
        fab_lines=fab_breakdowns,
        buyout_extended_total=buyout_extended,
        assembly_labor_hrs=assembly_hrs,
        electrical_labor_hrs=electrical_hrs,
        assembly_labor_rate=assembly_rate,
        electrical_labor_rate=electrical_rate,
        machined_subtotal=machined_subtotal,
        rates=rates,
    )
    return {
        "shop_data_source": shop.source,
        "fab_outs": fab_outs,
        "machined_outs": machined_outs,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Phase 3 — Verification + finalize gate
# ---------------------------------------------------------------------------


class FinalizeBlockedError(Exception):
    """Raised when finalize is refused due to outstanding Review / calc errors."""

    def __init__(self, blockers: List[Dict[str, Any]], message: str = "Cannot finalize"):
        self.blockers = blockers
        self.message = message
        super().__init__(message)


def _norm_confidence(value: Optional[str]) -> str:
    raw = (value or ConfidenceLevel.REVIEW.value).strip().lower()
    if raw in (
        ConfidenceLevel.CONFIRMED.value,
        ConfidenceLevel.MAJORITY.value,
        ConfidenceLevel.REVIEW.value,
    ):
        return raw
    return ConfidenceLevel.REVIEW.value


def _active_assemblies(estimate: QuoteEstimate) -> List[QuoteAssembly]:
    return [a for a in (estimate.assemblies or []) if not getattr(a, "is_deleted", False)]


def _active_fab(asm: QuoteAssembly) -> List[QuoteFabLineItem]:
    return [f for f in (asm.fab_line_items or []) if not getattr(f, "is_deleted", False)]


def _active_buyout(asm: QuoteAssembly) -> List[QuoteBuyoutLineItem]:
    return [b for b in (asm.buyout_line_items or []) if not getattr(b, "is_deleted", False)]


def _active_machined(estimate: QuoteEstimate) -> List[QuoteMachinedLineItem]:
    return [m for m in (estimate.machined_line_items or []) if not getattr(m, "is_deleted", False)]


def build_verification_report(estimate: QuoteEstimate) -> Dict[str, Any]:
    """Bid Verification summary + Priority Action Items for an estimate tree."""
    categories = {
        "fab": {"label": "Fab", "total": 0.0, "count": 0, "confirmed": 0, "majority": 0, "review": 0},
        "buyout": {
            "label": "Buyout",
            "total": 0.0,
            "count": 0,
            "confirmed": 0,
            "majority": 0,
            "review": 0,
        },
        "machined": {
            "label": "Machined",
            "total": 0.0,
            "count": 0,
            "confirmed": 0,
            "majority": 0,
            "review": 0,
        },
        "labor": {
            "label": "Labor",
            "total": 0.0,
            "count": 0,
            "confirmed": 0,
            "majority": 0,
            "review": 0,
        },
    }
    priority_actions: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []

    def bump(cat: str, confidence: str, amount: float) -> None:
        categories[cat]["count"] += 1
        categories[cat]["total"] += float(amount or 0)
        categories[cat][confidence] = categories[cat].get(confidence, 0) + 1

    for asm in _active_assemblies(estimate):
        for fl in _active_fab(asm):
            conf = _norm_confidence(fl.confidence)
            bump("fab", conf, fl.line_total)
            label = fl.detail_name or fl.part_number or f"Fab #{fl.id}"
            anchor = f"fab-{fl.id}"
            if conf == ConfidenceLevel.REVIEW.value:
                reason = (fl.verification_note or "").strip() or "Marked Review — needs human verification"
                action = {
                    "category": "fab",
                    "line_id": fl.id,
                    "assembly_id": asm.id,
                    "assembly_name": asm.name,
                    "label": label,
                    "confidence": conf,
                    "reason": reason,
                    "anchor": anchor,
                    "line_total": float(fl.line_total or 0),
                }
                priority_actions.append(action)
                blockers.append({**action, "blocker_type": "review"})
            if fl.calc_errors:
                for err in fl.calc_errors:
                    blockers.append(
                        {
                            "category": "fab",
                            "line_id": fl.id,
                            "assembly_id": asm.id,
                            "assembly_name": asm.name,
                            "label": label,
                            "confidence": conf,
                            "reason": err.get("message") if isinstance(err, dict) else str(err),
                            "anchor": anchor,
                            "line_total": float(fl.line_total or 0),
                            "blocker_type": "calc_error",
                        }
                    )
                    if conf != ConfidenceLevel.REVIEW.value:
                        priority_actions.append(
                            {
                                "category": "fab",
                                "line_id": fl.id,
                                "assembly_id": asm.id,
                                "assembly_name": asm.name,
                                "label": label,
                                "confidence": ConfidenceLevel.REVIEW.value,
                                "reason": err.get("message") if isinstance(err, dict) else str(err),
                                "anchor": anchor,
                                "line_total": float(fl.line_total or 0),
                            }
                        )

        for bl in _active_buyout(asm):
            conf = _norm_confidence(bl.confidence)
            bump("buyout", conf, bl.extended_cost)
            label = bl.description or bl.part_number or f"Buyout #{bl.id}"
            anchor = f"buyout-{bl.id}"
            note = (bl.verification_note or bl.price_source or "").strip()
            if conf == ConfidenceLevel.REVIEW.value:
                reason = note or "Buyout marked Review — add price source / verification note"
                action = {
                    "category": "buyout",
                    "line_id": bl.id,
                    "assembly_id": asm.id,
                    "assembly_name": asm.name,
                    "label": label,
                    "confidence": conf,
                    "reason": reason,
                    "anchor": anchor,
                    "line_total": float(bl.extended_cost or 0),
                }
                priority_actions.append(action)
                blockers.append({**action, "blocker_type": "review"})
                if not note:
                    blockers.append({**action, "blocker_type": "missing_note"})

    # Labor roll-up from bid breakdown (flat hours, treated as confirmed)
    breakdown = estimate.internal_breakdown or {}
    labor_cost = float(breakdown.get("assembly_labor_cost") or 0) + float(
        breakdown.get("electrical_labor_cost") or 0
    )
    labor_hrs = float(breakdown.get("assembly_hours") or 0) + float(
        breakdown.get("electrical_hours") or 0
    )
    if labor_hrs > 0 or labor_cost > 0:
        categories["labor"]["count"] = 1
        categories["labor"]["total"] = labor_cost
        categories["labor"]["confirmed"] = 1

    for mp in _active_machined(estimate):
        conf = _norm_confidence(mp.confidence)
        bump("machined", conf, mp.line_total)
        label = mp.description or mp.part_number or f"Machined #{mp.id}"
        anchor = f"machined-{mp.id}"
        if conf == ConfidenceLevel.REVIEW.value:
            reason = (mp.verification_note or "").strip() or "Marked Review — needs human verification"
            action = {
                "category": "machined",
                "line_id": mp.id,
                "assembly_id": None,
                "assembly_name": None,
                "label": label,
                "confidence": conf,
                "reason": reason,
                "anchor": anchor,
                "line_total": float(mp.line_total or 0),
            }
            priority_actions.append(action)
            blockers.append({**action, "blocker_type": "review"})

    # Deduplicate priority actions by (category, line_id, reason)
    seen = set()
    unique_actions: List[Dict[str, Any]] = []
    for a in priority_actions:
        key = (a.get("category"), a.get("line_id"), a.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        unique_actions.append(a)

    can_finalize = len(blockers) == 0
    # Also require at least one priced line so we don't finalize an empty shell
    has_lines = any(
        categories[c]["count"] > 0 for c in ("fab", "buyout", "machined")
    )
    if not has_lines:
        can_finalize = False
        blockers.append(
            {
                "category": "estimate",
                "line_id": 0,
                "assembly_id": None,
                "assembly_name": None,
                "label": "Estimate",
                "confidence": ConfidenceLevel.REVIEW.value,
                "reason": "Add at least one fab, buyout, or machined line before finalizing",
                "anchor": "section-fab",
                "line_total": 0.0,
                "blocker_type": "empty",
            }
        )
        unique_actions.append(
            {
                "category": "estimate",
                "line_id": 0,
                "assembly_id": None,
                "assembly_name": None,
                "label": "Estimate",
                "confidence": ConfidenceLevel.REVIEW.value,
                "reason": "Add at least one fab, buyout, or machined line before finalizing",
                "anchor": "section-fab",
                "line_total": 0.0,
            }
        )

    status = "ready_to_send" if can_finalize else ("needs_review" if unique_actions or blockers else "draft")

    return {
        "estimate_id": estimate.id,
        "status": status,
        "can_finalize": can_finalize,
        "review_count": len(unique_actions),
        "blocker_count": len(blockers),
        "categories": list(categories.values()),
        "priority_actions": unique_actions,
        "blockers": blockers,
        "banner": (
            None
            if can_finalize
            else f"{len(unique_actions)} item{'s' if len(unique_actions) != 1 else ''} need review before this bid can be finalized"
        ),
    }


def finalize_estimate(
    db: Session,
    estimate: QuoteEstimate,
    *,
    company_id: int,
    user_id: Optional[int],
    valid_days: int = 30,
    force: bool = False,
    audit: Optional[AuditService] = None,
) -> Dict[str, Any]:
    """Gate on Review/calc errors, freeze rate snapshot, create/update customer Quote.

    ``force=True`` bypasses the gate (admin escape hatch) but still records blockers
    in the audit note — not the default path.
    """
    from datetime import date, timedelta

    from app.models.quote import Quote, QuoteLine, QuoteStatus

    report = build_verification_report(estimate)
    if not report["can_finalize"] and not force:
        raise FinalizeBlockedError(
            blockers=report["blockers"],
            message=report["banner"] or "Cannot finalize while Review items remain",
        )

    shop = load_shop_data(db, company_id)
    recompute_estimate(db, estimate, shop)

    pkg = (
        db.query(RfqPackage)
        .filter(RfqPackage.id == estimate.rfq_package_id, RfqPackage.company_id == company_id)
        .first()
    )
    customer_name = (pkg.customer_name if pkg else None) or "Unspecified Customer"

    # Generate quote number (same pattern as RFQ flow)
    today = datetime.utcnow().strftime("%Y%m")
    prefix = f"QTE-{today}-"
    from app.models.quote import Quote as QuoteModel

    last = (
        db.query(QuoteModel)
        .filter(QuoteModel.quote_number.like(f"{prefix}%"))
        .order_by(QuoteModel.quote_number.desc())
        .first()
    )
    next_num = 1
    if last:
        try:
            next_num = int(last.quote_number.split("-")[-1]) + 1
        except Exception:
            next_num = 1
    quote_number = f"{prefix}{next_num:04d}"

    breakdown = estimate.internal_breakdown or {}
    sell = float(breakdown.get("sell_price") or estimate.grand_total or 0)
    cogs = float(breakdown.get("cogs") or 0)

    was_new_quote = False
    if estimate.quote_id:
        quote = db.query(Quote).filter(Quote.id == estimate.quote_id, Quote.company_id == company_id).first()
        if not quote:
            estimate.quote_id = None

    if not estimate.quote_id:
        was_new_quote = True
        quote = Quote(
            quote_number=quote_number,
            customer_name=customer_name,
            status=QuoteStatus.DRAFT,
            quote_date=date.today(),
            valid_until=date.today() + timedelta(days=max(valid_days, 1)),
            subtotal=sell,
            tax=0.0,
            total=sell,
            notes=f"Finalized from Estimate Workbench EW-{estimate.id}"
            + (f" / RFQ {pkg.rfq_number}" if pkg else ""),
            internal_notes=(
                f"COGS={cogs:.2f}; shop_data={shop.source}; "
                f"review_cleared={report['can_finalize']}; force={force}"
            ),
            created_by=user_id,
            company_id=company_id,
        )
        db.add(quote)
        db.flush()
        estimate.quote_id = quote.id
    else:
        quote = db.query(Quote).filter(Quote.id == estimate.quote_id).first()
        assert quote is not None
        quote.customer_name = customer_name
        quote.subtotal = sell
        quote.total = sell
        quote.valid_until = date.today() + timedelta(days=max(valid_days, 1))
        quote.internal_notes = (
            f"COGS={cogs:.2f}; shop_data={shop.source}; "
            f"review_cleared={report['can_finalize']}; force={force}; "
            f"re-finalized EW-{estimate.id}"
        )
        # Soft-clear existing lines by deleting (QuoteLine has no soft-delete)
        for line in list(quote.lines or []):
            db.delete(line)
        db.flush()

    line_number = 1
    for asm in _active_assemblies(estimate):
        for fl in _active_fab(asm):
            labor_hrs = float(fl.laser_hours or 0) + float(fl.brake_hours or 0) + float(fl.weld_hours or 0)
            labor_cost = float(fl.laser_cost or 0) + float(fl.brake_cost or 0) + float(fl.weld_cost or 0)
            unit = float(fl.line_total or 0) / max(int(fl.qty or 1), 1)
            db.add(
                QuoteLine(
                    quote_id=quote.id,
                    line_number=line_number,
                    description=f"[{asm.name}] {fl.detail_name}"
                    + (f" ({fl.part_number})" if fl.part_number else ""),
                    quantity=float(fl.qty or 1),
                    unit_price=unit,
                    line_total=float(fl.line_total or 0),
                    material_cost=float(fl.material_cost or 0),
                    labor_hours=labor_hrs,
                    labor_cost=labor_cost,
                    company_id=company_id,
                )
            )
            line_number += 1
        for bl in _active_buyout(asm):
            unit = float(bl.unit_cost or 0)
            # Apply buyout markup into unit so customer line matches marked-up total share
            marked = unit * (1.0 + float(shop.rates.buyout_markup or 0))
            db.add(
                QuoteLine(
                    quote_id=quote.id,
                    line_number=line_number,
                    description=f"[{asm.name}] BUYOUT: {bl.description}",
                    quantity=float(bl.qty or 1),
                    unit_price=marked,
                    line_total=marked * float(bl.qty or 1),
                    material_cost=float(bl.extended_cost or 0),
                    company_id=company_id,
                )
            )
            line_number += 1

    for mp in _active_machined(estimate):
        unit = float(mp.line_total or 0) / max(int(mp.qty or 1), 1)
        db.add(
            QuoteLine(
                quote_id=quote.id,
                line_number=line_number,
                description=f"MACHINED: {mp.description}"
                + (f" ({mp.part_number})" if mp.part_number else ""),
                quantity=float(mp.qty or 1),
                unit_price=unit,
                line_total=float(mp.line_total or 0),
                material_cost=float(mp.material_cost or 0),
                labor_hours=float(mp.turning_hours or 0) + float(mp.milling_hours or 0),
                labor_cost=float(mp.turning_cost or 0) + float(mp.milling_cost or 0),
                company_id=company_id,
            )
        )
        line_number += 1

    # Freeze rate / table snapshot onto estimate
    estimate.internal_breakdown = {
        **(estimate.internal_breakdown or {}),
        "finalized_at": datetime.utcnow().isoformat() + "Z",
        "finalized_by": user_id,
        "shop_data_source": shop.source,
        "rate_snapshot": {
            "laser_rate": shop.rates.laser_rate,
            "brake_rate": shop.rates.brake_rate,
            "weld_rate": shop.rates.weld_rate,
            "scrap_factor": shop.rates.scrap_factor,
            "target_margin": shop.rates.target_margin,
            "overhead_markup": shop.rates.overhead_markup,
            "buyout_markup": shop.rates.buyout_markup,
            "consumables_per_job": shop.rates.consumables_per_job,
            "assembly_labor_rate": shop.assembly_labor_rate,
            "electrical_labor_rate": shop.electrical_labor_rate,
            "cnc_turning_rate": shop.cnc_turning_rate,
            "cnc_mill_rate": shop.cnc_mill_rate,
        },
        "verification": {
            "can_finalize": report["can_finalize"],
            "review_count": report["review_count"],
            "forced": force,
        },
    }
    estimate.confidence_detail = {
        "status": "finalized",
        "categories": report["categories"],
        "priority_actions_cleared": report["can_finalize"],
    }

    if pkg:
        pkg.status = "workbench_finalized"

    if audit:
        audit.log_update(
            "quote_estimate",
            estimate.id,
            f"EW-{estimate.id}",
            old_values={"status": "draft"},
            new_values={
                "status": "finalized",
                "quote_id": quote.id,
                "quote_number": quote.quote_number,
                "grand_total": sell,
                "forced": force,
            },
            description=f"Finalized estimate workbench → quote {quote.quote_number}",
        )
        if was_new_quote:
            audit.log_create(
                "quote",
                quote.id,
                quote.quote_number,
                new_values={"source": "estimate_workbench", "estimate_id": estimate.id, "total": sell},
            )

    db.commit()
    return {
        "estimate_id": estimate.id,
        "quote_id": quote.id,
        "quote_number": quote.quote_number,
        "grand_total": sell,
        "verification": report,
        "forced": force,
    }
