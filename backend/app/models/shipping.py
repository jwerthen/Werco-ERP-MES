import enum
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, Date, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


class ShipmentStatus(str, enum.Enum):
    PENDING = "pending"
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class Shipment(Base, TenantMixin, SoftDeleteMixin):
    """Shipment header for shipping work orders to customers.

    Now also carries carrier-integration + financial data (rate-shop results,
    purchased labels/BOLs, costs, tracking status). All NEW money columns are
    ``Numeric(12, 2)`` (never Float); the pre-existing ``quantity_shipped`` /
    ``weight_lbs`` Float columns are left untouched for backward compatibility.
    Soft-deleted (never hard-deleted) so purchased-label records are preserved.
    """

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

    # --- Carrier integration (all NEW columns nullable -- backward compatible
    # with the legacy manual flow; the migration author adds these as ADD COLUMN). ---
    carrier_account_id = Column(Integer, ForeignKey("carrier_accounts.id"), nullable=True)
    ship_mode = Column(String(20), default="manual")  # "parcel" | "freight" | "manual"
    # Aggregator's shipment id -- the key inbound webhooks use to resolve the
    # owning tenant (NEVER trust caller-supplied company_id). Indexed for lookup.
    aggregator_shipment_id = Column(String(120), index=True, nullable=True)
    selected_rate_id = Column(String(120))
    service_code = Column(String(80))

    # Label / BOL artifacts stored via the existing Document model ("documents").
    label_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    bol_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)

    # Costs -- Numeric(12, 2), never Float.
    estimated_cost = Column(Numeric(12, 2))
    actual_cost = Column(Numeric(12, 2))
    cost_currency = Column(String(3), default="USD")

    label_purchased_at = Column(DateTime)
    voided_at = Column(DateTime)
    refund_status = Column(String(20))

    # Tracking sync state (flowed back from webhook / poll fallback).
    tracking_status = Column(String(30))
    tracking_status_detail = Column(String(255))
    last_tracking_sync_at = Column(DateTime)

    # Freight / LTL fields.
    freight_class = Column(String(10))
    nmfc_code = Column(String(20))
    pallet_count = Column(Integer)
    accessorials = Column(JSON)
    pro_number = Column(String(40))
    bol_number = Column(String(60))

    # Idempotency guard for buy-label/buy-bol. The migration author MUST add a
    # PARTIAL unique index uq_shipment_idempotency on (company_id, idempotency_key)
    # WHERE idempotency_key IS NOT NULL (so legacy NULL rows don't collide).
    idempotency_key = Column(String(80), nullable=True)

    work_order = relationship("WorkOrder")


class CertificateOfConformance(Base, TenantMixin):
    """Certificate of Conformance (CoC) -- a frozen-snapshot compliance artifact (G6-B).

    Design decision: the CoC is a DB **frozen snapshot**. The row stores the
    immutable certified facts (part/lot/serial/quantity snapshot + the full rendered
    content) at issue time; the PDF is rendered DETERMINISTICALLY on download from
    these stored facts -- there is no filesystem blob. A CoC is an APPEND-ONLY issued
    compliance record (like an audit entry), so it deliberately does NOT use
    SoftDeleteMixin.

    Idempotency: scoped per Shipment. The DB-enforced
    ``uq_coc_company_shipment`` unique constraint guarantees at most one CoC per
    (company, shipment), so a concurrent double-ship cannot mint two certificates --
    the loser raises ``IntegrityError`` which the issuing service treats as a no-op
    (mirrors the uq_wo_inventory_* idempotency precedent from migration 041).
    """

    __tablename__ = "certificates_of_conformance"
    __table_args__ = (
        UniqueConstraint("company_id", "shipment_id", name="uq_coc_company_shipment"),
        UniqueConstraint("company_id", "coc_number", name="uq_coc_company_number"),
    )

    id = Column(Integer, primary_key=True, index=True)

    coc_number = Column(String(50), nullable=False)

    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, index=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)

    # Frozen snapshot of the certified facts (captured at issue time, never mutated)
    customer_name = Column(String(255), nullable=True)
    customer_po = Column(String(100), nullable=True)
    part_number = Column(String(100), nullable=True)
    part_name = Column(String(255), nullable=True)
    revision = Column(String(50), nullable=True)  # part revision snapshot
    quantity = Column(Float, nullable=True)
    lot_number = Column(String(100), nullable=True)
    serial_numbers = Column(Text, nullable=True)  # JSON array snapshot
    conformance_statement = Column(Text, nullable=True)
    content_snapshot = Column(Text, nullable=True)  # JSON of the full rendered facts (immutable)

    issued_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    issued_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    shipment = relationship("Shipment")
    work_order = relationship("WorkOrder")


class ShipmentPackage(Base, TenantMixin, SoftDeleteMixin):
    """A single box or pallet within a shipment (parcel or LTL freight).

    Physical dimensions / weights are ``Numeric`` (never Float). Soft-deleted so
    package records tied to a purchased label/BOL are preserved.
    """

    __tablename__ = "shipment_packages"

    id = Column(Integer, primary_key=True, index=True)

    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False, index=True)

    sequence = Column(Integer)
    package_type = Column(String(20))  # e.g. "box" | "pallet"

    length_in = Column(Numeric(10, 2))
    width_in = Column(Numeric(10, 2))
    height_in = Column(Numeric(10, 2))
    weight_lbs = Column(Numeric(10, 2))

    tracking_number = Column(String(100))

    # Freight / LTL per-package classification.
    freight_class = Column(String(10))
    nmfc_code = Column(String(20))

    quantity = Column(Integer, default=1)

    created_at = Column(DateTime, default=datetime.utcnow)

    shipment = relationship("Shipment")


class ShipmentRateQuote(Base, TenantMixin):
    """A persisted rate-shop result for a shipment.

    Kept for compliance ("why this carrier / this price was chosen"); amounts are
    ``Numeric(12, 2)``, never Float.
    """

    __tablename__ = "shipment_rate_quotes"

    id = Column(Integer, primary_key=True, index=True)

    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False, index=True)

    provider_rate_id = Column(String(120))
    carrier = Column(String(100))
    service_code = Column(String(80))
    service_name = Column(String(120))
    mode = Column(String(20))  # "parcel" | "freight"

    amount = Column(Numeric(12, 2))
    currency = Column(String(3), default="USD")

    est_delivery_days = Column(Integer)
    est_delivery_date = Column(Date)

    is_selected = Column(Boolean, default=False)
    fetched_at = Column(DateTime, default=datetime.utcnow)

    created_at = Column(DateTime, default=datetime.utcnow)

    shipment = relationship("Shipment")


class ShipmentTrackingEvent(Base, TenantMixin):
    """An append-only tracking event for a shipment (from webhook or poll).

    De-duplicated by ``provider_event_id`` at the service layer. This is a
    historical record and is intentionally NOT soft-deletable.
    """

    __tablename__ = "shipment_tracking_events"

    id = Column(Integer, primary_key=True, index=True)

    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False, index=True)

    status = Column(String(30))
    status_detail = Column(String(255))
    occurred_at = Column(DateTime)
    location = Column(String(255))
    message = Column(Text)
    source = Column(String(20))  # "webhook" | "poll"
    provider_event_id = Column(String(120))

    created_at = Column(DateTime, default=datetime.utcnow)

    shipment = relationship("Shipment")
