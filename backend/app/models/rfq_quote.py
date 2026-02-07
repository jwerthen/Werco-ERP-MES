from datetime import datetime
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.db.database import Base


class RfqPackage(Base):
    """Uploaded RFQ package and file bundle for AI sheet-metal estimating."""

    __tablename__ = "rfq_packages"

    id = Column(Integer, primary_key=True, index=True)
    rfq_number = Column(String(50), unique=True, index=True, nullable=False)

    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    customer_name = Column(String(255), nullable=True)
    rfq_reference = Column(String(100), nullable=True, index=True)
    status = Column(String(50), default="uploaded", index=True)

    package_metadata = Column(JSON, nullable=True)
    parsing_warnings = Column(JSON, nullable=True)

    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    files = relationship("RfqPackageFile", back_populates="rfq_package", cascade="all, delete-orphan")
    estimates = relationship("QuoteEstimate", back_populates="rfq_package", cascade="all, delete-orphan")


class RfqPackageFile(Base):
    """Individual file inside an RFQ package."""

    __tablename__ = "rfq_package_files"

    id = Column(Integer, primary_key=True, index=True)
    rfq_package_id = Column(Integer, ForeignKey("rfq_packages.id"), nullable=False, index=True)

    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_ext = Column(String(20), nullable=False, index=True)
    mime_type = Column(String(120), nullable=True)
    file_size = Column(Integer, nullable=False)

    parse_status = Column(String(50), default="pending", index=True)
    parse_error = Column(Text, nullable=True)
    extracted_summary = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    rfq_package = relationship("RfqPackage", back_populates="files")


class QuoteEstimate(Base):
    """Internal estimate generated from RFQ package parsing and costing."""

    __tablename__ = "quote_estimates"

    id = Column(Integer, primary_key=True, index=True)
    rfq_package_id = Column(Integer, ForeignKey("rfq_packages.id"), nullable=False, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=True, index=True)

    version = Column(Integer, default=1)
    currency = Column(String(10), default="USD")

    material_total = Column(Float, default=0.0)
    hardware_consumables_total = Column(Float, default=0.0)
    outside_services_total = Column(Float, default=0.0)
    shop_labor_oh_total = Column(Float, default=0.0)
    margin_total = Column(Float, default=0.0)
    grand_total = Column(Float, default=0.0)

    lead_time_min_days = Column(Integer, nullable=True)
    lead_time_max_days = Column(Integer, nullable=True)
    lead_time_confidence = Column(Float, default=0.0)

    confidence_score = Column(Float, default=0.0)
    confidence_detail = Column(JSON, nullable=True)
    assumptions = Column(JSON, nullable=True)
    missing_specs = Column(JSON, nullable=True)
    source_attribution = Column(JSON, nullable=True)
    internal_breakdown = Column(JSON, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    rfq_package = relationship("RfqPackage", back_populates="estimates")
    quote = relationship("Quote", back_populates="estimates")
    line_summaries = relationship("QuoteLineSummary", back_populates="quote_estimate", cascade="all, delete-orphan")
    price_snapshots = relationship("PriceSnapshot", back_populates="quote_estimate", cascade="all, delete-orphan")


class QuoteLineSummary(Base):
    """Per-part summarized estimate output without operation-time line items."""

    __tablename__ = "quote_line_summaries"

    id = Column(Integer, primary_key=True, index=True)
    quote_estimate_id = Column(Integer, ForeignKey("quote_estimates.id"), nullable=False, index=True)

    part_number = Column(String(120), nullable=True, index=True)
    part_name = Column(String(255), nullable=False)
    quantity = Column(Float, default=1)
    material = Column(String(120), nullable=True)
    thickness = Column(String(60), nullable=True)
    flat_area = Column(Float, nullable=True)  # in^2
    cut_length = Column(Float, nullable=True)  # inches
    bend_count = Column(Integer, nullable=True)
    hole_count = Column(Integer, nullable=True)
    finish = Column(String(120), nullable=True)
    weld_required = Column(Boolean, default=False)
    assembly_required = Column(Boolean, default=False)
    part_total = Column(Float, default=0.0)

    confidence = Column(JSON, nullable=True)
    sources = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)

    quote_estimate = relationship("QuoteEstimate", back_populates="line_summaries")


class PriceSnapshot(Base):
    """Price inputs used for estimate calculations and fallback cache tracking."""

    __tablename__ = "price_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    quote_estimate_id = Column(Integer, ForeignKey("quote_estimates.id"), nullable=True, index=True)
    rfq_package_id = Column(Integer, ForeignKey("rfq_packages.id"), nullable=True, index=True)

    snapshot_scope = Column(String(40), default="estimate", index=True)  # estimate, cache
    price_type = Column(String(60), nullable=False, index=True)  # material, hardware, consumable, finish
    item_code = Column(String(120), nullable=True, index=True)
    material = Column(String(120), nullable=True, index=True)
    thickness = Column(String(60), nullable=True, index=True)
    unit = Column(String(30), default="each")
    unit_price = Column(Float, nullable=False)
    currency = Column(String(10), default="USD")

    supplier = Column(String(255), nullable=False)
    source_name = Column(String(255), nullable=False)
    source_url = Column(String(500), nullable=True)
    is_fallback = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    raw_data = Column(JSON, nullable=True)

    fetched_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True, index=True)

    quote_estimate = relationship("QuoteEstimate", back_populates="price_snapshots")
