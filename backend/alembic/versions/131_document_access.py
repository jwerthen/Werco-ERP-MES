"""Document access levels and the per-user export authorization (AS9100D 7.5.3.1 / 7.5.3.2)

Revision ID: 131_document_access
Revises: 130_floor_pin
Create Date: 2026-11-19

Context
-------
Every signed-in user can open every controlled document today, export-controlled customer
drawings included: no document read carries a role gate. The rule that closes that gap lives in
``app/services/document_access.py`` (one SQL clause and its Python twin); this revision adds only
the columns it reads, every object guarded on its own:

``documents`` (``Document``):

  - ``access_level``  String(30) NOT NULL, server default ``'standard'``.
  - CHECK ``ck_documents_access_level``:
    ``access_level IN ('standard', 'restricted', 'export_controlled')`` (a String + CHECK rather
    than a native PostgreSQL enum, precedent ``122`` / ``127`` / ``129``, so a fourth level later
    is a CHECK swap, not an ``ALTER TYPE``).
  - index ``ix_documents_access_level`` on ``(company_id, access_level)``, for the per-company
    access-level filter and the access review.

``users`` (``User``):

  - ``export_authorized``     Boolean NOT NULL, server default ``sa.false()`` -- the ONLY thing that
    opens an ``export_controlled`` document; no role bypasses it.
  - ``export_authorized_at``  DateTime NULL -- when it was granted (naive UTC).
  - ``export_authorized_by``  Integer NULL, FK ``fk_users_export_authorized_by`` -> ``users.id``,
    no ``ON DELETE`` (NO ACTION): who granted it. Users are deactivated, never physically
    deleted, so a grant always keeps naming its grantor.

No backfill beyond the server defaults (deliberate)
---------------------------------------------------
No data is backfilled. After upgrade EVERY document is ``'standard'`` and NOBODY is
export-authorized (``export_authorized`` false, ``_at`` / ``_by`` NULL), which is the truthful
state and exactly what the access rule treats as "visible to anyone who can read documents
today": behavior is unchanged until Quality flags a document or an Admin grants an authorization
-- each a reasoned, audited act of a person (``POST /documents/{id}/access-level``,
``PUT /users/{id}/export-authorization``), never a migration's inference. Nothing here guesses
which existing documents are export-controlled; the access review lists the candidates.
``audit_log`` is untouched, and no dialect receives an ``UPDATE``.

The server defaults are spelled exactly as on the models. ``access_level`` takes the plain string
``'standard'``. ``export_authorized`` takes ``sa.false()`` -- NOT the plain string ``"false"``
that ``130``'s ``require_floor_pin`` / ``054``'s ``allow_ai_egress`` use -- because it is a
security flag that must fail CLOSED: SQLite stores a string ``DEFAULT 'false'`` as the TEXT
``'false'``, and SQLAlchemy's SQLite Boolean reads any non-empty string as True, so ANY insert that
omits the column (raw SQL, a test fixture, pre-131 code running during a stamp rollback) would make
that user export-authorized and open every export-controlled document. ``sa.false()`` renders
``DEFAULT false`` on PostgreSQL and ``DEFAULT 0`` on SQLite, which the ORM reads as False. Model
and migration use the same expression, so ``create_all`` and the migration chain render the same
DEFAULT and the fresh-bootstrap diff holds.

With ``DEFAULT 0``, a row filled by this revision's ``ADD COLUMN`` on SQLite (native, or a
rebuild's ``INSERT ... SELECT``, which omits the new column) stores the integer 0, so no SQLite
storage normalization is needed (an earlier draft carried one; it could only ever run where it
was a no-op, and was removed). Nothing in the codebase adds this column with a TEXT default.

Tenant / compliance shape
-------------------------
- ``documents`` is an EXISTING tenant table (TenantMixin ``company_id`` NOT NULL + index) and
  ``users`` an existing per-company table; RLS has been on for both since ``059``. This revision
  creates no table, so the ENABLE-RLS new-table convention does not apply, and it emits no RLS /
  ``REVOKE`` / role-naming statement. Existing table-level grants and revokes cover new columns.
- The new index leads with ``company_id``, so it serves tenant-scoped reads only.
- Neither table is soft-deletable (``documents`` has no ``SoftDeleteMixin``; ``users.is_active``
  is untouched). Nothing here deletes a row; the SQLite rebuild below carries every row across.

Lock-step with the models (the stamped-over hazard)
---------------------------------------------------
Every column, the CHECK text, the FK name and the index are declared on ``Document``
(``app/models/document.py``, the CHECK text from its ``DOCUMENT_ACCESS_LEVEL_CHECK``) and ``User``
(``app/models/user.py``) in the same PR, so the ``create_all`` -> stamp -> upgrade bootstrap builds
them from the models and every guard here no-ops. The CHECK text here is a frozen literal equal
to the PR contract; ``tests/test_migration_131_document_access.py`` pins it byte-identical to the
model's (and every column, the FK and the index to theirs), and
``tests/test_migration_fresh_bootstrap.py``'s ``downgrade 088`` -> ``upgrade head`` round-trip
diffs the result against ``create_all``, FK and CHECK names included.

Idempotent and reversible
-------------------------
- Every object is guarded independently (088 / 127 / 129 style): each column by ``_has_column``,
  the FK by name, the CHECK by name, the index by name. A re-run, the bootstrap path, or a
  partially built schema converges object by object.
- Downgrade drops exactly what upgrade added, in reverse: ``documents`` before ``users``; within
  ``documents`` the index, then the CHECK, then the column; within ``users`` the FK, then the
  columns in reverse. Each drop is guarded.
- ``--sql`` (offline) has no database to inspect, so it renders the canonical PostgreSQL path.
  Offline SQLite is not supported: the rebuild below reflects the live table (as ``129``).

SQLite: why ``users`` and ``documents`` ARE rebuilt here, and what keeps that safe
-------------------------------------------------------------------------------------
``130`` added plain columns to ``users`` with SQLite's native ``ALTER`` precisely to avoid a
rebuild. This revision cannot: SQLite's ``ALTER TABLE`` cannot add a table-level CHECK or FK, and
the alternatives were checked and rejected --

  - an inline ``ADD COLUMN ... CONSTRAINT fk_users_export_authorized_by REFERENCES users (id)`` (or
    an inline CHECK) is legal SQLite, but SQLAlchemy reflects an inline FK with NO name and an
    inline CHECK with a mangled text, so the schema would differ from the ``create_all`` one the
    fresh-bootstrap round-trip demands, object for object;
  - a native ``DROP COLUMN`` cannot remove a column that a table-level FK or CHECK names ("unknown
    column ... in foreign key definition"), and that is exactly how ``create_all`` builds them, so
    the downgrade over a bootstrapped schema has to rebuild anyway;
  - hand-editing ``sqlite_master`` under ``PRAGMA writable_schema`` is not a migration tool.

So on SQLite, a table change that adds or drops the FK / CHECK, or drops a column, runs through
``batch_alter_table`` (copy, ``DROP TABLE``, rename), and every other change (a plain column
alone, the index) stays native (``recreate="never"``). On PostgreSQL batch mode never rebuilds:
it is plain ``ALTER TABLE`` either way. Two hazards were checked, the ones ``129`` / ``130`` met:

  - **Cascades.** ``user_workspace_records.user_id`` is ``ON DELETE CASCADE`` against ``users``,
    and eleven provenance FKs (``created_by`` / ``approved_by`` / ``released_by`` /
    ``started_by`` / ``completed_by`` / ``processed_by`` / ``updated_by``) are
    ``ON DELETE SET NULL``; nothing references ``documents`` with an action (all NO ACTION). Under ``PRAGMA foreign_keys = ON`` a rebuild's ``DROP TABLE`` runs an implicit
    ``DELETE`` that would cascade-delete or null those rows (or, for a NO ACTION child, abort
    half-way). Neither the app (``app/db/database.py``) nor ``alembic/env.py`` turns foreign keys
    on -- SQLite's default is OFF, so on every real SQLite run the ``DROP TABLE`` is a plain drop
    and every child row stays exactly as it was. The pragma cannot be switched off inside a
    transaction, so instead of trusting that, the migration READS it before any DDL and, when it
    is ON and a rebuild is due, raises without having changed anything (fail closed). Children's
    ``REFERENCES users`` / ``REFERENCES documents`` are by name, so they resolve to the rebuilt
    table.
  - **Triggers.** No trigger is ON either table and none -- in the models' ``after_create`` DDL or
    in any migration -- has a body naming ``users`` or ``documents`` (the test pins it). Each
    rebuild still runs under ``PRAGMA legacy_alter_table = ON`` (``129``'s wrapper, the prior value
    put back after), so a later trigger that reads either table cannot break THIS revision's
    rebuild in the fresh-bootstrap round-trip, which runs 131's downgrade over every later
    revision's objects.

Downgrade is LOSSY -- and unsafe for export-controlled documents. Read before running it.
------------------------------------------------------------------------------------------
Dropping the columns discards every document's access level and every export authorization. On
a later re-upgrade every document reads ``'standard'`` again -- every restricted and
export-controlled document becomes openable by every signed-in user -- and nobody is
authorized. The access-level and authorization audit rows still record what was set, but nothing
re-derives the columns from them. Before running it, export the list
(``SELECT id, document_number, access_level FROM documents WHERE access_level <> 'standard'``)
and the authorized users, and re-apply both through the verbs afterwards.

Pre-131 code never reads these columns, and its inserts satisfy every NOT NULL and the CHECK
through the server defaults, so a CODE rollback does NOT need this downgrade -- it needs a STAMP:
from a checkout of the CURRENT release run ``alembic stamp 130_floor_pin`` (this revision's
``down_revision``) before redeploying the older image. The levels and the grants stay, and the
next forward upgrade re-runs this revision as a guarded no-op that keeps them. Even under a stamp
the OLD code has no access gate: while it runs, every signed-in user can open every document,
export-controlled ones included. If any document is flagged at rollback time, treat the rollback
window as an export-control exposure and keep it short.

Locking / operations note
-------------------------
- Every ``ADD COLUMN`` is metadata-only on PostgreSQL 11+: the nullable columns have no default,
  and the two NOT NULL columns carry a constant server default stored once in the catalog
  (``attmissingval``) -- no table rewrite, no NOT NULL verification scan.
- The FK validates against an all-NULL column (one quick scan of ``users``, which it references
  itself); the CHECK scans ``documents`` once (every row is ``'standard'``, which passes); the
  index is a plain ``CREATE INDEX`` (not CONCURRENTLY -- the revision runs inside the migration
  transaction). Both tables are small, so each scan is brief.
- Each table takes an ACCESS EXCLUSIVE lock, held to the end of the migration transaction.
  ``users`` is read by every authenticated request, so the wait is what matters:
  ``SET LOCAL lock_timeout = '5s'`` bounds every wait when there is DDL to run and is handed back
  (``SET LOCAL lock_timeout = DEFAULT``) as soon as it is done (precedent 114-117, 122, 127-130).
  A timeout rolls the whole migration back cleanly.
- Lock order is ``users`` then ``documents``, the order a document request reads them in
  (authentication loads the user, then the route reads the document), so the migration never
  waits on a request that waits on it.

Deploy ordering: run BEFORE (or with) the release that maps the columns -- the migration runs at
API boot (``backend/start.sh``). Old code is safe on the new schema (it never selects the
columns, its inserts take the defaults). New code on the un-migrated schema is NOT: every user
load would select missing columns, failing every sign-in and authenticated request, so deploy the
API before or with the ARQ worker. Owner order after deploy: grant export authorization to the
right people FIRST, then run the access review and flag documents -- a document flagged
``export_controlled`` while nobody is authorized opens for no one.

Revision id ``131_document_access`` is 19 chars (<= 32, ``alembic_version.version_num``).
"""

