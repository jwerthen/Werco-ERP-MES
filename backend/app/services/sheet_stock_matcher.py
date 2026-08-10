"""Match a laser nest's read-off-the-sheet descriptors to a stock part.

The planner used to pick the sheet by hand for every nest in a package -- 42
combobox searches for one Miratech ZIP. This module answers that question
server-side so the review grid opens with the answer already in it.

WHAT THIS MODULE IS ALLOWED TO DECIDE
-------------------------------------
Tying a nest to a sheet part is not a display choice. The tie is what makes stock
leave inventory when the nest's operation completes (invariant 6), into an
as-built record that never auto-reverses. A wrong tie depletes the wrong heat lot
and the remedy is a compensating transaction with a reason and an audit row.

So this module REFUSES far more readily than it picks:

* Thickness is a HARD gate with a 0.002" tolerance -- no thickness agreement, no
  candidate, ever. Not a score component; a gate.
* A stated alloy that disagrees DROPS the candidate. There is no string-similarity
  anywhere in this file; `rapidfuzz` is deliberately not imported. "A36" and
  "A572" are two steels, and they are one edit apart.
* An under-specified alloy (`SS` with both 304 and 316 in the rack) or an
  unreadable thickness forces `ambiguous` -- the planner picks from a 2-item
  shortlist instead of 500 parts, and nothing is pre-filled.
* Two candidates within 8 points of each other means the data does not identify
  ONE sheet, so nothing is pre-filled and both are shown. This is the case that
  actually produces wrong-lot depletion, and it is the case the margin rule
  exists for.

PURE READ. No `AuditService`, no ledger row, no audit row, no event, no writes of
any kind -- the same structural property `GET /parts/{id}/backflush-readiness`
holds, and for the same reason: a preview is not an actor and records no intent.
The caller wraps this in try/except as well, but it also never raises.

ON-HAND IS NOT A RANKING INPUT
------------------------------
Stock annotates and warns; it never chooses. Ranking by "which lot happens to be
full" would let a purchasing accident decide a heat lot, which is a manufacturing
decision. A right-spec sheet with zero on hand is still returned, still ranked
first, and still auto-fills -- because refusing to tie it ships the nest untied,
which is exactly the failure this feature exists to close (65 of 74 completed
nests carried no tie in July 2026 and their sheet metal never left stock).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.inventory import InventoryItem
from app.models.laser_nest import LaserNest
from app.models.part import MATERIAL_SUPPLY_PART_TYPES, Part
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.material_consumption_service import CONSUMABLE_ITEM_CLAUSES
from app.services.sheet_stock_spec import (
    alloy_family,
    canonical_alloy,
    derive_sheet_spec,
    dims_inches,
    is_sheet_like,
    single_dim_inches,
    spec_key,
    thickness_inches,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables. Every one of these is a policy number, not an implementation detail;
# each carries the derivation that produced it so the next person can move it
# with an argument rather than a guess.
# ---------------------------------------------------------------------------

# The tightest adjacent pair in GAUGE_TO_INCHES is 24ga 0.0239 / 22ga 0.0299 --
# 0.006 apart. Every other pair is wider (12ga 0.1046 / 11ga 0.1196 = 0.015). At
# 0.002 this is 3x tighter than the tightest real gap, so it can NEVER bridge two
# stocked gauges, while still absorbing the rounding between a nest reading
# "12ga" (0.1046) and a part numbered `0.105-72X144-CS` (delta 0.0004).
THICKNESS_TOLERANCE_IN = 0.002

# Sheet dimensions are stated in whole or half inches; 0.01 absorbs formatting,
# not a different sheet.
DIM_TOLERANCE_IN = 0.01

# A candidate must clear this to be pre-filled. See `_score` for the realised
# values: an exact thickness + exact grade + exact size is 100.0, and a row whose
# sheet size could not be read tops out at 89.5 -- deliberately below the bar,
# because "read the sheet size and THEN pick" was the framing this was built to,
# and a row that produced no size did not clear it.
AUTO_FILL_MIN_SCORE = 90.0

# The runner-up must be this far back. Two `0.250-60X120-A36` sheets from
# different suppliers both score 100.0 with a margin of 0: the data does not
# identify one of them, so neither is pre-filled and both are shown.
AUTO_FILL_MIN_MARGIN = 8.0

# Worth offering in the shortlist even though it is not worth pre-filling.
SHORTLIST_MIN_SCORE = 60.0

MAX_CANDIDATES = 5

# Tie history is a corroborating signal, not a source of truth (see `_apply_history`).
HISTORY_WINDOW_DAYS = 365
HISTORY_MIN_TIES = 3
HISTORY_ROW_MAX = 2000

# Bounded catalog read. A tenant with more material parts than this gets a
# deterministic part_number-ordered prefix plus a CATALOG_TRUNCATED advisory on
# every row, so a partial list is never mistaken for a complete one.
CATALOG_MAX = 3000

# Alloys this shop treats as the same steel. SHOP POLICY ENCODED AS A CONSTANT,
# and the one place in this file where a wrong entry produces a wrong pre-fill on
# a fully-confident row. Two sets only, both about carbon steel naming:
#
#   * A36 is the structural grade; CS / HR / HRS / "mild steel" / "carbon steel"
#     are what the same rack gets called on a nest report.
#   * CRS / CR / "cold rolled" likewise.
#
# 304 and 316 are NOT here, and neither are 5052 and 6061. Those substitutions
# are corrosion- and forming-relevant and are a metallurgist's call, not a
# matcher's. Leaving a real equivalence OUT fails soft -- that descriptor
# degrades to a one-click shortlist instead of pre-filling.
ALLOY_EQUIVALENCE_SETS: Tuple[frozenset, ...] = (
    frozenset({"A36", "CS", "HRS", "HR"}),
    frozenset({"CRS", "CR"}),
)

# Score weights. The base is what surviving the thickness gate is worth on its
# own; the two soft components rank within that.
_BASE_SCORE = 60.0
_ALLOY_WEIGHT = 25.0
_SIZE_WEIGHT = 15.0

_ALLOY_EXACT = 1.0
_ALLOY_EQUIVALENT = 0.8
_ALLOY_UNKNOWN = 0.0

_SIZE_EXACT = 1.0
_SIZE_ONE_DIM = 0.6
_SIZE_ABSENT = 0.3
_SIZE_CONFLICT = 0.0

STATUS_MATCHED = "matched"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNMATCHED = "unmatched"

SEVERITY_GATE = "gate"
SEVERITY_ADVISORY = "advisory"


@dataclass
class MatchDiagnostic:
    """A machine key plus one sentence a planner can act on."""

    code: str
    severity: str
    detail: str


@dataclass
class CandidatePart:
    """One stock part the matcher believes could be this nest's sheet."""

    part_id: int
    part_number: str
    part_name: str
    unit_of_measure: Optional[str]
    score: float
    reason: str
    basis: str = "deterministic"
    on_hand: float = 0.0
    on_hand_known: bool = True
    demand: float = 0.0
    projected_on_hand: float = 0.0
    stock_state: str = "unknown"
    spec_thickness: Optional[str] = None
    spec_sheet_size: Optional[str] = None
    is_sheet_like: bool = True
    prior_tie_count: int = 0
    diagnostics: List[MatchDiagnostic] = field(default_factory=list)
    # Internal, never serialized: the alloy agreement that produced `score`.
    alloy_score: float = 0.0


