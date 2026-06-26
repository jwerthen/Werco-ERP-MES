"""API tests for POST /api/v1/copilot/chat and the NL-search LLM upgrade.

All Anthropic interaction is mocked — the copilot loop via a scripted
``run_llm_task`` double in ``app.services.copilot_service``, the NL-search
intent parse via ``app.services.llm_client.run_llm_task`` (imported lazily by
the search endpoint). No live API calls.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy.orm import Session

import app.api.endpoints.copilot as copilot_endpoint
import app.services.copilot_service as copilot_service
import app.services.llm_client as llm_client
from app.models.ai_learning import AIInteractionEvent
from app.models.audit_log import AuditLog
from app.models.work_order import WorkOrder

pytestmark = pytest.mark.api

CHAT_URL = "/api/v1/copilot/chat"
NL_URL = "/api/v1/search/nl"


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, tool_input: Dict[str, Any], block_id: str = "tu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


class FakeLLMResult:
    def __init__(self, blocks: List[Any]):
        self.model = "claude-sonnet-4-6"
        self.tier = "default"
        self.raw_response = SimpleNamespace(content=blocks)
        self.text = next((b.text for b in blocks if getattr(b, "type", None) == "text"), "")


class ScriptedLLM:
    def __init__(self, responses: List[FakeLLMResult]):
        self.responses = responses
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, ctx, **kwargs):
        self.calls.append({"ctx": ctx, **kwargs})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    copilot_endpoint._rate_buckets.clear()
    yield
    copilot_endpoint._rate_buckets.clear()


def _sse_frames(body: str) -> List[dict]:
    return [json.loads(chunk[len("data: ") :]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


# ---------------------------------------------------------------------------
# Auth / validation
# ---------------------------------------------------------------------------
class TestAuthAndValidation:
    def test_unauthenticated_request_rejected(self, client):
        response = client.post(CHAT_URL, json={"messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 401

    def test_last_message_must_be_from_user(self, client, auth_headers):
        response = client.post(
            CHAT_URL,
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]},
        )
        assert response.status_code == 422

    def test_empty_messages_rejected(self, client, auth_headers):
        response = client.post(CHAT_URL, headers=auth_headers, json={"messages": []})
        assert response.status_code == 422

    def test_per_user_rate_limit(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(copilot_endpoint, "COPILOT_RATE_LIMIT_PER_MINUTE", 1)
        scripted = ScriptedLLM([FakeLLMResult([_text_block("ok")])])
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)

        first = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert first.status_code == 200
        second = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": "hi again"}]},
        )
        assert second.status_code == 429

    def test_validation_failure_does_not_consume_rate_limit(self, client, auth_headers, monkeypatch):
        """A 422 (bad history) must not burn the caller's per-minute budget."""
        monkeypatch.setattr(copilot_endpoint, "COPILOT_RATE_LIMIT_PER_MINUTE", 1)
        scripted = ScriptedLLM([FakeLLMResult([_text_block("ok")])])
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)

        bad = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]},
        )
        assert bad.status_code == 422
        good = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert good.status_code == 200  # the single token was still available


