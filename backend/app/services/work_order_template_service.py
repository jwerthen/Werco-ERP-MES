"""Named work-order templates — save a job's plan under a name, run it again in one click.

THE WHOLE FEATURE IN ONE SENTENCE
---------------------------------
A template is a NAME plus a POINTER at the work order whose plan it stands for, and
"use template" hands that work order to
``work_order_duplicate_service.duplicate_work_order`` — the same copy engine ``POST
/work-orders/{id}/duplicate`` uses, landing a new ``DRAFT`` with ``PENDING``
operations.

THE RULE THIS MODULE EXISTS TO KEEP
-----------------------------------
**A template must never mint a work order the duplicate path would not have.** It
adds a name and a lookup; it adds no authority. Concretely, and each of these is a
property a test pins rather than a convention:

* The role gate is the duplicate endpoint's own trio (ADMIN / MANAGER / SUPERVISOR),
  enforced in the router.
* Every refusal ``duplicate_work_order`` raises propagates untouched — the retired
  produced part (409) and ``ProcessSheetUnavailableError``
  (409 ``PROCESS_SHEET_UNAVAILABLE``). This module catches neither.
* The skip envelope propagates untouched. ``DuplicateResult.skipped_operations`` /
  ``.skipped_allocations`` are safety information, not telemetry: a skipped material
  tie means the new job carries NO demand for that material, so no shortage is
  raised, the work runs, and stock is never deducted. The use endpoint returns the
  SAME ``WorkOrderDuplicateResponse`` envelope, so the planner sees the omission on
  the same result view the Duplicate dialog shows.
* The result lands ``DRAFT``. ``_copy_header`` hard-codes it, and
  :func:`_assert_landed_as_draft` re-checks it before the caller commits — see that
  function for why a redundant check earns its keep here.

WHY NOTHING ABOUT THE PLAN IS STORED
------------------------------------
A frozen plan would need ``work_order_template_operations`` / ``_nests`` /
``_allocations`` and a second copy service to walk them, re-deciding everything the
existing one already decides: what scales with quantity, how a nest tie's
``qty_planned`` is derived, which omissions skip and which refuse the whole call.
Two implementations of those rules is how the office and floor sequencing gates
drifted apart, and this system has paid that bill once already.

So the plan is read LIVE off the source work order every time — including for the
list, which returns a per-template :class:`TemplatePlanSummary` (operation count,
nest count, open ties, work centers, type, pooled-vs-sequenced) so the planner sees
what they are about to get. There is no stored summary that can go stale, because
there is no stored summary.

WHAT A DELETED SOURCE DOES: NOTHING TO THE TEMPLATE
---------------------------------------------------
The source work order is soft-deletable, and a template **reads through** the
tombstone: the plan summary is still computed off it, and ``POST /{id}/use`` still
produces a DRAFT. That is an owner decision (2026-08-27) overriding the original
refuse-on-deleted design — *"templates need to stay even if there is no work order
present for it"*. A catalog entry that stops working because somebody tidied up a
finished job is the worse failure: the name, the note and the one-click re-run are the
curated part, and an unrelated action destroyed them.

**The invariant-3 tension, stated rather than argued away.** Invariant 3 says every
read of a soft-deletable model carries its own ``is_deleted`` predicate, with four
legitimate non-filterers: the delete verb, the restore verb, a duplicate probe, and a
HISTORICAL RECORD. A template's source is the historical-record shape — the template
permanently names the job it was saved from, and ``source_work_order_id`` is
``NOT NULL`` with a plain FK and no ``ON DELETE``, so the template owns that row for
the row's whole life and the row can never physically vanish underneath it
(``work_orders.py``'s hard delete refuses 409 rather than orphaning one — that refusal
exists *because* this read stopped filtering).

The counter-argument is real: minting a NEW job from a deleted plan is closer to
SELECTION than to reading a record, and selection is exactly what invariant 3 gates.
It is not resolved, it is decided — and the half that stays gated is saving a template
FROM a deleted work order, which is still 404 (:func:`resolve_catalogable_work_order`).
An already-saved template is a catalog entry; a new one is a fresh selection.

**Only the refusal is dropped, never the signal.** ``TemplatePlanSummary`` carries
``source_work_order_deleted``, so every read still discloses that the job behind the
name is gone. ``available`` now means the much narrower "the source row could not be
resolved at all", which the FK makes near-unreachable and which is kept anyway: a
defensive branch that can still answer is better than one that was deleted.

TENANCY, AUDIT, ATOMICITY
-------------------------
Every read is company-scoped through ``tenant_query`` (invariant 1). Every write goes
through ``AuditService`` (invariant 2) — including USE, which writes a row against
the template naming the work order it produced, so "which template made this job" and
"how often is this template run" are both answerable from the chain. Nothing here
commits; the router wraps each write in ``atomic_transaction``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, func
from sqlalchemy.orm import Query, Session

from app.db.tenant_filter import tenant_query
from app.models.laser_nest import LaserNest
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.models.work_order_template import WorkOrderTemplate
from app.services.audit_service import AuditService
from app.services.material_tie_part_gate import part_is_tieable_material
from app.services.work_order_duplicate_service import DuplicateResult, duplicate_work_order

# Machine-readable reason a template cannot be used at all. Returned on the summary so
# the list can disable the button WITH the cause, and echoed in the 409 detail so the
# two cannot disagree.
#
# ONE value, and it is deliberately NOT the one that used to live here.
# ``source_work_order_deleted`` was retired as an *unavailability* reason when the owner
# ruled that a deleted source must not stop a template from working (module docstring):
# deletion is now disclosed on the summary as ``source_work_order_deleted: true`` while
# ``available`` stays true. What is left is the case the ``NOT NULL`` FK makes
# near-unreachable — the source ROW cannot be resolved at all (a cross-tenant id, or a
# row that escaped the FK somehow). Kept rather than deleted, because a branch that can
# still answer beats one that was removed on the argument that it cannot fire.
UNAVAILABLE_SOURCE_MISSING = "source_work_order_missing"


@dataclass
class TemplatePlanSummary:
    """What using this template would produce, read LIVE off the source work order.

    Nothing here is stored. That is the point: a template is a pointer, so the only
    honest summary is the one computed at read time. A stored ``nest_count`` would go
    stale the first time somebody soft-deleted a nest on the source, and the planner
    would pick a template believing it carries 21 nests and get 20.

    ``available`` is False only when the source work order row could not be RESOLVED —
    not when it has been soft-deleted. A soft-deleted source is summarised in full and
    is usable; it is disclosed through ``source_work_order_deleted`` instead. See the
    module docstring for why, and for the invariant-3 tension that decision carries.
    """

    available: bool = True
    unavailable_reason: Optional[str] = None
    # The disclosure that replaced the refusal. True means the job this template was
    # saved from has been soft-deleted: the template still works and still produces the
    # same DRAFT, but a planner reading the catalog should know the exemplar is in the
    # archive — restoring it, or saving a fresh template from a current job, are both
    # things they may want to do, and neither is discoverable if nothing says so.
    source_work_order_deleted: bool = False

    source_work_order_number: Optional[str] = None
    source_status: Optional[str] = None
    work_order_type: Optional[str] = None
    # False = a dispatch POOL (press-brake / weld-sub batches promote together);
    # True = a sequenced routing. Carried by the duplicate, so it is part of what the
    # planner is picking. Inert on a laser work order, whose pooling comes from
    # ``is_laser_dispatch_work_order`` instead — surfaced anyway rather than hidden,
    # because the column is what the copy will carry.
    sequential_operations: Optional[bool] = None
    priority: Optional[int] = None

    operation_count: int = 0
    # Live nests only. A nest-BEARING template is the laser case: its quantity is
    # derived from the sum of these nests' planned runs, not typed.
    nest_count: int = 0
    planned_runs_total: int = 0
    # OPEN ties only — cancelled/closed rows are tombstones the duplicate does not
    # copy, so counting them would promise material the copy will not carry.
    open_material_tie_count: int = 0
    # Distinct work centers the operations sit on, in sequence order. This is what
    # makes "the press-brake set" recognisable in a list of names.
    work_centers: list[str] = field(default_factory=list)
    source_quantity_ordered: Optional[float] = None

    @property
    def nest_bearing(self) -> bool:
        """True when the produced work order's quantity will be DERIVED, not typed."""
        return self.nest_count > 0


