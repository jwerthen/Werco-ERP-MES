"""``material_tie_part_gate`` as a predicate: exactly which part types may BE material.

The gate's job is one decision — may this part sit on the material side of a work-order
material tie? — and this file is the whole truth table for it, exercised directly with no
database, no request and no endpoint in the way.

WHY THE DECISION MATTERS. A ``work_order_material_allocations`` row is standing demand the
consumption engine draws against (invariant 6): completing the work order, or one of its
operations, posts an ISSUE for the tied part, FIFO-picks its lots and writes them onto the
as-built record. Nothing downstream distinguishes that from a legitimate material draw, and
consumption **never auto-reverses** — so a tie pointing at a part the shop PRODUCES makes a
job quietly eat finished goods to build itself, remediable only by a reasoned compensating
transaction against stock that should never have moved. The refusal therefore has to land
at tie time, the last moment an actor with intent is present.

The two write doors that call this (``POST /work-orders/{id}/material-allocations`` and
``_find_nest_material_part``, the shared resolver behind both laser-nest doors) are covered
over HTTP in ``tests/api/test_material_tie_part_type_gate.py`` and
``tests/api/test_material_tie_nest_import.py``. Those prove the gate is WIRED UP; this file
proves it decides correctly, including for the inputs no endpoint can currently produce.

The module holds the SECOND half of the same rule too — ``assert_part_type_change_allowed``
answers "may this part stop being tieable while LIVE ties exist", the mirror of "may this
part be tied" — and §2 below is its truth table. Its two conversion doors
(``PUT /parts/{id}`` and the BOM importer's assembly promotion) are covered over HTTP in
``tests/api/test_material_tie_part_type_gate.py`` and ``tests/api/test_bom_import.py``,
which is also where the work-order half of "live" is proved against a real database — this
file's fake query cannot execute a join, so it pins the query's SHAPE (both tenant scopes,
the join, distinct work orders) and leaves the row-level outcome to those two.
"""

import inspect

import pytest
from fastapi import HTTPException, status

import app.api.endpoints.work_order_materials as wo_materials_endpoint
import app.api.endpoints.work_orders as work_orders_endpoint
import app.services.material_consumption_service as consumption_service
import app.services.material_tie_part_gate as gate
import app.services.work_order_duplicate_service as duplicate_service
from app.models.part import ENGINEERING_PART_TYPES, MATERIAL_SUPPLY_PART_TYPES, Part, PartType
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_material import WorkOrderMaterialAllocation
from app.services.material_tie_part_gate import assert_part_is_tieable_material
from app.services.work_order_state_service import TERMINAL_WO_STATUSES

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("part_type", list(ENGINEERING_PART_TYPES))
def test_the_gate_refuses_every_engineering_part_type(part_type: PartType):
    """The refusal set is exactly ``ENGINEERING_PART_TYPES`` — parametrised off the tuple.

    Written against the constant rather than two literals, so that adding a further part
    type to the produced side (a future ``KIT``, say) makes this test cover it on the day
    the enum changes rather than on the day someone remembers.
    """
    part = Part(part_number="MTG-GATE-1", name="Produced thing", part_type=part_type)

    with pytest.raises(HTTPException) as excinfo:
        assert_part_is_tieable_material(part)

    assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = excinfo.value.detail
    assert isinstance(detail, str), "a plain string, so every client renders it verbatim"
    assert "MTG-GATE-1" in detail, "the refusal names the part so a planner can act on it"
    expected_kind = "an assembly" if part_type is PartType.ASSEMBLY else "a manufactured part"
    assert expected_kind in detail, "the sentence names the refused role in the shop's own words"


@pytest.mark.parametrize("part_type", list(MATERIAL_SUPPLY_PART_TYPES))
def test_the_gate_passes_every_material_supply_part_type(part_type: PartType):
    """All FOUR pass — not just ``raw_material``.

    ``purchased`` / ``hardware`` / ``consumable`` are bought and really are consumed by
    jobs (hardware into an assembly, weld wire at the machine). A gate quietly narrowed to
    raw stock would refuse ties the shop legitimately creates while looking stricter; the
    raw-stock preference is a picker DEFAULT with an escape hatch, and it lives in the
    frontend.
    """
    assert_part_is_tieable_material(Part(part_number="MTG-GATE-2", name="Bought thing", part_type=part_type))


