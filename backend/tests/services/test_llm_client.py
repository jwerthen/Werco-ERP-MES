"""Unit tests for the shared Anthropic client wrapper (no live API calls)."""

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.services.llm_client as llm_client
from app.services.llm_client import (
    LLMNotConfiguredError,
    estimate_cost_usd,
    reset_anthropic_client,
    run_llm_task,
)
from app.services.llm_model_router import LLMTaskContext

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeUsage:
    def __init__(self, input_tokens=1000, output_tokens=200, cache_creation=0, cache_read=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation
        self.cache_read_input_tokens = cache_read


class FakeResponse:
    def __init__(self, text='{"ok": true}', usage: Optional[FakeUsage] = None):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.usage = usage or FakeUsage()


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.messages = FakeMessages(response=response, error=error)
        self.with_options_calls: List[Dict[str, Any]] = []

    def with_options(self, **kwargs):
        self.with_options_calls.append(kwargs)
        return self


class RecordingSession:
    """Captures objects added via the dedicated telemetry session."""

    def __init__(self):
        self.added: List[Any] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def ctx() -> LLMTaskContext:
    return LLMTaskContext(task="po_extraction", input_chars=500)


@pytest.fixture
def fake_client(monkeypatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: client)
    return client


@pytest.fixture
def recording_session(monkeypatch) -> RecordingSession:
    session = RecordingSession()
    monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: session)
    return session


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------
class TestEstimateCost:
    def test_haiku_pinned_model(self):
        cost = estimate_cost_usd("claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == Decimal("6.000000")  # $1 in + $5 out

    def test_sonnet_with_cache(self):
        cost = estimate_cost_usd(
            "claude-sonnet-4-6",
            input_tokens=100_000,
            output_tokens=10_000,
            cache_creation_tokens=50_000,
            cache_read_tokens=200_000,
        )
        # 0.1*3 + 0.01*15 + 0.05*3.75 + 0.2*0.30 = 0.3 + 0.15 + 0.1875 + 0.06
        assert cost == Decimal("0.697500")

    def test_opus(self):
        cost = estimate_cost_usd("claude-opus-4-8", input_tokens=1_000_000, output_tokens=100_000)
        assert cost == Decimal("7.500000")  # $5 + $2.50

    def test_unknown_model_returns_none_not_crash(self):
        assert estimate_cost_usd("claude-future-9-9", input_tokens=1000, output_tokens=100) is None

    def test_zero_tokens(self):
        assert estimate_cost_usd("claude-sonnet-4-6") == Decimal("0.000000")


# ---------------------------------------------------------------------------
# run_llm_task — happy path
# ---------------------------------------------------------------------------
class TestRunLLMTask:
    def test_returns_text_and_usage(self, ctx, fake_client, recording_session):
        fake_client.messages.response = FakeResponse(
            text='{"po_number": "123"}',
            usage=FakeUsage(input_tokens=2000, output_tokens=300, cache_creation=100, cache_read=400),
        )
        result = run_llm_task(
            ctx,
            messages=[{"role": "user", "content": "extract"}],
            system="sys prompt",
            max_tokens=4096,
            company_id=1,
            feature="po_upload",
            prompt_version="1.0.0",
        )
        assert result.text == '{"po_number": "123"}'
        assert result.input_tokens == 2000
        assert result.output_tokens == 300
        assert result.cache_creation_tokens == 100
        assert result.cache_read_tokens == 400
        assert result.estimated_cost_usd is not None
        assert result.latency_ms >= 0
        assert result.prompt_version == "1.0.0"
        # Model came from the router, not hardcoded
        assert result.model
        assert result.tier in {"fast", "default", "reasoning"}

    def test_records_tenant_scoped_usage_event(self, ctx, fake_client, recording_session):
        run_llm_task(
            ctx,
            messages=[{"role": "user", "content": "extract"}],
            company_id=42,
            feature="po_upload",
            prompt_version="1.0.0",
        )
        assert recording_session.committed
        assert recording_session.closed
        assert len(recording_session.added) == 1
        event = recording_session.added[0]
        assert event.company_id == 42
        assert event.task == "po_extraction"
        assert event.feature == "po_upload"
        assert event.prompt_version == "1.0.0"
        assert event.success is True
        assert event.error_type is None
        assert event.input_tokens == 1000
        assert event.output_tokens == 200

    def test_cache_control_blocks_pass_through(self, ctx, fake_client, recording_session):
        system_blocks = [
            {"type": "text", "text": "stable prefix", "cache_control": {"type": "ephemeral"}},
        ]
        run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], system=system_blocks, company_id=1)
        sent = fake_client.messages.calls[0]
        assert sent["system"] == system_blocks

    def test_timeout_applied_via_with_options(self, ctx, fake_client, recording_session):
        run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=1, timeout=60.0)
        assert fake_client.with_options_calls == [{"timeout": 60.0}]

    def test_max_retries_applied_via_with_options(self, ctx, fake_client, recording_session):
        run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=1, max_retries=0)
        assert fake_client.with_options_calls == [{"max_retries": 0}]

    def test_timeout_and_max_retries_combined(self, ctx, fake_client, recording_session):
        run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=1, timeout=3.0, max_retries=1)
        assert fake_client.with_options_calls == [{"timeout": 3.0, "max_retries": 1}]

    def test_no_with_options_when_neither_given(self, ctx, fake_client, recording_session):
        run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=1)
        assert fake_client.with_options_calls == []

    def test_no_system_key_when_system_none(self, ctx, fake_client, recording_session):
        run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=1)
        assert "system" not in fake_client.messages.calls[0]


