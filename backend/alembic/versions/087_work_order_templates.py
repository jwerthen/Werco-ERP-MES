"""087 - work_order_templates: a named catalog of jobs the shop re-runs

Adds ONE new table, ``work_order_templates``, backing the
``/api/v1/work-order-templates`` router. One row per named plan: what the planner
calls it, an optional note, and a POINTER at the work order whose plan is the
exemplar.

Why a pointer table and not a frozen plan
-----------------------------------------
"Use template" calls ``services/work_order_duplicate_service.duplicate_work_order``
against the referenced work order — the same copy engine ``POST
/work-orders/{id}/duplicate`` uses, landing the result in ``DRAFT`` with ``PENDING``
operations. So this migration adds NO operations table, NO nests table and NO
allocations table: freezing the plan here would mean a second copy service
reimplementing every decision the existing one makes (what scales with quantity,
how a nest tie's ``qty_planned`` is derived, which omissions skip and which refuse
the whole call), and every future fix would have to be made twice.

The cost, stated so nobody files it as a bug: editing the source work order changes
what the template produces. See ``app/models/work_order_template.py``.

**AMENDED 2026-08-27 — deleting the source no longer breaks the template.** As
shipped, a template whose source work order was soft-deleted was reported UNAVAILABLE
and refused 409. The owner overrode that: the template now reads its plan straight
THROUGH the tombstone and still produces a DRAFT. No schema change was needed, and
that is not a coincidence — it is a property of the FK declared below.

``source_work_order_id`` is ``nullable=False`` with a PLAIN
``ForeignKeyConstraint(["source_work_order_id"], ["work_orders.id"])`` and **no
``ondelete``**. Postgres therefore refuses to remove a ``work_orders`` row any
template still points at, so *"the source work order is gone"* can only ever mean
**soft-deleted** — and a soft-deleted work order keeps every operation, nest, tie and
process-sheet step it had. The plan is always still there to be read, which is exactly
why read-through is a complete answer and a frozen-plan table would buy nothing.

**Do not add an ``ondelete`` to that constraint in a later migration, and do not
"clean up" the NOT NULL.** What used to be an incidental integrity error (a hard
delete raised ``ForeignKeyViolation`` and surfaced as a 500) is now depended upon:
``DELETE /work-orders/{id}?hard_delete=true`` refuses **409** naming the templates
saved from the job, before its first mutation. A ``CASCADE`` here would silently
destroy catalog entries as a side effect of deleting a draft work order.

Lock-step with ``app/models/work_order_template.py``
----------------------------------------------------
Every column and all EIGHT indexes are declared on the model too, and this file
mirrors the model rather than the other way round. Bootstrap here is ``create_all``
-> ``alembic stamp <baseline>`` -> incremental ``upgrade``, so a freshly-provisioned
database builds this table from ``create_all`` and then STAMPS PAST this revision.
Anything declared only here would be silently missing on every fresh environment
while ``alembic_version`` claimed it existed — the stamped-over hazard that cost
prod the ``008`` audit triggers (2026-07-07), ~42 read-path indexes (restored by
``079``) and ``003``'s named FKs and CHECKs (restored by ``080``). Hence the
``042``/``078``/``079``/``080``/``085`` convention: model and migration change in the
same PR.

Two consequences of "mirror the model, don't improve on it":

* Column ORDER matches ``create_all``'s emission exactly — the class's own columns
  first, then ``SoftDeleteMixin``'s three, then ``TenantMixin``'s ``company_id``
  appended last via the MRO. Cosmetic to Postgres; it is what lets a schema diff
  between a bootstrapped and a migrated database come back empty.
* ``created_at`` / ``updated_at`` are naive ``DateTime`` NOT NULL with **Python-side**
  ``datetime.utcnow`` defaults and no ``server_default``, matching the model and
  ``WorkOrder`` itself. Adding ``server_default=now()`` HERE only would give the two
  bootstrap paths two different schemas, which is the exact drift this convention
  exists to prevent. A raw-SQL INSERT that omits either column therefore fails; every
  writer goes through the ORM.

The unique name index is PARTIAL, on BOTH dialects
---------------------------------------------------
``uq_work_order_templates_company_name_live`` is UNIQUE over ``(company_id, name)``
``WHERE NOT is_deleted`` — one LIVE template per name per company, and deleting one
frees its name immediately rather than burning it forever.

It is created as an INDEX, not a ``UniqueConstraint``, because a constraint cannot
carry a predicate; and the predicate is passed as **both** ``postgresql_where`` and
``sqlite_where`` (the ``074``/``076`` convention). A ``postgresql_where`` alone is
silently ignored by SQLite — the dialect the entire pytest suite runs on — degrading
into a FULL unique index, so the tests would enforce a rule production does not:
refusing to reuse a deleted template's name. That divergence is exactly what ``076``
was written to fix elsewhere in this schema; it is not being re-introduced here.

The predicate text is ``NOT is_deleted`` rather than ``is_deleted = false`` because
one string has to compile on both dialects — Postgres rejects ``is_deleted = 0`` as a
type error, and SQLite has no ``false`` literal before 3.23. It is total, not
three-valued: ``SoftDeleteMixin.is_deleted`` is ``nullable=False`` with
``server_default='false'``, so no row can fall out of the index through a NULL.

Shape / compliance
------------------
* Tenant-scoped (invariant 1): ``company_id`` INTEGER NOT NULL, FK ``companies.id``,
  indexed — the ``TenantMixin`` shape. App-layer tenancy via ``tenant_query`` is the
  enforcement.
* Soft-deletable (invariant 3): ``is_deleted`` / ``deleted_at`` / ``deleted_by`` from
  ``SoftDeleteMixin``. ``deleted_by`` is a bare ``Integer`` with NO foreign key —
  that is the mixin's own shape (``app/db/mixins.py``), deliberately not made
  symmetric with the hand-written ``created_by`` FK.
* ``ENABLE ROW LEVEL SECURITY`` on the new table, Postgres only. This revision runs
  after ``059``/``060``, which enabled RLS across ``public`` and left it
  deny-by-default with zero policies, so a table created afterwards must enable it
  itself or it re-flags the Supabase Security Advisor's ``rls_disabled_in_public``
  ERROR (precedent ``061``/``084``/``085``; see ``docs/SUPABASE_SECURITY.md``). The
  app role bypasses RLS.
* The tamper-evident ``audit_log`` table is untouched, and so is every other existing
  table. This revision is purely additive DDL: it creates no enum, alters no enum,
  adds no column anywhere else, and — in particular — does NOT touch ``work_orders``.
  A template names a work order; the work order knows nothing about it.

No backfill
-----------
There is nothing to backfill. No template has ever existed, and inventing rows over
historical work orders would be fabricating a catalog nobody curated — the same
reasoning as ``085``'s empty combine table, ``084``'s empty alias table and ``083``'s
forward-only unit numbers.

Idempotent and reversible
-------------------------
Upgrade guards the table and each index independently (``_has_table`` / ``_has_index``),
so the create_all-bootstrapped path and any re-run are clean no-ops. Downgrade drops
the indexes then the table, each guarded, so a partially applied upgrade still
downgrades cleanly. Round-tripped locally: ``upgrade head`` -> ``downgrade -1`` ->
``upgrade head``.

Operational note: ``CREATE TABLE`` on a table that does not yet exist takes no lock
anything else can be waiting on, and there is no backfill pass, so this migration is
safe to run against live Postgres during traffic and carries no deploy-ordering
constraint of its own — beyond the obvious one that it must land before the code that
reads the table.

Revision ID: 087_work_order_templates
Revises: 086_part_active_before_delete
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "087_work_order_templates"
down_revision = "086_part_active_before_delete"
branch_labels = None
depends_on = None


TABLE = "work_order_templates"

# The partial predicate, one string for both dialects. Mirrored byte-for-byte on the
# model as ``work_order_template.LIVE_NAME_PREDICATE`` -- a test pins the two equal.
LIVE_NAME_PREDICATE = "NOT is_deleted"

# (index_name, columns, unique, predicate) -- every one of these is mirrored on the
# model. The first five come from ``index=True`` on the columns themselves
# (``is_deleted`` via SoftDeleteMixin, ``company_id`` via TenantMixin); the last three
# are the model's explicit ``__table_args__``.
INDEXES = [
    ("ix_work_order_templates_id", ["id"], False, None),
    ("ix_work_order_templates_name", ["name"], False, None),
    ("ix_work_order_templates_source_work_order_id", ["source_work_order_id"], False, None),
    ("ix_work_order_templates_is_deleted", ["is_deleted"], False, None),
    ("ix_work_order_templates_company_id", ["company_id"], False, None),
    # The catalog read: this company's live templates, in name order.
    ("ix_work_order_templates_company_live", ["company_id", "is_deleted", "name"], False, None),
    # "Which templates point at this work order?"
    ("ix_work_order_templates_company_source", ["company_id", "source_work_order_id"], False, None),
    # One LIVE template per name per company -- see the docstring on why this is a
    # partial INDEX rather than a UniqueConstraint, and why the predicate goes to both
    # dialects.
    ("uq_work_order_templates_company_name_live", ["company_id", "name"], True, LIVE_NAME_PREDICATE),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _create_table() -> None:
    # Column order matches create_all's emission order exactly: the class's own
    # columns first, then SoftDeleteMixin's three, then TenantMixin's ``company_id``
    # (appended via the MRO). Order is cosmetic to Postgres but keeping it identical
    # is what lets a schema diff between a bootstrapped and a migrated database come
    # back empty.
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        # What the planner calls this job. Stored verbatim -- there is no ingest-time
        # sanitization in this system and nothing renders it as raw HTML.
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        # THE POINTER, and the reason this table is thin. NOT NULL: a template with no
        # exemplar has no plan. No ``ondelete`` -- ``work_orders`` is soft-deleted, so
        # a physical delete is not the path, and a cascade here would silently destroy
        # a named catalog entry as a side effect of somebody removing a work order.
        sa.Column("source_work_order_id", sa.Integer(), nullable=False),
        # A prefill for the use endpoint, not a stored plan number. Ignored for a
        # nest-bearing template, whose quantity the duplicate service derives from the
        # copied nests' planned runs.
        sa.Column("default_quantity", sa.Float(), nullable=True),
        # NOT NULL with Python-side defaults only -- mirrors the model; see the
        # docstring on why no server defaults are added here.
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        # SoftDeleteMixin. ``is_deleted`` carries the server_default the mixin
        # declares; ``deleted_by`` is a bare Integer with no FK, which is the mixin's
        # own shape and is deliberately not "corrected" here.
        # ``server_default="false"`` is the plain string the mixin declares, NOT
        # ``sa.text("false")``. They diverge: SQLAlchemy quotes the string, so the two
        # bootstrap paths would emit ``DEFAULT 'false'`` (create_all) against
        # ``DEFAULT false`` (this migration) -- a schema diff that never comes back
        # empty, which is exactly what the lock-step convention exists to prevent.
        # Verified by diffing a create_all database against a migrated one. Same
        # spelling as 006/037/046/053.
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Integer(), nullable=True),
        # TenantMixin, last.
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["source_work_order_id"], ["work_orders.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1) The table. Guarded so the create_all bootstrap path (where the model already
    #    built it) no-ops rather than erroring on a duplicate table.
    if not _has_table(TABLE):
        _create_table()

    # 2) Indexes, each guarded independently -- create_all emits all eight too, and a
    #    re-run or a partially applied upgrade must be a no-op rather than a failure.
    #    The partial predicate goes to BOTH dialects; kwargs for the non-active dialect
    #    are simply ignored.
    for index_name, columns, unique, predicate in INDEXES:
        if _has_index(TABLE, index_name):
            continue
        kwargs = {}
        if predicate is not None:
            kwargs["postgresql_where"] = sa.text(predicate)
            kwargs["sqlite_where"] = sa.text(predicate)
        op.create_index(index_name, TABLE, columns, unique=unique, **kwargs)

    # 3) RLS on the new table (Postgres only; this revision runs after 059/060).
    #    Deny-by-default with zero policies; the app role bypasses it and app-layer
    #    tenancy via tenant_query stays the enforcement. Without this the Supabase
    #    Security Advisor re-flags rls_disabled_in_public as an ERROR.
    if is_postgres:
        op.execute(f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    # Indexes first, then the table (FK-safe order). Each guarded so a partial upgrade
    # downgrades cleanly. Dropping the table takes the FKs and the RLS setting with it
    # -- there is nothing left behind to un-enable.
    #
    # This drops the catalog if any template has been saved. That is correct for a
    # downgrade (the operator asked to unmake the schema), and it costs nothing that
    # cannot be rebuilt: a template holds only a name, a note and a pointer, so
    # re-saving from the same work order reproduces it exactly. Nothing a template ever
    # PRODUCED is touched -- the work orders it created are ordinary work orders, and
    # the tamper-evident account of every save/use/delete is the ``audit_log`` row,
    # which both directions of this revision leave alone.
    for index_name, _columns, _unique, _predicate in INDEXES:
        if _has_index(TABLE, index_name):
            op.drop_index(index_name, table_name=TABLE)

    if _has_table(TABLE):
        op.drop_table(TABLE)