def test_the_two_type_families_are_disjoint_and_cover_the_enum():
    """The truth table above is EXHAUSTIVE — asserted, not eyeballed.

    Both parametrised tests read from the two constants, so together they cover every
    ``PartType`` only while those constants still partition the enum. If a seventh value
    were added to neither tuple, both tests would keep passing while saying nothing about
    it — and the gate would silently fail open on a type nobody had classified.
    """
    engineering = set(ENGINEERING_PART_TYPES)
    material = set(MATERIAL_SUPPLY_PART_TYPES)
    assert engineering.isdisjoint(material)
    assert engineering | material == set(PartType)


@pytest.mark.parametrize("raw", [None, "widget", "", "MANUFACTURED_LOOKALIKE"])
def test_the_gate_fails_OPEN_on_an_unreadable_part_type(raw):
    """A NULL or unrecognised ``part_type`` PASSES, deliberately.

    The four material/supply types are the whole population this gate admits, so refusing
    an unknown value would break legitimate ties over legacy data while blocking nothing
    real. The one thing the gate must never do is refuse material the floor is actually
    consuming.

    ``"MANUFACTURED_LOOKALIKE"`` is in the list on purpose: the predicate normalises and
    compares WHOLE values, so a string that merely contains a produced type's name is not
    one. Pinning that stops anyone "hardening" the check into a substring match, which
    would start refusing real parts whose type happened to embed one of these words.
    """
    assert_part_is_tieable_material(Part(part_number="MTG-GATE-3", name="Legacy thing", part_type=raw))


def test_the_fail_open_is_safe_because_the_column_is_NOT_NULL():
    """The gate's stated argument for passing an unreadable type, asserted rather than assumed.

    ``material_tie_part_gate``'s docstring justifies the fail-open by saying a produced part
    cannot HAVE an unreadable type: ``Part.part_type`` is ``nullable=False``, so no write
    path can persist NULL, and ``PUT /parts/{id}`` forces the column into
    ``ENGINEERING_PART_TYPES`` on every write. That is a claim about the model — and if a
    future migration relaxed it, the fail-open would stop being a kindness to legacy data
    and become a hole a caller could aim at.

    Asserted off the MODEL column, which is dialect-independent, rather than by executing
    an insert — this suite runs on SQLite while production is Postgres.
    """
    assert Part.__table__.c.part_type.nullable is False


def test_both_write_doors_bind_the_SAME_gate_function():
    """One gate, not two copies that drift.

    The behavioural refusals in the two API suites would BOTH still pass if someone
    re-implemented the predicate inside the second endpoint module — they would only start
    disagreeing later, once one copy was edited. This is the direct proof that there is one
    implementation: both endpoint modules resolve the name to the identical callable.

    The same argument the shared ``parts.assert_backflush_change_allowed`` gate rests on: a
    gate in one of two files is not a gate.
    """
    assert wo_materials_endpoint.assert_part_is_tieable_material is assert_part_is_tieable_material
    assert work_orders_endpoint.assert_part_is_tieable_material is assert_part_is_tieable_material


# =========================================================================== #
# 2. THE SECOND HALF — may a TIED part stop being tieable?
# =========================================================================== #


def _part(part_type, *, part_id: int = 1, part_number: str = "MTG-CONV-1") -> Part:
    part = Part(part_number=part_number, name="Subject", part_type=part_type)
    part.id = part_id
    return part


class _FakeQuery:
    """The exact chain ``live_tie_work_order_ids`` makes, and nothing else.

    Hand-rolled rather than mocked so the shape of the query is part of the assertion:
    if the implementation stops going through ``tenant_query`` — the invariant-1 helper —
    stops joining the work order, or starts counting rows instead of distinct work orders,
    this stops resolving.
    """

    def __init__(self, rows, seen):
        self._rows = rows
        self._seen = seen

    def join(self, model, *_args):
        self._seen.setdefault("joined", []).append(model)
        return self

    def with_entities(self, *_args):
        return self

    def filter(self, *_args):
        return self

    def distinct(self):
        return self

    def all(self):
        return self._rows


