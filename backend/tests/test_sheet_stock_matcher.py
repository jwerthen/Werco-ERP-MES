"""The deterministic sheet-stock matcher — what it picks, and what it refuses to.

Read `app/services/sheet_stock_matcher.py`'s docstring first. The short version:
this module's output is ADVISORY. It proposes a sheet part for a nest; the
planner confirms it in the review grid before Import; the tie that results is
what makes stock leave inventory when the nest's operation completes, into an
as-built record that never auto-reverses. A wrong tie depletes the wrong heat
lot and the remedy is a compensating transaction with a reason and an audit row.

So the tests that matter most here are the REFUSALS, not the matches. In
particular `TestTheMarginRefusal` is the single most important class in the
file: two identical sheets from different suppliers is the exact shape that
produces wrong-lot depletion, and the margin rule is the only thing standing
between it and a pre-filled picker a planner clicks past.

The other load-bearing properties, each with its own class below:

* thickness is a HARD gate, tight enough that two stocked gauges can never
  bridge (`TestTheThicknessGate`);
* a stated grade that disagrees DROPS the candidate — no string similarity
  anywhere (`TestTheAlloyGate`);
* an under-specified grade or an unreadable thickness forces a shortlist, never
  a pick (`TestTheAlloyGate`, `TestUnreadableNestData`);
* on-hand annotates and warns but NEVER ranks (`TestStockAnnotation`,
  `TestThePackagePass`);
* tenant isolation (`TestTenantIsolation`);
* and the whole thing is a PURE READ (`TestPurity`).
"""

from typing import Optional

import pytest
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part, PartType, UnitOfMeasure
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderType
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation

# The module is imported whole as well as by name, and the whole-module import is
# deliberate: the two GATE diagnostics (NEST_THICKNESS_UNREADABLE,
# ALLOY_UNDER_SPECIFIED) reach a caller only as `SheetSuggestion.diagnostic` — a
# sentence, not a code — so `sheet_stock_matcher._candidates_for_triple` is the
# only seam where the machine KEY itself can be asserted. Everything else in this
# file goes through the public `match_sheet_parts`.
from app.services import sheet_stock_matcher
from app.services.sheet_stock_matcher import (
    AUTO_FILL_MIN_MARGIN,
    AUTO_FILL_MIN_SCORE,
    MAX_CANDIDATES,
    STATUS_AMBIGUOUS,
    STATUS_MATCHED,
    STATUS_UNMATCHED,
    THICKNESS_TOLERANCE_IN,
    match_sheet_parts,
)

pytestmark = pytest.mark.integration

COMPANY_ID = 1
OTHER_COMPANY_ID = 2


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def other_company(db_session: Session) -> Company:
    """A second tenant, so cross-tenant leakage is testable rather than assumed."""
    company = Company(id=OTHER_COMPANY_ID, name="Other Machining", slug="other", is_active=True)
    db_session.add(company)
    db_session.commit()
    return company


@pytest.fixture
def material_part(db_session: Session):
    """Create a material-supply part. The part NUMBER carries the spec."""

    def _make(
        part_number: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        company_id: int = COMPANY_ID,
        part_type: PartType = PartType.RAW_MATERIAL,
        unit_of_measure: Optional[UnitOfMeasure] = UnitOfMeasure.SHEETS,
        is_active: bool = True,
    ) -> Part:
        part = Part(
            part_number=part_number,
            name=name or part_number,
            description=description,
            part_type=part_type,
            unit_of_measure=unit_of_measure,
            is_active=is_active,
            company_id=company_id,
        )
        db_session.add(part)
        db_session.commit()
        db_session.refresh(part)
        return part

    return _make


