"""Laser nests with no sheet tied -- the worklist, the coverage figure and the sheet report.

A nest imported without a sheet part completes normally and deducts nothing (by design:
an untied operation is byte-identical to its pre-tie behavior, invariant 6d). Nothing
listed those nests, so sheet on-hand drifted high by every sheet they burned. These
reads are the office's way to see them.

PURE READ, STRUCTURALLY. This module takes no ``AuditService`` and no actor id, imports
no consumption verb, and writes no ledger row, audit row or event -- the same property
``sheet_stock_matcher`` and ``material_tie_view`` hold. The one action the worklist
offers, "Tie this sheet", is the EXISTING ``POST /work-orders/{id}/material-allocations``
verb, called by the client with ``proposed_tie``; nothing here creates a tie.

Office screens only. The dispatch board and the kiosk keep rendering NOTHING for an
untied operation (``material_tie_view``'s recorded contract); do not wire these reads
into either.

Definitions used throughout:

* a **nest operation** is a ``work_order_operations`` row with a live (not soft-deleted)
  ``laser_nests`` row pointing at it, on a non-deleted work order;
* it is **tied** when a live (not CANCELLED), operation-scoped ``work_order_material_allocations``
  row names it. ``CANCELLED`` is the tie tombstone; ``OPEN`` is the only other state written
  today, and a fully consumed tie stays OPEN. The reserved ``CLOSED`` state counts as tied
  so that a future "close once consumed" writer cannot make every consumed nest read as
  untied (``_LIVE_TIE_STATUSES``). A work-order-scoped tie on the same laser job
  does not make the nest tied either, but is reported (``job_level_tie_count``) because
  tying the nest as well could draw the sheet twice;
* **open** means the operation is not COMPLETE and its work order is not terminal;
* **completed** means the operation is COMPLETE; its instant is ``actual_end``, falling
  back to the row's ``updated_at`` for a completion that never stamped one.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, case, exists, func, or_
from sqlalchemy.orm import Query, Session, aliased

from app.db.tenant_filter import tenant_query
from app.models.inventory import CycleCount, CycleCountStatus
from app.models.laser_nest import LaserNest
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderType
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.schemas.untied_nests import (
    NestTieCoverageMonth,
    NestTieCoverageResponse,
    OpenCycleCountRef,
    ProposedNestTie,
    SheetCandidate,
    SheetSuggestionView,
    UntiedNestListResponse,
    UntiedNestRow,
    UntiedSheetGroup,
    UntiedSheetReportResponse,
    WorkOrderNestTieRow,
    WorkOrderNestTieSummary,
)
from app.services.company_time import company_zone
from app.services.laser_nest_text import normalize_nest_descriptors
from app.services.sheet_stock_matcher import STATUS_MATCHED, SheetSuggestion, match_sheet_parts
from app.services.sheet_stock_spec import spec_key
from app.services.work_order_state_service import TERMINAL_WO_STATUSES

logger = logging.getLogger(__name__)

# When the caller names no completed-nest window, "recently completed" means this many
# days back from now. A design default, not an owner decision.
DEFAULT_COMPLETED_WINDOW_DAYS = 90

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500

DEFAULT_COVERAGE_MONTHS = 6
MAX_COVERAGE_MONTHS = 24

# The sheet report reads at most this many completed untied nests; past it the response
# says ``truncated`` rather than presenting a partial total as a whole one.
SHEET_REPORT_MAX_ROWS = 5000

MAX_GROUP_WORK_ORDER_NUMBERS = 10
MAX_SUMMARY_UNTIED_ROWS = 200

_OPEN_COUNT_STATUSES = (CycleCountStatus.SCHEDULED, CycleCountStatus.IN_PROGRESS)

# A tie is live -- the nest counts as tied -- in any state but its CANCELLED tombstone.
# CLOSED is reserved (never written today; see ``AllocationStatus``), and a tie closed
# after it was consumed must still count, or consumed nests would surface as untied.
_LIVE_TIE_STATUSES = (AllocationStatus.OPEN, AllocationStatus.CLOSED)


# ---------------------------------------------------------------------------
# Shared query pieces
# ---------------------------------------------------------------------------


def _completion_instant():
    """The SQL expression for when a nest operation completed."""
    return func.coalesce(WorkOrderOperation.actual_end, WorkOrderOperation.updated_at)


def _has_open_operation_tie(company_id: int):
    """EXISTS a live (not CANCELLED) operation-scoped tie on the outer ``WorkOrderOperation`` row."""
    return exists().where(
        WorkOrderMaterialAllocation.company_id == company_id,
        WorkOrderMaterialAllocation.work_order_operation_id == WorkOrderOperation.id,
        WorkOrderMaterialAllocation.status.in_(_LIVE_TIE_STATUSES),
    )


def _laser_job_scope(work_order_id: int):
    """The laser job itself, or the LASER_CUTTING children of an assembly parent.

    One predicate for the worklist's ``work_order_id`` filter and the Work Order Detail
    summary, so the strip's figure and the list it links to count the same nests.
    """
    return or_(
        WorkOrder.id == work_order_id,
        and_(
            WorkOrder.parent_work_order_id == work_order_id,
            WorkOrder.work_order_type == WorkOrderType.LASER_CUTTING.value,
        ),
    )


def _nest_operation_query(db: Session, company_id: int, *entities: Any) -> Query:
    """Live nest operations on live work orders, every table scoped to ``company_id``."""
    query = (
        tenant_query(db, LaserNest, company_id)
        .join(WorkOrderOperation, WorkOrderOperation.id == LaserNest.work_order_operation_id)
        .join(WorkOrder, WorkOrder.id == WorkOrderOperation.work_order_id)
        .filter(
            LaserNest.is_deleted == False,  # noqa: E712
            WorkOrderOperation.company_id == company_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
    )
    if entities:
        query = query.with_entities(*entities)
    return query


def _open_clause():
    return and_(
        WorkOrderOperation.status != OperationStatus.COMPLETE,
        WorkOrder.status.notin_(list(TERMINAL_WO_STATUSES)),
    )


def _completed_clause(window_start: Optional[datetime], window_end: Optional[datetime]):
    clauses = [WorkOrderOperation.status == OperationStatus.COMPLETE]
    if window_start is not None:
        clauses.append(_completion_instant() >= window_start)
    if window_end is not None:
        clauses.append(_completion_instant() < window_end)
    return and_(*clauses)


def resolve_completed_window(
    window_start: Optional[datetime], window_end: Optional[datetime], *, apply_default: bool = True
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Default an unbounded completed-nest window to the last 90 days.

    The router turns the caller's company-local dates into the naive-UTC ``[start, end)``
    (``app.api.date_window.company_date_window``); this only fills the default.
    ``apply_default=False`` leaves an unbounded window unbounded (the worklist filtered to
    one job, so it lists every nest the job's summary counts, whatever its age).
    """
    if apply_default and window_start is None and window_end is None:
        window_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DEFAULT_COMPLETED_WINDOW_DAYS)
    return window_start, window_end


