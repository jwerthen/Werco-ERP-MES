"""The QMS prompt move (#9-0) sends the model exactly the bytes the inline code sent.

Before #9-0 the QMS clause-extraction prompt was an f-string inside
``POST /qms-standards/{id}/upload-pdf``. It now lives in ``prompts/qms.py`` as
``QMS_CLAUSE_EXTRACTION_TEMPLATE`` and is rendered and sent by
``qms_extraction_service``. The move was meant to change nothing the model sees,
so these tests compare the new request with the old one byte for byte.

**How "the old one" is known.** ``_legacy_inline_prompt`` below is the old
f-string, copied character for character from ``qms_standards.py`` at
``origin/main`` before #9-0 (only re-indented). It is evaluated against a
stand-in ``standard`` so its expressions, ``{standard.name}``,
``{standard.version or ''}`` and ``{pdf_text[:100000]}``, run unchanged. The
companion endpoint test, ``tests/api/test_qms_upload_request_parity.py``, drives
the real route through the same Anthropic-client seam and passes on the pre-move
code too. That is what proves this copy is faithful.

**What is compared.** The keyword arguments ``run_llm_task`` hands to
``client.messages.create`` (model, ``max_tokens``, messages, and the absence of
``system``/``tools``/``tool_choice``/``extra_body``), serialized in insertion
order. Also compared: that ``with_options`` is never called (no per-call timeout
or retry override, so the SDK defaults still apply), the routing context, and
the telemetry labels. The real ``run_llm_task`` runs; only the client and the
telemetry session are faked, and nothing reaches Anthropic.

**This is a one-off.** When the QMS prompt changes on purpose (with a version
bump), retire the legacy comparison here instead of editing the legacy copy. The
prompt-drift lock (#9-5a) is the long-term guard.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.services.llm_client as llm_client
import app.services.qms_extraction_service as qms_extraction_service
from app.services.llm_client import LLMEgressDisabledError, LLMNotConfiguredError
from app.services.llm_model_router import LLMTaskContext, select_anthropic_model
from app.services.prompts import PROMPT_REGISTRY, QMS_CLAUSE_EXTRACTION_PROMPT
from app.services.qms_extraction_service import (
    QMSClauseExtractionResult,
    build_qms_clause_extraction_prompt,
    extract_clauses_with_llm,
)

pytestmark = pytest.mark.unit

COMPANY_ID = 7

# Every env var that can change which model the router picks for this task.
_MODEL_ENV_VARS = (
    "ANTHROPIC_QMS_MODEL",
    "ANTHROPIC_MODEL_SELECTION",
    "ANTHROPIC_FAST_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "ANTHROPIC_REASONING_MODEL",
)


# ---------------------------------------------------------------------------
# The pre-#9-0 inline code, verbatim (re-indented only). Do not edit.
# ---------------------------------------------------------------------------
def _legacy_inline_prompt(standard: Any, pdf_text: str) -> str:
    clause_schema = """[
  {
    "clause_number": "string - e.g. '4.1', '8.5.2'",
    "title": "string - clause title",
    "description": "string - full clause text or summary of requirements"
  }
]"""

    prompt = f"""You are a QMS standards expert. Extract ALL clauses and sub-clauses from this quality management document.

The document is: {standard.name} {standard.version or ''}

Rules:
1. Extract EVERY numbered clause and sub-clause (e.g., 4.1, 4.2, 5.1.1, 8.5.2)
2. Include the clause number, title, and the full requirement text as description
3. Maintain the hierarchical numbering exactly as shown in the document
4. Include ALL levels of sub-clauses
5. For the description, include the actual requirement text — not just a summary
6. Return a JSON array of objects, nothing else

Schema:
{clause_schema}

Document text:
---
{pdf_text[:100000]}
---

