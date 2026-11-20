"""Maintenance checklists: a PM schedule's steps, a work order's checklist results, and which items a
completion leaves unticked.

``MaintenanceSchedule.checklist`` and ``MaintenanceWorkOrder.checklist_results`` are free text. The
Maintenance page writes a JSON array of strings (a schedule) or of ``{"item": ..., "done": ...}`` (a
work order); older rows, the PM import and the API may carry either shape, or one item per line.
:func:`parse_checklist` and :func:`parse_checklist_results` read them all, and the page's
``parseChecklistResults`` (``frontend/src/utils/maintenance.ts``) reads a work order's the same way,
so the server and the completion dialog agree on what a checklist says.

Item text is NORMALIZED on every read (owner decision 2026-11-20), by ONE function with a TypeScript
twin held to it by a shared fixture (``frontend/src/utils/__tests__/fixtures/
maintenance-checklist-normalization.json``, run by pytest and Jest): runs of whitespace (non-breaking
spaces included) collapse to one space, the ends are trimmed, and a leading bullet glyph is dropped.
Spelling, never meaning: "1." numbering is kept, and a dash that starts a number ("-40 F") is not a
bullet. Items then MATCH case-insensitively ("Check Oil" ticks "check oil"), keeping the first
spelling for display. Normalizing on read -- not by rewriting stored rows -- means legacy rows match
without a backfill (an UPDATE outside ``AuditService`` would break invariant 2).

Pure functions: no database, no audit, no side effects.
"""

import json
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

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
# ...including from the PM SCHEDULE's checklist while a work order judged on that live checklist is
# open against it -- one opened before migration 132, which has no checklist snapshot: emptying the
# schedule would be the same skip by a third route. (A work order opened since keeps its snapshot, so
# a schedule edit binds only the next one.)
API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL = (
    "Removing items from the checklist of a PM schedule whose open work order is still judged on the schedule's "
    "current checklist (it was opened before checklist snapshots) must be done by a person signed in to the app, "
    "not an API token."
)


class ChecklistEntry(NamedTuple):
    item: str
    done: bool


# Spelled out rather than ``\s``, which means different characters in Python and in JavaScript; the
# TypeScript twin uses this exact class. Every run of these becomes one space.
_WHITESPACE = "\t\n\v\f\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_WHITESPACE_RUN = re.compile(f"[{_WHITESPACE}]+")
# The same class at the ends of a whole stored checklist, stripped before it is parsed (``str.strip``
# and JavaScript's ``trim`` strip different sets: a leading byte-order mark would otherwise make the
# page read JSON the server reads as one line of text).
_WHITESPACE_EDGES = re.compile(f"^[{_WHITESPACE}]+|[{_WHITESPACE}]+$")
# Leading bullets, after the whitespace collapse: a bullet glyph (``\u2022 \u25e6 \u25aa \u00b7 \u2023
# \u2043``), or a dash / star / en dash / em dash FOLLOWED BY A SPACE (or nothing else) -- so ``-X axis``,
# ``-ve terminal``, ``-40 F`` and ``*CRITICAL*`` keep their marks -- each with the space after it, as
# many as there are.
_LEADING_BULLETS = re.compile("^(?:(?:[\u2022\u25e6\u25aa\u00b7\u2023\u2043]|[-*\u2013\u2014](?= |$)) ?)+")


def normalize_checklist_item(text: Any) -> str:
    """A checklist item's text as every reader sees it: whitespace runs collapsed to one space, the
    ends trimmed, leading bullet glyphs dropped. Idempotent. Spelling, never meaning: numbering
    ("1. Check oil"), a sign or emphasis mark written against a word ("-X axis", "*CRITICAL*") and
    case are kept (case is ignored only when MATCHING, see :func:`checklist_item_key`)."""
    collapsed = _WHITESPACE_RUN.sub(" ", _item_text(text)).strip(" ")
    return _LEADING_BULLETS.sub("", collapsed).strip(" ")


def checklist_item_key(text: Any) -> str:
    """What two checklist items are matched on: the normalized text, case-insensitively
    (``str.lower``, the twin of JavaScript's ``toLowerCase``). The two agree on every letter whose
    case mapping predates the Unicode version Python ships (14.0 for 3.11); a handful of letters added
    since lower-case in a newer browser only -- none in any shop checklist."""
    return normalize_checklist_item(text).lower()


