"""Restore ``company_id`` on the three QMS tables a create_all + stamp bootstrap left without it

Revision ID: 126_qms_company_id
Revises: 125_work_order_comments
Create Date: 2026-11-18

What production is missing, and why
-----------------------------------
A read-only comparison of production against ``Base.metadata`` (2026-11-18, alembic_version =
``125_work_order_comments``) found every model table and column present EXCEPT ``company_id`` on
``qms_standards``, ``qms_clauses`` and ``qms_clause_evidence``. All three tables held zero rows.

The QMS tables were built by ``Base.metadata.create_all()`` while the QMS models still had no
``company_id``, and the database was then ``alembic stamp``-ed past ``026_add_multi_tenancy``,
whose ``TENANT_TABLES`` adds the column to exactly these three tables. Commit ``59dbf26d``
(2026-06-05) later gave the three models ``TenantMixin`` on the assumption that ``026`` had run.
``create_all`` never alters a table that already exists, and ``026`` was stamped over, so nothing
ever added the column. Every tenant-scoped QMS query on such a database fails with
``UndefinedColumn``.

This is the stamped-over-DDL class ``079`` (indexes) and ``080`` (constraints) repaired, with one
difference: the model mirror already exists. ``TenantMixin`` has declared the column since
``59dbf26d``, so every ``create_all`` database built since then has it, and so does every database
that really ran ``026``. Only a database whose QMS tables predate ``59dbf26d`` AND that was stamped
past ``026`` lacks it. This revision adds nothing to the models.

What it restores
----------------
For each of the three tables, exactly what ``create_all`` builds from ``TenantMixin``'s
``Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)`` (introspected on a
``create_all``-built PostgreSQL 16, 2026-11-18):

  * ``company_id integer NOT NULL``, no default (``TenantMixin`` declares no server default);
  * FOREIGN KEY ``<table>_company_id_fkey`` (company_id) REFERENCES companies (id), with no
    ``ON DELETE`` / ``ON UPDATE`` action (NO ACTION), not deferrable -- the name PostgreSQL gives
    the model's unnamed FK. On SQLite, which keeps no name for it, the column is added with the
    same unnamed inline ``REFERENCES``;
  * btree INDEX ``ix_<table>_company_id`` (company_id), not unique.

On PostgreSQL ``ADD COLUMN`` appends, so on a database that never had the column it lands last --
which is where ``create_all`` puts ``TenantMixin``'s column on these tables too.

Every object is guarded INDEPENDENTLY
-------------------------------------
Column, backfill, NOT NULL, FK and index each have their own probe, so the revision no-ops object
by object wherever the object already exists:

  * the column: added (nullable) only when absent;
  * the backfill: runs only while the column is nullable. A NOT NULL column holds no NULL, so on a
    ``create_all`` database (and one that ran ``026``) this revision issues no write at all;
  * NOT NULL: set only while the column is nullable;
  * the FK: added only when NO foreign key on ``(company_id) -> companies`` exists under ANY name.
    A database that ran ``026`` carries it as ``fk_<table>_company_id``; a ``create_all`` one as
    ``<table>_company_id_fkey``. Either counts (``080``'s equivalence rule): a column is never
    double-constrained;
  * the index: added only when ``ix_<table>_company_id`` is absent (``026`` and ``create_all``
    use the same name).

Re-running is a no-op, and so is running it on any database that already has the column.

Backfill: derived, never guessed
--------------------------------
Parent first, filling only rows whose ``company_id`` is still NULL (a value already present --
for instance one an operator set by hand, below -- is never overwritten):

1. ``qms_standards``: the company of its creator, ``users.company_id`` via ``created_by``. A
   creator who is a platform principal (``is_superuser``, or role ``PLATFORM_ADMIN``) does NOT
   count: a platform principal works inside other companies through the active-company context
   (``get_current_company_id``), so their own ``company_id`` is no evidence of which company a
   standard belongs to (CLAUDE.md: never read ``current_user.company_id`` for scoping).
2. ``qms_clauses``: the company of its standard (``standard_id``).
3. ``qms_clause_evidence``: the company of its clause (``clause_id``).

After each table's derivation, a row still without a company is assigned the database's company
ONLY IF the database holds exactly one company. That is not a guess: the column is NOT NULL and
references ``companies``, so with one company there is exactly one value it can ever hold. The
alternative -- failing -- would push an operator into editing ``created_by`` on a quality record
just to get past a migration, which is worse than the one possible answer. The count of rows
assigned this way is logged.

With two or more companies, nothing is guessed: if any row of any of the three tables is still
unattributed, the revision RAISES before NOT NULL, naming each table, the row count and the ids
(ids only, no content). On PostgreSQL the revision runs inside the boot's migration transaction,
so the added columns and every backfilled value roll back with it, and the API does not start
(``start.sh`` runs ``alembic upgrade head`` first). The fix is an owner decision, recorded by hand
and then respected by a re-run: add the column (``ALTER TABLE <t> ADD COLUMN IF NOT EXISTS
company_id integer``), set each listed row's owning company, and run ``alembic upgrade head``
again. Production has zero rows in all three tables, so none of this can trigger there.

Out of scope, deliberately: cross-row consistency the tenant column cannot express on its own
(a clause whose ``parent_clause_id`` names a clause of another standard, or evidence whose
``document_id`` names another company's document). Those are write-time guards in the QMS
service (#348 / #353), they need data this revision does not judge, and production has no rows.

Downgrade: deliberately leaves the column in place
--------------------------------------------------
``downgrade`` changes nothing, on every database. That is a decision, with in-repo precedent, not
a stub:

  * The column belongs to revision ``125``'s own schema. ``TenantMixin`` declares it on all three
    models at ``125`` (since ``59dbf26d``), and on the migrated path ``026`` created it. A downgrade
    to ``125`` has to hand back a schema the ``125`` code can run against, and that schema has the
    column. Dropping it would recreate exactly the defect this revision repairs -- every
    tenant-scoped QMS query failing -- on production, and would break every ``create_all`` and
    ``026``-migrated database, where this revision added nothing in the first place.
  * ``079`` / ``080`` drop what they restored because they ALSO added the model mirrors: their
    previous revision's models did not declare those indexes and constraints, so dropping them
    returned the schema to what ``078`` / ``079`` declared. Followed faithfully, the same rule here
    -- return to what the previous revision declares -- keeps the column.
  * It matches ``038`` / ``069`` (re-assert what ``004`` owns; downgrade does not drop the
    ``version`` column) and ``060`` (does not drop ``008``'s triggers, whose lifecycle is ``008``'s).
  * A drop would also be lossy: the tenant attribution of every row written after this revision
    cannot be reliably re-derived (a standard with no non-platform creator, in a multi-company
    database, is attributable only by a person).

``alembic downgrade -1`` therefore moves ``alembic_version`` back to ``125`` and leaves the three
columns, their FKs, indexes and values exactly as they were, which is the state the ``125`` code
needs. An application rollback can use it, or ``alembic stamp 125_work_order_comments`` -- the two
are equivalent here. A re-upgrade is a no-op.

Locks and operations
--------------------
``SET LOCAL lock_timeout = '5s'`` (precedent ``059`` / ``060`` / ``114`` / ``117`` / ``123`` -
``125``). On a database that needs the repair: ``ADD COLUMN`` (nullable, no default) is
catalog-only; ``SET NOT NULL`` scans the table under ACCESS EXCLUSIVE; ``ADD CONSTRAINT ... FOREIGN
KEY`` validates the column and briefly takes SHARE ROW EXCLUSIVE on ``companies`` (blocks writes to
``companies``, not reads); ``CREATE INDEX`` takes SHARE on its table. All of it runs in the one
boot transaction, so the QMS tables stay locked until it commits. That is milliseconds for tables
the size of a quality-standards library (production: zero rows), and it keeps the whole repair
atomic -- the refusal above leaves nothing behind -- which is why this revision does not use
``080``'s ``NOT VALID`` / ``VALIDATE`` split or ``079``'s ``CONCURRENTLY``. On a database that
already has the column, the only statements are catalog reads plus the ``lock_timeout`` pair.

Nothing here reads or writes ``audit_log``, names a Supabase role, or creates a table (so no RLS
statement is owed: the tables keep their existing posture). Non-PostgreSQL (the SQLite
``create_all`` path) no-ops through the same guards; the SQLite repair branch exists so the
derivation and the refusal are exercised by the blocking test suite
(``tests/test_migration_126_qms_company_id.py``).

Revision id is 18 characters (<= 32; see docs/DEVELOPMENT.md -> revision ids).
"""

