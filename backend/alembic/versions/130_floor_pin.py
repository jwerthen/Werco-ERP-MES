"""Floor PIN for badge / employee-ID sign-on: PIN columns on users, the policy switch on companies

Revision ID: 130_floor_pin
Revises: 129_owned_property
Create Date: 2026-11-19

Context
-------
``POST /auth/employee-login`` (the Login page's Employee-ID mode, the solo kiosk, badge scans) and
``POST /auth/kiosk-badge-token`` (the crew station) authenticate on the badge / employee ID alone,
so a completion or inspection recorded in that session is weak evidence of who did it (AS9100D
8.5.2 / 7.5.3). The PIN step that closes that gap lives in the auth services; this revision adds
only the columns it reads, every one guarded on its own:

``users`` (``User``):

  - ``floor_pin_hash``            String(255) NULL -- the bcrypt hash of the person's floor PIN.
    NULL = no PIN. Never serialized, logged or audited; no index (a PIN is verified against the
    one user the badge already resolved, never looked up).
  - ``floor_pin_set_at``          DateTime NULL -- when the PIN was last set (naive UTC).
  - ``floor_pin_failed_attempts`` Integer NOT NULL, server default ``'0'`` -- CUMULATIVE wrong
    PINs since the last correct PIN or admin unlock / set / clear (a lock does not reset it).
  - ``floor_pin_locked_until``    DateTime NULL -- the PIN lockout (naive UTC). Every lock
    expires: the tiers escalate from 15 minutes to a 7-day cap, never to a permanent lock.
  - ``floor_pin_reset_required``  Boolean NOT NULL, server default ``sa.false()`` -- set when a
    promotion into Admin / Manager / Quality cleared a PIN; badge sign-on is refused until an
    Admin sets a new PIN or clears it.

  These are deliberately separate from ``failed_login_attempts`` / ``locked_until``, which stay
  untouched: a PIN lockout never locks the person's password login, and the reverse.

``companies`` (``Company``):

  - ``require_floor_pin``         Boolean NOT NULL, server default ``'false'`` -- when on, a badge
    whose user has no PIN is refused instead of signed in.

No backfill: behavior is unchanged after upgrade (deliberate)
-------------------------------------------------------------
No data is backfilled. After upgrade NO user has a PIN (every ``floor_pin_hash`` is NULL, every
counter 0, nothing locked, no reset required) and EVERY company is off
(``require_floor_pin`` false), which is the
truthful state and exactly what the sign-on code treats as "no PIN step": badge sign-on behaves
as it does today until a supervisor sets a PIN or an Admin turns the policy on -- each a reasoned,
audited act of a person, never a migration's. ``audit_log`` is untouched. PostgreSQL receives no
``UPDATE`` at all; the one SQLite-only statement below rewrites how SQLite STORES the default
``false`` on pre-existing rows, never what any row means.

The ``floor_pin_failed_attempts`` and ``require_floor_pin`` server defaults are plain strings,
spelled exactly as on the models and as ``054``'s ``allow_ai_egress`` (``server_default="false"``)
-- never ``sa.text`` (``072`` used ``sa.text("false")`` for ``allow_sms_egress``, which renders a
different DEFAULT on SQLite and is one of the accepted ``059``-``088`` differences the
fresh-bootstrap round-trip stays above). A plain string also keeps the SQLite batch from needing a
rebuild (below). The later boolean, ``floor_pin_reset_required``, uses ``sa.false()`` on model
and migration alike: it renders ``DEFAULT false`` on PostgreSQL and ``DEFAULT 0`` on SQLite, so
that column carries no TEXT-``'false'`` artifact and needs no SQLite normalization.

SQLite storage normalization (local / test databases only; PostgreSQL is untouched): SQLite
stores a plain ``DEFAULT 'false'`` as the TEXT ``'false'`` for a row that existed before the
column was added, and SQLAlchemy's SQLite Boolean reads any non-empty string as True. Left alone,
every company on an incrementally-migrated SQLite dev database would read ``require_floor_pin``
True through the ORM and every badge user without a PIN would be locked out of local dev. So on
SQLite only, right after ``require_floor_pin`` is added, the upgrade runs
``UPDATE companies SET require_floor_pin = 0 WHERE require_floor_pin = 'false'``. This is a
DIALECT-STORAGE FIX, NOT A DATA BACKFILL: ``'false'`` and ``0`` are the same value (false); only
the TEXT spelling SQLite's ALTER left behind is replaced by the integer the ORM writes. It runs
only when this upgrade added the column, so a re-run never touches a value. PostgreSQL casts
``'false'`` to boolean false itself and gets no ``UPDATE``. (``allow_ai_egress`` /
``allow_sms_egress`` carry the same pre-existing artifact on such a database; this revision does
not touch them.) The integer default has no such artifact (SQLite converts ``'0'`` to 0).

Tenant / compliance shape
-------------------------
- ``users`` is an existing per-company table (``company_id`` NOT NULL + index) and ``companies``
  is the tenant root; RLS has been on for both since ``059``. This revision creates no table, so
  the ENABLE-RLS new-table convention does not apply, and it emits no RLS / ``REVOKE`` /
  role-naming statement. Existing table-level grants and revokes cover new columns.
- Neither table is soft-deletable; nothing here deletes a row.

Lock-step with the models (the stamped-over hazard)
---------------------------------------------------
Every column (name, type, nullability, server default) is declared on ``User``
(``app/models/user.py``) and ``Company`` (``app/models/company.py``) in the same PR, so the
``create_all`` -> stamp -> upgrade bootstrap builds them from the models and every guard here
no-ops. ``tests/test_migration_130_floor_pin.py`` pins model == migration == the PR contract, and
``tests/test_migration_fresh_bootstrap.py``'s ``downgrade 088`` -> ``upgrade head`` round-trip
diffs the result against ``create_all``.

Idempotent and reversible
-------------------------
- Every column is guarded independently by ``_has_column``. A re-run, the bootstrap path, or a
  partially built schema converges column by column.
- Each table's change runs through ``batch_alter_table(..., recreate="never")``: on PostgreSQL
  that is plain ``ALTER TABLE``; on SQLite it is SQLite's NATIVE ``ALTER TABLE ... ADD COLUMN`` /
  ``DROP COLUMN`` (the latter needs SQLite 3.35+, precedent ``123``), never the copy-and-rename
  rebuild. A rebuild is exactly what must not happen to these two tables: dozens of tables
  reference ``users`` and every tenant table references ``companies``, some ``ON DELETE CASCADE``
  (``user_workspace``, ``runtime_metric``), and a rebuild's ``DROP TABLE`` under
  ``PRAGMA foreign_keys = ON`` runs an implicit ``DELETE`` that would cascade-delete those
  children (or abort on a NO ACTION one). Plain columns with plain-string defaults need no
  rebuild, and a native ``DROP COLUMN`` accepts them (no index, constraint, trigger or view names
  them).
- Triggers: no trigger is ON either table and none -- in the models' ``after_create`` DDL or in
  any migration -- has a body naming ``users`` or ``companies``. The ``PRAGMA legacy_alter_table``
  wrapper ``129`` needed is for a rebuild's drop + rename, which never happens here, so it is not
  used; the native ``ALTER`` keeps the table in place and any such trigger resolving (the test
  proves it with one).
- Downgrade drops exactly what upgrade added, in reverse: ``companies`` before ``users``, and
  within a table the columns in reverse. Each drop is guarded.
- ``--sql`` (offline) has no database to inspect, so it renders the canonical path.

Downgrade is LOSSY -- read before running it
--------------------------------------------
Dropping the columns discards every floor PIN (each person must be issued a new one after a
re-upgrade), every PIN lockout in force, and each company's policy -- a company that required PINs
returns to badge-only sign-on, and on re-upgrade it reads off again. The PIN-set and policy
audit rows still record that a PIN was set (never its value) and what the policy was, but
nothing re-derives the columns from them.

Pre-130 code never reads these columns, and its inserts satisfy both NOT NULL columns through the
server defaults, so a CODE rollback does NOT need this downgrade -- it needs a STAMP: from a
checkout of the CURRENT release run ``alembic stamp 129_owned_property`` (this revision's
``down_revision``) before redeploying the older image. The PINs and the policy stay, and the next
forward upgrade re-runs this revision as a guarded no-op that keeps them. Even under a stamp the
OLD code has no PIN step: while it runs, every badge signs in on the badge alone, whatever the
policy says.

Locking / operations note
-------------------------
- Every ``ADD COLUMN`` is metadata-only on PostgreSQL 11+: the nullable columns have no default,
  and the two NOT NULL columns carry a constant server default stored once in the catalog
  (``attmissingval``) -- no table rewrite, no NOT NULL verification scan. ``DROP COLUMN`` is
  metadata-only too. No index, FK or CHECK is created, so nothing scans either table.
- Each table still takes an ACCESS EXCLUSIVE lock, held to the end of the migration transaction.
  ``users`` is read by every authenticated request, so the wait is what matters:
  ``SET LOCAL lock_timeout = '5s'`` bounds it (and so bounds how long requests queue behind it)
  when there is DDL to run, and is handed back (``SET LOCAL lock_timeout = DEFAULT``) as soon as it
  is done (precedent 114-117, 122, 127-129). A timeout rolls the whole migration back cleanly.
- Lock order is ``users`` then ``companies``, the order request authentication reads them in
  (the user, then its company), so the migration never waits on a request that waits on it.

Deploy ordering: run BEFORE (or with) the release that maps the columns -- the migration runs at
API boot (``backend/start.sh``). Old code is safe on the new schema (it never selects the columns,
and its inserts take the defaults). New code on the un-migrated schema is NOT: every user and
company load would select missing columns, failing every sign-in and authenticated request, so
deploy the API before or with the ARQ worker.

Revision id ``130_floor_pin`` is 13 chars (<= 32, ``alembic_version.version_num``).
"""

