"""Scoped display tokens for unattended shop-floor TV wallboards (A0.5).

A DisplayToken row is the revocation anchor for a long-lived ``type="display"``
JWT. The JWT carries the row's ``jti``; the wallboard auth dependency
(``app.api.deps.get_display_or_user``) looks the row up on every request and
rejects the token when the row is missing, revoked, or past ``expires_at`` —
so an admin can kill a wall-mounted TV's access without touching the device.

Security properties (compliance-relevant):
- Display tokens authenticate ONLY the read-only wallboard endpoint. Every
  other dependency goes through ``verify_token``, which rejects any JWT whose
  ``type`` claim is not ``"access"`` — a display token can never act as a user.
- Tenant-scoped via ``TenantMixin`` (non-null ``company_id``); the dependency
  derives the active company from this row, not from the client.
- The JWT itself is never stored — only its ``jti``. Issuance and revocation
  are audit-logged through ``AuditService`` (see
  ``app.services.display_token_service``).
- TV pairing uses a short-lived one-time setup code: only its SHA-256 hex
  (``setup_code_hash``) is stored — never the plaintext code — alongside its
  own expiry (``setup_code_expires_at``, ~15 minutes) and a single-use marker
  (``setup_code_used_at``).
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class DisplayToken(Base, TenantMixin):
    __tablename__ = "display_tokens"

    id = Column(Integer, primary_key=True, index=True)

    # Human label for the screen ("North wall TV", "Weld bay monitor").
    label = Column(String(100), nullable=False)

    # JWT ID claim — the revocation handle. Unique across all tenants.
    jti = Column(String(64), nullable=False, unique=True, index=True)

    # Authoritative expiry (the JWT carries the same instant in ``exp``); the
    # dependency checks THIS column so expiry holds even if the JWT were
    # minted with a different lifetime. Naive UTC, like DowntimeEvent et al.
    expires_at = Column(DateTime, nullable=False)

    # Revocation flag — revoke, never delete, so the issuance trail survives.
    revoked = Column(Boolean, nullable=False, default=False, server_default='false')
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # One-time TV pairing (setup code). SHA-256 hex of the NORMALIZED code —
    # the plaintext code is NEVER stored; the claim endpoint hashes what the
    # TV types and looks the row up by this column.
    setup_code_hash = Column(String(64), nullable=True, index=True)

    # Setup-code expiry — codes live ~15 minutes, independent of the token's
    # own ``expires_at``. Naive UTC, like ``expires_at``.
    setup_code_expires_at = Column(DateTime, nullable=True)

    # Single-use marker — set on first successful claim; a set value means the
    # code can never be redeemed again.
    setup_code_used_at = Column(DateTime, nullable=True)

    # Optional per-TV work-center-type preset ("machining", "welding", ...).
    # The claim response hands it back so the person at the TV never types a
    # query param.
    dept = Column(String(50), nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    creator = relationship("User", foreign_keys=[created_by])
    revoker = relationship("User", foreign_keys=[revoked_by])