@pytest.fixture
def stock(db_session: Session):
    """Put a consumable lot of `quantity` on the rack for `part`."""

    def _make(
        part: Part,
        quantity: float,
        *,
        company_id: Optional[int] = None,
        status: str = "available",
        is_active: bool = True,
        lot_number: str = "LOT-1",
    ) -> InventoryItem:
        item = InventoryItem(
            part_id=part.id,
            location="RACK-A-01",
            warehouse="MAIN",
            quantity_on_hand=quantity,
            quantity_available=quantity,
            lot_number=lot_number,
            status=status,
            is_active=is_active,
            company_id=company_id if company_id is not None else part.company_id,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        return item

    return _make


@pytest.fixture
def tie_history(db_session: Session, test_work_center):
    """Record `times` past nest ties to `part` for one descriptor triple.

    Each tie needs its own operation: `laser_nests` is unique on
    `work_order_operation_id` and the allocation table's partial unique index
    allows one OPEN tie per (company, operation, part). That is also what makes
    the count meaningful — it is a count of nests, not of re-previews.
    """
    counter = {"n": 0}

    def _make(
        part: Part,
        *,
        material: str,
        thickness: str,
        sheet_size: str,
        times: int = 3,
        company_id: Optional[int] = None,
    ) -> None:
        company = company_id if company_id is not None else part.company_id
        counter["n"] += 1
        # A standalone laser-cutting WO: part-less, which is the only shape
        # `ck_work_orders_part_required_unless_laser` allows without a part, and
        # the shape a nest package actually imports into.
        work_order = WorkOrder(
            work_order_number=f"WO-HIST-{counter['n']:03d}",
            work_order_type=WorkOrderType.LASER_CUTTING.value,
            quantity_ordered=float(times),
            status="completed",
            company_id=company,
        )
        db_session.add(work_order)
        db_session.flush()

        package = LaserNestPackage(
            package_name=f"HIST-{counter['n']:03d}",
            child_work_order_id=work_order.id,
            company_id=company,
        )
        db_session.add(package)
        db_session.flush()

        for index in range(times):
            operation = WorkOrderOperation(
                work_order_id=work_order.id,
                work_center_id=test_work_center.id,
                sequence=(index + 1) * 10,
                name=f"LASER {index + 1}",
                company_id=company,
            )
            db_session.add(operation)
            db_session.flush()

            db_session.add(
                LaserNest(
                    package_id=package.id,
                    work_order_operation_id=operation.id,
                    nest_name=f"NEST-{counter['n']}-{index}",
                    cnc_file_name=f"NEST-{counter['n']}-{index}.nc",
                    planned_runs=1,
                    material=material,
                    thickness=thickness,
                    sheet_size=sheet_size,
                    company_id=company,
                )
            )
            db_session.add(
                WorkOrderMaterialAllocation(
                    work_order_id=work_order.id,
                    work_order_operation_id=operation.id,
                    part_id=part.id,
                    source=AllocationSource.NEST,
                    status=AllocationStatus.OPEN,
                    qty_per_run=1.0,
                    qty_planned=1.0,
                    unit_of_measure="sheets",
                    company_id=company,
                )
            )
        db_session.commit()

    return _make


def nest(
    source_file: str,
    *,
    material: Optional[str] = None,
    thickness: Optional[str] = None,
    sheet_size: Optional[str] = None,
    planned_runs=1,
) -> dict:
    """One row of an import preview, in the shape `match_sheet_parts` reads."""
    return {
        "source_file": source_file,
        "material": material,
        "thickness": thickness,
        "sheet_size": sheet_size,
        "planned_runs": planned_runs,
    }


def codes(candidate) -> set:
    return {diagnostic.code for diagnostic in candidate.diagnostics}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestAConfidentMatch:
    def test_exact_thickness_grade_and_size_scores_100_and_prefills(self, db_session, material_part):
        sheet = material_part("0.250-60X120-A36", name="A36 HR plate")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_MATCHED
        assert suggestion.auto_fill_part_id == sheet.id
        assert suggestion.candidates[0].score == 100.0
        assert suggestion.candidates[0].part_number == "0.250-60X120-A36"
        assert suggestion.candidates[0].basis == "deterministic"
        assert suggestion.diagnostic is None

    def test_the_reason_names_the_evidence_rather_than_a_confidence_number(self, db_session, material_part):
        material_part("0.250-60X120-A36", name="A36 HR plate")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        # A confidence number is not an artifact anyone can audit; a sentence is.
        reason = result["NEST-01.pdf"].candidates[0].reason
        assert "0.250" in reason
        assert "A36" in reason
        assert "60x120" in reason

    def test_dimension_order_does_not_matter(self, db_session, material_part):
        """A nest reading 120x60 and a part numbered ...-60X120 are one sheet."""
        sheet = material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="120x60")],
        )

        assert result["NEST-01.pdf"].auto_fill_part_id == sheet.id
        assert result["NEST-01.pdf"].candidates[0].score == 100.0


# ---------------------------------------------------------------------------
# THE MARGIN REFUSAL — the case that produces wrong-lot depletion
# ---------------------------------------------------------------------------


