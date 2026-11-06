"""Make eight business-number columns unique PER COMPANY instead of globally

Revision ID: 116_business_keys_per_company
Revises: 115_customers_unique_per_company
Create Date: 2026-11-05

Context
-------
Eight ``TenantMixin`` tables carry a business key declared ``unique=True, index=True``,
which ``create_all`` builds as a GLOBAL unique index ``ix_<table>_<column>``:

==================================  ====================  =================================
table                               column                who chooses the value
==================================  ====================  =================================
``ncrs``                            ``ncr_number``        ``services/quality_numbering.py``
``cars``                            ``car_number``        ``services/quality_numbering.py``
``fais``                            ``fai_number``        ``services/fai_service.py``
``customer_complaints``             ``complaint_number``  the complaint service's generator
``return_material_authorizations``  ``rma_number``        the complaint service's generator
``equipment``                       ``equipment_id``      the user (gauge / asset tag)
``tools``                           ``tool_id``           the user (tool / fixture tag)
``downtime_reason_codes``           ``code``              the user (reason-code catalog)
==================================  ====================  =================================

``equipment.equipment_id`` and ``tools.tool_id`` are the String business keys, not the
integer foreign keys of the same name on ``calibration_records`` / ``tool_checkouts`` /
``tool_usage_logs`` (those reference the primary key and are untouched). ``024`` created
four of these tables with the same ``Column(..., unique=True, index=True)`` shape, which
alembic's ``create_table`` emits as the same ``ix_`` unique index; the other four have
only ever come from ``create_all``, and prod was bootstrapped ``create_all`` -> stamp
``058`` either way. ``026``/``027`` widened the tenant uniques of other tables but never
these eight.

Why: a global key on a per-company value is a multi-tenant defect in both directions.

- ``quality_numbering`` numbers NCRs and CARs PER COMPANY (``NCR-YYYYMMDD-001`` under a
  per-(namespace, company) advisory lock), so a second tenant's first NCR or CAR of the
  day mints the number the first tenant already holds and the global index rejects the
  insert as a 500 (``tests/api/test_soft_delete_read_sweep.py`` had to skip a
  cross-tenant NCR test because of it).
- ``fai_service._number`` allocates ``FAI-YYYYMMDD-NNN`` PER COMPANY the same way (a
  per-company generator lock and a ``tenant_query`` read of the day's last number), so a
  second tenant's first FAI of the day collides on the global index and 500s -- the same
  defect as NCR / CAR.
- Complaint and RMA numbering is moving to per-company for the same reason; under the
  global key that move would reproduce the same 500.
- Equipment, tool and downtime reason-code identifiers are user-entered. A global unique
  refuses a tenant's identifier because ANOTHER tenant uses it, and the refusal itself
  tells the first tenant which identifiers the second one holds.

This revision replaces each global unique with a tenant-scoped one and keeps a plain
single-column index on the column for lookups (the ``027`` / ``115`` shape), per table:

- ``ix_<table>_<column>``             UNIQUE (<column>)              -> plain index on (<column>)
- ``uq_<table>_company_<column>``     UNIQUE (company_id, <column>)  new

Defensively, any NAMED single-column unique CONSTRAINT on the column (the Postgres
``<table>_<column>_key`` shape a ``unique=True`` column without ``index=True`` would have
produced, or a constraint sharing the ``ix_`` name -- the three storage shapes ``027``
drops) is dropped too. None is known to exist anywhere; the guard costs one reflection
per table.

No pre-flight dedup (verified)
------------------------------
Every new key is strictly LOOSER than the one it replaces: adding ``company_id`` as a
leading column can only relax a unique key (precedent ``045``). Every row that satisfied a
global index satisfies the new constraint, so the upgrade cannot fail on data. All eight
columns are ``NOT NULL``. No constraint has a partial predicate: a soft-deleted NCR keeps
its number (a quality record's identifier is never reused), which is also what keeps a
later restore from colliding.

The DOWNGRADE is the tightening direction. Once two companies hold the same number or
code, the global unique cannot be rebuilt, so ``downgrade()`` counts cross-row duplicates
for ALL EIGHT columns FIRST and raises before any DDL runs, naming each ``table.column``
and how many values collide (never the values -- they are tenant data and this lands in
deploy logs). Resolving them is a data decision for an operator, not something a
migration may guess at: renumbering an NCR, CAR or FAI rewrites the identifier a quality
record was issued under.

Lock-step with the models (the stamped-over hazard)
---------------------------------------------------
Each owning model declares its ``uq_<table>_company_<column>`` ``UniqueConstraint`` in
``__table_args__`` and keeps ``index=True`` on the column (now non-unique), so
``create_all`` builds exactly the post-116 shape and the bootstrap (``create_all`` -> stamp
``058`` -> upgrade) runs this revision as a clean no-op:
``app/models/quality.py`` (NonConformanceReport, CorrectiveActionRequest,
FirstArticleInspection),
``app/models/customer_complaint.py`` (CustomerComplaint, ReturnMaterialAuthorization),
``app/models/calibration.py`` (Equipment), ``app/models/tool_management.py`` (Tool),
``app/models/downtime.py`` (DowntimeReasonCode).
``tests/test_migration_116_business_keys_per_company.py`` pins the names and columns
equal, and ``tests/test_migration_fresh_bootstrap.py``'s round-trip diffs the re-upgraded
schema against ``create_all``.

Idempotent and reversible (088 style -- every object guarded independently)
---------------------------------------------------------------------------
- Each legacy single-column unique constraint is dropped only if reflection finds it.
- Each ``ix_<table>_<column>`` is probed for EXISTENCE and UNIQUENESS separately: a unique
  one is dropped and rebuilt plain, a missing one is built plain, a plain one is left
  alone. The probe is repeated after the legacy constraints are gone -- on PostgreSQL a
  constraint sharing the ``ix_`` name owns that index and dropping it drops the index.
- Each new ``uq_<table>_company_<column>`` is created only if absent.
- Every guard is per TABLE as well as per object: a database where some tables were
  already re-keyed and others were not converges table by table.
- Downgrade mirrors it: each ``uq_`` constraint dropped only if present, each ``ix_``
  index rebuilt UNIQUE only if it is not already.
- Constraint DDL runs inside ``batch_alter_table``: a plain ``ALTER`` on PostgreSQL, a
  table rebuild on SQLite (which cannot ``ALTER TABLE ... ADD/DROP CONSTRAINT``;
  precedent ``089``). Index DDL is dialect-neutral and runs outside the batch, after it.
- ``--sql`` (offline) has no database to inspect, so it emits the canonical path: every
  index rebuild and every constraint (precedent ``089``).

Shape / compliance
------------------
Pure DDL over eight EXISTING tables: no row is read except by the downgrade's duplicate
count, none is written, and the tamper-evident ``audit_log`` chain is untouched. No table
is created, so the ENABLE-RLS new-table convention does not apply and no RLS / REVOKE /
role-naming statement is emitted (all eight have had RLS since ``059``, which enabled it
on every table in ``public``).

Locking / operations note
-------------------------
On PostgreSQL ``ADD CONSTRAINT ... UNIQUE`` and ``DROP INDEX`` take an ACCESS EXCLUSIVE
lock on each table in turn, and ``alembic/env.py`` runs every pending revision in ONE
transaction, so all eight locks are held until the deploy commits. The tables are small
(the NCR / CAR / FAI and complaint / RMA registers, the gauge and tool masters, a
reason-code catalog: tens to hundreds of rows each), so each index build is short.
``SET LOCAL lock_timeout = '5s'`` is taken once, only when there is DDL to run, so a long
transaction holding any of the eight fails the deploy fast instead of queueing every
NCR / FAI / gauge / tool read behind it (and turns a lock-order wait against a live
multi-table transaction into a fast failure rather than a stall). It is handed back
(``SET LOCAL lock_timeout = DEFAULT``) before the revision returns so it never leaks into a
later revision in the same run (precedent ``114``).

Order: apply this revision BEFORE (or with) the release that ships it, never after. Old
code is safe on the new schema -- it never relied on cross-tenant uniqueness, and every
looser key accepts every row the old one did. The reverse is not safe: the same release
moves complaint / RMA numbering onto per-company sequences, so new code on the pre-116
schema can mint a ``CC-`` / ``RMA-`` number another company already holds and 500 on the
global index. The normal boot path migrates before the new API serves, which satisfies
this; it matters only for a hand-run migration.

Revision id ``116_business_keys_per_company`` is 29 chars -- inside the
``alembic_version.version_num`` varchar(32) ceiling on a freshly bootstrapped DB
(docs/DEVELOPMENT.md). Do not lengthen it past 32.
"""

