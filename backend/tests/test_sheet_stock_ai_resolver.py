"""Unit tests for the AI leg of sheet-stock matching (no live API calls).

The subject is an ADVISORY re-ranker. Most of what these tests pin down is what
it must NOT do: it may never set ``auto_fill_part_id``, never change ``status``,
never surface a part that was not on the shortlist it was given, and never
raise. A tie drives real inventory depletion into an as-built record that never
auto-reverses, so every one of those is a safety property, not a style rule.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.services.llm_client as llm_client
import app.services.sheet_stock_ai_resolver as resolver
from app.services.sheet_stock_ai_resolver import (
    AI_BASIS,
    DIAG_AI_PICK_OUT_OF_SET,
    DIAG_AI_UNAVAILABLE,
    MAX_AI_REASON_CHARS,
    resolve_ambiguous_sheet_matches,
)
from app.services.sheet_stock_matcher import (
    STATUS_AMBIGUOUS,
    STATUS_MATCHED,
    STATUS_UNMATCHED,
    CandidatePart,
    SheetSuggestion,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _candidate(part_id: int, part_number: str, **overrides: Any) -> CandidatePart:
    kwargs: Dict[str, Any] = {
        "part_id": part_id,
        "part_number": part_number,
        "part_name": f"Sheet {part_number}",
        "unit_of_measure": "EA",
        "score": 85.0,
        "reason": "0.250 matches the nest's thickness.",
        "spec_thickness": "0.250",
        "spec_sheet_size": "60X120",
        "on_hand": 4.0,
    }
    kwargs.update(overrides)
    return CandidatePart(**kwargs)


def _ambiguous(
    *part_numbers: str,
    diagnostic: str = "Both sheets fit this nest's spec. Pick the one this job runs.",
) -> SheetSuggestion:
    return SheetSuggestion(
        status=STATUS_AMBIGUOUS,
        auto_fill_part_id=None,
        candidates=[_candidate(100 + i, pn) for i, pn in enumerate(part_numbers)],
        diagnostic=diagnostic,
    )


def _picks_json(*picks: Dict[str, Any]) -> str:
    return json.dumps({"picks": list(picks)})


class _FakeLLM:
    """Stands in for ``run_llm_task``; records every call."""

    def __init__(self, text: str = "", error: Optional[BaseException] = None):
        self.text = text
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, ctx: Any, **kwargs: Any) -> Any:
        self.calls.append({"ctx": ctx, **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text, model="claude-sonnet-4-6")


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeLLM) -> _FakeLLM:
    monkeypatch.setattr(resolver, "run_llm_task", fake)
    return fake


def _codes(candidate: CandidatePart) -> List[str]:
    return [d.code for d in candidate.diagnostics]


# ---------------------------------------------------------------------------
# The happy path exists only to make the refusals meaningful
# ---------------------------------------------------------------------------
def test_valid_pick_moves_candidate_to_rank_one(monkeypatch):
    fake = _install(
        monkeypatch,
        _FakeLLM(
            _picks_json(
                {
                    "key": "g1",
                    "part_number": "0.250-60X120-304",
                    "reason": "Nest calls out 304 stainless; this sheet is 304 at 60x120.",
                }
            )
        ),
    )
    suggestions = {"nest-1.pdf": _ambiguous("0.250-60X120-316", "0.250-60X120-304")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    candidates = suggestions["nest-1.pdf"].candidates
    assert [c.part_number for c in candidates] == ["0.250-60X120-304", "0.250-60X120-316"]
    assert candidates[0].basis == AI_BASIS
    assert candidates[0].reason == "Nest calls out 304 stainless; this sheet is 304 at 60x120."
    # The loser keeps its deterministic identity untouched.
    assert candidates[1].basis == "deterministic"
    assert len(fake.calls) == 1


def test_pick_leaves_the_deterministic_score_alone(monkeypatch):
    """The model did not re-derive the score and must not appear to have."""
    _install(
        monkeypatch,
        _FakeLLM(_picks_json({"key": "g1", "part_number": "B", "reason": "B is the 304 sheet."})),
    )
    suggestions = {"n.pdf": _ambiguous("A", "B")}
    suggestions["n.pdf"].candidates[1].score = 77.5

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert suggestions["n.pdf"].candidates[0].score == 77.5


def test_overlong_reason_is_truncated_not_dropped(monkeypatch):
    _install(
        monkeypatch,
        _FakeLLM(_picks_json({"key": "g1", "part_number": "B", "reason": "x" * 900})),
    )
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    top = suggestions["n.pdf"].candidates[0]
    assert top.part_number == "B"
    assert len(top.reason) == MAX_AI_REASON_CHARS


# ---------------------------------------------------------------------------
# Fence 1 — the returned part number must be in THAT group's shortlist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bogus",
    [
        "0.250-60X120-321",  # a plausible part this tenant does not stock
        "0.250-60x120-304",  # right part, wrong case: exact match or nothing
        " 0.250-60X120-304",  # right part, leading space
        "",
        12345,  # not even a string
        ["0.250-60X120-304"],  # unhashable: must not blow up the `in` check
    ],
)
def test_out_of_set_part_number_is_discarded(monkeypatch, bogus):
    _install(
        monkeypatch,
        _FakeLLM(_picks_json({"key": "g1", "part_number": bogus, "reason": "Looks right to me."})),
    )
    suggestions = {"n.pdf": _ambiguous("0.250-60X120-316", "0.250-60X120-304")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    candidates = suggestions["n.pdf"].candidates
    # Deterministic order survives intact.
    assert [c.part_number for c in candidates] == ["0.250-60X120-316", "0.250-60X120-304"]
    assert all(c.basis == "deterministic" for c in candidates)
    assert DIAG_AI_PICK_OUT_OF_SET in _codes(candidates[0])
    assert all(d.severity == "advisory" for d in candidates[0].diagnostics)


def test_part_number_from_another_group_is_out_of_set(monkeypatch):
    """The hallucination fence is also the cross-tenant fence: a string is only
    valid against the shortlist the group that answered was actually shown."""
    _install(
        monkeypatch,
        _FakeLLM(
            _picks_json(
                # g1 was shown A/B; answering it with g2's part is out of set.
                {"key": "g1", "part_number": "C", "reason": "C fits."},
                {"key": "g2", "part_number": "C", "reason": "C is the 10ga sheet."},
            )
        ),
    )
    suggestions = {
        "n1.pdf": _ambiguous("A", "B", diagnostic="0.250 spec is ambiguous."),
        "n2.pdf": _ambiguous("C", "D", diagnostic="10ga spec is ambiguous."),
    }

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert [c.part_number for c in suggestions["n1.pdf"].candidates] == ["A", "B"]
    assert DIAG_AI_PICK_OUT_OF_SET in _codes(suggestions["n1.pdf"].candidates[0])
    # The group that was answered legitimately still gets its promotion.
    assert suggestions["n2.pdf"].candidates[0].part_number == "C"
    assert suggestions["n2.pdf"].candidates[0].basis == AI_BASIS


def test_unknown_group_key_is_discarded(monkeypatch):
    _install(
        monkeypatch,
        _FakeLLM(_picks_json({"key": "g99", "part_number": "B", "reason": "B fits."})),
    )
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert [c.part_number for c in suggestions["n.pdf"].candidates] == ["A", "B"]
    assert all(c.basis == "deterministic" for c in suggestions["n.pdf"].candidates)


# ---------------------------------------------------------------------------
# Fence 2 — an unauditable proposal is not a proposal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reason", ["", "   ", "\n\t ", None, 42])
def test_pick_without_a_usable_reason_is_dropped(monkeypatch, reason):
    pick: Dict[str, Any] = {"key": "g1", "part_number": "B"}
    if reason is not None:
        pick["reason"] = reason
    _install(monkeypatch, _FakeLLM(_picks_json(pick)))
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    candidates = suggestions["n.pdf"].candidates
    assert [c.part_number for c in candidates] == ["A", "B"]
    assert all(c.basis == "deterministic" for c in candidates)
    assert candidates[1].reason == "0.250 matches the nest's thickness."


def test_null_part_number_is_an_abstention_not_an_error(monkeypatch):
    _install(
        monkeypatch,
        _FakeLLM(_picks_json({"key": "g1", "part_number": None, "reason": "Cannot tell from the nest."})),
    )
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    candidates = suggestions["n.pdf"].candidates
    assert [c.part_number for c in candidates] == ["A", "B"]
    # An abstention is a correct answer, so it earns no failure advisory.
    assert candidates[0].diagnostics == []


# ---------------------------------------------------------------------------
# Fence 3 — pre-fill stays the deterministic gate's alone
# ---------------------------------------------------------------------------
def test_auto_fill_part_id_is_never_set(monkeypatch):
    """The strongest claim in this module. A successful, in-set, well-reasoned
    pick still may not pre-fill the planner's picker."""
    _install(
        monkeypatch,
        _FakeLLM(_picks_json({"key": "g1", "part_number": "B", "reason": "B is the 304 sheet at 60x120."})),
    )
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    suggestion = suggestions["n.pdf"]
    assert suggestion.auto_fill_part_id is None
    assert suggestion.status == STATUS_AMBIGUOUS
    # The promotion did happen -- this is not a vacuous assertion.
    assert suggestion.candidates[0].part_number == "B"
    assert suggestion.candidates[0].basis == AI_BASIS


