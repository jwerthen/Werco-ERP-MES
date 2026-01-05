from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.quote_config import QuoteMaterial, QuoteMachine, QuoteFinish, QuoteSettings, MaterialCategory, MachineType
from pydantic import BaseModel
import json
import math

router = APIRouter()


# ============ LASER CUTTING SPEED TABLE ============
# Speeds in inches per minute (converted from m/min, using midpoint of ranges)
# Format: thickness -> {material_category: speed_ipm}
# Original data in m/min, converted: m/min * 39.37 = ipm

LASER_CUTTING_SPEEDS = {
    # 20ga (0.9mm) - Using midpoint of ranges
    "20ga": {"steel": 531, "stainless": 3346, "aluminum": 3740},      # Steel O2: 13.5m/min, SS N2: 85, Al N2: 95
    "0.0359": {"steel": 531, "stainless": 3346, "aluminum": 3740},
    
    # 16ga (1.5mm)
    "16ga": {"steel": 354, "stainless": 2067, "aluminum": 2264},      # Steel O2: 9m/min, SS N2: 52.5, Al N2: 57.5
    "0.0598": {"steel": 354, "stainless": 2067, "aluminum": 2264},
    
    # 11ga (3.0mm)  
    "11ga": {"steel": 167, "stainless": 728, "aluminum": 827},        # Steel O2: 4.25m/min, SS N2: 18.5, Al N2: 21
    "0.1196": {"steel": 167, "stainless": 728, "aluminum": 827},
    
    # 3/16" (4.7mm)
    "0.1875": {"steel": 112, "stainless": 364, "aluminum": 404},      # Steel O2: 2.85m/min, SS N2: 9.25, Al N2: 10.25
    "3/16": {"steel": 112, "stainless": 364, "aluminum": 404},
    
    # 1/4" (6.35mm)
    "0.250": {"steel": 83, "stainless": 236, "aluminum": 276},        # Steel O2: 2.1m/min, SS N2: 6, Al N2: 7
    "1/4": {"steel": 83, "stainless": 236, "aluminum": 276},
    
    # 3/8" (9.5mm)
    "0.375": {"steel": 55, "stainless": 118, "aluminum": 134},        # Steel O2: 1.4m/min, SS N2: 3, Al N2: 3.4
    "3/8": {"steel": 55, "stainless": 118, "aluminum": 134},
    
    # 1/2" (12.7mm)
    "0.500": {"steel": 41, "stainless": 67, "aluminum": 73},          # Steel O2: 1.05m/min, SS N2: 1.7, Al N2: 1.85
    "1/2": {"steel": 41, "stainless": 67, "aluminum": 73},
    
    # 5/8" (15.8mm)
    "0.625": {"steel": 31, "stainless": 39, "aluminum": 43},          # Steel O2: 0.8m/min, SS N2: 1.0, Al N2: 1.1
    "5/8": {"steel": 31, "stainless": 39, "aluminum": 43},
    
    # 3/4" (19.0mm)
    "0.750": {"steel": 28, "stainless": 30, "aluminum": 35},          # Steel O2: 0.7m/min, SS N2: 0.75, Al N2: 0.9
    "3/4": {"steel": 28, "stainless": 30, "aluminum": 35},
    
    # 1" (25.4mm)
    "1.000": {"steel": 20, "stainless": 16, "aluminum": 20},          # Steel O2: 0.5m/min, SS N2: 0.4, Al N2: 0.5
    "1": {"steel": 20, "stainless": 16, "aluminum": 20},
    
    # Additional common gauges (interpolated)
    "7ga": {"steel": 140, "stainless": 550, "aluminum": 630},         # ~0.1793" - between 11ga and 3/16
    "0.1793": {"steel": 140, "stainless": 550, "aluminum": 630},
    
    "10ga": {"steel": 200, "stainless": 900, "aluminum": 1000},       # ~0.1345" - between 11ga and 16ga
    "0.1345": {"steel": 200, "stainless": 900, "aluminum": 1000},
    
    "12ga": {"steel": 250, "stainless": 1200, "aluminum": 1400},      # ~0.1046"
    "0.1046": {"steel": 250, "stainless": 1200, "aluminum": 1400},
    
    "14ga": {"steel": 300, "stainless": 1500, "aluminum": 1700},      # ~0.0747"
    "0.0747": {"steel": 300, "stainless": 1500, "aluminum": 1700},
    
    "18ga": {"steel": 430, "stainless": 2700, "aluminum": 3000},      # ~0.0478"
    "0.0478": {"steel": 430, "stainless": 2700, "aluminum": 3000},
    
    "22ga": {"steel": 600, "stainless": 3800, "aluminum": 4200},      # ~0.0299"
    "0.0299": {"steel": 600, "stainless": 3800, "aluminum": 4200},
    
    "24ga": {"steel": 650, "stainless": 4100, "aluminum": 4500},      # ~0.0239"
    "0.0239": {"steel": 650, "stainless": 4100, "aluminum": 4500},
    
    # 1/8" 
    "0.125": {"steel": 180, "stainless": 800, "aluminum": 900},
    "1/8": {"steel": 180, "stainless": 800, "aluminum": 900},
}


