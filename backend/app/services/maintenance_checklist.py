"""A maintenance work order's checklist results, and which items a completion leaves unticked.

``MaintenanceWorkOrder.checklist_results`` is free text. The Maintenance page writes a JSON
array of ``{"item": ..., "done": ...}``; older rows (and the API) may carry a JSON array of
strings or one item per line. :func:`parse_checklist_results` reads all three the way the page's
``parseChecklistResults`` (``frontend/src/utils/maintenance.ts``) does, so the server and the
completion dialog agree on what the checklist says.

Pure functions: no database, no audit, no side effects.
"""

import json
from typing import Any, List, NamedTuple, Optional, Tuple


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
    return [ChecklistEntry(line.strip(), False) for line in text.splitlines() if line.strip()]


def unticked_checklist_items(stored: Optional[str], completed_with: Optional[str]) -> Tuple[List[str], int]:
    """The checklist items a completion leaves unticked, and how many items there are in all.

    ``stored`` is the checklist the work order carried before the completion; ``completed_with``
    the one it completes with (the same text when the completion does not send one). An item is
    done only when ``completed_with`` ticks it. An item ``stored`` carried that ``completed_with``
    leaves out -- or a completion that clears the checklist -- counts as NOT done: dropping a line
    is not a way to close without doing it. Items are matched by their text."""
    ticked = {entry.item for entry in parse_checklist_results(completed_with) if entry.done}
    items: List[str] = []
    seen = set()
    for entry in [*parse_checklist_results(completed_with), *parse_checklist_results(stored)]:
        if entry.item not in seen:
            seen.add(entry.item)
            items.append(entry.item)
    return [item for item in items if item not in ticked], len(items)
