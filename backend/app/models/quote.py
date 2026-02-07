from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class QuoteStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING = "pending"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONVERTED = "converted"


class Quote(Base):
    """Quote/Estimate for customer jobs"""
    __tablename__ = "quotes"
    
    id = Column(Integer, primary_key=True, index=True)
    quote_number = Column(String(50), unique=True, index=True, nullable=False)
    revision = Column(String(10), default="A")
    
    # Customer
    customer_name = Column(String(255), nullable=False)
    customer_contact = Column(String(255))
    customer_email = Column(String(255))
    customer_phone = Column(String(50))
    customer_po = Column(String(100))
    
    status = Column(SQLEnum(QuoteStatus), default=QuoteStatus.DRAFT)
    
    # Dates
    quote_date = Column(Date, default=datetime.utcnow)
    valid_until = Column(Date, nullable=True)
    
    # Totals
    subtotal = Column(Float, default=0.0)
    tax = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    
    # Lead time
    lead_time_days = Column(Integer)
    
    # Terms
    payment_terms = Column(String(100))
    notes = Column(Text)
    internal_notes = Column(Text)
    
    # Linked work order (if converted)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    lines = relationship("QuoteLine", back_populates="quote", cascade="all, delete-orphan")
    estimates = relationship("QuoteEstimate", back_populates="quote")


class QuoteLine(Base):
    """Line items for a quote"""
    __tablename__ = "quote_lines"
    
    id = Column(Integer, primary_key=True, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=False)
    line_number = Column(Integer, nullable=False)
    
    # Part reference (optional - could be custom description)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    
    # Line details
    description = Column(Text, nullable=False)
    quantity = Column(Float, nullable=False)
    unit_price = Column(Float, nullable=False)
    line_total = Column(Float, default=0.0)
    
    # Cost breakdown (for internal use)
    material_cost = Column(Float, default=0.0)
    labor_hours = Column(Float, default=0.0)
    labor_cost = Column(Float, default=0.0)
    overhead_cost = Column(Float, default=0.0)
    
    # Margin
    markup_pct = Column(Float, default=0.0)
    
    notes = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    quote = relationship("Quote", back_populates="lines")
    part = relationship("Part")