from typing import Dict, List, Optional

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "116_business_keys_per_company"
down_revision = "115_customers_unique_per_company"
branch_labels = None
depends_on = None

# table -> the business-key column that becomes unique per company (processing order)
UNIQUE_KEYS: Dict[str, str] = {
    "ncrs": "ncr_number",
    "cars": "car_number",
    "fais": "fai_number",
    "customer_complaints": "complaint_number",
    "return_material_authorizations": "rma_number",
    "equipment": "equipment_id",
    "tools": "tool_id",
    "downtime_reason_codes": "code",
}

# table -> the single-column index create_all names ``ix_<table>_<column>``
INDEX_NAMES: Dict[str, str] = {table: f"ix_{table}_{column}" for table, column in UNIQUE_KEYS.items()}
# table -> the tenant-scoped unique constraint that replaces the global one
CONSTRAINT_NAMES: Dict[str, str] = {table: f"uq_{table}_company_{column}" for table, column in UNIQUE_KEYS.items()}


def _offline() -> bool:
    # ``--sql`` has no database to inspect: emit the canonical statements.
    return bool(op.get_context().as_sql)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _inspector():
    return sa.inspect(op.get_bind())


def _index_is_unique(table: str, index_name: str) -> Optional[bool]:
    """``None`` when the index is absent, otherwise whether it is UNIQUE."""
    for index in _inspector().get_indexes(table):
        if index["name"] == index_name:
            return bool(index.get("unique"))
    return None


