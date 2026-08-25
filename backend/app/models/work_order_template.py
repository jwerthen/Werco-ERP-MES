"""A named, reusable job plan — the catalog entry behind "run that one again".

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
The shop re-runs the same work constantly: the same laser nest group, the same
press-brake part set. Two doors already existed and neither is a catalog:

* **Import Nest Package** re-extracts the PDFs, re-confirms every nest, and
  force-sets ``RELEASED`` — the new job is on the dispatch board before anyone
  has looked at it.
* **Duplicate** copies a plan onto a fresh DRAFT correctly, but you have to go
  *find* last month's work order first. There is no list of "the jobs we re-run".

This row is that list. It is a NAME, a NOTE and a POINTER at the work order whose
plan is the exemplar — nothing else. Using it calls
``services/work_order_duplicate_service.duplicate_work_order`` against that source,
which is the SAME copy engine ``POST /work-orders/{id}/duplicate`` uses, so a
template can never mint a work order a duplicate could not.

**A template is a POINTER, never a frozen plan.** That is the load-bearing choice
here and it has consequences worth stating before someone files them as bugs:

* Edit the source work order — add an operation, soft-delete a nest, cancel a tie —
  and the template's next use reflects the edit. This is intended: the exemplar IS
  the plan, and a planner improving the master job expects the improvement to carry.
  It is also why the list endpoint returns a LIVE plan summary read off the source
  (operation count, nest count, open ties, work centers) rather than a stored one:
  what the planner is shown is what they will get, with no hidden drift.
* Delete the source work order and the template stops working. It is not
  auto-removed and it is not hidden — it is listed as UNAVAILABLE with the reason,
  and using it is refused 409. Silently hiding it is the mask trap invariant 3
  documents; silently deleting it destroys a name nobody asked to lose.
* Process-sheet steps are re-snapshotted from each family's currently-RELEASED
  revision at use time (``_resnapshot_process_sheet_steps``), so one template used
  twice six months apart can legitimately produce two different travelers. Correct,
  and only possible because this is a pointer.

The alternative — freezing the plan into ``work_order_template_operations`` /
``_nests`` / ``_allocations`` — was rejected outright. It would mean a SECOND copy
service reimplementing every decision the 1,300-line duplicate service already makes
(what scales with quantity, what a nest tie's ``qty_planned`` is derived from, which
skips reach the planner and which refuse the whole call), and every future fix would
have to be made twice or the two would drift. One copy engine, one set of rules.

WHY THIS IS NOT A SECOND WORK-ORDER TABLE
------------------------------------------
Nothing on the shop floor reads this table. It holds no status, no operations, no
quantity produced; ``dispatch_service``, the kiosk and the wallboard have never
heard of it. The only thing a template can do is ask the duplicate service for a
new DRAFT work order — which is the same thing a planner does by hand, with the
same role gate, the same refusals and the same skip envelope.

WHY ``SoftDeleteMixin`` HERE, WHEN ``InventoryCombine`` REFUSED IT
------------------------------------------------------------------
``InventoryCombine`` and ``PartNumberAlias`` deliberately carry no tombstone,
arguing that a third state is a mask every future reader must remember to filter
(the trap invariant 3 documents after the 2026-08-16 ``Vendor`` sweep). That
argument is about EVENT rows: a combine happened or it did not, and reversing one
is a new combine, never an edit.

A template is the other kind of object — a curated catalog entry, closer to
``Vendor`` than to a ledger row — and it is deleted by a person who may be wrong.
So it takes the tombstone, and the mask is contained rather than latent: every read
of this table lives in ``services/work_order_template_service.py`` behind
``_live_template_or_404`` / ``live_templates_query``, which is the per-site
discipline invariant 3 actually asks for.

Two consequences are deliberate:

* **The unique name index is PARTIAL — live rows only.** Deleting "Miratech nest
  group" frees the name immediately. An unconditional constraint would burn the
  name forever and force the planner to invent "Miratech nest group 2".
* **There is no restore verb, on purpose.** A template holds no information that
  cannot be reproduced in one click: re-open the source work order and press "Save
  as template" again, and the result is identical, because the plan was never in
  here. The tombstone exists so nothing is physically destroyed (invariant 3's
  letter) — not because the row needs an undo path, and not to keep audit rows
  readable: ``_live_template_or_404`` 404s a deleted template anyway, and every
  audit row already carries the name verbatim in ``resource_identifier`` and
  ``extra_data.template_name``.

THIS ROW IS NOT THE AUDIT RECORD
--------------------------------
The tamper-evident account of a template being created, renamed, deleted or USED is
the ``audit_log`` row written through ``AuditService`` (invariant 2 — the
``008``/``060`` triggers refuse UPDATE and DELETE, and the hash chain covers it).
This table is a convenience index; the chain is the record.

See ``app/services/work_order_template_service.py`` for the rules,
``docs/WORK_ORDER_TEMPLATES.md`` for the runbook, and ``docs/API.md`` -> Work Orders
-> "Work order templates".
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, text

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin

# One LIVE template per name per company. Declared as a PARTIAL unique index rather
# than a UniqueConstraint precisely so a deleted template releases its name — see the
# module docstring.
#
# The predicate is written ``NOT is_deleted`` rather than ``is_deleted = false``
# because ONE string has to compile on both dialects: Postgres rejects
# ``is_deleted = 0`` (type error) and SQLite — which the entire pytest suite runs on —
# has no ``false`` literal before 3.23. ``NOT is_deleted`` is valid in both, and it is
# total because ``SoftDeleteMixin.is_deleted`` is ``nullable=False`` with a
# ``server_default='false'``, so it is never NULL and no row can fall out of the index
# through three-valued logic.
#
# It is declared for BOTH dialects (``postgresql_where`` AND ``sqlite_where``, the
# 074/076 convention). A ``postgresql_where`` alone degrades to a FULL unique index on
# SQLite, which would make the test suite enforce a rule production does not — here,
# refusing to reuse a deleted template's name.
LIVE_NAME_PREDICATE = "NOT is_deleted"

UNIQUE_LIVE_NAME_INDEX = "uq_work_order_templates_company_name_live"


class WorkOrderTemplate(Base, SoftDeleteMixin, TenantMixin):
    """A named plan the shop re-runs: a pointer at the work order that exemplifies it."""

    __tablename__ = "work_order_templates"
    __table_args__ = (
        # The catalog read: this company's live templates, in name order. Mirrored
        # into migration 087 in the same PR (the 042/078/079/080 lock-step
        # convention: an index declared only in a migration is skipped entirely by
        # the ``create_all`` + ``alembic stamp`` bootstrap, which is how prod lost
        # ~42 read-path indexes and 22 lineage FKs).
        Index("ix_work_order_templates_company_live", "company_id", "is_deleted", "name"),
        # "Which templates point at this work order?" — not read by a screen today; it
        # serves the batched source lookup in ``plan_summaries_for`` (an ``IN`` over
        # source ids on every catalog page) and keeps the obvious future question
        # answerable without a scan.
        Index("ix_work_order_templates_company_source", "company_id", "source_work_order_id"),
        Index(
            UNIQUE_LIVE_NAME_INDEX,
            "company_id",
            "name",
            unique=True,
            postgresql_where=text(LIVE_NAME_PREDICATE),
            sqlite_where=text(LIVE_NAME_PREDICATE),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    # What the planner calls this job. Stored VERBATIM: there is no ingest-time
    # sanitization anywhere in this system (bleach was removed 2026-07-30 because
    # stripping corrupted quality records), and nothing renders it as raw HTML.
    name = Column(String(120), nullable=False, index=True)

    # Free text: "the Miratech housing set — run 3 sheets unless Ken says otherwise".
    notes = Column(Text, nullable=True)

    # THE POINTER. Not nullable: a template with no exemplar has no plan, and there
    # is nothing sensible for the use endpoint to copy. The referenced work order is
    # SOFT-DELETABLE, so every read of it must carry its own ``is_deleted == False``
    # (invariant 3 — ``tenant_query`` scopes ``company_id`` and nothing else); a
    # template whose source is tombstoned is reported UNAVAILABLE, never hidden and
    # never silently repaired.
    source_work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, index=True)

    # A PREFILL, not a stored plan number. ``POST /work-order-templates/{id}/use``
    # falls back to it when the caller sends no quantity, which is what makes the
    # click-once case click-once. It is IGNORED for a nest-bearing template, because
    # a laser work order's ``quantity_ordered`` is DEFINED as the sum of its nests'
    # planned runs and the duplicate service derives it — see
    # ``laser_nest_service._recompute_child_quantity_ordered``. Nullable: "whatever
    # the source ran" is a legitimate answer and is what the use path falls back to.
    default_quantity = Column(Float, nullable=True)

    # Hand-rolled rather than ``TimestampMixin``, matching ``WorkOrder`` itself: the
    # mixin's tz-aware columns with ``server_default='now()'`` are not what the rest
    # of this table's neighbourhood uses, and mixing the two shapes is how a schema
    # diff between the create_all and migrated bootstraps stops coming back empty.
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Deliberately NO ``relationship()`` to the source work order. Nothing reads one:
    # the plan summary is built from BATCHED queries in
    # ``work_order_template_service.plan_summaries_for`` precisely so a catalog page
    # costs a bounded number of round trips, and a lazy relationship sitting here is an
    # invitation to write the per-row version that reintroduces the N+1.

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkOrderTemplate {self.name!r} -> work_order {self.source_work_order_id}>"