def _value(member: Any) -> str:
    return str(getattr(member, "value", member))


def sheet_descriptor(material: Optional[str], thickness: Optional[str], sheet_size: Optional[str]) -> str:
    """'A36 / 0.250 / 60x120'-style label from the normalized descriptors; '?' for a blank."""
    norm = normalize_nest_descriptors(material, thickness, sheet_size)
    return " / ".join(part or "?" for part in norm)


def _job_level_tie_counts(db: Session, company_id: int, work_order_ids: Iterable[int]) -> Dict[int, int]:
    ids = sorted(set(work_order_ids))
    if not ids:
        return {}
    rows = (
        tenant_query(db, WorkOrderMaterialAllocation, company_id)
        .with_entities(WorkOrderMaterialAllocation.work_order_id, func.count(WorkOrderMaterialAllocation.id))
        .filter(
            WorkOrderMaterialAllocation.work_order_id.in_(ids),
            WorkOrderMaterialAllocation.work_order_operation_id.is_(None),
            WorkOrderMaterialAllocation.status.in_(_LIVE_TIE_STATUSES),
        )
        .group_by(WorkOrderMaterialAllocation.work_order_id)
        .all()
    )
    return {work_order_id: int(count) for work_order_id, count in rows}


# ---------------------------------------------------------------------------
# Matcher adapter
# ---------------------------------------------------------------------------


