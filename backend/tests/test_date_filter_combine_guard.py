"""GUARD: no UTC-midnight day boundaries in ``app/``.

The bug this keeps out (P1.1, 2026-11): a calendar-date filter written as

    Model.created_at >= datetime.combine(start_date, datetime.min.time())
    Model.created_at <= datetime.combine(end_date, datetime.max.time())

compares a NAIVE-UTC timestamp column against UTC midnight. For a Central shop that
"day" runs from 6 PM (CST) / 7 PM (CDT) the evening before to 5:59 / 6:59 PM, so every
second-shift row lands in the next day's export, report, downtime list or KPI. A
calendar date the user names is a day in the COMPANY's time zone; the fix is
``app.api.date_window.company_date_window`` (routers, 422 on a bad range),
``app.services.company_time.company_window`` (services) or
``app.core.time_utils.local_date_range_to_utc`` -- local midnight to the EXCLUSIVE
next local midnight, converted to naive UTC.

The guard parses every module under ``backend/app`` and flags each
``datetime.combine(<date>, <min/max time>)`` call that passes no ``tzinfo`` -- the
min/max time spelled ``datetime.min.time()``, ``datetime.max.time()``, ``time.min`` or
``time.max``. A zone-aware combine (third positional argument or ``tzinfo=``) is the
correct shape and is not flagged.

``NON_FILTER_USES`` names the genuinely non-filter uses -- date-to-datetime coercions
for arithmetic, display or audit snapshots, where no UTC column is being bounded --
each keyed ``(path, enclosing function)`` with its occurrence count and the reason.
It is stale-checked like ``test_route_tenant_scope_guard``: an entry whose function no
longer has that many naive combines fails, so the list can only shrink honestly. A new
FILTER must not be added here; use the helpers above.
"""

import ast
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

pytestmark = pytest.mark.unit

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# (path relative to backend/, enclosing function qualname) -> (occurrences, reason)
NON_FILTER_USES: Dict[Tuple[str, str], Tuple[int, str]] = {
    ("app/api/endpoints/engineering_changes.py", "get_eco_dashboard"): (
        1,
        "ECO cycle-time arithmetic: completed_date (a Date) as a midnight datetime minus created_at, "
        "for an average in whole days. Not a filter.",
    ),
    ("app/api/endpoints/scheduling.py", "_audit_schedule_value"): (
        1,
        "Audit-snapshot normalizer: renders a date as a datetime string so before/after snapshots "
        "compare like with like. Not a filter.",
    ),
    ("app/services/prediction_service.py", "PredictionService.predict_delivery"): (
        3,
        "Forecast arithmetic over date-granular schedule projections (scheduled_start/end are dates); "
        "the result is a predicted date, not a bound on a stored timestamp.",
    ),
    ("app/services/auto_evidence_service.py", "_query_suppliers"): (
        1,
        "Evidence-card display date for a scorecard's period_end (a Date). Not a filter.",
    ),
    ("app/services/scheduling_impact_service.py", "_iso"): (
        1,
        "Serializes a date-granular projection value for the impact preview. Not a filter.",
    ),
    ("app/services/scheduling_impact_service.py", "SchedulingImpactService.preview"): (
        2,
        "Date-granular schedule projections coerced to datetimes for the before/after comparison "
        "of the impact preview. Not a filter.",
    ),
}

_MIN_MAX = {"min", "max"}


def _is_min_max_time(node: ast.AST) -> bool:
    """``datetime.min.time()`` / ``datetime.max.time()`` / ``time.min`` / ``time.max``."""
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "time"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr in _MIN_MAX
        )
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _MIN_MAX
        and isinstance(node.value, ast.Name)
        and node.value.id == "time"
    )


def _is_naive_combine(node: ast.Call) -> bool:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "combine"):
        return False
    if len(node.args) < 2 or not _is_min_max_time(node.args[1]):
        return False
    has_tzinfo = len(node.args) >= 3 or any(keyword.arg == "tzinfo" for keyword in node.keywords)
    return not has_tzinfo


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: List[str] = []
        self.hits: List[Tuple[str, int]] = []

    def _scoped(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        if _is_naive_combine(node):
            self.hits.append((".".join(self.stack) or "<module>", node.lineno))
        self.generic_visit(node)


def find_naive_combines(source: str) -> List[Tuple[str, int]]:
    finder = _Finder()
    finder.visit(ast.parse(source))
    return finder.hits


def _scan_app() -> Dict[Tuple[str, str], List[int]]:
    found: Dict[Tuple[str, str], List[int]] = {}
    backend = APP_ROOT.parent
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(backend).as_posix()
        for qualname, lineno in find_naive_combines(path.read_text(encoding="utf-8")):
            found.setdefault((rel, qualname), []).append(lineno)
    return found


def test_no_utc_midnight_day_boundaries_in_app():
    found = _scan_app()
    offenders = []
    for key, lines in sorted(found.items()):
        allowed = NON_FILTER_USES.get(key, (0, ""))[0]
        if len(lines) > allowed:
            offenders.append(f"  {key[0]}:{','.join(map(str, lines))} in {key[1]}")

    assert not offenders, (
        "datetime.combine(<date>, <min/max time>) with no tzinfo is a UTC-midnight day boundary. "
        "Filter a UTC column by a calendar date with app.api.date_window.company_date_window / "
        "app.services.company_time.company_window (local midnight to the EXCLUSIVE next local "
        "midnight, filter with a strict <). If this is genuinely not a filter, add it to "
        "NON_FILTER_USES with the reason:\n" + "\n".join(offenders)
    )


def test_allowlist_is_not_stale():
    found = _scan_app()
    stale = [
        f"  {key}: allows {count}, found {len(found.get(key, []))}"
        for key, (count, _reason) in sorted(NON_FILTER_USES.items())
        if len(found.get(key, [])) != count
    ]

    assert not stale, "NON_FILTER_USES entries that no longer match the code -- shrink or delete them:\n" + "\n".join(
        stale
    )


def test_every_allowlist_entry_has_a_reason():
    assert all(reason.strip() for _count, reason in NON_FILTER_USES.values())


@pytest.mark.parametrize(
    "snippet",
    [
        "datetime.combine(d, datetime.min.time())",
        "datetime.combine(d, datetime.max.time())",
        "datetime.combine(d, time.min)",
        "datetime.combine(d, time.max)",
        "dt.datetime.combine(d, dt.datetime.max.time())",
    ],
)
def test_finder_flags_the_naive_shapes(snippet):
    assert len(find_naive_combines(f"x = {snippet}")) == 1


@pytest.mark.parametrize(
    "snippet",
    [
        "datetime.combine(d, time.min, ZONE)",
        "datetime.combine(d, time.min, tzinfo=ZONE)",
        "datetime.combine(d, datetime.min.time(), ZONE)",
        "datetime.combine(d, t)",  # an arbitrary time is not a day boundary
        "datetime.combine(d, time(7, 30))",
    ],
)
def test_finder_ignores_zone_aware_and_non_boundary_combines(snippet):
    assert find_naive_combines(f"x = {snippet}") == []


def test_finder_reports_the_enclosing_qualname():
    source = "class A:\n    def m(self):\n        return datetime.combine(d, time.max)\n"
    assert find_naive_combines(source) == [("A.m", 3)]


def test_the_scan_covers_the_tree():
    """Vacuity check: the walk must actually read app/ (and see the allowlisted uses)."""
    found = _scan_app()
    assert len(list(APP_ROOT.rglob("*.py"))) > 200
    assert Counter(key[0] for key in found)["app/services/prediction_service.py"] == 1
