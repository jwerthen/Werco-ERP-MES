from sqlalchemy import Column, Integer, String, Float, Text, Boolean, DateTime, JSON, Enum as SQLEnum
from datetime import datetime
import enum
from app.db.database import Base


class MaterialCategory(str, enum.Enum):
    STEEL = "steel"
    STAINLESS = "stainless"
    ALUMINUM = "aluminum"
    BRASS = "brass"
    COPPER = "copper"
    TITANIUM = "titanium"
    PLASTIC = "plastic"
    OTHER = "other"


class MachineType(str, enum.Enum):
    CNC_MILL_3AXIS = "cnc_mill_3axis"
    CNC_MILL_4AXIS = "cnc_mill_4axis"
    CNC_MILL_5AXIS = "cnc_mill_5axis"
    CNC_LATHE = "cnc_lathe"
    LASER_FIBER = "laser_fiber"
    LASER_CO2 = "laser_co2"
    PLASMA = "plasma"
    WATERJET = "waterjet"
    PRESS_BRAKE = "press_brake"
    PUNCH_PRESS = "punch_press"


class QuoteMaterial(Base):
    """Materials available for quoting with pricing"""
    __tablename__ = "quote_materials"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Basic info
    name = Column(String(255), nullable=False)  # e.g., "6061-T6 Aluminum"
    category = Column(SQLEnum(MaterialCategory), nullable=False)
    description = Column(Text)
    
    # For CNC - stock pricing (per cubic inch or pound)
    stock_price_per_cubic_inch = Column(Float, default=0.0)
    stock_price_per_pound = Column(Float, default=0.0)
    density_lb_per_cubic_inch = Column(Float, default=0.0)  # For weight calculations
    
    # For Sheet Metal - per square foot by gauge
    sheet_pricing = Column(JSON)  # {"10ga": 5.50, "12ga": 4.25, "14ga": 3.75, ...}
    
    # Cutting speed multiplier (1.0 = baseline, <1 = slower/harder)
    machinability_factor = Column(Float, default=1.0)
    
    # Markup
    material_markup_pct = Column(Float, default=20.0)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class QuoteMachine(Base):
    """Machine configurations for quoting"""
    __tablename__ = "quote_machines"
    
    id = Column(Integer, primary_key=True, index=True)
    
    name = Column(String(255), nullable=False)  # e.g., "Haas VF-2"
    machine_type = Column(SQLEnum(MachineType), nullable=False)
    description = Column(Text)
    
    # Hourly rates
    rate_per_hour = Column(Float, nullable=False)  # Shop rate including overhead
    setup_rate_per_hour = Column(Float)  # If different from run rate
    
    # For laser/plasma/waterjet - cutting speeds by material thickness
    # {"steel": {"10ga": 150, "12ga": 200}, "aluminum": {...}}
    cutting_speeds = Column(JSON)  # inches per minute
    
    # For press brake - time per bend
    bend_time_seconds = Column(Float, default=15.0)
    setup_time_per_bend_type = Column(Float, default=300.0)  # 5 min per unique bend
    
    # CNC specific
    typical_setup_hours = Column(Float, default=1.0)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class QuoteFinish(Base):
    """Finishing operations with pricing"""
    __tablename__ = "quote_finishes"
    
    id = Column(Integer, primary_key=True, index=True)
    
    name = Column(String(255), nullable=False)  # e.g., "Anodize Type II Clear"
    category = Column(String(100))  # plating, coating, heat_treat, etc.
    description = Column(Text)
    
    # Pricing options
    price_per_part = Column(Float, default=0.0)
    price_per_sqft = Column(Float, default=0.0)
    price_per_lb = Column(Float, default=0.0)
    minimum_charge = Column(Float, default=0.0)
    
    # Lead time impact
    additional_days = Column(Integer, default=0)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class QuoteSettings(Base):
    """Global quote settings"""
    __tablename__ = "quote_settings"
    
    id = Column(Integer, primary_key=True, index=True)
    setting_key = Column(String(100), unique=True, nullable=False)
    setting_value = Column(Text)
    setting_type = Column(String(50))  # number, text, json
    description = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Default settings to seed:
# - default_markup_pct: 35
# - minimum_order_charge: 150
# - rush_multiplier: 1.5
# - quantity_breaks: {"10": 0.95, "25": 0.90, "50": 0.85, "100": 0.80}
# - standard_lead_days: 10
# - tolerance_surcharges: {"+/-.005": 1.0, "+/-.001": 1.25, "+/-.0005": 1.5}
