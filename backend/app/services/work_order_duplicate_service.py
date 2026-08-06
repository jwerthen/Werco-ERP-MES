"""Duplicate a work order — re-run the same job plan without re-entering it.

The motivating case is a laser nest package: 40+ nests confirmed once through the
AI import wizard, then run again next month. Re-uploading the package means
re-paying the extraction cost and re-confirming every row, so this copies the
PLAN (header, operations, nests, material ties) onto a fresh DRAFT work order and
leaves the PRODUCTION RECORD behind.

The rule that decides every field
---------------------------------
**Instructions carry; the production record does not.** Anything that describes
what to make and how to make it is copied. Anything that records what actually
happened — quantities produced or scrapped, actual hours and cost, actual/started
timestamps, who released or completed it, lot and serial numbers, the last kiosk
report, consumed material — is left at its zero/NULL default. Copying any of it
would fabricate history on a job that has not run, which an AS9100D reader would
read as a real record.

Four consequences of that rule are worth stating outright, because they are
decisions rather than omissions:

``parent_work_order_id`` is NOT carried.
    The duplicate is an INDEPENDENT work order. Re-attaching it to the source's
    assembly parent would add a second laser child against demand the first child
    already satisfied — the parent would then be waiting on two children to make
    one set of parts, and its completion rollup would count both. A duplicate of a
    child laser WO is therefore a standalone job; if the shop genuinely needs a
    second child under the same assembly, that is a nest import against the parent,
    not a duplicate.

``must_ship_by`` is NOT carried.
    It is the promise date of the ORIGINAL order and it takes precedence over
    ``due_date`` in the OTD/OTIF metric (``docs/LEAN_ROADMAP.md``). Carrying it
    forward would silently override the ``due_date`` the caller just supplied and
    score the new job against a promise nobody made for it. The duplicate starts
    with no must-ship date; set one on the work order if it has one.

``scheduled_start`` / ``scheduled_end`` are NOT carried (header or operation).
    They are ``SchedulingService`` OUTPUT for the source job's dates. Release runs
    scheduling again for the new work order, so copying them would only put stale
    slots on the board in the meantime.

``run_order`` is NOT carried (operation).
    Same class of thing, one layer down: it is a MANAGER'S DISPATCH RANKING for one
    machine's board (``dispatch_service``), not part of the plan. A 40-nest duplicate
    that arrived with its ranks pre-filled would, at release, drop 40 pre-ordered
    operations into the ordering set of a laser that already has queued work and
    displace the sequence the manager set for it. Advisory rather than gating, so this
    misplaces work rather than blocking it — but it is still the duplicate deciding
    something only the manager gets to decide. The new operations start unranked and
    the manager ranks them on the board like any other new work.

What the duplicate re-derives instead of copying
------------------------------------------------
Two things are recomputed rather than carried, because copying them would be copying a
number that was only ever true at the SOURCE's quantity:

*Process-sheet step snapshots* (``wo_operation_steps``) are RE-SNAPSHOTTED from each
sheet family's currently-RELEASED revision, exactly as ``create_work_order`` does — see
``_resnapshot_process_sheet_steps``. Copying the source's snapshot rows would freeze a
revision that may since have been superseded; copying nothing (the pre-fix behavior)
silently disarmed the operation-completion gate on a job whose whole premise is "same
plan as last time".

*Quantity-derived plan numbers* (operation ``run_time_hours`` and ``component_quantity``,
header ``estimated_hours`` / ``estimated_cost``) are SCALED — see
``_scale_quantity_derived_plan``. They are stored pre-multiplied by the ordered
quantity at creation, so an unscaled copy claims the source's hours — and the source's
whole-job component demand — at the duplicate's quantity.

The laser-nest document reference
---------------------------------
Nest ``document_id`` IS carried, and no new ``Document`` row or blob is created —
the duplicate points at the SOURCE work order's drawing. That is verified safe for
reading: ``api/endpoints/laser_nests.get_laser_nest_document`` resolves the PDF via
``nest.document_id`` and filters the ``Document`` by ``company_id`` only, never by
work order, so the operator preview works on the duplicate exactly as on the source.

**The Document row still belongs to the source work order.** Deleting the source's
drawing (or the source work order, if that ever cascades to its documents) breaks
the duplicate's preview. Copying the blob was rejected as the alternative: it
doubles storage per duplicate for a byte-identical PDF and creates a second
document number for one drawing, which is worse for traceability than a shared
reference.

What the duplicate REFUSES, and what it merely skips
----------------------------------------------------
The governing rule: **a duplicate must never mint a work order the create path would
have rejected.** One button is not a licence to route around a gate a planner would
have hit by hand. Two conditions therefore fail the whole call with a 409:

* the source's produced part has since been SOFT-DELETED
  (``_assert_source_part_available``) — a retired part must not go back into
  production in one click;
* an operation's process-sheet FAMILY has no released revision
  (``ProcessSheetUnavailableError``, code ``PROCESS_SHEET_UNAVAILABLE``) — byte-identical
  to what ``create_work_order`` raises for the same condition.

Everything else that cannot be copied faithfully is SKIPPED rather than guessed at, and
every skip is both recorded in the work order's audit ``extra_data`` (invariant 2 — an
omission belongs on the chain, not in a log line) and RETURNED to the caller on
``DuplicateResult`` so the endpoint can put it in front of the planner. A skip the
planner never sees is the failure mode that matters here: they release the job believing
it carries its material demand, no shortage shows, and stock is never deducted.

**That rule is structural, not a convention**, in two places:

* Each skip is built AS ITS RESPONSE SCHEMA (``WorkOrderDuplicateSkippedOperation`` /
  ``WorkOrderDuplicateSkippedAllocation``), here, inside the caller's transaction —
  never as a hand-rolled dict validated after the commit. A mistyped key is then a
  ``ValidationError`` that rolls the whole duplicate back, not a 500 on a work order
  that already exists and whose skips nobody will ever see. One definition also means
  the audit ``extra_data`` shape and the response shape cannot drift apart: the audit
  rows are ``model_dump()`` of the very objects the endpoint returns.
* An omission with NO channel to reach the planner through RAISES rather than logging.
  See ``_copy_laser_nests``: a nest whose source operation was not copied would reach
  neither list, so it is a hard failure instead of a dropped nest.

Tenancy, audit, atomicity
-------------------------
Every read here is company-scoped through ``tenant_query`` (invariant 1) — the
caller resolves the source work order the same way. Nothing commits: the work
order, its operations, their process-sheet step snapshots, the nest package, the
nests, the material ties and every ``audit_log`` row are flushed into ONE unit of
work that the CALLER commits, so a partial duplicate (a header with no nests, or
operations with no snapshot steps) cannot survive a failure mid-copy.
``AuditService.log`` only flushes, which is what makes that hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.process_sheet import WOOperationStep
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.schemas.work_order import WorkOrderDuplicateSkippedAllocation, WorkOrderDuplicateSkippedOperation
from app.services import process_sheet_service
from app.services.audit_service import AuditService

# Both imported rather than reimplemented, so a duplicated work order can never
# disagree with the paths that created the thing it is copying:
#   _recompute_child_quantity_ordered — the ONE definition of a laser work order's
#     ordered quantity (sum of its non-deleted planned runs).
#   _uom_value — the unit snapshot every other tie-creating path takes.
from app.services.laser_nest_service import _recompute_child_quantity_ordered, _uom_value

# No module logger, deliberately. Every omission this service can produce has a channel
# that reaches the planner (the two ``skipped_*`` lists) and the audit chain; anything
# that has neither raises. A log line is exactly the outcome those rules exist to
# prevent, so there is nothing here for one to say.


@dataclass
class DuplicateResult:
    """What one duplicate produced, for the caller's audit/telemetry needs.

    The two ``skipped_*`` lists are part of the CONTRACT, not debug output: they are
    written to the work order's audit ``extra_data`` AND returned to the planner on the
    endpoint's response envelope. See the module docstring on refusals vs skips.

    They are typed as the RESPONSE SCHEMAS, not as ``list[dict]``, so the endpoint hands
    them straight to ``WorkOrderDuplicateResponse`` and the audit rows are their
    ``model_dump()``. That is what keeps a malformed skip from surviving to a 500 after
    the commit — see the module docstring.
    """

    work_order: WorkOrder
    operation_count: int = 0
    nest_count: int = 0
    allocation_count: int = 0
    package_id: Optional[int] = None
    # Ties on the source that were deliberately NOT copied, with the reason. Recorded
    # on the work order's audit row so an omission is on the chain rather than silent.
    skipped_allocations: list[WorkOrderDuplicateSkippedAllocation] = field(default_factory=list)
    # Source operations that were deliberately NOT copied, same channel, same reason.
    skipped_operations: list[WorkOrderDuplicateSkippedOperation] = field(default_factory=list)
    # Which sheet families resolved to which released revisions at snapshot time — the
    # same summary shape ``create_work_order`` stamps on its own audit row.
    process_sheet_snapshot: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class _LaserNestBacking:
    """Which SOURCE operations back a laser nest, and which back only a dead one.

    Read once, up front, because two different decisions need it and they need it at
    different points in the copy:

    ``nest_backed_operation_ids``
        Every source operation with a nest row, live or soft-deleted. A tie on one of
        these plans against NEST RUNS, never against the work-order quantity — see
        ``_recomputed_qty_planned``, where getting that wrong is a ~40x planning error.

    ``deleted_only_operation_ids``
        Operations whose only nest is soft-deleted. ``soft_delete_laser_nest`` parks
        such an operation ON_HOLD *without* cancelling its material tie, so the source
        row is inert but still present. Copying it resets it to PENDING, and a laser WO
        promotes EVERY pending operation to READY at release
        (``work_order_state_service.release_first_ready_operation``) — so the duplicate
        would push a nest task with no nest, no CNC number and no drawing onto the kiosk
        queue. These operations are skipped entirely.

    ``uq_laser_nests_operation`` is unconditional on ``work_order_operation_id``, so an
    operation has at most one nest row ever and the two sets can only differ by live
    nests. The "no live nest" test is still written out rather than assumed, so the sets
    stay correct if that constraint is ever relaxed.
    """

    nest_backed_operation_ids: frozenset[int]
    deleted_only_operation_ids: frozenset[int]


def duplicate_work_order(
    db: Session,
    *,
    source: WorkOrder,
    quantity_ordered: float,
    due_date: Optional[date],
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> DuplicateResult:
    """Copy ``source``'s plan onto a new DRAFT work order in the same company.

    ``source`` must already be resolved company-scoped and non-deleted by the caller
    (the endpoint answers a miss with 404). Everything this function reads is
    re-scoped to ``company_id`` anyway — a duplicate that pulled another tenant's
    operations would be a security defect, not a copy bug.

    Raises ``HTTPException(409)`` when the source's part has been retired, and
    ``ProcessSheetUnavailableError`` (409, ``PROCESS_SHEET_UNAVAILABLE``) when a sheet
    family has no released revision — both BEFORE anything the caller would have to
    unwind, and both matching what the create path does. See the module docstring.

    Flushes but never commits. Wrap the call in ``atomic_transaction(db)``.
    """
    # Local import: ``generate_work_order_number`` lives in the work-orders ENDPOINT
    # module, which imports this service at load time. Importing it at module scope
    # would close the cycle. Same precedent as ``laser_nest_service._create_nest_document``
    # importing ``documents.generate_document_number``.
    from app.api.endpoints.work_orders import generate_work_order_number

    # Refusal gate first, before a single row is written: a duplicate must not be able
    # to put a retired part back into production.
    _assert_source_part_available(db, source=source, company_id=company_id)

    nest_backing = _laser_nest_backing(db, source=source, company_id=company_id)

    new_work_order = _copy_header(
        db,
        source=source,
        quantity_ordered=quantity_ordered,
        due_date=due_date,
        company_id=company_id,
        user_id=user_id,
        work_order_number=generate_work_order_number(db, company_id),
    )

    operation_map, skipped_operations = _copy_operations(
        db,
        source=source,
        new_work_order=new_work_order,
        company_id=company_id,
        skip_operation_ids=nest_backing.deleted_only_operation_ids,
    )

    # Re-snapshot the traveler BEFORE the nests and ties, so the 409 for an unreleasable
    # sheet family lands as early as the create path's does.
    process_sheet_snapshot = _resnapshot_process_sheet_steps(db, operation_map=operation_map, company_id=company_id)

    package, nests, planned_runs_by_source_operation = _copy_laser_nests(
        db,
        source=source,
        new_work_order=new_work_order,
        operation_map=operation_map,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
    )

    # ------------------------------------------------------------------
    # A nest-bearing work order's quantity is DERIVED, not chosen.
    # ------------------------------------------------------------------
    # `_recompute_child_quantity_ordered` defines `quantity_ordered` as the sum of
    # the WO's non-deleted `planned_runs`, and every nest mutation path in
    # `laser_nest_service` re-asserts it. Honouring a caller-supplied quantity here
    # would leave the duplicate as the ONE laser work order in the system where that
    # is false — until the next nest edit silently "corrected" it out from under the
    # planner. Nests carry their runs across verbatim, so the sum is already the
    # right answer; the requested quantity is deliberately ignored in this case, and
    # the endpoint reports the derived value back so the UI never shows a number the
    # server did not store.
    effective_quantity = float(quantity_ordered)
    if nests:
        effective_quantity = _recompute_child_quantity_ordered(db, new_work_order, company_id)

    # ONE quantity ratio for the whole duplicate: the plan numbers below and every
    # non-nest-backed tie in ``_copy_material_allocations`` scale by the same factor, or
    # the duplicate would plan hours for one quantity and material for another.
    quantity_ratio = _scale_quantity_derived_plan(
        new_work_order,
        operation_map,
        source_quantity=float(source.quantity_ordered or 0),
        new_quantity=effective_quantity,
        quantity_is_derived=bool(nests),
    )

    allocations, skipped = _copy_material_allocations(
        db,
        source=source,
        new_work_order=new_work_order,
        operation_map=operation_map,
        nest_backed_operation_ids=nest_backing.nest_backed_operation_ids,
        planned_runs_by_source_operation=planned_runs_by_source_operation,
        quantity_ratio=quantity_ratio,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
    )

    result = DuplicateResult(
        work_order=new_work_order,
        operation_count=len(operation_map),
        nest_count=len(nests),
        allocation_count=len(allocations),
        package_id=package.id if package is not None else None,
        skipped_allocations=skipped,
        skipped_operations=skipped_operations,
        process_sheet_snapshot=process_sheet_snapshot,
    )

    # Lineage on the tamper-evident chain (invariant 2). The source work order's id AND
    # number are recorded so an auditor reading the new job can see it was DERIVED from
    # another one — the row on the new WO is the only place that fact exists, since the
    # duplicate carries no FK back to its source.
    audit.log_create(
        resource_type="work_order",
        resource_id=new_work_order.id,
        resource_identifier=new_work_order.work_order_number,
        new_values=new_work_order,
        description=(f"Duplicated work order {source.work_order_number} as {new_work_order.work_order_number}"),
        extra_data={
            "source": "work_order_duplicate",
            "source_work_order_id": source.id,
            "source_work_order_number": source.work_order_number,
            "part_id": new_work_order.part_id,
            "quantity": float(new_work_order.quantity_ordered),
            # Recorded only when the two differ, i.e. a nest-bearing WO whose
            # quantity was derived from its runs instead of taken from the caller.
            **(
                {"requested_quantity": float(quantity_ordered)}
                if float(new_work_order.quantity_ordered) != float(quantity_ordered)
                else {}
            ),
            "operation_count": result.operation_count,
            "laser_nest_count": result.nest_count,
            "laser_nest_package_id": result.package_id,
            "material_allocation_count": result.allocation_count,
            # ``model_dump()`` of the very objects the endpoint returns — one definition,
            # so the chain and the response can never describe a skip differently.
            "skipped_material_allocations": [entry.model_dump() for entry in result.skipped_allocations],
            # Source operations deliberately not copied (a soft-deleted laser nest), on
            # the chain for the same reason the tie skips are: an omission a planner can
            # act on must not exist only in a log line.
            "skipped_operations": [entry.model_dump() for entry in result.skipped_operations],
            # Same key and same shape ``create_work_order`` writes: which sheet families
            # resolved to which released revisions for this job's traveler. A duplicate
            # is snapshotted from the CURRENTLY-released revision, so this is how an
            # auditor sees that the duplicate's traveler may differ from the source's.
            "process_sheet_snapshot": result.process_sheet_snapshot,
            # The factor the copied plan numbers were multiplied by (1.0 = unscaled).
            "quantity_ratio": quantity_ratio,
        },
    )
    return result


def _assert_source_part_available(db: Session, *, source: WorkOrder, company_id: int) -> None:
    """Refuse (409) to duplicate a work order whose produced part has been retired.

    ``POST /work-orders`` has the same gap today — it resolves the part by id without a
    soft-delete predicate — so this is not fixing a regression, it is refusing to make
    the gap one click wide. Duplicate is the path where it matters: creating a work order
    by hand at least means typing the retired part number in, while duplicating a
    completed job puts a retired part back into production with a button.

    It is also the same rule the tie half of this service already applies. ``POST
    /work-orders/{id}/material-allocations`` refuses a deleted part outright, so
    ``_copy_material_allocations`` skips ties whose part is gone; refusing the tie's part
    while blindly copying the PRODUCED part would be the service contradicting itself.

    Part-less sources (standalone laser-cutting nest WOs) are a no-op here: the
    ``ck_work_orders_part_required_unless_laser`` CHECK is what makes ``part_id IS NULL``
    legal for them, and there is no part to retire.
    """
    if source.part_id is None:
        return
    part = tenant_query(db, Part, company_id).filter(Part.id == source.part_id).first()
    if part is not None and not getattr(part, "is_deleted", False):
        return

    identifier = part.part_number if part is not None else f"id {source.part_id}"
    raise HTTPException(
        status_code=409,
        detail=(
            f"Cannot duplicate work order {source.work_order_number}: its part {identifier} has been deleted. "
            "Restore the part, or create a new work order against a current part."
        ),
    )


def _laser_nest_backing(db: Session, *, source: WorkOrder, company_id: int) -> _LaserNestBacking:
    """One read of the source's nest rows, live and soft-deleted. See ``_LaserNestBacking``.

    Nests are reached through their OPERATION (the join
    ``laser_nest_service._recompute_child_quantity_ordered`` uses), which scopes to this
    work order and drops nests with a NULL ``work_order_operation_id`` — those back no
    operation, so they inform no decision here. Because that is an INNER join on
    ``LaserNest.work_order_operation_id == WorkOrderOperation.id``, every id the loop
    below sees is non-NULL by construction and no NULL guard is written for it. Unlike
    every other nest read in this module, this one does NOT filter ``is_deleted``: a dead
    nest is precisely what it is looking for.
    """
    rows = (
        db.query(LaserNest.work_order_operation_id, LaserNest.is_deleted)
        .join(WorkOrderOperation, LaserNest.work_order_operation_id == WorkOrderOperation.id)
        .filter(
            LaserNest.company_id == company_id,
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == source.id,
        )
        .all()
    )

    nest_backed: set[int] = set()
    with_live_nest: set[int] = set()
    for operation_id, is_deleted in rows:
        nest_backed.add(operation_id)
        if not is_deleted:
            with_live_nest.add(operation_id)

    return _LaserNestBacking(
        nest_backed_operation_ids=frozenset(nest_backed),
        deleted_only_operation_ids=frozenset(nest_backed - with_live_nest),
    )


def _resnapshot_process_sheet_steps(
    db: Session,
    *,
    operation_map: dict[int, WorkOrderOperation],
    company_id: int,
) -> list[dict]:
    """Re-snapshot each copied operation's process-sheet steps. Returns the audit summary.

    ``wo_operation_steps`` is the immutable per-operation snapshot of a released process
    sheet, and it is the input to the operation-completion gate
    (``process_sheet_service.missing_required_steps``). That gate is documented to return
    ``[]`` — complete freely — for an operation with ZERO snapshot steps, which is the
    correct answer for work that predates process sheets and the WRONG answer for a
    duplicate: a source operation that refuses completion without a conforming,
    gauge-attributed MEASUREMENT record would, on a duplicate carrying no steps, complete
    with no measurement, no SPC point, no gauge attribution, no OOT->NCR path and nothing
    to pre-fill the AS9102 FAI — on a job whose entire premise is "same plan as last
    time". Note that the deliberate escape hatch, force-complete, stamps the very steps it
    bypasses onto its audit row; a duplicate with no steps bypassed the same gate with no
    record at all.

    **Re-snapshot, never copy the source's snapshot rows.** ``WorkOrderOperation`` carries
    no ``process_sheet_id``, so the sheet FAMILY is resolved through the source
    operation's own ``wo_operation_steps.source_sheet_id`` — the released revision that
    was snapshotted onto the source. Handing that id to
    ``process_sheet_service.snapshot_steps_for_work_order`` is exactly what the routing
    hands it (a sheet row to resolve a family from), so the duplicate lands on whatever
    revision is released NOW. That is the settled snapshot semantics (scope doc
    2026-07-06: releasing Rev B flows to future WOs without re-attaching routings) and a
    duplicate is a future WO. Copying the source's rows verbatim would instead freeze a
    revision that may since have been superseded or obsoleted.

    The seam is reused rather than reimplemented, which is what buys the refusal:
    ``snapshot_steps_for_work_order`` raises ``ProcessSheetUnavailableError`` (409,
    ``PROCESS_SHEET_UNAVAILABLE``) for a family with no released revision, so the
    duplicate refuses exactly where ``create_work_order`` refuses. It also does its own
    flush for operation PKs, and only flushes — the caller's unit of work stays atomic.

    One ``source_sheet_id`` per source operation: the snapshot writes every step of ONE
    resolved sheet onto an operation, so the lowest ``(sequence, id)`` step's sheet id
    identifies the family unambiguously. Operations with no snapshot steps contribute no
    pair and are left with none, which is correct — they had no sheet attached.
    """
    if not operation_map:
        return []

    steps = (
        tenant_query(db, WOOperationStep, company_id)
        .filter(WOOperationStep.work_order_operation_id.in_(list(operation_map)))
        .order_by(WOOperationStep.work_order_operation_id, WOOperationStep.sequence, WOOperationStep.id)
        .all()
    )
    if not steps:
        return []

    sheet_id_by_source_operation: dict[int, int] = {}
    for step in steps:
        sheet_id_by_source_operation.setdefault(step.work_order_operation_id, step.source_sheet_id)

    pairs: list[tuple[WorkOrderOperation, Optional[int]]] = [
        (operation_map[source_operation_id], sheet_id)
        for source_operation_id, sheet_id in sheet_id_by_source_operation.items()
    ]
    return process_sheet_service.snapshot_steps_for_work_order(db, company_id, pairs)


def _scale_quantity_derived_plan(
    new_work_order: WorkOrder,
    operation_map: dict[int, WorkOrderOperation],
    *,
    source_quantity: float,
    new_quantity: float,
    quantity_is_derived: bool,
) -> float:
    """Scale the copied plan numbers to the duplicate's quantity. Returns the ratio used.

    Three columns are stored PRE-MULTIPLIED by the ordered quantity, so copying them
    verbatim states the SOURCE's job at the duplicate's quantity:

    ``WorkOrderOperation.run_time_hours``
        ``create_routing_operations_for_work_order`` writes ``run_hours_per_unit x
        quantity``. Duplicate a 100-piece job at 10 and every operation still claims the
        100-piece run hours, which is not cosmetic: scheduling sizes capacity and slots
        from it, the dispatch board shows it, and ``completion_cost_service`` reads
        ``run_time_hours`` FIRST and only falls back to ``run_time_per_piece x
        ordered_qty`` when it is zero.

    ``WorkOrderOperation.component_quantity``
        The WHOLE-JOB total for a component operation — ``qty_per_assembly x
        work_order.quantity_ordered``, written that way by BOTH writers
        (``work_orders._create_assembly_routing_operations`` and
        ``work_orders._reconcile_operation_component_quantities``) and relied on as such
        by ``completion_inventory_service._routing_backflush_demand``. Duplicate a
        100-piece assembly at 10 and the component operations still carry the 100-piece
        total. Its readers are live, not archival: ``work_order_state_service
        .operation_target_quantity`` returns ``component_quantity`` IN PREFERENCE to the
        work-order quantity, so the kiosk, the dispatch board, the wallboard, the
        completion caps and the op-satisfied rollup would all target the source's total;
        and the backflush leg recovers a per-unit rate as ``stated / ordered``, which on
        an unscaled copy is off by ``source_qty / new_qty``.

    Header ``estimated_hours`` / ``estimated_cost``
        Stale the same way — the cost estimator integrates the operations at the ordered
        quantity.

    **Scaled, not recomputed.** Two recomputations were available and both are worse:

    * from ``run_time_per_piece`` — the routing copy never writes that column at all (it
      writes only ``run_time_hours``), so it is 0.0 on every routing-generated operation
      and recomputing would zero out the whole plan;
    * for ``component_quantity``, by calling ``work_orders
      ._reconcile_operation_component_quantities`` on the new work order, which is what
      ``create_work_order`` does. It only heals when the produced part still has an ACTIVE
      BOM that still contains the component — a component dropped from the BOM since the
      source ran keeps the stale whole-job total, silently — and none of the readers named
      above run it, so nothing else would ever correct it. It would also re-derive the
      plan from today's BOM rather than reproduce the source's, which contradicts this
      service's rule that the instructions CARRY. Multiplying by ``new_quantity /
      source_quantity`` needs no BOM, cannot partially apply, and reproduces
      ``qty_per_assembly x new_quantity`` (and ``run_hours_per_unit x new_quantity``)
      exactly, because that is what the copied value was divided into.

    ``setup_time_hours`` is deliberately NOT scaled — setup is per-job, and creation does
    not multiply it by quantity either.

    Two cases return 1.0 and touch nothing:

    * ``quantity_is_derived`` — a nest-bearing duplicate. Its quantity is not the
      caller's to choose; it is the sum of the nests' planned runs, and the nests carry
      their runs across VERBATIM, so per-run the plan is unchanged and the honest factor
      is exactly 1.0. Computing ``new / source`` here instead would let drift between the
      source's stored header quantity and its own runs sum leak in as a spurious rescale
      of a plan nothing about has changed. (Nest operations are born with
      ``run_time_hours = 0.0`` anyway, so this guard is about being right on purpose
      rather than by arithmetic accident.)
    * a non-positive ``source_quantity`` — legacy data that predates the ``> 0`` CHECK.
      Copy the plan unscaled rather than divide by zero or refuse a job the planner is
      trying to re-run. Same posture as the tie ratio this value now also serves.

    **Why scaling ``component_quantity`` cannot corrupt a nest operation.** On a nest
    operation the column is NOT a quantity-derived total at all — ``laser_nest_service``
    writes ``planned_runs`` into it (at import, at manual create, and on every edit via
    ``sync_laser_nest_to_operation``), and runs carry across verbatim, so scaling it by
    anything but 1.0 would be a corruption. It is unreachable rather than merely
    unlikely, on the ``quantity_is_derived`` guard above plus one structural fact:

    * ``quantity_is_derived`` is ``bool(nests)`` — the source's LIVE nests — and this
      function returns 1.0 before its first ``setattr``, so if even one live nest exists
      NOTHING on the work order is scaled;
    * and if NO live nest exists, every nest-backed operation is in
      ``deleted_only_operation_ids`` (``nest_backed - with_live_nest``), so
      ``_copy_operations`` skips it and it never enters ``operation_map`` — the loop below
      cannot reach it.

    So the two sets — operations whose ``component_quantity`` means runs, and operations
    this loop touches — are disjoint by construction, not by coincidence.
    """
    if quantity_is_derived or source_quantity <= 0:
        return 1.0
    ratio = float(new_quantity) / source_quantity
    if ratio == 1.0:
        return ratio

    new_work_order.estimated_hours = float(new_work_order.estimated_hours or 0.0) * ratio
    new_work_order.estimated_cost = float(new_work_order.estimated_cost or 0.0) * ratio
    for operation in operation_map.values():
        operation.run_time_hours = float(operation.run_time_hours or 0.0) * ratio
        # Whole-job component total, not a per-unit rate — see the docstring. Only the
        # component operations carry one; the rest are 0.0 and stay 0.0.
        operation.component_quantity = float(operation.component_quantity or 0.0) * ratio
    return ratio


def _copy_header(
    db: Session,
    *,
    source: WorkOrder,
    quantity_ordered: float,
    due_date: Optional[date],
    company_id: int,
    user_id: int,
    work_order_number: str,
) -> WorkOrder:
    """The new work order row. Every omitted column is the production record.

    Deliberately NOT set (left at the column default / NULL): ``quantity_complete``,
    ``quantity_scrapped``, ``scrap_reason``, ``scrap_reason_code_id``, ``actual_start``,
    ``actual_end``, ``actual_hours``, ``actual_cost``, ``lot_number``,
    ``serial_numbers``, ``released_by``, ``released_at``, ``current_operation_id``,
    ``scheduled_start``, ``scheduled_end``, ``version``, ``parent_work_order_id``,
    ``must_ship_by`` — see the module docstring for the three that are judgement calls.

    ``part_id`` and ``work_order_type`` are copied as a PAIR, which is what keeps
    ``ck_work_orders_part_required_unless_laser`` satisfied by construction: a
    part-less source can only be a ``laser_cutting`` work order, and the duplicate
    stays one.
    """
    new_work_order = WorkOrder(
        company_id=company_id,
        work_order_number=work_order_number,
        part_id=source.part_id,
        work_order_type=source.work_order_type,
        quantity_ordered=float(quantity_ordered),
        due_date=due_date,
        status=WorkOrderStatus.DRAFT,
        priority=source.priority,
        customer_name=source.customer_name,
        customer_po=source.customer_po,
        po_line_item=source.po_line_item,
        po_date=source.po_date,
        sales_order_id=source.sales_order_id,
        notes=source.notes,
        special_instructions=source.special_instructions,
        # Estimates are part of the PLAN (what this job is expected to take), so they
        # carry; the actuals beside them are the record and do not. Both are stated at
        # the SOURCE's quantity, so ``_scale_quantity_derived_plan`` rescales them once
        # the duplicate's effective quantity is known.
        estimated_hours=source.estimated_hours or 0.0,
        estimated_cost=source.estimated_cost or 0.0,
        created_by=user_id,
    )
    db.add(new_work_order)
    db.flush()
    return new_work_order


def _copy_operations(
    db: Session,
    *,
    source: WorkOrder,
    new_work_order: WorkOrder,
    company_id: int,
    skip_operation_ids: frozenset[int],
) -> tuple[dict[int, WorkOrderOperation], list[WorkOrderDuplicateSkippedOperation]]:
    """One fresh operation per source operation. Returns ``({source_op_id: new_op}, skipped)``.

    ``WorkOrderOperation`` carries no ``SoftDeleteMixin`` (only ``TenantMixin``), so
    there is no ``is_deleted`` predicate to apply here — every operation row on the
    source is live by definition. That is exactly why ``skip_operation_ids`` has to be
    passed in: an operation whose LASER NEST was soft-deleted is a dead operation wearing
    a live row, and only the nest table knows it (see ``_LaserNestBacking``). Each skip is
    returned with its reason for the audit row and the caller's response.

    Carried (the instructions): ``sequence``, ``operation_number``, ``name``,
    ``description``, ``operation_group``, ``setup_instructions``, ``run_instructions``,
    ``setup_time_hours``, ``run_time_hours``, ``run_time_per_piece``, ``work_center_id``,
    ``component_part_id``, ``component_quantity``, ``requires_inspection``,
    ``inspection_type``.

    ``run_time_hours`` and ``component_quantity`` are carried here but rescaled
    afterwards by ``_scale_quantity_derived_plan`` — both are stored pre-multiplied by
    the ordered quantity, so both are plan numbers stated at the SOURCE's quantity. They
    are copied verbatim at this point because the duplicate's EFFECTIVE quantity is not
    known until the nests have been copied (a nest-bearing work order derives it from
    their runs), so the one ratio is applied in one place, once, afterwards.

    Reset (the record): ``status`` -> ``PENDING``, every ``quantity_*`` (including
    ``quantity_reworked``) -> 0, ``actual_setup_hours`` / ``actual_run_hours`` -> 0, and
    ``actual_start`` / ``actual_end`` / ``started_by`` / ``completed_by`` /
    ``last_reported_*`` / ``inspection_complete`` / ``scrap_reason`` /
    ``scrap_reason_code_id`` / ``scheduled_start`` / ``scheduled_end`` / ``version``
    left at their defaults.

    Reset (scheduling OUTPUT, not the plan): ``run_order``. See the module docstring —
    it is the manager's dispatch ranking for one machine's board, the same class of thing
    as ``scheduled_start`` / ``scheduled_end``, and carrying it would let a duplicate
    displace the run order the manager set for work already queued at that machine.

    PENDING is right even for a laser dispatch pool whose source nest ops were born
    READY: the duplicate is DRAFT, and ``release_first_ready_operation`` promotes EVERY
    pending nest op to READY at release for a laser work order (only the lowest-sequence
    one for a routed job). Birthing them READY here would put un-released work on the
    kiosk queue.
    """
    source_operations = (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.work_order_id == source.id)
        .order_by(WorkOrderOperation.sequence, WorkOrderOperation.id)
        .all()
    )

    operation_map: dict[int, WorkOrderOperation] = {}
    skipped_operations: list[WorkOrderDuplicateSkippedOperation] = []
    for operation in source_operations:
        if operation.id in skip_operation_ids:
            # The nest this operation exists to run was soft-deleted. Copying it would
            # put a nest task with no nest on the kiosk queue at release.
            #
            # Built as the RESPONSE SCHEMA, not a dict: this runs inside the caller's
            # transaction, so a malformed skip fails here and rolls the duplicate back,
            # instead of 500-ing on the response of a work order that already committed.
            skipped_operations.append(
                WorkOrderDuplicateSkippedOperation(
                    source_operation_id=operation.id,
                    operation_number=operation.operation_number,
                    sequence=operation.sequence,
                    reason="laser_nest_deleted",
                )
            )
            continue
        new_operation = WorkOrderOperation(
            company_id=company_id,
            work_order_id=new_work_order.id,
            work_center_id=operation.work_center_id,
            component_part_id=operation.component_part_id,
            component_quantity=operation.component_quantity,
            sequence=operation.sequence,
            operation_number=operation.operation_number,
            name=operation.name,
            description=operation.description,
            operation_group=operation.operation_group,
            setup_instructions=operation.setup_instructions,
            run_instructions=operation.run_instructions,
            setup_time_hours=operation.setup_time_hours or 0.0,
            run_time_hours=operation.run_time_hours or 0.0,
            run_time_per_piece=operation.run_time_per_piece or 0.0,
            actual_setup_hours=0.0,
            actual_run_hours=0.0,
            status=OperationStatus.PENDING,
            quantity_complete=0.0,
            quantity_scrapped=0.0,
            quantity_reworked=0.0,
            requires_inspection=operation.requires_inspection,
            inspection_type=operation.inspection_type,
            inspection_complete=False,
        )
        db.add(new_operation)
        operation_map[operation.id] = new_operation

    # Flush before the snapshots, nests and ties are built: all three scope themselves
    # to an operation id, and the session runs with autoflush off.
    db.flush()
    return operation_map, skipped_operations


def _copy_laser_nests(
    db: Session,
    *,
    source: WorkOrder,
    new_work_order: WorkOrder,
    operation_map: dict[int, WorkOrderOperation],
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> tuple[Optional[LaserNestPackage], list[LaserNest], dict[int, int]]:
    """Copy the source's live nests onto ONE new package on the duplicate.

    Returns ``(package, new_nests, {source_op_id: planned_runs})`` — the last map is
    the run basis the material-tie copy scales an operation-scoped ``qty_planned`` by.

    Nests are reached through their OPERATION (the same join
    ``laser_nest_service._recompute_child_quantity_ordered`` uses), which does three
    things at once: it scopes to this work order, it drops soft-deleted nests
    (``is_deleted == False`` — invariant 3), and it drops any nest whose
    ``work_order_operation_id`` is NULL. That last one is deliberate: a nest with no
    operation has nothing to attach to on the duplicate and would be invisible anyway
    (``WorkOrderResponse`` surfaces nests through operations).

    ONE package for all of them, regardless of how many the source had. The two unique
    constraints both hold by construction: ``uq_laser_nests_operation`` because every
    new nest gets a freshly-created operation, and ``uq_laser_nests_package_file``
    (``package_id``, ``nest_name``, ``cnc_file_name``) because at most one package per
    work order can hold IMPORTED nests — an import wipes every prior package on the
    child WO — and manual nests carry ``cnc_file_name IS NULL``, which both Postgres
    and SQLite treat as distinct in a unique constraint.

    A live nest whose source operation is somehow absent from ``operation_map`` RAISES
    (``RuntimeError``) rather than being logged and dropped. It is unreachable today —
    the only operations ``_copy_operations`` skips are ones with no live nest — but it is
    the single omission in this service with no channel to the planner: there is no
    "skipped nest" list, so a dropped nest would reach neither the response envelope nor
    the audit ``extra_data``. Raising makes an unreachable case that later becomes
    reachable fail loudly instead of silently shipping a short laser job.

    ``source_path`` is deliberately NULL on the new package: the duplicate was not
    imported from a package directory, it was derived from another work order, and
    pointing at the source's (temporary) extract path would be a false provenance.
    That NULL does NOT make this package reusable by
    ``laser_nest_service.create_manual_laser_nest`` — its lookup matches on
    ``package_name == "Manual entry"`` AND ``source_path IS NULL``, and this package
    carries the SOURCE's package name (see ``_source_package_name``). A planner who
    later adds a nest by hand to the duplicate therefore gets a second, separate
    "Manual entry" package on the same work order, exactly as they would on any
    imported one. That is the existing behavior for imported packages, not a
    regression introduced here.
    """
    source_nests = (
        db.query(LaserNest)
        .join(WorkOrderOperation, LaserNest.work_order_operation_id == WorkOrderOperation.id)
        .filter(
            LaserNest.company_id == company_id,
            LaserNest.is_deleted == False,  # noqa: E712
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == source.id,
        )
        .order_by(LaserNest.id)
        .all()
    )
    if not source_nests:
        return None, [], {}

    package = LaserNestPackage(
        company_id=company_id,
        # Independent job: the duplicate's package hangs off the NEW work order only.
        parent_work_order_id=None,
        child_work_order_id=new_work_order.id,
        package_name=_source_package_name(db, source_nests, company_id, source),
        source_path=None,
        import_status="imported",
        created_by=user_id,
    )
    db.add(package)
    db.flush()

    new_nests: list[LaserNest] = []
    planned_runs_by_source_operation: dict[int, int] = {}
    for nest in source_nests:
        new_operation = operation_map.get(nest.work_order_operation_id)
        if new_operation is None:
            # Unreachable today: the only operations ``_copy_operations`` skips are those
            # with no LIVE nest, and the join above returns live nests only.
            #
            # It RAISES rather than logging-and-continuing because this is the one
            # omission in the whole service with no channel to the planner: there is no
            # "skipped nest" list on ``DuplicateResult``, so a dropped nest would reach
            # neither the response envelope nor the audit ``extra_data``, and the planner
            # would release a laser job silently short one nest. Failing the call rolls
            # the whole duplicate back (the caller's ``atomic_transaction``) and leaves a
            # 500 someone has to explain — which is the correct cost for an invariant
            # that was supposed to be impossible to violate.
            raise RuntimeError(
                f"Cannot duplicate work order {source.work_order_number}: live nest {nest.id} is backed by "
                f"source operation {nest.work_order_operation_id}, which was not copied. A nest that reaches "
                "neither the response nor the audit chain must not be silently dropped."
            )
        planned_runs_by_source_operation[nest.work_order_operation_id] = int(nest.planned_runs or 0)
        new_nest = LaserNest(
            company_id=company_id,
            package_id=package.id,
            work_order_operation_id=new_operation.id,
            nest_name=nest.nest_name,
            cnc_file_name=nest.cnc_file_name,
            cnc_file_path=nest.cnc_file_path,
            cnc_number=nest.cnc_number,
            # The REFERENCE, not the blob — see the module docstring.
            document_id=nest.document_id,
            planned_runs=nest.planned_runs,
            completed_runs=0,
            material=nest.material,
            thickness=nest.thickness,
            sheet_size=nest.sheet_size,
        )
        db.add(new_nest)
        new_nests.append(new_nest)

    # Flush so every nest has a real PK before the audit rows name it.
    db.flush()

    # One CREATE row per nest, in the same shape the package import writes
    # (``work_orders._run_laser_nest_import``), plus the source lineage.
    for new_nest in new_nests:
        audit.log_create(
            resource_type="laser_nest",
            resource_id=new_nest.id,
            resource_identifier=new_nest.cnc_number or new_nest.nest_name,
            new_values={
                "nest_name": new_nest.nest_name,
                "cnc_number": new_nest.cnc_number,
                "planned_runs": new_nest.planned_runs,
                "material": new_nest.material,
                "thickness": new_nest.thickness,
                "sheet_size": new_nest.sheet_size,
                "document_id": new_nest.document_id,
                "work_order_operation_id": new_nest.work_order_operation_id,
                "package_id": new_nest.package_id,
            },
            extra_data={
                "child_work_order_id": new_work_order.id,
                "source": "work_order_duplicate",
                "source_work_order_id": source.id,
                "source_work_order_number": source.work_order_number,
                # The drawing is SHARED with the source work order, not re-uploaded.
                "document_shared_with_source": new_nest.document_id is not None,
            },
        )

    return package, new_nests, planned_runs_by_source_operation


def _source_package_name(
    db: Session,
    source_nests: list[LaserNest],
    company_id: int,
    source: WorkOrder,
) -> str:
    """Name for the duplicate's single package: the source's earliest package name.

    Read off the NESTS' own ``package_id`` rather than off
    ``LaserNestPackage.child_work_order_id`` — the nests are the rows being copied, so
    their package is the one whose name describes them, whichever work order the
    package happens to be linked to. Tenant-scoped, with a derived fallback so a name
    always exists (``package_name`` is NOT NULL).
    """
    package_ids = {nest.package_id for nest in source_nests if nest.package_id is not None}
    if package_ids:
        package = (
            tenant_query(db, LaserNestPackage, company_id)
            .filter(LaserNestPackage.id.in_(package_ids))
            .order_by(LaserNestPackage.id)
            .first()
        )
        if package is not None and package.package_name:
            return package.package_name
    return f"Duplicated from {source.work_order_number}"


def _copy_material_allocations(
    db: Session,
    *,
    source: WorkOrder,
    new_work_order: WorkOrder,
    operation_map: dict[int, WorkOrderOperation],
    nest_backed_operation_ids: frozenset[int],
    planned_runs_by_source_operation: dict[int, int],
    quantity_ratio: float,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> tuple[list[WorkOrderMaterialAllocation], list[WorkOrderDuplicateSkippedAllocation]]:
    """Copy the OPEN material ties. Returns ``(new_allocations, skipped)``.

    Only ``status == OPEN`` ties are copied. CANCELLED/CLOSED rows are tombstones
    (``work_order_material.py``: status IS the tombstone, there is no ``is_deleted``
    here) — a tie the planner untied, or one a nest re-import superseded, is not part
    of the plan being re-run.

    Scope follows the source: an operation-scoped tie attaches to the CORRESPONDING new
    operation, a work-order-scoped tie (``work_order_operation_id IS NULL``) attaches to
    the new work order. The two are not interchangeable — a work-order-scoped tie with a
    ``qty_per_run`` is a 422 on the tie API — so a tie whose operation was somehow not
    copied is SKIPPED rather than silently re-scoped to the work order.

    Reset, and this is the load-bearing one: ``pinned_inventory_item_id`` /
    ``pinned_lot_number`` are ALWAYS cleared. A lot pin says "consume from THIS lot",
    and the lot the source job pinned was very likely consumed by the source job. The
    duplicate would then point at stock that no longer exists, or — worse — at a lot
    that has since been re-received under the same row and has nothing to do with this
    work. Unpinned means FIFO picks at consume time, which is the correct default for a
    job that has not started. ``qty_consumed`` resets to 0 and ``status`` to OPEN for the
    same reason: the ledger, not this row, is the record of what moved.

    ``qty_planned`` is re-derived for a nest-backed tie and SCALED for every other one, so
    a duplicate at a different quantity plans a different amount of material:

    * operation-scoped, nest-backed — RE-DERIVED as ``COALESCE(qty_per_run, 1.0) x
      planned_runs``, byte-identical to what
      ``laser_nest_service.create_nest_material_allocation`` computes when a nest tie is
      first created. This is the one branch that does not scale the source value, and it
      has the better basis for it: the runs came across verbatim and the nest-tie creator
      is the definition of what such a tie should hold. NEVER the work-order quantity —
      see ``_recomputed_qty_planned`` for why substituting it there is a ~40x error.
    * everything else (operation-scoped without a nest, and work-order-scoped) — the
      source's ``qty_planned`` scaled by ``quantity_ratio``, the one factor
      ``_scale_quantity_derived_plan`` also applied to the plan hours, so the duplicate
      cannot plan hours for one quantity and material for another. A same-quantity
      duplicate (the common case) multiplies by exactly 1.0 and reproduces the source
      value bit-for-bit.

      **``qty_planned`` is caller-supplied and INDEPENDENT of ``qty_per_run``**, which is
      why it is scaled rather than recomputed from the rate. ``MaterialAllocationCreate``
      requires ``qty_planned`` and leaves ``qty_per_run`` optional (defaulted to 1.0 on an
      operation-scoped tie when omitted — ``api/endpoints/work_order_materials``), so
      "500 lb of bar to OP20" is a perfectly ordinary tie with ``qty_per_run = 1.0`` and
      ``qty_planned = 500``. Recomputing it as ``qty_per_run x quantity_ordered`` — which
      this branch used to do — silently rewrote that to the work-order quantity, with no
      skip, nothing on the response and nothing on the chain to say the number changed.

    ``unit_of_measure`` is RE-SNAPSHOTTED from the part, not carried from the source
    tie. The model documents the column as a snapshot of ``Part.unit_of_measure`` *at
    tie time*, and this tie's time is now — a part restocked in a different unit since
    the source job would otherwise hand the new tie a label its own quantity is no
    longer expressed in, which is the one way a copied tie can quietly mis-state how
    much material a nest draws. Falls back to the source value only when the part row
    carries no unit at all.

    Three conditions SKIP a tie rather than guess at it — TWO of them producible. Each
    appends a ``WorkOrderDuplicateSkippedAllocation`` carrying the source allocation id,
    the part, the source operation and a machine-readable ``reason``; the list is recorded
    in the work order's audit ``extra_data`` AND returned to the caller, which is what
    puts the omission in front of the planner rather than only on the chain:

    ``part_not_available``
        The tie's part has since been SOFT-DELETED. ``POST
        /work-orders/{id}/material-allocations`` refuses a deleted part outright, so
        re-creating one here would mint a tie no planner could have made by hand.
    ``operation_not_copied``
        The tie's operation was not copied — in practice its laser nest was
        soft-deleted, so the operation was skipped (see ``_copy_operations``). Re-scoping
        the tie to the work order instead is not available: a work-order-scoped tie with
        a ``qty_per_run`` is a 422 on the tie API, so the two scopes are not
        interchangeable.
    ``nest_runs_unavailable`` — DEFENSIVE; NOT CURRENTLY PRODUCIBLE.
        The tie is operation-scoped and nest-backed, but no run count came across for it.
        No caller can see this today and clients should not be told to expect it: an
        operation in ``nest_backed_operation_ids`` with no live nest is by construction in
        ``deleted_only_operation_ids``, so ``_copy_operations`` skips it and its tie hits
        ``operation_not_copied`` first; and every operation with a live nest gets an entry
        in ``planned_runs_by_source_operation`` (``_copy_laser_nests`` writes one per nest
        it copies, and raises rather than skipping if it cannot). The branch is kept
        because the alternative if it ever DID become reachable — substituting the
        work-order quantity — is a ~40x planning error, and refusing beats that. See
        ``_recomputed_qty_planned``.
    """
    source_allocations = (
        tenant_query(db, WorkOrderMaterialAllocation, company_id)
        .filter(
            WorkOrderMaterialAllocation.work_order_id == source.id,
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
        )
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )
    if not source_allocations:
        return [], []

    # No ``part_id is not None`` filter: ``work_order_material_allocations.part_id`` is
    # NOT NULL, so every row has one and a guard here would be dead code that also made
    # the response schema pretend the field is nullable.
    part_ids = {allocation.part_id for allocation in source_allocations}
    parts = {part.id: part for part in tenant_query(db, Part, company_id).filter(Part.id.in_(part_ids)).all()}

    new_allocations: list[WorkOrderMaterialAllocation] = []
    skipped: list[WorkOrderDuplicateSkippedAllocation] = []

    def _skip(allocation: WorkOrderMaterialAllocation, reason: str) -> None:
        # The RESPONSE SCHEMA, not a dict — validated here, inside the caller's
        # transaction, rather than at response-build time after the commit.
        skipped.append(
            WorkOrderDuplicateSkippedAllocation(
                source_allocation_id=allocation.id,
                part_id=allocation.part_id,
                # Named so a chain reader can join a skipped tie to the skipped
                # operation entry that explains it.
                source_work_order_operation_id=allocation.work_order_operation_id,
                reason=reason,
            )
        )

    for allocation in source_allocations:
        part = parts.get(allocation.part_id)
        if part is None or getattr(part, "is_deleted", False):
            _skip(allocation, "part_not_available")
            continue

        new_operation_id: Optional[int] = None
        if allocation.work_order_operation_id is not None:
            new_operation = operation_map.get(allocation.work_order_operation_id)
            if new_operation is None:
                _skip(allocation, "operation_not_copied")
                continue
            new_operation_id = new_operation.id

        qty_planned = _recomputed_qty_planned(
            allocation,
            nest_backed=allocation.work_order_operation_id in nest_backed_operation_ids,
            planned_runs=planned_runs_by_source_operation.get(allocation.work_order_operation_id),
            quantity_ratio=quantity_ratio,
        )
        if qty_planned is None:
            _skip(allocation, "nest_runs_unavailable")
            continue

        new_allocation = WorkOrderMaterialAllocation(
            company_id=company_id,
            work_order_id=new_work_order.id,
            work_order_operation_id=new_operation_id,
            part_id=allocation.part_id,
            source=allocation.source,
            status=AllocationStatus.OPEN,
            qty_per_run=allocation.qty_per_run,
            qty_planned=qty_planned,
            # Snapshot of the part's unit AS OF NOW — see the docstring. Routed through
            # the same helper the nest and hand-created tie paths use, so all three
            # snapshot the unit identically (lowercase enum value, "each" fallback).
            unit_of_measure=_uom_value(part),
            qty_consumed=0.0,
            # Never carry a lot pin — see the docstring.
            pinned_inventory_item_id=None,
            pinned_lot_number=None,
            notes=allocation.notes,
            created_by=user_id,
        )
        db.add(new_allocation)
        new_allocations.append(new_allocation)

    db.flush()

    # Same audit shape as ``laser_nest_service.create_nest_material_allocation`` and
    # ``POST /work-orders/{id}/material-allocations`` — the three tie-creation paths are
    # kept in lock-step so a chain reader sees one consistent record per tie.
    for new_allocation in new_allocations:
        part = parts.get(new_allocation.part_id)
        part_number = part.part_number if part is not None else new_allocation.part_id
        audit.log_create(
            "work_order_material_allocation",
            new_allocation.id,
            f"WO {new_work_order.work_order_number} / part {part_number}",
            new_values=new_allocation,
            description=(
                f"Tied {new_allocation.qty_planned} {new_allocation.unit_of_measure} of part {part_number} "
                f"to work order {new_work_order.work_order_number} (duplicated from "
                f"{source.work_order_number})"
            ),
            extra_data={
                "work_order_id": new_work_order.id,
                "work_order_operation_id": new_allocation.work_order_operation_id,
                "part_id": new_allocation.part_id,
                "source": new_allocation.source.value,
                "pinned_inventory_item_id": None,
                "duplicated_from_work_order_id": source.id,
                "duplicated_from_work_order_number": source.work_order_number,
            },
        )

    return new_allocations, skipped


def _recomputed_qty_planned(
    allocation: WorkOrderMaterialAllocation,
    *,
    nest_backed: bool,
    planned_runs: Optional[int],
    quantity_ratio: float,
) -> Optional[float]:
    """The duplicate's planning demand for one tie, or ``None`` to SKIP it.

    Exactly ONE branch re-derives the number from scratch — the nest-backed one, which
    has a better basis than the stored value: ``COALESCE(qty_per_run, 1.0) x
    planned_runs`` is what ``laser_nest_service.create_nest_material_allocation`` writes
    when a nest tie is first created, and the runs came across verbatim. Every other tie
    SCALES the source's own ``qty_planned`` by ``quantity_ratio``.

    **Why the non-nest operation-scoped tie scales rather than recomputing.**
    ``qty_planned`` is caller-supplied and independent of ``qty_per_run``:
    ``MaterialAllocationCreate`` requires ``qty_planned`` (``gt=0``) and leaves
    ``qty_per_run`` optional, and the endpoint stores ``qty_planned`` verbatim while
    defaulting ``qty_per_run`` to 1.0 on an operation-scoped tie when it is omitted. So
    "500 lb of bar to OP20" is an ordinary tie holding ``qty_per_run = 1.0``,
    ``qty_planned = 500``. Recomputing it as ``qty_per_run x quantity_ordered`` — the
    previous behavior — turned that into the work-order quantity: a silent rewrite, with
    no skip, nothing on the response and nothing on the chain. Scaling also makes this
    function's contract uniform, which the recomputation broke: a SAME-quantity duplicate
    now reproduces the source value bit-for-bit on every non-nest tie, not just the
    work-order-scoped ones.

    **Why the nest-backed branch REFUSES instead of substituting.** A nest-backed
    operation's run basis is ``planned_runs``, which is a per-nest number (a nest runs 3
    sheets) and has nothing to do with the work order's ordered quantity, which for a
    laser WO is the SUM of every nest's runs (40 nests x 3 = 120). Falling back to the
    work-order quantity therefore does not degrade gracefully — it inflates one nest's
    sheet demand by roughly the nest count. ``qty_planned`` is planning demand only, so
    nothing over-consumes on the back of it (the consumption engine reconciles to
    ``qty_per_run x (complete + scrapped)``, not to this column), but the shortage and MRP
    views read it and would show a ~40x material requirement for that nest. A missing tie
    the planner is told about beats a present tie that is wrong by two orders of
    magnitude. That refusal is defence, not a live path — see ``_copy_material_allocations``
    on ``nest_runs_unavailable``.

    ``planned_runs == 0`` is a legitimate value and is honoured as 0.0, not treated as
    "unknown". Only ``None`` — nest-backed but no run count came across at all — refuses.
    The distinction matters because the caller passes ``dict.get(...)``, where a truthiness
    test would have swallowed a real zero.
    """
    # The branch predicate stated in full — "operation-scoped AND nest-backed" — rather
    # than leaning on the caller deriving ``nest_backed`` from the operation id. That
    # keeps this pure function correct when called on its own (a work-order-scoped tie
    # has no runs to multiply, so answering ``qty_per_run x planned_runs`` for one would
    # be nonsense), which is how the defensive branch below is tested at all.
    if nest_backed and allocation.work_order_operation_id is not None:
        if planned_runs is None:
            return None
        # NULL qty_per_run on an operation-scoped tie means "not run-scaled"; readers
        # treat it as 1.0 (COALESCE(qty_per_run, 1.0)) — work_order_material.py.
        effective_qty_per_run = float(allocation.qty_per_run) if allocation.qty_per_run is not None else 1.0
        return effective_qty_per_run * float(planned_runs)
    return float(allocation.qty_planned or 0.0) * quantity_ratio