from contextlib import contextmanager
from typing import Callable, Iterator, List, NamedTuple, Optional, Tuple

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "131_document_access"
down_revision = "130_floor_pin"
branch_labels = None
depends_on = None

USERS = "users"
DOCUMENTS = "documents"

# The server defaults, exactly as on the models. ``export_authorized`` is ``sa.false()`` (renders
# ``false`` on PostgreSQL, ``0`` on SQLite), never the string "false", which SQLite stores as TEXT
# that the ORM reads as True: a security flag must fail closed (module docstring).
DEFAULT_EXPORT_AUTHORIZED = sa.false()
DEFAULT_ACCESS_LEVEL = "standard"

# Lock-step with ``app.models.document.DOCUMENT_ACCESS_LEVEL_CHECK`` (the test pins them equal).
ACCESS_LEVEL_CHECK_SQL = "access_level IN ('standard', 'restricted', 'export_controlled')"

# PostgreSQL only, and LOCAL: bounds the wait for each lock, then hands the normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"

# SQLite only: lets a rebuild's drop + rename survive a trigger whose body names the table (129).
LEGACY_ALTER_TABLE_PRAGMA = "PRAGMA legacy_alter_table"
FOREIGN_KEYS_PRAGMA = "PRAGMA foreign_keys"

