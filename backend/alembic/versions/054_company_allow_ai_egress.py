"""Per-company allow_ai_egress kill switch on companies (grandfather existing tenants ON)

Revision ID: 054_company_allow_ai
Revises: 053_laser_nest_manual
Create Date: 2026-06-24

Context
-------
Adds the per-company ``allow_ai_egress`` kill switch -- the outbound-egress gate for
AI document-extraction calls to the Anthropic API. The model change lives in
``app/models/company.py::Company``:

    allow_ai_egress = Column(Boolean, nullable=False, default=False, server_default="false")

It mirrors the two existing per-company egress kill switches, byte-for-byte on the
column args: ``CompanyShippingProfile.allow_carrier_egress`` (046) and
``CompanyPrintProfile.allow_print_egress`` (051). NOT NULL, defaults OFF at the
column level so a brand-new tenant created after this migration starts with AI
egress disabled.

Grandfather decision (load-bearing -- the reason this is a hand-written migration)
---------------------------------------------------------------------------------
AI document extraction is currently ALWAYS-ON for every tenant; there is no gate in
front of it today. A bare ``ADD COLUMN ... DEFAULT false`` would silently flip every
existing tenant to AI-OFF the instant this deploys, breaking a feature they rely on.

The product decision is:
- NEW tenants default OFF  -> the column keeps ``server_default 'false'`` (so future
  INSERTs that omit the column get false).
- EXISTING tenants are grandfathered ON -> after adding the column, this migration
  runs a DATA backfill ``UPDATE companies SET allow_ai_egress = true`` so every
  company that exists at migration time keeps today's AI-always behavior.

The column is ADDED with ``server_default 'false'`` first (an online-safe,
metadata-only ADD COLUMN on Postgres -- no table rewrite, every existing row reads
false), and the backfill THEN flips existing rows to true. The server_default is
LEFT IN PLACE afterward (the model declares it), so it governs only rows inserted
after this point -- exactly the new-tenants-OFF behavior.

What this migration does (on EXISTING databases)
------------------------------------------------
ALTERs ``companies``:
1. ADD ``allow_ai_egress BOOLEAN NOT NULL DEFAULT false``.
2. BACKFILL ``UPDATE companies SET allow_ai_egress = true`` -- grandfather every
   existing tenant to today's AI-always behavior.

Tenant / compliance shape
-------------------------
``companies`` is the tenant root, not a per-tenant data table, so there is no
``company_id`` to add here -- the column simply hangs on the company row. This is a
config flag, not domain data: no ``TenantMixin``/``SoftDeleteMixin`` columns are
involved. This migration does NOT touch the tamper-evident ``audit_log`` table and
backfills no audit rows. The only data written is the grandfather UPDATE on
``companies`` itself. The flip-on of existing tenants is an intentional,
audit-relevant policy change; it is applied via this migration and the enforcement /
endpoint layer (owned by other agents) is responsible for any per-change audit log
once the switch is operator-toggleable.

Idempotent and reversible
-------------------------
- Upgrade guards the ``add_column`` with ``_has_column`` (precedents 051/053). On the
  ``create_all -> stamp -> upgrade`` bootstrap path (docs/DEVELOPMENT.md) the column
  already exists from the model, so the add is a clean no-op; re-runs are likewise
  no-ops. The backfill is idempotent: re-running ``SET allow_ai_egress = true`` over
  already-true rows changes nothing (it sets the same value). The backfill is scoped
  INSIDE the add-column guard, so it runs ONLY when the column is first created (the
  real first-time migration on an established prod DB). On the create_all -> stamp ->
  upgrade bootstrap path the column already exists, so both the add and the backfill
  are skipped and freshly-seeded companies keep the server_default 'false'
  (new-tenants-OFF). A downgrade (drops the column) followed by a re-upgrade re-adds
  and re-grandfathers ON -- acceptable, since the per-tenant OFF choices were already
  destroyed when the column was dropped.
- Downgrade drops the column, guarded with ``_has_column`` so it round-trips cleanly
  on Postgres and on the SQLite used for local dev / pytest. The grandfather state is
  not separately reversible (the flag ceases to exist), which is correct: removing
  the column restores the prior schema exactly.

Locking / operations note
-------------------------
The ADD COLUMN is a single NULLABLE-equivalent metadata change with a constant
server_default -- on modern Postgres (11+) this does NOT rewrite the table and takes
only a brief ACCESS EXCLUSIVE lock to update the catalog. The backfill is a single
set-based ``UPDATE companies SET ...`` over the ``companies`` table; ``companies`` is
small (one row per tenant, tens of rows, not a hot high-volume table), so the update
and its row locks are negligible -- no batching is warranted at this size.

Deploy ordering: this migration is forward-compatible with old application code (old
code never reads the column). New enforcement code that reads ``allow_ai_egress``
must not ship before this migration runs, or it would read a missing column. Apply
this migration, then deploy the enforcement layer.

Revision id ``054_company_allow_ai`` is 20 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "054_company_allow_ai"
down_revision = "053_laser_nest_manual"
branch_labels = None
depends_on = None

COMPANIES = "companies"
COLUMN = "allow_ai_egress"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def upgrade() -> None:
    # 1. Add the column. NOT NULL with server_default 'false' so the ADD COLUMN is
    #    online-safe on a populated table (every existing row reads false) and new
    #    tenants created after this point default OFF. Guarded for the create_all
    #    bootstrap / re-run no-op path.
    if not _has_column(COMPANIES, COLUMN):
        op.add_column(
            COMPANIES,
            sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default="false"),
        )

        # 2. Grandfather: flip every EXISTING tenant to AI-ON so the current
        #    always-on AI-extraction behavior is preserved. Scoped INSIDE the
        #    add-column guard on purpose: it runs ONLY when the column is first
        #    created (the real first-time migration on an established prod DB).
        #    On the create_all -> stamp -> upgrade bootstrap path the column
        #    already exists, so this is skipped and freshly-seeded companies keep
        #    the server_default 'false' (new-tenants-OFF). The server_default
        #    stays 'false' (untouched) -> only future INSERTs default OFF.
        op.execute(sa.text("UPDATE companies SET allow_ai_egress = true"))


def downgrade() -> None:
    # Drop the column (guarded). Removing the column restores the prior schema
    # exactly; the grandfather state ceases to exist with it.
    if _has_column(COMPANIES, COLUMN):
        op.drop_column(COMPANIES, COLUMN)
