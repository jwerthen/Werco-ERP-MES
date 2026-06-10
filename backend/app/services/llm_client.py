"""Shared Anthropic client wrapper with usage telemetry and prompt caching.

All LLM call sites route through :func:`run_llm_task`, which:

- holds a lazy module-level singleton ``anthropic.Anthropic`` client,
- selects the model via the existing :func:`select_anthropic_model` router,
- forwards ``system``/``messages`` verbatim (including ``cache_control``
  blocks for prompt caching),
- captures ``response.usage`` (input/output/cache-write/cache-read tokens),
  latency, and an estimated USD cost from :data:`MODEL_PRICING_USD_PER_MTOK`,
- records a tenant-scoped :class:`~app.models.ai_usage.AIUsageEvent` row on a
  short-lived dedicated session so telemetry can never entangle or break the
  caller's transaction.

Telemetry is strictly fire-and-forget: any failure (DB down, missing company
context, unknown model pricing) logs a warning and the AI result is still
returned to the caller.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from app.services.llm_model_router import LLMModelDecision, LLMTaskContext, select_anthropic_model

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
# USD per million tokens (MTok). Fetched 2026-06-09 from
# https://docs.claude.com/en/docs/about-claude/pricing
# (redirects to https://platform.claude.com/docs/en/about-claude/pricing).
# cache_write_5m is the 5-minute prompt-cache write rate (1.25x input);
# cache_read is the cache hit/refresh rate (0.1x input).
# EDIT HERE when Anthropic pricing changes or new models are pinned.
# Unknown models record estimated_cost_usd as NULL — they never crash a call.
MODEL_PRICING_USD_PER_MTOK: Dict[str, Dict[str, Decimal]] = {
    "claude-haiku-4-5-20251001": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
        "cache_write_5m": Decimal("1.25"),
        "cache_read": Decimal("0.10"),
    },
    "claude-haiku-4-5": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
        "cache_write_5m": Decimal("1.25"),
        "cache_read": Decimal("0.10"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_write_5m": Decimal("3.75"),
        "cache_read": Decimal("0.30"),
    },
    "claude-opus-4-8": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
        "cache_write_5m": Decimal("6.25"),
        "cache_read": Decimal("0.50"),
    },
}

_MTOK = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")


class LLMNotConfiguredError(RuntimeError):
    """Raised when the Anthropic SDK is missing or no API key is configured.

    ``reason`` is ``"library"`` (anthropic package not importable) or
    ``"api_key"`` (ANTHROPIC_API_KEY unset) so callers can keep their existing
    user-facing error strings.
    """

    def __init__(self, reason: str):
        self.reason = reason
        message = "anthropic package not installed" if reason == "library" else "ANTHROPIC_API_KEY not set"
        super().__init__(message)


@dataclass
class LLMTaskResult:
    """Result of one LLM call routed through :func:`run_llm_task`."""

    text: str
    model: str
    tier: str
    model_selection_reason: str
    prompt_version: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    estimated_cost_usd: Optional[Decimal]
    latency_ms: int
    raw_response: Any


# ---------------------------------------------------------------------------
# Lazy singleton client
# ---------------------------------------------------------------------------
_client: Optional[Any] = None
_client_lock = threading.Lock()


def get_anthropic_client() -> Any:
    """Return the module-level singleton ``anthropic.Anthropic`` client.

    The client is created lazily on first use with the API key from the
    environment (matching the pre-wrapper call sites). Per-call timeouts are
    applied with ``client.with_options(...)`` so one shared connection pool
    serves every feature.
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on env
                raise LLMNotConfiguredError("library") from exc

            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise LLMNotConfiguredError("api_key")
            _client = anthropic.Anthropic(api_key=api_key)
    return _client


def reset_anthropic_client() -> None:
    """Drop the cached client (used by tests and key-rotation paths)."""
    global _client
    with _client_lock:
        _client = None


