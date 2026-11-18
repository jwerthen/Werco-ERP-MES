"""Self-tests for ``tests/llm_replay.py``.

The replay harness is only worth anything if it fails loudly when a pipeline makes
the wrong number of calls. These pin the two directions of that contract, plus the
claim that it drives the REAL ``run_llm_task`` (telemetry row included) rather than
a stub of it.
"""

import pytest
from pydantic import ValidationError

from app.services.llm_client import run_llm_task
from app.services.llm_model_router import LLMTaskContext
from tests.llm_replay import ReplayClient, ReplayExhausted, install_replay, text_message

pytestmark = pytest.mark.unit

_CTX = LLMTaskContext(task="laser_nest_extraction", input_chars=10)


def _call(**overrides):
    kwargs = dict(
        messages=[{"role": "user", "content": "hello"}],
        system="system text",
        max_tokens=64,
        company_id=1,
        feature="replay_self_test",
        prompt_version="9.9.9",
        timeout=12.0,
    )
    kwargs.update(overrides)
    return run_llm_task(_CTX, **kwargs)


def test_serves_scripted_responses_through_the_real_wrapper(monkeypatch):
    replay = install_replay(monkeypatch, [text_message("first", output_tokens=7)])

    result = _call()

    assert result.text == "first"
    assert result.output_tokens == 7
    # The request the SDK would have sent, as the wrapper laid it out.
    assert len(replay.requests) == 1
    assert replay.requests[0]["system"] == "system text"
    assert replay.requests[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert replay.requests[0]["max_tokens"] == 64
    assert replay.options == [{"timeout": 12.0}]
    # The wrapper's own telemetry write ran, into the recording session.
    assert len(replay.usage_rows) == 1
    row = replay.usage_rows[0]
    assert (row.company_id, row.feature, row.prompt_version, row.success) == (1, "replay_self_test", "9.9.9", True)
    replay.assert_exhausted()


def test_an_extra_call_escapes_a_broad_except(monkeypatch):
    replay = install_replay(monkeypatch, [text_message("only one")])
    _call()

    # The pipelines degrade inside `except Exception`; the overrun must not be
    # swallowed there, or a pipeline making a surprise third call could still pass.
    with pytest.raises(ReplayExhausted):
        try:
            _call()
        except Exception:  # noqa: BLE001 - this is the swallow the harness must survive
            pytest.fail("ReplayExhausted was caught by `except Exception`")

    with pytest.raises(AssertionError, match="ran past the end"):
        replay.assert_exhausted()


def test_an_unrequested_response_fails_the_exhaustion_check(monkeypatch):
    replay = install_replay(monkeypatch, [text_message("pass 1"), text_message("pass 2")])
    _call()

    with pytest.raises(AssertionError, match="never requested"):
        replay.assert_exhausted()


def test_a_malformed_script_is_refused_up_front():
    with pytest.raises(ValidationError):
        ReplayClient([{"content": "not a message"}])
