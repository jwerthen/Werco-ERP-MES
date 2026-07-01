"""Create signin_stations + visitor_logs (visitor sign-in tablet)

Revision ID: 056_visitor_logs
Revises: 055_wo_scrap_reason
Create Date: 2026-06-30

Context
-------
The visitor sign-in tablet (branch ``feat/visitor-signin``) adds two new
tenant-scoped tables:

  * ``signin_stations`` -- ``app/models/signin_station.py::SigninStation``. The
    PIN-based, company-binding + revocation anchor for an unattended entrance
    tablet; a structural twin of ``display_tokens`` (050). Revoke, never delete.
  * ``visitor_logs`` -- ``app/models/visitor_log.py::VisitorLog``. One row per
    visitor presence record (sign-in / sign-out). SoftDeleteMixin (the
    attendance record survives for audit) + TenantMixin.

``visitor_logs.signin_station_id`` is an FK to ``signin_stations.id``, so
``signin_stations`` MUST be created first. This migration writes no data and
never touches the tamper-evident ``audit_log`` table; issuance/sign-in/sign-out
are audit-logged by the service layer through ``AuditService``.

Native enum types (load-bearing)
--------------------------------
``visitor_logs`` declares two native ``SQLEnum`` columns over ``str``-backed
``enum.Enum`` classes (mirroring ``downtime``):

  * ``visitorstatus``  -- columns ``status``
  * ``visitorpurpose`` -- column ``purpose``

SQLAlchemy stores the UPPERCASE MEMBER NAMES for a ``str``-backed Enum on
Postgres (verified against create_all; same behavior as ``certificationtype``
in 043) -- NOT the lowercase ``.value`` strings. The label lists below are kept
byte-for-byte in lock-step with the model so a defensively-created type matches
the one ``create_all`` builds:

  * ``visitorstatus``  = SIGNED_IN, SIGNED_OUT
  * ``visitorpurpose`` = MEETING, DELIVERY, CONTRACTOR, INTERVIEW, AUDIT, OTHER

Autogenerate does NOT reliably emit ``CREATE TYPE`` DDL for these, so the types
are created EXPLICITLY and idempotently here (``checkfirst=True``) BEFORE the
``visitor_logs`` table, and the columns reference them with ``create_type=False``
so ``create_table`` never tries to (re-)create the type. The ``purpose`` /
``status`` columns carry NO server_default (Python-side default only), matching
the model. The types are dropped in ``downgrade`` AFTER the table.

Tenant / compliance shape
-------------------------
Both tables are TenantMixin -> ``company_id`` Integer FK ``companies.id`` NOT
NULL, indexed (``ix_*_company_id``); ``company_id`` sits LAST in column order
because mixin columns are appended after the class's own columns. Every query
against these tables MUST be company-scoped. ``visitor_logs`` is SoftDeleteMixin
(``is_deleted`` NOT NULL ``server_default='false'`` indexed, ``deleted_at``
``DateTime(timezone=True)``, ``deleted_by`` plain Integer -- NO FK, matching the
mixin); reads filter ``is_deleted == False``. ``signin_stations`` has no
SoftDeleteMixin: revocation (``revoked`` / ``revoked_at`` / ``revoked_by``), not
deletion, is the kill switch, so the issuance trail survives.

DateTime columns are all naive UTC (no ``timezone=True``) EXCEPT
``visitor_logs.deleted_at`` (``DateTime(timezone=True)`` from SoftDeleteMixin) --
matched per-column. Booleans ``revoked`` / ``safety_acknowledged`` /
``is_deleted`` carry ``server_default='false'`` (model + SoftDeleteMixin
precedent); ``created_at`` / ``signed_in_at`` have app-side
``default=datetime.utcnow`` only -- NO server default.

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), NOT a
bare ``upgrade head`` on an empty DB. So a DB bootstrapped from the updated
models already has BOTH tables (and both enum types) when this migration runs
over the stamp.

- Upgrade guards each ``create_table`` with ``_has_table`` and every
  ``create_index`` with ``_has_index`` (precedent 046/048/049/050); the enum
  types are created with ``checkfirst=True`` (precedent 043/007). All three make
  a create_all-bootstrapped DB and re-runs clean no-ops.
- Downgrade drops indexes (reverse order) then the table for each table, then
  the two enum types (``checkfirst=True``), all guarded -- in reverse
  dependency order (``visitor_logs`` before ``signin_stations``; enum types
  last). Round-trips cleanly on Postgres and on the SQLite used for local dev /
  pytest (where SQLEnum renders as VARCHAR and the native-type create/drop is
  dialect-guarded to a no-op).

Locking / operations note
-------------------------
Brand-new empty tables: ``CREATE TYPE`` + ``CREATE TABLE`` + index builds are
instantaneous and take no lock on any existing table. No backfill. No
deploy-ordering constraint beyond signin_stations-before-visitor_logs (handled
in-migration): old application code never references either table; new code only
reads/writes them after this migration (or a create_all bootstrap) has run.

Revision id ``056_visitor_logs`` is 16 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "056_visitor_logs"
down_revision = "055_wo_scrap_reason"
branch_labels = None
depends_on = None

STATIONS_TABLE = "signin_stations"
LOGS_TABLE = "visitor_logs"

# Native enum types declared by app/models/visitor_log.py. SQLAlchemy stores the
# UPPERCASE member NAMES for a str-backed Enum on Postgres (verified against
# create_all) -- kept byte-for-byte in lock-step with the model so a defensively
# created type matches the one create_all builds.
VISITOR_STATUS_NAME = "visitorstatus"
VISITOR_STATUS_LABELS = ["SIGNED_IN", "SIGNED_OUT"]

VISITOR_PURPOSE_NAME = "visitorpurpose"
VISITOR_PURPOSE_LABELS = [
    "MEETING",
    "DELIVERY",
    "CONTRACTOR",
    "INTERVIEW",
    "AUDIT",
    "OTHER",
]

# (index_name, columns, unique). Mirrors index=True / TenantMixin / SoftDeleteMixin
# declarations so create_all and upgrade converge.
STATIONS_INDEXES = [
    ("ix_signin_stations_company_id", ["company_id"], False),
    ("ix_signin_stations_id", ["id"], False),
]

LOGS_INDEXES = [
    ("ix_visitor_logs_company_id", ["company_id"], False),
    ("ix_visitor_logs_id", ["id"], False),
    ("ix_visitor_logs_is_deleted", ["is_deleted"], False),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _enum_type(name: str, labels):
    """Native enum reference that NEVER auto-creates/drops itself.

    On Postgres this binds the existing named type; on SQLite SQLEnum renders as
    a VARCHAR check, matching what create_all emits from the model. ``create_type``
    is False so create_table/drop_table never touch the type -- the explicit
    ``_create_enums`` / ``_drop_enums`` passes own its lifecycle.
    """
    return postgresql.ENUM(*labels, name=name, create_type=False)


def _create_enums() -> None:
    """Idempotently create the two native enum types (Postgres only).

    ``checkfirst=True`` makes this a no-op when create_all already built the type
    (bootstrap path) or on a re-run. On SQLite there is no native enum type, so
    the create is dialect-guarded away.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    postgresql.ENUM(*VISITOR_STATUS_LABELS, name=VISITOR_STATUS_NAME).create(bind, checkfirst=True)
    postgresql.ENUM(*VISITOR_PURPOSE_LABELS, name=VISITOR_PURPOSE_NAME).create(bind, checkfirst=True)