def test_status_and_prefill_survive_every_failure_mode(monkeypatch):
    for text in ('{"picks": [{"key": "g1", "part_number": "ZZZ", "reason": "no"}]}', "not json", "{}"):
        _install(monkeypatch, _FakeLLM(text))
        suggestions = {"n.pdf": _ambiguous("A", "B")}
        resolve_ambiguous_sheet_matches(suggestions, company_id=7)
        assert suggestions["n.pdf"].auto_fill_part_id is None
        assert suggestions["n.pdf"].status == STATUS_AMBIGUOUS


# ---------------------------------------------------------------------------
# Grouping and call economics
# ---------------------------------------------------------------------------
def test_no_ambiguous_rows_makes_zero_llm_calls(monkeypatch):
    fake = _install(monkeypatch, _FakeLLM(_picks_json()))
    suggestions = {
        "n1.pdf": SheetSuggestion(status=STATUS_MATCHED, auto_fill_part_id=101, candidates=[_candidate(101, "A")]),
        "n2.pdf": SheetSuggestion(status=STATUS_UNMATCHED, candidates=[], diagnostic="Nothing matched."),
    }

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert fake.calls == []
    assert suggestions["n1.pdf"].auto_fill_part_id == 101
    assert suggestions["n1.pdf"].candidates[0].diagnostics == []


