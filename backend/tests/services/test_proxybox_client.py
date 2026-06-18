"""Unit tests for the ProxyBox print-bridge client.

NO network: HTTP I/O is mocked with ``httpx.MockTransport`` (same pattern as the
EasyPost adapter tests). These pin down the wire contract and secret hygiene:

* submit POSTs to ``/print/{target}`` with the ``X-API-Key`` header and a body of
  ``{content: <base64 pdf>, contentType: "pdf_base64", copies, paperSize}``.
* the returned job id is parsed and polled to a terminal state.
* a terminal-failure job raises ``ProxyBoxError`` whose message NEVER contains the
  API key.
* an HTTP error response raises ``ProxyBoxError`` without leaking the key.
* a no-job-id (fire-and-forget) submit is treated as accepted (no polling).
"""

import asyncio
import base64
import json

import httpx
import pytest

from app.services.proxybox_client import (
    API_KEY_HEADER,
    CONTENT_TYPE_PDF_BASE64,
    ProxyBoxClient,
    ProxyBoxError,
)

pytestmark = pytest.mark.unit

_API_KEY = "PBX_SUPER_SECRET_KEY_9999"
_BASE = "https://pbx-test.pbxz.cloud/api/v1"
_TARGET = "usb_sn_DYM0514872"


def _client_with(handler) -> ProxyBoxClient:
    """A ProxyBoxClient whose AsyncClient routes through ``handler`` (no network)."""
    client = ProxyBoxClient(_BASE, _API_KEY, _TARGET)
    transport = httpx.MockTransport(handler)
    client._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        base_url=_BASE, headers={API_KEY_HEADER: _API_KEY}, transport=transport
    )
    return client


def test_submit_and_poll_to_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/print/" in request.url.path:
            captured["submit_path"] = request.url.path
            captured["api_key"] = request.headers.get(API_KEY_HEADER)
            body = json.loads(request.content)
            captured["body"] = body
            return httpx.Response(200, json={"jobId": "job-123", "status": "queued"})
        if "/jobs/job-123" in request.url.path:
            return httpx.Response(200, json={"id": "job-123", "status": "done"})
        return httpx.Response(404, json={"error": "unexpected"})

    client = _client_with(handler)
    result = asyncio.run(
        client.print_and_wait(b"%PDF-fake", copies=2, paper_size="4x6", poll_interval=0.01, max_wait=2)
    )

    assert captured["submit_path"].endswith(f"/print/{_TARGET}")
    assert captured["api_key"] == _API_KEY
    body = captured["body"]
    assert sorted(body.keys()) == ["content", "contentType", "copies", "paperSize"]
    assert body["contentType"] == CONTENT_TYPE_PDF_BASE64
    assert body["copies"] == 2
    assert body["paperSize"] == "4x6"
    assert base64.b64decode(body["content"]) == b"%PDF-fake"
    assert result["succeeded"] is True
    assert result["terminal"] is True


def test_terminal_failure_raises_without_leaking_key():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/print/" in request.url.path:
            return httpx.Response(200, json={"jobId": "jX", "status": "queued"})
        return httpx.Response(200, json={"id": "jX", "status": "failed"})

    client = _client_with(handler)
    with pytest.raises(ProxyBoxError) as exc:
        asyncio.run(client.print_and_wait(b"%PDF-fake", poll_interval=0.01, max_wait=2))
    assert _API_KEY not in str(exc.value)


def test_http_error_raises_without_leaking_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client_with(handler)
    with pytest.raises(ProxyBoxError) as exc:
        asyncio.run(client.submit_pdf(b"%PDF-fake"))
    assert _API_KEY not in str(exc.value)


def test_fire_and_forget_submit_is_accepted_without_polling():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/print/" in request.url.path:
            return httpx.Response(200, json={})  # no job id
        raise AssertionError("must not poll when there is no job id")

    client = _client_with(handler)
    result = asyncio.run(client.print_and_wait(b"%PDF-fake", poll_interval=0.01, max_wait=2))
    assert result["succeeded"] is True
    assert result["job_id"] is None


def test_empty_pdf_rejected():
    client = _client_with(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ProxyBoxError):
        asyncio.run(client.submit_pdf(b""))


def test_missing_config_rejected():
    with pytest.raises(ProxyBoxError):
        ProxyBoxClient("", _API_KEY, _TARGET)
    with pytest.raises(ProxyBoxError):
        ProxyBoxClient(_BASE, _API_KEY, "")
    with pytest.raises(ProxyBoxError):
        ProxyBoxClient(_BASE, "", _TARGET)