class TestTheMarginRefusal:
    def test_two_identical_sheets_from_different_suppliers_are_never_prefilled(self, db_session, material_part, stock):
        """THE most important test in this file.

        Two `0.250 A36 60X120` sheets from different suppliers both score a
        perfect 100.0. The data does not identify ONE of them, and a machine that
        picks anyway picks a heat lot — which is a manufacturing decision made by
        a tiebreak. So neither is pre-filled and BOTH are shown, and the planner
        makes the call the data cannot.
        """
        central = material_part("0.250-60X120-A36-CENTRAL", name="A36 HR plate (Central Steel)")
        ryerson = material_part("0.250-60X120-A36-RYERSON", name="A36 HR plate (Ryerson)")
        # Stock is deliberately lopsided: on-hand must not break the tie either.
        stock(central, 0.0)
        stock(ryerson, 40.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None

        scores = {candidate.part_id: candidate.score for candidate in suggestion.candidates}
        assert scores == {central.id: 100.0, ryerson.id: 100.0}, "both must survive, at an identical score"

        # The refusal names both, so the planner sees what the tie was between.
        assert suggestion.diagnostic is not None
        assert "0.250-60X120-A36-CENTRAL" in suggestion.diagnostic
        assert "0.250-60X120-A36-RYERSON" in suggestion.diagnostic

    def test_a_runner_up_inside_the_margin_refuses_even_when_the_best_clears_the_score_bar(
        self, db_session, material_part
    ):
        """95.0 vs 100.0 is a 5-point gap — inside the 8-point margin."""
        material_part("0.250-60X120-A36")  # exact grade -> 100.0
        material_part("0.250-60X120-CS")  # equivalent steel -> 95.0

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert [candidate.score for candidate in suggestion.candidates] == [100.0, 95.0]
        assert 100.0 - 95.0 < AUTO_FILL_MIN_MARGIN
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None

    def test_a_runner_up_outside_the_margin_still_prefills(self, db_session, material_part):
        """The margin rule must not refuse everything that has company."""
        best = material_part("0.250-60X120-A36")
        material_part("0.250-48X96-A36")  # size conflict -> 85.0

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert [candidate.score for candidate in suggestion.candidates] == [100.0, 85.0]
        assert 100.0 - 85.0 >= AUTO_FILL_MIN_MARGIN
        assert suggestion.status == STATUS_MATCHED
        assert suggestion.auto_fill_part_id == best.id

    def test_the_shortlist_is_capped(self, db_session, material_part):
        for index in range(MAX_CANDIDATES + 3):
            material_part(f"0.250-60X120-A36-S{index}")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_AMBIGUOUS
        assert len(suggestion.candidates) == MAX_CANDIDATES
        # Deterministic order, so two previews of one package never disagree.
        assert [c.part_number for c in suggestion.candidates] == sorted(c.part_number for c in suggestion.candidates)


# ---------------------------------------------------------------------------
# Gate A — thickness
# ---------------------------------------------------------------------------


class TestTheThicknessGate:
    def test_a_gauge_nest_matches_its_decimal_part_number(self, db_session, material_part):
        """`10ga` and `0.1345` are the same sheet, written two ways."""
        sheet = material_part("0.1345-60X120-CS")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="CS", thickness="10ga", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].status == STATUS_MATCHED
        assert result["NEST-01.pdf"].auto_fill_part_id == sheet.id

    def test_rounding_between_a_gauge_and_a_keyed_decimal_is_absorbed(self, db_session, material_part):
        """12ga is 0.1046 and the rack tag says 0.105 — 0.0004 apart."""
        sheet = material_part("0.105-60X120-CS")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="CS", thickness="12ga", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].auto_fill_part_id == sheet.id

    def test_two_stocked_gauges_can_never_bridge(self, db_session, material_part):
        """11ga (0.1196) must never match a 10ga (0.1345) sheet.

        The tolerance is 3x tighter than the tightest real gap in the gauge
        table, so no pair of stocked gauges can be bridged by it. This is the
        property that keeps a nest off the wrong rack.
        """
        material_part("0.1345-60X120-CS")  # 10 ga

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="CS", thickness="11ga", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_UNMATCHED
        assert suggestion.candidates == []
        assert suggestion.auto_fill_part_id is None
        assert 0.1345 - 0.1196 > THICKNESS_TOLERANCE_IN

    def test_a_near_miss_outside_tolerance_is_dropped(self, db_session, material_part):
        material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.2525", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].status == STATUS_UNMATCHED
        assert result["NEST-01.pdf"].candidates == []


# ---------------------------------------------------------------------------
# Gate B — alloy
# ---------------------------------------------------------------------------


