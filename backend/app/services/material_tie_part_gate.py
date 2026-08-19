"""THE part-type gate behind every work-order material tie. Shared by EVERY seam that arms one.

WHY IT EXISTS. A ``work_order_material_allocations`` row is not a note — it is standing
demand that the consumption engine draws against. Invariant 6: completing a work order
(or one of its operations) posts an ISSUE for the tied part and DEPLETES that part's
stock, FIFO-picking lots and writing them onto the as-built record. So tying a part the
shop *produces* — a MANUFACTURED part or an ASSEMBLY — makes a job consume finished goods
in order to build itself. Nothing downstream can tell that apart from a legitimate
material draw: the ledger row, the lot linkage and the cost roll-up are all shaped
identically, and consumption **never auto-reverses** (invariant 6b), so the only remedy
after the fact is a reasoned compensating transaction against inventory that should never
have moved. The refusal has to happen at tie time, which is the last moment an actor with
intent is present.

WHY IT IS SHARED. There are exactly three constructors of a tie's ``part_id``:

* ``POST /work-orders/{id}/materials`` (``api.endpoints.work_order_materials``),
* ``_find_nest_material_part`` in ``api.endpoints.work_orders``, the shared resolver
  behind BOTH the laser-package import and the manual nest create, and
* ``work_order_duplicate_service._copy_material_allocations``, which copies an EXISTING
  tie row onto a new draft work order.

The first two resolve a caller-supplied ``part_id`` and call
``assert_part_is_tieable_material``. The third re-resolves nothing a caller chose, so it
asks the same question through the non-raising ``part_is_tieable_material`` and SKIPS the
tie (``part_not_tieable``) rather than raising — a duplicate is not a tie-creation
request and aborting one over a legacy row would refuse the whole copy. Same predicate,
two shapes; the DECISION lives in exactly one place.

AND ONE RESURRECTOR, which is not a constructor and is exactly as dangerous.
``material_consumption_service.reopen_allocations_cancelled_by_delete`` mints no
``part_id`` — it flips an EXISTING tie from CANCELLED back to OPEN when
``POST /work-orders/{id}/restore`` undoes a work-order soft delete. That re-arms standing
demand, and it does so at a moment when the part may have been reclassified since the
delete, so it asks ``part_is_tieable_material`` too and leaves a non-tieable tie
CANCELLED (reason ``PART_NOT_TIEABLE_REASON``, reported on the restore's response
envelope and written to the restore's audit row). Closing it there rather than in
``live_tie_work_order_ids`` is deliberate: see that function's docstring for why counting
a deleted job's ties would be the wrong repair.

A gate implemented in one of two files is not a gate. This follows the precedent set by
``parts.assert_backflush_change_allowed``, which lives in one module and is *imported*
into ``materials.py`` rather than re-implemented, because both handlers write the same
rows. Same rule here: import this, don't copy the predicate.

THE SECOND HALF OF THE SAME RULE. ``assert_part_is_tieable_material`` answers "may this
part BE tied?". ``assert_part_type_change_allowed`` answers the mirror question, "may
this part STOP being tieable while ties already exist?" — because the hazard is not only
that a produced part gets tied, it is equally that a tied part gets RECLASSIFIED into a
produced one. Reclassification arrives at the identical end state (standing demand that
depletes finished goods) by a door the tie gate never sees: the tie was legitimate when
it was made, and nothing re-checks it afterwards. Two conversion doors exist —
``PUT /parts/{id}`` and the BOM importer's assembly promotion — and both consult this
module rather than carrying their own copy of the reasoning.

WHY 409 THERE AND 422 HERE. They refuse different things. The tie gate refuses the
REQUEST'S OWN CONTENT — the ``part_id`` in the body names something that cannot play the
material role — which is an unprocessable entity. The conversion gate refuses a
well-formed request because of STATE THAT ALREADY EXISTS elsewhere in the tenant: the
part type being asked for is perfectly valid, and the very same request succeeds the
moment those ties are untied. That is a conflict, and 409 is the code every other
"you must undo something first" refusal in this system already uses (the backflush
readiness refusal, the sequencing flip on a worked operation, a PO with received
material). It also tells the caller the right thing: retry after changing state, not
after changing the payload.

AND THE REMEDY HAS TO BE REACHABLE, or 409 is the wrong shape for it. "Succeeds the moment
those ties are untied" is a promise about a step the planner can actually take, and it
holds only because the conversion gate counts LIVE demand rather than tie history: nothing
in ``app/`` ever closes a tie at completion, so a count keyed on ``status == OPEN`` alone
would refuse the conversion **forever** on any part the shop had ever consumed, and would
point at an untie that ``DELETE …/material-allocations/{id}`` refuses (409, material
issued) and that ``return_and_untie`` could only perform by crediting a shipped job's
material back into stock. See ``live_tie_work_order_ids`` — that is why the count joins the
work order.

This module deliberately imports **nothing from ``app.api.endpoints``** — both endpoint
modules import it, so any dependency the other way would be a cycle.

WHY 422 AND NOT 404. The part exists, the caller is entitled to see it (it is their own
tenant's row, and it is listed by the parts catalog they already read), and hiding it
would be a lie that sends a planner hunting for a missing record. What is refused is the
part's **ROLE** in this request — a produced part cannot play the material side of a tie —
which is precisely an unprocessable-entity case, the same shape as the existing lot-pin
refusals in ``work_order_materials._resolve_pinned_item``. Tenant misses stay 404 there,
where they belong; this gate never sees a part the caller may not read.

FAIL-OPEN ON THE TYPE, DELIBERATELY. Only ``manufactured`` and ``assembly`` are refused.
A NULL or otherwise unrecognised ``part_type`` **passes**. The four material/supply types
are the normal case and the whole population this gate is meant to admit, so refusing an
unknown value would break legitimate ties over legacy data while blocking nothing real: a
row with no readable type is not something the shop produces — the engineering catalog is
built by ``PUT /parts/{id}``, which forces the column into ``ENGINEERING_PART_TYPES`` on
every write, and ``parts.py`` filters NULL ``part_type`` out of its own list, so a produced
part with an unreadable type is not a state the API can reach. The one thing this gate
must never do is refuse material the floor is really consuming.

Note the scope on purpose: this refuses PRODUCED parts, not "everything that is not raw
stock". ``purchased`` / ``hardware`` / ``consumable`` are bought and are genuinely
consumed by jobs (hardware into an assembly, consumables at the machine), so they pass.
Narrowing the *pickers* toward raw stock is a UI concern; narrowing what may be WRITTEN to
what cannot be nonsense is this one.

WHAT THE REFUSAL SENTENCE DOES **NOT** CLAIM. The 422 reads "A work order consumes
material; it does not consume the parts the shop produces." That is true of a MATERIAL
TIE; it is **not** a categorical rule about this system, and stating it as one would be
false. A job here really can consume a shop-made sub-assembly — the **BOM backflush leg**
(``services/completion_inventory_service``) issues a ``make`` component as a stocked unit
at work-order completion. So the boundary this module actually draws is: *consuming a
shop-made sub-assembly belongs to the BOM (``Part.backflush_components``), not to a
per-operation material tie.*

Say the cost out loud rather than counting it as a free win. That leg is **opt-in and
default-off**; it is settable only through ``PUT /parts/{id}`` / ``PUT /materials/{id}``
behind ``parts.assert_backflush_change_allowed``; it is scoped to the whole WORK ORDER,
never to one operation; and per ``docs/MATERIAL_CONSUMPTION_PLAN.md`` → "Exposing the
flag" **no production job has yet exercised it**. This gate therefore removes the only
per-operation way a planner had to declare "this operation consumes a sub-assembly we
make", and points at a replacement that is neither on by default nor proven. That is a
real narrowing and it is the OWNER'S to confirm — recorded here rather than fixed by
weakening the gate, because the failure it prevents (finished goods silently depleted,
unrecoverable under invariant 6b) is strictly worse than the one it introduces (a planner
has to reach for the BOM). The remedy sentence stays "Tie raw stock instead" because that
is the right instruction for the overwhelmingly common case — a mis-picked part number.
"""

