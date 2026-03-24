from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class ChartType(str, enum.Enum):
    XBAR_R = "xbar_r"
    XBAR_S = "xbar_s"
    INDIVIDUAL_MR = "individual_mr"
    P_CHART = "p_chart"
    NP_CHART = "np_chart"
    C_CHART = "c_chart"
    U_CHART = "u_chart"


class SPCCharacteristic(Base):
    """SPC Characteristic - defines what measurement is being tracked"""
    __tablename__ = "spc_characteristics"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False)
    characteristic_type = Column(String(50), nullable=False)  # dimensional, weight, force, temperature, visual
    unit_of_measure = Column(String(50))
    specification_nominal = Column(Float, nullable=True)
    specification_usl = Column(Float, nullable=True)  # Upper Spec Limit
    specification_lsl = Column(Float, nullable=True)  # Lower Spec Limit
    chart_type = Column(SQLEnum(ChartType), default=ChartType.XBAR_R)
    subgroup_size = Column(Integer, default=5)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    operation_number = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    is_critical = Column(Boolean, default=False)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    part = relationship("Part")
    work_center = relationship("WorkCenter")
    control_limits = relationship("SPCControlLimit", back_populates="characteristic", cascade="all, delete-orphan")
    measurements = relationship("SPCMeasurement", back_populates="characteristic", cascade="all, delete-orphan")
    capability_studies = relationship("SPCProcessCapability", back_populates="characteristic", cascade="all, delete-orphan")


class SPCControlLimit(Base):
    """Calculated control limits for an SPC characteristic"""
    __tablename__ = "spc_control_limits"

    id = Column(Integer, primary_key=True, index=True)
    characteristic_id = Column(Integer, ForeignKey("spc_characteristics.id"), nullable=False)
    calculation_date = Column(DateTime, default=datetime.utcnow)
    ucl = Column(Float, nullable=False)  # Upper Control Limit
    lcl = Column(Float, nullable=False)  # Lower Control Limit
    center_line = Column(Float, nullable=False)
    ucl_range = Column(Float, nullable=True)
    lcl_range = Column(Float, nullable=True)
    center_line_range = Column(Float, nullable=True)
    sample_count = Column(Integer, nullable=False)
    is_current = Column(Boolean, default=True)
    calculated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    characteristic = relationship("SPCCharacteristic", back_populates="control_limits")


class SPCMeasurement(Base):
    """Individual measurement data point for SPC"""
    __tablename__ = "spc_measurements"

    id = Column(Integer, primary_key=True, index=True)
    characteristic_id = Column(Integer, ForeignKey("spc_characteristics.id"), nullable=False)
    subgroup_number = Column(Integer, nullable=False)
    measurement_value = Column(Float, nullable=False)
    sample_number = Column(Integer, nullable=False)  # Position within subgroup (1 to subgroup_size)
    measured_at = Column(DateTime, default=datetime.utcnow)
    measured_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    lot_number = Column(String(100), nullable=True)
    serial_number = Column(String(100), nullable=True)
    is_out_of_control = Column(Boolean, default=False)
    violation_rules = Column(String(255), nullable=True)  # Which Western Electric rules violated
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    characteristic = relationship("SPCCharacteristic", back_populates="measurements")
    work_order = relationship("WorkOrder")


class SPCProcessCapability(Base):
    """Cp/Cpk process capability study results"""
    __tablename__ = "spc_process_capabilities"

    id = Column(Integer, primary_key=True, index=True)
    characteristic_id = Column(Integer, ForeignKey("spc_characteristics.id"), nullable=False)
    study_date = Column(DateTime, default=datetime.utcnow)
    sample_count = Column(Integer, nullable=False)
    mean = Column(Float, nullable=False)
    std_dev = Column(Float, nullable=False)
    cp = Column(Float, nullable=True)
    cpk = Column(Float, nullable=True)
    pp = Column(Float, nullable=True)
    ppk = Column(Float, nullable=True)
    within_spec_pct = Column(Float, nullable=True)
    is_capable = Column(Boolean, default=False)
    performed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    characteristic = relationship("SPCCharacteristic", back_populates="capability_studies")