def is_anthropic_api_error(exc: BaseException) -> bool:
    """True when ``exc`` is an ``anthropic.APIError`` (without a hard import)."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover - depends on env
        return False
    return isinstance(exc, anthropic.APIError)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------
def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> Optional[Decimal]:
    """Estimated USD cost for one call, or None when the model is unpriced."""
    pricing = MODEL_PRICING_USD_PER_MTOK.get(model)
    if pricing is None:
        return None
    total = (
        Decimal(input_tokens) * pricing["input"]
        + Decimal(output_tokens) * pricing["output"]
        + Decimal(cache_creation_tokens) * pricing["cache_write_5m"]
        + Decimal(cache_read_tokens) * pricing["cache_read"]
    ) / _MTOK
    return total.quantize(_COST_QUANTUM)


# ---------------------------------------------------------------------------
# Telemetry (fire-and-forget, dedicated session)
# ---------------------------------------------------------------------------
def _usage_session_factory() -> Any:
    """Open a short-lived session dedicated to the telemetry write.

    Imported lazily so importing this module never requires a configured
    database. Tests monkeypatch this function.
    """
    from app.db.session import SessionLocal

    return SessionLocal()


def _record_usage_event(
    *,
    company_id: Optional[int],
    task: str,
    model: str,
    tier: Optional[str],
    feature: Optional[str],
    prompt_version: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    estimated_cost_usd: Optional[Decimal],
    latency_ms: Optional[int],
    success: bool,
    error_type: Optional[str],
) -> None:
    """Persist one AIUsageEvent row. Never raises — telemetry must not break the caller."""
    try:
        if company_id is None:
            logger.warning("AI usage telemetry skipped for task %s: no company context", task)
            return

        from app.models.ai_usage import AIUsageEvent

        db = _usage_session_factory()
        try:
            db.add(
                AIUsageEvent(
                    company_id=company_id,
                    task=task,
                    model=model,
                    tier=tier,
                    feature=feature,
                    prompt_version=prompt_version,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_tokens=cache_creation_tokens,
                    cache_read_tokens=cache_read_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    latency_ms=latency_ms,
                    success=success,
                    error_type=error_type,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Failed to record AI usage event for task %s: %s", task, exc)


def _usage_tokens(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage", None)
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_creation_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    }


def _first_text(response: Any) -> str:
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text" or hasattr(block, "text"):
            return str(block.text)
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_llm_task(
    ctx: LLMTaskContext,
    *,
    messages: List[Dict[str, Any]],
    system: Optional[Union[str, List[Dict[str, Any]]]] = None,
    max_tokens: int = 4096,
    company_id: Optional[int] = None,
    feature: Optional[str] = None,
    prompt_version: Optional[str] = None,
    timeout: Optional[float] = None,
) -> LLMTaskResult:
    """Run one Anthropic Messages API call with model routing and telemetry.

    Args:
        ctx: Task context fed to ``select_anthropic_model`` (model routing is
            NOT duplicated here).
        messages: Messages array, forwarded verbatim (``cache_control`` blocks
            pass through untouched).
        system: Optional system prompt — a plain string or a list of content
            blocks. Use block form with ``cache_control: {"type": "ephemeral"}``
            to enable prompt caching on the stable prefix.
        max_tokens: Response token cap.
        company_id: Active company for tenant-scoped usage telemetry. When
            None the call still runs; telemetry is skipped with a warning.
        feature: Product surface label recorded on the usage row.
        prompt_version: Version string from ``app.services.prompts``.
        timeout: Per-call SDK timeout in seconds (None = SDK default).

    Raises:
        LLMNotConfiguredError: SDK missing or API key unset (no telemetry row).
        Exception: API errors are re-raised after a failure telemetry row is
            recorded.
    """
    client = get_anthropic_client()
    if timeout is not None:
        client = client.with_options(timeout=timeout)

    decision: LLMModelDecision = select_anthropic_model(ctx)

    create_kwargs: Dict[str, Any] = {
        "model": decision.model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system is not None:
        create_kwargs["system"] = system

    started = time.monotonic()
    try:
        response = client.messages.create(**create_kwargs)
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _record_usage_event(
            company_id=company_id,
            task=ctx.task,
            model=decision.model,
            tier=decision.tier.value,
            feature=feature,
            prompt_version=prompt_version,
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            estimated_cost_usd=None,
            latency_ms=latency_ms,
            success=False,
            error_type=type(exc).__name__,
        )
        raise

    latency_ms = int((time.monotonic() - started) * 1000)
    tokens = _usage_tokens(response)
    cost = estimate_cost_usd(decision.model, **tokens)
    if cost is None:
        logger.warning("No pricing entry for model %s; recording estimated_cost_usd as NULL", decision.model)

    _record_usage_event(
        company_id=company_id,
        task=ctx.task,
        model=decision.model,
        tier=decision.tier.value,
        feature=feature,
        prompt_version=prompt_version,
        estimated_cost_usd=cost,
        latency_ms=latency_ms,
        success=True,
        error_type=None,
        **tokens,
    )

    return LLMTaskResult(
        text=_first_text(response),
        model=decision.model,
        tier=decision.tier.value,
        model_selection_reason=decision.reason,
        prompt_version=prompt_version,
        estimated_cost_usd=cost,
        latency_ms=latency_ms,
        raw_response=response,
        **tokens,
    )
