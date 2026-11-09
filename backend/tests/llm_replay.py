"""Replay scripted Anthropic responses through the REAL ``run_llm_task``.

Minimal version (roadmap #9-4); #9-5b extends it for the offline eval goldens.

Most AI tests stub ``run_llm_task`` itself, which skips the wrapper: model routing,
the per-company egress gate, the request layout the SDK actually receives, and the
usage telemetry row. This module stubs one layer lower instead. ``install_replay``
replaces ``llm_client.get_anthropic_client`` with a ``ReplayClient`` that hands back
scripted ``anthropic.types.Message`` objects in order, and points
``llm_client._usage_session_factory`` at a ``RecordingSession`` so each telemetry row
is captured rather than written. Everything between the feature code and the SDK runs
for real. ``run_llm_task`` is never patched.

A script is a contract, in both directions:

* **An extra call fails the test.** It raises ``ReplayExhausted``, a
  ``BaseException`` on purpose. The AI pipelines degrade inside broad
  ``except Exception`` blocks, so an ordinary exception would be swallowed into a
  plausible-looking degraded result and the test could pass.
* **A response nobody asked for fails the test** at teardown, through the
  ``llm_replay`` fixture (``ReplayClient.assert_exhausted``). A pipeline that silently
  skipped its verification pass leaves its second response unread.

Offline by construction: nothing here can reach the network, and no API key is read.

Usage::

    from tests.llm_replay import llm_replay, text_message  # noqa: F401  (fixture import)

    def test_something(llm_replay):
        replay = llm_replay([text_message('{"a": 1}'), text_message('{"a": 1}')])
        ...  # call the feature
        assert len(replay.requests) == 2
"""

from __future__ import annotations

import itertools
from typing import Any, Callable, Dict, Iterator, List, Mapping, Sequence

import pytest
from anthropic.types import Message

import app.services.llm_client as llm_client

_message_ids = itertools.count(1)


class ReplayExhausted(BaseException):
    """The code under test made more Anthropic calls than the script has responses.

    A ``BaseException`` so it escapes the pipelines' ``except Exception`` degrade
    paths and fails the test where it happened, instead of degrading.
    """


def text_message(
    text: str,
    *,
    model: str = "claude-replay",
    input_tokens: int = 100,
    output_tokens: int = 20,
    stop_reason: str = "end_turn",
) -> Dict[str, Any]:
    """A Messages API response carrying one text block, as ``Message`` JSON."""
    return {
        "id": f"msg_replay_{next(_message_ids)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class RecordingSession:
    """Stands in for the telemetry session ``_usage_session_factory`` opens.

    Committed objects land in ``sink``; a rollback drops what was pending. Any read
    (``execute``) raises, so a test that restores the real egress gate sees it fail
    CLOSED, the same way it does when the database is unreachable.
    """

    def __init__(self, sink: List[Any]):
        self._sink = sink
        self._pending: List[Any] = []

    def add(self, obj: Any) -> None:
        self._pending.append(obj)

    def commit(self) -> None:
        self._sink.extend(self._pending)
        self._pending.clear()

    def rollback(self) -> None:
        self._pending.clear()

    def close(self) -> None:
        self._pending.clear()

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("RecordingSession records telemetry writes only; it cannot answer reads")


class _ReplayMessages:
    def __init__(self, owner: "ReplayClient"):
        self._owner = owner

    def create(self, **kwargs: Any) -> Message:
        return self._owner._next_response(kwargs)


class ReplayClient:
    """A scripted stand-in for ``anthropic.Anthropic``.

    ``requests`` holds every ``messages.create`` kwargs dict in call order, so a test
    can assert what the model was actually sent. ``options`` holds every
    ``with_options`` call (the per-call timeout / retry budget). ``usage_rows`` holds
    the ``AIUsageEvent`` objects ``run_llm_task`` committed.
    """

    def __init__(self, responses: Sequence[Mapping[str, Any]]):
        # Validated up front: a malformed script fails at install, not mid-pipeline,
        # where a broad except could turn it into a degraded result.
        self._responses: List[Message] = [Message.model_validate(dict(response)) for response in responses]
        self.requests: List[Dict[str, Any]] = []
        self.options: List[Dict[str, Any]] = []
        self.usage_rows: List[Any] = []
        self.extra_calls = 0
        self.messages = _ReplayMessages(self)

    def with_options(self, **kwargs: Any) -> "ReplayClient":
        self.options.append(dict(kwargs))
        return self

    def _next_response(self, kwargs: Dict[str, Any]) -> Message:
        self.requests.append(kwargs)
        index = len(self.requests) - 1
        if index >= len(self._responses):
            self.extra_calls += 1
            raise ReplayExhausted(
                f"Anthropic call #{index + 1} was not scripted: the replay holds {len(self._responses)} response(s)"
            )
        return self._responses[index]

    @property
    def remaining(self) -> int:
        return max(0, len(self._responses) - len(self.requests))

    def assert_exhausted(self) -> None:
        """Fail unless every scripted response was requested, and nothing more."""
        if self.extra_calls:
            raise AssertionError(f"{self.extra_calls} Anthropic call(s) ran past the end of the replay script")
        if self.remaining:
            raise AssertionError(
                f"{self.remaining} scripted response(s) were never requested: "
                f"the code made {len(self.requests)} of {len(self._responses)} expected Anthropic calls"
            )


def install_replay(monkeypatch: pytest.MonkeyPatch, responses: Sequence[Mapping[str, Any]]) -> ReplayClient:
    """Serve ``responses`` to the real ``run_llm_task`` and record its telemetry.

    Patches ``llm_client.get_anthropic_client`` and ``llm_client._usage_session_factory``
    only. Prefer the ``llm_replay`` fixture, which also checks at teardown that the
    script was used up.
    """
    client = ReplayClient(responses)
    monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: client)
    monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: RecordingSession(client.usage_rows))
    return client


@pytest.fixture
def llm_replay(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Sequence[Mapping[str, Any]]], ReplayClient]]:
    """Factory fixture: ``llm_replay([...responses])`` installs a ``ReplayClient``.

    At teardown, every client it installed must have served exactly its script.
    """
    installed: List[ReplayClient] = []

    def _install(responses: Sequence[Mapping[str, Any]]) -> ReplayClient:
        client = install_replay(monkeypatch, responses)
        installed.append(client)
        return client

    yield _install
    for client in installed:
        client.assert_exhausted()
