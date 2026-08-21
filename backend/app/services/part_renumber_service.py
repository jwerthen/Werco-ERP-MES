"""Renumbering a part in place, and reporting what that will do first.

WHY IN PLACE, AND NOT A NEW PART
--------------------------------
The alternative -- create the new number as a new part and mark the old one
obsolete -- was considered and rejected by the owner (2026-08-21). For a part the
shop has been RUNNING, superseding means the on-hand stock, open work orders and
BOM lines all still point at the old part: BOM rework, a stock transfer, and two
permanent identities for one physical article. AS9100D 8.5.2 wants one identity
per article, so superseding is the *less* compliant answer here, not the safer one.

So the number moves and everything keeps pointing at the same row. Stock, open
work orders, BOM lines, POs, ties and lots need no migration at all -- they are
all FKs on ``part_id``.

THE ORDERING IS THE FEATURE
---------------------------
``_drain_operation_component_links`` runs BEFORE the swap, while the old number is
still live. That is the load-bearing constraint of the whole design, and reversing
those two steps silently breaks work-order quantity rollups with no error anywhere.

An assembly work order's operations carry ``name`` minted as
``f"{component.part_number} - {routing_op.name}"``. That string is not just a
label: ``work_orders._reconcile_operation_component_quantities`` splits it on
``" - "`` and uses the prefix as a LOOKUP KEY to repair a NULL
``component_part_id`` and then reconcile ``component_quantity`` -- the operator's
quantity target. Once the number changes, that lookup misses forever and the miss
is a silent ``continue``.

Draining first converts every such row from "linked by string" to "linked by FK"
while the string still matches. Afterwards ``op.name`` is only text.

WHY THE STRINGS ARE NEVER REWRITTEN
-----------------------------------
Four reasons, each sufficient on its own:

1. ``op.name`` on a released work order is part of the released quality plan
   (invariant 5). CLAUDE.md's ``operation_number`` convention made exactly this
   call for exactly this reason -- legacy values are forward-only, never
   backfilled, because an UPDATE mutates released quality plans.
2. It cannot distinguish a minted ``"ABC-1 - Deburr"`` from an office-typed name.
   ``PUT /work-orders/operations/{id}`` exposes ``name`` as free text, so a
   supervisor's hand-typed ``"Weld - Station 3"`` satisfies any pattern test and
   would have its instruction destroyed.
3. ``WorkOrderOperation`` maps ``version`` as ``version_id_col``, so a bulk
   rewrite bumps every touched row's version and any concurrent request holding
   one raises ``StaleDataError`` -- a **409 at an operator's Complete button**.
4. The traveler PDF reads the component number LIVE, so a partial rewrite would
   print the old number in ``name`` beside the new one in the same row: a
   mixed-identity quality document, which is worse than a consistently old one.

The floor-facing consequence -- that on an assembly job the baked prefix is
currently the only way a component number reaches the operator -- is fixed by
showing the LIVE number on the kiosk and dispatch board instead (PR3), which fixes
it for this rename and every future one without mutating anything.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias, normalize_alias_key
from app.models.work_order import WorkOrder
from app.services.part_number_resolver import find_part_number_conflict
from app.services.sheet_stock_spec import canonical_alloy, derive_sheet_spec, is_sheet_like

logger = logging.getLogger(__name__)

# Work orders whose operations can still be repaired. A terminal work order is a
# finished record: its operations keep whatever prefix they were released with,
# which is correct (invariant 5) and is why the impact read reports the stale-prefix
# count separately from the repairable one.
_OPEN_WO_STATUSES = ("draft", "released", "in_progress", "on_hold")


@dataclass
class SheetSpecDelta:
    """What the sheet matcher reads out of the old number versus the new one.

    For sheet and plate the part number IS the material spec -- thickness, size and
    alloy are parsed out of the string because ``Part`` carries no such columns. So
    a renumber can silently change which physical sheet the laser-nest matcher
    believes this part is, or stop it recognizing it at all.

    Reported, never refused. If the CURRENT string is wrong the matcher is ALREADY
    mis-matching, and fixing the number is the repair -- refusing would block the
    case the feature exists for. It is also unenforceable: the number is free text,
    so a refusal only pushes the operator to a spelling that defeats the check.
    """

    is_sheet_like_before: bool = False
    is_sheet_like_after: bool = False
    thickness_before: Optional[str] = None
    thickness_after: Optional[str] = None
    sheet_size_before: Optional[str] = None
    sheet_size_after: Optional[str] = None
    alloy_before: Optional[str] = None
    alloy_after: Optional[str] = None

    @property
    def spec_before(self) -> bool:
        return bool(self.thickness_before or self.sheet_size_before or self.alloy_before)

    @property
    def spec_after(self) -> bool:
        return bool(self.thickness_after or self.sheet_size_after or self.alloy_after)

    @property
    def changed(self) -> bool:
        return (
            self.thickness_before != self.thickness_after
            or self.sheet_size_before != self.sheet_size_after
            or self.alloy_before != self.alloy_after
        )


@dataclass
class RenumberDiagnostic:
    code: str
    detail: str


@dataclass
class RenumberImpact:
    """What a renumber would do. Computed by a PURE READ that writes nothing."""

    current_part_number: str
    normalized_new_part_number: Optional[str] = None
    eligible: bool = False
    blockers: List[RenumberDiagnostic] = field(default_factory=list)
    advisories: List[RenumberDiagnostic] = field(default_factory=list)
    open_work_order_count: int = 0
    operations_with_stale_prefix: int = 0
    operations_needing_repair: int = 0
    existing_aliases: List[str] = field(default_factory=list)
    sheet: SheetSpecDelta = field(default_factory=SheetSpecDelta)


def _live_part_or_404(db: Session, company_id: int, part_id: int) -> Part:
    part = tenant_query(db, Part, company_id).filter(Part.id == part_id, Part.is_deleted == False).first()  # noqa: E712
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


def _open_work_orders_for_component(db: Session, company_id: int, part_id: int, part_number: str) -> List[WorkOrder]:
    """Open work orders that could carry this part's number baked into an operation name.

    THREE ways a work order can depend on this part, and the third is the one that
    matters most -- it is also the one the obvious implementation misses.

    1. The work order PRODUCES this part (``WorkOrder.part_id``).
    2. An operation already links to it as a component (``component_part_id``).
    3. **An operation names it in its baked prefix but has NO component link yet.**

    (3) is the whole point of the drain, and (1) and (2) cannot find it. The work
    order is for somebody else's ASSEMBLY, so ``part_id`` is the assembly's; and
    ``component_part_id`` is NULL, which is precisely the state we are here to
    repair. So the ONLY thing connecting that operation to this part is the string
    we are about to invalidate -- which means the search has to use that string,
    while it still matches. Searching by FK alone finds nothing and the drain
    silently repairs zero rows.

    ``part_number`` is passed in rather than read off the part so the caller cannot
    accidentally search on the NEW number after the swap; by then the prefix no
    longer matches anything and the search is worthless.

    Scoped to non-terminal work orders. A finished one keeps what it was released
    with, by design (invariant 5).
    """
    from app.models.work_order import WorkOrderOperation

    own = (
        tenant_query(db, WorkOrder, company_id)
        .filter(
            WorkOrder.part_id == part_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_(_OPEN_WO_STATUSES),
        )
        .all()
    )

    linked_ids = {
        row[0]
        for row in (
            tenant_query(db, WorkOrderOperation, company_id)
            .with_entities(WorkOrderOperation.work_order_id)
            .filter(WorkOrderOperation.component_part_id == part_id)
            .distinct()
            .all()
        )
    }
    prefixed_ids = {
        row[0]
        for row in (
            tenant_query(db, WorkOrderOperation, company_id)
            .with_entities(WorkOrderOperation.work_order_id)
            .filter(WorkOrderOperation.name.ilike(f"{part_number} - %"))
            .distinct()
            .all()
        )
    }

    seen = {wo.id for wo in own}
    candidate_ids = [wo_id for wo_id in (linked_ids | prefixed_ids) if wo_id not in seen]
    extra: List[WorkOrder] = []
    if candidate_ids:
        extra = (
            tenant_query(db, WorkOrder, company_id)
            .filter(
                WorkOrder.id.in_(candidate_ids),
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.status.in_(_OPEN_WO_STATUSES),
            )
            .all()
        )
    return own + extra


def _count_prefix_operations(db: Session, company_id: int, part_number: str) -> tuple[int, int]:
    """(operations carrying this prefix, of which need a link repaired).

    Two counts, not one, and the distinction is what keeps the impact screen
    honest. The prefix is consulted ONLY when ``component_part_id IS NULL``, so
    the raw count includes rows that are already linked and entirely fine. Showing
    only the raw number would put a large, alarming figure in front of the operator
    for work that needs nothing done to it.
    """
    from app.models.work_order import WorkOrderOperation

    prefix = f"{part_number} - %"
    base = (
        tenant_query(db, WorkOrderOperation, company_id)
        .join(WorkOrder, WorkOrder.id == WorkOrderOperation.work_order_id)
        .filter(
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_(_OPEN_WO_STATUSES),
            WorkOrderOperation.name.ilike(prefix),
        )
    )
    total = base.count()
    needing = base.filter(WorkOrderOperation.component_part_id.is_(None)).count()
    return total, needing


def _sheet_delta(part: Part, new_number: Optional[str]) -> SheetSpecDelta:
    before = derive_sheet_spec(part.part_number, part.name)
    delta = SheetSpecDelta(
        is_sheet_like_before=is_sheet_like(part.part_number, part.name, part.description),
        thickness_before=before.thickness,
        sheet_size_before=before.sheet_size,
        alloy_before=canonical_alloy(part.part_number) or canonical_alloy(part.name),
    )
    if not new_number:
        return delta
    after = derive_sheet_spec(new_number, part.name)
    delta.is_sheet_like_after = is_sheet_like(new_number, part.name, part.description)
    delta.thickness_after = after.thickness
    delta.sheet_size_after = after.sheet_size
    delta.alloy_after = canonical_alloy(new_number) or canonical_alloy(part.name)
    return delta


def _sheet_advisories(delta: SheetSpecDelta) -> List[RenumberDiagnostic]:
    """Plain-language advisories about what the nest screen will do differently.

    Deliberately advisories, never blockers -- see ``SheetSpecDelta``. The wording
    is aimed at a planner, not a developer: what the system reads today, what it
    will read after, and what visibly changes on the nest screen.
    """
    out: List[RenumberDiagnostic] = []
    if not (delta.is_sheet_like_before or delta.is_sheet_like_after):
        return out

    if delta.spec_before and not delta.spec_after:
        out.append(
            RenumberDiagnostic(
                code="SHEET_SPEC_LOST",
                detail=(
                    "This number states the material spec and the new one does not. The nest screen "
                    "reads thickness, size and grade out of the part number to suggest a sheet — after "
                    "this change it will stop suggesting this sheet automatically, and someone will "
                    "have to pick it from the full material list by hand. Nests already tied to it are "
                    "unaffected."
                ),
            )
        )
    elif delta.spec_after and not delta.spec_before:
        out.append(
            RenumberDiagnostic(
                code="SHEET_SPEC_GAINED",
                detail=(
                    "The new number states a material spec and the current one does not. From now on "
                    "the nest screen will offer this sheet on its own for nests that call for it — and "
                    "a suggestion someone accepts takes that stock off the shelf when the operation "
                    "finishes. Make sure it matches the sheet on the rack."
                ),
            )
        )
    elif delta.spec_before and delta.spec_after and delta.changed:
        out.append(
            RenumberDiagnostic(
                code="SHEET_SPEC_CHANGED",
                detail=(
                    "Both numbers state a material spec and they disagree "
                    f"(was {delta.thickness_before or '?'} / {delta.sheet_size_before or '?'} / "
                    f"{delta.alloy_before or '?'}; becomes {delta.thickness_after or '?'} / "
                    f"{delta.sheet_size_after or '?'} / {delta.alloy_after or '?'}). The nest screen "
                    "will match this stock to different nests than it does today. Make sure the new "
                    "number describes the sheet physically on the rack."
                ),
            )
        )
    return out


def build_renumber_impact(
    db: Session,
    company_id: int,
    part_id: int,
    new_part_number: Optional[str],
) -> RenumberImpact:
    """What a renumber would do. PURE READ -- writes nothing, structurally.

    Takes no ``AuditService`` and no actor, so it cannot write an audit row, a
    ledger row or an event even by accident. Keeping that structural rather than
    conventional is the rule the backflush readiness companions established.

    A stale ``eligible: true`` is NOT authorization. The write re-runs every probe
    against the state at write time; this read only tells the operator what to
    expect.
    """
    part = _live_part_or_404(db, company_id, part_id)
    normalized = normalize_alias_key(new_part_number) if new_part_number else None

    impact = RenumberImpact(
        current_part_number=part.part_number,
        normalized_new_part_number=normalized,
        existing_aliases=[
            row.alias_number
            for row in tenant_query(db, PartNumberAlias, company_id)
            .filter(PartNumberAlias.part_id == part.id)
            .order_by(PartNumberAlias.created_at.desc())
            .all()
        ],
    )

    impact.open_work_order_count = len(_open_work_orders_for_component(db, company_id, part.id, part.part_number))
    total, needing = _count_prefix_operations(db, company_id, part.part_number)
    impact.operations_with_stale_prefix = total
    impact.operations_needing_repair = needing
    impact.sheet = _sheet_delta(part, normalized)

    if not normalized:
        # No candidate number: report the current state, eligible undecided.
        return impact

    if normalized == normalize_alias_key(part.part_number):
        impact.eligible = True
        impact.advisories.append(
            RenumberDiagnostic(
                code="NO_CHANGE",
                detail="That is already this part's number — renumbering would do nothing.",
            )
        )
        return impact

    for blocker in _renumber_blockers(db, company_id, part, normalized):
        impact.blockers.append(blocker)

    impact.advisories.extend(_sheet_advisories(impact.sheet))
    impact.eligible = not impact.blockers
    return impact


def _renumber_blockers(
    db: Session,
    company_id: int,
    part: Part,
    normalized_new: str,
) -> List[RenumberDiagnostic]:
    """Every condition that refuses the write, in the order the write checks them.

    Each entry is verbatim the 409 detail the write would raise, so the impact
    screen can never disagree with the refusal the operator is about to get.
    """
    out: List[RenumberDiagnostic] = []

    conflict = find_part_number_conflict(db, company_id, normalized_new, excluding_part_id=part.id)
    if conflict:
        out.append(RenumberDiagnostic(code=conflict.code, detail=conflict.detail))

    # The other direction: the number being RETIRED must not already be a retired
    # number of some other part. Without this probe the alias insert would violate
    # uq_part_number_aliases_company_key and surface as a 500 -- main.py has no
    # IntegrityError handler.
    old_key = normalize_alias_key(part.part_number)
    clash = (
        tenant_query(db, PartNumberAlias, company_id)
        .filter(PartNumberAlias.alias_number_key == old_key, PartNumberAlias.part_id != part.id)
        .first()
    )
    if clash:
        out.append(
            RenumberDiagnostic(
                code="OLD_NUMBER_ALREADY_RETIRED",
                detail=(
                    f"'{part.part_number}' is already a retired number for another part. Retiring it "
                    "again would make it ambiguous."
                ),
            )
        )
    return out


def _drain_operation_component_links(db: Session, company_id: int, part: Part) -> int:
    """Link operations to this part by FK while its number still matches. Returns rows repaired.

    MUST run before the swap -- see the module docstring.

    Delegates to ``work_orders._reconcile_operation_component_quantities`` rather
    than re-implementing the match, and that is not merely DRY. The existing repair
    is BOM-MEMBERSHIP-GATED: it builds its lookup map from the work order's own
    produced part's active BOM and bails entirely when there is no active BOM. A
    hand-rolled "any operation whose name starts with this number" pass would be
    strictly BROADER -- and ``component_part_id IS NULL`` is a load-bearing
    DISCRIMINATOR elsewhere, not merely a repair target:

    * ``work_order_state_service`` skips ops WITH a component FK when pooling
      quantity_complete;
    * its per-line rollup is ALL-OR-NOTHING -- one operation gaining an FK collapses
      a pooled work order's per-line progress to ``None``, which the wallboard then
      renders as the header number instead of honest per-line progress, with no
      error anywhere;
    * a third site gates whether an operation advances ``work_order.quantity_complete``.

    The work orders that would be caught by a broad pass and not by the gated one
    are exactly the batch/pool jobs whose part has no BOM -- the ones that rollup
    depends on. So the gate is the safety property; reusing the function makes
    breaking it structurally impossible.
    """
    from app.api.endpoints.work_orders import _reconcile_operation_component_quantities

    repaired = 0
    # part.part_number is still the OLD number here -- the drain runs before the swap.
    for work_order in _open_work_orders_for_component(db, company_id, part.id, part.part_number):
        try:
            if _reconcile_operation_component_quantities(db, work_order, company_id):
                repaired += 1
        except Exception:  # pragma: no cover - defensive
            # One unreconcilable work order must not abort the renumber; the number
            # still moves and the operator sees the count that was repaired.
            logger.warning(
                "renumber drain failed for work_order_id=%s part_id=%s",
                work_order.id,
                part.id,
                exc_info=True,
            )
    return repaired


@dataclass
class RenumberResult:
    part: Part
    previous_part_number: str
    alias_id: Optional[int]
    alias_created: bool
    alias_reclaimed: bool
    work_orders_repaired: int
    operations_with_stale_prefix: int
    sheet_spec_changed: bool
    no_op: bool = False


def renumber_part(
    db: Session,
    company_id: int,
    part_id: int,
    *,
    new_part_number: str,
    expected_part_number: str,
    reason: str,
    actor_user_id: Optional[int],
) -> RenumberResult:
    """Swap a part's number in place and retire the old one. Caller wraps the transaction.

    Flushes, never commits -- the endpoint wraps this in ``atomic_transaction`` so
    the swap, the alias row, the drained links and the audit row are one unit.
    """
    part = _live_part_or_404(db, company_id, part_id)
    old_number = part.part_number
    normalized_new = normalize_alias_key(new_part_number)

    # Re-stating the current number changes nothing and must not fail. Same rule
    # assert_backflush_change_allowed follows, and the same rule log_update follows
    # when a diff comes back empty.
    if normalized_new == normalize_alias_key(old_number):
        return RenumberResult(
            part=part,
            previous_part_number=old_number,
            alias_id=None,
            alias_created=False,
            alias_reclaimed=False,
            work_orders_repaired=0,
            operations_with_stale_prefix=0,
            sheet_spec_changed=False,
            no_op=True,
        )

    # Compare-and-swap precondition. Part maps NO version_id_col, so this string is
    # the only concurrency control available: if someone renumbered this part while
    # the operator had the dialog open, we must refuse rather than retire a number
    # that is no longer the one they saw.
    if normalize_alias_key(expected_part_number) != normalize_alias_key(old_number):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This part's number changed while you were editing (it is now '{old_number}'). "
                "Reload and try again."
            ),
        )

    blockers = _renumber_blockers(db, company_id, part, normalized_new)
    if blockers:
        # Refuse BEFORE any write, so a refused renumber leaves the row untouched.
        raise HTTPException(status_code=409, detail=blockers[0].detail)

    sheet_delta = _sheet_delta(part, normalized_new)
    _, stale_prefix_count = _count_prefix_operations(db, company_id, old_number)

    # STEP 1 -- DRAIN, while the old number is still live. Reversing this with the
    # swap below is the one ordering mistake that breaks quantity rollups silently.
    work_orders_repaired = _drain_operation_component_links(db, company_id, part)

    # STEP 2 -- the swap, as a compare-and-swap on the old value.
    updated = (
        tenant_query(db, Part, company_id)
        .filter(Part.id == part.id, Part.part_number == old_number)
        .update(
            {"part_number": normalized_new, "updated_at": datetime.utcnow()},
            synchronize_session=False,
        )
    )
    if updated != 1:
        # Someone changed it between our read and this statement.
        raise HTTPException(
            status_code=409,
            detail="This part's number changed while you were editing. Reload and try again.",
        )
    db.expire(part)

    # STEP 3 -- retire the old number.
    #
    # Renaming a part INTO a number it previously carried (A -> B -> A) reclaims
    # that alias rather than colliding with it: the number is live again, and a live
    # part always beats a retired one, so the row is now dead weight. Deleting it
    # keeps the two key spaces disjoint and needs no "retired-but-superseded" third
    # state. Nothing is lost -- both renames are audit rows, which are the record.
    reclaimed = (
        tenant_query(db, PartNumberAlias, company_id)
        .filter(
            PartNumberAlias.part_id == part.id,
            PartNumberAlias.alias_number_key == normalized_new,
        )
        .delete(synchronize_session=False)
    )

    alias = PartNumberAlias(
        part_id=part.id,
        alias_number=old_number,
        alias_number_key=normalize_alias_key(old_number),
        reason=reason,
        created_by=actor_user_id,
        company_id=company_id,
    )
    db.add(alias)
    try:
        db.flush()
    except IntegrityError as exc:
        # The probes race; the constraint does not. Re-raise as a 409 rather than
        # letting it reach main.py's bare Exception handler as a 500. Do NOT write
        # an audit row here: AuditService.log opens a SAVEPOINT inside this same
        # failed transaction.
        raise HTTPException(
            status_code=409,
            detail=(f"'{normalized_new}' was taken while you were editing. Reload and choose another " "number."),
        ) from exc

    return RenumberResult(
        part=part,
        previous_part_number=old_number,
        alias_id=alias.id,
        alias_created=True,
        alias_reclaimed=bool(reclaimed),
        work_orders_repaired=work_orders_repaired,
        operations_with_stale_prefix=stale_prefix_count,
        sheet_spec_changed=sheet_delta.changed,
        no_op=False,
    )


__all__ = [
    "RenumberDiagnostic",
    "RenumberImpact",
    "RenumberResult",
    "SheetSpecDelta",
    "build_renumber_impact",
    "renumber_part",
]


# Referenced by the endpoint layer for the audit payload; kept here so the shape of
# what gets recorded lives beside the code that decides it.
def audit_extra_data(result: RenumberResult, reason: str) -> dict:
    """What the audit row must carry beyond the generic column diff.

    The generic ``log_update`` diff records ``part_number`` old -> new for free. Two
    things it cannot know, both of which matter later:

    * The GATE'S VERDICT at the time of the write. A work order raised five minutes
      after a renumber makes the part look as though it always had that history, so
      the counts are not reconstructable afterwards.
    * The REASON, which every other identity-affecting verb in this system requires
      (receiving void, NCR void, vendor delete).
    """
    return {
        "old_part_number": result.previous_part_number,
        "new_part_number": result.part.part_number,
        "reason": reason,
        "alias_id": result.alias_id,
        "alias_reclaimed": result.alias_reclaimed,
        "work_orders_repaired": result.work_orders_repaired,
        "operations_with_stale_prefix": result.operations_with_stale_prefix,
        "sheet_spec_changed": result.sheet_spec_changed,
    }
