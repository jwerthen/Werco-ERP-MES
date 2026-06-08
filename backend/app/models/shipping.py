import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class ShipmentStatus(str, enum.Enum):
    PENDING = "pending"
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class Shipment(Base, TenantMixin):
    """Shipment header for shipping work orders to customers"""

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
