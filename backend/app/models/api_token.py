"""Long-lived, revocable, per-user API tokens for non-interactive clients.

An ApiToken row is the revocation anchor for a long-lived ``type="api"`` JWT —
the credential a bot or an agent (the "Werco Assistant", an MCP client such as
Cursor) presents instead of the 15-minute interactive access token. The JWT
carries the row's ``jti``; ``app.api.deps.get_current_user`` looks the row up on
EVERY request and rejects the token when the row is missing, revoked, past
``expires_at``, or when its ``user_id`` / ``company_id`` claims disagree with the
row — so an Admin can kill a bot's access without touching the bot.

Security properties (compliance-relevant):
- A token is bound to exactly ONE real user (``user_id``) and is only as
  powerful as that user: it carries the user's role and ``require_role`` decides
  exactly as it does for the SPA. There is no god role, no scope widening, and
  no bypass of any endpoint gate.
- ``scope`` and ``ai_external`` (migration ``119_api_token_scope``) can only
  NARROW what the bound user can do, never widen it. ``scope = "read_only"``
  refuses every non-GET route and every route under a tag in
  ``WERCO_MCP_READ_ONLY_DENY_TAGS`` (``app.api.deps.get_current_user``, and again at
  the MCP door); ``ai_external`` marks a read-only token whose answers leave the
  company for an outside AI service (only ever set together with ``read_only``).
  Both are fixed at mint: there is no update verb -- changing either is revoke
  and re-mint. ``"full"`` / ``false`` are the server defaults, so every row
  minted before ``119`` reads exactly as it behaved.
- The ROW is authoritative, never the JWT's claims. The auth path pins
  ``_active_company_id`` to this row's ``company_id`` — a forged or replayed
  claim can never widen tenancy, and a platform admin's context switch never
  leaks into a token.
- Path-fenced: an API token is refused on every ``/api/v1/auth`` verb (no
  refresh, no logout, no kiosk-badge mint, no company switch) and on
  ``/api/v1/api-tokens`` itself — minting, listing and revoking tokens is an
  Admin's interactive act, never something a token can do to itself.
- Tenant-scoped via ``TenantMixin`` (non-null ``company_id``, indexed); every
  read goes through ``tenant_query``.
- The JWT itself is NEVER stored — only its ``jti``. The plaintext is returned
  exactly once, at issuance, and must never appear in a log line, an audit row,
  or an error message. Issuance and revocation are audit-logged through
  ``AuditService`` with metadata only (see ``app.services.api_token_service``).
- Revocation is the tombstone. Rows are never physically deleted (who held
  access is a record), and a revoke is one-way: the first revocation's reason,
  actor and instant are the record and a second call refuses rather than
  overwrites. Deliberately NOT ``SoftDeleteMixin`` — like ``inventory_combines``,
  there is no third state to forget to filter; ``revoked`` is the whole story.
- ``expires_at`` NULL means the token never expires (the owner's default for a
  standing bot credential); a non-NULL value is checked from THIS column, not
  from the JWT's ``exp``, so expiry holds even if the JWT were minted with a
  different lifetime. Rotating ``SECRET_KEY`` invalidates every API token at
  once — that is a feature.
- ``last_used_at`` is a coarse liveness marker touched at most once per five
  minutes by the auth path, never on every call, so a busy bot does not turn
  every read into a write.

Lock-step with migration ``088_api_tokens``: every column and all six indexes
are declared here AND mirrored in the migration (the 042/078/079/080/085/087
convention — an index declared only in a migration is skipped entirely by the
``create_all`` + ``alembic stamp`` bootstrap; ``tests/test_migration_088_api_tokens.py``
pins the two equal).
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class ApiTokenScope(str, enum.Enum):
    """What a token may do AS its user. ``FULL`` is everything the user's role allows;
    ``READ_ONLY`` narrows that to reads outside the denied tags. Never wider than the user."""

    FULL = "full"
    READ_ONLY = "read_only"


# Byte-identical to the condition in ``119_api_token_scope`` (the lock-step test compares
# the two strings). A String + CHECK rather than a native ENUM: the recent house style,
# and it never needs an ``ALTER TYPE`` on live Postgres.
API_TOKEN_SCOPE_CHECK_NAME = "ck_api_tokens_scope"
API_TOKEN_SCOPE_CHECK_CONDITION = "scope IN ('full', 'read_only')"


class ApiToken(Base, TenantMixin):
    __tablename__ = "api_tokens"
    __table_args__ = (
        # "Which tokens does this user hold?" — the list read and the per-user
        # revoke sweep, tenant-led.
        Index("ix_api_tokens_company_user", "company_id", "user_id"),
        # "Which live tokens exist in this company?" — the default list read
        # (include_revoked=false), tenant-led.
        Index("ix_api_tokens_company_revoked", "company_id", "revoked"),
        # The only two scopes there are; anything else would be a value no gate knows.
        CheckConstraint(API_TOKEN_SCOPE_CHECK_CONDITION, name=API_TOKEN_SCOPE_CHECK_NAME),
    )

    id = Column(Integer, primary_key=True, index=True)

    # The ONLY identity the token can act as. NOT NULL: a token with no user has
    # no role, and therefore no power.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Human label for the holder ("Werco Assistant — Grok Bot").
    label = Column(String(100), nullable=False)

    # JWT ID claim — the revocation handle and the ONLY thing stored about the
    # JWT. Unique across all tenants (the auth path resolves the row by jti
    # before it knows the company).
    jti = Column(String(64), nullable=False, unique=True, index=True)

    # Authoritative expiry; NULL = never expires. The auth path checks THIS
    # column, not the JWT's ``exp``. Naive UTC, like DisplayToken.
    expires_at = Column(DateTime, nullable=True)

    # Revocation trail — revoke, never delete, so who held access survives.
    revoked = Column(Boolean, nullable=False, default=False, server_default='false')
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Required on the revoke verb; the first revocation's reason is the record.
    revoke_reason = Column(String(255), nullable=True)

    # Coarse liveness marker — touched at most once per five minutes.
    last_used_at = Column(DateTime, nullable=True)

    # The Admin who issued it.
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Fixed at mint (119). ``full`` = the user's whole role; ``read_only`` = GET only and
    # never a denied tag. The server default is what every pre-119 row reads.
    scope = Column(String(16), nullable=False, default=ApiTokenScope.FULL.value, server_default="full")
    # Fixed at mint (119). True only on a read-only token whose answers go to an outside
    # AI service; the export-control rules key on it. Plain-string server default, like
    # ``revoked`` (never ``sa.text("false")``).
    ai_external = Column(Boolean, nullable=False, default=False, server_default="false")

    user = relationship("User", foreign_keys=[user_id])
    creator = relationship("User", foreign_keys=[created_by])
    revoker = relationship("User", foreign_keys=[revoked_by])

    # The list response's correlation handle: enough of the jti to tell two
    # tokens apart in a listing and an audit row, never enough to matter --
    # the jti is not the secret (the signature is), and it mints nothing
    # without SECRET_KEY. NOT a column; the migration lock-step test sees
    # columns and indexes only.
    JTI_PREFIX_LENGTH = 8

    @property
    def jti_prefix(self) -> str:
        return (self.jti or "")[: self.JTI_PREFIX_LENGTH]
