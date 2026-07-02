"""Shared-PIN crew-station kiosks bound to a work center.

A ``KioskStation`` is the company-binding + revocation anchor for an unattended
shop-floor crew tablet at a work center. It is the work-center-bound twin of
``app.models.signin_station.SigninStation`` (the visitor sign-in tablet):

- The tablet is unlocked with a **shared numeric PIN** (bcrypt-hashed at rest in
  ``pin_hash``). The PIN mints a scoped ``type="kiosk"`` JWT (see
  ``app.core.security.create_kiosk_token``).
- The station token authorizes exactly TWO things via
  ``app.api.deps.get_kiosk_or_user`` and the badge-token mint
  (``POST /auth/kiosk-badge-token``): reading its OWN work center's queue
  (roster-enriched) and exchanging a badge scan for a short-lived,
  kiosk-scoped OPERATOR access token. Nothing else.

Security properties (compliance-relevant):
- Kiosk tokens authenticate ONLY through ``get_kiosk_or_user`` and the badge
  mint. Every other dependency goes through ``verify_token``, which rejects any
  JWT whose ``type`` claim is not ``"access"`` — a kiosk station token can never
  act as a user.
- Tenant-scoped via ``TenantMixin`` (non-null ``company_id``); the auth
  dependency derives the active company from THIS row, never from the client's
  ``cid`` claim.
- ``work_center_id`` is non-null: the station is physically bound to one work
  center and may only read that work center's queue.
- The PIN is never stored in plaintext and never echoed back; only its bcrypt
  hash lands in ``pin_hash``. Revocation is a status flip (``revoked``), never
  a row delete, so the issuance trail survives.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class KioskStation(Base, TenantMixin):
    __tablename__ = "kiosk_stations"

    id = Column(Integer, primary_key=True, index=True)

    # Human label for the tablet ("Weld Bay Kiosk"); surfaces on the kiosk header.
    label = Column(String(100), nullable=False)

    # The work center this station is physically bound to. A station may only
    # read ITS OWN work center's queue (enforced in get_kiosk_or_user callers).
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False, index=True)

    # bcrypt hash of the shared numeric PIN (reuse core/security password hasher).
    pin_hash = Column(String(255), nullable=False)

    # Revocation flag — checked on every station-login, every kiosk-token
    # request, and every badge-token mint. Revoke, never delete, so the
    # issuance trail survives.
    revoked = Column(Boolean, nullable=False, default=False, server_default='false')
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Updated on every successful station-login.
    last_used_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
    creator = relationship("User", foreign_keys=[created_by])
    revoker = relationship("User", foreign_keys=[revoked_by])
