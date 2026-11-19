"""PO quality flow-down: the per-company profile, the Q-clause catalog, and the PO's clause pick + frozen block

Revision ID: 128_po_flowdown
Revises: 127_ncr_car_records
Create Date: 2026-11-19

Context
-------
AS9100D 8.4.3 requires the organization to communicate its quality requirements to external
providers. The ERP purchase order (the browser print and the emailed server PDF, both fed by
``GET /print/purchase-orders/{po_id}/print-data``) carried no reference to the supplier quality
requirements document or to its clauses. This revision adds the storage for that flow-down; the
block itself is built by ``app/services/po_flowdown_service.py`` and frozen onto the PO by a
mapper listener on ``PurchaseOrder`` when the PO leaves DRAFT.

``po_flowdown_profiles`` (new, tenant-scoped, one row per company)
  The company's flow-down header: ``enabled`` (NOT NULL DEFAULT false -- a company prints no
  block until a person turns it on), ``heading`` (NOT NULL DEFAULT 'Supplier Quality
  Requirements'), ``document_reference`` (the quality document and its revision),
  ``body`` (the standard flow-down paragraph), ``updated_by`` FK users, timestamps, and the
  ``version`` compare-and-swap counter ``PUT /purchasing/flowdown-profile`` checks.
  UNIQUE ``uq_po_flowdown_profiles_company`` (company_id): one profile per company.

``po_quality_clauses`` (new, tenant-scoped)
  The company's clause catalog: ``code`` (e.g. ``Q1``; immutable after create), ``title``,
  ``body``, ``default_selected`` (NOT NULL DEFAULT false -- pre-checked on a new PO),
  ``is_active`` (NOT NULL DEFAULT true -- retire, never delete), ``sort_order`` (NOT NULL
  DEFAULT 0), ``created_by`` / ``updated_by`` FK users, timestamps. UNIQUE
  ``uq_po_quality_clauses_company_code`` (company_id, code): a code is unique per company,
  never globally.

``purchase_orders`` (existing tenant table, soft-deletable)
  - ``quality_clause_codes`` JSON NULL -- the buyer's clause pick. NULL means "the catalog's
    active ``default_selected`` clauses", so every PO creator (MRP drafts, the uploader, the
    loaders) needs no change; an explicit list, empty included, is the buyer's choice.
  - ``flowdown_snapshot`` JSON NULL -- the block as it stood when the PO was issued, written
    once by the mapper listener and never re-derived.

No backfill, anywhere (deliberate)
----------------------------------
No row is read or written. Every existing PO reads NULL in both new columns, and both NULLs are
truthful: a NULL ``quality_clause_codes`` is the catalog-defaults pick (on a DRAFT PO that is
what the live block renders), and a NULL ``flowdown_snapshot`` on an ISSUED PO means "issued
before 128": nothing was flowed down then, so the print shows NO block. Back-filling a snapshot
would print requirements on a PO the supplier never received them on, so it is never done --
not here, and not by any later job. ``audit_log`` is untouched. Neither column is ever tightened
to NOT NULL. The new tables start empty: no company is given a profile or a clause.

Tenant / compliance shape
-------------------------
- Both new tables carry the TenantMixin ``company_id`` (Integer, FK companies, NOT NULL,
  indexed), and their business keys are per-company ``UniqueConstraint``s, never a column
  ``unique=True``.
- Both get ``ENABLE ROW LEVEL SECURITY`` with zero policies plus ``REVOKE ALL`` from ``PUBLIC``
  (table and sequence) -- the deny-by-default Supabase posture (docs/SUPABASE_SECURITY.md) --
  and the ``anon`` / ``authenticated`` REVOKEs sit under a ``pg_roles`` existence check,
  because those roles exist only on Supabase and a statement naming a missing role aborts the
  migration on plain PostgreSQL (CI, local Docker). This DDL runs under the dialect check only,
  NEVER under a table guard: ``create_all`` (every non-production boot) can build these tables
  before this revision runs, and the models' ``after_create`` listeners carry the same
  statements (a test pins the two byte-identical).
- ``purchase_orders`` is an existing table: RLS has been on since ``059`` and existing grants
  cover new columns, so no RLS / REVOKE statement names it. Its soft-delete columns
  (``is_deleted`` / ``deleted_at`` / ``deleted_by``) are untouched; on SQLite the downgrade's
  batch rebuild carries them, and every row, across. Nothing here deletes a row.

Lock-step with the models (the stamped-over hazard)
---------------------------------------------------
Every column, index, unique constraint and FK here is declared on ``POFlowdownProfile`` /
``POQualityClause`` (``app/models/po_flowdown.py``) and ``PurchaseOrder`` with the same name and
shape, and this file mirrors the models rather than the other way round, so the ``create_all``
-> stamp 058 -> upgrade bootstrap builds them from the models and every guard here no-ops. FKs
are UNNAMED on both sides (PostgreSQL names them ``<table>_<column>_fkey`` either way) and are
matched by shape. Server defaults are the plain strings the models declare (``"false"``, never
``sa.text("false")``, which renders a different ``DEFAULT``). The timestamps are naive UTC
``DateTime`` with no server default. ``POFlowdownProfile`` declares its own ``updated_at``
rather than inheriting ``OptimisticLockMixin``'s, whose ``server_default='now()'`` is a quoted
string that PostgreSQL folds into a constant at CREATE TABLE time: every later row would
default to the instant the table was created, and ``create_all`` and this revision would each
bake a different instant, so the PostgreSQL bootstrap round-trip could never match. Only the
mixin's ``version`` (Integer NOT NULL DEFAULT '1') is used verbatim.
``tests/test_migration_128_po_flowdown.py`` pins model == migration; the fresh-bootstrap
round-trip diffs the result against ``create_all``.

Idempotent and reversible
-------------------------
Every object is guarded INDEPENDENTLY (the 088 / 124 / 125 style): each table, each unique
constraint, each FK (by its columns and target), each index and each ``purchase_orders``
column. A re-run, the bootstrap path, or a partially built schema converges object by object.
``--sql`` (offline) has no database to inspect, so it renders the canonical statements.

Downgrade is LOSSY -- read before running it
--------------------------------------------
It drops ``purchase_orders.flowdown_snapshot`` and ``quality_clause_codes``, then both tables
(their indexes, constraints and RLS go with them). That discards every PO's frozen record of the
quality requirements flowed down to its supplier (AS9100D 8.4.3 evidence), every clause pick,
the clause catalog and the profile. Pre-128 code never reads any of them, so a CODE rollback
does not need this downgrade -- it needs a STAMP: from a checkout of the CURRENT release run
``alembic stamp 127_ncr_car_records`` before redeploying the older image. The data stays, and
the next forward upgrade re-runs this revision as a guarded no-op that keeps it. Export the
snapshots first if the downgrade is ever genuinely required.

Locking / operations note
-------------------------
Creating the tables briefly takes a SHARE ROW EXCLUSIVE lock on ``companies`` / ``users`` (the
new FKs). The two ``purchase_orders`` columns are nullable with no default: a metadata-only
``ADD COLUMN`` on PostgreSQL 11+ (no rewrite, no scan), but each still takes a brief ACCESS
EXCLUSIVE lock on ``purchase_orders``. ``SET LOCAL lock_timeout = '5s'`` bounds every wait and is
handed back (``SET LOCAL lock_timeout = DEFAULT``) when the DDL is done (precedent 059 / 060 /
114-117 / 122-127).

Deploy ordering: run BEFORE (or with) the release that maps the columns -- the migration runs at
API boot (``backend/start.sh``). Old code is safe on the new schema (it never selects the new
columns or tables). New code on the un-migrated schema is NOT: every PO load would select missing
columns, so deploy the API before or with the ARQ worker.

Revision id ``128_po_flowdown`` is 15 chars (<= 32, ``alembic_version.version_num``).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "128_po_flowdown"
down_revision = "127_ncr_car_records"
branch_labels = None
depends_on = None

PROFILES = "po_flowdown_profiles"
CLAUSES = "po_quality_clauses"
PURCHASE_ORDERS = "purchase_orders"

# Neither new table references the other: create in this order, drop in the reverse.
TABLES = (PROFILES, CLAUSES)

PROFILES_UNIQUE = "uq_po_flowdown_profiles_company"
CLAUSES_UNIQUE = "uq_po_quality_clauses_company_code"

DEFAULT_HEADING = "Supplier Quality Requirements"

# {table: ((name, columns), ...)} -- each mirrored in the model's ``__table_args__``.
UNIQUES = {
    PROFILES: ((PROFILES_UNIQUE, ("company_id",)),),
    CLAUSES: ((CLAUSES_UNIQUE, ("company_id", "code")),),
}

# {table: ((local columns, referred table), ...)} -- unnamed, as on the models; every one
# references the referred table's ``id``.
FOREIGN_KEYS = {
    PROFILES: ((("updated_by",), "users"), (("company_id",), "companies")),
    CLAUSES: ((("created_by",), "users"), (("updated_by",), "users"), (("company_id",), "companies")),
}

# {table: ((name, columns), ...)} -- the TenantMixin ``company_id`` index.
INDEXES = {
    PROFILES: (("ix_po_flowdown_profiles_company_id", ("company_id",)),),
    CLAUSES: (("ix_po_quality_clauses_company_id", ("company_id",)),),
}

# The two ``purchase_orders`` columns, in model order. Nullable, no default, never backfilled.
PO_COLUMNS = ("quality_clause_codes", "flowdown_snapshot")

# PostgreSQL only, and LOCAL: bounds the wait for each lock, then hands the normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _columns(table_name: str) -> list:
    """Fresh ``Column`` objects (a Column belongs to one table only), in create_all's emission
    order: the class's own, then ``TenantMixin.company_id``, then (profiles only)
    ``OptimisticLockMixin.version``. Every timestamp is naive UTC with a Python-side default
    only, as on the models."""
    if table_name == PROFILES:
        return [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("heading", sa.String(length=255), nullable=False, server_default=DEFAULT_HEADING),
            sa.Column("document_reference", sa.String(length=100), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("updated_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            # Declared on the model itself, overriding OptimisticLockMixin.updated_at: the mixin's
            # quoted server_default 'now()' is folded into a constant at CREATE TABLE time on
            # PostgreSQL (every later row would default to the migration's own instant), and two
            # bootstrap paths would never agree on that constant.
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            # OptimisticLockMixin.version, verbatim.
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        ]
    return [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("default_selected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
    ]


def _po_column(column_name: str) -> sa.Column:
    return sa.Column(column_name, sa.JSON(), nullable=True)


def security_sql(table_name: str) -> tuple:
    """The RLS / REVOKE DDL for one new table. Byte-identical, statement for statement, to what
    the model's ``after_create`` listeners emit (a test pins it)."""
    return (
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY",
        f"REVOKE ALL ON TABLE {table_name} FROM PUBLIC",
        f"REVOKE ALL ON SEQUENCE {table_name}_id_seq FROM PUBLIC",
        f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE {table_name} FROM anon;
        REVOKE ALL ON SEQUENCE {table_name}_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE {table_name} FROM authenticated;
        REVOKE ALL ON SEQUENCE {table_name}_id_seq FROM authenticated;
      END IF;
    END $$""",
    )


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
    return bool(op.get_context().as_sql)


def _dialect() -> str:
    return op.get_context().dialect.name


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    if _offline():
        return False
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col.get("name") == column_name for col in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix.get("name") == index_name for ix in _inspector().get_indexes(table_name))


def _has_unique(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(uq.get("name") == constraint_name for uq in _inspector().get_unique_constraints(table_name))


def _has_foreign_key(table_name: str, columns: tuple, referred_table: str) -> bool:
    """Matched by shape, not name: these FKs are unnamed on the models and in ``create_table``
    (SQLite reflects no name for them; PostgreSQL generates ``<table>_<column>_fkey``)."""
    if not _has_table(table_name):
        return False
    return any(
        tuple(fk.get("constrained_columns") or ()) == tuple(columns) and fk.get("referred_table") == referred_table
        for fk in _inspector().get_foreign_keys(table_name)
    )


def _recovered_fk_name(table_name: str, columns: tuple) -> str:
    """The name PostgreSQL itself gives an unnamed FK, so an FK re-added to a partially
    bootstrapped table is indistinguishable there from the create_all one. (Alembic's batch mode
    refuses an unnamed constraint, so SQLite gets the same name; only that recovery path shows it.)
    """
    return f"{table_name}_{'_'.join(columns)}_fkey"


def _create_table(table_name: str) -> None:
    op.create_table(
        table_name,
        *_columns(table_name),
        sa.PrimaryKeyConstraint("id"),
        # create_all's order (key, unique, then FKs), so even the stored CREATE TABLE text matches.
        *(sa.UniqueConstraint(*columns, name=name) for name, columns in UNIQUES[table_name]),
        *(sa.ForeignKeyConstraint(list(columns), [f"{referred}.id"]) for columns, referred in FOREIGN_KEYS[table_name]),
    )


def _upgrade_table(table_name: str) -> None:
    if not _has_table(table_name):
        _create_table(table_name)
    elif not _offline():
        # A partial bootstrap: the table exists, possibly without one of its constraints. Each is
        # checked on its own; any that is missing goes in through one batch (a passthrough on
        # PostgreSQL, a single table rebuild on SQLite, which cannot ALTER in a constraint).
        missing_uniques = [(n, c) for n, c in UNIQUES[table_name] if not _has_unique(table_name, n)]
        missing_fks = [(c, r) for c, r in FOREIGN_KEYS[table_name] if not _has_foreign_key(table_name, c, r)]
        if missing_uniques or missing_fks:
            with op.batch_alter_table(table_name) as batch:
                for name, columns in missing_uniques:
                    batch.create_unique_constraint(name, list(columns))
                for columns, referred in missing_fks:
                    batch.create_foreign_key(_recovered_fk_name(table_name, columns), referred, list(columns), ["id"])
    for index_name, columns in INDEXES[table_name]:
        if not _has_index(table_name, index_name):
            op.create_index(index_name, table_name, list(columns), unique=False)


def _upgrade_purchase_orders() -> None:
    # Nullable, no default: a metadata-only ADD COLUMN on PostgreSQL (no rewrite) and a native
    # ADD COLUMN on SQLite (no rebuild). Each column is guarded on its own. No UPDATE follows.
    for column_name in PO_COLUMNS:
        if _offline() or not _has_column(PURCHASE_ORDERS, column_name):
            op.add_column(PURCHASE_ORDERS, _po_column(column_name))


def _secure_table(table_name: str) -> None:
    # RLS / REVOKE run under the dialect check only -- never under a table guard -- because
    # create_all can build these tables before this revision runs. Idempotent on PostgreSQL.
    for statement in security_sql(table_name):
        op.execute(statement)


def upgrade() -> None:
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    for table_name in TABLES:
        _upgrade_table(table_name)
    _upgrade_purchase_orders()

    if postgres:
        for table_name in TABLES:
            _secure_table(table_name)
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY: discards every issued PO's frozen flow-down record, every clause pick, the clause
    # catalog and the profile. See the module docstring -- a code rollback needs a STAMP, not this.
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    present = [name for name in reversed(PO_COLUMNS) if _offline() or _has_column(PURCHASE_ORDERS, name)]
    if present:
        # A passthrough ALTER TABLE ... DROP COLUMN on PostgreSQL; on SQLite a table rebuild that
        # carries every row and every other column -- the soft-delete trio included -- across
        # (precedent 100 / 127; no trigger names purchase_orders, so the rebuild is safe).
        with op.batch_alter_table(PURCHASE_ORDERS) as batch:
            for column_name in present:
                batch.drop_column(column_name)

    for table_name in reversed(TABLES):
        if _offline() or _has_table(table_name):
            op.drop_table(table_name)

    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
