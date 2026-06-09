from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint

from app.db.database import Base
from app.db.mixins import OptimisticLockMixin, SoftDeleteMixin, TenantMixin


class CarrierAccount(Base, TenantMixin, SoftDeleteMixin, OptimisticLockMixin):
    """Per-company multi-carrier aggregator credentials (EasyPost / Zenkraft / ...).

    COMPLIANCE / SECURITY:
    - Secrets are stored Fernet-encrypted (``encrypted_api_key`` /
      ``webhook_secret_encrypted``). Plaintext keys are NEVER persisted, logged,
      serialized to API responses (expose only last4), or placed in audit /
      operational-event payloads.
    - Tenant-scoped via ``TenantMixin``; soft-deleted (never hard-deleted) because
      a carrier account may be referenced by purchased labels / BOLs.
    - ``carrier_refs`` holds the aggregator's bring-your-own-carrier account
      references, e.g. ``{"fedex": "...", "ups": "...", "fedex_freight": "..."}``.
      These are opaque account handles, not secrets.
    """

    __tablename__ = "carrier_accounts"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_carrier_account_company_name"),)

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String(120), nullable=False)
    provider = Column(String(50), nullable=False)  # "easypost" | "zenkraft"
    environment = Column(String(20), default="production")  # "production" | "test"

    # Encrypted secrets -- never expose in API responses (last4 only) or logs.
    encrypted_api_key = Column(Text, nullable=False)
    webhook_secret_encrypted = Column(Text, nullable=True)

    # Opaque bring-your-own-carrier account references (NOT secrets).
    carrier_refs = Column(JSON, default=dict)  # {"fedex", "ups", "fedex_freight": "<provider account ref>"}

    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CompanyShippingProfile(Base, TenantMixin, OptimisticLockMixin):
    """Per-company shipping defaults (one row per company).

    ``Company.address`` is free-text and unusable for carrier labels, so the
    ship-from origin is decomposed into discrete fields here. Package defaults
    pre-fill the Schedule-Shipment flow.

    SAFETY: ``allow_carrier_egress`` is the per-company kill switch for any
    outbound carrier call that transmits customer data (address validation,
    rate-shop, buy-label, buy-bol, schedule-pickup). It DEFAULTS FALSE -- the
    service refuses to make those calls until a human explicitly enables egress
    (CUI / DoD-contract sign-off). A pure credential ``test-connection`` (sends no
    customer data) is exempt.
    """

    __tablename__ = "company_shipping_profiles"
    __table_args__ = (UniqueConstraint("company_id", name="uq_company_shipping_profile_company"),)

    id = Column(Integer, primary_key=True, index=True)

    # Decomposed ship-from origin (label-grade address).
    ship_from_name = Column(String(255))
    ship_from_company = Column(String(255))
    ship_from_phone = Column(String(50))
    ship_from_email = Column(String(255))
    ship_from_street1 = Column(String(255))
    ship_from_street2 = Column(String(255))
    ship_from_city = Column(String(100))
    ship_from_state = Column(String(50))
    ship_from_zip = Column(String(20))
    ship_from_country = Column(String(2), default="US")

    # Package defaults (physical units -- Numeric, never Float).
    default_package_weight_lbs = Column(Numeric(10, 2))
    default_package_length_in = Column(Numeric(10, 2))
    default_package_width_in = Column(Numeric(10, 2))
    default_package_height_in = Column(Numeric(10, 2))

    # Per-company customer-data egress kill switch -- defaults OFF.
    allow_carrier_egress = Column(Boolean, nullable=False, default=False, server_default="false")

    created_at = Column(DateTime, default=datetime.utcnow)
