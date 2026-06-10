"""Create display_tokens (A0.5 TV wallboard: scoped display-JWT revocation anchors)

Revision ID: 050_display_tokens
Revises: 049_ai_usage_events
Create Date: 2026-06-10

Context
-------
A0.5 TV wallboard adds ``app/models/display_token.py::DisplayToken`` -- one row
per issued long-lived ``type="display"`` JWT for an unattended shop-floor TV.
The row is the revocation anchor: the wallboard auth dependency looks up the
JWT's ``jti`` here on every request and rejects the token when the row is
missing, revoked, or past ``expires_at``. Only the ``jti`` is stored, never the
JWT itself. Issuance/revocation are audit-logged through ``AuditService`` by
the service layer; this migration does not touch the tamper-evident
``audit_log`` table, writes no data, and backfills nothing.

Tenant / compliance shape
-------------------------
TenantMixin -> ``company_id`` Integer FK to ``companies.id``, NOT NULL, indexed
(``ix_display_tokens_company_id``) -- the same shape as ``ai_usage_events``
(049) and the 046 shipping tables. Every query against this table MUST be
company-scoped; the wallboard dependency derives the active company from this
row. No SoftDeleteMixin: revocation (``revoked`` / ``revoked_at`` /
``revoked_by``), not deletion, is the kill switch, so the issuance trail
survives by design. No OptimisticLockMixin (the only post-insert write is the
idempotent revoke flip).

Lock-step with the model (load-bearing)
---------------------------------------
Column list, the three FKs, and the three indexes below are kept byte-for-byte
in lock-step with the model so the ``create_all`` bootstrap path
(docs/DEVELOPMENT.md) and a Postgres ``alembic upgrade`` converge on the
IDENTICAL schema. Verified against autogenerate output, which emitted exactly
this DDL. Notes from that review:
- ``jti`` is ``unique=True, index=True`` on the model; SQLAlchemy materializes
  that as a single UNIQUE INDEX ``ix_display_tokens_jti`` (no separate
  UniqueConstraint), so this migration creates the unique index -- creating a
  named UniqueConstraint instead would diverge from create_all.
- ``revoked`` carries ``server_default='false'`` (matches the model and the
  SoftDeleteMixin ``is_deleted`` precedent); ``created_at`` has an app-side
  ``default=datetime.utcnow`` only -- NO server default, mirroring the model.
- All DateTime columns are naive UTC (no ``timezone=True``), like
  DowntimeEvent et al.; ``company_id`` sits last in column order because mixin
  columns are appended after the class's own columns.

Idempotent and reversible
-------------------------
- Upgrade guards ``create_table`` with ``_has_table`` and every
  ``create_index`` with ``_has_index`` (precedent: 046/048/049). This matters
  because bootstrap is ``create_all() -> stamp -> upgrade``
  (docs/DEVELOPMENT.md): a DB bootstrapped from the updated model already has
  the table (including the unique jti index) when this migration runs over the
  stamp, and the guards make that a clean no-op. Re-runs are likewise no-ops.
- Downgrade drops the indexes in reverse creation order (unique jti index
  included), then the table, all guarded, so it round-trips cleanly on
  Postgres and on the SQLite used for local dev / pytest.

Locking / operations note
-------------------------
Brand-new empty table: ``CREATE TABLE`` + index builds are instantaneous and
take no lock on any existing table. No backfill. No deploy-ordering
constraint: old application code never references the table; new code only
reads/writes it after this migration (or a create_all bootstrap) has run.

Revision id ``050_display_tokens`` is 18 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "050_display_tokens"
down_revision = "049_ai_usage_events"
branch_labels = None
depends_on = None

TABLE_NAME = "display_tokens"

# (index_name, columns, unique). Mirrors the model's index=True / unique=True /
# TenantMixin declarations so create_all and upgrade converge. ``jti`` is a
# UNIQUE index -- that IS the model's uniqueness constraint (see docstring).
INDEXES = [
    ("ix_display_tokens_company_id", ["company_id"], False),
    ("ix_display_tokens_id", ["id"], False),
    ("ix_display_tokens_jti", ["jti"], True),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def upgrade() -> None:
    # Guarded so a create_all-bootstrapped DB (table already present) and
    # re-runs no-op. Kept in lock-step with app/models/display_token.py::DisplayToken.
    if not _has_table(TABLE_NAME):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), nullable=False),
            # Human label for the screen ("North wall TV", "Weld bay monitor").
            sa.Column("label", sa.String(length=100), nullable=False),
            # JWT ID claim -- the revocation handle. Unique across all tenants
            # via the UNIQUE index ix_display_tokens_jti below.
            sa.Column("jti", sa.String(length=64), nullable=False),
            # Authoritative expiry; the auth dependency checks this column.
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            # Revocation trail -- revoke, never delete.
            sa.Column("revoked", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_by", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=False),
            # App-side default=datetime.utcnow only; no server default (model parity).
            sa.Column("created_at", sa.DateTime(), nullable=False),
            # TenantMixin -- non-null company scope, FK to companies.id.
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["revoked_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for index_name, columns, unique in INDEXES:
        if not _has_index(TABLE_NAME, index_name):
            op.create_index(index_name, TABLE_NAME, columns, unique=unique)


def downgrade() -> None:
    if not _has_table(TABLE_NAME):
        return
    for index_name, _columns, _unique in reversed(INDEXES):
        if _has_index(TABLE_NAME, index_name):
            op.drop_index(index_name, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
