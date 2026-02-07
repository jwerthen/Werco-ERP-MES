"""Deterministic sheet-metal costing and lead-time estimation helpers."""

from dataclasses import dataclass
from math import ceil
from typing import Dict, Optional


DEFAULT_DENSITY_LB_PER_IN3: Dict[str, float] = {
    "carbon_steel": 0.284,
    "stainless": 0.289,
    "aluminum": 0.097,
}


DEFAULT_CUT_SPEED_IPM: Dict[str, float] = {
    "carbon_steel": 220.0,
    "stainless": 180.0,
    "aluminum": 260.0,
}


@dataclass(frozen=True)
class SheetMetalCostConfig:
    scrap_factor: float = 0.10
    laser_rate_per_hour: float = 150.0
    brake_rate_per_hour: float = 85.0
    welding_rate_per_hour: float = 95.0
    assembly_rate_per_hour: float = 70.0
    shop_overhead_pct: float = 20.0
    sec_per_bend: float = 30.0
    bend_setup_minutes: float = 8.0
    laser_setup_minutes: float = 12.0
    weld_minutes_per_part: float = 12.0
    assembly_minutes_per_part: float = 10.0
    finish_default_rate_per_sqft: float = 8.0
    finish_default_outside_service_days: int = 4
    base_queue_days: int = 3
    effective_daily_capacity_hours: float = 24.0
    outside_service_buffer_days: int = 0
    target_margin_pct: float = 22.0


def normalize_material(material: Optional[str]) -> Optional[str]:
    if not material:
        return None
    value = material.strip().lower()
    if any(token in value for token in ("stainless", "304", "316")):
        return "stainless"
    if any(token in value for token in ("alum", "5052", "6061")):
        return "aluminum"
    if any(token in value for token in ("steel", "a36", "crs", "hrs", "carbon")):
        return "carbon_steel"
    return None


def calc_required_weight_lbs(
    flat_area_in2: float,
    thickness_in: float,
    material_key: str,
    quantity: float,
    density_override: Optional[float] = None,
) -> float:
    density = density_override if density_override and density_override > 0 else DEFAULT_DENSITY_LB_PER_IN3.get(material_key, 0.284)
    return max(flat_area_in2, 0.0) * max(thickness_in, 0.0) * density * max(quantity, 0.0)


def calc_material_cost(
    required_weight_lbs: float,
    unit_price_per_lb: float,
    scrap_factor: float,
) -> float:
    return max(required_weight_lbs, 0.0) * max(unit_price_per_lb, 0.0) * (1.0 + max(scrap_factor, 0.0))


def calc_cutting_cost(
    cut_length_in: float,
    quantity: float,
    material_key: str,
    machine_rate_per_hour: float,
    setup_minutes: float,
    cut_speed_ipm_override: Optional[float] = None,
) -> Dict[str, float]:
    cut_speed = cut_speed_ipm_override if cut_speed_ipm_override and cut_speed_ipm_override > 0 else DEFAULT_CUT_SPEED_IPM.get(material_key, 180.0)
    runtime_minutes = 0.0
    if cut_speed > 0:
        runtime_minutes = (max(cut_length_in, 0.0) * max(quantity, 0.0)) / cut_speed
    total_hours = (runtime_minutes + max(setup_minutes, 0.0)) / 60.0
    return {
        "hours": total_hours,
        "cost": total_hours * max(machine_rate_per_hour, 0.0),
        "speed_ipm": cut_speed,
    }


def calc_bending_cost(
    bend_count: int,
    quantity: float,
    sec_per_bend: float,
    setup_minutes: float,
    brake_rate_per_hour: float,
) -> Dict[str, float]:
    runtime_seconds = max(bend_count, 0) * max(quantity, 0.0) * max(sec_per_bend, 0.0)
    setup_seconds = max(setup_minutes, 0.0) * 60.0 if bend_count > 0 else 0.0
    total_hours = (runtime_seconds + setup_seconds) / 3600.0
    return {"hours": total_hours, "cost": total_hours * max(brake_rate_per_hour, 0.0)}


def calc_weld_assembly_cost(
    weld_required: bool,
    assembly_required: bool,
    quantity: float,
    weld_minutes_per_part: float,
    assembly_minutes_per_part: float,
    welding_rate_per_hour: float,
    assembly_rate_per_hour: float,
) -> Dict[str, float]:
    weld_hours = (max(weld_minutes_per_part, 0.0) * max(quantity, 0.0) / 60.0) if weld_required else 0.0
    assembly_hours = (max(assembly_minutes_per_part, 0.0) * max(quantity, 0.0) / 60.0) if assembly_required else 0.0
    weld_cost = weld_hours * max(welding_rate_per_hour, 0.0)
    assembly_cost = assembly_hours * max(assembly_rate_per_hour, 0.0)
    return {
        "hours": weld_hours + assembly_hours,
        "cost": weld_cost + assembly_cost,
        "weld_cost": weld_cost,
        "assembly_cost": assembly_cost,
    }


def calc_finishing_cost(
    finish: Optional[str],
    flat_area_in2: float,
    quantity: float,
    finish_rate_per_sqft: Optional[float],
) -> float:
    if not finish:
        return 0.0
    sqft = max(flat_area_in2, 0.0) / 144.0
    rate = finish_rate_per_sqft if finish_rate_per_sqft and finish_rate_per_sqft > 0 else 8.0
    return sqft * max(quantity, 0.0) * rate


def calc_shop_labor_oh(labor_cost: float, overhead_pct: float) -> float:
    return max(labor_cost, 0.0) * (1.0 + max(overhead_pct, 0.0) / 100.0)


def calc_margin(subtotal_without_margin: float, margin_pct: float) -> float:
    return max(subtotal_without_margin, 0.0) * (max(margin_pct, 0.0) / 100.0)


def estimate_lead_time_range(
    total_shop_hours: float,
    outside_service_days: int,
    base_queue_days: int,
    effective_daily_capacity_hours: float,
    extra_outside_service_buffer_days: int = 0,
) -> Dict[str, float]:
    capacity_hours = max(effective_daily_capacity_hours, 1.0)
    run_days = ceil(max(total_shop_hours, 0.0) / capacity_hours)
    nominal_days = max(base_queue_days, 0) + run_days + max(outside_service_days, 0) + max(extra_outside_service_buffer_days, 0)
    min_days = max(1, ceil(nominal_days * 0.85))
    max_days = max(min_days, ceil(nominal_days * 1.25))
    confidence = 0.85 if outside_service_days == 0 else 0.70
    return {"min_days": float(min_days), "max_days": float(max_days), "confidence": confidence}