import logging

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "126_qms_company_id"
down_revision = "125_work_order_comments"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

STANDARDS = "qms_standards"
CLAUSES = "qms_clauses"
EVIDENCE = "qms_clause_evidence"

# Parent first: each table's backfill reads the company of the table before it.
TABLES = (STANDARDS, CLAUSES, EVIDENCE)

COLUMN = "company_id"
REFERRED_TABLE = "companies"

# How many unattributed ids the refusal lists per table (the count is always exact).
MAX_LISTED_IDS = 20


def fk_name(table_name: str) -> str:
    """The name PostgreSQL gives ``TenantMixin``'s unnamed FK under ``create_all``."""
    return f"{table_name}_{COLUMN}_fkey"


def index_name(table_name: str) -> str:
    """SQLAlchemy's ``ix_%(column_0_label)s`` for ``Column(..., index=True)`` -- also ``026``'s name."""
    return f"ix_{table_name}_{COLUMN}"


# Where each table's company comes from, parent first: a correlated subquery returning at most one
# value (every join is on a primary key).
#
# A platform principal's own company is not evidence (module docstring): ``is_superuser`` or role
# PLATFORM_ADMIN, compared case-insensitively because production's ``userrole`` enum may carry
# either the member NAMES (``PLATFORM_ADMIN``) or the values (``platform_admin``) -- see ``026``.
DERIVATION_SQL = {
    STANDARDS: (
        "SELECT u.company_id FROM users u "
        "WHERE u.id = qms_standards.created_by "
        "AND COALESCE(u.is_superuser, FALSE) = FALSE "
        "AND UPPER(CAST(u.role AS VARCHAR(32))) <> 'PLATFORM_ADMIN'"
    ),
    CLAUSES: "SELECT s.company_id FROM qms_standards s WHERE s.id = qms_clauses.standard_id",
    EVIDENCE: "SELECT c.company_id FROM qms_clauses c WHERE c.id = qms_clause_evidence.clause_id",
}


