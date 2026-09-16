"""The raw source receive cap applies before any multipart spool is constructed."""

import json

import pytest
from starlette.requests import Request

from app.core.config import settings
from app.middleware.fabrication_quote_body_limit import (
    FabricationQuoteBodyLimitMiddleware,
)

MAXIMUM = 25 * 1024 * 1024 + 65536
PATH = settings.API_V1_PREFIX.rstrip("/") + "/fabrication-quotes/123/files"


def scope(path=PATH, headers=None, method="POST", kind="http"):
    return {
        "type": kind,
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
    }


@pytest.mark.asyncio
async def test_oversize_content_length_rejects_before_receive_or_downstream():
    sent = []

    async def forbidden(*args):
        raise AssertionError("Oversize declared upload reached receive or multipart parser")

    async def send(message):
        sent.append(message)

    await FabricationQuoteBodyLimitMiddleware(forbidden)(
        scope(headers=[(b"content-length", str(MAXIMUM + 1).encode())]), forbidden, send
    )
    assert sent[0]["status"] == 413
    assert "25 MiB" in json.loads(sent[1]["body"])["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [None, b"1", b"garbage", b"-1"])
@pytest.mark.parametrize("suffix", ["", "/"])
async def test_stream_aggregate_rejected_before_spooling_with_absent_or_false_length(declared, suffix):
    sent, consumed = [], []
    chunks = iter([b"x" * MAXIMUM, b"x"])

    async def forbidden(*args):
        raise AssertionError("Overflow reached downstream multipart parser/spool")

    async def receive():
        consumed.append(True)
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    async def send(message):
        sent.append(message)

    headers = [(b"content-length", declared)] if declared is not None else []
    await FabricationQuoteBodyLimitMiddleware(forbidden)(scope(PATH + suffix, headers), receive, send)
    assert sent[0]["status"] == 413
    assert len(consumed) == 2


@pytest.mark.asyncio
async def test_exact_transport_limit_is_replayed_once_without_byte_changes():
    chunks = [b"a" * (MAXIMUM - 3), b"end"]
    sent, received = [], []

    async def receive():
        if not chunks:
            return {"type": "http.disconnect"}
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def downstream(_scope, replay, _send):
        assert not chunks, "Downstream started before the complete size check"
        first = await replay()
        received.append(first)
        assert first["more_body"] is False
        assert len(first["body"]) == MAXIMUM
        assert first["body"].startswith(b"aaa") and first["body"].endswith(b"end")
        assert await replay() == {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await FabricationQuoteBodyLimitMiddleware(downstream)(scope(), receive, send)
    assert len(received) == 1 and not sent


@pytest.mark.asyncio
async def test_actual_multipart_parser_only_runs_after_bounded_body_is_complete(
    monkeypatch,
):
    import starlette.formparsers

    content = b"SYNTHETIC ONLY - not a manufacturing drawing"
    body = (
        b'--synthetic-boundary\r\nContent-Disposition: form-data; name="file"; filename="synthetic.dxf"\r\nContent-Type: application/dxf\r\n\r\n'
        + content
        + b"\r\n--synthetic-boundary--\r\n"
    )
    chunks = [body[:25], body[25:]]
    spool_calls = []
    original_spool = starlette.formparsers.SpooledTemporaryFile

    def observed_spool(*args, **kwargs):
        assert not chunks, "Multipart spooling began before the transport-size check finished"
        spool_calls.append(True)
        return original_spool(*args, **kwargs)

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", observed_spool)

    async def receive():
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def downstream(request_scope, replay, _send):
        request = Request(request_scope, replay)
        async with request.form() as form:
            assert form["file"].filename == "synthetic.dxf"
            assert await form["file"].read() == content

    async def send(_message):
        pass

    await FabricationQuoteBodyLimitMiddleware(downstream)(
        scope(headers=[(b"content-type", b"multipart/form-data; boundary=synthetic-boundary")]),
        receive,
        send,
    )
    assert spool_calls == [True]


@pytest.mark.asyncio
async def test_disconnect_does_not_send_truncated_body_to_parser():
    messages = iter(
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def forbidden(*args):
        raise AssertionError("Disconnected body reached downstream or response")

    async def receive():
        return next(messages)

    await FabricationQuoteBodyLimitMiddleware(forbidden)(scope(), receive, forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_scope",
    [
        scope(PATH, method="GET"),
        scope(PATH + "/original"),
        scope(PATH.replace("/123/", "/not-a-number/")),
        scope("/unrelated"),
        scope(kind="websocket"),
    ],
)
async def test_other_routes_methods_and_protocols_pass_through_without_body_read(
    request_scope,
):
    entered = []

    async def receive():
        raise AssertionError("Unrelated request body was consumed")

    async def send(_message):
        pass

    async def downstream(actual_scope, actual_receive, actual_send):
        entered.append(actual_scope)
        assert actual_receive is receive and actual_send is send

    await FabricationQuoteBodyLimitMiddleware(downstream)(request_scope, receive, send)
    assert entered == [request_scope]
