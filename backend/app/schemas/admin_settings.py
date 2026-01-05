from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.models.quote_config import MaterialCategory, MachineType, ProcessType, CostUnit


# ============ MATERIALS ============

class MaterialBase(BaseModel):
    name: str
    category: MaterialCategory
    description: Optional[str] = None
    stock_price_per_cubic_inch: float = 0.0
    stock_price_per_pound: float = 0.0
    density_lb_per_cubic_inch: float = 0.0
    sheet_pricing: Optional[Dict[str, float]] = None
    machinability_factor: float = 1.0
    material_markup_pct: float = 20.0


class MaterialCreate(MaterialBase):
    pass


class MaterialUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[MaterialCategory] = None
    description: Optional[str] = None
    stock_price_per_cubic_inch: Optional[float] = None
    stock_price_per_pound: Optional[float] = None
    density_lb_per_cubic_inch: Optional[float] = None
    sheet_pricing: Optional[Dict[str, float]] = None
    machinability_factor: Optional[float] = None
    material_markup_pct: Optional[float] = None
    is_active: Optional[bool] = None


class MaterialResponse(MaterialBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ============ MACHINES ============

class MachineBase(BaseModel):
    name: str
    machine_type: MachineType
    description: Optional[str] = None
    rate_per_hour: float
    setup_rate_per_hour: Optional[float] = None
    cutting_speeds: Optional[Dict[str, Any]] = None
    bend_time_seconds: float = 15.0
    setup_time_per_bend_type: float = 300.0
    typical_setup_hours: float = 1.0


class MachineCreate(MachineBase):
    pass


class MachineUpdate(BaseModel):
    name: Optional[str] = None
    machine_type: Optional[MachineType] = None
    description: Optional[str] = None
    rate_per_hour: Optional[float] = None
    setup_rate_per_hour: Optional[float] = None
    cutting_speeds: Optional[Dict[str, Any]] = None
    bend_time_seconds: Optional[float] = None
    setup_time_per_bend_type: Optional[float] = None
    typical_setup_hours: Optional[float] = None
    is_active: Optional[bool] = None


class MachineResponse(MachineBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ============ FINISHES ============

class FinishBase(BaseModel):
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    price_per_part: float = 0.0
    price_per_sqft: float = 0.0
    price_per_lb: float = 0.0
    minimum_charge: float = 0.0
    additional_days: int = 0


class FinishCreate(FinishBase):
    pass


class FinishUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    price_per_part: Optional[float] = None
    price_per_sqft: Optional[float] = None
    price_per_lb: Optional[float] = None
    minimum_charge: Optional[float] = None
    additional_days: Optional[int] = None
    is_active: Optional[bool] = None


class FinishResponse(FinishBase):
    id: int
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


# ============ LABOR RATES ============

class LaborRateBase(BaseModel):
    name: str
    rate_per_hour: float
    description: Optional[str] = None


class LaborRateCreate(LaborRateBase):
    pass


class LaborRateUpdate(BaseModel):
    name: Optional[str] = None
    rate_per_hour: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class LaborRateResponse(LaborRateBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ============ WORK CENTER RATES ============

class WorkCenterRateUpdate(BaseModel):
    hourly_rate: float


class WorkCenterRateResponse(BaseModel):
    id: int
    code: str
    name: str
    work_center_type: str
    hourly_rate: float
    is_active: bool
    
    class Config:
        from_attributes = True


# ============ OUTSIDE SERVICES ============

class OutsideServiceBase(BaseModel):
    name: str
    vendor_name: Optional[str] = None
    process_type: ProcessType
    default_cost: float = 0.0
    cost_unit: CostUnit = CostUnit.PER_PART
    minimum_charge: float = 0.0
    typical_lead_days: int = 5
    description: Optional[str] = None


class OutsideServiceCreate(OutsideServiceBase):
    pass


class OutsideServiceUpdate(BaseModel):
    name: Optional[str] = None
    vendor_name: Optional[str] = None
    process_type: Optional[ProcessType] = None
    default_cost: Optional[float] = None
    cost_unit: Optional[CostUnit] = None
    minimum_charge: Optional[float] = None
    typical_lead_days: Optional[int] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class OutsideServiceResponse(OutsideServiceBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ============ SETTINGS ============

class SettingUpdate(BaseModel):
    value: str
    setting_type: str = "text"
    description: Optional[str] = None


class SettingResponse(BaseModel):
    setting_key: str
    setting_value: str
    setting_type: str
    description: Optional[str] = None
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ============ AUDIT LOG ============

class AuditLogResponse(BaseModel):
    id: int
    entity_type: str
    entity_id: Optional[int] = None
    entity_name: Optional[str] = None
    action: str
    field_changed: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    changed_by: Optional[int] = None
    changed_at: datetime
    
    class Config:
        from_attributes = True


class AuditLogWithUser(AuditLogResponse):
    user_name: Optional[str] = None