def get_laser_cutting_speed(thickness: str, material_category: str) -> float:
    """
    Get laser cutting speed in inches per minute for given thickness and material.
    Returns default of 100 IPM if not found.
    """
    # Normalize thickness string
    thickness_clean = thickness.strip().lower().replace('"', '').replace("'", "")
    
    # Try direct lookup
    if thickness_clean in LASER_CUTTING_SPEEDS:
        speeds = LASER_CUTTING_SPEEDS[thickness_clean]
        # Map material categories
        if material_category in ['steel', 'galvanized']:
            return speeds.get('steel', 100)
        elif material_category in ['stainless']:
            return speeds.get('stainless', 80)
        elif material_category in ['aluminum']:
            return speeds.get('aluminum', 120)
        else:
            return speeds.get('steel', 100)  # Default to steel speeds
    
    # Try without 'ga' suffix
    if 'ga' in thickness_clean:
        gauge_num = thickness_clean.replace('ga', '')
        if gauge_num + 'ga' in LASER_CUTTING_SPEEDS:
            return get_laser_cutting_speed(gauge_num + 'ga', material_category)
    
    # Default fallback
    return 100.0


# ============ SCHEMAS ============

class CNCQuoteRequest(BaseModel):
    """Input for CNC machining quote calculation"""
    # Part dimensions (inches)
    length: float
    width: float
    height: float
    
    # Material
    material_id: int
    
    # Complexity factors
    num_setups: int = 1
    complexity: str = "medium"  # simple, medium, complex, very_complex
    
    # Features (each adds time)
    num_holes: int = 0
    num_tapped_holes: int = 0
    num_pockets: int = 0
    num_slots: int = 0
    
    # Tolerances
    tightest_tolerance: str = "standard"  # standard, tight, precision, ultra
    
    # Finish
    surface_finish: str = "as_machined"  # as_machined, light_deburr, smooth, mirror
    finish_ids: List[int] = []
    
    # Quantity and lead time
    quantity: int = 1
    rush: bool = False


class SheetMetalQuoteRequest(BaseModel):
    """Input for sheet metal quote calculation"""
    # Flat pattern dimensions (inches)
    flat_length: float
    flat_width: float
    
    # Material
    material_id: int
    gauge: str  # "10ga", "12ga", "14ga", "16ga", "18ga", etc.
    
    # Cutting
    cut_perimeter: float  # Total inches of cutting (outer + inner)
    num_holes: int = 0
    num_slots: int = 0
    
    # Bending
    num_bends: int = 0
    num_unique_bends: int = 0  # Different bend angles/radii (affects setup)
    
    # Hardware
    num_pem_inserts: int = 0
    num_weld_nuts: int = 0
    
    # Finish
    finish_ids: List[int] = []
    
    # Quantity and lead time
    quantity: int = 1
    rush: bool = False


class QuoteCalculationResult(BaseModel):
    """Result of quote calculation"""
    # Cost breakdown
    material_cost: float
    cutting_cost: float
    machining_cost: float
    setup_cost: float
    bending_cost: float
    hardware_cost: float
    finish_cost: float
    
    # Totals
    unit_cost: float
    subtotal: float
    markup_amount: float
    quantity_discount: float
    rush_charge: float
    total: float
    unit_price: float
    
    # Time estimates
    estimated_hours: float
    lead_time_days: int
    
    # Breakdown details
    details: dict