FOREIGN_KEYS_ON_REFUSAL = (
    "131_document_access must rebuild {tables} on SQLite (SQLite's ALTER TABLE cannot add or drop "
    "a CHECK or FOREIGN KEY), and this connection has PRAGMA foreign_keys = ON: the rebuild's "
    "DROP TABLE would run an implicit DELETE that cascade-deletes or nulls every row referencing "
    "the table. Nothing was changed. Re-run with foreign keys off (SQLite's default, and what "
    "the app and alembic/env.py use)."
)


def _user_columns() -> List[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only), in model order. The FK
    is a separate, named constraint below: a ``ForeignKey`` on the Column would make SQLite's
    native ADD COLUMN path try an ALTER ADD CONSTRAINT it does not have."""
    return [
        sa.Column("export_authorized", sa.Boolean(), nullable=False, server_default=DEFAULT_EXPORT_AUTHORIZED),
        sa.Column("export_authorized_at", sa.DateTime(), nullable=True),
        sa.Column("export_authorized_by", sa.Integer(), nullable=True),
    ]


def _document_columns() -> List[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only)."""
    return [
        sa.Column("access_level", sa.String(length=30), nullable=False, server_default=DEFAULT_ACCESS_LEVEL),
    ]


USER_COLUMN_NAMES = tuple(column.name for column in _user_columns())
DOCUMENT_COLUMN_NAMES = tuple(column.name for column in _document_columns())