from typing import Any, Optional, Set

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.part import Part, PartType, is_engineering_part_type, normalize_part_type_value
from app.models.work_order import WorkOrder
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.services.work_order_state_service import TERMINAL_WO_STATUSES

#: The machine-readable reason BOTH non-raising callers report when they decline to put a
#: tie on a produced part: ``work_order_duplicate_service`` when it copies a plan, and
#: ``material_consumption_service.reopen_allocations_cancelled_by_delete`` when it undoes
#: a work-order delete. Their two response envelopes are documented as "the same shape",
#: and a pair of copied string literals is how that claim quietly stops being true.
PART_NOT_TIEABLE_REASON = "part_not_tieable"


def part_is_tieable_material(part: Part) -> bool:
    """THE predicate: may this part sit on the MATERIAL side of a work-order tie?

    The one place the scope decision lives — refuse the two PRODUCED types, admit every
    material/supply type, and fail OPEN on a NULL or unrecognised value (module docstring
    for why). ``assert_part_is_tieable_material`` is this plus an HTTP 422, and
    ``work_order_duplicate_service`` reads it directly because a copier has a skip list
    where an endpoint has a status code. Two shapes, one decision — do not re-derive it
    from ``is_engineering_part_type`` at a call site, or the scope becomes something each
    caller re-decides.
    """
    return not is_engineering_part_type(part.part_type)