def _unique_constraints(table: str) -> Dict[str, List[str]]:
    """Named unique constraints on ``table`` -> their column list."""
    return {
        constraint["name"]: list(constraint["column_names"])
        for constraint in _inspector().get_unique_constraints(table)
        if constraint.get("name")
    }


def _legacy_global_constraints(table: str, constraints: Dict[str, List[str]]) -> List[str]:
    """Named single-column unique constraints on the table's business key (the global shapes)."""
    column = UNIQUE_KEYS[table]
    return sorted(name for name, columns in constraints.items() if columns == [column])


def _set_lock_timeout() -> None:
    if _is_postgres():
        op.execute("SET LOCAL lock_timeout = '5s'")


def _reset_lock_timeout() -> None:
    if _is_postgres():
        op.execute("SET LOCAL lock_timeout = DEFAULT")


def upgrade() -> None:
    offline = _offline()
    legacy: Dict[str, List[str]] = {}
    add_constraint: Dict[str, bool] = {}
    index_states: Dict[str, Optional[bool]] = {}
    for table in UNIQUE_KEYS:
        if offline:
            legacy[table] = []
            add_constraint[table] = True
            index_states[table] = True
        else:
            constraints = _unique_constraints(table)
            legacy[table] = _legacy_global_constraints(table, constraints)
            add_constraint[table] = CONSTRAINT_NAMES[table] not in constraints
            # Decides only WHETHER there is index work; each index is re-probed below, after
            # the legacy constraints are gone -- on PostgreSQL a constraint sharing the
            # ``ix_`` name owns that index, and dropping it drops the index with it.
            index_states[table] = _index_is_unique(table, INDEX_NAMES[table])

    constraint_work = [t for t in UNIQUE_KEYS if legacy[t] or add_constraint[t]]
    index_work = [t for t in UNIQUE_KEYS if index_states[t] is not False]
    if not (constraint_work or index_work):
        return  # the create_all bootstrap path: every guard fired

    _set_lock_timeout()

    for table, column in UNIQUE_KEYS.items():
        if table in constraint_work:
            with op.batch_alter_table(table) as batch:
                for name in legacy[table]:
                    batch.drop_constraint(name, type_="unique")
                if add_constraint[table]:
                    batch.create_unique_constraint(CONSTRAINT_NAMES[table], ["company_id", column])

        index_name = INDEX_NAMES[table]
        state = True if offline else _index_is_unique(table, index_name)
        if state is True:
            op.drop_index(index_name, table_name=table)
            op.create_index(index_name, table, [column], unique=False)
        elif state is None:
            op.create_index(index_name, table, [column], unique=False)

    _reset_lock_timeout()


def _refuse_cross_row_duplicates() -> None:
    """Raise before any DDL when any of the global uniques could not be rebuilt."""
    bind = op.get_bind()
    colliding: Dict[str, int] = {}
    for table, column in UNIQUE_KEYS.items():
        value = sa.column(column)
        duplicated = (
            sa.select(value)
            .select_from(sa.table(table, value))
            .where(value.isnot(None))
            .group_by(value)
            .having(sa.func.count() > 1)
            .subquery()
        )
        count = bind.execute(sa.select(sa.func.count()).select_from(duplicated)).scalar()
        if count:
            colliding[f"{table}.{column}"] = int(count)
    if colliding:
        detail = ", ".join(f"{count} {key} value(s)" for key, count in colliding.items())
        raise RuntimeError(
            f"Cannot downgrade 116_business_keys_per_company: {detail} are shared by more than "
            "one row (two companies may now hold the same number or code), so the global "
            "unique indexes cannot be rebuilt. Resolve the collisions first; this revision will "
            "not choose which company's record to renumber."
        )


def downgrade() -> None:
    offline = _offline()
    drop_constraint: Dict[str, bool] = {}
    index_states: Dict[str, Optional[bool]] = {}
    if not offline:
        _refuse_cross_row_duplicates()
    for table in UNIQUE_KEYS:
        if offline:
            drop_constraint[table] = True
            index_states[table] = False
        else:
            drop_constraint[table] = CONSTRAINT_NAMES[table] in _unique_constraints(table)
            index_states[table] = _index_is_unique(table, INDEX_NAMES[table])

    constraint_work = [t for t in UNIQUE_KEYS if drop_constraint[t]]
    index_work = [t for t in UNIQUE_KEYS if index_states[t] is not True]
    if not (constraint_work or index_work):
        return

    _set_lock_timeout()

    for table, column in UNIQUE_KEYS.items():
        if table in constraint_work:
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(CONSTRAINT_NAMES[table], type_="unique")

        index_name = INDEX_NAMES[table]
        state = False if offline else _index_is_unique(table, index_name)
        if state is False:
            op.drop_index(index_name, table_name=table)
            op.create_index(index_name, table, [column], unique=True)
        elif state is None:
            op.create_index(index_name, table, [column], unique=True)

    _reset_lock_timeout()
