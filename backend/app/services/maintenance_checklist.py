"""A maintenance work order's checklist results, and which items a completion leaves unticked.

``MaintenanceWorkOrder.checklist_results`` is free text. The Maintenance page writes a JSON
array of ``{"item": ..., "done": ...}``; older rows (and the API) may carry a JSON array of
strings or one item per line. :func:`parse_checklist_results` reads all three the way the page's
``parseChecklistResults`` (``frontend/src/utils/maintenance.ts``) does, so the server and the
completion dialog agree on what the checklist says.

Pure functions: no database, no audit, no side effects.
"""

import json
import re
from collections import Counter
from typing import Any, List, NamedTuple, Optional, Sequence, Tuple

# Owner decision 2026-11-17, under the 2026-11-07 scrap principle (``API_TOKEN_SHORT_CLOSE``): why a
# PM step was skipped is a person's judgment, so a standing API token may complete a maintenance
# work order only with every checklist item ticked.
API_TOKEN_INCOMPLETE_CHECKLIST = (
    "Completing a maintenance work order with checklist items unticked must be done by a person signed in "
    "to the app, not an API token."
)
# ...and, so a token cannot reach the same end in two steps (PUT the checklist down, then complete):
# taking items off an open work order's checklist is a person's edit too.
API_TOKEN_CHECKLIST_REMOVAL = (
    "Removing items from a maintenance work order's checklist must be done by a person signed in to the app, "
    "not an API token."
)
# ...including from the PM SCHEDULE's checklist while a work order is open against it: completion
# judges the schedule's steps too, so emptying the schedule would be the same skip by a third route.
API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL = (
    "Removing items from the checklist of a PM schedule that has an open work order must be done by a person "
    "signed in to the app, not an API token."
)


class ChecklistEntry(NamedTuple):
    item: str
    done: bool


def _item_text(value: Any) -> str:
    """``String(value ?? '').trim()`` as the page computes it, for the JSON scalars it can meet."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return (value if isinstance(value, str) else str(value)).strip()


def parse_checklist_results(raw: Optional[str]) -> List[ChecklistEntry]:
    """The checklist ``raw`` carries, blank items dropped. Only ``"done": true`` is ticked: a
    plain string item, or a line of text, is an unticked item."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        entries: List[ChecklistEntry] = []
        for entry in parsed:
            if isinstance(entry, list):
                # The page reads a nested array as an object with no ``item``: dropped.
                continue
            if isinstance(entry, dict):
                item, done = _item_text(entry.get("item")), entry.get("done") is True
            else:
                item, done = _item_text(entry), False
            if item:
                entries.append(ChecklistEntry(item, done))
        return entries
    # ``\r?\n`` exactly, like the page's reader: ``str.splitlines`` also splits on a lone ``\r``
    # and other separators, which would make the server see items the dialog does not show.
    return [ChecklistEntry(line.strip(), False) for line in re.split(r"\r?\n", text) if line.strip()]


def unticked_checklist_items(
    stored: Optional[str], completed_with: Optional[str], required: Sequence[str] = ()
) -> Tuple[List[str], int]:
    """The checklist items a completion leaves unticked, and how many items there are in all.

    ``stored`` is the checklist the work order carried before the completion; ``completed_with``
    the one it completes with (the same text when the completion does not send one). An item is
    done only when ``completed_with`` ticks it. An item ``stored`` carried that ``completed_with``
    leaves out -- or a completion that clears the checklist -- counts as NOT done: dropping a line
    is not a way to close without doing it. ``required`` adds items that must be ticked whatever
    either checklist says: a PM work order's schedule checklist, so a step taken off the work
    order (at Start PM, or by a later edit) still needs a tick or a reason.

    Items are matched by their text, as a MULTISET: a checklist may name the same step twice
    ("Grease bearing" for each of two bearings), and ticking one copy must not tick the other.
    Each text counts as many times as the larger of the two checklists carries it; the returned
    list repeats a text once per unticked copy."""
    completed = parse_checklist_results(completed_with)
    previous = parse_checklist_results(stored)
    ticked = Counter(entry.item for entry in completed if entry.done)
    in_completed = Counter(entry.item for entry in completed)
    in_previous = Counter(entry.item for entry in previous)
    in_required = Counter(item for item in required if item)
    unticked: List[str] = []
    total = 0
    for text in dict.fromkeys([*(entry.item for entry in [*completed, *previous]), *in_required]):
        copies = max(in_completed[text], in_previous[text], in_required[text])
        total += copies
        unticked.extend([text] * (copies - ticked[text]))
    return unticked, total


def removed_items(before: Sequence[str], after: Sequence[str]) -> List[str]:
    """Items ``before`` lists that ``after`` does not, as a multiset (one copy of a repeated item
    removed counts)."""
    remaining = Counter(after)
    removed: List[str] = []
    for item in before:
        if remaining[item] > 0:
            remaining[item] -= 1
        else:
            removed.append(item)
    return removed


def removed_checklist_items(stored: Optional[str], new: Optional[str]) -> List[str]:
    """Work-order checklist items ``stored`` carries that ``new`` does not. Ticking, unticking or
    adding an item removes nothing."""
    return removed_items(
        [entry.item for entry in parse_checklist_results(stored)],
        [entry.item for entry in parse_checklist_results(new)],
    )


def describe_items(items: Sequence[str], limit: int = 10) -> str:
    """``items`` for a message: ``a; b; c``, the tail after ``limit`` summarized."""
    shown = "; ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown}; and {len(items) - limit} more"
