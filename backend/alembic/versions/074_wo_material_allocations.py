"""Create work_order_material_allocations (optional work-order <-> material tie)

Revision ID: 074_wo_material_allocations
Revises: 073_sms_provider_delivery
Create Date: 2026-07-25

Context
-------
PR 1 (schema only) of the material-consumption feature. It adds ONE new
tenant-scoped table, ``work_order_material_allocations``, lock-step with
``app/models/work_order_material.py::WorkOrderMaterialAllocation``. The row is the
OPTIONAL tie that says "this work order (or this operation of it) consumes this
material part" -- the headline case being a laser nest tied to a sheet part,
consuming ``qty_per_run`` sheets per completed run.

Nothing existing changes. No column is added to ``work_orders`` or
``work_order_operations``, no default allocation is created, and NO DATA IS
WRITTEN AT ALL. A work order with no allocation row behaves byte-identically to
today; "not tied" is simply the absence of a row, which is why there is nothing
to backfill and nothing to grandfather. Correct-forward only.

The ledger side of the feature (``inventory_transactions.allocation_id`` + the
reference composite index) is deliberately a SEPARATE revision,
``075_inventory_txn_allocation_ref``, so a change to the hottest table in the
system can be reviewed and rolled independently of this pure CREATE TABLE.

Head note (2026-07-25)
----------------------
``alembic heads`` reports exactly ONE head, ``073_sms_provider_delivery``
(verified before authoring), and ``alembic history`` shows the linear chain
``071_display_token_show_customer -> 071_soft_delete_purchasing_ncr ->
072_notifications_foundation -> 073_sms_provider_delivery``. Two files share the
``071_`` numeric prefix but the graph is single-headed -- the trap 072 and 073
both documented. ``down_revision`` is that true head revision id.

Native enum types (load-bearing)
--------------------------------
The table declares two native ``SQLEnum`` columns over ``str``-backed
``enum.Enum`` classes:

  * ``allocationsource`` -- column ``source``  = NEST, BOM, MANUAL
  * ``allocationstatus`` -- column ``status``  = OPEN, CLOSED, CANCELLED

**SQLAlchemy persists the UPPERCASE MEMBER NAMES for these.** The model uses a
plain ``SQLEnum(AllocationSource)`` / ``SQLEnum(AllocationStatus)`` with NO
``values_callable``, and that form binds ``enum.name`` -- so
``AllocationStatus.OPEN`` is stored and bound as ``'OPEN'``, never as the
lowercase ``'open'`` value. Verified in-env against a compiled Postgres
bind_processor and against a real ``create_all`` on SQLite (which renders the same
labels into a VARCHAR). Precedent: 056 (``visitorstatus``), 043
(``certificationtype``), and the ``inventory.py`` partial-index predicates
(``transaction_type = 'RECEIVE'``). The convention is NOT uniform in this
codebase -- ``parttype`` / ``unitofmeasure`` DO pass ``values_callable`` and store
lowercase values (018/019) -- so the label lists below are kept byte-for-byte in
lock-step with the model rather than derived by convention.

Autogenerate does not reliably emit ``CREATE TYPE`` for these, so both types are
created EXPLICITLY and idempotently (``checkfirst=True``) BEFORE the table, and
the columns reference them with ``create_type=False`` so ``create_table`` never
tries to re-create them. They are dropped in ``downgrade`` AFTER the table.
Neither column carries a ``server_default`` (Python-side default only, matching
the model and the 056/072 precedent).

Indexes -- and the partial-predicate literal
--------------------------------------------
Six plain indexes mirror the model's ``index=True`` declarations plus TenantMixin
(``id``, ``work_order_id``, ``work_order_operation_id``, ``part_id``, ``status``,
``company_id``), one plain composite backs the per-WO read
(``ix_wo_material_alloc_company_wo`` on ``company_id, work_order_id``), and TWO
PARTIAL UNIQUE indexes enforce "at most one OPEN allocation per material part per
scope, per company":

  * ``uq_wo_material_alloc_open_op`` -- ``(company_id, work_order_operation_id, part_id)``
    WHERE ``work_order_operation_id IS NOT NULL AND status = 'OPEN'``
  * ``uq_wo_material_alloc_open_wo`` -- ``(company_id, work_order_id, part_id)``
    WHERE ``work_order_operation_id IS NULL AND status = 'OPEN'``

``'OPEN'`` is UPPERCASE for the reason above: it is what the native PG enum binds.
Writing ``'open'`` would produce an index whose predicate matches NOTHING -- it
would create cleanly, psql would list it, and uniqueness would silently never be
enforced. That is the specific failure this note exists to prevent. (This docstring
deliberately contains no backslashes: the migration tests strip ``__doc__`` from the
raw source by substring, which an escape sequence would break.)

The predicates are partial on purpose: any number of CLOSED/CANCELLED rows may
accumulate for the same key, which is what lets untie-then-re-tie (and a nest
re-import) work without ever deleting a row. ``status`` is the tombstone; the
table deliberately does NOT use ``SoftDeleteMixin`` (see the model docstring).

Both partial indexes declare ``sqlite_where`` alongside ``postgresql_where`` so
dev/pytest gets the SAME semantics as production. SQLite has supported partial
indexes since 3.8.0 (verified in-env on 3.50.4 / SQLAlchemy 2.0). Without it
SQLite would build a FULL unique index and wrongly reject a re-tie after a
CANCELLED row -- a dev-only divergence. The service layer must still do an
app-level "already OPEN?" check so a duplicate tie surfaces as an intelligible
409 rather than an ``IntegrityError``.

Row Level Security
------------------
``ENABLE ROW LEVEL SECURITY`` on the new table, Postgres-only. Per the
deny-by-default new-table convention (docs/SUPABASE_SECURITY.md, gated in-repo by
``test_migration_059_060_supabase_hardening.py::
test_every_table_creating_migration_after_059_enables_rls``): this revision runs
AFTER 059's sweep, so a table it creates must enable RLS itself or the Supabase
Security Advisor re-flags the ``rls_disabled_in_public`` ERROR. Zero policies are
added -- the posture is deny-by-default with app-layer tenancy
(``tenant_query()`` / ``get_current_company_id``) as the enforcement; the app role
bypasses RLS. Precedent 061/072.

The tamper-evident ``audit_log`` table is NOT touched and NOT backfilled.

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), NOT a
bare ``upgrade head`` on an empty DB -- so a DB bootstrapped from the updated
models already has the table, both enum types, and every index when this
migration runs over the stamp. ``create_table`` is guarded by ``_has_table``,
every ``create_index`` by ``_has_index``, and the enum types by ``checkfirst=True``
(precedent 056), so the bootstrap path and any re-run are clean no-ops. The
``ENABLE ROW LEVEL SECURITY`` statement is itself idempotent in Postgres
(re-enabling an already-enabled table is a no-op).

``downgrade`` is real, not a stub: DISABLE RLS defensively (the table drop would
remove it anyway), drop all nine indexes in REVERSE creation order, drop the
table, then drop the two enum types (``checkfirst=True``) -- types last because
the table depends on them. Round-trips on Postgres and on the SQLite used for
local dev / pytest, where ``SQLEnum`` renders as VARCHAR and the native-type
create/drop is dialect-guarded to a no-op.

Ordering note for ``075``: once 075 is applied, ``inventory_transactions``
carries an FK to this table, so a downgrade must run 075 first (alembic's normal
reverse order does exactly that). Downgrading 074 alone while 075 is applied would
correctly fail on the dependent FK rather than silently orphan the ledger.

``company_id`` sits LAST in the ``create_table`` column list because TenantMixin's
columns are appended after the class's own (MRO order), which is what
``create_all`` emits -- same convention as 072:212-232 / 056:209-216.

Locking / operations note
-------------------------
A brand-new EMPTY table: ``CREATE TYPE`` + ``CREATE TABLE`` + nine index builds
are instantaneous and take NO lock on any existing table. Nothing is read or
written on ``work_orders``, ``work_order_operations``, ``parts``,
``inventory_items``, or ``inventory_transactions`` -- the FKs are outbound only,
and adding an inbound FK reference does not lock the referenced table's rows
(Postgres takes a brief ShareRowExclusive on the referenced tables to validate the
constraint definition; there is no scan because the new table is empty). No
backfill, no ``CONCURRENTLY`` consideration.

Deploy ordering is unconstrained in the safe direction: this revision may run
arbitrarily far ahead of the app deploy, because no shipped code path references
the table yet (PR 1 is schema + model only; the service and endpoints land in a
later PR). Old code neither reads nor writes it. Running the app deploy FIRST is
what would break, so run the migration first as usual.

Revision id ``074_wo_material_allocations`` is 27 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (``alembic_version.version_num``
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "074_wo_material_allocations"
down_revision = "073_sms_provider_delivery"
branch_labels = None
depends_on = None

ALLOCATIONS_TABLE = "work_order_material_allocations"

# Native enum types declared by app/models/work_order_material.py. SQLAlchemy
# stores the UPPERCASE member NAMES for a plain (no values_callable) str-backed
# Enum -- kept byte-for-byte in lock-step with the model so a defensively created
# type matches the one create_all builds.
SOURCE_ENUM_NAME = "allocationsource"
SOURCE_ENUM_LABELS = ["NEST", "BOM", "MANUAL"]

STATUS_ENUM_NAME = "allocationstatus"
STATUS_ENUM_LABELS = ["OPEN", "CLOSED", "CANCELLED"]

# The literal the native enum binds for AllocationStatus.OPEN. UPPERCASE member
# NAME, not the lowercase .value -- see the module docstring. Mirrors
# work_order_material.OPEN_STATUS_SQL_LITERAL.
OPEN_STATUS_SQL_LITERAL = "OPEN"

OPEN_OPERATION_PREDICATE = f"work_order_operation_id IS NOT NULL AND status = '{OPEN_STATUS_SQL_LITERAL}'"
OPEN_WORK_ORDER_PREDICATE = f"work_order_operation_id IS NULL AND status = '{OPEN_STATUS_SQL_LITERAL}'"

# (index_name, columns, unique, where_predicate_or_None). Mirrors the model's
# index=True declarations, TenantMixin, and __table_args__ so create_all and
# upgrade converge. Created in this order; dropped in reverse.
ALLOCATION_INDEXES = [
    ("ix_work_order_material_allocations_id", ["id"], False, None),
    ("ix_work_order_material_allocations_work_order_id", ["work_order_id"], False, None),
    (
        "ix_work_order_material_allocations_work_order_operation_id",
        ["work_order_operation_id"],
        False,
        None,
    ),
    ("ix_work_order_material_allocations_part_id", ["part_id"], False, None),
    ("ix_work_order_material_allocations_status", ["status"], False, None),
    ("ix_work_order_material_allocations_company_id", ["company_id"], False, None),
    ("ix_wo_material_alloc_company_wo", ["company_id", "work_order_id"], False, None),
    (
        "uq_wo_material_alloc_open_op",
        ["company_id", "work_order_operation_id", "part_id"],
        True,
        OPEN_OPERATION_PREDICATE,
    ),
    (
        "uq_wo_material_alloc_open_wo",
        ["company_id", "work_order_id", "part_id"],
        True,
        OPEN_WORK_ORDER_PREDICATE,
    ),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


# ``_has_column`` is deliberately NOT defined here: this revision only CREATEs a
# table (never ALTERs one), so a column guard would be dead code. 075 defines it.
# Same reasoning 073 used for omitting ``_has_index``.
def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _enum_type(name: str, labels):
    """Native enum reference that NEVER auto-creates/drops itself.

    On Postgres this binds the existing named type; on SQLite SQLEnum renders as a
    VARCHAR, matching what create_all emits from the model. ``create_type=False``
    keeps create_table/drop_table from touching the type -- the explicit
    ``_create_enums`` / ``_drop_enums`` passes own its lifecycle (precedent 056).
    """
    return postgresql.ENUM(*labels, name=name, create_type=False)


def _create_enums() -> None:
    """Idempotently create both native enum types (Postgres only).

    ``checkfirst=True`` makes this a no-op when create_all already built the type
    (bootstrap path) or on a re-run. SQLite has no native enum type, so the create
    is dialect-guarded away.
    """
    bind = op.get_bind()
    if not _is_postgres(bind):
        return
    postgresql.ENUM(*SOURCE_ENUM_LABELS, name=SOURCE_ENUM_NAME).create(bind, checkfirst=True)
    postgresql.ENUM(*STATUS_ENUM_LABELS, name=STATUS_ENUM_NAME).create(bind, checkfirst=True)


def _drop_enums() -> None:
    """Idempotently drop both native enum types (Postgres only), AFTER the table."""
    bind = op.get_bind()
    if not _is_postgres(bind):
        return
    postgresql.ENUM(*STATUS_ENUM_LABELS, name=STATUS_ENUM_NAME).drop(bind, checkfirst=True)
    postgresql.ENUM(*SOURCE_ENUM_LABELS, name=SOURCE_ENUM_NAME).drop(bind, checkfirst=True)


def _create_allocations_table() -> None:
    # Lock-step with app/models/work_order_material.py::WorkOrderMaterialAllocation.
    # TenantMixin's company_id is appended after the class's own columns (MRO
    # order), so it sits LAST -- matching create_all's emission order (072/056).
    op.create_table(
        ALLOCATIONS_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_order_id", sa.Integer(), nullable=False),
        # Set => operation-scoped tie (the nest case). NULL => work-order-scoped.
        sa.Column("work_order_operation_id", sa.Integer(), nullable=True),
        # The MATERIAL part consumed -- never the part produced.
        sa.Column("part_id", sa.Integer(), nullable=False),
        # Native enums created above with create_type=False; no server_default --
        # Python-side default only, matching the model.
        sa.Column("source", _enum_type(SOURCE_ENUM_NAME, SOURCE_ENUM_LABELS), nullable=False),
        sa.Column("status", _enum_type(STATUS_ENUM_NAME, STATUS_ENUM_LABELS), nullable=False),
        # Operation-scoped only: material per completed run. NULL on a WO-scoped
        # tie; readers treat NULL as 1.0. No default on either side.
        sa.Column("qty_per_run", sa.Float(), nullable=True),
        sa.Column("qty_planned", sa.Float(), nullable=False),
        # Snapshot of Part.unit_of_measure at tie time (lowercase enum VALUE).
        sa.Column("unit_of_measure", sa.String(length=20), nullable=False),
        # Cache; the inventory_transactions ledger is authoritative.
        sa.Column("qty_consumed", sa.Float(), nullable=False, server_default="0"),
        # Lot-directed tie; NULL => FIFO at consume time.
        sa.Column("pinned_inventory_item_id", sa.Integer(), nullable=True),
        sa.Column("pinned_lot_number", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # TenantMixin -- non-null company scope, FK to companies.id, LAST.
        sa.Column("company_id", sa.Integer(), nullable=False),
        # FK clause order mirrors create_all's emission order (model column
        # discovery order); clause order is semantically irrelevant to the schema.
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"]),
        sa.ForeignKeyConstraint(["work_order_operation_id"], ["work_order_operations.id"]),
        sa.ForeignKeyConstraint(["part_id"], ["parts.id"]),
        sa.ForeignKeyConstraint(["pinned_inventory_item_id"], ["inventory_items.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_indexes() -> None:
    """Create every index, guarded. Partial predicates go to BOTH dialects.

    ``postgresql_where`` AND ``sqlite_where`` are passed for the two partial unique
    indexes so pytest/SQLite enforces the same rule as production Postgres rather
    than a FULL unique index (which would reject a legitimate re-tie after a
    CANCELLED row). Dialect kwargs for the non-active dialect are simply ignored.
    """
    for index_name, columns, unique, predicate in ALLOCATION_INDEXES:
        if _has_index(ALLOCATIONS_TABLE, index_name):
            continue
        kwargs = {}
        if predicate is not None:
            kwargs["postgresql_where"] = sa.text(predicate)
            kwargs["sqlite_where"] = sa.text(predicate)
        op.create_index(index_name, ALLOCATIONS_TABLE, columns, unique=unique, **kwargs)


def upgrade() -> None:
    # Enum types BEFORE the table; every step guarded so a create_all-bootstrapped
    # DB and re-runs are clean no-ops. NO data statement of any kind -- "not tied"
    # is the absence of a row, so there is nothing to backfill.
    _create_enums()
    if not _has_table(ALLOCATIONS_TABLE):
        _create_allocations_table()
    _create_indexes()

    # Deny-by-default RLS on the new table (Postgres only, zero policies). Runs
    # after 059/060, so a table created here must enable RLS itself. Idempotent:
    # re-enabling an already-enabled table is a no-op in Postgres.
    if _is_postgres(op.get_bind()):
        op.execute(f'ALTER TABLE public."{ALLOCATIONS_TABLE}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    # Reverse dependency order: disable RLS (defensive -- the drop removes it
    # anyway), drop indexes in reverse creation order, drop the table, then the
    # enum types it owns. All guarded.
    if _has_table(ALLOCATIONS_TABLE):
        if _is_postgres(op.get_bind()):
            op.execute(f'ALTER TABLE public."{ALLOCATIONS_TABLE}" DISABLE ROW LEVEL SECURITY')
        for index_name, _columns, _unique, _predicate in reversed(ALLOCATION_INDEXES):
            if _has_index(ALLOCATIONS_TABLE, index_name):
                op.drop_index(index_name, table_name=ALLOCATIONS_TABLE)
        op.drop_table(ALLOCATIONS_TABLE)

    _drop_enums()