def _candidate_view(candidate: Any) -> SheetCandidate:
    return SheetCandidate(
        part_id=candidate.part_id,
        part_number=candidate.part_number,
        part_name=candidate.part_name,
        unit_of_measure=candidate.unit_of_measure,
        score=candidate.score,
        reason=candidate.reason,
        basis=candidate.basis,
        on_hand=candidate.on_hand,
        on_hand_known=candidate.on_hand_known,
    )


def _suggestion_view(suggestion: SheetSuggestion) -> SheetSuggestionView:
    """The matcher's answer, minus its package-projection stock diagnostics.

    ``match_sheet_parts`` projects stock CUMULATIVELY across the rows it is handed (it was
    built for one package). Across a cross-job worklist that projection means nothing, so
    only each candidate's own on-hand is carried; the per-candidate diagnostics and
    ``stock_state`` are deliberately not.
    """
    candidates = [_candidate_view(c) for c in suggestion.candidates]
    chosen = None
    if suggestion.status == STATUS_MATCHED and suggestion.auto_fill_part_id is not None:
        chosen = next((c for c in candidates if c.part_id == suggestion.auto_fill_part_id), None)
    status = suggestion.status if suggestion.status in ("matched", "ambiguous", "unmatched") else "unmatched"
    if status == "matched" and chosen is None:
        status = "ambiguous"
    return SheetSuggestionView(
        status=status,  # type: ignore[arg-type]
        candidate=chosen,
        candidates=candidates,
        diagnostic=suggestion.diagnostic,
    )


def _match(db: Session, company_id: int, rows: List[dict]) -> Tuple[Dict[str, SheetSuggestionView], bool]:
    """Run the matcher, never letting a matcher failure cost the read its rows."""
    if not rows:
        return {}, True
    try:
        # A SAVEPOINT, so a database error inside the matcher (a statement timeout, say)
        # rolls back only the matcher's statements. On Postgres an uncontained error aborts
        # the whole transaction and every later read on this session would fail.
        with db.begin_nested():
            raw = match_sheet_parts(db, company_id=company_id, nests=rows)
    except Exception:  # noqa: BLE001 - suggestions are advisory; the list must still load
        logger.warning("untied-nest read: sheet matcher failed; rows returned without suggestions", exc_info=True)
        return {}, False
    return {key: _suggestion_view(value) for key, value in raw.items()}, True


# ---------------------------------------------------------------------------
# GET /laser-nests/untied
# ---------------------------------------------------------------------------


@dataclass
class UntiedNestFilters:
    status: str = "all"
    work_order_id: Optional[int] = None
    work_center_id: Optional[int] = None
    completed_from: Optional[datetime] = None
    completed_to: Optional[datetime] = None
    limit: int = DEFAULT_LIST_LIMIT
    offset: int = 0