@dataclass
class TemplateUseResult:
    """One use of a template: the duplicate's own result, plus which template ran it."""

    template: WorkOrderTemplate
    duplicate: DuplicateResult


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def live_templates_query(db: Session, company_id: int) -> Query:
    """Every LIVE template in this company. The one place the tombstone filter lives.

    ``tenant_query`` scopes ``company_id`` and nothing else (invariant 3), so the
    ``is_deleted == False`` predicate is written here explicitly and every read goes
    through this function rather than re-deriving it. The deliberate NON-users are
    named rather than left to be discovered:

    * :func:`create_template`'s duplicate-name probe reads LIVE rows only — which is
      correct here and is NOT the usual "a duplicate probe must see tombstones"
      exception, because the unique index is PARTIAL
      (``uq_work_order_templates_company_name_live``, ``WHERE NOT is_deleted``). A
      deleted template does not own its name, so probing tombstones would refuse a
      name the database would happily accept.
    * :func:`delete_template` resolves through :func:`_live_template_or_404`, so a
      second delete answers 404 rather than re-deleting. There is no restore verb to
      need the other direction — see the model docstring.
    """
    return tenant_query(db, WorkOrderTemplate, company_id).filter(WorkOrderTemplate.is_deleted == False)  # noqa: E712


def templates_pointing_at_work_order(db: Session, work_order_id: int, company_id: int) -> Query:
    """Every template row whose FK names ``work_order_id`` — **tombstones included**.

    The one read in this module scoped to the FOREIGN KEY rather than to what a user
    can see, and that is the whole point. ``work_order_templates.source_work_order_id``
    is ``NOT NULL`` with a plain FK and no ``ON DELETE``, and Postgres does not consult
    ``is_deleted`` before refusing to drop the row it points at. A soft-deleted
    template is invisible in the catalog and still holds that reference just as hard as
    a live one.

    So the hard-delete guard in ``work_orders.py`` MUST come through here and not
    through :func:`live_templates_query`: filtering tombstones out would let the
    physical delete proceed into a ``ForeignKeyViolation`` — a 500 on prod that no test
    on this repo's in-memory SQLite can reproduce, since SQLite runs with foreign-key
    enforcement off. Refusing legibly is the behavior; soft-deleting the work order is
    the remedy, and it is unaffected.

    Company-scoped like every other probe on that path. A template in ANOTHER company
    naming this work order is not a state any verb here can create (the save path
    resolves the source company-scoped), and naming one in a refusal would leak that it
    exists (invariant 1).
    """
    return tenant_query(db, WorkOrderTemplate, company_id).filter(
        WorkOrderTemplate.source_work_order_id == work_order_id
    )


