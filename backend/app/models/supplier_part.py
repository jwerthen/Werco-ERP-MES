from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base


class SupplierPartMapping(Base):
    """Maps supplier/vendor part numbers to internal parts"""
    __tablename__ = "supplier_part_mappings"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # The barcode/part number from the supplier
    supplier_part_number = Column(String(255), index=True, nullable=False)
    
    # Optional: specific vendor this mapping applies to
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    
    # Our internal part
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False)
    
    # Additional info that might be on supplier label
    supplier_description = Column(Text)
    supplier_uom = Column(String(50))  # Their unit of measure
    conversion_factor = Column(Float, default=1.0)  # Convert their UOM to ours
    
    # Defaults for receiving
    default_location_id = Column(Integer, ForeignKey("inventory_locations.id"), nullable=True)
    
    is_active = Column(Boolean, default=True)
    notes = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    part = relationship("Part")
    vendor = relationship("Vendor")
