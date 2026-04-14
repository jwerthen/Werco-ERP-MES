from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base


class Company(Base):
    """
    Represents a company/organization using the ERP system.
    Supports parent-child hierarchy (e.g., Werco -> acquired subsidiaries).
    """
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, index=True, nullable=False)
    logo_url = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    # Hierarchy: parent company (e.g., Werco) -> subsidiaries
    parent_company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)

    # Company-level settings
    timezone = Column(String(50), default="America/Chicago")
    address = Column(Text, nullable=True)
    phone = Column(String(50), nullable=True)
    website = Column(String(255), nullable=True)

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    parent = relationship("Company", remote_side=[id], backref="subsidiaries")
    users = relationship("User", back_populates="company")