Return ONLY a valid JSON array. No markdown, no explanations."""
    return prompt


def _legacy_task_context(pdf_text: str) -> LLMTaskContext:
    """The routing context the inline code built (pre-#9-0, verbatim values)."""
    return LLMTaskContext(
        task="qms_clause_extraction",
        input_chars=len(pdf_text),
        max_output_tokens=16000,
    )


def _legacy_create_kwargs(name: str, version: Optional[str], pdf_text: str) -> Dict[str, Any]:
    """What ``run_llm_task`` passed to ``messages.create`` for the inline call.

    The inline call passed ``messages`` and ``max_tokens=16000`` and no
    ``system``/``tools``/``tool_choice``/``cache_conversation``, so
    ``run_llm_task`` built exactly these three keys, in this order.
    """
    standard = SimpleNamespace(name=name, version=version)
    return {
        "model": select_anthropic_model(_legacy_task_context(pdf_text)).model,
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": _legacy_inline_prompt(standard, pdf_text)}],
    }


def _wire(obj: Any) -> str:
    """Serialize the way the drift lock will: insertion order kept, no key sorting."""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Inputs. Each one targets a way a template move could change the bytes.
# ---------------------------------------------------------------------------
_SHORT_TEXT = (
    "4 Context of the organization\n4.1 Understanding the organization and its context\n"
    "8.5.2 Identification and traceability"
)

RENDER_CASES = [
    pytest.param("AS9100D", "2016", _SHORT_TEXT, id="name-and-version"),
    # The design calls this one out: `{standard.version or ''}` leaves a trailing space.
    pytest.param("AS9100D", None, _SHORT_TEXT, id="version-None"),
    pytest.param("ISO 9001", "", _SHORT_TEXT, id="version-empty-string"),
    # str.format must not re-parse substituted values (a re-parse raises or eats braces).
    pytest.param("{name} {0} {{x}}", "Rev {B}", "clause {0} and {document_text} and {{literal}} %s %(x)s", id="braces"),
    pytest.param("Qualitäts-Handbuch", "Ausgabe 3 — 2024", "§ 4.1 Kontext — “quoted” ✓\n", id="non-ascii"),
    # Only the first 100,000 chars are sent; routing still sees the full length.
    pytest.param("AS9100D", "2016", ("8.5.2 traceability " * 8000)[:150_000], id="over-100k-truncated"),
    pytest.param("AS9100D", "2016", "x" * 100_000, id="exactly-100k"),
    pytest.param("AS9100D", "2016", "\n\n  leading and trailing whitespace kept  \n\n", id="whitespace"),
]


# ---------------------------------------------------------------------------
# Fakes (no network; the real run_llm_task runs)
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, text: str):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.usage = SimpleNamespace(
            input_tokens=1200, output_tokens=300, cache_creation_input_tokens=0, cache_read_input_tokens=0
        )


class _FakeMessages:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.reply)


class _FakeClient:
    def __init__(self, reply: str):
        self.messages = _FakeMessages(reply)
        self.with_options_calls: List[Dict[str, Any]] = []

    def with_options(self, **kwargs):
        self.with_options_calls.append(kwargs)
        return self


class _RecordingSession:
    def __init__(self):
        self.added: List[Any] = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _default_model_routing(monkeypatch):
    for name in _MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def telemetry(monkeypatch) -> _RecordingSession:
    session = _RecordingSession()
    monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: session)
    return session


@pytest.fixture
def routed_contexts(monkeypatch) -> List[LLMTaskContext]:
    """Record every routing context run_llm_task hands the model router."""
    seen: List[LLMTaskContext] = []
    real = llm_client.select_anthropic_model

    def _recording(ctx):
        seen.append(ctx)
        return real(ctx)

    monkeypatch.setattr(llm_client, "select_anthropic_model", _recording)
    return seen


def _install_client(monkeypatch, reply: str = "[]") -> _FakeClient:
    client = _FakeClient(reply)
    monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# 1. The rendered prompt is byte-identical to the inline f-string
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,version,pdf_text", RENDER_CASES)
def test_rendered_prompt_is_byte_identical_to_the_inline_f_string(name, version, pdf_text):
    legacy = _legacy_inline_prompt(SimpleNamespace(name=name, version=version), pdf_text)

    rendered = build_qms_clause_extraction_prompt(pdf_text, name, version)

    assert rendered.encode("utf-8") == legacy.encode("utf-8")


def test_version_none_renders_the_name_and_one_trailing_space():
    """Pin the exact label bytes for the version=None case the design names."""
    rendered = build_qms_clause_extraction_prompt(_SHORT_TEXT, "AS9100D", None)

    assert "\nThe document is: AS9100D \n" in rendered


# ---------------------------------------------------------------------------
# 2. The request that reaches the Anthropic client is byte-identical
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,version,pdf_text", RENDER_CASES)
def test_request_sent_to_anthropic_is_byte_identical(monkeypatch, telemetry, routed_contexts, name, version, pdf_text):
    client = _install_client(monkeypatch, reply="[]")

    extract_clauses_with_llm(pdf_text, name, version, COMPANY_ID)

    assert len(client.messages.calls) == 1
    sent = client.messages.calls[0]
    expected = _legacy_create_kwargs(name, version, pdf_text)
    # Byte equality over the whole request, key order included (insertion order is
    # what the SDK serializes). No system / tools / tool_choice / extra_body key.
    assert _wire(sent) == _wire(expected)
    assert list(sent) == ["model", "max_tokens", "messages"]
    # No per-call timeout or retry override: the SDK defaults still apply, as before.
    assert client.with_options_calls == []
    # The routing context is the inline code's, field for field (defaults included).
    assert routed_contexts == [_legacy_task_context(pdf_text)]


def test_routing_still_sees_the_full_text_length_not_the_truncated_one(monkeypatch, telemetry, routed_contexts):
    """150k chars of text: only 100k are sent, but routing escalates on 150k (Opus)."""
    client = _install_client(monkeypatch, reply="[]")
    pdf_text = "y" * 150_000

    extract_clauses_with_llm(pdf_text, "AS9100D", "2016", COMPANY_ID)

    assert routed_contexts[0].input_chars == 150_000
    assert client.messages.calls[0]["model"] == "claude-opus-4-8"
    content = client.messages.calls[0]["messages"][0]["content"]
    assert "y" * 100_000 + "\n---" in content
    assert "y" * 100_001 not in content