class TestTheAlloyGate:
    def test_a_disagreeing_stated_grade_drops_the_candidate(self, db_session, material_part, stock):
        """An A36 nest never matches a 304 sheet, however alone that 304 is.

        There is no string similarity anywhere in the matcher and `rapidfuzz` is
        deliberately not imported: "A36" and "A572" are two steels and they are
        one edit apart. Dropping is the whole behavior — the planner gets an
        empty shortlist and a picker, not a near-miss to click.
        """
        stainless = material_part("0.250-60X120-304", name="304 stainless plate")
        stock(stainless, 99.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_UNMATCHED
        assert suggestion.candidates == []
        assert suggestion.auto_fill_part_id is None

    def test_an_equivalent_grade_scores_95_and_still_prefills(self, db_session, material_part):
        """The nest report says CS; the rack is numbered A36. Same steel, same rack."""
        sheet = material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="CS", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.candidates[0].score == 95.0
        assert suggestion.candidates[0].score >= AUTO_FILL_MIN_SCORE
        assert suggestion.status == STATUS_MATCHED
        assert suggestion.auto_fill_part_id == sheet.id
        assert "same steel" in suggestion.candidates[0].reason

    def test_a_corrosion_relevant_substitution_is_not_an_equivalence(self, db_session, material_part):
        """304 and 316 are NOT interchangeable, and neither are 5052 and 6061.

        Leaving a real equivalence out of the table fails SOFT (one extra click);
        putting a wrong one in produces a confident wrong pre-fill.
        """
        material_part("0.250-60X120-316")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="304", thickness="0.250", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].status == STATUS_UNMATCHED

    def test_a_bare_ss_with_two_grades_in_the_rack_is_ambiguous_never_prefilled(self, db_session, material_part, stock):
        """`SS` is not a grade — it is a statement that the grade was not stated.

        The planner picks from a 2-item shortlist instead of 500 parts, which is
        the whole value on offer here; what is NOT on offer is the matcher
        deciding between 304 and 316 on its own.
        """
        grade_304 = material_part("0.125-60X120-304", name="304 stainless sheet")
        grade_316 = material_part("0.125-60X120-316", name="316 stainless sheet")
        stock(grade_304, 12.0)
        stock(grade_316, 0.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="SS", thickness="0.125", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None
        assert {c.part_id for c in suggestion.candidates} == {grade_304.id, grade_316.id}
        # Neither scores an alloy agreement: 60 base + 0 alloy + 15 size.
        assert {c.score for c in suggestion.candidates} == {75.0}
        assert "304" in suggestion.diagnostic and "316" in suggestion.diagnostic

        # The machine key, at the only seam that exposes it.
        _, diagnostics, alloy_ambiguous = sheet_stock_matcher._candidates_for_triple(
            "SS", "0.125", "60x120", _catalog_for(db_session)
        )
        assert alloy_ambiguous is True
        assert "ALLOY_UNDER_SPECIFIED" in {d.code for d in diagnostics}

    def test_a_nest_stating_no_grade_at_all_is_ambiguous_too(self, db_session, material_part):
        material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material=None, thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None
        assert len(suggestion.candidates) == 1  # still shortlisted, just not picked

    def test_stock_whose_own_grade_is_unstated_is_ranked_but_never_prefilled(self, db_session, material_part):
        """The nest names a grade and the part number does not. Not a conflict, not an agreement."""
        material_part("0.250-60X120", name="HR plate")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert [c.score for c in suggestion.candidates] == [75.0]
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None


# ---------------------------------------------------------------------------
# Soft component — sheet size
# ---------------------------------------------------------------------------


class TestSheetSizeScoring:
    def test_no_sheet_size_stated_caps_the_score_below_the_bar(self, db_session, material_part):
        """89.5 is deliberately half a point under 90.

        "Read the sheet size and THEN pick" is the framing this was built to, and
        a nest row that produced no size did not clear it.
        """
        material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size=None)],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.candidates[0].score == 89.5
        assert suggestion.candidates[0].score < AUTO_FILL_MIN_SCORE
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None

    def test_a_size_conflict_keeps_the_candidate_but_never_prefills(self, db_session, material_part):
        """A small nest can legitimately be cut from a bigger sheet, and the shop does that.

        So a disagreeing size is a RANKING penalty, not a gate — the candidate
        stays on the shortlist at 85.0 and the planner decides.
        """
        oversize = material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="48x96")],
        )

        suggestion = result["NEST-01.pdf"]
        assert [c.part_id for c in suggestion.candidates] == [oversize.id]
        assert suggestion.candidates[0].score == 85.0
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None
        assert "48x96" in suggestion.candidates[0].reason

    def test_one_matching_dimension_scores_94_and_does_prefill(self, db_session, material_part):
        """PINNING an asymmetry, not endorsing it.

        A nest that states ONE edge scores 94.0 and pre-fills. A nest that states
        NO size scores 89.5 and refuses. So partial size evidence pre-fills where
        absent size evidence does not — which is defensible (one confirmed edge
        IS evidence) but is the single softest pre-fill the model allows, and it
        is reached with a soft component the module itself calls "weaker, never
        stronger". Pinned here so moving `_SIZE_ONE_DIM` (0.6) or
        `AUTO_FILL_MIN_SCORE` (90.0) is a deliberate act with a failing test
        attached, rather than a silent change to what auto-fills.
        """
        sheet = material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.candidates[0].score == 94.0
        assert suggestion.candidates[0].score >= AUTO_FILL_MIN_SCORE
        assert suggestion.status == STATUS_MATCHED
        assert suggestion.auto_fill_part_id == sheet.id
        assert "one dimension matches" in suggestion.candidates[0].reason

    def test_a_single_dimension_that_matches_nothing_is_a_conflict(self, db_session, material_part):
        material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="97")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.candidates[0].score == 85.0
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None


# ---------------------------------------------------------------------------
# Unreadable input
# ---------------------------------------------------------------------------


class TestUnreadableNestData:
    def test_an_unreadable_thickness_never_prefills(self, db_session, material_part):
        """A bare "16" is 16 inches arithmetically and 16 ga to a human. It is neither.

        Gate A cannot be evaluated at all, so nothing is matched on spec — the
        matcher fails closed rather than picking the one sheet in the rack.
        """
        material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="16", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status in {STATUS_UNMATCHED, STATUS_AMBIGUOUS}
        assert suggestion.auto_fill_part_id is None
        assert suggestion.candidates == []
        assert suggestion.diagnostic is not None
        assert "16" in suggestion.diagnostic

        candidates, diagnostics, _ = sheet_stock_matcher._candidates_for_triple(
            "A36", "16", "60x120", _catalog_for(db_session)
        )
        assert candidates == []
        assert "NEST_THICKNESS_UNREADABLE" in {d.code for d in diagnostics}

    @pytest.mark.parametrize("thickness", [None, "", "   ", "as noted", "9ga"])
    def test_every_unreadable_thickness_fails_closed(self, db_session, material_part, thickness):
        material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness=thickness, sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].auto_fill_part_id is None
        assert result["NEST-01.pdf"].candidates == []


