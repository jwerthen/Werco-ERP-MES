"""Rank the sheet-stock shortlist the deterministic matcher could not resolve.

``sheet_stock_matcher`` answers most nests outright and refuses the rest. This
module runs on the refusals ONLY: the ``ambiguous`` rows that already carry a
shortlist. It reorders that shortlist and attaches a sentence. That is the whole
of its authority.

WHAT THIS MODULE IS NOT ALLOWED TO DECIDE
-----------------------------------------
It never sets ``auto_fill_part_id`` and never changes ``status``. Pre-fill is
assigned by the deterministic gate alone, and the wizard treats even that as a
proposal the planner confirms before Import. The tie drives real inventory
depletion at operation completion into an AS9100D as-built record that never
auto-reverses, so nothing here may shorten the path between a model's opinion
and a committed tie. A promoted candidate arrives at rank 1 wearing
``basis='ai_disambiguated'`` precisely so the UI can say who ranked it.

The fences on the response are not defensive style, they are the design:

* The returned part number is re-resolved by EXACT string match against the
  shortlist THAT group was given. This is the hallucination fence and the
  cross-tenant fence at once -- the shortlist came out of a tenant-scoped
  catalog read, so a string that is not in it cannot name this tenant's stock.
* A pick with a blank reason is dropped. An unauditable proposal is not a
  proposal; the same rule the deterministic matcher applies to its own
  candidates (``_reason_for`` returning "" drops the row).
* A ``null`` part number is a legitimate abstention and leaves the row alone.

PURE READ, and never raises. No session, no audit row, no ledger row, no event
-- it only mutates the in-memory suggestion objects it is handed. Every failure
path (egress off, unconfigured, bad JSON, anything else) leaves the
deterministic order exactly as the matcher produced it and attaches an
``AI_UNAVAILABLE`` advisory so the planner knows the row was never AI-ranked.

COST SHAPE
----------
ONE ``run_llm_task`` call per preview, covering every ambiguous group -- not one
per nest. Rows are grouped by their candidate part ids plus the refusal
diagnostic, so a 42-nest Miratech package with three distinct unresolved specs
costs one call carrying three groups.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

from app.services.laser_nest_extraction_service import _strip_json_fences
from app.services.llm_client import LLMEgressDisabledError, LLMNotConfiguredError, run_llm_task
from app.services.llm_model_router import LLMTaskContext
from app.services.prompts.sheet_stock import SHEET_STOCK_DISAMBIGUATION_PROMPT
from app.services.sheet_stock_matcher import (
    SEVERITY_ADVISORY,
    STATUS_AMBIGUOUS,
    CandidatePart,
    MatchDiagnostic,
    SheetSuggestion,
)

logger = logging.getLogger(__name__)

# The basis stamped on a candidate this module lifted to rank 1. The wizard
# reads it to label the row; `deterministic` and `history` are the other two.
AI_BASIS = "ai_disambiguated"

# The model is told to keep a reason under 160 characters. 300 is the storage
# ceiling, not the ask -- a model that overruns its instruction gets truncated
# rather than dropped, because the pick itself is still re-resolved and checked.
MAX_AI_REASON_CHARS = 300

# Groups sent in one call. `max_tokens` is 512 and one pick costs roughly 55
# output tokens (a 160-char reason plus the key and the part number), so eight
# groups is the most that can be answered without the response being cut off
# mid-object -- which would throw away every pick in it, not just the last.
# Real packages produce one to three groups; this is a ceiling, not a budget.
MAX_AI_GROUPS = 8

# One call per preview carrying a per-request shortlist means a cache block
# would be written and never read, and a write costs 1.25x. No cache_control.
AI_MAX_TOKENS = 512

# A planner is watching a spinner. The SDK's default of 2 retries would turn
# this into a 60-second worst case, so retries are off and the ceiling is real.
AI_TIMEOUT_SECONDS = 20.0
AI_MAX_RETRIES = 0

AI_TASK = "sheet_stock_disambiguation"

DIAG_AI_PICK_OUT_OF_SET = "AI_PICK_OUT_OF_SET"
DIAG_AI_UNAVAILABLE = "AI_UNAVAILABLE"


@dataclass
class _Group:
    """One distinct unresolved spec, and every row that shares it."""

    key: str
    diagnostic: str
    # The representative shortlist, used to render the prompt. Members hold
    # their own per-row copies of the same parts (they differ in demand and
    # projected stock, never in identity).
    shortlist: List[CandidatePart]
    allowed_part_numbers: Set[str]
    members: List[SheetSuggestion] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def _collect_groups(suggestions: Dict[str, SheetSuggestion]) -> List[_Group]:
    """Distinct ambiguous specs, in grid order, deduped by shortlist + refusal.

    Two nests cut from the same unresolved spec produce the same candidate ids
    and the same refusal sentence, so they share one group and cost one entry in
    the prompt. A row with no candidates is skipped entirely: there is nothing to
    rank, and inventing options is exactly what this feature must not do.
    """
    groups: Dict[Tuple[Tuple[int, ...], str], _Group] = {}
    for suggestion in suggestions.values():
        if suggestion.status != STATUS_AMBIGUOUS or not suggestion.candidates:
            continue
        signature = (
            tuple(candidate.part_id for candidate in suggestion.candidates),
            suggestion.diagnostic or "",
        )
        group = groups.get(signature)
        if group is None:
            group = _Group(
                key=f"g{len(groups) + 1}",
                diagnostic=suggestion.diagnostic or "",
                shortlist=list(suggestion.candidates),
                allowed_part_numbers={
                    candidate.part_number for candidate in suggestion.candidates if candidate.part_number
                },
            )
            groups[signature] = group
        group.members.append(suggestion)
    return list(groups.values())


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------
def _render_candidate(candidate: CandidatePart) -> str:
    uom = candidate.unit_of_measure or "EA"
    if candidate.on_hand_known:
        stock = f"on hand: {candidate.on_hand:g} {uom}"
    else:
        stock = "on hand: unknown"
    fields = [
        f'part_number: "{candidate.part_number}"',
        f"name: {candidate.part_name or '(unnamed)'}",
        f"thickness: {candidate.spec_thickness or 'not parsed'}",
        f"sheet size: {candidate.spec_sheet_size or 'not parsed'}",
        stock,
    ]
    if candidate.reason:
        fields.append(f"server note: {candidate.reason}")
    return " | ".join(fields)


def _render_prompt(groups: List[_Group]) -> str:
    """One block per group. The nest's own descriptor text rides in the refusal
    sentence and the per-candidate server notes, so it is never restated."""
    lines: List[str] = [
        f"{len(groups)} nest specification(s) need a sheet chosen. Answer each group by its key.",
        "",
    ]
    for group in groups:
        lines.append(f"### GROUP {group.key}")
        if group.diagnostic:
            lines.append(f"Why the server could not pick one sheet: {group.diagnostic}")
        lines.append("Candidate stock (each already matches this nest's thickness):")
        for candidate in group.shortlist:
            lines.append(f"  - {_render_candidate(candidate)}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Applying the response
# ---------------------------------------------------------------------------
def _attach_row_diagnostic(group: _Group, code: str, detail: str) -> None:
    """Row-level advisory: one per member, on its rank-1 candidate.

    Candidate diagnostics are the only diagnostics list on the wire, and the
    top candidate is the row's face in the grid. Copying it onto all five would
    repeat one fact five times without adding one.
    """
    for suggestion in group.members:
        if suggestion.candidates:
            suggestion.candidates[0].diagnostics.append(
                MatchDiagnostic(code=code, severity=SEVERITY_ADVISORY, detail=detail)
            )


def _promote(group: _Group, part_number: str, reason: str) -> None:
    """Move the named candidate to rank 1 in every row sharing this group.

    ``score`` is left exactly as the deterministic matcher computed it -- the
    model did not re-derive it and must not appear to have. Only the order, the
    basis and the sentence change.

    PRIOR-TIE EVIDENCE SURVIVES the rewrite. A candidate the history leg lifted
    into the shortlist carries the strongest signal in the whole feature -- what
    planners at this shop actually tied to this spec before -- and replacing its
    sentence outright would drop that on the floor in favour of a model's
    opinion. The count itself lives in ``prior_tie_count`` and was never at risk;
    it is the sentence a planner reads that was.
    """
    for suggestion in group.members:
        chosen = next((c for c in suggestion.candidates if c.part_number == part_number), None)
        if chosen is None:
            continue
        suggestion.candidates = [chosen] + [c for c in suggestion.candidates if c is not chosen]
        chosen.basis = AI_BASIS
        if chosen.prior_tie_count > 0:
            chosen.reason = (f"{reason} Planners have tied this sheet to {chosen.prior_tie_count} nests of this spec.")[
                :MAX_AI_REASON_CHARS
            ]
        else:
            chosen.reason = reason


def _apply_picks(groups_by_key: Dict[str, _Group], payload: Any) -> None:
    """Fence, then apply. Raises only when the envelope itself is unusable."""
    picks = payload.get("picks") if isinstance(payload, dict) else None
    if not isinstance(picks, list):
        raise ValueError("sheet-stock disambiguation response has no 'picks' list")

    answered: Set[str] = set()
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        key = pick.get("key")
        group = groups_by_key.get(key) if isinstance(key, str) else None
        if group is None:
            logger.warning("sheet-stock AI returned an unknown group key %r; discarded", key)
            continue
        if group.key in answered:
            # Only the first answer for a group counts; a second one is noise
            # and would silently overwrite an already-applied promotion.
            logger.warning("sheet-stock AI answered group %s twice; later answer discarded", group.key)
            continue
        answered.add(group.key)

        part_number = pick.get("part_number")
        if part_number is None:
            # An explicit abstention. The deterministic order stands, which is
            # the correct outcome, so this is not a failure and not a warning.
            continue
        if not isinstance(part_number, str) or part_number not in group.allowed_part_numbers:
            logger.warning(
                "sheet-stock AI proposed %r for group %s, which was not on its shortlist; discarded",
                part_number,
                group.key,
            )
            _attach_row_diagnostic(
                group,
                DIAG_AI_PICK_OUT_OF_SET,
                (
                    "The assistant proposed a part that was not among the options it was shown, "
                    "so its suggestion was discarded. The list below is the server's own ranking."
                ),
            )
            continue

        reason = pick.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            logger.warning(
                "sheet-stock AI proposed %s for group %s with no reason; discarded",
                part_number,
                group.key,
            )
            continue

        _promote(group, part_number, reason.strip()[:MAX_AI_REASON_CHARS])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _degrade(groups: List[_Group], detail: str) -> None:
    for group in groups:
        _attach_row_diagnostic(group, DIAG_AI_UNAVAILABLE, detail)


def _resolve(suggestions: Dict[str, SheetSuggestion], *, company_id: int) -> None:
    groups = _collect_groups(suggestions)
    if not groups:
        # No LLM call at all. A package the matcher resolved cleanly must cost
        # nothing, and a preview is re-run every time the planner reopens it.
        return

    if len(groups) > MAX_AI_GROUPS:
        _degrade(
            groups[MAX_AI_GROUPS:],
            (
                f"Only the first {MAX_AI_GROUPS} unresolved sheet specs were sent for review; "
                "this one keeps the server's own ranking."
            ),
        )
        groups = groups[:MAX_AI_GROUPS]

    prompt = _render_prompt(groups)
    groups_by_key = {group.key: group for group in groups}

    try:
        result = run_llm_task(
            LLMTaskContext(task=AI_TASK, input_chars=len(prompt), max_output_tokens=AI_MAX_TOKENS),
            messages=[{"role": "user", "content": prompt}],
            system=SHEET_STOCK_DISAMBIGUATION_PROMPT.text,
            max_tokens=AI_MAX_TOKENS,
            # MANDATORY. `_ai_egress_allowed` returns True on None, so omitting
            # this would walk straight past the per-company CUI kill switch.
            company_id=company_id,
            feature=AI_TASK,
            prompt_version=SHEET_STOCK_DISAMBIGUATION_PROMPT.version,
            timeout=AI_TIMEOUT_SECONDS,
            max_retries=AI_MAX_RETRIES,
        )
        payload = json.loads(_strip_json_fences(result.text))
        _apply_picks(groups_by_key, payload)
        logger.info(
            "sheet-stock AI disambiguation: company=%s groups=%s model=%s",
            company_id,
            len(groups),
            result.model,
        )
    except LLMEgressDisabledError as exc:
        logger.warning("sheet-stock AI disambiguation skipped: %s", exc)
        _degrade(
            groups,
            "AI review is turned off for this company, so this row keeps the server's own ranking.",
        )
    except LLMNotConfiguredError as exc:
        logger.warning("sheet-stock AI disambiguation skipped: %s", exc)
        _degrade(groups, "AI review is not configured, so this row keeps the server's own ranking.")
    except json.JSONDecodeError as exc:
        logger.warning("sheet-stock AI disambiguation returned unparseable JSON: %s", exc)
        _degrade(groups, "AI review returned an unreadable answer, so this row keeps the server's own ranking.")
    except Exception as exc:  # noqa: BLE001 - an advisory leg may never sink a preview
        logger.warning("sheet-stock AI disambiguation failed: %s", exc, exc_info=True)
        _degrade(groups, "AI review could not be completed, so this row keeps the server's own ranking.")


def resolve_ambiguous_sheet_matches(suggestions: dict[str, SheetSuggestion], *, company_id: int) -> None:
    """Re-rank the ambiguous rows in ``suggestions`` in place. Never raises.

    Args:
        suggestions: The matcher's output, keyed by nest ``source_file``.
            Mutated in place; rows that are not ``ambiguous``, and ambiguous
            rows with no candidates, are left untouched.
        company_id: The active company. Passed to ``run_llm_task`` so the
            per-company ``allow_ai_egress`` kill switch is enforced and the
            usage row is tenant-scoped.

    Returns:
        None. The caller reads the mutated ``suggestions``.
    """
    try:
        _resolve(suggestions, company_id=company_id)
    except Exception:  # noqa: BLE001 - the contract is that this never raises
        logger.warning(
            "sheet-stock AI disambiguation aborted; deterministic order kept",
            exc_info=True,
        )


__all__ = [
    "AI_BASIS",
    "DIAG_AI_PICK_OUT_OF_SET",
    "DIAG_AI_UNAVAILABLE",
    "MAX_AI_GROUPS",
    "MAX_AI_REASON_CHARS",
    "resolve_ambiguous_sheet_matches",
]