class _TableChange(NamedTuple):
    """Everything this revision adds to one table."""

    table: str
    columns: Callable[[], List[sa.Column]]
    # (constraint name, referred table, local column); every one references the referred ``id``.
    foreign_keys: Tuple[Tuple[str, str, str], ...]
    # (constraint name, SQL), in creation order.
    checks: Tuple[Tuple[str, str], ...]
    # (index name, columns), or None.
    index: Optional[Tuple[str, Tuple[str, ...]]]


# Upgrade order (the lock order the docstring states); downgrade walks it reversed.
TABLE_CHANGES: Tuple[_TableChange, ...] = (
    _TableChange(
        USERS,
        _user_columns,
        (("fk_users_export_authorized_by", USERS, "export_authorized_by"),),
        (),
        None,
    ),
    _TableChange(
        DOCUMENTS,
        _document_columns,
        (),
        (("ck_documents_access_level", ACCESS_LEVEL_CHECK_SQL),),
        ("ix_documents_access_level", ("company_id", "access_level")),
    ),
)


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


def _has_batch_work(work: _Pending) -> bool:
    return bool(work.columns or work.foreign_keys or work.checks)


def _has_work(work: _Pending) -> bool:
    return _has_batch_work(work) or work.index


def _rebuilds(work: _Pending, dropping: bool) -> bool:
    """Whether this table's batch is a copy-and-rename rebuild on SQLite: adding or dropping the
    FK / CHECK always is; dropping a column is (a create_all FK or CHECK may name it); a plain
    column added alone is not (native ADD COLUMN). Never on PostgreSQL."""
    if work.foreign_keys or work.checks:
        return True
    return dropping and bool(work.columns)


def _batch_recreate(work: _Pending, dropping: bool) -> str:
    # ``never`` pins the native ALTER whenever no rebuild is due; ``auto`` rebuilds on SQLite
    # (it is handed a constraint or a drop) and is plain ALTER TABLE on PostgreSQL.
    return "auto" if _rebuilds(work, dropping) else "never"


def _missing(change: _TableChange, offline: bool) -> _Pending:
    table = change.table
    return _Pending(
        change,
        [column for column in change.columns() if offline or not _has_column(table, column.name)],
        [fk for fk in change.foreign_keys if offline or not _has_foreign_key(table, fk[0])],
        [check for check in change.checks if offline or not _has_check_constraint(table, check[0])],
        change.index is not None and (offline or not _has_index(table, change.index[0])),
    )


