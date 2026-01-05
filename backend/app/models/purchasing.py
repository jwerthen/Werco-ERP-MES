from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class POStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    PARTIAL = "partial"
    RECEIVED = "received"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class Vendor(Base):
    """Supplier/Vendor master"""
    __tablename__ = "vendors"
    
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    
    # Contact info
    contact_name = Column(String(255))
    email = Column(String(255))
    phone = Column(String(50))
    
    # Address
    address_line1 = Column(String(255))
    address_line2 = Column(String(255))
    city = Column(String(100))
    state = Column(String(50))
    postal_code = Column(String(20))
    country = Column(String(100), default="USA")
    
    # Terms
    payment_terms = Column(String(100))
    lead_time_days = Column(Integer, default=14)
    
    # Quality
    is_approved = Column(Boolean, default=False)
    approval_date = Column(Date, nullable=True)
    quality_rating = Column(Float, nullable=True)  # 1-5 scale
    
    # Certifications
    is_as9100_certified = Column(Boolean, default=False)
    is_iso9001_certified = Column(Boolean, default=False)
    
    is_active = Column(Boolean, default=True)
    notes = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    purchase_orders = relationship("PurchaseOrder", back_populates="vendor")


class PurchaseOrder(Base):
    """Purchase Order header"""
    __tablename__ = "purchase_orders"
    
    id = Column(Integer, primary_key=True, index=True)
    po_number = Column(String(50), unique=True, index=True, nullable=False)
    
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False)
    
    status = Column(SQLEnum(POStatus), default=POStatus.DRAFT)
    
    # Dates
    order_date = Column(Date, nullable=True)
    required_date = Column(Date, nullable=True)
    expected_date = Column(Date, nullable=True)
    
    # Totals
    subtotal = Column(Float, default=0.0)
    tax = Column(Float, default=0.0)
    shipping = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    
    # Shipping
    ship_to = Column(Text)
    shipping_method = Column(String(100))
    
    # Reference
    mrp_action_id = Column(Integer, nullable=True)  # If created from MRP
    
    notes = Column(Text)
    
    # Approval workflow
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    vendor = relationship("Vendor", back_populates="purchase_orders")
    lines = relationship("PurchaseOrderLine", back_populates="purchase_order", cascade="all, delete-orphan")


class PurchaseOrderLine(Base):
    """Purchase Order line item"""
    __tablename__ = "purchase_order_lines"
    
    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    line_number = Column(Integer, nullable=False)
    
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False)
    
    quantity_ordered = Column(Float, nullable=False)
    quantity_received = Column(Float, default=0.0)
    
    unit_price = Column(Float, nullable=False)
    line_total = Column(Float, default=0.0)
    
    required_date = Column(Date, nullable=True)
    is_closed = Column(Boolean, default=False)
    
    notes = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    purchase_order = relationship("PurchaseOrder", back_populates="lines")
    part = relationship("Part")
    receipts = relationship("POReceipt", back_populates="po_line")


class ReceiptStatus(str, enum.Enum):
    PENDING_INSPECTION = "pending_inspection"
    INSPECTED = "inspected"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    QUARANTINE = "quarantine"


class POReceipt(Base):
    """Receipt against a PO line - tracks each delivery"""
    __tablename__ = "po_receipts"
    
    id = Column(Integer, primary_key=True, index=True)
    receipt_number = Column(String(50), unique=True, index=True, nullable=False)
    
    po_line_id = Column(Integer, ForeignKey("purchase_order_lines.id"), nullable=False)
    
    # Quantities
    quantity_received = Column(Float, nullable=False)
    quantity_accepted = Column(Float, default=0.0)
    quantity_rejected = Column(Float, default=0.0)
    
    # Traceability (AS9100D)
    lot_number = Column(String(100))
    serial_numbers = Column(Text)  # Comma-separated or JSON
    heat_number = Column(String(100))  # For metals
    cert_number = Column(String(100))  # Cert of conformance
    
    # Location
    location_id = Column(Integer, ForeignKey("inventory_locations.id"), nullable=True)
    
    # Inspection
    status = Column(SQLEnum(ReceiptStatus), default=ReceiptStatus.PENDING_INSPECTION)
    requires_inspection = Column(Boolean, default=True)
    inspected_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    inspected_at = Column(DateTime, nullable=True)
    inspection_notes = Column(Text)
    
    # Packing slip / shipping info
    packing_slip_number = Column(String(100))
    carrier = Column(String(100))
    tracking_number = Column(String(100))
    
    received_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow)
    
    notes = Column(Text)
    
    po_line = relationship("PurchaseOrderLine", back_populates="receipts")
    location = relationship("InventoryLocation")