# ---------------------------------------------------------------------------
# Tenant isolation (invariant 1)
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_another_companys_part_is_never_returned(self, db_session, material_part, stock, other_company):
        """The other tenant's rack is a perfect match and must still be invisible."""
        theirs = material_part("0.250-60X120-A36", company_id=OTHER_COMPANY_ID)
        stock(theirs, 100.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_UNMATCHED
        assert suggestion.candidates == []
        assert suggestion.auto_fill_part_id != theirs.id
        assert theirs.id not in {c.part_id for c in suggestion.candidates}

    def test_a_cross_tenant_twin_does_not_create_a_false_ambiguity(self, db_session, material_part, other_company):
        """The sharper form of the same assertion.

        Both tenants stock `0.250-60X120-A36`. Unscoped, the twin would tie the
        margin rule at 0 and this row would come back AMBIGUOUS — so a passing
        `matched` here proves the scoping ran, not merely that nothing leaked
        into a list.
        """
        ours = material_part("0.250-60X120-A36", company_id=COMPANY_ID)
        theirs = material_part("0.250-60X120-A36", company_id=OTHER_COMPANY_ID)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_MATCHED
        assert suggestion.auto_fill_part_id == ours.id
        assert [c.part_id for c in suggestion.candidates] == [ours.id]
        assert theirs.id not in {c.part_id for c in suggestion.candidates}

    def test_the_other_tenant_still_sees_its_own_sheet(self, db_session, material_part, other_company):
        """Isolation, not suppression — the fixture has to be real on both sides."""
        theirs = material_part("0.250-60X120-A36", company_id=OTHER_COMPANY_ID)

        result = match_sheet_parts(
            db_session,
            company_id=OTHER_COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].auto_fill_part_id == theirs.id

    def test_another_companys_stock_never_annotates_our_candidate(
        self, db_session, material_part, stock, other_company
    ):
        ours = material_part("0.250-60X120-A36", company_id=COMPANY_ID)
        # A lot of the same part number, on the other tenant's rack.
        theirs = material_part("0.250-60X120-A36", company_id=OTHER_COMPANY_ID)
        stock(theirs, 500.0)
        stock(ours, 0.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        top = result["NEST-01.pdf"].candidates[0]
        assert top.on_hand == 0.0
        assert top.stock_state == "none"


# ---------------------------------------------------------------------------
# Stock annotation — it warns, it never ranks
# ---------------------------------------------------------------------------


class TestStockAnnotation:
    def test_zero_on_hand_still_prefills(self, db_session, material_part, stock):
        """Refusing to tie a right-spec sheet ships the nest UNTIED.

        That is precisely the failure this feature exists to close: 65 of 74
        completed nests carried no tie in July 2026 and their sheet metal never
        left stock. An empty rack is a purchasing problem, not a matching one.
        """
        sheet = material_part("0.250-60X120-A36")
        stock(sheet, 0.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120", planned_runs=3)],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_MATCHED
        assert suggestion.auto_fill_part_id == sheet.id

        top = suggestion.candidates[0]
        assert top.stock_state == "none"
        assert top.on_hand == 0.0
        assert top.on_hand_known is True
        assert top.demand == 3.0
        assert "NO_STOCK_ON_HAND" in codes(top)

    def test_an_empty_rack_points_at_a_stocked_alternative(self, db_session, material_part, stock):
        best = material_part("0.250-60X120-A36")
        alternate = material_part("0.250-48X96-A36")  # size conflict -> 85.0, 15 back
        stock(best, 0.0)
        stock(alternate, 5.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        # On-hand annotates; it does NOT re-rank. The empty sheet still wins.
        assert suggestion.auto_fill_part_id == best.id
        top = suggestion.candidates[0]
        assert {"NO_STOCK_ON_HAND", "ALTERNATE_WITH_STOCK"} <= codes(top)
        assert alternate.part_number in " ".join(d.detail for d in top.diagnostics)

    def test_held_and_inactive_lots_are_not_counted_as_on_hand(self, db_session, material_part, stock):
        """The engine will not draw from these, so the preview must not promise them."""
        sheet = material_part("0.250-60X120-A36")
        stock(sheet, 10.0, status="quarantine", lot_number="LOT-HELD")
        stock(sheet, 7.0, is_active=False, lot_number="LOT-DEAD")
        stock(sheet, 2.0, lot_number="LOT-GOOD")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].candidates[0].on_hand == 2.0

    def test_covered_stock_carries_no_shortage_diagnostic(self, db_session, material_part, stock):
        sheet = material_part("0.250-60X120-A36")
        stock(sheet, 10.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120", planned_runs=4)],
        )

        top = result["NEST-01.pdf"].candidates[0]
        assert top.stock_state == "covered"
        assert top.projected_on_hand == 6.0
        assert codes(top) == set()


# ---------------------------------------------------------------------------
# The package pass — cumulative demand across the grid
# ---------------------------------------------------------------------------


class TestThePackagePass:
    def test_twelve_nests_against_eight_sheets_marks_the_later_rows_short(self, db_session, material_part, stock):
        """Visible at review time instead of at completion, when the lot goes negative."""
        sheet = material_part("0.250-60X120-A36")
        stock(sheet, 8.0)

        files = [f"NEST-{index:02d}.pdf" for index in range(1, 13)]
        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest(f, material="A36", thickness="0.250", sheet_size="60x120", planned_runs=1) for f in files],
        )

        states = [result[f].candidates[0].stock_state for f in files]
        assert states == ["covered"] * 8 + ["short"] * 4

        for f in files[8:]:
            assert "PACKAGE_DEMAND_EXCEEDS_STOCK" in codes(result[f].candidates[0])
        for f in files[:8]:
            assert "PACKAGE_DEMAND_EXCEEDS_STOCK" not in codes(result[f].candidates[0])

    def test_the_package_pass_never_changes_which_part_is_claimed(self, db_session, material_part, stock):
        """Row 30 must not get a different part than row 1 for the same spec.

        If it did, one physical sheet spec would split across two part numbers
        inside a single as-built record.
        """
        sheet = material_part("0.250-60X120-A36")
        alternate = material_part("0.250-48X96-A36")
        stock(sheet, 8.0)
        stock(alternate, 500.0)

        files = [f"NEST-{index:02d}.pdf" for index in range(1, 13)]
        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest(f, material="A36", thickness="0.250", sheet_size="60x120", planned_runs=1) for f in files],
        )

        assert {result[f].auto_fill_part_id for f in files} == {sheet.id}
        assert {result[f].status for f in files} == {STATUS_MATCHED}
        assert {result[f].candidates[0].part_id for f in files} == {sheet.id}

    def test_each_row_carries_its_own_demand(self, db_session, material_part, stock):
        """Two nests sharing a triple share a spec, but not a demand."""
        sheet = material_part("0.250-60X120-A36")
        stock(sheet, 100.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[
                nest("A.pdf", material="A36", thickness="0.250", sheet_size="60x120", planned_runs=2),
                nest("B.pdf", material="A36", thickness="0.250", sheet_size="60x120", planned_runs=5),
            ],
        )

        assert result["A.pdf"].candidates[0].demand == 2.0
        assert result["B.pdf"].candidates[0].demand == 5.0
        assert result["A.pdf"].candidates[0].projected_on_hand == 98.0
        assert result["B.pdf"].candidates[0].projected_on_hand == 93.0