from typing import Callable, List, NamedTuple, Tuple

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "130_floor_pin"
down_revision = "129_owned_property"
branch_labels = None
depends_on = None

USERS = "users"
COMPANIES = "companies"

# The server defaults of the first two NOT NULL columns. Plain strings, exactly as on the models
# and 054's ``allow_ai_egress`` (never ``sa.text``, which renders a different DEFAULT on SQLite,
# breaks the fresh-bootstrap diff and would force a SQLite batch rebuild). The later boolean
# (``floor_pin_reset_required``) uses ``sa.false()`` on model and migration alike (module docstring).
DEFAULT_FAILED_ATTEMPTS = "0"
DEFAULT_REQUIRE_FLOOR_PIN = "false"

# PostgreSQL only, and LOCAL: bounds the wait for each lock, then hands the normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"

# SQLite only: a dialect-storage fix, NOT a data backfill (module docstring). SQLite's ADD COLUMN
# leaves pre-existing rows holding the default as the TEXT 'false', which SQLAlchemy's SQLite
# Boolean reads as True; this re-spells that same false as the integer 0 the ORM writes.
SQLITE_NORMALIZE_REQUIRE_FLOOR_PIN_SQL = "UPDATE companies SET require_floor_pin = 0 WHERE require_floor_pin = 'false'"