def _present(change: _TableChange, offline: bool) -> _Pending:
    table = change.table
    return _Pending(
        change,
        [column for column in change.columns() if offline or _has_column(table, column.name)],
        [fk for fk in change.foreign_keys if offline or _has_foreign_key(table, fk[0])],
        [check for check in change.checks if offline or _has_check_constraint(table, check[0])],
        change.index is not None and (offline or _has_index(table, change.index[0])),
    )


def _refuse_rebuild_under_enforced_foreign_keys(pending: List[_Pending], dropping: bool) -> None:
    """Fail closed BEFORE any DDL when a SQLite rebuild is due on a connection enforcing foreign
    keys: its DROP TABLE would cascade-delete or null the rows referencing the table (module
    docstring). The pragma cannot be switched off inside a transaction, so it is read, not set."""
    if not _is_sqlite() or _offline():
        return
    tables = [work.change.table for work in pending if _rebuilds(work, dropping)]
    if tables and op.get_bind().exec_driver_sql(FOREIGN_KEYS_PRAGMA).scalar():
        raise RuntimeError(FOREIGN_KEYS_ON_REFUSAL.format(tables=" and ".join(tables)))


@contextmanager
def _sqlite_rebuild_safe(rebuild: bool) -> Iterator[None]:
    """Run a rebuilding ``batch_alter_table`` under ``PRAGMA legacy_alter_table = ON`` on SQLite,
    then put the connection's previous value back (129's wrapper). A no-op for a native ALTER and
    on every other dialect. Connection-scoped and never persisted, so nothing outlives the
    migration."""
    if not (rebuild and _is_sqlite()):
        yield
        return
    previous = 0 if _offline() else op.get_bind().exec_driver_sql(LEGACY_ALTER_TABLE_PRAGMA).scalar()
    op.execute(f"{LEGACY_ALTER_TABLE_PRAGMA} = ON")
    try:
        yield
    finally:
        op.execute(f"{LEGACY_ALTER_TABLE_PRAGMA} = {'ON' if previous else 'OFF'}")


def upgrade() -> None:
    offline = _offline()
    pending = [work for work in (_missing(change, offline) for change in TABLE_CHANGES) if _has_work(work)]
    if not pending:
        return  # the create_all bootstrap (or a re-run) already built every object
    _refuse_rebuild_under_enforced_foreign_keys(pending, dropping=False)

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    for work in pending:
        table = work.change.table
        if _has_batch_work(work):
            rebuild = _rebuilds(work, dropping=False)
            recreate = _batch_recreate(work, dropping=False)
            # Columns first (the FK and the CHECK name them), then the FK, then the CHECK. No
            # backfill follows: every document takes 'standard', every user reads unauthorized.
            with _sqlite_rebuild_safe(rebuild), op.batch_alter_table(table, recreate=recreate) as batch:
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
    # LOSSY, and on re-upgrade every export-controlled document reads 'standard' (visible to all).
    # See the module docstring -- a code rollback needs ``alembic stamp 130_floor_pin``, not this.
    offline = _offline()
    pending = [work for work in (_present(change, offline) for change in reversed(TABLE_CHANGES)) if _has_work(work)]
    if not pending:
        return
    _refuse_rebuild_under_enforced_foreign_keys(pending, dropping=True)

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    for work in pending:
        table = work.change.table
        if work.index:
            # Before the batch: a SQLite rebuild re-creates every surviving index, and this one
            # names ``access_level``.
            op.drop_index(work.change.index[0], table_name=table)
        if _has_batch_work(work):
            rebuild = _rebuilds(work, dropping=True)
            recreate = _batch_recreate(work, dropping=True)
            with _sqlite_rebuild_safe(rebuild), op.batch_alter_table(table, recreate=recreate) as batch:
                for name, _sql in reversed(work.checks):
                    batch.drop_constraint(name, type_="check")
                for name, _referred_table, _local_column in reversed(work.foreign_keys):
                    batch.drop_constraint(name, type_="foreignkey")
                for column in reversed(work.columns):
                    batch.drop_column(column.name)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