# ---------------------------------------------------------------------------
# PURITY — a preview is not an actor and records no intent
# ---------------------------------------------------------------------------


class TestPurity:
    def test_match_sheet_parts_writes_nothing(self, db_session, material_part, stock, test_user: User):
        """No ledger row, no audit row, no mutation. Structurally, not by convention.

        The same property `GET /parts/{id}/backflush-readiness` holds, and for the
        same reason: this runs from a GET where there is no actor and no reason,
        so anything it wrote would be an unattributable state change in an
        AS9100D record.
        """
        sheet = material_part("0.250-60X120-A36")
        item = stock(sheet, 8.0)

        # Pre-existing rows, so the assertion is "unchanged", not "table empty".
        db_session.add(
            AuditLog(
                sequence_number=1,
                integrity_hash="x" * 64,
                action="CREATE",
                resource_type="part",
                resource_id=sheet.id,
                company_id=COMPANY_ID,
            )
        )
        db_session.add(
            InventoryTransaction(
                part_id=sheet.id,
                inventory_item_id=item.id,
                transaction_type=TransactionType.RECEIVE,
                quantity=8.0,
                created_by=test_user.id,
                company_id=COMPANY_ID,
            )
        )
        db_session.commit()

        audit_before = db_session.query(AuditLog).count()
        ledger_before = db_session.query(InventoryTransaction).count()
        on_hand_before = item.quantity_on_hand

        match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[
                nest(f"NEST-{i:02d}.pdf", material="A36", thickness="0.250", sheet_size="60x120", planned_runs=3)
                for i in range(1, 6)
            ],
        )

        db_session.expire_all()
        assert db_session.query(AuditLog).count() == audit_before == 1
        assert db_session.query(InventoryTransaction).count() == ledger_before == 1
        # The projection claims stock in memory only; the rack is untouched.
        assert db_session.get(InventoryItem, item.id).quantity_on_hand == on_hand_before == 8.0

    def test_a_short_package_still_writes_nothing(self, db_session, material_part, stock):
        """The shortage path is the one most likely to grow a write. It must not."""
        sheet = material_part("0.250-60X120-A36")
        stock(sheet, 1.0)

        match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[
                nest(f"NEST-{i:02d}.pdf", material="A36", thickness="0.250", sheet_size="60x120") for i in range(1, 11)
            ],
        )

        db_session.expire_all()
        assert db_session.query(AuditLog).count() == 0
        assert db_session.query(InventoryTransaction).count() == 0
        assert db_session.query(InventoryItem).filter_by(part_id=sheet.id).one().quantity_on_hand == 1.0


# ---------------------------------------------------------------------------
# Tie history — corroboration, and DEMOTE-ONLY
# ---------------------------------------------------------------------------