def test_ambiguous_row_with_no_candidates_makes_zero_llm_calls(monkeypatch):
    """Nothing to rank. Inventing options is exactly what this must not do."""
    fake = _install(monkeypatch, _FakeLLM(_picks_json()))
    suggestions = {"n.pdf": SheetSuggestion(status=STATUS_AMBIGUOUS, candidates=[], diagnostic="No sheet matched.")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert fake.calls == []


def test_two_nests_sharing_a_spec_cost_one_group_and_both_get_the_pick(monkeypatch):
    fake = _install(
        monkeypatch,
        _FakeLLM(_picks_json({"key": "g1", "part_number": "B", "reason": "B is the 304 sheet."})),
    )
    suggestions = {"n1.pdf": _ambiguous("A", "B"), "n2.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert len(fake.calls) == 1
    prompt = fake.calls[0]["messages"][0]["content"]
    assert prompt.count("### GROUP") == 1
    for key in ("n1.pdf", "n2.pdf"):
        assert suggestions[key].candidates[0].part_number == "B"
        assert suggestions[key].candidates[0].basis == AI_BASIS
    # Per-row candidate objects stay distinct -- one promotion must not alias.
    assert suggestions["n1.pdf"].candidates[0] is not suggestions["n2.pdf"].candidates[0]


def test_same_shortlist_with_different_refusals_are_separate_groups(monkeypatch):
    fake = _install(monkeypatch, _FakeLLM(_picks_json()))
    suggestions = {
        "n1.pdf": _ambiguous("A", "B", diagnostic="Grade not stated."),
        "n2.pdf": _ambiguous("A", "B", diagnostic="Two sheets both fit."),
    }

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    prompt = fake.calls[0]["messages"][0]["content"]
    assert prompt.count("### GROUP") == 2


def test_group_cap_degrades_the_overflow_and_still_sends_one_call(monkeypatch):
    fake = _install(monkeypatch, _FakeLLM(_picks_json()))
    suggestions = {f"n{i}.pdf": _ambiguous("A", "B", diagnostic=f"spec {i} is ambiguous") for i in range(12)}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert len(fake.calls) == 1
    prompt = fake.calls[0]["messages"][0]["content"]
    assert prompt.count("### GROUP") == resolver.MAX_AI_GROUPS
    overflow = suggestions["n11.pdf"].candidates[0]
    assert DIAG_AI_UNAVAILABLE in _codes(overflow)


# ---------------------------------------------------------------------------
# The request itself
# ---------------------------------------------------------------------------
def test_request_carries_company_id_and_the_versioned_prompt(monkeypatch):
    """``_ai_egress_allowed`` returns True on a None company_id, so omitting it
    would walk straight past the per-company CUI kill switch."""
    from app.services.prompts.sheet_stock import SHEET_STOCK_DISAMBIGUATION_PROMPT

    fake = _install(monkeypatch, _FakeLLM(_picks_json()))
    resolve_ambiguous_sheet_matches({"n.pdf": _ambiguous("A", "B")}, company_id=42)

    call = fake.calls[0]
    assert call["company_id"] == 42
    assert call["system"] == SHEET_STOCK_DISAMBIGUATION_PROMPT.text
    assert call["prompt_version"] == SHEET_STOCK_DISAMBIGUATION_PROMPT.version
    assert call["max_retries"] == 0
    assert call["timeout"] == 20.0
    assert call["ctx"].task == "sheet_stock_disambiguation"
    # A tool_use-leading response would make _first_text return "" and turn a
    # clean parse into an obscure JSONDecodeError; caching a per-request
    # shortlist writes a block that is never read, at 1.25x.
    assert "tools" not in call and "tool_choice" not in call
    assert "cache_control" not in json.dumps(call["messages"])


def test_prompt_is_registered_in_the_prompt_registry():
    from app.services.prompts import PROMPT_REGISTRY

    prompt = PROMPT_REGISTRY["sheet_stock_disambiguation"]
    assert prompt.version == "1.0.0"
    assert prompt.text


def test_router_sends_this_task_to_the_default_tier(monkeypatch):
    from app.services.llm_model_router import LLMModelTier, LLMTaskContext, select_anthropic_model

    monkeypatch.delenv("ANTHROPIC_SHEET_STOCK_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_MODEL_SELECTION", "auto")

    # A tiny prompt scores 0 on complexity, which would land on FAST if the task
    # were unrouted. It must not.
    decision = select_anthropic_model(
        LLMTaskContext(task="sheet_stock_disambiguation", input_chars=120, max_output_tokens=512)
    )
    assert decision.tier == LLMModelTier.DEFAULT


# ---------------------------------------------------------------------------
# Degradation — every failure keeps the deterministic order
# ---------------------------------------------------------------------------
def test_egress_off_never_calls_anthropic_and_leaves_the_order_untouched(monkeypatch):
    """Drives the REAL ``run_llm_task`` so the kill switch itself is exercised.

    ``conftest``'s autouse ``_allow_ai_egress_by_default`` patches the egress
    seam to allow; this re-patches it to deny, which wins because both share one
    monkeypatch instance.
    """
    created: List[Dict[str, Any]] = []

    class _Messages:
        def create(self, **kwargs):  # pragma: no cover - must never run
            created.append(kwargs)
            raise AssertionError("Anthropic was called with AI egress disabled")

    class _Client:
        def __init__(self):
            self.messages = _Messages()

        def with_options(self, **_kwargs):
            return self

    monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: _Client())
    monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: False)
    monkeypatch.setattr(resolver, "run_llm_task", llm_client.run_llm_task)

    suggestions = {"n.pdf": _ambiguous("A", "B")}
    resolve_ambiguous_sheet_matches(suggestions, company_id=7)  # must not raise

    assert created == []
    candidates = suggestions["n.pdf"].candidates
    assert [c.part_number for c in candidates] == ["A", "B"]
    assert all(c.basis == "deterministic" for c in candidates)
    assert suggestions["n.pdf"].auto_fill_part_id is None
    # Pin the branch: this must be the egress refusal, not some other failure
    # that happens to land in the same generic degradation.
    unavailable = [d for d in candidates[0].diagnostics if d.code == DIAG_AI_UNAVAILABLE]
    assert len(unavailable) == 1
    assert "turned off for this company" in unavailable[0].detail


