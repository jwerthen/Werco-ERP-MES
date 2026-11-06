from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


class Customer(Base, SoftDeleteMixin, TenantMixin):
    """Customer master record.

    ``name`` and ``code`` are unique PER COMPANY (migration 115), never globally:
    a global unique let one tenant's customer name or code block another tenant's
    and disclosed that it existed. Neither constraint has a partial predicate, so a
    soft-deleted customer keeps its name and code. The plain ``ix_customers_name`` /
    ``ix_customers_code`` indexes stay for single-column lookups. The constraints
    and indexes here must stay byte-identical to
    ``alembic/versions/115_customers_unique_per_company.py`` (the create_all ->
    stamp -> upgrade bootstrap builds them from this class).
    """

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_customers_company_name"),
        UniqueConstraint("company_id", "code", name="uq_customers_company_code"),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    code = Column(String(50), index=True)

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