def assert_part_is_tieable_material(part: Part) -> None:
    """Refuse (**422**) a work-order material tie whose part is one the shop PRODUCES.

    Called immediately after the part resolve and BEFORE any mutation on both tie-write
    seams, so a refusal leaves the request byte-identical to one that never arrived.

    Passes for every material/supply type (``purchased`` / ``raw_material`` / ``hardware``
    / ``consumable``) and for a NULL or unrecognised ``part_type`` — see the module
    docstring for why the unknown case fails OPEN.
    """
    if part_is_tieable_material(part):
        return

    # Name the refused role in the shop's own words. Both values are already known to be
    # engineering types here, so this only picks WHICH sentence, never WHETHER to refuse.
    kind = (
        "an assembly" if normalize_part_type_value(part.part_type) == PartType.ASSEMBLY.value else "a manufactured part"
    )
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            f"Part {part.part_number} is {kind}, not stock material. A work order consumes material; "
            "it does not consume the parts the shop produces. Tie raw stock instead."
        ),
    )


def live_tie_work_order_ids(db: Session, part: Part, *, company_id: int) -> Set[int]:
    """The distinct UNFINISHED work orders holding an OPEN tie to ``part``, company-scoped.

    "Live" is TWO conditions, and the conversion gate needs both, because what it is
    counting is not ties — it is **demand that will still be drawn**.

    **1. The TIE must be OPEN.** On this table ``status`` IS the tombstone (there is no
    ``SoftDeleteMixin`` — see ``models/work_order_material``), so a CANCELLED row is an
    untied tie and a CLOSED row is a finished one. Neither is standing demand.

    **2. The WORK ORDER must not be TERMINAL.** Consumption is driven exclusively from the
    two completion seams — ``apply_operation_completion_inventory_effects`` and
    ``apply_completion_inventory_effects`` — and every path that reaches them refuses a
    work order in ``TERMINAL_WO_STATUSES``: the completion verbs guard on it directly, and
    the reconcile-on-read fires effects only for work orders that appear in
    ``transitions``, which a terminal work order can never enter (the early terminal guard
    in ``work_order_state_service`` returns first). So a tie held by a COMPLETE / CLOSED /
    CANCELLED job **carries no live demand**: nothing will ever drive a completion against
    it, and no reclassification of its part can make finished goods move.

    Counting one anyway is not merely over-strict, it is a **dead end**. Nothing in
    ``app/`` ever writes ``AllocationStatus.CLOSED`` (that model's docstring says so
    outright) and work-order completion neither closes nor cancels ties — only WO delete,
    nest re-import detach, and ``return_and_untie`` do — so every tie a part has ever
    carried stays OPEN forever once its job ships. A status-only count therefore refuses
    the ordinary "we used to buy this bracket, now we make it" reclassification **on any
    part the shop has ever consumed**, permanently, while naming a remedy the planner
    cannot reach: ``DELETE …/material-allocations/{id}`` itself refuses 409 once the ledger
    shows material issued, and the only other untie verb, ``return_and_untie``, posts a
    compensating RETURN crediting legitimately-consumed material back into stock —
    falsifying the as-built record of a finished job (invariants 5 and 6b) to satisfy a
    catalog edit. It would also disagree with the tie-creation door this module gates:
    ``POST …/material-allocations`` already refuses a NEW tie on a terminal work order,
    because "a tie that can never consume is a lie". Same reasoning, reached from the other
    side, so it had better give the same answer.

    RESIDUAL — reopening. If a terminal work order could be moved back to a non-terminal
    STATUS, the demand this count skipped would come back live with no gate re-run.
    **Today no verb does that**: ``PUT /work-orders/{id}`` refuses terminal →
    non-terminal with a 409 of its own (G6-A), and every other status write is gated on a
    non-terminal source status.

    ``POST /work-orders/{id}/restore`` is the verb that resurrects demand by the OTHER
    route — it clears ``is_deleted`` without touching ``status``, and re-OPENs the ties
    the delete cancelled — so it is emphatically not covered by the paragraph above, and
    for a while this docstring wrongly cited it as if it were. It closes the hole
    ITSELF instead: ``reopen_allocations_cancelled_by_delete`` re-asks
    ``part_is_tieable_material`` per tie and leaves a non-tieable one CANCELLED, named on
    the restore's response envelope and in the restore's audit row. Any further reopen
    verb must do the same, because nothing re-checks a tie after it is made — the same
    asymmetry ``assert_part_type_change_allowed`` exists to cover.

    Both sides of the join are scoped with the invariant-1 helpers — ``tenant_query`` for
    the allocation, ``tenant_filter`` for the work order — so neither a foreign allocation
    nor a foreign work order can reach the count.

    There is deliberately **no** ``WorkOrder.is_deleted`` filter (invariant 3's "filter,
    or say why not"), and the honest reason is NOT that it makes no difference. It makes
    all the difference: ``cancel_open_allocations_for_work_order`` runs on the work-order
    soft delete and CANCELs every OPEN tie the job holds, so a deleted work order's demand
    drops out of this count entirely and a conversion that was 409 a moment ago goes
    straight through. An earlier version of this docstring reasoned from exactly that fact
    — "a deleted job holds no OPEN tie to count in the first place" — to the conclusion
    that counting one anyway would merely be conservative. The first half is precisely why
    the second half was backwards: DELETE → reclassify → RESTORE is a three-verb bypass
    (all three supported, all ADMIN/MANAGER) that ends in an OPEN tie to a produced part
    on a live work order.

    Counting deleted jobs' ties here would still be the WRONG repair. It would refuse the
    reclassification at a moment when the hazard is genuinely not live (a deleted work
    order completes nothing and its CANCELLED ties consume nothing), and it would not stop
    the bypass anyway — the restore is what re-arms the demand, and it would run
    regardless. So the refusal lives at the narrower and more honest seam:
    ``reopen_allocations_cancelled_by_delete`` re-asks ``part_is_tieable_material`` as it
    puts each tie back, and leaves a now-produced part's tie CANCELLED with a reported
    reason. This count stays a pure question about LIVE demand.

    The join is an INNER one, so a tie whose work order cannot be resolved at all — an
    orphan, or a row belonging to another company — is not counted. That is the right
    direction rather than a gap: the completion path loads the work order before it can
    consume anything, so a tie with no reachable work order can never draw stock either.

    The count is of WORK ORDERS, not of rows, because that is the unit of the remedy: one
    job may hold an operation-scoped tie per nest, and telling a planner "12 ties" when
    there is one job to go and fix sends them looking for eleven things that are not there.
    """
    query = tenant_query(db, WorkOrderMaterialAllocation, company_id).join(
        WorkOrder, WorkOrder.id == WorkOrderMaterialAllocation.work_order_id
    )
    rows = (
        tenant_filter(query, WorkOrder, company_id)
        .with_entities(WorkOrderMaterialAllocation.work_order_id)
        .filter(
            WorkOrderMaterialAllocation.part_id == part.id,
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
            WorkOrder.status.notin_(TERMINAL_WO_STATUSES),
        )
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def part_type_change_refusal(db: Session, part: Part, new_part_type: Any, *, company_id: int) -> Optional[str]:
    """The conversion refusal SENTENCE, or ``None`` when the change is allowed.

    Non-raising half of ``assert_part_type_change_allowed``. It exists because the two
    conversion doors need the same decision in different shapes: ``PUT /parts/{id}`` is a
    single-record verb and refuses **409**, while the BOM importer's assembly promotion is
    one incidental step inside a multi-record import whose established channel for "this
    one thing could not be done" is its ``warnings`` list. Returning the sentence keeps the
    reasoning — and the wording a planner reads — in one place either way.

    Refuses only the RECLASSIFICATION DIRECTION THAT CREATES THE HAZARD, and only while
    the hazard is live. All three must hold:

    1. the part is currently NOT an engineering type (so it is tieable today),
    2. the requested type IS an engineering type (so it would stop being tieable), and
    3. at least one OPEN allocation in this company still ties it **on a work order that
       has not finished** — see ``live_tie_work_order_ids`` for why the work-order half of
       that is not optional: ties are never closed at completion, so a status-only count
       would refuse this conversion forever on any part the shop has ever consumed, and
       point at a remedy that would falsify a shipped job's as-built record.

    Everything else passes untouched: engineering → engineering (an ordinary
    manufactured/assembly edit), engineering → material (the direction that *removes* the
    hazard, and which ``PUT /parts/{id}``'s own 400 refuses for unrelated reasons anyway),
    material → material, and a no-op restatement of the current value — which condition 1
    and 2 exclude by construction, since a type cannot be both non-engineering and
    engineering. An untied part converts freely, and so does one whose only ties belong to
    finished work; the gate protects LIVE DEMAND, not classes and not history.
    """
    if is_engineering_part_type(part.part_type):
        return None
    if not is_engineering_part_type(new_part_type):
        return None

    work_order_ids = live_tie_work_order_ids(db, part, company_id=company_id)
    if not work_order_ids:
        return None

    count = len(work_order_ids)
    kind = (
        "an assembly" if normalize_part_type_value(new_part_type) == PartType.ASSEMBLY.value else "a manufactured part"
    )
    # "UNFINISHED", not "open". Two reasons the adjective is load-bearing. It is what the
    # count now means (a finished job's tie is skipped, so a planner must not go looking at
    # one), and "open" is already this table's tie STATUS — the sentence would be naming
    # one thing with a word the API uses for another.
    jobs = "1 unfinished work order still ties" if count == 1 else f"{count} unfinished work orders still tie"
    subject = "that work order" if count == 1 else "those work orders"
    return (
        f"Part {part.part_number} cannot be reclassified as {kind}: {jobs} it as material. "
        "A tie depletes the tied part when that work completes, so reclassifying it now would leave "
        f"standing demand that consumes finished goods. Untie {subject} first, then change the part type."
    )


def assert_part_type_change_allowed(db: Session, part: Part, new_part_type: Any, *, company_id: int) -> None:
    """Refuse (**409**) reclassifying a TIED material part into one the shop PRODUCES.

    The second half of the rule this module holds: ``assert_part_is_tieable_material``
    answers "may this part be tied as material", and this answers "may this part stop
    being tieable while ties exist". Both end states are identical — an OPEN allocation on
    an UNFINISHED work order whose part is MANUFACTURED or ASSEMBLY, which depletes
    finished goods at completion and never auto-reverses (invariant 6b) — so gating only
    the first door would leave the hazard reachable by simply doing the two steps in the
    other order.

    **409, not 422** (module docstring): the requested part type is a perfectly valid
    value and the payload is well-formed. What refuses it is state that already exists
    elsewhere in the tenant, and the identical request succeeds once that state is
    cleared — a promise that holds only because the count is of LIVE demand and skips ties
    on finished work (``live_tie_work_order_ids``), since untying a shipped job's tie is
    not a step anyone can take. ``detail`` is a plain string naming the part, how many
    unfinished work orders still tie it, and the remedy, because the Axios interceptor
    renders a server ``detail`` verbatim and the point of the refusal is that a human goes
    and unties something.

    Call it BEFORE the first ``setattr`` on both conversion doors, exactly as
    ``parts.assert_backflush_change_allowed`` is called — a refusal must leave the row
    untouched. Reads only ``part.id`` / ``part.part_number`` / ``part.part_type``, none of
    which the update loop has written yet at that point.
    """
    refusal = part_type_change_refusal(db, part, new_part_type, company_id=company_id)
    if refusal is None:
        return
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)