# ---------------------------------------------------------------------------
# Non-streaming path (?stream=false)
# ---------------------------------------------------------------------------
class TestNonStreaming:
    def test_json_response_contract(
        self, client, auth_headers, monkeypatch, db_session: Session, test_work_order: WorkOrder
    ):
        scripted = ScriptedLLM(
            [
                FakeLLMResult(
                    [_tool_use_block("lookup_work_order", {"number_or_id": test_work_order.work_order_number})]
                ),
                FakeLLMResult([_text_block(f"{test_work_order.work_order_number} is in DRAFT.")]),
            ]
        )
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)

        response = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": f"where is {test_work_order.work_order_number}?"}]},
        )
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == {"answer", "references", "tool_trace", "interaction_id", "rounds", "truncated"}
        assert data["rounds"] == 1
        assert data["truncated"] is False
        assert data["tool_trace"][0]["tool"] == "lookup_work_order"
        ref = data["references"][0]
        assert ref["type"] == "work_order"
        assert ref["label"] == test_work_order.work_order_number
        assert ref["url"] == f"/work-orders/{test_work_order.id}"

        # The turn was recorded for the learning loop and committed.
        event = db_session.query(AIInteractionEvent).filter(AIInteractionEvent.id == data["interaction_id"]).one()
        assert event.source_module == "copilot"
        assert event.company_id == 1

    def test_chat_turn_writes_no_audit_rows(
        self, client, auth_headers, monkeypatch, db_session: Session, test_work_order: WorkOrder
    ):
        scripted = ScriptedLLM([FakeLLMResult([_text_block("nothing to do")])])
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)
        response = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": "anything blocked?"}]},
        )
        assert response.status_code == 200
        assert db_session.query(AuditLog).count() == 0

    def test_llm_not_configured_returns_503(self, client, auth_headers, monkeypatch):
        def raise_not_configured(ctx, **kwargs):
            raise llm_client.LLMNotConfiguredError("api_key")

        monkeypatch.setattr(copilot_service, "run_llm_task", raise_not_configured)
        response = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 503

    def test_api_failure_returns_502_without_internals(self, client, auth_headers, monkeypatch):
        def raise_api_error(ctx, **kwargs):
            raise RuntimeError("upstream exploded with secret details")

        monkeypatch.setattr(copilot_service, "run_llm_task", raise_api_error)
        response = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 502
        assert "secret details" not in response.text

    def test_egress_disabled_returns_403(self, client, auth_headers, monkeypatch):
        """AI egress OFF for the company maps to 403 (a policy decision, not a
        server error) on the non-streaming path."""

        def raise_egress_off(ctx, **kwargs):
            raise llm_client.LLMEgressDisabledError(company_id=1)

        monkeypatch.setattr(copilot_service, "run_llm_task", raise_egress_off)
        response = client.post(
            CHAT_URL,
            headers=auth_headers,
            params={"stream": "false"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# SSE streaming path (default)
# ---------------------------------------------------------------------------
class TestStreaming:
    def test_sse_event_framing(self, client, auth_headers, monkeypatch, test_work_order: WorkOrder):
        scripted = ScriptedLLM(
            [
                FakeLLMResult(
                    [_tool_use_block("lookup_work_order", {"number_or_id": test_work_order.work_order_number})]
                ),
                FakeLLMResult([_text_block("Here is the status of that job.")]),
            ]
        )
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)

        response = client.post(
            CHAT_URL,
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": f"status of {test_work_order.work_order_number}"}]},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        frames = _sse_frames(response.text)
        types = [frame["type"] for frame in frames]
        assert types[0] == "tool_use"
        assert "delta" in types
        assert types[-1] == "final"
        assert types.index("tool_use") < types.index("delta")

        final = frames[-1]
        assert final["answer"] == "Here is the status of that job."
        delta_text = "".join(frame["text"] for frame in frames if frame["type"] == "delta")
        assert delta_text.strip() == final["answer"]

    def test_stream_error_emits_error_frame_not_500(self, client, auth_headers, monkeypatch):
        def raise_api_error(ctx, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(copilot_service, "run_llm_task", raise_api_error)
        response = client.post(CHAT_URL, headers=auth_headers, json={"messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200  # stream already started; error travels in-band
        frames = _sse_frames(response.text)
        assert frames[-1]["type"] == "error"
        assert "boom" not in frames[-1]["message"]

    def test_egress_disabled_emits_error_frame(self, client, auth_headers, monkeypatch):
        """On the streaming path, AI egress OFF travels in-band as a terminal SSE
        error frame (200, not 403 -- the stream has already started)."""

        def raise_egress_off(ctx, **kwargs):
            raise llm_client.LLMEgressDisabledError(company_id=1)

        monkeypatch.setattr(copilot_service, "run_llm_task", raise_egress_off)
        response = client.post(CHAT_URL, headers=auth_headers, json={"messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        frames = _sse_frames(response.text)
        assert frames[-1]["type"] == "error"
        assert "disabled" in frames[-1]["message"].lower()


# ---------------------------------------------------------------------------
# NL search — rule parser first, LLM assist, fallback, stable contract
# ---------------------------------------------------------------------------
NL_RESPONSE_KEYS = {"query", "confidence", "interpreted_filters", "used_fallback", "results"}
NL_RESULT_KEYS = {"id", "type", "title", "subtitle", "url", "icon", "explanation", "matched_filters"}


class TestNaturalLanguageSearchLLM:
    def test_confident_rule_parse_skips_llm(self, client, auth_headers, monkeypatch):
        def fail_if_called(ctx, **kwargs):
            raise AssertionError("LLM must not be consulted when rules are confident")

        monkeypatch.setattr(llm_client, "run_llm_task", fail_if_called)
        response = client.post(NL_URL, headers=auth_headers, json={"query": "late blocked jobs"})
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == NL_RESPONSE_KEYS
        assert data["interpreted_filters"]["parser"] == "rules"

    def test_llm_path_used_when_rules_weak(self, client, auth_headers, monkeypatch, test_work_order: WorkOrder):
        llm_filters = {
            "late": True,
            "blocked": False,
            "material_missing": False,
            "hot": True,
            "work_center_terms": ["laser"],
            "active_jobs": True,
        }
        scripted = ScriptedLLM([FakeLLMResult([_text_block(json.dumps(llm_filters))])])
        monkeypatch.setattr(llm_client, "run_llm_task", scripted)

        response = client.post(NL_URL, headers=auth_headers, json={"query": "anything slipping?"})
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == NL_RESPONSE_KEYS
        assert data["interpreted_filters"]["parser"] == "llm"
        assert data["used_fallback"] is False
        assert len(scripted.calls) == 1
        assert scripted.calls[0]["ctx"].task == "nl_search"
        # The 3s NL budget is wall-clock honest: no SDK retries behind the timeout.
        assert scripted.calls[0]["max_retries"] == 0
        for item in data["results"]:
            assert set(item.keys()) == NL_RESULT_KEYS

    def test_llm_failure_falls_back_to_rules(self, client, auth_headers, monkeypatch):
        def raise_api_error(ctx, **kwargs):
            raise RuntimeError("anthropic 529")

        monkeypatch.setattr(llm_client, "run_llm_task", raise_api_error)
        response = client.post(NL_URL, headers=auth_headers, json={"query": "anything late?"})
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == NL_RESPONSE_KEYS
        assert data["interpreted_filters"]["parser"] == "rules"

    def test_egress_disabled_falls_back_to_rules_silently(self, client, auth_headers, monkeypatch):
        """AI egress OFF for the company is a policy state, not a failure: the NL
        intent parse returns None and the rule parser covers the query silently
        (parser='rules', a normal 200 response -- no error surfaced)."""

        def raise_egress_off(ctx, **kwargs):
            raise llm_client.LLMEgressDisabledError(company_id=1)

        monkeypatch.setattr(llm_client, "run_llm_task", raise_egress_off)
        response = client.post(NL_URL, headers=auth_headers, json={"query": "anything slipping?"})
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == NL_RESPONSE_KEYS
        assert data["interpreted_filters"]["parser"] == "rules"

    def test_llm_garbage_output_falls_back_to_rules(self, client, auth_headers, monkeypatch):
        scripted = ScriptedLLM([FakeLLMResult([_text_block("Sorry, I cannot help with that.")])])
        monkeypatch.setattr(llm_client, "run_llm_task", scripted)
        response = client.post(NL_URL, headers=auth_headers, json={"query": "anything late?"})
        assert response.status_code == 200
        assert response.json()["interpreted_filters"]["parser"] == "rules"

    def test_llm_work_center_terms_sanitized(self, client, auth_headers, monkeypatch):
        llm_filters = {
            "late": False,
            "blocked": False,
            "material_missing": False,
            "hot": False,
            "work_center_terms": ["LASER%'; DROP TABLE--", "weld_"],
            "active_jobs": True,
        }
        scripted = ScriptedLLM([FakeLLMResult([_text_block(json.dumps(llm_filters))])])
        monkeypatch.setattr(llm_client, "run_llm_task", scripted)
        response = client.post(NL_URL, headers=auth_headers, json={"query": "whats cooking"})
        assert response.status_code == 200
        terms = response.json()["interpreted_filters"]["work_center_terms"]
        for term in terms:
            assert "%" not in term
            assert "'" not in term
            assert "_" not in term
