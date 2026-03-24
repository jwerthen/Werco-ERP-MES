from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime, date
import enum
from app.db.database import Base


class ToolStatus(str, enum.Enum):
    AVAILABLE = "available"
    IN_USE = "in_use"
    MAINTENANCE = "maintenance"
    RETIRED = "retired"
    LOST = "lost"
    DAMAGED = "damaged"


class ToolType(str, enum.Enum):
    CUTTING_TOOL = "cutting_tool"
    FIXTURE = "fixture"
    JIG = "jig"
    GAUGE = "gauge"
    MOLD = "mold"
    DIE = "die"
    DRILL_BIT = "drill_bit"
    END_MILL = "end_mill"
    INSERT = "insert"
    HOLDER = "holder"
    CLAMP = "clamp"
    OTHER = "other"


class Tool(Base):
    """Tool & fixture inventory"""
    __tablename__ = "tools"

    id = Column(Integer, primary_key=True, index=True)
    tool_id = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    tool_type = Column(SQLEnum(ToolType), default=ToolType.OTHER)
    description = Column(Text, nullable=True)

    manufacturer = Column(String(255), nullable=True)
    model_number = Column(String(100), nullable=True)
    serial_number = Column(String(100), nullable=True)

    status = Column(SQLEnum(ToolStatus), default=ToolStatus.AVAILABLE)
    location = Column(String(255), nullable=True)

    current_work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    current_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    max_life_hours = Column(Float, nullable=True)
    current_life_hours = Column(Float, default=0)
    max_life_cycles = Column(Integer, nullable=True)
    current_life_cycles = Column(Integer, default=0)
    life_remaining_pct = Column(Float, nullable=True)

    purchase_date = Column(Date, nullable=True)
    purchase_cost = Column(Float, default=0)

    last_inspection_date = Column(Date, nullable=True)
    next_inspection_date = Column(Date, nullable=True)
    inspection_interval_days = Column(Integer, nullable=True)

    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    current_work_center = relationship("WorkCenter", foreign_keys=[current_work_center_id])
    current_user = relationship("User", foreign_keys=[current_user_id])
    checkouts = relationship("ToolCheckout", back_populates="tool", order_by="ToolCheckout.checked_out_at.desc()")
    usage_logs = relationship("ToolUsageLog", back_populates="tool", order_by="ToolUsageLog.usage_date.desc()")


class ToolCheckout(Base):
    """Check-in / check-out history"""
    __tablename__ = "tool_checkouts"

    id = Column(Integer, primary_key=True, index=True)
    tool_id = Column(Integer, ForeignKey("tools.id"), nullable=False)

    checked_out_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    checked_out_at = Column(DateTime, default=datetime.utcnow)
    checked_in_at = Column(DateTime, nullable=True)

    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)

    condition_out = Column(String(50), default="good")
    condition_in = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    tool = relationship("Tool", back_populates="checkouts")
    user = relationship("User", foreign_keys=[checked_out_by])
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])


class ToolUsageLog(Base):
    """Track usage against work orders"""
    __tablename__ = "tool_usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    tool_id = Column(Integer, ForeignKey("tools.id"), nullable=False)

    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)

    usage_hours = Column(Float, default=0)
    usage_cycles = Column(Integer, default=0)
    usage_date = Column(Date, default=date.today)

    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    tool = relationship("Tool", back_populates="usage_logs")
    user = relationship("User", foreign_keys=[recorded_by])
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
