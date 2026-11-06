"""Make customers.name and customers.code unique PER COMPANY instead of globally

Revision ID: 115_customers_unique_per_company
Revises: 114_wo_dispatch_rank
Create Date: 2026-11-05

Context
-------
``customers`` is a ``TenantMixin`` table, yet ``name`` and ``code`` were declared
``unique=True, index=True`` -- which ``create_all`` builds as two GLOBAL unique indexes,
``ix_customers_name`` and ``ix_customers_code``. No migration ever created this table
(it has only ever come from ``create_all``), and ``026``/``027`` widened the tenant
uniques of twelve other tables but never this one. So one tenant's customer name or code
blocked every other tenant from using it -- the refusal itself disclosing that the name
existed somewhere -- and the router's per-company code generator minted ``ACM001`` for a
second tenant only to have the global index reject it as a 500.

This revision replaces both global uniques with tenant-scoped ones and keeps a plain
single-column index on each column for lookups (the ``027`` vendor shape):

- ``ix_customers_name``          UNIQUE (name)              -> plain index on (name)
- ``ix_customers_code``          UNIQUE (code)              -> plain index on (code)
- ``uq_customers_company_name``  UNIQUE (company_id, name)  new
- ``uq_customers_company_code``  UNIQUE (company_id, code)  new

Defensively, any NAMED single-column unique CONSTRAINT on ``name`` or ``code`` (the
Postgres ``customers_name_key`` shape a ``unique=True`` column without ``index=True``
would have produced, or a constraint sharing the ``ix_`` name -- the three storage
shapes ``027`` drops) is dropped too. Neither is known to exist anywhere; the guard costs
one reflection.

No pre-flight dedup (verified)
------------------------------
Both new keys are strictly LOOSER than the ones they replace: adding ``company_id`` as a
leading column can only relax a unique key (precedent ``045``). Every row that satisfied
the global indexes satisfies the new constraints, so the upgrade cannot fail on data.
NULL codes stay legal and repeatable on both engines. Neither constraint has a partial
predicate: a soft-deleted customer keeps its name and code, which is what keeps a later
restore from colliding (the router's duplicate probes deliberately see tombstones).

The DOWNGRADE is the tightening direction. Once two companies hold the same name or
code, the global unique cannot be rebuilt, so ``downgrade()`` counts cross-row duplicates
FIRST and raises before any DDL runs, naming how many values collide (never the values --
they are tenant data and this lands in deploy logs). Resolving them is a data decision
for an operator, not something a migration may guess at.

Lock-step with the model (the stamped-over hazard)
--------------------------------------------------
``Customer.__table_args__`` declares both named ``UniqueConstraint`` s and the columns
keep ``index=True`` (now non-unique), so ``create_all`` builds exactly the post-115 shape
and the bootstrap (``create_all`` -> stamp ``058`` -> upgrade) runs this revision as a
clean no-op. ``tests/test_migration_115_customers_unique_per_company.py`` pins the names
and columns equal, and ``tests/test_migration_fresh_bootstrap.py``'s round-trip diffs the
re-upgraded schema against ``create_all``.

Idempotent and reversible (088 style -- every object guarded independently)
---------------------------------------------------------------------------
- Each legacy single-column unique constraint is dropped only if reflection finds it.
- Each ``ix_customers_<col>`` is probed for EXISTENCE and UNIQUENESS separately: a unique
  one is dropped and rebuilt plain, a missing one is built plain, a plain one is left
  alone.
- Each new ``uq_customers_company_<col>`` is created only if absent.
- Downgrade mirrors it: each ``uq_`` constraint dropped only if present, each ``ix_``
  index rebuilt UNIQUE only if it is not already.
- Constraint DDL runs inside ``batch_alter_table``: a plain ``ALTER`` on PostgreSQL, a
  table rebuild on SQLite (which cannot ``ALTER TABLE ... ADD/DROP CONSTRAINT``;
  precedent ``089``). Index DDL is dialect-neutral and runs outside the batch, after it.
- ``--sql`` (offline) has no database to inspect, so it emits the canonical path: the
  index rebuilds and both constraints (precedent ``089``).

Shape / compliance
------------------
Pure DDL over one EXISTING table: no row is read except by the downgrade's duplicate
count, none is written, and the tamper-evident ``audit_log`` chain is untouched. No
table is created, so the ENABLE-RLS new-table convention does not apply and no RLS /
REVOKE / role-naming statement is emitted (``customers`` has had RLS since ``059``).

Locking / operations note
-------------------------
On PostgreSQL ``ADD CONSTRAINT ... UNIQUE`` builds its index under an ACCESS EXCLUSIVE
lock on ``customers`` (the customer master: hundreds of rows, read by every picker), so
the window is short. ``SET LOCAL lock_timeout = '5s'`` is taken only when there is DDL to
run, so a long transaction holding ``customers`` fails the deploy fast instead of queueing
every picker read behind it, and is handed back (``SET LOCAL lock_timeout = DEFAULT``)
before the revision returns so it never leaks into a later revision in the same run
(precedent ``114``). Safe to apply before or after the app deploy: old code never relied
on cross-tenant uniqueness, and the looser key accepts every row the old one did.

Revision id ``115_customers_unique_per_company`` is 32 chars -- exactly the
``alembic_version.version_num`` varchar(32) ceiling on a freshly bootstrapped DB
(docs/DEVELOPMENT.md). Do not lengthen it.
"""