def _live_template_or_404(db: Session, template_id: int, company_id: int) -> WorkOrderTemplate:
    """Resolve one live template, or 404.

    404 and never 403: a template in another company must be indistinguishable from
    one that does not exist, or the response leaks that it does (invariant 1).
    """
    template = live_templates_query(db, company_id).filter(WorkOrderTemplate.id == template_id).first()
    if template is None:
        raise HTTPException(status_code=404, detail="Work order template not found")
    return template


def resolve_source_work_order(db: Session, work_order_id: int, company_id: int) -> Optional[WorkOrder]:
    """The source work order behind a template — **soft-deleted or not**.

    This is the one read in this module that deliberately carries no ``is_deleted``
    predicate, i.e. the one place it departs from invariant 3's default. The
    justification AND the counter-argument are in the module docstring under "WHAT A
    DELETED SOURCE DOES"; the short version is that a template permanently NAMES the
    job it was saved from and the ``NOT NULL`` FK means it owns that row for the row's
    whole life, so this is the historical-record exception rather than a forgotten
    filter. It is not a comfortable one — see the docstring — and it was the owner's
    call, not an inference from the invariant.

    **Tenancy is unchanged.** ``tenant_query`` still scopes ``company_id``, so a
    cross-tenant id still resolves to ``None`` (invariant 1). Nothing about this change
    widens what one company can see.

    Returning ``None`` rather than raising is still deliberate, and it now means
    something much narrower: the row could not be resolved AT ALL. The LIST path turns
    that into ``available=False``, the USE path into a 409 — one resolver, two answers,
    so the catalog and the write still cannot disagree. Deletion is no longer either
    answer; it is disclosed as ``source_work_order_deleted`` on the summary.

    The SELECTION half — which work order a NEW template may be saved from — is
    :func:`resolve_catalogable_work_order`, and that one does filter.
    """
    return tenant_query(db, WorkOrder, company_id).filter(WorkOrder.id == work_order_id).first()


def resolve_catalogable_work_order(db: Session, work_order_id: int, company_id: int) -> Optional[WorkOrder]:
    """The **live** work order a new template may be saved FROM, or ``None``.

    Keeps invariant 3's explicit tombstone filter, because pointing a brand-new
    template at a work order is SELECTION, not reading a record: a job somebody has
    deleted is not a job the shop should be able to catalogue as one to re-run.

    The asymmetry with :func:`resolve_source_work_order` is the design, not an
    inconsistency — an already-saved template reads through a tombstone, a new one
    cannot be created over one. Written as a wrapper so there is exactly one place that
    knows how a template's source is fetched, with the predicate visible on top.
    """
    source = resolve_source_work_order(db, work_order_id, company_id)
    if source is None or source.is_deleted:
        return None
    return source