@dataclass
class SheetSuggestion:
    """The matcher's advisory answer for one nest."""

    status: str
    auto_fill_part_id: Optional[int] = None
    candidates: List[CandidatePart] = field(default_factory=list)
    diagnostic: Optional[str] = None


@dataclass(frozen=True)
class _CatalogPart:
    """A material part reduced to what matching reads."""

    part_id: int
    part_number: str
    part_name: str
    unit_of_measure: Optional[str]
    thickness_text: Optional[str]
    size_text: Optional[str]
    thickness_in: Optional[float]
    dims: Optional[Tuple[float, float]]
    alloy: Optional[str]
    sheet_like: bool


def _alloys_equivalent(left: str, right: str) -> bool:
    return any(left in group and right in group for group in ALLOY_EQUIVALENCE_SETS)


def _load_catalog(db: Session, company_id: int) -> Tuple[List[_CatalogPart], bool]:
    """Every active material part in the tenant, reduced to a parsed spec.

    Reads the ORM row directly rather than going through ``materials.py``'s
    ``_part_to_response``, on purpose: a part number containing a space or an inch
    mark is invisible to the wizard's own ``/materials`` load but is still real
    stock a nest can be cut from, and the shortlist can surface it.
    """
    rows = (
        tenant_query(db, Part, company_id)
        .filter(
            Part.part_type.in_(MATERIAL_SUPPLY_PART_TYPES),
            Part.is_deleted == False,  # noqa: E712
            Part.is_active == True,  # noqa: E712
        )
        .with_entities(
            Part.id,
            Part.part_number,
            Part.name,
            Part.description,
            Part.unit_of_measure,
        )
        .order_by(Part.part_number)
        .limit(CATALOG_MAX + 1)
        .all()
    )
    truncated = len(rows) > CATALOG_MAX
    catalog: List[_CatalogPart] = []
    for row in rows[:CATALOG_MAX]:
        spec = derive_sheet_spec(row.part_number, row.name)
        uom = getattr(row.unit_of_measure, "value", row.unit_of_measure)
        catalog.append(
            _CatalogPart(
                part_id=row.id,
                part_number=row.part_number or "",
                part_name=row.name or "",
                unit_of_measure=str(uom) if uom is not None else None,
                thickness_text=spec.thickness,
                size_text=spec.sheet_size,
                thickness_in=thickness_inches(spec.thickness),
                dims=dims_inches(spec.sheet_size),
                # The grade lives in the part number's trailing segment
                # (`0.188-72X144-A36`) far more reliably than in the prose name.
                alloy=canonical_alloy(row.part_number) or canonical_alloy(row.name),
                sheet_like=is_sheet_like(row.part_number, row.name, row.description),
            )
        )
    return catalog, truncated