from typing import Dict, List, Optional

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "115_customers_unique_per_company"
down_revision = "114_wo_dispatch_rank"
branch_labels = None
depends_on = None

TABLE_NAME = "customers"
UNIQUE_COLUMNS = ("name", "code")

# column -> the single-column index create_all names ``ix_<table>_<column>``
INDEX_NAMES = {column: f"ix_{TABLE_NAME}_{column}" for column in UNIQUE_COLUMNS}
# column -> the tenant-scoped unique constraint that replaces the global one
CONSTRAINT_NAMES = {column: f"uq_{TABLE_NAME}_company_{column}" for column in UNIQUE_COLUMNS}


def _offline() -> bool:
    # ``--sql`` has no database to inspect: emit the canonical statements.
    return bool(op.get_context().as_sql)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _inspector():
    return sa.inspect(op.get_bind())


def _index_is_unique(index_name: str) -> Optional[bool]:
    """``None`` when the index is absent, otherwise whether it is UNIQUE."""
    for index in _inspector().get_indexes(TABLE_NAME):
        if index["name"] == index_name:
            return bool(index.get("unique"))
    return None


def _unique_constraints() -> Dict[str, List[str]]:
    """Named unique constraints on ``customers`` -> their column list."""
    return {
        constraint["name"]: list(constraint["column_names"])
        for constraint in _inspector().get_unique_constraints(TABLE_NAME)
        if constraint.get("name")
    }


def _legacy_global_constraints(constraints: Dict[str, List[str]]) -> List[str]:
    """Named single-column unique constraints on ``name`` / ``code`` (the global shapes)."""
    return sorted(name for name, columns in constraints.items() if len(columns) == 1 and columns[0] in UNIQUE_COLUMNS)


def _set_lock_timeout() -> None:
    if _is_postgres():
        op.execute("SET LOCAL lock_timeout = '5s'")


def _reset_lock_timeout() -> None:
    if _is_postgres():
        op.execute("SET LOCAL lock_timeout = DEFAULT")