# ============ HELPER FUNCTIONS ============

def get_setting(db: Session, key: str, default=None):
    """Get a quote setting value"""
    setting = db.query(QuoteSettings).filter(QuoteSettings.setting_key == key).first()
    if not setting:
        return default
    if setting.setting_type == "number":
        return float(setting.setting_value)
    if setting.setting_type == "json":
        return json.loads(setting.setting_value)
    return setting.setting_value


def apply_quantity_breaks(subtotal: float, quantity: int, db: Session) -> float:
    """Apply quantity discount"""
    breaks = get_setting(db, "quantity_breaks", {"10": 0.95, "25": 0.90, "50": 0.85, "100": 0.80})
    discount_pct = 1.0
    
    for qty_str, pct in sorted(breaks.items(), key=lambda x: int(x[0])):
        if quantity >= int(qty_str):
            discount_pct = pct
    
    return subtotal * (1 - discount_pct) if discount_pct < 1.0 else 0


# ============ CNC CALCULATOR ============

@router.post("/cnc", response_model=QuoteCalculationResult)
def calculate_cnc_quote(
    request: CNCQuoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Calculate quote for CNC machined part"""
    
    # Get material
    material = db.query(QuoteMaterial).filter(QuoteMaterial.id == request.material_id).first()
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    
    # Get machine rate (use first active CNC mill)
    machine = db.query(QuoteMachine).filter(
        QuoteMachine.machine_type.in_([MachineType.CNC_MILL_3AXIS, MachineType.CNC_MILL_4AXIS, MachineType.CNC_MILL_5AXIS]),
        QuoteMachine.is_active == True
    ).first()
    
    # Default rates if no machine configured
    machine_rate = machine.rate_per_hour if machine else 85.0
    setup_rate = machine.setup_rate_per_hour if machine and machine.setup_rate_per_hour else machine_rate
    base_setup_hours = machine.typical_setup_hours if machine else 1.0
    
    # Calculate stock volume (add 0.25" per side for stock)
    stock_length = request.length + 0.5
    stock_width = request.width + 0.5
    stock_height = request.height + 0.5
    stock_volume = stock_length * stock_width * stock_height
    
    # Material cost
    if material.stock_price_per_cubic_inch > 0:
        raw_material = stock_volume * material.stock_price_per_cubic_inch
    else:
        weight = stock_volume * material.density_lb_per_cubic_inch
        raw_material = weight * material.stock_price_per_pound
    
    material_cost = raw_material * (1 + material.material_markup_pct / 100)
    
    # Complexity time multipliers
    complexity_factors = {
        "simple": 0.7,
        "medium": 1.0,
        "complex": 1.5,
        "very_complex": 2.5
    }
    complexity_mult = complexity_factors.get(request.complexity, 1.0)
    
    # Base machining time (rough estimate based on volume to remove)
    # Assume removing 50% of stock volume, at ~1 cubic inch per minute for steel
    removal_volume = stock_volume * 0.5
    base_time_minutes = removal_volume / (1.0 * material.machinability_factor)
    
    # Add feature time
    feature_time = (
        request.num_holes * 2 +  # 2 min per hole
        request.num_tapped_holes * 4 +  # 4 min per tapped hole
        request.num_pockets * 10 +  # 10 min per pocket
        request.num_slots * 5  # 5 min per slot
    )
    
    # Total machining time
    machining_minutes = (base_time_minutes + feature_time) * complexity_mult
    machining_hours = machining_minutes / 60
    
    # Tolerance surcharge
    tolerance_surcharges = get_setting(db, "tolerance_surcharges", {
        "standard": 1.0, "tight": 1.15, "precision": 1.35, "ultra": 1.6
    })
    tolerance_mult = tolerance_surcharges.get(request.tightest_tolerance, 1.0)
    
    # Surface finish surcharge
    finish_surcharges = {"as_machined": 1.0, "light_deburr": 1.05, "smooth": 1.15, "mirror": 1.4}
    finish_mult = finish_surcharges.get(request.surface_finish, 1.0)
    
    # Apply multipliers
    machining_hours = machining_hours * tolerance_mult * finish_mult
    
    # Setup time
    setup_hours = base_setup_hours * request.num_setups
    
    # Calculate costs
    machining_cost = machining_hours * machine_rate
    setup_cost = setup_hours * setup_rate
    
    # Finishing costs
    finish_cost = 0
    finish_days = 0
    for fid in request.finish_ids:
        finish = db.query(QuoteFinish).filter(QuoteFinish.id == fid).first()
        if finish:
            if finish.price_per_part > 0:
                finish_cost += finish.price_per_part
            # Could also calculate by surface area or weight
            finish_cost = max(finish_cost, finish.minimum_charge)
            finish_days += finish.additional_days
    
    # Unit cost
    unit_cost = material_cost + machining_cost + setup_cost / request.quantity + finish_cost
    
    # Subtotal
    subtotal = unit_cost * request.quantity
    
    # Markup
    markup_pct = get_setting(db, "default_markup_pct", 35)
    markup_amount = subtotal * (markup_pct / 100)
    
    # Quantity discount
    quantity_discount = apply_quantity_breaks(subtotal + markup_amount, request.quantity, db)
    
    # Rush charge
    rush_mult = get_setting(db, "rush_multiplier", 1.5)
    rush_charge = (subtotal + markup_amount) * (rush_mult - 1) if request.rush else 0
    
    # Minimum order
    minimum = get_setting(db, "minimum_order_charge", 150)
    
    # Total
    total = max(subtotal + markup_amount - quantity_discount + rush_charge, minimum)
    
    # Lead time
    base_lead = get_setting(db, "standard_lead_days", 10)
    lead_time = math.ceil(base_lead * (0.5 if request.rush else 1.0)) + finish_days
    
    return QuoteCalculationResult(
        material_cost=round(material_cost * request.quantity, 2),
        cutting_cost=0,
        machining_cost=round(machining_cost * request.quantity, 2),
        setup_cost=round(setup_cost, 2),
        bending_cost=0,
        hardware_cost=0,
        finish_cost=round(finish_cost * request.quantity, 2),
        unit_cost=round(unit_cost, 2),
        subtotal=round(subtotal, 2),
        markup_amount=round(markup_amount, 2),
        quantity_discount=round(quantity_discount, 2),
        rush_charge=round(rush_charge, 2),
        total=round(total, 2),
        unit_price=round(total / request.quantity, 2),
        estimated_hours=round(machining_hours + setup_hours, 2),
        lead_time_days=lead_time,
        details={
            "stock_dimensions": f"{stock_length:.2f} x {stock_width:.2f} x {stock_height:.2f}",
            "stock_volume_ci": round(stock_volume, 2),
            "machining_hours": round(machining_hours, 2),
            "setup_hours": round(setup_hours, 2),
            "machine_rate": machine_rate,
            "complexity_multiplier": complexity_mult,
            "tolerance_multiplier": tolerance_mult,
            "finish_multiplier": finish_mult
        }
    )


# ============ SHEET METAL CALCULATOR ============

@router.post("/sheet-metal", response_model=QuoteCalculationResult)
def calculate_sheet_metal_quote(
    request: SheetMetalQuoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Calculate quote for sheet metal part"""
    
    # Get material
    material = db.query(QuoteMaterial).filter(QuoteMaterial.id == request.material_id).first()
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    
    # Get sheet pricing for gauge
    sheet_pricing = material.sheet_pricing or {}
    price_per_sqft = sheet_pricing.get(request.gauge, 5.0)  # Default $5/sqft
    
    # Get machines
    laser = db.query(QuoteMachine).filter(
        QuoteMachine.machine_type.in_([MachineType.LASER_FIBER, MachineType.LASER_CO2]),
        QuoteMachine.is_active == True
    ).first()
    
    brake = db.query(QuoteMachine).filter(
        QuoteMachine.machine_type == MachineType.PRESS_BRAKE,
        QuoteMachine.is_active == True
    ).first()
    
    # Default rates
    laser_rate = laser.rate_per_hour if laser else 150.0
    brake_rate = brake.rate_per_hour if brake else 75.0
    bend_time_sec = brake.bend_time_seconds if brake else 15.0
    bend_setup_sec = brake.setup_time_per_bend_type if brake else 300.0
    
    # Get cutting speed from lookup table (inches per minute)
    cutting_speed = get_laser_cutting_speed(request.gauge, material.category.value)
    
    # Material cost (add 10% for nesting waste)
    flat_area_sqft = (request.flat_length * request.flat_width) / 144
    material_cost = flat_area_sqft * 1.10 * price_per_sqft * (1 + material.material_markup_pct / 100)
    
    # Cutting time and cost
    cutting_minutes = request.cut_perimeter / cutting_speed
    
    # Pierce time based on thickness (seconds per pierce)
    # Thin (< 3mm / ~11ga): 0.1 - 0.3 sec -> 0.2 avg
    # Medium (6mm-12mm / 1/4" - 1/2"): 0.5 - 1.5 sec -> 1.0 avg
    # Thick (19mm+ / 3/4"+): 2.0 - 5.0 sec -> 3.5 avg
    thickness_to_pierce_time = {
        # Thin materials (< 3mm)
        "24ga": 0.2, "22ga": 0.2, "20ga": 0.2, "18ga": 0.2, "16ga": 0.2,
        "14ga": 0.2, "12ga": 0.2, "11ga": 0.3,
        "0.0239": 0.2, "0.0299": 0.2, "0.0359": 0.2, "0.0478": 0.2,
        "0.0598": 0.2, "0.0747": 0.2, "0.1046": 0.2, "0.1196": 0.3,
        # Medium materials (3mm - 12mm)
        "0.125": 0.5, "1/8": 0.5,
        "7ga": 0.6, "0.1793": 0.6,
        "0.1875": 0.7, "3/16": 0.7,
        "0.250": 1.0, "1/4": 1.0,
        "0.375": 1.2, "3/8": 1.2,
        "0.500": 1.5, "1/2": 1.5,
        # Thick materials (> 12mm)
        "0.625": 2.5, "5/8": 2.5,
        "0.750": 3.5, "3/4": 3.5,
        "1.000": 4.5, "1": 4.5,
    }
    
    # Get pierce time for this thickness
    thickness_clean = request.gauge.strip().lower().replace('"', '').replace("'", "")
    pierce_time_sec = thickness_to_pierce_time.get(thickness_clean, 0.5)
    
    # Total pierces = outer profile (1) + holes + slots
    num_pierces = 1 + request.num_holes + request.num_slots
    pierce_time_minutes = (num_pierces * pierce_time_sec) / 60
    
    total_cutting_minutes = cutting_minutes + pierce_time_minutes
    
    # Add 17.5% buffer for parts with more than 20 holes (accounts for repositioning, heat management)
    if request.num_holes > 20:
        total_cutting_minutes = total_cutting_minutes * 1.175
    cutting_hours = total_cutting_minutes / 60
    cutting_cost = cutting_hours * laser_rate
    
    # Bending time and cost
    bend_run_time = request.num_bends * bend_time_sec / 3600  # hours
    bend_setup_time = request.num_unique_bends * bend_setup_sec / 3600  # hours
    total_bend_hours = bend_run_time + bend_setup_time / request.quantity
    bending_cost = total_bend_hours * brake_rate * request.quantity
    
    # Hardware cost
    pem_cost_each = 0.50  # Typical PEM insert cost
    weld_nut_cost_each = 0.25
    hardware_material = (request.num_pem_inserts * pem_cost_each + 
                         request.num_weld_nuts * weld_nut_cost_each)
    hardware_labor = (request.num_pem_inserts + request.num_weld_nuts) * 0.5 / 60 * 50  # 30 sec each @ $50/hr
    hardware_cost = hardware_material + hardware_labor
    
    # Finishing costs
    finish_cost = 0
    finish_days = 0
    for fid in request.finish_ids:
        finish = db.query(QuoteFinish).filter(QuoteFinish.id == fid).first()
        if finish:
            if finish.price_per_sqft > 0:
                finish_cost += flat_area_sqft * finish.price_per_sqft
            elif finish.price_per_part > 0:
                finish_cost += finish.price_per_part
            finish_cost = max(finish_cost, finish.minimum_charge)
            finish_days += finish.additional_days
    
    # Setup cost (laser setup is minimal, brake setup per unique bend included above)
    laser_setup = 0.25 * laser_rate  # 15 min laser setup
    setup_cost = laser_setup
    
    # Unit cost
    unit_material = material_cost
    unit_cutting = cutting_cost
    unit_bending = bending_cost / request.quantity if request.quantity > 0 else 0
    unit_hardware = hardware_cost
    unit_finish = finish_cost
    unit_setup = setup_cost / request.quantity
    
    unit_cost = unit_material + unit_cutting + unit_bending + unit_hardware + unit_finish + unit_setup
    
    # Subtotal
    subtotal = unit_cost * request.quantity
    
    # Markup
    markup_pct = get_setting(db, "default_markup_pct", 35)
    markup_amount = subtotal * (markup_pct / 100)
    
    # Quantity discount
    quantity_discount = apply_quantity_breaks(subtotal + markup_amount, request.quantity, db)
    
    # Rush charge
    rush_mult = get_setting(db, "rush_multiplier", 1.5)
    rush_charge = (subtotal + markup_amount) * (rush_mult - 1) if request.rush else 0
    
    # Minimum order
    minimum = get_setting(db, "minimum_order_charge", 150)
    
    # Total
    total = max(subtotal + markup_amount - quantity_discount + rush_charge, minimum)
    
    # Lead time
    base_lead = get_setting(db, "standard_lead_days", 7)  # Sheet metal usually faster
    lead_time = math.ceil(base_lead * (0.5 if request.rush else 1.0)) + finish_days
    
    # Total hours
    total_hours = cutting_hours + total_bend_hours + 0.25  # Include setup
    
    return QuoteCalculationResult(
        material_cost=round(material_cost * request.quantity, 2),
        cutting_cost=round(cutting_cost * request.quantity, 2),
        machining_cost=0,
        setup_cost=round(setup_cost, 2),
        bending_cost=round(bending_cost, 2),
        hardware_cost=round(hardware_cost * request.quantity, 2),
        finish_cost=round(finish_cost * request.quantity, 2),
        unit_cost=round(unit_cost, 2),
        subtotal=round(subtotal, 2),
        markup_amount=round(markup_amount, 2),
        quantity_discount=round(quantity_discount, 2),
        rush_charge=round(rush_charge, 2),
        total=round(total, 2),
        unit_price=round(total / request.quantity, 2),
        estimated_hours=round(total_hours * request.quantity, 2),
        lead_time_days=lead_time,
        details={
            "flat_area_sqft": round(flat_area_sqft, 2),
            "cutting_minutes": round(total_cutting_minutes, 1),
            "cutting_speed_ipm": cutting_speed,
            "bend_time_hours": round(total_bend_hours, 2),
            "laser_rate": laser_rate,
            "brake_rate": brake_rate
        }
    )


# ============ CONFIGURATION ENDPOINTS ============

@router.get("/materials")
def list_materials(
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List available materials for quoting"""
    query = db.query(QuoteMaterial).filter(QuoteMaterial.is_active == True)
    if category:
        query = query.filter(QuoteMaterial.category == category)
    return query.order_by(QuoteMaterial.name).all()


@router.post("/materials")
def create_material(
    name: str,
    category: str,
    stock_price_per_cubic_inch: float = 0,
    stock_price_per_pound: float = 0,
    density_lb_per_cubic_inch: float = 0,
    sheet_pricing: Optional[dict] = None,
    machinability_factor: float = 1.0,
    material_markup_pct: float = 20.0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Create a new quote material"""
    material = QuoteMaterial(
        name=name,
        category=MaterialCategory(category),
        stock_price_per_cubic_inch=stock_price_per_cubic_inch,
        stock_price_per_pound=stock_price_per_pound,
        density_lb_per_cubic_inch=density_lb_per_cubic_inch,
        sheet_pricing=sheet_pricing,
        machinability_factor=machinability_factor,
        material_markup_pct=material_markup_pct
    )
    db.add(material)
    db.commit()
    db.refresh(material)
    return material


@router.get("/machines")
def list_machines(
    machine_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List available machines for quoting"""
    query = db.query(QuoteMachine).filter(QuoteMachine.is_active == True)
    if machine_type:
        query = query.filter(QuoteMachine.machine_type == machine_type)
    return query.order_by(QuoteMachine.name).all()


@router.post("/machines")
def create_machine(
    name: str,
    machine_type: str,
    rate_per_hour: float,
    setup_rate_per_hour: Optional[float] = None,
    typical_setup_hours: float = 1.0,
    cutting_speeds: Optional[dict] = None,
    bend_time_seconds: float = 15.0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Create a new quote machine"""
    machine = QuoteMachine(
        name=name,
        machine_type=MachineType(machine_type),
        rate_per_hour=rate_per_hour,
        setup_rate_per_hour=setup_rate_per_hour,
        typical_setup_hours=typical_setup_hours,
        cutting_speeds=cutting_speeds,
        bend_time_seconds=bend_time_seconds
    )
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


@router.get("/finishes")
def list_finishes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List available finishes"""
    return db.query(QuoteFinish).filter(QuoteFinish.is_active == True).order_by(QuoteFinish.name).all()


@router.post("/finishes")
def create_finish(
    name: str,
    category: str,
    price_per_part: float = 0,
    price_per_sqft: float = 0,
    minimum_charge: float = 0,
    additional_days: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Create a new finish option"""
    finish = QuoteFinish(
        name=name,
        category=category,
        price_per_part=price_per_part,
        price_per_sqft=price_per_sqft,
        minimum_charge=minimum_charge,
        additional_days=additional_days
    )
    db.add(finish)
    db.commit()
    db.refresh(finish)
    return finish


@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all quote settings"""
    settings = db.query(QuoteSettings).all()
    result = {}
    for s in settings:
        if s.setting_type == "number":
            result[s.setting_key] = float(s.setting_value)
        elif s.setting_type == "json":
            result[s.setting_key] = json.loads(s.setting_value)
        else:
            result[s.setting_key] = s.setting_value
    return result


@router.post("/settings/{key}")
def update_setting(
    key: str,
    value: str,
    setting_type: str = "text",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Update a quote setting"""
    setting = db.query(QuoteSettings).filter(QuoteSettings.setting_key == key).first()
    if setting:
        setting.setting_value = value
        setting.setting_type = setting_type
    else:
        setting = QuoteSettings(
            setting_key=key,
            setting_value=value,
            setting_type=setting_type
        )
        db.add(setting)
    db.commit()
    return {"status": "ok", "key": key}


@router.post("/seed-defaults")
def seed_default_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Seed default materials, machines, and settings"""
    
    # Default settings
    default_settings = [
        ("default_markup_pct", "35", "number"),
        ("minimum_order_charge", "150", "number"),
        ("rush_multiplier", "1.5", "number"),
        ("standard_lead_days", "10", "number"),
        ("quantity_breaks", '{"10": 0.95, "25": 0.90, "50": 0.85, "100": 0.80}', "json"),
        ("tolerance_surcharges", '{"standard": 1.0, "tight": 1.15, "precision": 1.35, "ultra": 1.6}', "json"),
    ]
    
    for key, value, stype in default_settings:
        existing = db.query(QuoteSettings).filter(QuoteSettings.setting_key == key).first()
        if not existing:
            db.add(QuoteSettings(setting_key=key, setting_value=value, setting_type=stype))
    
    # Default materials
    default_materials = [
        ("6061-T6 Aluminum", "aluminum", 0.12, 3.50, 0.098, 1.5, {"10ga": 4.50, "12ga": 3.75, "14ga": 3.25, "16ga": 2.75, "18ga": 2.25}),
        ("304 Stainless Steel", "stainless", 0.35, 4.50, 0.289, 0.6, {"10ga": 8.50, "12ga": 7.00, "14ga": 6.00, "16ga": 5.00, "18ga": 4.00}),
        ("1018 Cold Rolled Steel", "steel", 0.08, 1.20, 0.283, 1.0, {"10ga": 3.50, "12ga": 2.75, "14ga": 2.25, "16ga": 1.85, "18ga": 1.50}),
        ("A36 Hot Rolled Steel", "steel", 0.06, 0.90, 0.283, 1.0, {"10ga": 3.00, "12ga": 2.50, "14ga": 2.00, "16ga": 1.65, "18ga": 1.35}),
        ("5052-H32 Aluminum", "aluminum", 0.11, 3.25, 0.097, 1.4, {"10ga": 4.25, "12ga": 3.50, "14ga": 3.00, "16ga": 2.50, "18ga": 2.00}),
        ("Brass 260", "brass", 0.45, 5.50, 0.308, 0.8, {}),
        ("Delrin/Acetal", "plastic", 0.08, 2.50, 0.051, 2.0, {}),
    ]
    
    for name, cat, price_ci, price_lb, density, mach, sheets in default_materials:
        existing = db.query(QuoteMaterial).filter(QuoteMaterial.name == name).first()
        if not existing:
            db.add(QuoteMaterial(
                name=name,
                category=MaterialCategory(cat),
                stock_price_per_cubic_inch=price_ci,
                stock_price_per_pound=price_lb,
                density_lb_per_cubic_inch=density,
                machinability_factor=mach,
                sheet_pricing=sheets
            ))
    
    # Default machines
    default_machines = [
        ("CNC Mill - 3 Axis", "cnc_mill_3axis", 85.0, 75.0, 1.0, None, None),
        ("CNC Mill - 4 Axis", "cnc_mill_4axis", 110.0, 90.0, 1.5, None, None),
        ("CNC Mill - 5 Axis", "cnc_mill_5axis", 150.0, 120.0, 2.0, None, None),
        ("CNC Lathe", "cnc_lathe", 75.0, 65.0, 0.75, None, None),
        ("Fiber Laser", "laser_fiber", 175.0, 150.0, 0.25, 
         {"steel": {"10ga": 120, "12ga": 180, "14ga": 250, "16ga": 350, "18ga": 450},
          "stainless": {"10ga": 80, "12ga": 120, "14ga": 180, "16ga": 250, "18ga": 320},
          "aluminum": {"10ga": 200, "12ga": 300, "14ga": 400, "16ga": 500, "18ga": 600}}, None),
        ("Press Brake", "press_brake", 65.0, 55.0, 0.25, None, 15.0),
    ]
    
    for name, mtype, rate, setup, setup_hrs, speeds, bend in default_machines:
        existing = db.query(QuoteMachine).filter(QuoteMachine.name == name).first()
        if not existing:
            db.add(QuoteMachine(
                name=name,
                machine_type=MachineType(mtype),
                rate_per_hour=rate,
                setup_rate_per_hour=setup,
                typical_setup_hours=setup_hrs,
                cutting_speeds=speeds,
                bend_time_seconds=bend or 15.0
            ))
    
    # Default finishes
    default_finishes = [
        ("Powder Coat - Standard Colors", "coating", 0, 8.00, 25.00, 3),
        ("Powder Coat - Custom Color", "coating", 0, 12.00, 50.00, 5),
        ("Anodize Type II - Clear", "plating", 0, 6.00, 35.00, 5),
        ("Anodize Type II - Color", "plating", 0, 8.00, 45.00, 5),
        ("Anodize Type III Hard", "plating", 0, 15.00, 75.00, 7),
        ("Zinc Plating", "plating", 0, 4.00, 25.00, 3),
        ("Nickel Plating", "plating", 0, 10.00, 50.00, 5),
        ("Passivate", "treatment", 2.00, 0, 15.00, 2),
        ("Black Oxide", "treatment", 1.50, 0, 20.00, 2),
        ("Chem Film / Alodine", "treatment", 0, 3.00, 20.00, 2),
        ("Wet Paint - Single Color", "coating", 0, 10.00, 35.00, 3),
        ("Bead Blast", "finish", 1.00, 0, 15.00, 1),
        ("Tumble Deburr", "finish", 0.50, 0, 10.00, 1),
    ]
    
    for name, cat, per_part, per_sqft, minimum, days in default_finishes:
        existing = db.query(QuoteFinish).filter(QuoteFinish.name == name).first()
        if not existing:
            db.add(QuoteFinish(
                name=name,
                category=cat,
                price_per_part=per_part,
                price_per_sqft=per_sqft,
                minimum_charge=minimum,
                additional_days=days
            ))
    
    db.commit()
    return {"status": "ok", "message": "Default data seeded"}
