"""
Models for Analytics Module - Report Templates
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base


class ReportTemplate(Base):
    """Saved custom report templates"""
    __tablename__ = "report_templates"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    
    # Report configuration stored as JSON
    data_source = Column(String(50), nullable=False)
    columns = Column(JSON, nullable=False)
    filters = Column(JSON, default=[])
    group_by = Column(JSON, default=[])
    sort = Column(JSON, default=[])
    
    # Sharing
    is_shared = Column(Boolean, default=False)
    
    # Audit
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    creator = relationship("User")


class KPITarget(Base):
    """Configurable KPI targets"""
    __tablename__ = "kpi_targets"
    
    id = Column(Integer, primary_key=True, index=True)
    kpi_name = Column(String(100), nullable=False, unique=True)
    target_value = Column(String(100), nullable=False)  # String to support % or absolute
    target_type = Column(String(20), default="gte")  # gte, lte, eq
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)  # Optional per-WC target
    
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
