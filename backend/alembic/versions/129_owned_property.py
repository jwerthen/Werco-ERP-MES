"""Ownership markers on inventory stock rows and tools (AS9100D 8.5.3 customer / supplier property)

Revision ID: 129_owned_property
Revises: 128_po_flowdown
Create Date: 2026-11-19

Context
-------
AS9100D 8.5.3 requires the organization to identify, verify, protect and safeguard property
belonging to customers or external providers while it is under the organization's control.
``inventory_items`` (a stock row: part + location + lot) and ``tools`` (the tool and fixture
register) had no way to say that a row is NOT the company's own, so customer-furnished material
and customer- or supplier-owned tooling were indistinguishable from company stock: drawn by FIFO,
merged on receive, and counted in valuation. This revision adds the marker; the behavior that
reads it (FIFO skips owned stock, merge keys include ownership, the reasoned change-of-ownership
verb, valuation excludes it) lives in the services.

Each of ``inventory_items`` and ``tools`` gains, every object guarded on its own:

  - ``ownership``          String(20) NOT NULL, server default ``'company'``, plus CHECK
    ``ck_<table>_ownership``: ``ownership IN ('company', 'customer', 'supplier')`` (a String +
    CHECK rather than a native PostgreSQL enum, precedent ``122`` / ``127``, so a fourth value
    later is a CHECK swap, not an ``ALTER TYPE``);
  - ``owner_customer_id``  Integer NULL, FK ``fk_<table>_owner_customer_id`` -> ``customers.id``;
  - ``owner_vendor_id``    Integer NULL, FK ``fk_<table>_owner_vendor_id`` -> ``vendors.id``;
  - CHECK ``ck_<table>_owner_matches``: a company row names no owner, a customer row names a
    customer and no vendor, a supplier row names a vendor and no customer;
  - index ``ix_<table>_ownership`` on ``(company_id, ownership)``, for the per-company ownership
    filters on the inventory and tool lists.

No backfill beyond the server default (deliberate)
--------------------------------------------------
No ``UPDATE`` is issued. Every pre-existing row reads ``ownership = 'company'`` with both owner
columns NULL -- exactly what every row has meant until now, and a combination the owner-match
CHECK admits. Nothing here guesses which existing rows are really a customer's: an ownership
change is a reasoned, audited act of a person (``POST /inventory/items/{id}/ownership``, or a tool
edit), never a migration's inference. ``audit_log`` is untouched.

The two FKs carry no ``ON DELETE`` (NO ACTION): an ownership marker is a property record, so a
physical delete of a customer or vendor still named as an owner is refused by the database rather
than nulling the marker -- which the owner-match CHECK would refuse anyway, and which would
silently turn the owner's property into company stock. Customers and vendors are normally
soft-deleted; ``DELETE /customers/{id}?hard_delete=true`` is the one physical path, and it should
probe for owned stock and tools before it deletes rather than surface this FK as an error.

Tenant / compliance shape
-------------------------
- ``inventory_items`` and ``tools`` are EXISTING tenant tables: each already carries the
  TenantMixin non-null ``company_id`` + index, and RLS has been on since ``059``. This revision
  creates no table, so the ENABLE-RLS new-table convention does not apply, and it emits no RLS /
  ``REVOKE`` / role-naming statement. Existing grants cover new columns.
- The new index leads with ``company_id``, so it serves tenant-scoped reads only.
- Neither table is soft-deletable (``is_active`` / ``status`` are their tombstones); both are
  untouched. On SQLite the batch rebuild carries every row and every other column, constraint and
  index across. Nothing here deletes a row.

Lock-step with the models (the stamped-over hazard)
---------------------------------------------------
Every column, FK name, CHECK text and the index are declared on ``InventoryItem``
(``app/models/inventory.py``) and ``Tool`` (``app/models/tool_management.py``) in the same commit,
the CHECK texts built there from one shared constant, so the ``create_all`` -> stamp -> upgrade
bootstrap builds them from the models and every guard here no-ops. The CHECK texts here are
literals equal to the PR contract; ``tests/test_migration_129_owned_property.py`` pins them
byte-identical to the models' (and the server default to the models' plain ``"company"``,
never ``sa.text``), and ``tests/test_migration_fresh_bootstrap.py``'s ``downgrade 088`` ->
``upgrade head`` round-trip diffs the result against ``create_all``.

Idempotent and reversible
-------------------------
- Every object is guarded independently (088 / 127 style): each column by ``_has_column``, each
  FK by name, each CHECK by name, the index by name. A re-run, the bootstrap path, or a partially
  built schema converges object by object.
- On SQLite each table change runs in batch mode (an FK or CHECK makes it rebuild the table,
  carrying every row across; ``ownership`` takes its server default on the copy); on PostgreSQL
  batch mode emits plain ``ALTER TABLE``.
- SQLite rebuild vs. triggers that name the table: ``104``'s ``tr_stock_observation_insert``
  (model ``after_create`` DDL, ``app/models/stock_piece.py``) is a trigger ON
  ``stock_piece_observations`` whose BODY reads ``inventory_items``. A rebuild drops the table and
  renames the copy into place, and SQLite 3.26+ re-parses every trigger on that rename, so it dies
  with "no such table: main.inventory_items" (precedent ``123``, which side-stepped the same trap
  on ``parts`` with a native DROP COLUMN -- not available here, since a CHECK and an FK must be
  added and dropped). Each rebuild therefore runs under ``PRAGMA legacy_alter_table = ON``, the
  remedy SQLite documents for its 12-step generalized ALTER TABLE, and the prior value is put
  back after. That pragma is connection-scoped and never persisted; the trigger's stored text is
  left byte-identical and resolves to the rebuilt table. No trigger is ON either table (a rebuild
  would silently drop one; the fresh-bootstrap round-trip would show it), and none names
  ``tools``. PostgreSQL never rebuilds, so the pragma is SQLite-only.
- Downgrade drops exactly what upgrade added, in reverse: ``tools`` before ``inventory_items``,
  and within each table the index, then the CHECKs, then the FKs, then the columns. Each drop is
  guarded.
- ``--sql`` (offline) has no database to inspect, so it renders the canonical path.

Downgrade is LOSSY -- and worse than lossy for owned stock. Read before running it.
------------------------------------------------------------------------------------
Dropping the columns discards which stock rows and tools belong to a customer or supplier, and
who. Every owned row then reads as company stock to every query: FIFO draws it, a receive merges
into it, valuation counts it. The ownership-change and receipt audit rows still record what was
set, but nothing re-derives the columns from them.

Pre-129 code never reads these columns, and its inserts satisfy both CHECKs through the server
default, so a CODE rollback does NOT need this downgrade -- it needs a STAMP: from a checkout of
the CURRENT release run ``alembic stamp 128_po_flowdown`` (this revision's ``down_revision``)
before redeploying the older image. The markers stay, and the next forward upgrade re-runs this
revision as a guarded no-op that keeps them. Even under a stamp, the OLD code cannot see the
markers: while it runs, it draws, merges into and values owned rows as company stock. If any
owned stock exists at rollback time, put those rows on hold for the rollback window.

Locking / operations note
-------------------------
- ``ownership`` is NOT NULL with a constant server default: on PostgreSQL 11+ that ``ADD COLUMN``
  is metadata-only -- the default is stored once in the catalog (``attmissingval``), with no table
  rewrite and no NOT NULL verification scan. The two owner columns are nullable with no default,
  also metadata-only.
- Each FK validates against an all-NULL column (a quick scan) and takes a SHARE ROW EXCLUSIVE lock
  on ``customers`` / ``vendors``; each CHECK scans its table once (every row is ``'company'`` with
  NULL owners, which passes); the index is a plain ``CREATE INDEX`` (not CONCURRENTLY -- the
  revision runs inside the migration transaction). Both tables are small, so each scan is brief.
- Each table takes an ACCESS EXCLUSIVE lock -- ``inventory_items`` first, then ``tools`` -- held to
  the end of the migration transaction. ``SET LOCAL lock_timeout = '5s'`` bounds every wait when
  there is DDL to run and is handed back (``SET LOCAL lock_timeout = DEFAULT``) as soon as it is
  done (precedent 114-117, 122, 127, 128). A timeout rolls the whole migration back cleanly.

Deploy ordering: run BEFORE (or with) the release that maps the columns -- the migration runs at
API boot (``backend/start.sh``). Old code is safe on the new schema (it never selects the
columns, and its inserts take the ``'company'`` default). New code on the un-migrated schema is
NOT: every inventory and tool load would select missing columns, so deploy the API before or with
the ARQ worker.

Revision id ``129_owned_property`` is 18 chars (<= 32, ``alembic_version.version_num``).
"""