def plan_summaries_for(
    db: Session,
    templates: list[WorkOrderTemplate],
    company_id: int,
) -> dict[int, TemplatePlanSummary]:
    """Live plan summaries for a whole page of templates, in a bounded number of queries.

    Batched rather than per-row on purpose: the catalog is a list screen, and a
    per-template summary built with a handful of queries each is the PERF-4 N+1 shape
    this repo has fixed elsewhere. FIVE queries total regardless of page size —
    sources, operations+work-centers, nests, ties, tied parts — and every one of them
    tenant-scoped, on every joined table.

    THE COUNTS MUST MATCH WHAT THE COPY ACTUALLY PRODUCES
    ----------------------------------------------------
    This summary is the whole justification for a template being a POINTER rather than
    a frozen plan: what the planner is shown is what they will get. A count that is
    merely an upper bound breaks that promise in the direction that matters — a row
    reading "2 ops · 21 nests · 4 open ties" when the copy will carry one fewer tie is
    a planner releasing a job believing it carries material demand it does not have.

    So the two counts that ``duplicate_work_order`` can SKIP are reduced by exactly the
    skips it would apply, using the SAME rules rather than a second opinion:

    * an operation whose only laser nest is soft-deleted is not copied
      (``_copy_operations`` via ``_LaserNestBacking.deleted_only_operation_ids``), so it
      is not counted, and neither is a tie scoped to it (``operation_not_copied``);
    * a tie whose part has been soft-deleted (``part_not_available``) or is one the shop
      PRODUCES (``part_not_tieable``) is not copied, so it is not counted. The second
      test is ``material_tie_part_gate.part_is_tieable_material`` — the shared predicate
      the copy itself calls, imported rather than restated, so the two cannot drift.

    That is reuse of the rules, not a reimplementation of the copy: no plan is built
    here and nothing is written. The remaining skip reason, ``nest_runs_unavailable``,
    is documented as defensive and unreachable, so there is nothing to subtract for it.

    A SOFT-DELETED source is summarised in full, exactly like a live one, and flagged
    ``source_work_order_deleted``. The batched read below therefore carries no
    ``is_deleted`` predicate — it must change in LOCKSTEP with
    :func:`resolve_source_work_order` or a template the use path happily runs would
    render with a blank plan, which is the "summary must match what the copy produces"
    promise broken in the direction that matters.

    Only a source that could not be resolved at all gets an ``available=False`` summary
    carrying nothing but the reason. Such a template is still NOT dropped from the
    list: hiding it is the mask trap invariant 3 documents — the planner's template
    silently vanishes and nothing says why.
    """
    summaries: dict[int, TemplatePlanSummary] = {}
    if not templates:
        return summaries

    source_ids = {template.source_work_order_id for template in templates}

    # No ``is_deleted`` filter, in lockstep with ``resolve_source_work_order`` — a
    # tombstoned source is summarised like any other. ``tenant_query`` still scopes
    # ``company_id``, so this is narrower than it looks: it reads through a tombstone,
    # never across a tenant.
    sources = {
        work_order.id: work_order
        for work_order in tenant_query(db, WorkOrder, company_id).filter(WorkOrder.id.in_(source_ids)).all()
    }
    resolved_source_ids = set(sources)

    operation_rows = []
    nest_rows = []
    tie_rows = []
    if resolved_source_ids:
        # Operations + their work centers, in sequence order, so ``work_centers``
        # reads the way the traveler does rather than alphabetically.
        #
        # The company predicate on the OUTER-JOINED ``WorkCenter`` rides in the ON
        # clause, not in ``.filter()``. Two reasons, both load-bearing:
        #   * ``WorkCenter`` is a ``TenantMixin`` table and its ``name`` is SELECTed
        #     straight into the response. Without a predicate, an operation pointing at
        #     another company's work center (``_assert_work_center_in_company`` exists
        #     in ``work_orders.py`` because that cross-tenant write really happened, so
        #     pre-fix rows can still exist) would render that tenant's work-center name
        #     here and in the panel's CSV export. That is invariant 1.
        #   * putting it in ``.filter()`` would silently convert the OUTER join to an
        #     INNER one, dropping every work-center-less operation from
        #     ``operation_count`` — trading a leak for an undercount.
        operation_rows = (
            db.query(
                WorkOrderOperation.work_order_id,
                WorkOrderOperation.id,
                WorkCenter.name,
            )
            .outerjoin(
                WorkCenter,
                and_(
                    WorkOrderOperation.work_center_id == WorkCenter.id,
                    WorkCenter.company_id == company_id,
                ),
            )
            .filter(
                WorkOrderOperation.company_id == company_id,
                WorkOrderOperation.work_order_id.in_(resolved_source_ids),
            )
            .order_by(WorkOrderOperation.work_order_id, WorkOrderOperation.sequence, WorkOrderOperation.id)
            .all()
        )

        # Nests reached through their operation — the same join ``_copy_laser_nests``
        # and ``_laser_nest_backing`` use, which scopes to the work order and drops
        # nests with a NULL operation. Unlike the copy's own read this does NOT filter
        # ``is_deleted``: it needs BOTH, because a live nest feeds the nest count while
        # an operation whose nests are ALL dead is an operation the copy will skip.
        nest_rows = (
            db.query(
                WorkOrderOperation.work_order_id,
                WorkOrderOperation.id,
                LaserNest.is_deleted,
                LaserNest.planned_runs,
            )
            .join(WorkOrderOperation, LaserNest.work_order_operation_id == WorkOrderOperation.id)
            .filter(
                LaserNest.company_id == company_id,
                WorkOrderOperation.company_id == company_id,
                WorkOrderOperation.work_order_id.in_(resolved_source_ids),
            )
            .all()
        )

        # OPEN ties only — cancelled/closed rows are tombstones the copy does not
        # carry. This is the set ``_copy_material_allocations`` STARTS from; the skips
        # it then applies are subtracted below.
        tie_rows = (
            db.query(
                WorkOrderMaterialAllocation.work_order_id,
                WorkOrderMaterialAllocation.work_order_operation_id,
                WorkOrderMaterialAllocation.part_id,
            )
            .filter(
                WorkOrderMaterialAllocation.company_id == company_id,
                WorkOrderMaterialAllocation.work_order_id.in_(resolved_source_ids),
                WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
            )
            .all()
        )

    # Which operations will the copy refuse to carry? An operation is nest-backed if it
    # has any nest row at all; it is DEAD if none of them is live. Byte-identical to
    # ``_LaserNestBacking``'s ``nest_backed - with_live_nest``.
    nest_backed_operations: set[int] = set()
    operations_with_live_nest: set[int] = set()
    nest_counts: dict[int, tuple[int, int]] = {}
    for work_order_id, operation_id, is_deleted, planned_runs in nest_rows:
        nest_backed_operations.add(operation_id)
        if is_deleted:
            continue
        operations_with_live_nest.add(operation_id)
        count, runs = nest_counts.get(work_order_id, (0, 0))
        nest_counts[work_order_id] = (count + 1, runs + int(planned_runs or 0))
    dead_nest_operations = nest_backed_operations - operations_with_live_nest

    operation_counts: dict[int, int] = {}
    work_centers: dict[int, list[str]] = {}
    for work_order_id, operation_id, work_center_name in operation_rows:
        if operation_id in dead_nest_operations:
            # The copy skips this one (``laser_nest_deleted``), so neither its count
            # nor its work center belongs in a summary of what the planner will get.
            continue
        operation_counts[work_order_id] = operation_counts.get(work_order_id, 0) + 1
        if work_center_name:
            names = work_centers.setdefault(work_order_id, [])
            # Distinct, but in first-appearance (sequence) order — a set would
            # scramble the route into an arbitrary order on every read.
            if work_center_name not in names:
                names.append(work_center_name)

    # One batched read of the tied parts, so the two part-shaped skips can be applied
    # with the copy's own predicate instead of a second opinion.
    tie_part_ids = {part_id for _wo, _op, part_id in tie_rows if part_id is not None}
    tieable_part_ids: set[int] = set()
    if tie_part_ids:
        for part in tenant_query(db, Part, company_id).filter(Part.id.in_(tie_part_ids)).all():
            if getattr(part, "is_deleted", False):
                continue  # part_not_available
            if not part_is_tieable_material(part):
                continue  # part_not_tieable — the SHARED predicate, not a restatement
            tieable_part_ids.add(part.id)

    tie_counts: dict[int, int] = {}
    for work_order_id, operation_id, part_id in tie_rows:
        if part_id not in tieable_part_ids:
            continue
        if operation_id is not None and operation_id in dead_nest_operations:
            continue  # operation_not_copied
        tie_counts[work_order_id] = tie_counts.get(work_order_id, 0) + 1

    for template in templates:
        source = sources.get(template.source_work_order_id)
        if source is None:
            # Not the deleted case any more -- the read above sees tombstones. This is
            # the source row failing to resolve at all, which the NOT NULL FK makes
            # near-unreachable. Answered rather than crashed.
            summaries[template.id] = TemplatePlanSummary(
                available=False,
                unavailable_reason=UNAVAILABLE_SOURCE_MISSING,
            )
            continue

        nest_count, planned_runs_total = nest_counts.get(source.id, (0, 0))
        status_value = source.status.value if hasattr(source.status, "value") else source.status
        type_value = (
            source.work_order_type.value if hasattr(source.work_order_type, "value") else source.work_order_type
        )
        summaries[template.id] = TemplatePlanSummary(
            available=True,
            # Usable AND deleted is the ordinary state now, not a contradiction: the
            # flag discloses, it does not gate.
            source_work_order_deleted=bool(source.is_deleted),
            source_work_order_number=source.work_order_number,
            source_status=status_value,
            work_order_type=type_value,
            sequential_operations=bool(source.sequential_operations),
            priority=source.priority,
            operation_count=operation_counts.get(source.id, 0),
            nest_count=nest_count,
            planned_runs_total=planned_runs_total,
            open_material_tie_count=tie_counts.get(source.id, 0),
            work_centers=work_centers.get(source.id, []),
            source_quantity_ordered=float(source.quantity_ordered or 0.0),
        )

    return summaries


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def _normalized_name(name: str) -> str:
    """The stored form of a template name: trimmed, inner whitespace collapsed.

    Normalises SPELLING, never MEANING — the same rule
    ``laser_nest_text.normalize_*`` follows. Case is preserved (the planner's
    capitalisation is theirs), and nothing is stripped or escaped: this system does
    no ingest-time sanitisation, and the name is never rendered as raw HTML.

    Collapsing matters because the uniqueness rule is a database index over the
    stored bytes: without it ``"Miratech  nest"`` and ``"Miratech nest"`` are two
    templates that look identical in a list.
    """
    return " ".join((name or "").split())