def list_untied_nests(db: Session, company_id: int, filters: UntiedNestFilters) -> UntiedNestListResponse:
    """Nest operations with no live operation tie: open ones plus completed ones in the window."""
    window_start, window_end = resolve_completed_window(
        filters.completed_from, filters.completed_to, apply_default=filters.work_order_id is None
    )
    parent = aliased(WorkOrder)

    query = _nest_operation_query(db, company_id).filter(~_has_open_operation_tie(company_id))
    if filters.status == "open":
        query = query.filter(_open_clause())
    elif filters.status == "completed":
        query = query.filter(_completed_clause(window_start, window_end))
    else:
        query = query.filter(or_(_open_clause(), _completed_clause(window_start, window_end)))
    if filters.work_order_id is not None:
        # The laser job itself, or the assembly parent whose laser child carries the nest.
        query = query.filter(_laser_job_scope(filters.work_order_id))
    if filters.work_center_id is not None:
        query = query.filter(WorkOrderOperation.work_center_id == filters.work_center_id)

    total = query.with_entities(func.count(LaserNest.id)).scalar() or 0

    is_complete = case((WorkOrderOperation.status == OperationStatus.COMPLETE, 1), else_=0)
    # Completed rows newest first; open rows carry no instant here (NULL), so they fall
    # through to the stable job / sequence / nest keys and a clock-in or count on an open
    # nest cannot move it across a page boundary.
    completed_instant = case((WorkOrderOperation.status == OperationStatus.COMPLETE, _completion_instant()), else_=None)
    rows = (
        query.outerjoin(
            parent,
            and_(parent.id == WorkOrder.parent_work_order_id, parent.company_id == company_id),
        )
        .outerjoin(
            WorkCenter,
            and_(WorkCenter.id == WorkOrderOperation.work_center_id, WorkCenter.company_id == company_id),
        )
        .with_entities(
            LaserNest,
            WorkOrderOperation,
            WorkOrder,
            parent.work_order_number,
            WorkCenter.name,
        )
        .order_by(
            is_complete.asc(),
            completed_instant.desc(),
            WorkOrder.work_order_number.asc(),
            WorkOrderOperation.sequence.asc(),
            LaserNest.id.asc(),
        )
        .limit(filters.limit)
        .offset(filters.offset)
        .all()
    )

    matcher_rows = [
        {
            "source_file": str(operation.id),
            "material": nest.material,
            "thickness": nest.thickness,
            "sheet_size": nest.sheet_size,
            "planned_runs": nest.planned_runs,
        }
        for nest, operation, _wo, _parent_number, _wc_name in rows
    ]
    suggestions, matcher_available = _match(db, company_id, matcher_rows)
    job_ties = _job_level_tie_counts(db, company_id, (wo.id for _n, _o, wo, _p, _w in rows))
    counted_after = _last_completed_count_by_part(
        db,
        company_id,
        {
            candidate.part_id
            for _n, operation, work_order, _p, _w in rows
            if operation.status == OperationStatus.COMPLETE and work_order.status not in TERMINAL_WO_STATUSES
            for suggestion in [suggestions.get(str(operation.id))]
            if suggestion is not None
            for candidate in suggestion.candidates
        },
    )

    items: List[UntiedNestRow] = []
    for nest, operation, work_order, parent_number, work_center_name in rows:
        terminal = work_order.status in TERMINAL_WO_STATUSES
        blocked_reason = (
            f"Work order {work_order.work_order_number} is {_value(work_order.status)}; "
            "a sheet can no longer be tied to it."
            if terminal
            else None
        )
        suggestion = suggestions.get(str(operation.id))
        proposed = None
        if not terminal and suggestion is not None and suggestion.candidate is not None:
            proposed = ProposedNestTie(
                work_order_id=work_order.id,
                part_id=suggestion.candidate.part_id,
                work_order_operation_id=operation.id,
                qty_per_run=1.0,
                qty_planned=float(nest.planned_runs or 1),
            )
        completed = operation.status == OperationStatus.COMPLETE
        completed_at = (operation.actual_end or operation.updated_at) if completed else None
        counted_part_ids: List[int] = []
        if completed and not terminal and suggestion is not None and completed_at is not None:
            counted_part_ids = [
                c.part_id
                for c in suggestion.candidates
                if counted_after.get(c.part_id) is not None and counted_after[c.part_id] > completed_at
            ]
        items.append(
            UntiedNestRow(
                laser_nest_id=nest.id,
                work_order_operation_id=operation.id,
                operation_number=operation.operation_number,
                operation_name=operation.name,
                operation_status=_value(operation.status),
                work_order_id=work_order.id,
                work_order_number=work_order.work_order_number,
                work_order_status=_value(work_order.status),
                parent_work_order_id=work_order.parent_work_order_id,
                parent_work_order_number=parent_number,
                work_center_id=operation.work_center_id,
                work_center_name=work_center_name,
                nest_name=nest.nest_name,
                cnc_number=nest.cnc_number,
                material=nest.material,
                thickness=nest.thickness,
                sheet_size=nest.sheet_size,
                sheet_descriptor=sheet_descriptor(nest.material, nest.thickness, nest.sheet_size),
                planned_runs=float(nest.planned_runs or 0),
                runs_complete=float(operation.quantity_complete or 0),
                runs_scrapped=float(operation.quantity_scrapped or 0),
                completed_at=completed_at,
                is_completed=completed,
                job_level_tie_count=job_ties.get(work_order.id, 0),
                tieable=not terminal,
                tie_blocked_reason=blocked_reason,
                suggestion=suggestion,
                proposed_tie=proposed,
                counted_since_completion_part_ids=counted_part_ids,
            )
        )

    return UntiedNestListResponse(
        items=items,
        total=int(total),
        limit=filters.limit,
        offset=filters.offset,
        completed_from=window_start,
        completed_to=window_end,
        matcher_available=matcher_available,
    )