from contextlib import contextmanager
from typing import Callable, Iterator, List, NamedTuple, Tuple

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "129_owned_property"
down_revision = "128_po_flowdown"
branch_labels = None
depends_on = None

INVENTORY_ITEMS = "inventory_items"
TOOLS = "tools"

# The only server default. A plain string, as on the models (never ``sa.text``, which renders a
# different DEFAULT on SQLite and breaks the fresh-bootstrap diff).
DEFAULT_OWNERSHIP = "company"

# Lock-step with the models' shared CHECK constants (the test pins both tables' texts equal to
# these). Identical on both tables: neither names its table.
OWNERSHIP_CHECK_SQL = "ownership IN ('company', 'customer', 'supplier')"
OWNER_MATCHES_CHECK_SQL = (
    "(ownership = 'company' AND owner_customer_id IS NULL AND owner_vendor_id IS NULL) OR "
    "(ownership = 'customer' AND owner_customer_id IS NOT NULL AND owner_vendor_id IS NULL) OR "
    "(ownership = 'supplier' AND owner_vendor_id IS NOT NULL AND owner_customer_id IS NULL)"
)

# PostgreSQL only, and LOCAL: bounds the wait for each lock, then hands the normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"

# SQLite only: lets a batch rebuild's drop + rename survive a trigger whose body names the table
# (104's ``tr_stock_observation_insert`` reads ``inventory_items``). See the module docstring.
LEGACY_ALTER_TABLE_PRAGMA = "PRAGMA legacy_alter_table"