def _patched(monkeypatch, rows):
    """Point the gate's tenant helpers at a fixed row set and record their arguments.

    BOTH helpers are recorded. The tie count reads two tenant tables now — the allocation
    and the work order it hangs off — and invariant 1 is only satisfied if both are scoped,
    so a test that watched only ``tenant_query`` would pass on a query that joined
    ``work_orders`` company-wide.
    """
    seen = {}

    def fake_tenant_query(db, model, company_id):
        seen["model"] = model
        seen["company_id"] = company_id
        return _FakeQuery(rows, seen)

    def fake_tenant_filter(query, model, company_id):
        seen.setdefault("filtered", []).append((model, company_id))
        return query

    monkeypatch.setattr(gate, "tenant_query", fake_tenant_query)
    monkeypatch.setattr(gate, "tenant_filter", fake_tenant_filter)
    return seen


def test_the_conversion_is_refused_only_when_live_ties_stand(monkeypatch):
    """All THREE conditions, and the 409 they produce."""
    seen = _patched(monkeypatch, [(7,), (9,)])
    part = _part(PartType.RAW_MATERIAL)

    with pytest.raises(HTTPException) as excinfo:
        gate.assert_part_type_change_allowed(object(), part, PartType.ASSEMBLY, company_id=42)

    assert excinfo.value.status_code == status.HTTP_409_CONFLICT, "a conflict with existing STATE, not a bad payload"
    detail = excinfo.value.detail
    assert isinstance(detail, str), "a plain string, so every client renders it verbatim"
    assert "MTG-CONV-1" in detail
    assert "2 unfinished work orders still tie" in detail
    assert "Untie those work orders first" in detail, "the sentence names the remedy"
    assert seen["company_id"] == 42, "the tie count is TENANT-SCOPED (invariant 1)"
    assert seen["filtered"] == [(WorkOrder, 42)], "and so is the work order it joins (invariant 1, both sides)"


def test_an_untied_part_converts_and_the_refusal_helper_returns_None(monkeypatch):
    _patched(monkeypatch, [])
    part = _part(PartType.RAW_MATERIAL)

    assert gate.part_type_change_refusal(object(), part, PartType.MANUFACTURED, company_id=1) is None
    gate.assert_part_type_change_allowed(object(), part, PartType.MANUFACTURED, company_id=1)


@pytest.mark.parametrize(
    "current, requested",
    [
        # Engineering -> engineering: an ordinary manufactured/assembly edit.
        (PartType.MANUFACTURED, PartType.ASSEMBLY),
        # Engineering -> material: the direction that REMOVES the hazard.
        (PartType.ASSEMBLY, PartType.RAW_MATERIAL),
        # Material -> material: reclassifying a bought item, still tieable either way.
        (PartType.PURCHASED, PartType.RAW_MATERIAL),
        # No-op restatements, from both sides.
        (PartType.RAW_MATERIAL, PartType.RAW_MATERIAL),
        (PartType.MANUFACTURED, PartType.MANUFACTURED),
    ],
)
def test_every_direction_but_material_to_produced_passes_even_with_ties(monkeypatch, current, requested):
    """Only the direction that CREATES the hazard is gated, ties or no ties.

    Deliberately run with a non-empty tie set, so a gate that had widened into "a tied
    part's type is frozen" fails here — that would strand exactly the legacy rows a planner
    needs to correct, including a produced part that legacy ties still point at.
    """
    _patched(monkeypatch, [(7,)])

    assert gate.part_type_change_refusal(object(), _part(current), requested, company_id=1) is None


def test_the_count_is_of_DISTINCT_work_orders_and_reads_singular_at_one(monkeypatch):
    """One job holding several operation-scoped ties is ONE thing to untie.

    A laser work order carries a tie per nest operation, so a row count would tell a
    planner to go and fix twelve things when there is one job — a remedy sentence that is
    not true is worse than none.
    """
    _patched(monkeypatch, [(7,)])
    part = _part(PartType.PURCHASED)

    refusal = gate.part_type_change_refusal(object(), part, PartType.MANUFACTURED, company_id=1)
    assert refusal is not None
    assert "1 unfinished work order still ties" in refusal
    assert "Untie that work order first" in refusal