def backfill_sql(table_name: str) -> str:
    """Fill ``company_id`` where it is still NULL AND the derivation yields a company, so a row it
    cannot attribute is never touched (and the logged count is exactly the rows attributed).
    Dialect-neutral (PostgreSQL and SQLite), rendered verbatim in offline ``--sql`` mode."""
    source = DERIVATION_SQL[table_name]
    return f"UPDATE {table_name} SET company_id = ({source}) WHERE company_id IS NULL AND ({source}) IS NOT NULL"


def sole_company_sql(table_name: str) -> str:
    """Assign the database's ONLY company to rows still unattributed -- a no-op unless exactly one
    company exists, when the NOT NULL FK admits exactly that one value (module docstring)."""
    return (
        f"UPDATE {table_name} SET company_id = (SELECT MIN(id) FROM companies) "
        "WHERE company_id IS NULL AND (SELECT COUNT(*) FROM companies) = 1"
    )


# Offline (``--sql``) twin of ``_refuse_unattributed``: there is no database to read, so the
# rendered script refuses on its own, before SET NOT NULL.
OFFLINE_REFUSAL_SQL = (
    "DO $$ BEGIN\n"
    "  IF EXISTS (SELECT 1 FROM qms_standards WHERE company_id IS NULL)\n"
    "     OR EXISTS (SELECT 1 FROM qms_clauses WHERE company_id IS NULL)\n"
    "     OR EXISTS (SELECT 1 FROM qms_clause_evidence WHERE company_id IS NULL) THEN\n"
    "    RAISE EXCEPTION '126_qms_company_id: QMS rows have no attributable company. "
    "Set company_id on them by hand, then re-run. See the revision docstring.';\n"
    "  END IF;\n"
    "END $$"
)