def _columns() -> List[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only), in model order."""
    return [
        sa.Column("ownership", sa.String(length=20), nullable=False, server_default=DEFAULT_OWNERSHIP),
        sa.Column("owner_customer_id", sa.Integer(), nullable=True),
        sa.Column("owner_vendor_id", sa.Integer(), nullable=True),
    ]


COLUMN_NAMES = tuple(column.name for column in _columns())


class _TableChange(NamedTuple):
    """Everything this revision adds to one table."""

    table: str
    columns: Callable[[], List[sa.Column]]
    # (constraint name, referred table, local column); every one references the referred ``id``.
    foreign_keys: Tuple[Tuple[str, str, str], ...]
    # (constraint name, SQL), in creation order.
    checks: Tuple[Tuple[str, str], ...]
    # (index name, columns)
    index: Tuple[str, Tuple[str, ...]]


def _table_change(table: str) -> _TableChange:
    return _TableChange(
        table,
        _columns,
        (
            (f"fk_{table}_owner_customer_id", "customers", "owner_customer_id"),
            (f"fk_{table}_owner_vendor_id", "vendors", "owner_vendor_id"),
        ),
        (
            (f"ck_{table}_ownership", OWNERSHIP_CHECK_SQL),
            (f"ck_{table}_owner_matches", OWNER_MATCHES_CHECK_SQL),
        ),
        (f"ix_{table}_ownership", ("company_id", "ownership")),
    )


# Upgrade order (the lock order the docstring states); downgrade walks it reversed.
TABLE_CHANGES: Tuple[_TableChange, ...] = (_table_change(INVENTORY_ITEMS), _table_change(TOOLS))


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
    return bool(op.get_context().as_sql)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_context().dialect.name == "sqlite"


@contextmanager
def _sqlite_rebuild_safe() -> Iterator[None]:
    """Run a ``batch_alter_table`` under ``PRAGMA legacy_alter_table = ON`` on SQLite, then put
    the connection's previous value back. A no-op on every other dialect (PostgreSQL never
    rebuilds). Connection-scoped and never persisted, so nothing outlives the migration."""
    if not _is_sqlite():
        yield
        return
    previous = 0 if _offline() else op.get_bind().exec_driver_sql(LEGACY_ALTER_TABLE_PRAGMA).scalar()
    op.execute(f"{LEGACY_ALTER_TABLE_PRAGMA} = ON")
    try:
        yield
    finally:
        op.execute(f"{LEGACY_ALTER_TABLE_PRAGMA} = {'ON' if previous else 'OFF'}")


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_foreign_key(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(fk["name"] == constraint_name for fk in _inspector().get_foreign_keys(table_name))


def _has_check_constraint(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ck["name"] == constraint_name for ck in _inspector().get_check_constraints(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


class _Pending(NamedTuple):
    change: _TableChange
    columns: List[sa.Column]
    foreign_keys: List[Tuple[str, str, str]]
    checks: List[Tuple[str, str]]
    index: bool


def _has_work(work: _Pending) -> bool:
    return bool(work.columns or work.foreign_keys or work.checks or work.index)


def _has_batch_work(work: _Pending) -> bool:
    return bool(work.columns or work.foreign_keys or work.checks)


def _missing(change: _TableChange, offline: bool) -> _Pending:
    table = change.table
    return _Pending(
        change,
        [column for column in change.columns() if offline or not _has_column(table, column.name)],
        [fk for fk in change.foreign_keys if offline or not _has_foreign_key(table, fk[0])],
        [check for check in change.checks if offline or not _has_check_constraint(table, check[0])],
        offline or not _has_index(table, change.index[0]),
    )


def _present(change: _TableChange, offline: bool) -> _Pending:
    table = change.table
    return _Pending(
        change,
        [column for column in change.columns() if offline or _has_column(table, column.name)],
        [fk for fk in change.foreign_keys if offline or _has_foreign_key(table, fk[0])],
        [check for check in change.checks if offline or _has_check_constraint(table, check[0])],
        offline or _has_index(table, change.index[0]),
    )


def upgrade() -> None:
    offline = _offline()
    pending = [work for work in (_missing(change, offline) for change in TABLE_CHANGES) if _has_work(work)]
    if not pending:
        return  # the create_all bootstrap (or a re-run) already built every object

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    for work in pending:
        table = work.change.table
        if _has_batch_work(work):
            # Columns first (the CHECKs and FKs name them), then the FKs, then the CHECKs. No
            # UPDATE follows: every existing row takes the 'company' server default.
            with _sqlite_rebuild_safe(), op.batch_alter_table(table) as batch:
                for column in work.columns:
                    batch.add_column(column)
                for name, referred_table, local_column in work.foreign_keys:
                    batch.create_foreign_key(name, referred_table, [local_column], ["id"])
                for name, sql in work.checks:
                    batch.create_check_constraint(name, sa.text(sql))
        if work.index:
            index_name, index_columns = work.change.index
            op.create_index(index_name, table, list(index_columns), unique=False)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY: discards which stock rows and tools a customer or supplier owns, and every owned row
    # then reads as company stock. See the module docstring -- a code rollback needs
    # ``alembic stamp 128_po_flowdown``, not this.
    offline = _offline()
    pending = [work for work in (_present(change, offline) for change in reversed(TABLE_CHANGES)) if _has_work(work)]
    if not pending:
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    for work in pending:
        table = work.change.table
        if work.index:
            # Before the batch: a SQLite rebuild re-creates every surviving index, and this one
            # names ``ownership``.
            op.drop_index(work.change.index[0], table_name=table)
        if _has_batch_work(work):
            with _sqlite_rebuild_safe(), op.batch_alter_table(table) as batch:
                for name, _sql in reversed(work.checks):
                    batch.drop_constraint(name, type_="check")
                for name, _referred_table, _local_column in reversed(work.foreign_keys):
                    batch.drop_constraint(name, type_="foreignkey")
                for column in reversed(work.columns):
                    batch.drop_column(column.name)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