def test_the_tie_count_reads_the_allocation_table_JOINED_to_the_work_order_both_scoped(monkeypatch):
    """Two tenant tables, two scopes — invariant 1 on both sides of the join.

    The count cannot be answered from ``work_order_material_allocations`` alone any more:
    whether a tie is live depends on its work order's status, so the query joins
    ``work_orders``. That join is the half a reader is most likely to leave unscoped, and
    an unscoped one would let another company's work order decide this company's answer.
    """
    seen = _patched(monkeypatch, [(3,), (3,), (4,)])

    assert gate.live_tie_work_order_ids(object(), _part(PartType.RAW_MATERIAL), company_id=9) == {3, 4}
    assert seen["model"] is WorkOrderMaterialAllocation
    assert seen["company_id"] == 9
    assert seen["joined"] == [WorkOrder], "the work order is JOINED, not inferred"
    assert seen["filtered"] == [(WorkOrder, 9)], "and scoped with tenant_filter, same company"


def test_the_terminal_statuses_come_from_the_shared_constant(monkeypatch):
    """``TERMINAL_WO_STATUSES`` is imported, never re-declared here.

    The set is the single source of truth for "this work order has finished its lifecycle"
    and it has been wrong before by omission — CANCELLED was the value guards kept
    forgetting when they spelled the set out as COMPLETE/CLOSED. A local copy in this
    module would re-open exactly that: a CANCELLED job's ties would keep refusing a
    conversion nothing would ever act on.
    """
    assert gate.TERMINAL_WO_STATUSES is TERMINAL_WO_STATUSES
    assert gate.TERMINAL_WO_STATUSES == {
        WorkOrderStatus.COMPLETE,
        WorkOrderStatus.CLOSED,
        WorkOrderStatus.CANCELLED,
    }


def test_the_raising_and_non_raising_halves_are_ONE_decision():
    """``assert_part_type_change_allowed`` is ``part_type_change_refusal`` plus a 409.

    Two shapes exist because the two conversion doors answer differently — a single-record
    PUT refuses, a multi-record import warns and carries on — and the whole point is that
    the DECISION and the sentence are not duplicated between them.
    """
    source = inspect.getsource(gate.assert_part_type_change_allowed)
    assert "part_type_change_refusal(" in source, "the assert delegates rather than re-deriving"


def test_the_tieability_predicate_backs_the_422_wrapper_and_the_duplicate_copier():
    """One predicate, three call sites — the module's own "a gate in one of two files" rule.

    ``part_is_tieable_material`` is what both the 422 assert and
    ``work_order_duplicate_service`` (which skips ``part_not_tieable`` instead of raising)
    ask. A copier that re-derived the scope from ``is_engineering_part_type`` would work on
    the day it was written and drift the first time the scope moved.
    """
    assert gate.part_is_tieable_material(Part(part_number="X", name="X", part_type=PartType.RAW_MATERIAL)) is True
    assert gate.part_is_tieable_material(Part(part_number="X", name="X", part_type=PartType.ASSEMBLY)) is False
    assert duplicate_service.part_is_tieable_material is gate.part_is_tieable_material


def test_the_RESTORE_seam_binds_the_same_predicate_and_the_same_skip_reason():
    """The fourth caller, and the only one that re-arms demand nobody re-named.

    ``reopen_allocations_cancelled_by_delete`` flips an EXISTING tie back to OPEN when a
    work-order soft delete is undone. It constructs no ``part_id``, so it never passes the
    two 422 doors — and the conversion gate cannot cover it either, because the delete
    CANCELs the ties that count would have seen. That makes DELETE → reclassify → RESTORE
    a three-verb bypass unless this seam asks the predicate itself.

    The REASON literal is shared for the same reason the predicate is: the duplicate's and
    the restore's envelopes are documented as reporting ``part_not_tieable`` identically,
    and two copies of a string is how that stops being true without anything failing.
    """
    assert consumption_service.part_is_tieable_material is gate.part_is_tieable_material
    assert consumption_service.PART_NOT_TIEABLE_REASON is gate.PART_NOT_TIEABLE_REASON
    assert duplicate_service.PART_NOT_TIEABLE_REASON is gate.PART_NOT_TIEABLE_REASON
    assert gate.PART_NOT_TIEABLE_REASON == "part_not_tieable", "the wire value is part of two API contracts"

    source = inspect.getsource(consumption_service.reopen_allocations_cancelled_by_delete)
    assert "part_is_tieable_material(" in source, "the seam asks the predicate rather than re-deriving the scope"