# Native ALTER on SQLite, never a copy-and-rename rebuild of ``users`` / ``companies`` (see the
# module docstring). On PostgreSQL batch mode never recreates, so this is the same plain ALTER.
BATCH_RECREATE = "never"


def _user_columns() -> List[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only), in model order."""
    return [
        sa.Column("floor_pin_hash", sa.String(length=255), nullable=True),
        sa.Column("floor_pin_set_at", sa.DateTime(), nullable=True),
        sa.Column("floor_pin_failed_attempts", sa.Integer(), nullable=False, server_default=DEFAULT_FAILED_ATTEMPTS),
        sa.Column("floor_pin_locked_until", sa.DateTime(), nullable=True),
        sa.Column("floor_pin_reset_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    ]


def _company_columns() -> List[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only)."""
    return [
        sa.Column("require_floor_pin", sa.Boolean(), nullable=False, server_default=DEFAULT_REQUIRE_FLOOR_PIN),
    ]


USER_COLUMN_NAMES = tuple(column.name for column in _user_columns())
COMPANY_COLUMN_NAMES = tuple(column.name for column in _company_columns())


class _TableChange(NamedTuple):
    """Everything this revision adds to one table: its columns, nothing else."""

    table: str
    columns: Callable[[], List[sa.Column]]


# Upgrade order (the lock order the docstring states); downgrade walks it reversed.
TABLE_CHANGES: Tuple[_TableChange, ...] = (
    _TableChange(USERS, _user_columns),
    _TableChange(COMPANIES, _company_columns),
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


def _missing(change: _TableChange, offline: bool) -> List[sa.Column]:
    return [column for column in change.columns() if offline or not _has_column(change.table, column.name)]


def _present(change: _TableChange, offline: bool) -> List[sa.Column]:
    return [column for column in change.columns() if offline or _has_column(change.table, column.name)]


def upgrade() -> None:
    offline = _offline()
    pending = [(change.table, columns) for change in TABLE_CHANGES if (columns := _missing(change, offline))]
    if not pending:
        return  # the create_all bootstrap (or a re-run) already built every column

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    for table, columns in pending:
        # No backfill follows: no user has a PIN, the counter takes '0', every company reads off.
        with op.batch_alter_table(table, recreate=BATCH_RECREATE) as batch:
            for column in columns:
                batch.add_column(column)
        if table == COMPANIES and _is_sqlite() and any(column.name == "require_floor_pin" for column in columns):
            # Dialect-storage fix, not a backfill: re-spell SQLite's TEXT 'false' as the integer 0
            # (the same false) so the ORM does not read every pre-existing company as True.
            op.execute(SQLITE_NORMALIZE_REQUIRE_FLOOR_PIN_SQL)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY: discards every floor PIN, PIN lockout and company policy. See the module docstring --
    # a code rollback needs ``alembic stamp 129_owned_property``, not this.
    offline = _offline()
    pending = [(change.table, columns) for change in reversed(TABLE_CHANGES) if (columns := _present(change, offline))]
    if not pending:
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    for table, columns in pending:
        with op.batch_alter_table(table, recreate=BATCH_RECREATE) as batch:
            for column in reversed(columns):
                batch.drop_column(column.name)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