# PostgreSQL only, and LOCAL: bounds the wait for the locks the repair takes, then hands the
# normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


class UnattributedQmsRowsError(RuntimeError):
    """Raised when a QMS row has no company this revision can derive (multi-company database)."""


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the repair a database without the column needs.
    return bool(op.get_context().as_sql)


def _dialect() -> str:
    return op.get_context().dialect.name


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    if _offline():
        return True
    return _inspector().has_table(table_name)


def _company_column(table_name: str):
    """The reflected ``company_id`` column, or None when it is absent (always absent offline)."""
    if _offline():
        return None
    return next((col for col in _inspector().get_columns(table_name) if col["name"] == COLUMN), None)


def _is_nullable(table_name: str) -> bool:
    if _offline():
        return True
    column = _company_column(table_name)
    return column is None or bool(column["nullable"])


def _has_company_fk(table_name: str) -> bool:
    """Any FK on exactly ``(company_id)`` referencing ``companies``, whatever its name: ``026``
    named it ``fk_<table>_company_id``, ``create_all`` leaves it to PostgreSQL's default."""
    if _offline():
        return False
    return any(
        list(fk.get("constrained_columns") or ()) == [COLUMN] and fk.get("referred_table") == REFERRED_TABLE
        for fk in _inspector().get_foreign_keys(table_name)
    )


def _has_index(table_name: str) -> bool:
    if _offline():
        return False
    return any(ix.get("name") == index_name(table_name) for ix in _inspector().get_indexes(table_name))


def _add_column(table_name: str) -> None:
    if _dialect() == "sqlite":
        # SQLite cannot add a constraint later without rebuilding the table, but it accepts an
        # inline REFERENCES on ADD COLUMN when the column is nullable with no default. That keeps
        # the FK unnamed, exactly as create_all builds it there.
        op.execute(f"ALTER TABLE {table_name} ADD COLUMN {COLUMN} INTEGER REFERENCES {REFERRED_TABLE} (id)")
    else:
        # Nullable, no default: a catalog-only change on PostgreSQL. The FK is added by its own
        # guarded step below, under create_all's name.
        op.add_column(table_name, sa.Column(COLUMN, sa.Integer(), nullable=True))


def _execute_counting(sql: str):
    """Run a backfill statement; return its row count online, None offline."""
    if _offline():
        op.execute(sql)
        return None
    return op.get_bind().execute(sa.text(sql)).rowcount


def _backfill(table_name: str) -> None:
    derived = _execute_counting(backfill_sql(table_name))
    sole = _execute_counting(sole_company_sql(table_name))
    if not _offline():
        log.info(
            "126_qms_company_id: %s -- %s row(s) attributed by derivation, %s to the sole company",
            table_name,
            derived,
            sole,
        )


def _unattributed(table_name: str):
    """(count, first ids) of rows in ``table_name`` whose company_id is still NULL."""
    bind = op.get_bind()
    count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name} WHERE {COLUMN} IS NULL")).scalar_one()
    ids = [
        row[0]
        for row in bind.execute(
            sa.text(f"SELECT id FROM {table_name} WHERE {COLUMN} IS NULL ORDER BY id LIMIT {MAX_LISTED_IDS}")
        )
    ]
    return count, ids


