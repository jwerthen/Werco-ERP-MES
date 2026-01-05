from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class BOMItemType(str, enum.Enum):
    MAKE = "make"  # Parts we manufacture
    BUY = "buy"  # Parts we purchase
    PHANTOM = "phantom"  # Sub-assembly that explodes into its components


class BOM(Base):
    """Bill of Materials - Top level BOM for a part/assembly"""
    __tablename__ = "boms"
    
    id = Column(Integer, primary_key=True, index=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, unique=True)
    revision = Column(String(20), default="A")
    description = Column(Text)
    
    # Status
    status = Column(String(50), default="draft")  # draft, released, obsolete
    is_active = Column(Boolean, default=True)
    
    # BOM Type for multi-level support
    bom_type = Column(String(50), default="standard")  # standard, phantom, configurable
    
    # Effectivity dates for AS9100D
    effective_date = Column(DateTime, nullable=True)
    obsolete_date = Column(DateTime, nullable=True)
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, nullable=True)
    approved_by = Column(Integer, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    
    # Relationships
    part = relationship("Part", back_populates="bom")
    items = relationship("BOMItem", back_populates="bom", cascade="all, delete-orphan", order_by="BOMItem.item_number")


class BOMItem(Base):
    """Individual line item in a BOM - supports multi-level nesting"""
    __tablename__ = "bom_items"
    
    id = Column(Integer, primary_key=True, index=True)
    bom_id = Column(Integer, ForeignKey("boms.id"), nullable=False)
    component_part_id = Column(Integer, ForeignKey("parts.id"), nullable=False)
    
    # Item details
    item_number = Column(Integer, nullable=False)  # Line item number (10, 20, 30...)
    quantity = Column(Float, nullable=False, default=1.0)
    item_type = Column(SQLEnum(BOMItemType), nullable=False)
    
    # Unit of measure for this line (may differ from part UOM)
    unit_of_measure = Column(String(20), default="each")
    
    # Reference designator for assemblies (e.g., "R1, R2, R3" for resistors)
    reference_designator = Column(String(255))
    
    # Find number - used in drawings (1, 2, 3...)
    find_number = Column(String(20))
    
    # Notes/instructions
    notes = Column(Text)
    
    # For operations - which work center processes this
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    operation_sequence = Column(Integer, default=10)  # 10, 20, 30...
    
    # Scrap factor for material planning
    scrap_factor = Column(Float, default=0.0)  # 0.05 = 5% scrap allowance
    
    # Lead time offset (days before parent is needed)
    lead_time_offset = Column(Integer, default=0)
    
    # Optional/alternate part flags
    is_optional = Column(Boolean, default=False)
    is_alternate = Column(Boolean, default=False)
    alternate_group = Column(String(50), nullable=True)  # Groups alternates together
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    bom = relationship("BOM", back_populates="items")
    component_part = relationship("Part", foreign_keys=[component_part_id])
    work_center = relationship("WorkCenter")
