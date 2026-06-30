"""Shared-PIN sign-in stations for the visitor sign-in tablet.

A ``SigninStation`` is the company-binding + revocation anchor for an
unattended visitor sign-in tablet at a facility entrance. It is the PIN-based
twin of ``app.models.display_token.DisplayToken`` (the wallboard display
token), with two differences:

- The tablet is unlocked with a **shared numeric PIN** (bcrypt-hashed at rest
  in ``pin_hash``) rather than a one-time link. The PIN mints a scoped
  ``type="signin"`` JWT (see ``app.core.security.create_signin_token``).
- The station authorizes exactly TWO scoped writes (visitor sign-in and
  sign-out) via ``app.api.deps.get_signin_principal`` — nothing else.

Security properties (compliance-relevant):
- Signin tokens authenticate ONLY the two visitor-write endpoints. Every other
  dependency goes through ``verify_token``, which rejects any JWT whose
  ``type`` claim is not ``"access"`` — a signin token can never act as a user.
- Tenant-scoped via ``TenantMixin`` (non-null ``company_id``); the auth
  dependency derives the active company from THIS row, never from the client's
  ``cid`` claim.
- The PIN is never stored in plaintext and never echoed back; only its bcrypt
  hash lands in ``pin_hash``. Revocation is a status flip (``revoked``), never
  a row delete, so the issuance trail survives.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class SigninStation(Base, TenantMixin):
    __tablename__ = "signin_stations"

    id = Column(Integer, primary_key=True, index=True)

    # Human label for the tablet ("Lobby Tablet"); becomes the audit actor string.
    label = Column(String(100), nullable=False)

    # bcrypt hash of the shared numeric PIN (reuse core/security password hasher).
    pin_hash = Column(String(255), nullable=False)

    # Revocation flag — checked on every station-login and every signin-token
    # request. Revoke, never delete, so the issuance trail survives.
    revoked = Column(Boolean, nullable=False, default=False, server_default='false')
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Updated on every successful station-login.
    last_used_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    creator = relationship("User", foreign_keys=[created_by])
    revoker = relationship("User", foreign_keys=[revoked_by])
