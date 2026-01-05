from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from datetime import datetime
from app.db.database import Base


class Customer(Base):
    """Customer master record"""
    __tablename__ = "customers"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    code = Column(String(50), unique=True, index=True)
    
    # Contact info
    contact_name = Column(String(255))
    email = Column(String(255))
    phone = Column(String(50))
    
    # Address
    address_line1 = Column(String(255))
    address_line2 = Column(String(255))
    city = Column(String(100))
    state = Column(String(50))
    zip_code = Column(String(20))
    country = Column(String(100), default="USA")
    
    # Shipping address (if different)
    ship_to_name = Column(String(255))
    ship_address_line1 = Column(String(255))
    ship_address_line2 = Column(String(255))
    ship_city = Column(String(100))
    ship_state = Column(String(50))
    ship_zip_code = Column(String(20))
    ship_country = Column(String(100))
    
    # Terms
    payment_terms = Column(String(100), default="Net 30")
    credit_limit = Column(Integer, default=0)
    
    # Requirements
    requires_coc = Column(Boolean, default=True)  # Certificate of Conformance
    requires_fai = Column(Boolean, default=False)  # First Article Inspection
    special_requirements = Column(Text)
    
    # Status
    is_active = Column(Boolean, default=True)
    
    notes = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