def _assert_name_available(
    db: Session,
    *,
    name: str,
    company_id: int,
    exclude_template_id: Optional[int] = None,
) -> None:
    """Refuse (409) a name another LIVE template in this company already holds.

    Case-insensitive, which is DELIBERATELY STRICTER than the database index
    (``uq_work_order_templates_company_name_live`` is over the stored bytes, so
    Postgres and SQLite would both accept "Miratech Nest" beside "Miratech nest").
    Two templates whose names differ only in case are indistinguishable in a picker,
    and the picker is the entire feature. The index stays as the backstop against a
    race this probe cannot close; this is the message a planner can act on.

    Probes LIVE rows only — see :func:`live_templates_query` on why that is right
    here rather than the usual tombstone-matching duplicate probe.
    """
    probe = live_templates_query(db, company_id).filter(func.lower(WorkOrderTemplate.name) == (name or "").lower())
    if exclude_template_id is not None:
        probe = probe.filter(WorkOrderTemplate.id != exclude_template_id)
    existing = probe.first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A work order template named '{existing.name}' already exists. "
                "Pick a different name, or rename the existing template."
            ),
        )


def create_template(
    db: Session,
    *,
    source: WorkOrder,
    name: str,
    notes: Optional[str],
    default_quantity: Optional[float],
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> WorkOrderTemplate:
    """Save ``source``'s plan under a name. Writes ONE row and touches nothing else.

    ``source`` must already be resolved company-scoped and non-deleted by the caller.

    **The source work order is not modified.** Not its status, not its quantities, not
    its ties, not its dispatch position — this function adds a row that points AT it.
    That is worth stating because "save as template" reads like an action on the work
    order, and the acceptance criterion for this feature is that the exemplar comes
    through untouched.

    There is deliberately no validity gate here beyond the name. A template may name a
    work order whose part is currently retired or whose process-sheet family has no
    released revision — both of which ``duplicate_work_order`` refuses at USE time,
    which is the right moment: refusing to SAVE would block cataloguing a job that
    will be perfectly fine next month, and the refusal would name a condition the
    planner cannot see from the save dialog.

    Flushes but never commits. Wrap the call in ``atomic_transaction(db)``.
    """
    stored_name = _normalized_name(name)
    _assert_name_available(db, name=stored_name, company_id=company_id)

    template = WorkOrderTemplate(
        company_id=company_id,
        name=stored_name,
        notes=notes,
        source_work_order_id=source.id,
        default_quantity=float(default_quantity) if default_quantity is not None else None,
        created_by=user_id,
    )
    db.add(template)
    db.flush()

    audit.log_create(
        resource_type="work_order_template",
        resource_id=template.id,
        resource_identifier=template.name,
        new_values=template,
        description=(f"Saved work order {source.work_order_number} as template '{template.name}'"),
        extra_data={
            "source": "work_order_template_save",
            "source_work_order_id": source.id,
            "source_work_order_number": source.work_order_number,
            "default_quantity": template.default_quantity,
        },
    )
    return template


def update_template(
    db: Session,
    *,
    template: WorkOrderTemplate,
    name: Optional[str],
    notes: Optional[str],
    default_quantity: Optional[float],
    notes_provided: bool,
    default_quantity_provided: bool,
    company_id: int,
    audit: AuditService,
) -> WorkOrderTemplate:
    """Rename a template / edit its note or default quantity.

    The two ``*_provided`` flags exist because ``None`` is a MEANINGFUL value for both
    nullable fields — "clear the note", "no default quantity" — and a partial update
    that could not express clearing would leave a planner unable to undo a typo. The
    router derives them from ``model_fields_set`` so an omitted key and an explicit
    ``null`` stay distinguishable.

    Deliberately NOT editable: ``source_work_order_id``. Re-pointing a template at a
    different work order under the same name silently changes what every future click
    produces, with the name — the only thing anyone reads — unchanged. Save a new
    template and delete the old one; both halves are then on the chain.

    Flushes but never commits.
    """
    old_values = {
        "name": template.name,
        "notes": template.notes,
        "default_quantity": template.default_quantity,
    }

    if name is not None:
        stored_name = _normalized_name(name)
        _assert_name_available(db, name=stored_name, company_id=company_id, exclude_template_id=template.id)
        template.name = stored_name
    if notes_provided:
        template.notes = notes
    if default_quantity_provided:
        template.default_quantity = float(default_quantity) if default_quantity is not None else None

    db.flush()

    audit.log_update(
        resource_type="work_order_template",
        resource_id=template.id,
        resource_identifier=template.name,
        old_values=old_values,
        new_values={
            "name": template.name,
            "notes": template.notes,
            "default_quantity": template.default_quantity,
        },
        description=f"Updated work order template '{template.name}'",
        extra_data={"source_work_order_id": template.source_work_order_id},
    )
    return template


def delete_template(
    db: Session,
    *,
    template: WorkOrderTemplate,
    user_id: int,
    audit: AuditService,
) -> WorkOrderTemplate:
    """Soft-delete a template (invariant 3). Nothing it ever produced is affected.

    The work orders this template created are ordinary work orders and are not
    touched, and neither is the source work order it pointed at. What disappears is a
    name in a picker.

    No reason is required, unlike the destructive verbs elsewhere in this system
    (receipt void, NCR void, vendor delete, part renumber). Those all unwind or
    re-identify a PRODUCTION record; this removes a shortcut. Demanding a written
    justification for tidying a list would train people to type "x" — which is worse
    for the chain than not asking, because it makes every other required reason in the
    system look like a formality.

    Flushes but never commits.
    """
    # Snapshot BEFORE the mutation. ``soft_delete`` sets is_deleted/deleted_at/
    # deleted_by, so passing the live object afterwards would record the POST-delete
    # state as ``old_values`` -- an audit row whose "before" says it was already
    # deleted. The house precedent (purchasing.py) sidesteps this by passing no
    # old_values at all; a real before-image is better, so take it here.
    old_values = {
        "name": template.name,
        "notes": template.notes,
        "source_work_order_id": template.source_work_order_id,
        "default_quantity": template.default_quantity,
        "is_deleted": bool(template.is_deleted),
    }
    template.soft_delete(user_id)
    db.flush()

    audit.log_delete(
        resource_type="work_order_template",
        resource_id=template.id,
        resource_identifier=template.name,
        old_values=old_values,
        description=f"Deleted work order template '{template.name}'",
        extra_data={
            "source_work_order_id": template.source_work_order_id,
            # The name is freed for reuse the moment this commits -- the unique index
            # is partial (WHERE NOT is_deleted). Recorded so a chain reader can tell a
            # later template of the same name from this one.
            "name_released_for_reuse": True,
        },
        soft_delete=True,
    )
    return template


def _assert_landed_as_draft(work_order: WorkOrder, template: WorkOrderTemplate) -> None:
    """Refuse to hand back anything that is not a DRAFT. Rolls the whole use back.

    This is a redundant check today — ``_copy_header`` hard-codes
    ``status=WorkOrderStatus.DRAFT`` and nothing between here and there changes it —
    and it is kept anyway, which is a deliberate exception to "don't write dead code".

    The reason is the failure mode. The ENTIRE point of routing templates through the
    duplicate path rather than through the nest importer is that the importer
    force-sets ``RELEASED`` and puts a job on the dispatch board before anyone has
    reviewed it. If a future change to ``_copy_header`` — or a new keyword argument
    threaded through it — ever made the copy land RELEASED, a template would silently
    become the third release-forcing door in this system, and it would look identical
    to the planner right up to the moment unreviewed work appeared on the floor. That
    is not a defect a test on some other file would catch at the right time, and it is
    not one the chain would explain after the fact.

    So the guarantee is asserted where it is relied upon, and it fails LOUDLY: the
    caller's ``atomic_transaction`` rolls back, so a work order that broke the promise
    does not survive the request that made it.
    """
    status = work_order.status
    if status is WorkOrderStatus.DRAFT or status == WorkOrderStatus.DRAFT.value:
        return
    actual = status.value if hasattr(status, "value") else status
    raise RuntimeError(
        f"Work order template '{template.name}' produced work order "
        f"{work_order.work_order_number} with status {actual!r}, not DRAFT. A template must never "
        "put work on the dispatch board without a planner releasing it; refusing rather than "
        "committing an unreviewed released job."
    )


def use_template(
    db: Session,
    *,
    template: WorkOrderTemplate,
    quantity_ordered: Optional[float],
    due_date: Optional[date],
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> TemplateUseResult:
    """Create a new DRAFT work order from ``template``. The copy engine does the work.

    ``quantity_ordered`` is OPTIONAL, which is what makes the click-once case
    click-once. Resolution order — first positive value wins:

    1. the caller's value, when they supplied one;
    2. ``template.default_quantity``, the number the planner saved with the template;
    3. the source work order's own ``quantity_ordered``.

    If all three are non-positive the call is refused 422 rather than defaulted to 1:
    a fabricated quantity of one on a job that should have run fifty is a plan the
    planner never approved, and a legacy source with a zero quantity is the only way
    to get here.

    For a NEST-BEARING template none of this matters and that is not a bug: a laser
    work order's ``quantity_ordered`` is DEFINED as the sum of its nests' planned runs
    (``laser_nest_service._recompute_child_quantity_ordered``), the duplicate service
    derives it, and the resolved value above is overruled. It is still resolved and
    still sent, because the underlying call requires a positive number — and the
    response reports what was actually STORED, so no planner is shown a quantity the
    server did not keep.

    ``due_date`` is never inherited. The source's due date belongs to the run that
    already happened; carrying it forward would make the new job overdue the moment it
    exists — red on the dispatch board and counted against OTD — for a promise nobody
    made. ``None`` means unscheduled, which reads as "not promised yet" everywhere,
    where a stale date reads as "late". (``must_ship_by`` is not carried either; that
    refusal lives in the duplicate service.)

    **A SOFT-DELETED source is used normally.** It used to be a 409; the owner
    overrode that (module docstring), because a catalog entry that stops working
    because somebody deleted last month's job is the worse failure. Nothing about the
    copy changes — ``duplicate_work_order`` never inspected whether the source was
    deleted, it copies the object it is handed, and its own ``is_deleted`` predicates
    are about PARTS and LASER NESTS rather than the source work order.

    Raises 409 only when the source row cannot be resolved AT ALL, which the ``NOT
    NULL`` FK makes near-unreachable, and propagates every refusal
    ``duplicate_work_order`` raises untouched. Flushes but never commits — wrap the
    call in ``atomic_transaction(db)``.
    """
    source = resolve_source_work_order(db, template.source_work_order_id, company_id)
    if source is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Template '{template.name}' cannot be used: the work order it was saved from "
                f"(#{template.source_work_order_id}) could not be found in this company. Delete this "
                "template and save a new one from a current job."
            ),
        )

    effective_quantity = _resolve_quantity(template, source, quantity_ordered)

    result = duplicate_work_order(
        db,
        source=source,
        quantity_ordered=effective_quantity,
        due_date=due_date,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
    )

    # The promise this whole feature rests on, checked where it is relied upon.
    _assert_landed_as_draft(result.work_order, template)

    # A second row, against the TEMPLATE rather than the work order. The duplicate
    # service already wrote the work order's own CREATE row naming its source work
    # order; this one names the TEMPLATE, which is the only place the fact that a
    # catalog entry (rather than a planner browsing the list) produced this job
    # exists. It is also what makes "how often do we run this template" answerable
    # from the chain instead of from nothing.
    audit.log(
        action="USE_TEMPLATE",
        resource_type="work_order_template",
        resource_id=template.id,
        resource_identifier=template.name,
        description=(
            f"Created work order {result.work_order.work_order_number} as a draft from template " f"'{template.name}'"
        ),
        extra_data={
            "source": "work_order_template_use",
            "template_id": template.id,
            "template_name": template.name,
            "source_work_order_id": source.id,
            "source_work_order_number": source.work_order_number,
            "created_work_order_id": result.work_order.id,
            "created_work_order_number": result.work_order.work_order_number,
            "created_work_order_status": WorkOrderStatus.DRAFT.value,
            "quantity": float(result.work_order.quantity_ordered),
            # Recorded only when the two differ, i.e. a nest-bearing template whose
            # quantity was derived from its runs rather than taken from the request.
            # Same key and same condition the duplicate service uses.
            **(
                {"requested_quantity": effective_quantity}
                if float(result.work_order.quantity_ordered) != effective_quantity
                else {}
            ),
            "operation_count": result.operation_count,
            "laser_nest_count": result.nest_count,
            "material_allocation_count": result.allocation_count,
            # The same omissions the response envelope carries, on the chain too --
            # ``model_dump()`` of the very objects the endpoint returns, so the two
            # can never describe a skip differently.
            "skipped_material_allocations": [entry.model_dump() for entry in result.skipped_allocations],
            "skipped_operations": [entry.model_dump() for entry in result.skipped_operations],
        },
    )

    return TemplateUseResult(template=template, duplicate=result)