def test_unconfigured_llm_degrades_quietly(monkeypatch):
    _install(monkeypatch, _FakeLLM(error=llm_client.LLMNotConfiguredError("api_key")))
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert [c.part_number for c in suggestions["n.pdf"].candidates] == ["A", "B"]
    assert DIAG_AI_UNAVAILABLE in _codes(suggestions["n.pdf"].candidates[0])


@pytest.mark.parametrize(
    "text",
    ["", "not json at all", "[]", '{"picks": "nope"}', '{"nope": []}', "```json\n{oops}\n```"],
)
def test_unusable_response_degrades_with_an_advisory(monkeypatch, text):
    _install(monkeypatch, _FakeLLM(text))
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert [c.part_number for c in suggestions["n.pdf"].candidates] == ["A", "B"]
    assert DIAG_AI_UNAVAILABLE in _codes(suggestions["n.pdf"].candidates[0])


def test_fenced_json_is_parsed(monkeypatch):
    _install(
        monkeypatch,
        _FakeLLM(
            "```json\n" + _picks_json({"key": "g1", "part_number": "B", "reason": "B is the 304 sheet."}) + "\n```"
        ),
    )
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)

    assert suggestions["n.pdf"].candidates[0].part_number == "B"


def test_arbitrary_exception_never_escapes(monkeypatch):
    _install(monkeypatch, _FakeLLM(error=RuntimeError("connection reset")))
    suggestions = {"n.pdf": _ambiguous("A", "B")}

    resolve_ambiguous_sheet_matches(suggestions, company_id=7)  # must not raise

    assert [c.part_number for c in suggestions["n.pdf"].candidates] == ["A", "B"]
    assert DIAG_AI_UNAVAILABLE in _codes(suggestions["n.pdf"].candidates[0])


def test_malformed_suggestions_never_escape(monkeypatch):
    """The outer guard: the contract is never-raises, whatever it is handed."""
    _install(monkeypatch, _FakeLLM(_picks_json()))

    resolve_ambiguous_sheet_matches({"n.pdf": object()}, company_id=7)  # type: ignore[dict-item]


def test_empty_suggestions_is_a_no_op(monkeypatch):
    fake = _install(monkeypatch, _FakeLLM(_picks_json()))

    resolve_ambiguous_sheet_matches({}, company_id=7)

    assert fake.calls == []