# ---------------------------------------------------------------------------
# run_llm_task — telemetry never breaks the caller
# ---------------------------------------------------------------------------
class TestTelemetryNeverBreaksCaller:
    def test_session_factory_raises_result_still_returned(self, ctx, fake_client, monkeypatch, caplog):
        def explode():
            raise RuntimeError("database is down")

        monkeypatch.setattr(llm_client, "_usage_session_factory", explode)
        with caplog.at_level(logging.WARNING):
            result = run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=1)
        assert result.text == '{"ok": true}'
        assert any("Failed to record AI usage event" in record.message for record in caplog.records)

    def test_commit_raises_session_rolled_back_and_closed(self, ctx, fake_client, monkeypatch, caplog):
        session = RecordingSession()

        def bad_commit():
            raise RuntimeError("commit failed")

        session.commit = bad_commit  # type: ignore[method-assign]
        monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: session)
        with caplog.at_level(logging.WARNING):
            result = run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=1)
        assert result.text == '{"ok": true}'
        assert session.rolled_back
        assert session.closed

    def test_missing_company_skips_telemetry_with_warning(self, ctx, fake_client, recording_session, caplog):
        with caplog.at_level(logging.WARNING):
            result = run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=None)
        assert result.text == '{"ok": true}'
        assert recording_session.added == []
        assert any("no company context" in record.message for record in caplog.records)

    def test_unknown_model_records_null_cost(self, ctx, fake_client, recording_session, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_PO_MODEL", "claude-unpriced-model")
        result = run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=1)
        assert result.estimated_cost_usd is None
        assert recording_session.added[0].estimated_cost_usd is None


# ---------------------------------------------------------------------------
# run_llm_task — failure path
# ---------------------------------------------------------------------------
class TestFailurePath:
    def test_api_error_recorded_then_reraised(self, ctx, monkeypatch, recording_session):
        client = FakeClient(error=ValueError("upstream 529"))
        monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: client)

        with pytest.raises(ValueError, match="upstream 529"):
            run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=7)

        assert len(recording_session.added) == 1
        event = recording_session.added[0]
        assert event.success is False
        assert event.error_type == "ValueError"
        assert event.company_id == 7
        assert event.input_tokens == 0

    def test_not_configured_error_reasons(self, monkeypatch):
        reset_anthropic_client()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(LLMNotConfiguredError) as exc_info:
            llm_client.get_anthropic_client()
        assert exc_info.value.reason == "api_key"
        reset_anthropic_client()
