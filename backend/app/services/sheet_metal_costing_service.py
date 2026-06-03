"""Deterministic sheet-metal costing and lead-time estimation helpers."""

from dataclasses import dataclass
from math import ceil
import re
from typing import Dict, Optional

DEFAULT_DENSITY_LB_PER_IN3: Dict[str, float] = {
    "carbon_steel": 0.284,
    "stainless": 0.289,
    "aluminum": 0.097,
    "copper": 0.323,
}


DEFAULT_CUT_SPEED_IPM: Dict[str, float] = {
    "carbon_steel": 220.0,
    "stainless": 180.0,
    "aluminum": 260.0,
    "copper": 140.0,
}


GAUGE_TO_INCHES: Dict[str, float] = {
    "24ga": 0.0239,
    "22ga": 0.0299,
    "20ga": 0.0359,
    "18ga": 0.0478,
    "16ga": 0.0598,
    "14ga": 0.0747,
    "12ga": 0.1046,
    "11ga": 0.1196,
    "10ga": 0.1345,
    "7ga": 0.1793,
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
    laser_pierce_seconds: float = 0.8
    laser_min_charge: float = 0.0
    brake_min_charge: float = 0.0
    weld_minutes_per_part: float = 12.0
    assembly_minutes_per_part: float = 10.0
    finish_default_rate_per_sqft: float = 8.0
    finish_min_charge: float = 0.0
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
    if any(token in value for token in ("copper", "c110", "electrolytic tough pitch")):
        return "copper"
    if any(token in value for token in ("steel", "a36", "crs", "hrs", "carbon")):
        return "carbon_steel"
    return None


def normalize_thickness_key(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.replace("gauge", "ga")
    text = re.sub(r"\s+", "", text)
    text = text.replace('"', "in")
    return text


def parse_thickness_to_inches(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", "")
    if not text:
        return None

    gauge_match = re.search(r"\b(\d{1,2})\s*(?:ga|gauge)\b", text)
    if gauge_match:
        return GAUGE_TO_INCHES.get(f"{gauge_match.group(1)}ga")

    mixed_fraction_match = re.search(r"\b(\d+)\s+(\d+)\s*/\s*(\d+)\b", text)
    if mixed_fraction_match:
        whole = float(mixed_fraction_match.group(1))
        numerator = float(mixed_fraction_match.group(2))
        denominator = float(mixed_fraction_match.group(3))
        if denominator:
            return whole + (numerator / denominator)

    fraction_match = re.search(r"\b(\d+)\s*/\s*(\d+)\b", text)
    if fraction_match:
        numerator = float(fraction_match.group(1))
        denominator = float(fraction_match.group(2))
        if denominator:
            return numerator / denominator

    mm_match = re.search(r"(\d*\.?\d+)\s*mm\b", text)
    if mm_match:
        return float(mm_match.group(1)) / 25.4

    inch_match = re.search(r"(\d*\.?\d+)\s*(?:in|inch|inches|\")?\b", text)
    if inch_match:
        return float(inch_match.group(1))

    return None


def estimate_cut_speed_ipm(material_key: str, thickness_in: Optional[float], override: Optional[float] = None) -> float:
    base_speed = override if override and override > 0 else DEFAULT_CUT_SPEED_IPM.get(material_key, 180.0)
    if not thickness_in or thickness_in <= 0:
        return base_speed
    # Baseline speeds are treated as roughly 1/8 in. Thin stock can move faster;
    # thick plate slows down, but clamp to avoid extreme guesses from bad inputs.
    multiplier = min(1.35, max(0.35, 0.125 / thickness_in))
    return base_speed * multiplier


def calc_dynamic_scrap_factor(
    base_scrap_factor: float,
    quantity: float,
    flat_area_in2: float,
    cut_length_in: float,
    bend_count: int = 0,
    hole_count: Optional[int] = None,
    geometry_confidence: float = 1.0,
) -> float:
    scrap = max(base_scrap_factor, 0.0)
    qty = max(quantity, 0.0)
    flat_area = max(flat_area_in2, 0.0)
    cut_length = max(cut_length_in, 0.0)

    if qty <= 2:
        scrap += 0.04
    elif qty >= 25:
        scrap = max(scrap - 0.02, 0.04)

    if flat_area > 0:
        sqft_per_part = flat_area / 144.0
        if sqft_per_part < 0.25:
            scrap += 0.03
        elif sqft_per_part > 8.0:
            scrap += 0.02

    if cut_length > 0 and flat_area > 0:
        perimeter_density = cut_length / flat_area
        if perimeter_density > 1.2:
            scrap += 0.02

    if bend_count >= 6:
        scrap += 0.02
    if hole_count and hole_count >= 20:
        scrap += 0.01
    if geometry_confidence and geometry_confidence < 0.7:
        scrap += 0.03

    return min(scrap, 0.35)


def calc_required_weight_lbs(
    flat_area_in2: float,
    thickness_in: float,
    material_key: str,
    quantity: float,
    density_override: Optional[float] = None,
) -> float:
    density = (
        density_override
        if density_override and density_override > 0
        else DEFAULT_DENSITY_LB_PER_IN3.get(material_key, 0.284)
    )
    return max(flat_area_in2, 0.0) * max(thickness_in, 0.0) * density * max(quantity, 0.0)


def calc_material_cost(
    required_weight_lbs: float,
    unit_price_per_lb: float,
    scrap_factor: float,
    material_markup_pct: float = 0.0,
) -> float:
    raw_cost = max(required_weight_lbs, 0.0) * max(unit_price_per_lb, 0.0) * (1.0 + max(scrap_factor, 0.0))
    return raw_cost * (1.0 + max(material_markup_pct, 0.0) / 100.0)


def calc_cutting_cost(
    cut_length_in: float,
    quantity: float,
    material_key: str,
    machine_rate_per_hour: float,
    setup_minutes: float,
    cut_speed_ipm_override: Optional[float] = None,
    thickness_in: Optional[float] = None,
    pierce_count: int = 0,
    pierce_time_seconds: float = 0.0,
    min_charge: float = 0.0,
) -> Dict[str, float]:
    cut_speed = estimate_cut_speed_ipm(material_key, thickness_in, cut_speed_ipm_override)
    runtime_minutes = 0.0
    if cut_speed > 0:
        runtime_minutes = (max(cut_length_in, 0.0) * max(quantity, 0.0)) / cut_speed
    pierce_seconds = max(pierce_count, 0) * max(quantity, 0.0) * max(pierce_time_seconds, 0.0)
    total_hours = (runtime_minutes + max(setup_minutes, 0.0) + pierce_seconds / 60.0) / 60.0
    raw_cost = total_hours * max(machine_rate_per_hour, 0.0)
    cost = max(raw_cost, max(min_charge, 0.0)) if total_hours > 0 else 0.0
    return {
        "hours": total_hours,
        "cost": cost,
        "raw_cost": raw_cost,
        "minimum_charge_applied": max(0.0, cost - raw_cost),
        "speed_ipm": cut_speed,
        "pierce_count": float(max(pierce_count, 0)),
    }


def calc_bending_cost(
    bend_count: int,
    quantity: float,
    sec_per_bend: float,
    setup_minutes: float,
    brake_rate_per_hour: float,
    unique_bend_groups: Optional[int] = None,
    complexity_multiplier: float = 1.0,
    min_charge: float = 0.0,
) -> Dict[str, float]:
    bend_qty = max(bend_count, 0)
    runtime_seconds = bend_qty * max(quantity, 0.0) * max(sec_per_bend, 0.0) * max(complexity_multiplier, 0.0)
    setup_groups = unique_bend_groups if unique_bend_groups and unique_bend_groups > 0 else (1 if bend_qty > 0 else 0)
    setup_seconds = max(setup_minutes, 0.0) * 60.0 * setup_groups if bend_qty > 0 else 0.0
    total_hours = (runtime_seconds + setup_seconds) / 3600.0
    raw_cost = total_hours * max(brake_rate_per_hour, 0.0)
    cost = max(raw_cost, max(min_charge, 0.0)) if total_hours > 0 else 0.0
    return {
        "hours": total_hours,
        "cost": cost,
        "raw_cost": raw_cost,
        "minimum_charge_applied": max(0.0, cost - raw_cost),
        "unique_bend_groups": float(setup_groups),
    }


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
    price_per_part: float = 0.0,
    price_per_lb: float = 0.0,
    required_weight_lbs: float = 0.0,
    minimum_charge: float = 0.0,
) -> float:
    if not finish:
        return 0.0
    sqft = max(flat_area_in2, 0.0) / 144.0
    rate = 8.0 if finish_rate_per_sqft is None else max(finish_rate_per_sqft, 0.0)
    area_cost = sqft * max(quantity, 0.0) * rate
    part_cost = max(price_per_part, 0.0) * max(quantity, 0.0)
    weight_cost = max(price_per_lb, 0.0) * max(required_weight_lbs, 0.0)
    raw_cost = area_cost + part_cost + weight_cost
    return max(raw_cost, max(minimum_charge, 0.0)) if raw_cost > 0 else 0.0


def estimate_unique_bend_groups(bend_count: int) -> int:
    if bend_count <= 0:
        return 0
    return max(1, min(4, ceil(bend_count / 6)))


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
    nominal_days = (
        max(base_queue_days, 0) + run_days + max(outside_service_days, 0) + max(extra_outside_service_buffer_days, 0)
    )
    min_days = max(1, ceil(nominal_days * 0.85))
    max_days = max(min_days, ceil(nominal_days * 1.25))
    confidence = 0.85 if outside_service_days == 0 else 0.70
    return {"min_days": float(min_days), "max_days": float(max_days), "confidence": confidence}