def upgrade() -> None:
    offline = _offline()
    if offline:
        constraints: Dict[str, List[str]] = {}
        legacy: List[str] = []
        missing_constraints = [CONSTRAINT_NAMES[c] for c in UNIQUE_COLUMNS]
        index_states: Dict[str, Optional[bool]] = {c: True for c in UNIQUE_COLUMNS}
    else:
        constraints = _unique_constraints()
        legacy = _legacy_global_constraints(constraints)
        missing_constraints = [CONSTRAINT_NAMES[c] for c in UNIQUE_COLUMNS if CONSTRAINT_NAMES[c] not in constraints]
        # Decides only WHETHER there is index work; each index is re-probed below, after
        # the legacy constraints are gone -- on PostgreSQL a constraint sharing the
        # ``ix_`` name owns that index, and dropping it drops the index with it.
        index_states = {c: _index_is_unique(INDEX_NAMES[c]) for c in UNIQUE_COLUMNS}

    index_work = [c for c in UNIQUE_COLUMNS if index_states[c] is not False]
    if not (legacy or missing_constraints or index_work):
        return  # the create_all bootstrap path: every guard fired

    _set_lock_timeout()

    if legacy or missing_constraints:
        with op.batch_alter_table(TABLE_NAME) as batch:
            for name in legacy:
                batch.drop_constraint(name, type_="unique")
            for column in UNIQUE_COLUMNS:
                if CONSTRAINT_NAMES[column] in missing_constraints:
                    batch.create_unique_constraint(CONSTRAINT_NAMES[column], ["company_id", column])

    for column in UNIQUE_COLUMNS:
        index_name = INDEX_NAMES[column]
        state = True if offline else _index_is_unique(index_name)
        if state is True:
            op.drop_index(index_name, table_name=TABLE_NAME)
            op.create_index(index_name, TABLE_NAME, [column], unique=False)
        elif state is None:
            op.create_index(index_name, TABLE_NAME, [column], unique=False)

    _reset_lock_timeout()


def _refuse_cross_row_duplicates() -> None:
    """Raise before any DDL when the global uniques could not be rebuilt."""
    bind = op.get_bind()
    colliding = {}
    for column in UNIQUE_COLUMNS:
        value = sa.column(column)
        duplicated = (
            sa.select(value)
            .select_from(sa.table(TABLE_NAME, value))
            .where(value.isnot(None))
            .group_by(value)
            .having(sa.func.count() > 1)
            .subquery()
        )
        count = bind.execute(sa.select(sa.func.count()).select_from(duplicated)).scalar()
        if count:
            colliding[column] = int(count)
    if colliding:
        detail = ", ".join(f"{count} {column} value(s)" for column, count in colliding.items())
        raise RuntimeError(
            f"Cannot downgrade 115_customers_unique_per_company: {detail} are shared by more than "
            "one customer row (two companies may now hold the same name or code), so the global "
            "unique indexes cannot be rebuilt. Resolve the collisions first; this revision will "
            "not choose which company's customer to rename."
        )


def downgrade() -> None:
    offline = _offline()
    if offline:
        present_constraints = [CONSTRAINT_NAMES[c] for c in UNIQUE_COLUMNS]
        index_states: Dict[str, Optional[bool]] = {c: False for c in UNIQUE_COLUMNS}
    else:
        _refuse_cross_row_duplicates()
        constraints = _unique_constraints()
        present_constraints = [CONSTRAINT_NAMES[c] for c in UNIQUE_COLUMNS if CONSTRAINT_NAMES[c] in constraints]
        index_states = {c: _index_is_unique(INDEX_NAMES[c]) for c in UNIQUE_COLUMNS}

    index_work = [c for c in UNIQUE_COLUMNS if index_states[c] is not True]
    if not (present_constraints or index_work):
        return

    _set_lock_timeout()

    if present_constraints:
        with op.batch_alter_table(TABLE_NAME) as batch:
            for name in present_constraints:
                batch.drop_constraint(name, type_="unique")

    for column in UNIQUE_COLUMNS:
        index_name = INDEX_NAMES[column]
        state = False if offline else _index_is_unique(index_name)
        if state is False:
            op.drop_index(index_name, table_name=TABLE_NAME)
            op.create_index(index_name, TABLE_NAME, [column], unique=True)
        elif state is None:
            op.create_index(index_name, TABLE_NAME, [column], unique=True)

    _reset_lock_timeout()
