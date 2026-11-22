"""Add parts.customer_supplied: a customer-supplied part creates no MRP demand

Revision ID: 134_part_customer_supplied
Revises: 132_maintenance_pm_integrity
Create Date: 2026-11-22

Context
-------
AS9100D 8.5.3 (property belonging to customers): some parts on a BOM are furnished by the
customer, never bought or made by the shop. MRP had no way to know that, so it planned demand
for them like any other component -- a requirement, an action, and through the action doors a
draft purchase order to a vendor for a part the customer is going to ship in. Owner decision
2026-11-22: a customer-supplied part creates NO MRP demand. The BOM explosion skips it (no
requirement, no action, no recursion into its own BOM) and every door that turns an MRP action
into supply refuses it. This revision adds the one column that decision keys off, over the
EXISTING ``parts`` table (``app/models/part.py::Part``):

    parts.customer_supplied  BOOLEAN NOT NULL DEFAULT 'false'

The flag is set only through ``PUT /parts/{id}`` / ``PUT /materials/{id}`` (audited by
``log_update``), never on create or import -- the same posture as ``backflush_components``. It
says nothing about stock ownership: who owns a stock row is that row's own ``ownership`` column
(``129_owned_property``), and nothing infers one from the other.

No backfill on PostgreSQL (deliberate)
--------------------------------------
Every existing part reads ``false`` from the server default, which is exactly how MRP treats
every part today, so nothing changes until a person flags a part. There is no ``UPDATE`` on
PostgreSQL: ``ADD COLUMN ... NOT NULL DEFAULT <constant>`` is metadata-only on PostgreSQL 11+
(the constant is stored once in the catalog), and the default IS the backfill -- no separate
nullable-then-backfill-then-NOT-NULL sequence is needed, because the column is born with a
value. Which parts the customer really furnishes is a person's judgment, so the migration never
guesses one: nothing is inferred from customer-owned stock, a customer name, or anything else.
``audit_log`` is untouched.

SQLite storage normalization (local / test databases only; PostgreSQL is untouched): SQLite
stores a plain ``DEFAULT 'false'`` as the TEXT ``'false'`` for a row that existed before the
column was added, and SQLAlchemy's SQLite Boolean reads any non-empty string as True. Left
alone, every part on an incrementally-migrated SQLite dev database would read as
customer-supplied and MRP would plan nothing at all. So on SQLite only, right after the column
is added, the upgrade runs ``SQLITE_NORMALIZE_SQL`` (``'false'`` -> ``0``). This is a
DIALECT-STORAGE FIX, NOT A DATA BACKFILL (precedent ``119`` / ``130``): ``'false'`` and ``0`` are
the same value; only the TEXT spelling SQLite's ALTER left behind is replaced by the integer the
ORM writes. It runs only when this upgrade added the column, so a re-run never touches a value.

Shape / compliance
------------------
- ``parts`` is an existing tenant table (TenantMixin non-null ``company_id`` + index) with RLS
  enabled since ``059``. This revision creates no table, so the ENABLE-RLS new-table convention
  does not apply, and it emits no RLS / ``REVOKE`` / role statement. Existing table-level grants
  and revokes cover new columns.
- ``parts`` is soft-deletable: the ``is_deleted`` / ``deleted_at`` / ``deleted_by`` columns are
  untouched, no row is read, written or deleted, and the table is never rebuilt (below).
- Pure DDL plus the SQLite-only storage fix.

Lock-step with the model (the stamped-over hazard)
--------------------------------------------------
``Part.customer_supplied`` carries the identical type (Boolean), nullability (NOT NULL) and
server default -- the plain string ``"false"``, spelled exactly as the model's
``backflush_components`` and ``is_deleted`` spell it, never ``sa.text("false")`` or
``sa.false()``, which render a different ``DEFAULT`` on SQLite and break the fresh-bootstrap
diff. No index, unique, FK or CHECK on either side. The model's Python-side ``default=False``
needs no migration counterpart. ``tests/test_migration_134_part_customer_supplied.py`` pins
model == migration == the contract; ``tests/test_migration_fresh_bootstrap.py``'s
``downgrade 088`` -> ``upgrade head`` round-trip diffs the result against ``create_all``. Only
ordinal position differs (``create_all`` emits the column after ``backflush_components``;
``ADD COLUMN`` appends it), which nothing compares.

Idempotent and reversible
-------------------------
- Upgrade is guarded by ``_has_table`` / ``_has_column``: over a ``create_all``-bootstrapped
  schema (create_all -> stamp 058 -> upgrade, where the model already built the column) and on a
  re-run it is a clean no-op that emits nothing. A schema with no ``parts`` table at all is left
  alone (``create_all`` builds the column from the model).
- Downgrade really drops the column, guarded; a second downgrade is a no-op.
- Both directions run through ``batch_alter_table(..., recreate="never")``: plain ``ALTER TABLE``
  on PostgreSQL, and SQLite's NATIVE ``ADD COLUMN`` / ``DROP COLUMN`` (the latter needs SQLite
  3.35+, precedent ``123`` / ``130``), never the copy-and-rename rebuild. A rebuild of ``parts``
  is exactly what must not happen: the ``tr_stock_observation_insert`` trigger
  (``104_stock_piece_observations``) reads ``parts`` in its body, so a rebuild's drop + rename
  would need ``129``'s ``legacy_alter_table`` wrapper, and some thirty model FKs reference
  ``parts``, so a rebuild's ``DROP TABLE`` under ``PRAGMA foreign_keys = ON`` would run an implicit
  ``DELETE`` against every child. A plain column with a constant default needs no rebuild, and a
  native ``DROP COLUMN`` accepts it (no index, constraint, trigger or view names it).
- ``--sql`` (offline) has no database to inspect, so it renders the canonical statements.

Downgrade is LOSSY -- read before running it
--------------------------------------------
Dropping the column discards every customer-supplied flag a person set. After a downgrade (and
on a re-upgrade, which reads every part ``false`` again) MRP plans demand for those parts once
more, and the MRP action doors will offer to buy them. The ``log_update`` audit rows still
record who flagged which part, but nothing re-derives the column from them.

Pre-134 code never reads the column, and its inserts satisfy the NOT NULL through the server
default, so a CODE rollback does NOT need this downgrade -- it needs a STAMP: from a checkout of
the CURRENT release run ``alembic stamp`` with this revision's ``down_revision`` before
redeploying the older image. The flags stay, and the next forward upgrade re-runs this revision
as a guarded no-op that keeps them (the older code simply plans demand for those parts while it
runs).

Locking / operations note
-------------------------
The ``ADD COLUMN`` (and a downgrade's ``DROP COLUMN``) is metadata-only on PostgreSQL 11+: no
table rewrite, no NOT NULL verification scan, no index build. It still takes a brief ACCESS
EXCLUSIVE lock on ``parts``, which many requests read (every BOM, work-order, inventory and MRP
path joins it), so ``SET LOCAL lock_timeout = '5s'`` bounds the wait -- and so bounds how long
requests queue behind it -- when there is DDL to run, and is handed back
(``SET LOCAL lock_timeout = DEFAULT``) as soon as it is done (precedent 114-117, 122-132). A
timeout rolls the migration back cleanly; re-run the deploy.

Deploy ordering: run BEFORE (or with) the release that maps the column -- the migration runs at
API boot (``backend/start.sh``). Old code is safe on the new schema (it never selects the
column, and its inserts take the default). New code on the un-migrated schema is NOT: every
``Part`` load would select a missing column, failing every parts, BOM, work-order and MRP read,
so deploy the API before or with the ARQ worker.

Revision id ``134_part_customer_supplied`` is 26 chars (<= 32, ``alembic_version.version_num``).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "134_part_customer_supplied"
down_revision = "132_maintenance_pm_integrity"
branch_labels = None
depends_on = None

TABLE_NAME = "parts"
COLUMN_NAME = "customer_supplied"

# The plain string the model declares (``Part.customer_supplied``), never ``sa.text`` /
# ``sa.false()``: those render a different DEFAULT on SQLite and break the fresh-bootstrap diff.
SERVER_DEFAULT = "false"

# PostgreSQL only, and LOCAL: bounds the wait for the ACCESS EXCLUSIVE lock on parts, then hands
# the normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"

# SQLite only: a dialect-storage fix, NOT a data backfill (module docstring). SQLite's ADD COLUMN
# leaves pre-existing rows holding the default as the TEXT 'false', which SQLAlchemy's SQLite
# Boolean reads as True; this re-spells that same false as the integer 0 the ORM writes.
SQLITE_NORMALIZE_SQL = "UPDATE parts SET customer_supplied = 0 WHERE customer_supplied = 'false'"

# Native ALTER on SQLite, never a copy-and-rename rebuild of ``parts`` (module docstring). On
# PostgreSQL batch mode never recreates, so this is the same plain ALTER.
BATCH_RECREATE = "never"


def _column() -> sa.Column:
    """A fresh ``Column`` (a Column can belong to one table only)."""
    return sa.Column(COLUMN_NAME, sa.Boolean(), nullable=False, server_default=SERVER_DEFAULT)


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
    return bool(op.get_context().as_sql)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_context().dialect.name == "sqlite"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def upgrade() -> None:
    if not _offline():
        if not _has_table(TABLE_NAME):
            return  # no parts table at all: create_all builds the column from the model
        if _has_column(TABLE_NAME, COLUMN_NAME):
            return  # the create_all bootstrap (or a re-run) already built it

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    # NOT NULL with a constant default: metadata-only on PostgreSQL 11+, and the default is the
    # backfill. No UPDATE follows on PostgreSQL.
    with op.batch_alter_table(TABLE_NAME, recreate=BATCH_RECREATE) as batch:
        batch.add_column(_column())
    if _is_sqlite():
        # Dialect-storage fix, not a backfill: re-spell SQLite's TEXT 'false' as the integer 0 (the
        # same false) so the ORM does not read every pre-existing part as customer-supplied.
        op.execute(SQLITE_NORMALIZE_SQL)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY: discards every customer-supplied flag. See the module docstring -- a code rollback
    # needs a STAMP to this revision's down_revision, not this.
    if not _offline() and not _has_column(TABLE_NAME, COLUMN_NAME):
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    with op.batch_alter_table(TABLE_NAME, recreate=BATCH_RECREATE) as batch:
        batch.drop_column(COLUMN_NAME)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
