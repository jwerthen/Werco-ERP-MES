from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class ShipmentStatus(str, enum.Enum):
    PENDING = "pending"
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class Shipment(Base):
    """Shipment header for shipping work orders to customers"""
    __tablename__ = "shipments"
    
    id = Column(Integer, primary_key=True, index=True)
    shipment_number = Column(String(50), unique=True, index=True, nullable=False)
    
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False)
    
    status = Column(SQLEnum(ShipmentStatus), default=ShipmentStatus.PENDING)
    
    # Customer info
    ship_to_name = Column(String(255))
    ship_to_address = Column(Text)
    ship_to_city = Column(String(100))
    ship_to_state = Column(String(50))
    ship_to_zip = Column(String(20))
    
    # Shipping details
    carrier = Column(String(100))
    service_type = Column(String(100))
    tracking_number = Column(String(100))
    
    # Quantities
    quantity_shipped = Column(Float, default=0.0)
    
    # Weights/dimensions
    weight_lbs = Column(Float)
    num_packages = Column(Integer, default=1)
    
    # Dates
    ship_date = Column(Date, nullable=True)
    estimated_delivery = Column(Date, nullable=True)
    actual_delivery = Column(Date, nullable=True)
    
    # Packing slip
    packing_slip_number = Column(String(50))
    packing_notes = Column(Text)
    
    # Certification
    cert_of_conformance = Column(Boolean, default=False)
    
    shipped_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    work_order = relationship("WorkOrder")