# ---------------------------------------------------------------------------
# GET /laser-nests/untied/coverage
# ---------------------------------------------------------------------------


def _shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _percent(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def coverage_first_month(db: Session, company_id: int, months: int) -> date:
    """The first local day of the oldest month a ``months``-long coverage read spans."""
    zone = company_zone(db, company_id)
    today_local = datetime.now(timezone.utc).astimezone(zone).date()
    start_year, start_month = _shift_month(today_local.year, today_local.month, -(months - 1))
    return date(start_year, start_month, 1)


def nest_tie_coverage(
    db: Session, company_id: int, first_month: date, months: int, window_start: Optional[datetime]
) -> NestTieCoverageResponse:
    """Completed nests per company-local month, and how many of them carry an OPEN tie.

    ``first_month`` comes from ``coverage_first_month``; ``window_start`` is its local
    midnight as naive UTC (the router resolves it through ``company_date_window``).
    """
    zone = company_zone(db, company_id)
    start_year, start_month = first_month.year, first_month.month

    tied_flag = case((_has_open_operation_tie(company_id), 1), else_=0)
    rows = (
        _nest_operation_query(db, company_id, _completion_instant(), tied_flag)
        .filter(_completed_clause(window_start, None))
        .all()
    )

    keys = [f"{y:04d}-{m:02d}" for y, m in (_shift_month(start_year, start_month, i) for i in range(months))]
    completed: Counter = Counter()
    tied: Counter = Counter()
    for instant, is_tied in rows:
        if instant is None:
            continue
        local = instant.replace(tzinfo=timezone.utc).astimezone(zone)
        key = f"{local.year:04d}-{local.month:02d}"
        completed[key] += 1
        if is_tied:
            tied[key] += 1

    month_rows = [
        NestTieCoverageMonth(
            month=key,
            completed_nests=completed[key],
            tied_nests=tied[key],
            untied_nests=completed[key] - tied[key],
            tied_percent=_percent(tied[key], completed[key]),
        )
        for key in keys
    ]
    total_completed = sum(completed[key] for key in keys)
    total_tied = sum(tied[key] for key in keys)
    return NestTieCoverageResponse(
        months=month_rows,
        completed_nests=total_completed,
        tied_nests=total_tied,
        tied_percent=_percent(total_tied, total_completed),
    )


# ---------------------------------------------------------------------------
# GET /laser-nests/untied/sheet-report
# ---------------------------------------------------------------------------


@dataclass
class _GroupAccumulator:
    triples: Counter = field(default_factory=Counter)
    nest_count: int = 0
    sheets_burned: float = 0.0
    tieable_nest_count: int = 0
    tieable_sheets_burned: float = 0.0
    work_orders: Dict[str, datetime] = field(default_factory=dict)
    first: Optional[datetime] = None
    last: Optional[datetime] = None


def _last_completed_count_by_part(db: Session, company_id: int, part_ids: Iterable[int]) -> Dict[int, datetime]:
    """Latest COMPLETED part-scoped cycle count per part (one batched read).

    A count completed after an untied nest completed has already corrected on-hand for
    the sheets that nest burned; tying the nest afterwards would deduct them again when
    its job completes. The worklist flags that, it never refuses it.
    """
    ids = sorted(set(part_ids))
    if not ids:
        return {}
    rows = (
        tenant_query(db, CycleCount, company_id)
        .with_entities(CycleCount.part_id, func.max(CycleCount.completed_at))
        .filter(
            CycleCount.part_id.in_(ids),
            CycleCount.status == CycleCountStatus.COMPLETED,
            CycleCount.completed_at.isnot(None),
        )
        .group_by(CycleCount.part_id)
        .all()
    )
    return {part_id: completed_at for part_id, completed_at in rows if completed_at is not None}


def _open_counts_by_part(db: Session, company_id: int, part_ids: Sequence[int]) -> Dict[int, List[OpenCycleCountRef]]:
    if not part_ids:
        return {}
    counts = (
        tenant_query(db, CycleCount, company_id)
        .filter(CycleCount.part_id.in_(list(part_ids)), CycleCount.status.in_(_OPEN_COUNT_STATUSES))
        .order_by(CycleCount.scheduled_date.asc(), CycleCount.id.asc())
        .all()
    )
    result: Dict[int, List[OpenCycleCountRef]] = defaultdict(list)
    for count in counts:
        if count.part_id is None:
            continue
        result[count.part_id].append(
            OpenCycleCountRef(
                id=count.id,
                count_number=count.count_number,
                status=_value(count.status),
                scheduled_date=count.scheduled_date,
            )
        )
    return result


def untied_sheet_report(
    db: Session,
    company_id: int,
    completed_from: Optional[datetime],
    completed_to: Optional[datetime],
) -> UntiedSheetReportResponse:
    """Completed untied nests grouped by sheet spec, with sheets burned and the matched part.

    Sizes the sheet stock that left the shop with no deduction, so the office can count
    those parts through the existing cycle-count workflow. Posts nothing: no back-dated
    ledger row, ever.
    """
    window_start, window_end = resolve_completed_window(completed_from, completed_to)
    instant = _completion_instant()
    rows = (
        _nest_operation_query(
            db,
            company_id,
            LaserNest.material,
            LaserNest.thickness,
            LaserNest.sheet_size,
            WorkOrderOperation.quantity_complete,
            WorkOrderOperation.quantity_scrapped,
            WorkOrder.work_order_number,
            WorkOrder.status,
            instant,
        )
        .filter(_completed_clause(window_start, window_end), ~_has_open_operation_tie(company_id))
        .order_by(instant.desc(), LaserNest.id.desc())
        .limit(SHEET_REPORT_MAX_ROWS + 1)
        .all()
    )
    truncated = len(rows) > SHEET_REPORT_MAX_ROWS
    rows = rows[:SHEET_REPORT_MAX_ROWS]

    groups: Dict[str, _GroupAccumulator] = {}
    for material, thickness, sheet_size, good, scrapped, wo_number, wo_status, completed_at in rows:
        key = spec_key(material, thickness, sheet_size)
        group = groups.setdefault(key, _GroupAccumulator())
        group.triples[normalize_nest_descriptors(material, thickness, sheet_size)] += 1
        group.nest_count += 1
        burned = float(good or 0) + float(scrapped or 0)
        group.sheets_burned += burned
        if wo_status not in TERMINAL_WO_STATUSES:
            # Still tieable from the worklist: a tie, consumed when the job completes,
            # corrects these. Counting them as well would deduct them twice.
            group.tieable_nest_count += 1
            group.tieable_sheets_burned += burned
        if completed_at is not None:
            previous = group.work_orders.get(wo_number)
            if previous is None or completed_at > previous:
                group.work_orders[wo_number] = completed_at
            group.first = completed_at if group.first is None or completed_at < group.first else group.first
            group.last = completed_at if group.last is None or completed_at > group.last else group.last
        else:
            group.work_orders.setdefault(wo_number, datetime.min)

    representative: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {}
    for key, group in groups.items():
        # Most common normalized spelling; ties broken by the spelling itself so the label is stable.
        representative[key] = sorted(group.triples.items(), key=lambda item: (-item[1], str(item[0])))[0][0]

    matcher_rows = [
        {
            "source_file": key,
            "material": triple[0],
            "thickness": triple[1],
            "sheet_size": triple[2],
            "planned_runs": groups[key].sheets_burned or 1,
        }
        for key, triple in representative.items()
    ]
    suggestions, matcher_available = _match(db, company_id, matcher_rows)
    matched_part_ids = sorted(
        {s.candidate.part_id for s in suggestions.values() if s.candidate is not None}
        | {s.candidates[0].part_id for s in suggestions.values() if s.candidate is None and s.candidates}
    )
    open_counts = _open_counts_by_part(db, company_id, matched_part_ids)

    out: List[UntiedSheetGroup] = []
    for key, group in groups.items():
        material, thickness, sheet_size = representative[key]
        suggestion = suggestions.get(key)
        part_id = None
        if suggestion is not None:
            if suggestion.candidate is not None:
                part_id = suggestion.candidate.part_id
            elif suggestion.candidates:
                part_id = suggestion.candidates[0].part_id
        ordered_wos = sorted(group.work_orders.items(), key=lambda item: (item[1], item[0]), reverse=True)
        out.append(
            UntiedSheetGroup(
                spec_key=key,
                material=material,
                thickness=thickness,
                sheet_size=sheet_size,
                sheet_descriptor=" / ".join(value or "?" for value in (material, thickness, sheet_size)),
                nest_count=group.nest_count,
                sheets_burned=group.sheets_burned,
                tieable_nest_count=group.tieable_nest_count,
                tieable_sheets_burned=group.tieable_sheets_burned,
                work_order_count=len(group.work_orders),
                work_order_numbers=[number for number, _ in ordered_wos[:MAX_GROUP_WORK_ORDER_NUMBERS]],
                first_completed_at=group.first,
                last_completed_at=group.last,
                suggestion=suggestion,
                open_cycle_counts=open_counts.get(part_id, []) if part_id is not None else [],
            )
        )
    out.sort(key=lambda g: (-g.sheets_burned, -g.nest_count, g.spec_key))

    return UntiedSheetReportResponse(
        groups=out,
        nest_count=sum(g.nest_count for g in out),
        sheets_burned=sum(g.sheets_burned for g in out),
        tieable_sheets_burned=sum(g.tieable_sheets_burned for g in out),
        completed_from=window_start,
        completed_to=window_end,
        truncated=truncated,
        matcher_available=matcher_available,
    )


# ---------------------------------------------------------------------------
# GET /laser-nests/work-orders/{work_order_id}/tie-summary
# ---------------------------------------------------------------------------


def nest_tie_summary_message(untied: int, total: int) -> Optional[str]:
    if untied <= 0:
        return None
    noun = "nest has" if total == 1 else "nests have"
    return f"{untied} of {total} {noun} no sheet tied"


def work_order_nest_tie_summary(db: Session, company_id: int, work_order: WorkOrder) -> WorkOrderNestTieSummary:
    """Untied count for a job's own nests plus its laser child jobs' nests.

    The caller resolves ``work_order`` tenant-scoped and non-deleted. A job with no nests
    answers ``nest_count: 0`` -- not an error, so the client can call it for any job.
    """
    tied_flag = case((_has_open_operation_tie(company_id), 1), else_=0)
    scope = _laser_job_scope(work_order.id)
    rows = (
        _nest_operation_query(
            db,
            company_id,
            LaserNest.id,
            LaserNest.nest_name,
            LaserNest.cnc_number,
            WorkOrderOperation.id,
            WorkOrderOperation.operation_number,
            WorkOrderOperation.status,
            WorkOrder.id,
            WorkOrder.work_order_number,
            tied_flag,
        )
        .filter(scope)
        .order_by(WorkOrder.work_order_number.asc(), WorkOrderOperation.sequence.asc(), LaserNest.id.asc())
        .all()
    )
    untied_rows: List[WorkOrderNestTieRow] = []
    tied_count = 0
    laser_work_order_ids = set()
    for nest_id, nest_name, cnc_number, op_id, op_number, op_status, wo_id, wo_number, is_tied in rows:
        laser_work_order_ids.add(wo_id)
        if is_tied:
            tied_count += 1
            continue
        if len(untied_rows) < MAX_SUMMARY_UNTIED_ROWS:
            untied_rows.append(
                WorkOrderNestTieRow(
                    laser_nest_id=nest_id,
                    work_order_operation_id=op_id,
                    work_order_id=wo_id,
                    work_order_number=wo_number,
                    operation_number=op_number,
                    cnc_number=cnc_number,
                    nest_name=nest_name,
                    operation_status=_value(op_status),
                )
            )
    total = len(rows)
    untied = total - tied_count
    job_ties = _job_level_tie_counts(db, company_id, laser_work_order_ids)
    return WorkOrderNestTieSummary(
        work_order_id=work_order.id,
        work_order_number=work_order.work_order_number,
        nest_count=total,
        tied_count=tied_count,
        untied_count=untied,
        job_level_tie_count=sum(job_ties.values()),
        message=nest_tie_summary_message(untied, total),
        untied=untied_rows,
    )