class TestTieHistory:
    """History can add doubt or context. It can never promote anything TO a pre-fill.

    Three repetitions of one mistake are indistinguishable from three correct
    decisions, and on a shop that has tied nine nests total the table is empty on
    day one anyway. So the deterministic spec path carries the feature and this
    path only ever demotes — which is exactly what makes it safe to feed a
    pre-fill decision at all.
    """

    def test_history_agreeing_with_the_spec_annotates_and_keeps_the_prefill(
        self, db_session, material_part, tie_history
    ):
        sheet = material_part("0.250-60X120-A36")
        tie_history(sheet, material="A36", thickness="0.250", sheet_size="60x120", times=3)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_MATCHED
        assert suggestion.auto_fill_part_id == sheet.id
        assert suggestion.candidates[0].prior_tie_count == 3
        assert "3 nests" in suggestion.candidates[0].reason

    def test_history_disagreeing_with_the_spec_demotes_out_of_prefill(self, db_session, material_part, tie_history):
        """The spec says one sheet, the planners have said another one three times.

        That is a genuine disagreement about a heat lot, and neither side gets to
        settle it silently: the row drops to `ambiguous`, the historical part is
        surfaced at rank 1 with `basis="history"` and NO score, and the planner
        is told what disagrees with what.
        """
        spec_match = material_part("0.250-60X120-A36")
        what_they_actually_use = material_part("SHT-A36-STD", name="A36 sheet, 1/4 stock")
        tie_history(what_they_actually_use, material="A36", thickness="0.250", sheet_size="60x120", times=4)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None

        top = suggestion.candidates[0]
        assert top.part_id == what_they_actually_use.id
        assert top.basis == "history"
        assert top.score == 0.0  # history is not a score, and must not read as one
        assert top.prior_tie_count == 4
        assert spec_match.id in {c.part_id for c in suggestion.candidates}
        assert "HISTORY_SPEC_DISAGREEMENT" in codes(top)
        assert spec_match.part_number in suggestion.diagnostic

    def test_history_never_promotes_an_unmatched_row_to_a_prefill(self, db_session, material_part, tie_history):
        """The strongest possible history — and still no pre-fill.

        No part in the catalog matches the spec at all, and planners have tied
        this exact spec to one sheet six times. The row becomes `ambiguous` (the
        planner gets the shortcut) and NEVER `matched`.
        """
        off_convention = material_part("SHT-A36-STD", name="A36 sheet")
        tie_history(off_convention, material="A36", thickness="0.250", sheet_size="60x120", times=6)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None
        assert suggestion.candidates[0].part_id == off_convention.id
        assert suggestion.candidates[0].basis == "history"

    def test_history_below_the_repetition_floor_is_ignored(self, db_session, material_part, tie_history):
        """Two ties is an anecdote. The floor is three."""
        spec_match = material_part("0.250-60X120-A36")
        other = material_part("SHT-A36-STD", name="A36 sheet")
        tie_history(other, material="A36", thickness="0.250", sheet_size="60x120", times=2)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].status == STATUS_MATCHED
        assert result["NEST-01.pdf"].auto_fill_part_id == spec_match.id

    def test_another_tenants_history_never_reaches_this_one(
        self, db_session, material_part, tie_history, other_company
    ):
        spec_match = material_part("0.250-60X120-A36", company_id=COMPANY_ID)
        theirs = material_part("SHT-A36-STD", name="A36 sheet", company_id=OTHER_COMPANY_ID)
        tie_history(
            theirs,
            material="A36",
            thickness="0.250",
            sheet_size="60x120",
            times=5,
            company_id=OTHER_COMPANY_ID,
        )

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_MATCHED
        assert suggestion.auto_fill_part_id == spec_match.id
        assert theirs.id not in {c.part_id for c in suggestion.candidates}

    def test_a_spec_written_differently_still_lands_in_the_same_history_bucket(
        self, db_session, material_part, tie_history
    ):
        """`spec_key` is read-time only, so it works on pre-normalization rows.

        The stored nests say `0.25` / `144x60`; the preview says `0.250` /
        `60x144`. Same physical sheet, so the same history bucket — with no
        backfill of records that are deliberately forward-only.
        """
        spec_match = material_part("0.250-60X144-A36")
        other = material_part("SHT-A36-STD", name="A36 sheet")
        tie_history(other, material="A36", thickness="0.25", sheet_size="144x60", times=3)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x144")],
        )

        suggestion = result["NEST-01.pdf"]
        assert suggestion.status == STATUS_AMBIGUOUS
        assert suggestion.auto_fill_part_id is None
        assert suggestion.candidates[0].part_id == other.id
        assert spec_match.id in {c.part_id for c in suggestion.candidates}


# ---------------------------------------------------------------------------
# It never raises
# ---------------------------------------------------------------------------


