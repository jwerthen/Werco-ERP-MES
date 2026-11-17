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
from typing import Any, List, NamedTuple, Optional, Tuple

# Owner decision 2026-11-17, under the 2026-11-07 scrap principle (``API_TOKEN_SHORT_CLOSE``): why a
# PM step was skipped is a person's judgment, so a standing API token may complete a maintenance
# work order only with every checklist item ticked.
API_TOKEN_INCOMPLETE_CHECKLIST = (
    "Completing a maintenance work order with checklist items unticked must be done by a person signed in "
    "to the app, not an API token."
)


class ChecklistEntry(NamedTuple):
    item: str
    done: bool


def _item_text(value: Any) -> str:
    if value is None:
        return ""
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


def unticked_checklist_items(stored: Optional[str], completed_with: Optional[str]) -> Tuple[List[str], int]:
    """The checklist items a completion leaves unticked, and how many items there are in all.

    ``stored`` is the checklist the work order carried before the completion; ``completed_with``
    the one it completes with (the same text when the completion does not send one). An item is
    done only when ``completed_with`` ticks it. An item ``stored`` carried that ``completed_with``
    leaves out -- or a completion that clears the checklist -- counts as NOT done: dropping a line
    is not a way to close without doing it.

    Items are matched by their text, as a MULTISET: a checklist may name the same step twice
    ("Grease bearing" for each of two bearings), and ticking one copy must not tick the other.
    Each text counts as many times as the larger of the two checklists carries it; the returned
    list repeats a text once per unticked copy."""
    completed = parse_checklist_results(completed_with)
    previous = parse_checklist_results(stored)
    ticked = Counter(entry.item for entry in completed if entry.done)
    in_completed = Counter(entry.item for entry in completed)
    in_previous = Counter(entry.item for entry in previous)
    unticked: List[str] = []
    total = 0
    for text in dict.fromkeys(entry.item for entry in [*completed, *previous]):
        copies = max(in_completed[text], in_previous[text])
        total += copies
        unticked.extend([text] * (copies - ticked[text]))
    return unticked, total