def _refuse_unattributed(table_names) -> None:
    if _offline():
        op.execute(OFFLINE_REFUSAL_SQL)
        return
    findings = {}
    for table_name in table_names:
        count, ids = _unattributed(table_name)
        if count:
            findings[table_name] = (count, ids)
    if not findings:
        return
    companies = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM companies")).scalar_one()
    lines = [
        "126_qms_company_id: refusing to guess which company owns "
        f"{sum(count for count, _ in findings.values())} QMS row(s):"
    ]
    for table_name, (count, ids) in findings.items():
        more = "" if count <= len(ids) else f" (first {len(ids)} listed)"
        lines.append(f"  {table_name}: {count} row(s) without a company, ids {ids}{more}")
    lines += [
        "A standard's company is its creator's (users.company_id via created_by; a superuser or "
        "PLATFORM_ADMIN creator does not count), a clause inherits its standard's and evidence its "
        "clause's, and a row is given the database's company only when exactly one exists. This "
        f"database holds {companies} companies.",
        "On PostgreSQL this revision has rolled back: no column was added and no row changed.",
        "Deciding each row's owner is a person's call. Record it, then re-run `alembic upgrade head` "
        "(this revision keeps every company_id already set and fills only NULLs):",
        "  ALTER TABLE <table> ADD COLUMN IF NOT EXISTS company_id integer;",
        "  UPDATE <table> SET company_id = <owning company id> WHERE id IN (<ids above>);",
    ]
    raise UnattributedQmsRowsError("\n".join(lines))


def _enforce(table_name: str) -> None:
    """NOT NULL, then the FK, each only if missing. The index is created separately."""
    nullable = _is_nullable(table_name)
    missing_fk = not _has_company_fk(table_name)
    if not (nullable or missing_fk):
        return
    if _dialect() == "sqlite":
        # SQLite cannot ALTER a column's nullability or add a constraint: one batch rebuild does
        # both (precedent 123 / 125). The FK added here only covers a column that already existed
        # without one -- a state no create_all-built SQLite database is in -- and batch mode
        # refuses an unnamed constraint, so it carries PostgreSQL's name (as in 125).
        with op.batch_alter_table(table_name) as batch_op:
            if nullable:
                batch_op.alter_column(COLUMN, existing_type=sa.Integer(), nullable=False)
            if missing_fk:
                batch_op.create_foreign_key(fk_name(table_name), REFERRED_TABLE, [COLUMN], ["id"])
        return
    if nullable:
        # Every row is attributed (``_refuse_unattributed`` ran first), so this cannot fail on data.
        op.alter_column(table_name, COLUMN, existing_type=sa.Integer(), nullable=False)
    if missing_fk:
        # No ON DELETE / ON UPDATE: TenantMixin's ForeignKey("companies.id") declares none.
        op.create_foreign_key(fk_name(table_name), table_name, REFERRED_TABLE, [COLUMN], ["id"])


def upgrade() -> None:
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    present = [table_name for table_name in TABLES if _has_table(table_name)]

    # 1. The column, nullable, wherever it is absent.
    for table_name in present:
        if _company_column(table_name) is None:
            _add_column(table_name)

    # 2. Backfill, parent first, only while the column is nullable: a NOT NULL column holds no
    #    NULL, so a create_all / 026 database sees no write here at all.
    repairing = [table_name for table_name in present if _is_nullable(table_name)]
    for table_name in repairing:
        _backfill(table_name)

    # 3. Refuse -- before any NOT NULL -- if a row is still unattributed.
    if repairing:
        _refuse_unattributed(repairing)

    # 4. NOT NULL, FK and index, each guarded on its own.
    for table_name in present:
        _enforce(table_name)
        if not _has_index(table_name):
            op.create_index(index_name(table_name), table_name, [COLUMN], unique=False)

    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    """Deliberately changes nothing: ``company_id`` stays on all three tables.

    The column, its FK and its index are part of revision ``125``'s schema (``TenantMixin`` on the
    three models, ``026`` on the migrated path), so going back to ``125`` must keep them; dropping
    them would reinstate the defect this revision repairs and lose tenant attribution. See
    "Downgrade" in the module docstring for the full argument and its precedents (``038``,
    ``060``, ``069``).
    """
    return None
