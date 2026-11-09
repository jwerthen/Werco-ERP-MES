"""Capture the exact request a real prompt builder sends to Anthropic, then stop.

``CaptureClient`` stands in for the ``anthropic.Anthropic`` singleton at the one
seam every feature uses, ``llm_client.get_anthropic_client``. The real
``run_llm_task`` therefore still runs: model routing, the egress check (forced to
"allowed") and the ``create`` kwargs it builds are all real. The client returns
any scripted earlier responses in order (so a builder that makes two calls, such
as laser pass 1 then pass 2, reaches its second call), and on the call to capture
it records the kwargs and raises :class:`RequestCaptured`.

``RequestCaptured`` derives from ``BaseException`` on purpose. Every builder
wraps its call in a broad ``except Exception`` that degrades to an empty result;
a ``BaseException`` passes straight through all of them, and through
``run_llm_task``'s own failure branch, so no telemetry row is written for the
captured call. The usage writer and the egress lookup are also replaced for the
duration of a capture, so scripted earlier calls write nothing and nothing here
needs a database.

Used by ``tests/test_prompt_drift_guard.py``. The helpers here are plain
``unittest.mock`` context managers rather than pytest fixtures so that the
drift guard can render every prompt again in a fresh subprocess (its
``PYTHONHASHSEED`` determinism check).
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import ExitStack
from typing import Any, Callable, Dict, Iterable, List, Optional
from unittest.mock import patch

import anthropic
from anthropic.types import Message

import app.services.llm_client as llm_client

# ``client.messages.create`` kwargs that are part of what the model sees, in the
# order the canonical form keeps them. ``tools`` renders before ``system`` in the
# prompt prefix, so its order is kept too: a reordered tool list is a different
# cached prefix.
KEPT_REQUEST_KEYS = ("system", "messages", "tools", "tool_choice", "extra_body")

# Budgets, not layout. The model id is env-routed (and locked separately as the
# router's defaults), and the timeout/retry options never reach ``create``.
DROPPED_REQUEST_KEYS = ("model", "max_tokens")

BASE64_PLACEHOLDER = "<base64>"


class RequestCaptured(BaseException):
    """Raised by :class:`CaptureClient` on the captured call. Escapes ``except Exception``."""

    def __init__(self, request: Dict[str, Any]) -> None:
        super().__init__("request captured")
        self.request = request


class CaptureNotReached(AssertionError):
    """The builder returned without making the call the render case expected to capture."""


def _as_message(response: Any) -> Any:
    if isinstance(response, dict):
        return Message.model_validate(response)
    return response


def text_message(text: str) -> Dict[str, Any]:
    """A minimal scripted ``Message`` whose single block is ``text``."""
    return {
        "id": "msg_capture_text",
        "type": "message",
        "role": "assistant",
        "model": "capture",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def tool_use_message(name: str, tool_input: Dict[str, Any], *, tool_use_id: str = "toolu_capture_1") -> Dict[str, Any]:
    """A minimal scripted ``Message`` that asks for one tool call."""
    return {
        "id": "msg_capture_tool_use",
        "type": "message",
        "role": "assistant",
        "model": "capture",
        "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


class _Messages:
    def __init__(self, owner: "CaptureClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        return self._owner._create(kwargs)


class CaptureClient:
    """A fake Anthropic client: scripted responses first, then capture and stop."""

    def __init__(self, scripted_responses: Iterable[Any] = ()) -> None:
        self._scripted: List[Any] = [_as_message(response) for response in scripted_responses]
        self.requests: List[Dict[str, Any]] = []
        self.options: List[Dict[str, Any]] = []
        self.messages = _Messages(self)

    def with_options(self, **kwargs: Any) -> "CaptureClient":
        self.options.append(dict(kwargs))
        return self

    def _create(self, kwargs: Dict[str, Any]) -> Any:
        # Deep-copied: a builder may keep mutating the list it passed (the copilot
        # loop appends to its messages after each call).
        request = copy.deepcopy(kwargs)
        self.requests.append(request)
        if self._scripted:
            return self._scripted.pop(0)
        raise RequestCaptured(request)


def _no_usage_row(**_kwargs: Any) -> None:
    return None


def capture_request(
    build: Callable[[], Any],
    *,
    scripted: Iterable[Any] = (),
    patches: Iterable[Any] = (),
) -> Dict[str, Any]:
    """Run ``build`` against a :class:`CaptureClient` and return the captured ``create`` kwargs.

    Args:
        build: Calls the real request builder with fixed inputs.
        scripted: Responses (``Message`` dicts or objects) returned, in order,
            before the captured call.
        patches: Extra context managers (e.g. ``patch.object(...)``) entered for
            the duration of the build.

    Raises:
        CaptureNotReached: the builder returned without reaching the captured call
            (for example, it stopped before the call the scripted responses lead to).
    """
    client = CaptureClient(scripted)
    with ExitStack() as stack:
        stack.enter_context(patch.object(llm_client, "get_anthropic_client", lambda: client))
        stack.enter_context(patch.object(llm_client, "_ai_egress_allowed", lambda company_id: True))
        stack.enter_context(patch.object(llm_client, "_record_usage_event", _no_usage_row))
        for extra in patches:
            stack.enter_context(extra)
        try:
            result = build()
            # A generator builder (the copilot stream) only runs when consumed.
            if hasattr(result, "__next__"):
                for _event in result:
                    pass
        except RequestCaptured as captured:
            return captured.request
    raise CaptureNotReached(
        f"The builder returned without reaching the captured call ({len(client.requests)} call(s) made). "
        "A render case that captures nothing would lock nothing."
    )


# ---------------------------------------------------------------------------
# Canonical form and fingerprints
# ---------------------------------------------------------------------------
def _sentinel(value: Any) -> str:
    """``json.dumps`` default: SDK "not given" sentinels become fixed strings; anything else fails."""
    if isinstance(value, anthropic.NotGiven):
        return "<NOT_GIVEN>"
    if isinstance(value, anthropic.Omit):
        return "<OMIT>"
    raise TypeError(f"Unserializable value in a captured request: {type(value).__name__}")


def _strip_base64(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("type") == "base64" and "data" in value:
            return {key: (BASE64_PLACEHOLDER if key == "data" else _strip_base64(item)) for key, item in value.items()}
        return {key: _strip_base64(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_base64(item) for item in value]
    return value


def canonical_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce captured ``create`` kwargs to what the model sees, in insertion order.

    Raises ``ValueError`` on a key that is neither kept nor dropped, so a new
    request parameter (``temperature``, ``thinking``, ...) cannot slip past the
    guard unhashed: decide which list it belongs in.
    """
    unknown = [key for key in request if key not in KEPT_REQUEST_KEYS and key not in DROPPED_REQUEST_KEYS]
    if unknown:
        raise ValueError(
            f"Captured request has unclassified key(s) {unknown}. Add each to KEPT_REQUEST_KEYS "
            "(the model sees it) or DROPPED_REQUEST_KEYS (a budget) in tests/llm_capture.py."
        )
    return {key: _strip_base64(request[key]) for key in KEPT_REQUEST_KEYS if key in request}


def canonical_json(value: Any) -> str:
    """Insertion-ordered (no ``sort_keys``: key order is what the SDK sends), ASCII, compact."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=_sentinel)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def prompt_fingerprint(text: Optional[str], variants: Dict[str, List[Dict[str, Any]]]) -> str:
    """SHA-256 over ``{"text": Prompt.text, "variants": {name: [canonical requests]}}``."""
    return sha256_hex({"text": text, "variants": variants})
