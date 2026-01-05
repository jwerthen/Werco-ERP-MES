from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
from app.models.bom import BOMItemType


class BOMItemBase(BaseModel):
    component_part_id: int
    item_number: int
    quantity: float = 1.0
    item_type: BOMItemType
    unit_of_measure: str = "each"
    reference_designator: Optional[str] = None
    find_number: Optional[str] = None
    notes: Optional[str] = None
    work_center_id: Optional[int] = None
    operation_sequence: int = 10
    scrap_factor: float = 0.0
    lead_time_offset: int = 0
    is_optional: bool = False
    is_alternate: bool = False
    alternate_group: Optional[str] = None


class BOMItemCreate(BOMItemBase):
    pass


class BOMItemUpdate(BaseModel):
    quantity: Optional[float] = None
    item_type: Optional[BOMItemType] = None
    unit_of_measure: Optional[str] = None
    reference_designator: Optional[str] = None
    find_number: Optional[str] = None
    notes: Optional[str] = None
    work_center_id: Optional[int] = None
    operation_sequence: Optional[int] = None
    scrap_factor: Optional[float] = None
    lead_time_offset: Optional[int] = None
    is_optional: Optional[bool] = None
    is_alternate: Optional[bool] = None
    alternate_group: Optional[str] = None


class ComponentPartInfo(BaseModel):
    """Embedded part info for BOM item responses"""
    id: int
    part_number: str
    name: str
    revision: str
    part_type: str
    has_bom: bool = False
    
    class Config:
        from_attributes = True


class BOMItemResponse(BOMItemBase):
    id: int
    bom_id: int
    component_part: Optional[ComponentPartInfo] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class BOMItemWithChildren(BOMItemResponse):
    """BOM item with nested children for multi-level explosion"""
    children: List["BOMItemWithChildren"] = []
    level: int = 0
    extended_quantity: float = 0.0  # quantity * parent quantities
    
    class Config:
        from_attributes = True


class BOMBase(BaseModel):
    part_id: int
    revision: str = "A"
    description: Optional[str] = None
    bom_type: str = "standard"


class BOMCreate(BOMBase):
    items: List[BOMItemCreate] = []


class BOMUpdate(BaseModel):
    revision: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    bom_type: Optional[str] = None
    effective_date: Optional[datetime] = None


class PartInfo(BaseModel):
    """Embedded part info for BOM responses"""
    id: int
    part_number: str
    name: str
    revision: str
    part_type: str
    
    class Config:
        from_attributes = True


class BOMResponse(BOMBase):
    id: int
    status: str
    is_active: bool
    effective_date: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    part: Optional[PartInfo] = None
    items: List[BOMItemResponse] = []
    
    class Config:
        from_attributes = True


class BOMExploded(BaseModel):
    """Fully exploded multi-level BOM"""
    bom_id: int
    part_id: int
    part_number: str
    part_name: str
    revision: str
    total_levels: int
    items: List[BOMItemWithChildren]
    
    class Config:
        from_attributes = True


class BOMFlatItem(BaseModel):
    """Flattened BOM item for reports/MRP"""
    level: int
    item_number: int
    find_number: Optional[str]
    part_id: int
    part_number: str
    part_name: str
    part_type: str
    item_type: BOMItemType
    quantity_per: float
    extended_quantity: float
    unit_of_measure: str
    scrap_factor: float
    lead_time_offset: int
    is_optional: bool
    is_alternate: bool
    has_children: bool


class BOMFlattened(BaseModel):
    """Flattened BOM for tabular display"""
    bom_id: int
    part_number: str
    part_name: str
    revision: str
    total_items: int
    total_unique_parts: int
    items: List[BOMFlatItem]


# Required for self-referencing model
BOMItemWithChildren.model_rebuild()
