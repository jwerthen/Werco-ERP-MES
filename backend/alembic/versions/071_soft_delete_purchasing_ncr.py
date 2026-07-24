"""Add SoftDeleteMixin columns to vendors, purchase_orders, po_receipts, ncrs

Revision ID: 071_soft_delete_purchasing_ncr
Revises: 071_display_token_show_customer
Create Date: 2026-07-23

Re-parent note (2026-07-23)
---------------------------
Originally authored with ``down_revision = "070_operation_last_report"``. While
this branch was in review, PR #150 merged ``071_display_token_show_customer``
(also off ``070``) to ``main``; two revisions off one parent is two Alembic
heads. This revision was re-parented onto ``071_display_token_show_customer`` so
the graph has a single head once this branch merges. The revision id / filename
are intentionally left as ``071_soft_delete_purchasing_ncr`` (Alembic keys on
the revision string, not the numeric prefix) so every existing reference to
"migration 071" across the docs stays accurate; the numeric prefix simply
repeats — harmless and the migration-graph tests still pass.

Context
-------
Adds the three ``SoftDeleteMixin`` columns (app/db/mixins.py) to FOUR existing
tenant tables so their rows can be soft-deleted (and later restored) instead of
physically removed -- the AS9100D records-integrity posture already used by
``parts`` / ``work_orders`` / ``customers`` / ``boms`` / ``routings`` (added by
migration 006):

- ``vendors``          (app/models/purchasing.py::Vendor)
- ``purchase_orders``  (app/models/purchasing.py::PurchaseOrder)
- ``po_receipts``      (app/models/purchasing.py::POReceipt)
- ``ncrs``             (app/models/quality.py::NonConformanceReport)

Per table, three columns matching the mixin EXACTLY so autogenerate never
reports drift:

- ``is_deleted``  Boolean, NOT NULL, server_default ``false``, INDEXED
- ``deleted_at``  DateTime(timezone=True), nullable
- ``deleted_by``  Integer, nullable  (plain Integer -- NO FK, matching 006 and
                  the mixin's ``deleted_by`` declaration)

The ``is_deleted`` index is named ``ix_<table>_is_deleted`` -- SQLAlchemy's
``index=True`` default naming -- so the create_all bootstrap path and this
migration converge on ONE index object per table (precedent 006/050/065/068).

No backfill / no data written
-----------------------------
The ``is_deleted`` server_default ``false`` populates every pre-existing row as
"live" in the same metadata-only ADD COLUMN -- no separate UPDATE pass. That is
the truthful state: nothing has been soft-deleted yet. ``deleted_at`` /
``deleted_by`` stay NULL. The tamper-evident ``audit_log`` table is NOT touched
and NOT backfilled (invariant: never write it out of band -- the eventual
delete/restore actions are audited by the service layer via ``AuditService``).

Shape / compliance
------------------
All four are EXISTING tables: each already carries the TenantMixin non-null
``company_id`` + index, and RLS is already enabled on them -- 059
(``059_supabase_rls_hardening``) enabled ROW LEVEL SECURITY dynamically on every
``public`` table. This migration creates NO table (no ``op.create_table``), so
the "ENABLE ROW LEVEL SECURITY on every new table" convention does not apply and
no RLS DDL is emitted (precedent 064-070).

Idempotent and reversible
-------------------------
- Upgrade guards each ADD COLUMN with ``_has_column`` and guards each index by
  COVERED COLUMN (``_index_names_on_column``) rather than by name, so the
  create_all -> stamp -> upgrade bootstrap path -- where the models'
  ``SoftDeleteMixin`` already built the columns and the ``index=True`` index --
  no-ops instead of erroring, and a partial prior run resumes cleanly
  (precedent 006/065/068). A table missing entirely is skipped (bootstrap
  safety, precedent 069/070).
- Downgrade drops any index covering ``is_deleted`` by REFLECTED name (covers
  both the migration-built and any create_all-built variant), then drops
  EXACTLY the three columns this revision owns, each guarded by ``_has_column``.
  On SQLite the drops run in ONE batch context per table, which recreates the
  table without the columns (precedent 063/064/065/068/070) rather than relying
  on SQLite's version-gated ``ALTER TABLE ... DROP COLUMN``.
- Dialect-neutral DDL in both directions: a constant-default ADD COLUMN + plain
  CREATE INDEX work identically on PostgreSQL and on the SQLite dev/pytest DBs,
  so there is no ``_is_postgres`` early return here (precedent 068/070).

Locking / operations note
-------------------------
Each ADD COLUMN of ``is_deleted`` carries a CONSTANT server_default (``false``),
which on PostgreSQL 11+ is metadata-only -- the default is materialized lazily,
so there is NO table rewrite and NO backfill scan, just a brief ACCESS EXCLUSIVE
lock per statement. ``deleted_at`` / ``deleted_by`` are nullable with no default
(also metadata-only). Each CREATE INDEX (non-CONCURRENT) takes a SHARE lock on
its table for the build, blocking WRITES but not reads. These four tables are
small-to-moderate (procurement + quality, not the shop-floor dispatch hot path),
so the builds are expected to be sub-second. If ever run against a materially
larger ``po_receipts`` or during business hours, prefer building the indexes
CONCURRENTLY out-of-band (``CREATE INDEX CONCURRENTLY ix_<table>_is_deleted ON
<table> (is_deleted)`` in autocommit, then re-run this migration -- the
by-column guard makes it a clean no-op).

Deploy ordering: run BEFORE (or with) the app deploy that reads/writes the
soft-delete columns. Old code neither writes nor selects them and is unaffected
either way; a downgrade only removes the soft-delete capability (and any
soft-delete flags set in the meantime), never production history.

Revision id ``071_soft_delete_purchasing_ncr`` is 30 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "071_soft_delete_purchasing_ncr"
down_revision = "071_display_token_show_customer"
branch_labels = None
depends_on = None

# Tables receiving the SoftDeleteMixin columns.
TABLES = ("vendors", "purchase_orders", "po_receipts", "ncrs")

# The one indexed column; its index name matches the model's index=True default
# (ix_<table>_<column>) so create_all and this migration converge on one index.
INDEXED_COLUMN = "is_deleted"


def _index_name(table_name: str) -> str:
    return f"ix_{table_name}_{INDEXED_COLUMN}"


def _soft_delete_columns() -> list:
    """The three columns this revision owns, in add order (is_deleted first so
    its index can be created right after). Types match SoftDeleteMixin exactly."""
    return [
        sa.Column(INDEXED_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Integer(), nullable=True),
    ]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _index_names_on_column(table_name: str, column_name: str) -> list:
    """Names of indexes covering exactly this column.

    Checked by covered column rather than by name so both the migration-built
    and the create_all-bootstrapped index (identically named here, but the guard
    does not depend on that) are found (precedent 065/068).
    """
    if not _has_table(table_name):
        return []
    return [
        ix["name"]
        for ix in _inspector().get_indexes(table_name)
        if ix.get("column_names") == [column_name] and ix.get("name")
    ]


def upgrade() -> None:
    for table_name in TABLES:
        if not _has_table(table_name):
            # Partial bootstrap with the table absent: nothing to alter;
            # Base.metadata.create_all builds the columns + index from the ORM
            # mapping (precedent 069/070).
            continue

        # Constant-default ADD COLUMN -> metadata-only on PostgreSQL 11+, no
        # rewrite, no backfill scan. Existing rows become is_deleted=false.
        for column in _soft_delete_columns():
            if not _has_column(table_name, column.name):
                op.add_column(table_name, column)

        # Non-unique soft-delete index. No-ops when the create_all bootstrap path
        # already built it via the model's index=True.
        if not _index_names_on_column(table_name, INDEXED_COLUMN):
            op.create_index(_index_name(table_name), table_name, [INDEXED_COLUMN], unique=False)


def downgrade() -> None:
    conn = op.get_bind()

    # Reverse table order for symmetry; order is immaterial (independent tables).
    for table_name in reversed(TABLES):
        if not _has_table(table_name):
            continue

        # Drop the is_deleted index first (by reflected name -- covers the
        # migration-built and any differently-named create_all-built variant).
        # SQLite also cannot drop a column that is still named in an index.
        for actual_index_name in _index_names_on_column(table_name, INDEXED_COLUMN):
            op.drop_index(actual_index_name, table_name=table_name)

        present = [
            col.name for col in _soft_delete_columns() if _has_column(table_name, col.name)
        ]
        if not present:
            continue

        if conn.dialect.name == "sqlite":
            # One batch context recreates the table without the columns
            # (precedent 063/064/065/068/070) rather than relying on SQLite's
            # version-gated ALTER TABLE ... DROP COLUMN.
            with op.batch_alter_table(table_name) as batch_op:
                for column_name in present:
                    batch_op.drop_column(column_name)
        else:
            for column_name in present:
                op.drop_column(table_name, column_name)