def test_short_text_routes_to_the_default_tier(monkeypatch, telemetry):
    client = _install_client(monkeypatch, reply="[]")

    extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)

    assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"


def test_telemetry_row_keeps_its_labels_and_tenant(monkeypatch, telemetry):
    _install_client(monkeypatch, reply="[]")

    extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)

    assert len(telemetry.added) == 1
    row = telemetry.added[0]
    assert row.company_id == COMPANY_ID
    assert row.task == "qms_clause_extraction"
    assert row.feature == "qms_standards"
    assert row.prompt_version == "1.0.0"
    assert row.success is True


def test_prompt_version_is_unchanged_by_the_move():
    """No text changed, so no bump. A deliberate bump must retire this file's legacy check."""
    assert QMS_CLAUSE_EXTRACTION_PROMPT.version == "1.0.0"
    assert QMS_CLAUSE_EXTRACTION_PROMPT.text is None
    assert PROMPT_REGISTRY["qms_clause_extraction"] is QMS_CLAUSE_EXTRACTION_PROMPT


# ---------------------------------------------------------------------------
# 3. Parsing and exceptions are unchanged
# ---------------------------------------------------------------------------
_CLAUSES = [{"clause_number": "4.1", "title": "Context", "description": "Determine issues."}]


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(json.dumps(_CLAUSES), id="bare"),
        pytest.param("```json\n" + json.dumps(_CLAUSES) + "\n```", id="json-fence"),
        pytest.param("```\n" + json.dumps(_CLAUSES) + "\n```", id="plain-fence"),
        pytest.param("  \n```json\n" + json.dumps(_CLAUSES) + "\n```  \n", id="fence-with-outer-whitespace"),
    ],
)
def test_reply_is_unfenced_and_parsed(monkeypatch, telemetry, reply):
    _install_client(monkeypatch, reply=reply)

    result = extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)

    assert isinstance(result, QMSClauseExtractionResult)
    assert result.clauses == _CLAUSES
    assert result.model == "claude-sonnet-4-6"
    assert result.model_selection_reason == "QMS extraction needs complete clause coverage"


def test_array_elements_are_returned_unvalidated(monkeypatch, telemetry):
    """The handler coerces and filters elements; the service must not pre-filter them."""
    _install_client(monkeypatch, reply='[{"title": "no number"}, "a bare string", 3]')

    result = extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)

    assert result.clauses == [{"title": "no number"}, "a bare string", 3]


def test_a_json_object_raises_the_same_value_error(monkeypatch, telemetry):
    _install_client(monkeypatch, reply='{"clauses": []}')

    with pytest.raises(ValueError, match=r"^Expected a JSON array of clauses$") as excinfo:
        extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)

    assert not isinstance(excinfo.value, json.JSONDecodeError)


def test_non_json_raises_json_decode_error(monkeypatch, telemetry):
    _install_client(monkeypatch, reply="Here are the clauses: 4.1 Context")

    with pytest.raises(json.JSONDecodeError):
        extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)


def test_not_configured_propagates(monkeypatch, telemetry):
    def _no_key():
        raise LLMNotConfiguredError("api_key")

    monkeypatch.setattr(llm_client, "get_anthropic_client", _no_key)

    with pytest.raises(LLMNotConfiguredError) as excinfo:
        extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)

    assert excinfo.value.reason == "api_key"


def test_egress_off_propagates_and_nothing_is_sent(monkeypatch, telemetry):
    client = _install_client(monkeypatch, reply="[]")
    monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: company_id != COMPANY_ID)

    with pytest.raises(LLMEgressDisabledError):
        extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)

    assert client.messages.calls == []
    assert telemetry.added == []


def test_sdk_errors_propagate_unchanged(monkeypatch, telemetry):
    class _Boom(RuntimeError):
        pass

    client = _install_client(monkeypatch)

    def _raise(**kwargs):
        raise _Boom("upstream exploded")

    monkeypatch.setattr(client.messages, "create", _raise)

    with pytest.raises(_Boom, match="upstream exploded"):
        extract_clauses_with_llm(_SHORT_TEXT, "AS9100D", "2016", COMPANY_ID)

    # run_llm_task still records its failure row before re-raising.
    assert [row.success for row in telemetry.added] == [False]


def test_the_handler_no_longer_builds_the_prompt_itself():
    """The endpoint delegates; the prompt text exists in one place only."""
    import inspect

    from app.api.endpoints import qms_standards

    handler_source = inspect.getsource(qms_standards.upload_pdf_and_extract_clauses)
    assert "extract_clauses_with_llm(" in handler_source
    assert "run_llm_task" not in handler_source
    assert "You are a QMS standards expert" not in inspect.getsource(qms_standards)
    assert qms_extraction_service.QMS_DOCUMENT_TEXT_MAX_CHARS == 100000
