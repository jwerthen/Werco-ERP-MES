"""Estimate workbench models — Cut/Bend shop data + fab/buyout/machined lines.

See docs/ESTIMATE_WORKBENCH.md for the integration contract.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import OptimisticLockMixin, SoftDeleteMixin, TenantMixin


class CutBendTableKind(str, enum.Enum):
    LASER_SPEED = "laser_speed"
    PIERCE_TIME = "pierce_time"
    BRAKE_TIME = "brake_time"
    GAUGE_REFERENCE = "gauge_reference"
    WELD_REFERENCE = "weld_reference"


class ConfidenceLevel(str, enum.Enum):
    CONFIRMED = "confirmed"  # 3/3
    MAJORITY = "majority"  # 2/3
    REVIEW = "review"  # ≤1/3


class CutBendTable(Base, TenantMixin):
    """One of the five shop-physics lookup tables (per company)."""

    __tablename__ = "cut_bend_tables"
    __table_args__ = (
        UniqueConstraint("company_id", "kind", name="uq_cut_bend_tables_company_kind"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # CutBendTableKind values as plain VARCHAR (house pattern — no PG enum types)
    kind = Column(String(40), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    rows = relationship(
        "CutBendRow",
        back_populates="table",
        cascade="all, delete-orphan",
        order_by="CutBendRow.sort_order",
    )


class CutBendRow(Base, TenantMixin):
    """Thickness / gauge / fillet band row. Null family cells = past capacity."""

    __tablename__ = "cut_bend_rows"

    id = Column(Integer, primary_key=True, index=True)
    table_id = Column(Integer, ForeignKey("cut_bend_tables.id"), nullable=False, index=True)
    sort_order = Column(Integer, nullable=False, default=0)

    # Laser / pierce / brake key
    thickness_in = Column(Float, nullable=True, index=True)

    # Gauge reference key
    gauge = Column(Integer, nullable=True)

    # Laser speed / gauge columns (None = past capacity for that family)
    mild_steel = Column(Float, nullable=True)
    stainless = Column(Float, nullable=True)
    aluminum = Column(Float, nullable=True)

    # Pierce / brake single value
    value = Column(Float, nullable=True)

    # Weld reference
    fillet_leg_in = Column(Float, nullable=True)
    arc_in_per_min = Column(Float, nullable=True)
    min_per_in = Column(Float, nullable=True)

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    table = relationship("CutBendTable", back_populates="rows")


class QuoteAssembly(Base, TenantMixin, SoftDeleteMixin, OptimisticLockMixin):
    """Sub-job under a QuoteEstimate (e.g. TC-2-EXT)."""

    __tablename__ = "quote_assemblies"

    id = Column(Integer, primary_key=True, index=True)
    quote_estimate_id = Column(
        Integer, ForeignKey("quote_estimates.id"), nullable=False, index=True
    )
    name = Column(String(255), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    assembly_labor_hrs = Column(Float, nullable=False, default=0.0)
    electrical_labor_hrs = Column(Float, nullable=False, default=0.0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    estimate = relationship("QuoteEstimate", back_populates="assemblies")
    fab_line_items = relationship(
        "QuoteFabLineItem",
        back_populates="assembly",
        cascade="all, delete-orphan",
        order_by="QuoteFabLineItem.sort_order",
    )
    buyout_line_items = relationship(
        "QuoteBuyoutLineItem",
        back_populates="assembly",
        cascade="all, delete-orphan",
        order_by="QuoteBuyoutLineItem.sort_order",
    )


class QuoteFabLineItem(Base, TenantMixin, SoftDeleteMixin, OptimisticLockMixin):
    """Flat-pattern fab detail — geometry in, 4 cost buckets out."""

    __tablename__ = "quote_fab_line_items"

    id = Column(Integer, primary_key=True, index=True)
    assembly_id = Column(Integer, ForeignKey("quote_assemblies.id"), nullable=False, index=True)
    sort_order = Column(Integer, nullable=False, default=0)

    part_number = Column(String(120), nullable=True, index=True)
    detail_name = Column(String(255), nullable=False)
    material = Column(String(120), nullable=False, default="")
    material_family_override = Column(String(20), nullable=True)  # mild|stainless|aluminum
    qty = Column(Integer, nullable=False, default=1)

    thickness_in = Column(Float, nullable=True)
    width_in = Column(Float, nullable=True)
    length_in = Column(Float, nullable=True)
    cut_length_in = Column(Float, nullable=True)
    pierce_count = Column(Integer, nullable=False, default=0)
    bend_count = Column(Integer, nullable=False, default=0)
    weld_length_in = Column(Float, nullable=True)
    weld_minutes_ea = Column(Float, nullable=True)

    include_material = Column(Boolean, nullable=False, default=True)
    include_laser = Column(Boolean, nullable=False, default=True)
    include_brake = Column(Boolean, nullable=False, default=True)
    include_weld = Column(Boolean, nullable=False, default=True)

    # Cached computed outputs (recomputed on save / recalc)
    weight_ea_lb = Column(Float, nullable=True)
    material_cost = Column(Float, nullable=False, default=0.0)
    laser_cost = Column(Float, nullable=False, default=0.0)
    laser_hours = Column(Float, nullable=False, default=0.0)
    brake_cost = Column(Float, nullable=False, default=0.0)
    brake_hours = Column(Float, nullable=False, default=0.0)
    weld_cost = Column(Float, nullable=False, default=0.0)
    weld_hours = Column(Float, nullable=False, default=0.0)
    line_total = Column(Float, nullable=False, default=0.0)
    calc_warnings = Column(JSON, nullable=True)
    calc_errors = Column(JSON, nullable=True)

    # ConfidenceLevel values as plain VARCHAR
    confidence = Column(String(20), nullable=False, default=ConfidenceLevel.REVIEW.value, index=True)
    verification_note = Column(Text, nullable=True)
    field_confidence = Column(JSON, nullable=True)  # per-field votes (Phase 3+)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    assembly = relationship("QuoteAssembly", back_populates="fab_line_items")


class QuoteBuyoutLineItem(Base, TenantMixin, SoftDeleteMixin, OptimisticLockMixin):
    """Purchased hardware / components on an assembly."""

    __tablename__ = "quote_buyout_line_items"

    id = Column(Integer, primary_key=True, index=True)
    assembly_id = Column(Integer, ForeignKey("quote_assemblies.id"), nullable=False, index=True)
    sort_order = Column(Integer, nullable=False, default=0)

    category = Column(String(100), nullable=True)
    vendor = Column(String(255), nullable=True)
    part_number = Column(String(120), nullable=True, index=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True, index=True)
    description = Column(Text, nullable=False)
    qty = Column(Float, nullable=False, default=1.0)
    unit_cost = Column(Float, nullable=False, default=0.0)
    extended_cost = Column(Float, nullable=False, default=0.0)
    price_source = Column(Text, nullable=True)

    confidence = Column(String(20), nullable=False, default=ConfidenceLevel.REVIEW.value, index=True)
    verification_note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    assembly = relationship("QuoteAssembly", back_populates="buyout_line_items")
    part = relationship("Part")


class QuoteMachinedLineItem(Base, TenantMixin, SoftDeleteMixin, OptimisticLockMixin):
    """Turned / milled part on an estimate (not nested under assembly)."""

    __tablename__ = "quote_machined_line_items"

    id = Column(Integer, primary_key=True, index=True)
    quote_estimate_id = Column(
        Integer, ForeignKey("quote_estimates.id"), nullable=False, index=True
    )
    sort_order = Column(Integer, nullable=False, default=0)

    part_number = Column(String(120), nullable=True, index=True)
    description = Column(String(255), nullable=False)
    material = Column(String(120), nullable=False, default="")
    qty = Column(Integer, nullable=False, default=1)
    stock_dia_in = Column(Float, nullable=True)
    stock_length_in = Column(Float, nullable=True)
    turning_minutes = Column(Float, nullable=False, default=0.0)
    milling_minutes = Column(Float, nullable=False, default=0.0)

    weight_ea_lb = Column(Float, nullable=True)
    material_cost = Column(Float, nullable=False, default=0.0)
    turning_cost = Column(Float, nullable=False, default=0.0)
    turning_hours = Column(Float, nullable=False, default=0.0)
    milling_cost = Column(Float, nullable=False, default=0.0)
    milling_hours = Column(Float, nullable=False, default=0.0)
    line_total = Column(Float, nullable=False, default=0.0)

    confidence = Column(String(20), nullable=False, default=ConfidenceLevel.REVIEW.value, index=True)
    verification_note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    estimate = relationship("QuoteEstimate", back_populates="machined_line_items")
