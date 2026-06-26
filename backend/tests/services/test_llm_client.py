"""Unit tests for the shared Anthropic client wrapper (no live API calls)."""

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.services.llm_client as llm_client
from app.services.llm_client import (
    LLMEgressDisabledError,
    LLMNotConfiguredError,
    estimate_cost_usd,
    reset_anthropic_client,
    run_llm_task,
)
from app.services.llm_model_router import LLMTaskContext

pytestmark = pytest.mark.unit

# Capture the REAL ``_ai_egress_allowed`` at import time -- before the autouse
# ``_allow_ai_egress_by_default`` conftest fixture monkeypatches the module
# attribute to ``True``. The kill-switch unit tests below drive this captured
# function directly so they exercise the genuine fail-closed logic.
_REAL_AI_EGRESS_ALLOWED = llm_client._ai_egress_allowed


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


# ---------------------------------------------------------------------------
# AI egress kill switch — _ai_egress_allowed (the resolver, fail-closed)
# ---------------------------------------------------------------------------
class _EgressSession:
    """Minimal session whose ``execute(...).scalar_one_or_none()`` is scripted.

    Mirrors the single call ``_ai_egress_allowed`` makes:
    ``db.execute(select(...)).scalar_one_or_none()``. ``close()`` is recorded so
    every path can be proven to release the session.
    """

    def __init__(self, scalar=None, raise_on_execute: BaseException = None):
        self._scalar = scalar
        self._raise = raise_on_execute
        self.closed = False

    def execute(self, *args, **kwargs):
        if self._raise is not None:
            raise self._raise
        return SimpleNamespace(scalar_one_or_none=lambda: self._scalar)

    def close(self):
        self.closed = True


class TestAIEgressAllowed:
    """The genuine fail-closed resolver — driven via the import-time capture so the
    autouse ``_allow_ai_egress_by_default`` fixture (which patches the module attr
    to ``True``) never masks the real logic."""

    def test_no_company_context_allows_with_warning(self, monkeypatch, caplog):
        # company_id is None: there is no tenant to scope, so the resolver short-
        # circuits to True (the test/internal edge) and must NOT open a session.
        def _must_not_open():
            raise AssertionError("_usage_session_factory must not run when company_id is None")

        monkeypatch.setattr(llm_client, "_usage_session_factory", _must_not_open)
        with caplog.at_level(logging.WARNING):
            assert _REAL_AI_EGRESS_ALLOWED(None) is True
        assert any("no company context" in record.message for record in caplog.records)

    def test_flag_true_row_allows_and_closes_session(self, monkeypatch):
        session = _EgressSession(scalar=True)
        monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: session)
        assert _REAL_AI_EGRESS_ALLOWED(1) is True
        assert session.closed

    def test_flag_false_row_denies_and_closes_session(self, monkeypatch):
        session = _EgressSession(scalar=False)
        monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: session)
        assert _REAL_AI_EGRESS_ALLOWED(1) is False
        assert session.closed

    def test_company_not_found_denies_and_closes_session(self, monkeypatch, caplog):
        # scalar_one_or_none() -> None: unknown tenant, fail closed.
        session = _EgressSession(scalar=None)
        monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: session)
        with caplog.at_level(logging.WARNING):
            assert _REAL_AI_EGRESS_ALLOWED(999) is False
        assert session.closed
        assert any("not found" in record.message for record in caplog.records)

    def test_session_factory_raising_denies(self, monkeypatch, caplog):
        # The factory itself blows up (DB unreachable). No session to close;
        # the resolver must still deny (fail-closed) without propagating.
        def _explode():
            raise RuntimeError("database is down")

        monkeypatch.setattr(llm_client, "_usage_session_factory", _explode)
        with caplog.at_level(logging.ERROR):
            assert _REAL_AI_EGRESS_ALLOWED(1) is False
        assert any("fail-closed" in record.message for record in caplog.records)

    def test_execute_raising_denies_and_still_closes_session(self, monkeypatch):
        # The session opened but the query raised (e.g. missing column on stale
        # schema). Deny AND release the session in the finally block.
        session = _EgressSession(raise_on_execute=RuntimeError("no such column"))
        monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: session)
        assert _REAL_AI_EGRESS_ALLOWED(1) is False
        assert session.closed


# ---------------------------------------------------------------------------
# AI egress kill switch — the run_llm_task gate (no API call, no telemetry)
# ---------------------------------------------------------------------------
class TestRunLLMTaskEgressGate:
    def test_egress_disabled_raises_before_api_call_and_telemetry(
        self, ctx, fake_client, recording_session, monkeypatch
    ):
        """When egress is denied, run_llm_task raises LLMEgressDisabledError and
        the request never leaves the boundary: client.messages.create is NOT
        called and NO telemetry row is recorded."""
        monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: False)

        with pytest.raises(LLMEgressDisabledError) as exc_info:
            run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=5)

        assert exc_info.value.company_id == 5
        # No Anthropic call.
        assert fake_client.messages.calls == []
        # No usage row — not even a failure row.
        assert recording_session.added == []
        assert recording_session.committed is False

    def test_egress_allowed_proceeds(self, ctx, fake_client, recording_session, monkeypatch):
        """The positive control: when egress is allowed the call goes through and
        records exactly one usage row."""
        monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: True)

        result = run_llm_task(ctx, messages=[{"role": "user", "content": "q"}], company_id=5)

        assert result.text == '{"ok": true}'
        assert len(fake_client.messages.calls) == 1
        assert len(recording_session.added) == 1
        assert recording_session.added[0].success is True
