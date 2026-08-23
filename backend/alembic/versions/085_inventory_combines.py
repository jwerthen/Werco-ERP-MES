"""085 - inventory_combines: one folding of two SKUs into one, as a record

Adds ONE new table, ``inventory_combines``, backing ``POST /inventory/combine``.
One immutable row per combine: the SKU the stock left, the SKU it landed on, how
much moved, and the written reason it was allowed to.

Why a header table at all
-------------------------
A combine writes **2N ledger rows** — per moved lot line an ``ADJUST`` out of the
source part and an ``ADJUST`` into the target part, summing to exactly zero. It
deliberately does NOT add a ``COMBINE`` member to the ``transactiontype`` enum:
that would be an ``ALTER TYPE`` on live Postgres plus a value every consumer
(analytics, exports, traceability, job costing, the frontend label maps) currently
cannot see — a broad blast radius on live data for a labelling gain. ``ADJUST`` is
already this repo's compensating/reconciliation shape (invariant 3; receiving
void/correct), and it is the one type whose SIGN carries direction.

The price of reusing ``ADJUST`` is that the 2N rows are otherwise related only by
having happened at the same instant, which is not a relation anything can query.
This table is the group id they all carry (``reference_type='inventory_combine'``,
``reference_id`` = this row's id), the same way ``po_receipts`` groups a receipt's
movements. That reference type sits OUTSIDE both partial unique predicates
``uq_wo_inventory_receipt`` / ``uq_wo_inventory_issue`` (``reference_type =
'work_order' AND transaction_type = 'RECEIVE'/'ISSUE'``), so combine rows can never
collide with the work-order backflush idempotency guards, and outside
``work_order_ledger_filter``'s three reference types, so a combine never surfaces as
work-order material in job costing. Neither of those is touched by this migration —
they are stated here because the whole shape depends on them staying true.

Lock-step with ``app/models/inventory_combine.py``
--------------------------------------------------
Every column, the unique constraint and all SEVEN indexes are declared on the model
too, and this file mirrors the model rather than the other way round. That
convergence is not cosmetic: bootstrap here is ``create_all`` -> ``alembic stamp
<baseline>`` -> incremental ``upgrade``, so a freshly-provisioned database builds
this table from ``create_all`` and then STAMPS PAST this revision. Anything declared
only here would be silently missing on every fresh environment while
``alembic_version`` claimed it existed — the stamped-over hazard that cost prod the
``008`` audit triggers (2026-07-07), ~42 read-path indexes (restored by ``079``) and
``003``'s named FKs and CHECKs (restored by ``080``). Hence the ``042``/``078``/
``079``/``080`` convention: model and migration change in the same PR.

Two consequences of "mirror the model, don't improve on it", both deliberate:

* ``created_at`` is a naive ``DateTime`` NOT NULL with a **Python-side**
  ``datetime.utcnow`` default and no ``server_default``, matching the model and the
  house norm (``company``, ``user``, ``maintenance``, ...). Adding
  ``server_default=now()`` HERE only would give the two bootstrap paths two
  different schemas, which is the exact drift this convention exists to prevent.
  A raw-SQL INSERT that omits the column therefore fails; every writer goes through
  the ORM.
* ``source_deactivated`` is likewise NOT NULL with a Python-side ``default=False``
  and no server default. Same rule, same reason.

The unique constraint rides INSIDE ``create_table``
---------------------------------------------------
``uq_inventory_combines_company_number`` is declared as a real UNIQUE CONSTRAINT
(not a unique index) so it matches the model's ``__table_args__`` exactly, and it is
created as part of the table rather than as a separate guarded step because SQLite —
the dialect the test suite runs on — cannot ``ADD CONSTRAINT`` outside batch mode.
Same shape as ``084``.

It is NOT DEFERRABLE, and ``inventory_combine_service`` depends on that: it mints
``COMB-{id:06d}`` from the flushed primary key, so the INSERT carries a transient
``PENDING-<uuid4hex>`` value for one statement before being stamped. Per-statement
checking is why that placeholder has to be a uuid rather than a fixed literal — two
concurrent combines in the same company would collide on a fixed one. Do not make
this constraint deferrable thinking it relaxes something; it would relax the wrong
thing.

Shape / compliance
------------------
* Tenant-scoped (invariant 1): ``company_id`` INTEGER NOT NULL, FK ``companies.id``,
  indexed — the ``TenantMixin`` shape. App-layer tenancy via ``tenant_query`` is the
  enforcement.
* ``ENABLE ROW LEVEL SECURITY`` on the new table, Postgres only. This revision runs
  after ``059``/``060``, which enabled RLS across ``public`` and left it
  deny-by-default with zero policies, so a table created afterwards must enable it
  itself or it re-flags the Supabase Security Advisor's ``rls_disabled_in_public``
  ERROR (precedent ``061``/``084``; see ``docs/SUPABASE_SECURITY.md``). The app role
  bypasses RLS.
* No ``SoftDeleteMixin``, no ``is_active``, no ``status`` — same argument as
  ``PartNumberAlias``: a third state is a mask every future reader must remember to
  filter, the trap invariant 3 documents after the 2026-08-16 ``Vendor`` sweep. A
  combine happened or it did not; reversing one is a NEW, reasoned, audited combine
  in the other direction, never an edit of this row.
* The tamper-evident ``audit_log`` table is untouched, and so is every other
  existing table: this revision is purely additive DDL. It creates no enum, alters
  no enum, and adds no column anywhere else.

No backfill
-----------
There is nothing to backfill. No combine has ever happened, and inventing header
rows over historical adjustments would be fabricating a record of merges nobody
performed — the same reasoning as ``084``'s empty alias table and ``083``'s
forward-only unit numbers.

Idempotent and reversible
-------------------------
Upgrade guards the table and each index independently (``_has_table`` /
``_has_index``), so the create_all-bootstrapped path and any re-run are clean
no-ops. Downgrade drops the indexes then the table, each guarded, so a partially
applied upgrade still downgrades cleanly. Round-tripped locally: ``upgrade head`` ->
``downgrade -1`` -> ``upgrade head``.

Operational note: ``CREATE TABLE`` on a table that does not yet exist takes no lock
anything else can be waiting on, and there is no backfill pass, so this migration is
safe to run against live Postgres during traffic and carries no deploy-ordering
constraint of its own — beyond the obvious one that it must land before the code
that reads the table.

Revision ID: 085_inventory_combines
Revises: 084_part_number_aliases
Create Date: 2026-08-23
"""

