"""Per-company thermal-label print profile (ProxyBox / WHTP203e).

One row per company holds the configuration needed to send a rendered 4x6 PDF to
a ProxyBox Zero (pbxz.io) bridge that drives a Westinghouse WHTP203e direct-thermal
printer.

COMPLIANCE / SECURITY (mirrors ``CompanyShippingProfile``):
- The ProxyBox API key is stored Fernet-encrypted (``encrypted_api_key``) via the
  shared carriers crypto helper. The plaintext key is NEVER persisted, logged,
  serialized to API responses (only ``api_key_last4`` is shown), or placed in
  audit / operational-event payloads. It is decrypted in-memory only at the
  moment a print is submitted.
- Tenant-scoped via ``TenantMixin`` (one row per company; ``UniqueConstraint`` on
  ``company_id``).
- ``allow_print_egress`` is the per-company kill switch for any outbound call to
  the ProxyBox tunnel. It DEFAULTS FALSE and ``nullable=False`` -- the print
  service refuses to transmit to the printer bridge until a human explicitly
  enables egress.
- ``auto_print_on_receipt`` gates the auto-print-on-receipt background job; it is
  independent of the egress kill switch (both must be on for an auto-print to
  occur).
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.db.database import Base
from app.db.mixins import OptimisticLockMixin, TenantMixin


class CompanyPrintProfile(Base, TenantMixin, OptimisticLockMixin):
    """Per-company ProxyBox / thermal-printer configuration (one row per company)."""

    __tablename__ = "company_print_profiles"
    __table_args__ = (UniqueConstraint("company_id", name="uq_company_print_profile_company"),)

    id = Column(Integer, primary_key=True, index=True)

    # ProxyBox bridge connection. ``proxybox_base_url`` is the FULL base including
    # the API version path, e.g. "https://pbx-xxxx.pbxz.cloud/api/v1".
    proxybox_base_url = Column(String(255), nullable=True)
    # The target printer identifier registered on the ProxyBox device.
    proxybox_target = Column(String(120), nullable=True)

    # Encrypted ProxyBox API key -- never exposed in API responses (last4 only) or logs.
    encrypted_api_key = Column(Text, nullable=True)
    api_key_last4 = Column(String(8), nullable=True)  # display only

    # Print defaults.
    default_paper_size = Column(String(20), default="4x6")
    default_copies = Column(Integer, default=1)

    # Auto-print the receiving label as a background job after each PO receipt.
    auto_print_on_receipt = Column(Boolean, nullable=False, default=False, server_default="false")

    # Per-company outbound-egress kill switch for the ProxyBox tunnel -- defaults OFF.
    allow_print_egress = Column(Boolean, nullable=False, default=False, server_default="false")

    is_active = Column(Boolean, default=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ------------------------------------------------------------------
    # Encrypted-key helpers (mirror the carrier-account pattern).
    # ------------------------------------------------------------------

    def set_api_key(self, plaintext: str) -> None:
        """Encrypt + store the ProxyBox API key and capture its last-4 for display.

        SECURITY: the plaintext is encrypted via the shared carriers crypto helper
        (``INTEGRATION_ENCRYPTION_KEY``) and is never retained on the instance.
        """
        from app.services.carriers.crypto import encrypt_secret

        plaintext = (plaintext or "").strip()
        if not plaintext:
            self.clear_api_key()
            return
        self.encrypted_api_key = encrypt_secret(plaintext)
        self.api_key_last4 = plaintext[-4:]

    def clear_api_key(self) -> None:
        """Remove the stored key + its last-4 mask."""
        self.encrypted_api_key = None
        self.api_key_last4 = None

    def get_api_key(self) -> str:
        """Decrypt the stored ProxyBox API key for IN-MEMORY use only.

        Raises ``ValueError`` when no key is configured. The result must never be
        logged or returned in an API response.
        """
        if not self.encrypted_api_key:
            raise ValueError("No ProxyBox API key configured for this company")
        from app.services.carriers.crypto import decrypt_secret

        return decrypt_secret(self.encrypted_api_key)