def _load_history(db: Session, company_id: int) -> Dict[str, Tuple[int, int]]:
    """What planners actually tied to each sheet spec, keyed by ``spec_key``.

    Returns ``{spec_key: (part_id, tie_count)}`` for specs tied at least
    ``HISTORY_MIN_TIES`` times inside the window.

    Both sides of the join are tenant-scoped (invariant 1). ``CANCELLED`` rows are
    excluded because a re-import cancels and rebuilds ties wholesale, so counting
    them would weight a single nest by how many times it was re-previewed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_WINDOW_DAYS)
    rows = (
        db.query(
            LaserNest.material,
            LaserNest.thickness,
            LaserNest.sheet_size,
            WorkOrderMaterialAllocation.part_id,
            func.count().label("tie_count"),
        )
        .join(
            LaserNest,
            LaserNest.work_order_operation_id == WorkOrderMaterialAllocation.work_order_operation_id,
        )
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            LaserNest.company_id == company_id,
            LaserNest.is_deleted == False,  # noqa: E712
            WorkOrderMaterialAllocation.work_order_operation_id.isnot(None),
            WorkOrderMaterialAllocation.source == AllocationSource.NEST,
            WorkOrderMaterialAllocation.status != AllocationStatus.CANCELLED,
            WorkOrderMaterialAllocation.created_at >= cutoff,
        )
        .group_by(
            LaserNest.material,
            LaserNest.thickness,
            LaserNest.sheet_size,
            WorkOrderMaterialAllocation.part_id,
        )
        .limit(HISTORY_ROW_MAX)
        .all()
    )

    best: Dict[str, Tuple[int, int]] = {}
    for material, thickness, sheet_size, part_id, tie_count in rows:
        if tie_count < HISTORY_MIN_TIES:
            continue
        key = spec_key(material, thickness, sheet_size)
        if key == "?|?|?":
            continue
        current = best.get(key)
        if current is None or tie_count > current[1]:
            best[key] = (part_id, int(tie_count))
    return best


def _load_on_hand(db: Session, company_id: int, part_ids: Sequence[int]) -> Tuple[Dict[int, float], bool]:
    """Consumable on-hand per part, and whether the read landed.

    ``CONSUMABLE_ITEM_CLAUSES`` is IMPORTED from the consumption engine rather
    than re-declared -- the same discipline ``material_tie_view`` follows -- so a
    number shown here can never promise stock the engine would refuse to draw.
    """
    if not part_ids:
        return {}, True
    try:
        rows = (
            db.query(InventoryItem.part_id, func.sum(InventoryItem.quantity_on_hand))
            .filter(
                InventoryItem.company_id == company_id,
                InventoryItem.part_id.in_(list(part_ids)),
                *CONSUMABLE_ITEM_CLAUSES,
            )
            .group_by(InventoryItem.part_id)
            .all()
        )
    except Exception:  # noqa: BLE001 - a failed stock read must not lose the match
        logger.warning("sheet-stock on-hand read failed; suggestions degrade to unknown stock", exc_info=True)
        return {}, False
    return {part_id: float(total or 0.0) for part_id, total in rows}, True


def _size_score(
    nest_dims: Optional[Tuple[float, float]],
    nest_single: Optional[float],
    cand_dims: Optional[Tuple[float, float]],
) -> float:
    """Sheet size as a SOFT ranking component -- never a gate.

    A candidate whose size disagrees is kept at 0.0 rather than dropped: a small
    nest can legitimately be cut from a bigger sheet, and the shop does that.
    """
    if nest_dims and cand_dims:
        if (
            abs(nest_dims[0] - cand_dims[0]) <= DIM_TOLERANCE_IN
            and abs(nest_dims[1] - cand_dims[1]) <= DIM_TOLERANCE_IN
        ):
            return _SIZE_EXACT
        return _SIZE_CONFLICT
    if nest_single is not None and cand_dims:
        if any(abs(nest_single - dim) <= DIM_TOLERANCE_IN for dim in cand_dims):
            return _SIZE_ONE_DIM
        return _SIZE_CONFLICT
    return _SIZE_ABSENT


def _score(alloy_score: float, size_score: float) -> float:
    """Every survivor already has an exact thickness, so the base is 60.

    Realised values: exact/exact/exact = 100.0; equivalent alloy + exact size =
    95.0; exact alloy + one dimension = 94.0; exact alloy + no size = 89.5;
    exact alloy + size conflict = 85.0; unknown alloy + exact size = 75.0.
    """
    return _BASE_SCORE + _ALLOY_WEIGHT * alloy_score + _SIZE_WEIGHT * size_score


def _reason_for(
    candidate: _CatalogPart,
    alloy_score: float,
    size_score: float,
    nest_material: Optional[str],
    nest_size: Optional[str],
) -> str:
    """One sentence naming the evidence. Never empty.

    A confidence number is not an artifact anyone can audit; a sentence is. A
    candidate whose reason would come out blank is dropped by the caller.
    """
    parts: List[str] = []
    if candidate.thickness_text:
        parts.append(f"{candidate.thickness_text} matches the nest's thickness")
    if alloy_score >= _ALLOY_EXACT and candidate.alloy:
        parts.append(f"grade {candidate.alloy} matches")
    elif alloy_score >= _ALLOY_EQUIVALENT and candidate.alloy:
        nest_alloy = canonical_alloy(nest_material) or (nest_material or "").strip()
        parts.append(f"nest says {nest_alloy}, this stock is {candidate.alloy} (same steel)")
    elif candidate.alloy:
        parts.append(f"grade not stated on the nest; this stock is {candidate.alloy}")

    if size_score >= _SIZE_EXACT and candidate.size_text:
        parts.append(f"sheet size {candidate.size_text} matches")
    elif size_score == _SIZE_CONFLICT and candidate.size_text and nest_size:
        parts.append(f"nest reads {nest_size} and this sheet is {candidate.size_text}")
    elif size_score == _SIZE_ONE_DIM and candidate.size_text:
        parts.append(f"one dimension matches {candidate.size_text}")
    elif candidate.size_text:
        parts.append(f"sheet size {candidate.size_text}")

    if not parts:
        return ""
    return "; ".join(parts) + "."


def _candidates_for_triple(
    material: Optional[str],
    thickness: Optional[str],
    sheet_size: Optional[str],
    catalog: Sequence[_CatalogPart],
) -> Tuple[List[CandidatePart], List[MatchDiagnostic], bool]:
    """Gate + score the whole catalog for one descriptor triple.

    Returns ``(ranked candidates, diagnostics, alloy_is_ambiguous)``.
    """
    diagnostics: List[MatchDiagnostic] = []

    nest_thickness_in = thickness_inches(thickness)
    if nest_thickness_in is None:
        # Gate A cannot be evaluated at all. Fail closed: no spec candidates.
        diagnostics.append(
            MatchDiagnostic(
                code="NEST_THICKNESS_UNREADABLE",
                severity=SEVERITY_GATE,
                detail=(
                    f'The nest\'s thickness "{thickness or ""}" could not be read as a decimal or a '
                    "stocked gauge, so no sheet was matched on spec."
                ).strip(),
            )
        )
        return [], diagnostics, False

    nest_alloy = canonical_alloy(material)
    nest_family = alloy_family(material)
    nest_dims = dims_inches(sheet_size)
    nest_single = single_dim_inches(sheet_size)

    # Gate A -- thickness. HARD.
    survivors = [
        part
        for part in catalog
        if part.thickness_in is not None and abs(part.thickness_in - nest_thickness_in) <= THICKNESS_TOLERANCE_IN
    ]

    # Gate B -- alloy. HARD, with a defined escape for under-specification.
    alloy_ambiguous = False
    gated: List[Tuple[_CatalogPart, float]] = []
    if nest_alloy is not None:
        for part in survivors:
            if part.alloy is None:
                # The nest names a grade and this stock does not. Not a conflict,
                # but not an agreement either -- rank it, never pre-fill on it.
                gated.append((part, _ALLOY_UNKNOWN))
            elif part.alloy == nest_alloy:
                gated.append((part, _ALLOY_EXACT))
            elif _alloys_equivalent(part.alloy, nest_alloy):
                gated.append((part, _ALLOY_EQUIVALENT))
            # else: a stated grade that disagrees. DROPPED.
    else:
        # No grade on the nest -- either a bare family (`SS`) or nothing at all.
        # Every thickness survivor stays, none of them scores an agreement, and
        # the status is forced to ambiguous below.
        gated = [(part, _ALLOY_UNKNOWN) for part in survivors]
        distinct_alloys = {part.alloy for part in survivors if part.alloy}
        alloy_ambiguous = True
        if nest_family and len(distinct_alloys) >= 2:
            diagnostics.append(
                MatchDiagnostic(
                    code="ALLOY_UNDER_SPECIFIED",
                    severity=SEVERITY_GATE,
                    detail=(
                        f'The nest says "{material}" without a grade, and the rack holds '
                        f"{', '.join(sorted(distinct_alloys))}. Pick the one this job runs."
                    ),
                )
            )
        else:
            diagnostics.append(
                MatchDiagnostic(
                    code="ALLOY_UNDER_SPECIFIED",
                    severity=SEVERITY_GATE,
                    detail="The nest report states no material grade, so the sheet was not matched on grade.",
                )
            )

    candidates: List[CandidatePart] = []
    for part, alloy_score in gated:
        size_score = _size_score(nest_dims, nest_single, part.dims)
        score = _score(alloy_score, size_score)
        if score < SHORTLIST_MIN_SCORE:
            continue
        reason = _reason_for(part, alloy_score, size_score, material, sheet_size)
        if not reason:
            # An unauditable proposal is not a proposal.
            continue
        candidates.append(
            CandidatePart(
                part_id=part.part_id,
                part_number=part.part_number,
                part_name=part.part_name,
                unit_of_measure=part.unit_of_measure,
                score=round(score, 1),
                reason=reason,
                spec_thickness=part.thickness_text,
                spec_sheet_size=part.size_text,
                is_sheet_like=part.sheet_like,
                alloy_score=alloy_score,
            )
        )

    # Deterministic order: score desc, then part_number, so two equal-scoring
    # parts always rank the same way across previews of the same package.
    candidates.sort(key=lambda c: (-c.score, c.part_number))
    return candidates, diagnostics, alloy_ambiguous


def _decide_status(
    candidates: List[CandidatePart],
    diagnostics: List[MatchDiagnostic],
    alloy_ambiguous: bool,
) -> SheetSuggestion:
    """Apply the five pre-fill conditions. Any failure means ambiguous, not a guess."""
    if not candidates:
        detail = next((d.detail for d in diagnostics if d.severity == SEVERITY_GATE), None)
        return SheetSuggestion(status=STATUS_UNMATCHED, candidates=[], diagnostic=detail)

    shortlist = candidates[:MAX_CANDIDATES]
    best = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    margin = best.score - runner_up.score if runner_up else float("inf")

    refusal: Optional[str] = None
    if alloy_ambiguous:
        refusal = next(
            (d.detail for d in diagnostics if d.code == "ALLOY_UNDER_SPECIFIED"),
            "The nest does not state a grade precisely enough to pick one sheet.",
        )
    elif best.score < AUTO_FILL_MIN_SCORE:
        refusal = (
            f"Closest match is {best.part_number}, but the nest report did not state enough "
            "(grade or sheet size) to pick it without a look."
        )
    elif margin < AUTO_FILL_MIN_MARGIN and runner_up is not None:
        refusal = (
            f"{best.part_number} and {runner_up.part_number} both fit this nest's spec. " "Pick the one this job runs."
        )
    elif best.alloy_score < _ALLOY_EQUIVALENT:
        refusal = f"{best.part_number} matches on thickness, but the grades were never confirmed against each other."
    elif not best.is_sheet_like:
        refusal = f"{best.part_number} matches on dimensions but does not read as sheet or plate stock."

    if refusal is not None:
        if margin < AUTO_FILL_MIN_MARGIN and runner_up is not None and not alloy_ambiguous:
            diagnostics.append(MatchDiagnostic(code="AMBIGUOUS_CANDIDATES", severity=SEVERITY_GATE, detail=refusal))
        return SheetSuggestion(status=STATUS_AMBIGUOUS, candidates=shortlist, diagnostic=refusal)

    return SheetSuggestion(status=STATUS_MATCHED, auto_fill_part_id=best.part_id, candidates=shortlist)


def _apply_history(
    suggestion: SheetSuggestion,
    history_part_id: Optional[int],
    history_ties: int,
    catalog_by_id: Dict[int, _CatalogPart],
) -> None:
    """Fold in what planners actually tied to this spec before. DEMOTE-ONLY.

    History can force a row OUT of pre-fill and into review, and it can lift a
    part the grammar cannot parse (`SHT-A36-STD`) into the shortlist. It can never
    promote anything TO pre-fill: three repetitions of one mistake are
    indistinguishable from three correct decisions, and on a shop that has tied 9
    nests total it would be empty on day one anyway. The deterministic spec path
    carries the feature; history only ever adds doubt or context.
    """
    if history_part_id is None:
        return

    top = suggestion.candidates[0] if suggestion.candidates else None
    if top is not None and top.part_id == history_part_id:
        top.prior_tie_count = history_ties
        top.reason = f"{top.reason} Planners have tied this sheet to {history_ties} nests of this spec."
        return

    # Disagreement. Demote and surface the historical part at rank 1.
    historical = catalog_by_id.get(history_part_id)
    if historical is None:
        return

    suggestion.candidates = [candidate for candidate in suggestion.candidates if candidate.part_id != history_part_id]
    suggestion.candidates.insert(
        0,
        CandidatePart(
            part_id=historical.part_id,
            part_number=historical.part_number,
            part_name=historical.part_name,
            unit_of_measure=historical.unit_of_measure,
            score=0.0,
            reason=f"Planners tied this sheet to {history_ties} nests of this spec.",
            basis="history",
            spec_thickness=historical.thickness_text,
            spec_sheet_size=historical.size_text,
            is_sheet_like=historical.sheet_like,
            prior_tie_count=history_ties,
        ),
    )
    suggestion.candidates = suggestion.candidates[:MAX_CANDIDATES]

    if suggestion.status == STATUS_MATCHED:
        suggestion.status = STATUS_AMBIGUOUS
        detail = (
            f"Spec points at {top.part_number if top else 'one sheet'}, but planners have tied "
            f"{history_ties} nests of this spec to {historical.part_number}."
        )
        suggestion.auto_fill_part_id = None
        suggestion.diagnostic = detail
        for candidate in suggestion.candidates:
            candidate.diagnostics.append(
                MatchDiagnostic(code="HISTORY_SPEC_DISAGREEMENT", severity=SEVERITY_ADVISORY, detail=detail)
            )
    elif suggestion.status == STATUS_UNMATCHED:
        suggestion.status = STATUS_AMBIGUOUS
        suggestion.diagnostic = (
            f"No sheet matched this nest's spec, but planners have tied {history_ties} nests "
            f"of it to {historical.part_number}."
        )


def _annotate_stock(
    suggestion: SheetSuggestion,
    on_hand_by_part: Dict[int, float],
    on_hand_known: bool,
    remaining: Dict[int, float],
    planned_runs: float,
) -> None:
    """Attach on-hand, this row's demand, and the package-cumulative projection.

    The projection walks rows in grid order and claims stock as it goes, so twelve
    nests wanting eight sheets is visible at review time instead of at completion
    when the lot goes negative. It NEVER re-ranks: row 30 must not get a different
    part than row 1 for the same spec, or one physical sheet splits across two
    part numbers in an as-built record.
    """
    for candidate in suggestion.candidates:
        candidate.on_hand_known = on_hand_known
        candidate.on_hand = on_hand_by_part.get(candidate.part_id, 0.0) if on_hand_known else 0.0

    claimed = suggestion.auto_fill_part_id
    if claimed is None and suggestion.candidates:
        claimed = suggestion.candidates[0].part_id
    if claimed is None:
        return

    top = next((c for c in suggestion.candidates if c.part_id == claimed), None)
    if top is None:
        return

    # qty_per_run defaults to 1 sheet per run at preview time; the planner can
    # change it in the grid before importing.
    top.demand = float(planned_runs or 0.0)

    if not on_hand_known:
        top.stock_state = "unknown"
        top.diagnostics.append(
            MatchDiagnostic(
                code="ON_HAND_UNKNOWN",
                severity=SEVERITY_ADVISORY,
                detail="Stock levels could not be read, so shortage was not checked.",
            )
        )
        return

    starting = on_hand_by_part.get(claimed, 0.0)
    remaining[claimed] = remaining.get(claimed, starting) - top.demand
    top.projected_on_hand = remaining[claimed]

    uom = top.unit_of_measure or "EA"
    if starting <= 0:
        top.stock_state = "none"
        top.diagnostics.append(
            MatchDiagnostic(
                code="NO_STOCK_ON_HAND",
                severity=SEVERITY_ADVISORY,
                detail=(
                    f"{top.part_number} is the right sheet but has 0 {uom} on hand; " f"this nest needs {top.demand:g}."
                ),
            )
        )
    elif remaining[claimed] < 0:
        top.stock_state = "short"
        top.diagnostics.append(
            MatchDiagnostic(
                code="PACKAGE_DEMAND_EXCEEDS_STOCK",
                severity=SEVERITY_ADVISORY,
                detail=(
                    f"This package needs more {top.part_number} than the {starting:g} {uom} on hand; "
                    f"short by {abs(remaining[claimed]):g}."
                ),
            )
        )
    else:
        top.stock_state = "covered"

    if top.stock_state in {"none", "short"}:
        alternate = next(
            (c for c in suggestion.candidates if c.part_id != top.part_id and on_hand_by_part.get(c.part_id, 0.0) > 0),
            None,
        )
        if alternate is not None:
            top.diagnostics.append(
                MatchDiagnostic(
                    code="ALTERNATE_WITH_STOCK",
                    severity=SEVERITY_ADVISORY,
                    detail=(
                        f"{alternate.part_number} has {on_hand_by_part.get(alternate.part_id, 0.0):g} "
                        f"{alternate.unit_of_measure or 'EA'} on hand and also fits this nest."
                    ),
                )
            )


def match_sheet_parts(
    db: Session,
    *,
    company_id: int,
    nests: Iterable[dict],
) -> Dict[str, SheetSuggestion]:
    """Suggest a sheet-stock part for each nest in a preview, keyed by ``source_file``.

    Three queries per package regardless of nest count: the catalog, the tie
    history, and on-hand for the parts that survived gating. Nests are deduped by
    their descriptor triple first, so a 42-nest Miratech package runs the gates
    one to three times rather than 42.
    """
    rows = [nest for nest in nests if nest.get("source_file")]
    if not rows:
        return {}

    catalog, truncated = _load_catalog(db, company_id)
    catalog_by_id = {part.part_id: part for part in catalog}

    try:
        history = _load_history(db, company_id)
    except Exception:  # noqa: BLE001 - history is corroboration, never a prerequisite
        logger.warning("sheet-stock tie-history read failed; matching continues on spec alone", exc_info=True)
        history = {}

    # One evaluation per distinct descriptor triple.
    by_triple: Dict[Tuple[Optional[str], Optional[str], Optional[str]], SheetSuggestion] = {}
    for row in rows:
        triple = (row.get("material"), row.get("thickness"), row.get("sheet_size"))
        if triple in by_triple:
            continue
        candidates, diagnostics, alloy_ambiguous = _candidates_for_triple(triple[0], triple[1], triple[2], catalog)
        suggestion = _decide_status(candidates, diagnostics, alloy_ambiguous)
        key = spec_key(triple[0], triple[1], triple[2])
        history_entry = history.get(key)
        _apply_history(
            suggestion,
            history_entry[0] if history_entry else None,
            history_entry[1] if history_entry else 0,
            catalog_by_id,
        )
        if truncated:
            for candidate in suggestion.candidates:
                candidate.diagnostics.append(
                    MatchDiagnostic(
                        code="CATALOG_TRUNCATED",
                        severity=SEVERITY_ADVISORY,
                        detail=(
                            f"Only the first {CATALOG_MAX} material parts were searched; "
                            "the catalog is larger than that."
                        ),
                    )
                )
        by_triple[triple] = suggestion

    candidate_ids = {c.part_id for s in by_triple.values() for c in s.candidates}
    on_hand_by_part, on_hand_known = _load_on_hand(db, company_id, sorted(candidate_ids))

    # Per-ROW copies: two nests sharing a triple share a spec but not a demand.
    result: Dict[str, SheetSuggestion] = {}
    remaining: Dict[int, float] = {}
    buckets = {STATUS_MATCHED: 0, STATUS_AMBIGUOUS: 0, STATUS_UNMATCHED: 0}
    for row in rows:
        triple = (row.get("material"), row.get("thickness"), row.get("sheet_size"))
        shared = by_triple[triple]
        suggestion = SheetSuggestion(
            status=shared.status,
            auto_fill_part_id=shared.auto_fill_part_id,
            candidates=[
                CandidatePart(**{**candidate.__dict__, "diagnostics": list(candidate.diagnostics)})
                for candidate in shared.candidates
            ],
            diagnostic=shared.diagnostic,
        )
        try:
            planned_runs = float(row.get("planned_runs") or 1)
        except (TypeError, ValueError):
            planned_runs = 1.0
        _annotate_stock(suggestion, on_hand_by_part, on_hand_known, remaining, planned_runs)
        buckets[suggestion.status] = buckets.get(suggestion.status, 0) + 1
        result[row["source_file"]] = suggestion

    logger.info(
        "sheet-stock match: company=%s nests=%s matched=%s ambiguous=%s unmatched=%s catalog=%s",
        company_id,
        len(rows),
        buckets[STATUS_MATCHED],
        buckets[STATUS_AMBIGUOUS],
        buckets[STATUS_UNMATCHED],
        len(catalog),
    )
    return result
