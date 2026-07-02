"""Create kiosk_stations (crew-station kiosk tablets)

Revision ID: 057_kiosk_stations
Revises: 056_visitor_logs
Create Date: 2026-07-02

Context
-------
The crew-station kiosk (branch ``feat/crew-station-kiosk``) adds one new
tenant-scoped table:

  * ``kiosk_stations`` -- ``app/models/kiosk_station.py::KioskStation``. The
    PIN-based, company-binding + revocation anchor for an unattended shop-floor
    crew tablet, physically bound to ONE work center (non-null
    ``work_center_id`` FK). It is the work-center-bound twin of
    ``signin_stations`` (056) and structurally follows ``display_tokens``
    (050). Revoke, never delete -- the issuance trail survives.

This migration writes no data and never touches the tamper-evident
``audit_log`` table; station lifecycle (create / revoke / reset-PIN /
station-login) and badge-token mints are audit-logged by the service layer
through ``AuditService``. No enum types are involved.

Tenant / compliance shape
-------------------------
TenantMixin -> ``company_id`` Integer FK ``companies.id`` NOT NULL, indexed
(``ix_kiosk_stations_company_id``); ``company_id`` sits LAST in column order
because mixin columns are appended after the class's own columns. Every query
against this table MUST be company-scoped. No SoftDeleteMixin: revocation
(``revoked`` / ``revoked_at`` / ``revoked_by``), not deletion, is the kill
switch (same rationale as ``signin_stations``). ``pin_hash`` holds only the
bcrypt hash of the shared PIN -- never plaintext.

DateTime columns are all naive UTC (no ``timezone=True``), matching the model.
Boolean ``revoked`` carries ``server_default='false'`` (model precedent);
``created_at`` has an app-side ``default=datetime.utcnow`` only -- NO server
default. All matched per-column against the model.

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), NOT a
bare ``upgrade head`` on an empty DB. So a DB bootstrapped from the updated
models already has this table when this migration runs over the stamp.

- Upgrade guards the ``create_table`` with ``_has_table`` and every
  ``create_index`` with ``_has_index`` (precedent 046/048/049/050/056), so a
  create_all-bootstrapped DB and re-runs are clean no-ops.
- Downgrade drops indexes (reverse order) then the table, all guarded.
  Round-trips cleanly on Postgres and on the SQLite used for local dev /
  pytest (plain columns only -- no native types to special-case).

Locking / operations note
-------------------------
Brand-new empty table: ``CREATE TABLE`` + index builds are instantaneous and
take no lock on any existing table. No backfill. No deploy-ordering
constraint: old application code never references this table; new code only
reads/writes it after this migration (or a create_all bootstrap) has run.

Revision id ``057_kiosk_stations`` is 18 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "057_kiosk_stations"
down_revision = "056_visitor_logs"
branch_labels = None
depends_on = None

TABLE = "kiosk_stations"

# (index_name, columns, unique). Mirrors index=True / TenantMixin declarations
# so create_all and upgrade converge.
INDEXES = [
    ("ix_kiosk_stations_company_id", ["company_id"], False),
    ("ix_kiosk_stations_id", ["id"], False),
    ("ix_kiosk_stations_work_center_id", ["work_center_id"], False),
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
    # Kept in lock-step with app/models/kiosk_station.py::KioskStation. All
    # guarded so a create_all-bootstrapped DB and re-runs no-op.
    if not _has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            # Human label ("Weld Bay Kiosk") -- surfaces on the kiosk header.
            sa.Column("label", sa.String(length=100), nullable=False),
            # The work center this station is physically bound to; a station
            # may only read ITS OWN work center's queue.
            sa.Column("work_center_id", sa.Integer(), nullable=False),
            # bcrypt hash of the shared numeric PIN -- never plaintext.
            sa.Column("pin_hash", sa.String(length=255), nullable=False),
            # Revocation trail -- revoke, never delete.
            sa.Column("revoked", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_by", sa.Integer(), nullable=True),
            # Updated on every successful station-login.
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            # App-side default=datetime.utcnow only; no server default (model parity).
            sa.Column("created_at", sa.DateTime(), nullable=False),
            # TenantMixin -- non-null company scope, FK to companies.id, LAST.
            sa.Column("company_id", sa.Integer(), nullable=False),
            # FK clause order mirrors create_all's emission order (model
            # column-discovery order) so the rendered DDL matches byte-for-byte;
            # clause order is semantically irrelevant to the resulting schema.
            sa.ForeignKeyConstraint(["work_center_id"], ["work_centers.id"]),
            sa.ForeignKeyConstraint(["revoked_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for index_name, columns, unique in INDEXES:
        if not _has_index(TABLE, index_name):
            op.create_index(index_name, TABLE, columns, unique=unique)


def downgrade() -> None:
    # Indexes (reverse order) then the table, all guarded.
    if _has_table(TABLE):
        for index_name, _columns, _unique in reversed(INDEXES):
            if _has_index(TABLE, index_name):
                op.drop_index(index_name, table_name=TABLE)
        op.drop_table(TABLE)