class TestItNeverRaises:
    def test_an_empty_nest_list_returns_an_empty_mapping(self, db_session, material_part):
        material_part("0.250-60X120-A36")
        assert match_sheet_parts(db_session, company_id=COMPANY_ID, nests=[]) == {}

    def test_rows_without_a_source_file_are_skipped_not_crashed_on(self, db_session, material_part):
        material_part("0.250-60X120-A36")

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[
                {"material": "A36", "thickness": "0.250"},
                {"source_file": None, "material": "A36"},
                nest("REAL.pdf", material="A36", thickness="0.250", sheet_size="60x120"),
            ],
        )

        assert set(result) == {"REAL.pdf"}

    def test_a_nest_row_missing_every_descriptor_does_not_raise(self, db_session, material_part):
        material_part("0.250-60X120-A36")

        result = match_sheet_parts(db_session, company_id=COMPANY_ID, nests=[{"source_file": "BARE.pdf"}])

        assert result["BARE.pdf"].status == STATUS_UNMATCHED
        assert result["BARE.pdf"].auto_fill_part_id is None

    @pytest.mark.parametrize("planned_runs", [None, "", "many", -4, 2.5])
    def test_an_unusable_run_count_does_not_raise(self, db_session, material_part, stock, planned_runs):
        sheet = material_part("0.250-60X120-A36")
        stock(sheet, 8.0)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[
                nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120", planned_runs=planned_runs)
            ],
        )

        assert result["NEST-01.pdf"].auto_fill_part_id == sheet.id

    def test_a_broken_catalog_does_not_raise(self, db_session, material_part):
        """Every shape of junk the parts table actually holds, in one catalog."""
        material_part("MISC")  # no spec at all
        material_part("HW-10-32", name="10-32 x 1/2 SHCS")  # grammar mis-reads the NAME
        material_part("0.250-60X120-A36", unit_of_measure=None)  # no UoM
        material_part("999999-99999X99999-A36")  # arithmetically valid, physically absurd
        material_part("0.250-60X120-A36-B", description=None, name="   ")  # blank-ish name
        material_part("0.250-60X120-A36-C", part_type=PartType.CONSUMABLE)
        material_part("0.250-60X120-A36-D", is_active=False)  # excluded by the catalog read

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[
                nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120"),
                nest("NEST-02.pdf", material="🜛 unknown", thickness="?", sheet_size="?x?"),
            ],
        )

        assert set(result) == {"NEST-01.pdf", "NEST-02.pdf"}
        # The inactive part is never offered.
        inactive = db_session.query(Part).filter(Part.part_number == "0.250-60X120-A36-D").one()
        assert inactive.id not in {c.part_id for c in result["NEST-01.pdf"].candidates}

    def test_a_soft_deleted_part_is_never_offered(self, db_session, material_part):
        sheet = material_part("0.250-60X120-A36")
        sheet.soft_delete(user_id=None)
        db_session.commit()

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].candidates == []

    def test_a_manufactured_part_is_never_offered_as_stock(self, db_session, material_part):
        """The catalog read is material-supply types only — you do not nest onto a weldment."""
        material_part("0.250-60X120-A36", part_type=PartType.MANUFACTURED)

        result = match_sheet_parts(
            db_session,
            company_id=COMPANY_ID,
            nests=[nest("NEST-01.pdf", material="A36", thickness="0.250", sheet_size="60x120")],
        )

        assert result["NEST-01.pdf"].candidates == []


# ---------------------------------------------------------------------------
# Helper used by the two diagnostic-code assertions above
# ---------------------------------------------------------------------------


def _catalog_for(db: Session):
    catalog, _truncated = sheet_stock_matcher._load_catalog(db, COMPANY_ID)
    return catalog


class TestHostileAndMalformedDescriptors:
    """Regression net for the review findings that could break a whole preview.

    All three were reproduced against the branch before the fix: the matcher
    quotes the nest's own AI-extracted descriptors, which carry no length bound
    and no type guarantee anywhere on their path.
    """

    def test_an_overlong_thickness_cannot_overflow_the_schema_cap(self, db_session, test_company):
        """A garbled thickness cell must not produce a >300-char diagnostic.

        The schema caps every diagnostic/reason at 300 and that validation runs
        OUTSIDE the endpoint's matcher guard, so an overflow here is a 500 on a
        42-nest upload that already burned minutes of AI extraction.
        """
        nests = [
            {
                "source_file": "n1.pdf",
                "material": "A36",
                "thickness": "see customer print revision C sheet two general note four " * 8,
                "sheet_size": "60x120",
                "planned_runs": 1,
            }
        ]
        result = match_sheet_parts(db_session, company_id=test_company.id, nests=nests)
        suggestion = result["n1.pdf"]
        assert suggestion.diagnostic is None or len(suggestion.diagnostic) <= 300
        for candidate in suggestion.candidates:
            assert len(candidate.reason) <= 300
            for diagnostic in candidate.diagnostics:
                assert len(diagnostic.detail) <= 300

    def test_an_overlong_material_and_sheet_size_cannot_overflow_either(self, db_session, test_company):
        nests = [
            {
                "source_file": "n1.pdf",
                "material": "STAINLESS " + ("grade per print " * 20),
                "thickness": "0.250",
                "sheet_size": "48x96 " + ("as noted on the traveler " * 12),
                "planned_runs": 1,
            }
        ]
        result = match_sheet_parts(db_session, company_id=test_company.id, nests=nests)
        suggestion = result["n1.pdf"]
        assert suggestion.diagnostic is None or len(suggestion.diagnostic) <= 300
        for candidate in suggestion.candidates:
            assert len(candidate.reason) <= 300

    def test_non_string_descriptors_do_not_cost_the_package_its_suggestions(self, db_session, test_company):
        """One bad row must not raise and take all 42 rows' suggestions with it.

        ``thickness`` as a JSON number is the single most likely wrong shape from
        the extractor; ``material`` as an object made the dedupe key unhashable.
        Both raised before the fix, and the endpoint guard is per-CALL, not
        per-row, so the whole package silently reverted to manual picking.
        """
        nests = [
            {
                "source_file": "bad-number.pdf",
                "material": "A36",
                "thickness": 0.25,
                "sheet_size": 60.0,
                "planned_runs": 1,
            },
            {
                "source_file": "bad-object.pdf",
                "material": {"grade": "A36"},
                "thickness": ["0.25"],
                "sheet_size": None,
                "planned_runs": 1,
            },
            {
                "source_file": "good.pdf",
                "material": "A36",
                "thickness": "0.250",
                "sheet_size": "60x120",
                "planned_runs": 1,
            },
        ]
        result = match_sheet_parts(db_session, company_id=test_company.id, nests=nests)
        assert set(result) == {"bad-number.pdf", "bad-object.pdf", "good.pdf"}
        # The healthy row is unaffected by its neighbours.
        assert result["good.pdf"].status in {STATUS_MATCHED, STATUS_AMBIGUOUS, STATUS_UNMATCHED}