import sqlalchemy as sa
from alembic import op

revision = "085_inventory_combines"
down_revision = "084_part_number_aliases"
branch_labels = None
depends_on = None


TABLE = "inventory_combines"

# (index_name, columns, unique) -- every one of these is mirrored on the model.
#
# The first five come from ``index=True`` on the columns themselves (``company_id``
# via TenantMixin); the last two are the model's explicit ``__table_args__``
# composites answering "what was ever folded INTO / OUT OF this part?", which is the
# read the Parts screen and any investigation start from and which the single-column
# ``source_part_id`` / ``target_part_id`` indexes cannot serve without also scanning
# other tenants' rows.
INDEXES = [
    ("ix_inventory_combines_id", ["id"], False),
    ("ix_inventory_combines_combine_number", ["combine_number"], False),
    ("ix_inventory_combines_source_part_id", ["source_part_id"], False),
    ("ix_inventory_combines_target_part_id", ["target_part_id"], False),
    ("ix_inventory_combines_company_id", ["company_id"], False),
    ("ix_inventory_combines_company_source", ["company_id", "source_part_id"], False),
    ("ix_inventory_combines_company_target", ["company_id", "target_part_id"], False),
]

# Enforced as a real UNIQUE constraint (not a unique index) so it matches the model's
# __table_args__ UniqueConstraint exactly. See the docstring on why it is created
# inside create_table and why it must stay non-deferrable.
UNIQUE_CONSTRAINT = "uq_inventory_combines_company_number"


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
    # columns first, then TenantMixin's ``company_id`` (appended via the MRO). Order
    # is cosmetic to Postgres but keeping it identical is what lets a schema diff
    # between a bootstrapped and a migrated database come back empty.
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        # The minted human-facing number, ``COMB-000123``. String(50) leaves room for
        # a future prefix change without a type alteration on a live table.
        sa.Column("combine_number", sa.String(length=50), nullable=False),
        # Both FKs point at ``parts.id`` and both are NOT NULL. The source part is
        # never deleted -- it stays in the catalog at qty 0, optionally deactivated
        # (``source_deactivated``) -- so neither FK can be orphaned by the feature's
        # own verbs.
        sa.Column("source_part_id", sa.Integer(), nullable=False),
        sa.Column("target_part_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        # NOT NULL by design: every identity-affecting verb in this system requires a
        # written reason (receiving void, NCR void, vendor delete, part renumber), and
        # merging two article identities under AS9100D 8.5.2 is at least as
        # consequential as any of them.
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("lines_moved", sa.Integer(), nullable=True),
        # A SNAPSHOT of what the operator was shown when they approved, not a source
        # of truth -- the ledger is authoritative for what moved. These four are here
        # because the "before" figures are not reconstructable once later movements
        # have posted. Same reasoning as ``CycleCount.total_variance_value``.
        sa.Column("source_quantity_before", sa.Float(), nullable=True),
        sa.Column("source_quantity_after", sa.Float(), nullable=True),
        sa.Column("target_quantity_before", sa.Float(), nullable=True),
        sa.Column("target_quantity_after", sa.Float(), nullable=True),
        # NOT NULL with a Python-side default only -- mirrors the model; see the
        # docstring on why no server_default is added here.
        sa.Column("source_deactivated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["source_part_id"], ["parts.id"]),
        sa.ForeignKeyConstraint(["target_part_id"], ["parts.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "combine_number", name=UNIQUE_CONSTRAINT),
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1) The table. Guarded so the create_all bootstrap path (where the model already
    #    built it) no-ops rather than erroring on a duplicate table.
    if not _has_table(TABLE):
        _create_table()

    # 2) Indexes, each guarded independently -- create_all emits all seven too, and a
    #    re-run or a partially applied upgrade must be a no-op rather than a failure.
    for index_name, columns, unique in INDEXES:
        if not _has_index(TABLE, index_name):
            op.create_index(index_name, TABLE, columns, unique=unique)

    # 3) RLS on the new table (Postgres only; this revision runs after 059/060).
    #    Deny-by-default with zero policies; the app role bypasses it and app-layer
    #    tenancy via tenant_query stays the enforcement. Without this the Supabase
    #    Security Advisor re-flags rls_disabled_in_public as an ERROR.
    if is_postgres:
        op.execute(f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    # Indexes first, then the table (FK-safe order). Each guarded so a partial
    # upgrade downgrades cleanly. Dropping the table takes the unique constraint,
    # the FKs and the RLS setting with it -- there is nothing left behind to
    # un-enable.
    #
    # This drops real records if any combine has been performed. That is correct for
    # a downgrade (the operator asked to unmake the schema), and it is why the
    # tamper-evident account of a combine is the ``audit_log`` row written through
    # AuditService, not this table -- audit_log is untouched by both directions of
    # this revision, and the ledger rows the combine wrote survive too. Nothing that
    # moved stock is lost here; only the queryable index over it.
    for index_name, _columns, _unique in INDEXES:
        if _has_index(TABLE, index_name):
            op.drop_index(index_name, table_name=TABLE)
    if _has_table(TABLE):
        op.drop_table(TABLE)