def _resolve_quantity(
    template: WorkOrderTemplate,
    source: WorkOrder,
    requested: Optional[float],
) -> float:
    """The quantity the duplicate is asked for. See :func:`use_template` for the order.

    Each candidate must be POSITIVE to win, not merely present: ``quantity_ordered``
    has a ``> 0`` CHECK on new rows, so a zero or negative candidate would only fail
    deeper in with a message about a constraint rather than about the template.
    """
    for candidate in (requested, template.default_quantity, source.quantity_ordered):
        if candidate is None:
            continue
        value = float(candidate)
        if value > 0:
            return value

    raise HTTPException(
        status_code=422,
        detail=(
            f"Template '{template.name}' has no quantity to run: it carries no default quantity and "
            f"work order {source.work_order_number} has none either. Enter a quantity for this run."
        ),
    )


def summary_for_one(db: Session, template: WorkOrderTemplate, company_id: int) -> TemplatePlanSummary:
    """The single-template convenience over :func:`plan_summaries_for`.

    Routed through the batched implementation rather than given its own queries, so a
    detail page and a list row can never report a template differently.
    """
    return plan_summaries_for(db, [template], company_id).get(
        template.id,
        # Fail CLOSED. ``TemplatePlanSummary()`` defaults to ``available=True`` with
        # zero counts, so a fallback that used it would present an unresolvable
        # template as usable and then 409 on the click. Unreachable today --
        # ``plan_summaries_for`` writes an entry per template -- but the safe answer
        # to "I could not resolve this" is the one that disables the button.
        TemplatePlanSummary(available=False, unavailable_reason=UNAVAILABLE_SOURCE_MISSING),
    )


__all__ = [
    "UNAVAILABLE_SOURCE_MISSING",
    "TemplatePlanSummary",
    "TemplateUseResult",
    "create_template",
    "delete_template",
    "live_templates_query",
    "templates_pointing_at_work_order",
    "plan_summaries_for",
    "resolve_catalogable_work_order",
    "resolve_source_work_order",
    "summary_for_one",
    "update_template",
    "use_template",
    "_live_template_or_404",
]