def _drop_enums() -> None:
    """Idempotently drop the two native enum types (Postgres only), after the table."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    postgresql.ENUM(*VISITOR_PURPOSE_LABELS, name=VISITOR_PURPOSE_NAME).drop(bind, checkfirst=True)
    postgresql.ENUM(*VISITOR_STATUS_LABELS, name=VISITOR_STATUS_NAME).drop(bind, checkfirst=True)


def _create_signin_stations() -> None:
    # Kept in lock-step with app/models/signin_station.py::SigninStation.
    if not _has_table(STATIONS_TABLE):
        op.create_table(
            STATIONS_TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            # Human label ("Lobby Tablet") -- becomes the audit actor string.
            sa.Column("label", sa.String(length=100), nullable=False),
            # bcrypt hash of the shared numeric PIN -- never plaintext.
            sa.Column("pin_hash", sa.String(length=255), nullable=False),
            # Revocation trail -- revoke, never delete.
            sa.Column("revoked", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_by", sa.Integer(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            # App-side default=datetime.utcnow only; no server default (model parity).
            sa.Column("created_at", sa.DateTime(), nullable=False),
            # TenantMixin -- non-null company scope, FK to companies.id, LAST.
            sa.Column("company_id", sa.Integer(), nullable=False),
            # FK clause order mirrors create_all's emission order (model
            # column-discovery order) so the rendered DDL matches byte-for-byte;
            # clause order is semantically irrelevant to the resulting schema.
            sa.ForeignKeyConstraint(["revoked_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for index_name, columns, unique in STATIONS_INDEXES:
        if not _has_index(STATIONS_TABLE, index_name):
            op.create_index(index_name, STATIONS_TABLE, columns, unique=unique)


def _create_visitor_logs() -> None:
    # Kept in lock-step with app/models/visitor_log.py::VisitorLog.
    if not _has_table(LOGS_TABLE):
        op.create_table(
            LOGS_TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            # Visitor identity (CUI PII -- never egress externally).
            sa.Column("visitor_name", sa.String(length=120), nullable=False),
            sa.Column("visitor_company", sa.String(length=120), nullable=True),
            sa.Column("visitor_phone", sa.String(length=40), nullable=True),
            # Host -- free-text plus optional matched internal user.
            sa.Column("host_name", sa.String(length=120), nullable=True),
            sa.Column("host_user_id", sa.Integer(), nullable=True),
            # Native enum (created above with create_type=False so we don't
            # re-create it here); no server default -- Python-side default only.
            sa.Column("purpose", _enum_type(VISITOR_PURPOSE_NAME, VISITOR_PURPOSE_LABELS), nullable=False),
            sa.Column("purpose_note", sa.String(length=255), nullable=True),
            # Safety / NDA acknowledgment checkbox.
            sa.Column("safety_acknowledged", sa.Boolean(), server_default="false", nullable=False),
            # Presence lifecycle; status has Python-side default only (no server default).
            sa.Column("status", _enum_type(VISITOR_STATUS_NAME, VISITOR_STATUS_LABELS), nullable=False),
            # Naive UTC (no timezone=True); app-side default=datetime.utcnow only.
            sa.Column("signed_in_at", sa.DateTime(), nullable=False),
            sa.Column("signed_out_at", sa.DateTime(), nullable=True),
            sa.Column("signin_station_id", sa.Integer(), nullable=True),
            sa.Column("station_label", sa.String(length=100), nullable=True),
            # SoftDeleteMixin -- is_deleted indexed below; deleted_at is the only
            # timezone-aware DateTime; deleted_by is a plain Integer (NO FK).
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Integer(), nullable=True),
            # TenantMixin -- non-null company scope, FK to companies.id, LAST.
            sa.Column("company_id", sa.Integer(), nullable=False),
            # FK clause order mirrors create_all's emission order (model
            # column-discovery order) so the rendered DDL matches byte-for-byte;
            # clause order is semantically irrelevant to the resulting schema.
            sa.ForeignKeyConstraint(["host_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["signin_station_id"], ["signin_stations.id"]),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for index_name, columns, unique in LOGS_INDEXES:
        if not _has_index(LOGS_TABLE, index_name):
            op.create_index(index_name, LOGS_TABLE, columns, unique=unique)


def upgrade() -> None:
    # Order matters: signin_stations first (visitor_logs FKs it), enum types
    # before visitor_logs. All guarded so a create_all-bootstrapped DB and
    # re-runs no-op.
    _create_signin_stations()
    _create_enums()
    _create_visitor_logs()


def downgrade() -> None:
    # Reverse dependency order: drop visitor_logs (indexes then table), then the
    # enum types it owns, then signin_stations. All guarded.
    if _has_table(LOGS_TABLE):
        for index_name, _columns, _unique in reversed(LOGS_INDEXES):
            if _has_index(LOGS_TABLE, index_name):
                op.drop_index(index_name, table_name=LOGS_TABLE)
        op.drop_table(LOGS_TABLE)

    _drop_enums()

    if _has_table(STATIONS_TABLE):
        for index_name, _columns, _unique in reversed(STATIONS_INDEXES):
            if _has_index(STATIONS_TABLE, index_name):
                op.drop_index(index_name, table_name=STATIONS_TABLE)
        op.drop_table(STATIONS_TABLE)