def _item_text(value: Any) -> str:
    """``String(value ?? '')`` as the page computes it, for the JSON scalars it can meet."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return value if isinstance(value, str) else str(value)


# A work order's checklist splits on ``\r?\n`` exactly, like the page's reader: ``str.splitlines`` also
# splits on a lone ``\r`` and other separators, which would make the server see items the dialog does
# not show. (A schedule's checklist keeps ``splitlines``: the page never parses it, it shows the
# server's ``checklist_items``.)
_LINE_BREAK = re.compile(r"\r?\n")


def _strip_edges(text: str) -> str:
    return _WHITESPACE_EDGES.sub("", text)


def parse_checklist(text: Optional[str]) -> List[str]:
    """A PM schedule's stored checklist as a list of items (normalized; blanks dropped), tolerantly.

    The import and the page write a JSON array of strings; older UI rows hold one item per line. A
    JSON array of objects contributes each object's ``item`` / ``task`` / ``name`` / ``text``."""
    if not text or not _strip_edges(str(text)):
        return []
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError):
        loaded = None
    if isinstance(loaded, list):
        items: List[str] = []
        for entry in loaded:
            if isinstance(entry, dict):
                entry = next((entry[k] for k in ("item", "task", "name", "text") if entry.get(k)), "")
            item = normalize_checklist_item(entry)
            if item:
                items.append(item)
        return items
    return [item for item in (normalize_checklist_item(line) for line in str(text).splitlines()) if item]


def parse_checklist_results(raw: Optional[str]) -> List[ChecklistEntry]:
    """The checklist ``raw`` carries, blank items dropped. Only ``"done": true`` is ticked: a
    plain string item, or a line of text, is an unticked item."""
    text = _strip_edges(raw or "")
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
                item, done = normalize_checklist_item(entry.get("item")), entry.get("done") is True
            else:
                item, done = normalize_checklist_item(entry), False
            if item:
                entries.append(ChecklistEntry(item, done))
        return entries
    lines = (normalize_checklist_item(line) for line in _LINE_BREAK.split(text))
    return [ChecklistEntry(line, False) for line in lines if line]


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

    Items are matched by their NORMALIZED text, case-insensitively (:func:`checklist_item_key`), as
    a MULTISET: a checklist may name the same step twice ("Grease bearing" for each of two bearings),
    and ticking one copy must not tick the other. Each item counts as many times as the larger of the
    checklists carries it; the returned list repeats an item once per unticked copy, in the first
    spelling met (the completed checklist's, then the stored one's, then the required list's)."""
    completed = parse_checklist_results(completed_with)
    previous = parse_checklist_results(stored)
    required_items = [item for item in (normalize_checklist_item(text) for text in required) if item]
    spelling: Dict[str, str] = {}

    def keys(items: Iterable[str]) -> List[str]:
        found = []
        for item in items:
            key = item.lower()
            spelling.setdefault(key, item)
            found.append(key)
        return found

    in_completed = Counter(keys(entry.item for entry in completed))
    in_previous = Counter(keys(entry.item for entry in previous))
    in_required = Counter(keys(required_items))
    ticked = Counter(entry.item.lower() for entry in completed if entry.done)
    unticked: List[str] = []
    total = 0
    for key in dict.fromkeys([*in_completed, *in_previous, *in_required]):
        copies = max(in_completed[key], in_previous[key], in_required[key])
        total += copies
        unticked.extend([spelling[key]] * (copies - ticked[key]))
    return unticked, total


def removed_items(before: Sequence[str], after: Sequence[str]) -> List[str]:
    """Items ``before`` lists that ``after`` does not, as a multiset (one copy of a repeated item
    removed counts), matched as completion matches them (:func:`checklist_item_key`): a change of
    spacing, bullet or case removes nothing."""
    remaining = Counter(checklist_item_key(item) for item in after)
    removed: List[str] = []
    for item in before:
        key = checklist_item_key(item)
        if remaining[key] > 0:
            remaining[key] -= 1
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
