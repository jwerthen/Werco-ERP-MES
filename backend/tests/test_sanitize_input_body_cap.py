"""The ``sanitize_input`` request-body size cap (``MAX_SANITIZED_JSON_BODY_BYTES``).

**What is being protected.** ``app/main.py``'s ``sanitize_input`` middleware runs on
every JSON-bodied ``POST``/``PUT``/``PATCH`` — *before* any route's auth dependency —
and feeds the whole body through ``bleach.clean``. bleach is quadratic in adversarial
markup: 192 KB of ``"<a "`` burns ~4.7 CPU-seconds, versus ~0.005 s for the same volume
of benign text. With no cap, an unauthenticated caller could spend a request's worth of
bandwidth to buy seconds of server CPU. The cap closes that.

**Two gates, and both carry weight.**

1. A pre-read check on the declared ``Content-Length``. This is the DoS guard proper:
   it rejects *before* the body is buffered, so an oversized request costs essentially
   nothing. ``test_content_length_gate_rejects_without_ever_reading_the_body`` proves
   the "before" by making ``Request.body()`` explode.
2. A post-read check on ``len(body)``. ``Content-Length`` is absent under chunked
   transfer-encoding and can simply lie, so the bytes that actually arrived are
   re-checked. ``test_chunked_body_without_content_length_is_still_rejected`` proves
   this gate is load-bearing rather than belt-and-suspenders — it establishes first
   that the request really did arrive with no ``Content-Length``.

**Rejecting, not skipping.** Oversized bodies get 413 rather than being waved past the
sanitizer. Skipping would hand an attacker a trivial sanitizer bypass: pad the payload
past the cap and the sanitizer never runs.

**Two deliberate exemptions, both tested here because both are load-bearing.** Inbound
carrier webhooks (``/api/v1/webhooks/carriers/...``) skip the whole middleware — they
HMAC-verify the *exact raw bytes*, so neither rewriting nor rejecting the body is
acceptable. And the cap gates ``application/json`` only: every CSV/XLSX bulk import
arrives as ``multipart/form-data`` through ``UploadFile``, never touches the sanitizer,
and must not be capped at 256 KB.

**No multi-second payloads live here.** The adversarial cost is the *reason* for the
cap, not something to re-measure on every CI run; the boundary tests lower the cap via
``settings`` instead, and the one test that uses the shipped 256 KB default sends
benign text that gate 1 rejects without parsing.
"""

import json
from typing import Iterator

import pytest
import starlette.requests
from fastapi import Request, status
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

pytestmark = pytest.mark.api

ECHO_PATH = "/__test__/sanitize-input-echo"

# Small enough to keep payloads tiny, large enough to hold a realistic JSON envelope.
SMALL_CAP = 2048

# Ceiling on how much body the echo route will hand back verbatim.
ECHO_BODY_LIMIT = 16384

# The value shipped in app/core/config.py. Asserted rather than imported so that
# changing the default is a conscious, reviewed edit in two places.
DEFAULT_CAP_BYTES = 262144


@pytest.fixture
def echo_route() -> Iterator[str]:
    """Register a throwaway route that reports the body the app actually received.

    A dedicated route rather than a real endpoint: the cap lives in middleware, so any
    path exercises it, and a real endpoint would drag auth, tenancy and schema
    validation into a test about byte counts. It echoes the *post-middleware* body,
    which is what makes "the cap did not disable sanitization" directly observable —
    ``sanitize_input`` rewrites ``request._body``, so what this route reads is what a
    real handler would parse and persist.

    ``include_in_schema=False`` keeps it out of the OpenAPI document, and the route is
    removed on teardown so it cannot leak into another test in the same xdist worker.
    """

    async def _echo(request: Request) -> dict:
        raw = await request.body()
        return {
            "length": len(raw),
            "content_length_header": request.headers.get("content-length"),
            "transfer_encoding_header": request.headers.get("transfer-encoding"),
            # Only echoed when small, so an oversized-body test cannot accidentally
            # build a multi-megabyte JSON response. Comfortably above every body
            # these tests need to inspect.
            "body": raw.decode("utf-8", "replace") if len(raw) <= ECHO_BODY_LIMIT else None,
        }

    app.add_api_route(ECHO_PATH, _echo, methods=["POST"], include_in_schema=False)
    route = app.router.routes[-1]
    try:
        yield ECHO_PATH
    finally:
        app.router.routes.remove(route)


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    """Lower the cap so boundary tests need kilobytes, not hundreds of them."""
    monkeypatch.setattr(settings, "MAX_SANITIZED_JSON_BODY_BYTES", SMALL_CAP)
    return SMALL_CAP


_ENVELOPE_BYTES = len(b'{"notes":"' + b'"}')  # 12


def json_body_of_exact_size(size: int) -> bytes:
    """Build a syntactically valid JSON object body of exactly ``size`` bytes."""
    assert size >= _ENVELOPE_BYTES, f"cannot build a JSON body smaller than {_ENVELOPE_BYTES} bytes"
    return b'{"notes":"' + b"a" * (size - _ENVELOPE_BYTES) + b'"}'


def filler_length_for(size: int) -> int:
    """How many filler characters ``json_body_of_exact_size(size)`` carries.

    Accepted bodies are asserted on their decoded *content*, not their byte length,
    because ``sanitize_input`` re-serializes the body (see
    ``test_middleware_rewrites_the_body_and_leaves_content_length_stale``) — the bytes
    a handler receives are not the bytes the client sent.
    """
    return size - _ENVELOPE_BYTES


def received_notes(response) -> str:
    """Decode the ``notes`` value out of the echo route's view of the body."""
    body = response.json()["body"]
    assert body is not None, "echo route withheld the body; it was larger than the echo threshold"
    return json.loads(body)["notes"]


def post_json(client: TestClient, path: str, body: bytes) -> "object":
    return client.post(path, content=body, headers={"content-type": "application/json"})


def chunked(data: bytes, chunk_size: int = 512) -> Iterator[bytes]:
    """Yield ``data`` in pieces.

    Handing httpx an iterator (rather than ``bytes``) is what makes it send
    ``Transfer-Encoding: chunked`` with no ``Content-Length`` — which is precisely the
    request shape gate 1 cannot see.
    """
    for offset in range(0, len(data), chunk_size):
        yield data[offset : offset + chunk_size]


# ---------------------------------------------------------------------------
# The cap must not break, or disable, ordinary sanitized traffic.
# ---------------------------------------------------------------------------


def test_small_json_post_is_still_sanitized(client: TestClient, echo_route: str, small_cap: int):
    """An under-cap body sails through AND still gets sanitized.

    The second half is the point: a size cap that quietly turned the sanitizer off for
    everything would pass a "small requests still work" test while removing the
    protection entirely. The handler sees the sanitizer's output because
    ``sanitize_input`` rewrote ``request._body``.
    """
    response = client.post(
        echo_route,
        json={"notes": "<b>bold</b> note Runout<TIR", "other": "<script>alert(1)</script>ok"},
    )

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["body"] is not None
    received = json.loads(payload["body"])
    assert received == {
        # bleach strips the tag, keeps the text, and escapes the unterminated "<".
        "notes": "bold note Runout&lt;TIR",
        "other": "alert(1)ok",
    }


def test_body_exactly_at_the_cap_is_accepted(client: TestClient, echo_route: str, small_cap: int):
    """The comparison is strictly greater-than: a body of exactly ``cap`` bytes passes."""
    body = json_body_of_exact_size(small_cap)
    assert len(body) == small_cap

    response = post_json(client, echo_route, body)

    assert response.status_code == status.HTTP_200_OK
    assert received_notes(response) == "a" * filler_length_for(small_cap)


def test_body_one_byte_under_the_cap_is_accepted(client: TestClient, echo_route: str, small_cap: int):
    response = post_json(client, echo_route, json_body_of_exact_size(small_cap - 1))

    assert response.status_code == status.HTTP_200_OK
    assert received_notes(response) == "a" * filler_length_for(small_cap - 1)


def test_middleware_rewrites_the_body_and_leaves_content_length_stale(
    client: TestClient, echo_route: str, small_cap: int
):
    """Characterization of a pre-existing wart the cap sits next to.

    ``sanitize_input`` replaces ``request._body`` with ``json.dumps(sanitized)``, which
    re-serializes with ``json``'s default ``": "`` / ``", "`` separators. So a compact
    body sent as ``{"notes":"x"}`` reaches the handler as ``{"notes": "x"}`` — one byte
    longer — while the ``Content-Length`` header still reports the *original* size. The
    header is now wrong by construction.

    Nothing is broken today: Starlette's ``Request.body()`` returns the cached
    ``_body`` and ignores the header, so handlers see the full rewritten payload. It is
    pinned because it is a live trap for anything downstream that trusts
    ``Content-Length`` over the bytes it was handed (a streaming reader, a proxy, a
    future middleware), and because the two size gates are the closest code to it —
    note that both gates measure the *inbound* bytes, correctly, before any rewrite.
    """
    sent = b'{"notes":"clean"}'
    response = post_json(client, echo_route, sent)

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["body"] == '{"notes": "clean"}'  # note the injected space
    assert payload["length"] == len(sent) + 1
    # ...while the header the client sent is unchanged, and now understates the body.
    assert payload["content_length_header"] == str(len(sent))
    assert int(payload["content_length_header"]) < payload["length"]


# ---------------------------------------------------------------------------
# Gate 1: the Content-Length pre-read check (the DoS guard).
# ---------------------------------------------------------------------------


def test_body_one_byte_over_the_cap_is_rejected_with_413(client: TestClient, echo_route: str, small_cap: int):
    """Exact boundary on the reject side, plus the shape of the error."""
    oversized = small_cap + 1
    response = post_json(client, echo_route, json_body_of_exact_size(oversized))

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    detail = response.json()["detail"]
    # A useful detail names both numbers, so a caller can tell how far over it is
    # without guessing the server's configuration.
    assert str(oversized) in detail, detail
    assert str(small_cap) in detail, detail
    assert "too large" in detail.lower(), detail


def test_content_length_gate_rejects_without_ever_reading_the_body(
    client: TestClient, echo_route: str, small_cap: int, monkeypatch: pytest.MonkeyPatch
):
    """The DoS claim itself: gate 1 short-circuits *before* the body is buffered.

    Proven by poisoning ``Request.body()`` so that reading the body at all raises.
    ``sanitize_input`` is the first thing in the stack that would call it (only
    ``add_security_headers`` sits outside, and it never touches the body), so:

    * with gate 1 present -> 413, and the poison is never tripped;
    * with gate 1 removed -> ``body()`` raises inside the middleware's broad
      ``except Exception``, which logs and continues, and the request then reaches the
      echo route, which calls ``body()`` and blows up with a 500.

    So this test fails loudly if the pre-read check is ever demoted to a post-read one.
    """

    async def _explode(self):
        raise AssertionError("request body was buffered despite an oversized Content-Length")

    monkeypatch.setattr(starlette.requests.Request, "body", _explode)

    response = post_json(client, echo_route, json_body_of_exact_size(small_cap * 2))

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE


def test_oversized_body_never_reaches_the_sanitizer(
    client: TestClient, echo_route: str, small_cap: int, monkeypatch: pytest.MonkeyPatch
):
    """``sanitize_dict`` — and therefore bleach — is not called for a rejected body.

    ``sanitize_input`` imports ``sanitize_dict`` from the module inside the request, so
    patching the module attribute intercepts the real call site.
    """
    calls: list[dict] = []

    def _spy(data, keys=None):
        calls.append(data)
        return data

    import app.core.sanitization as sanitization

    monkeypatch.setattr(sanitization, "sanitize_dict", _spy)

    rejected = post_json(client, echo_route, json_body_of_exact_size(small_cap + 1))
    assert rejected.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert calls == [], "the sanitizer ran on a body the cap was supposed to reject"

    # Control: the same spy DOES fire for an under-cap body, so the empty list above
    # is evidence about the cap rather than about a patch that never took effect.
    accepted = post_json(client, echo_route, json_body_of_exact_size(small_cap - 1))
    assert accepted.status_code == status.HTTP_200_OK
    assert len(calls) == 1


def test_413_response_carries_cors_headers_for_an_allowed_origin(client: TestClient, echo_route: str, small_cap: int):
    """A browser must see the 413, not an opaque network error.

    ``sanitize_input`` is registered after ``CORSMiddleware``, which makes it the
    *outer* layer, so a response short-circuited here never passes back through CORS.
    The middleware therefore stamps the headers by hand; this pins that it actually
    does.
    """
    origin = "http://localhost:3000"
    assert origin in settings.cors_origins_list, "test assumes this origin is allowed in the default config"

    response = client.post(
        echo_route,
        content=json_body_of_exact_size(small_cap + 1),
        headers={
            "content-type": "application/json",
            "origin": origin,
            "x-requested-with": "XMLHttpRequest",
        },
    )

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert response.headers.get("access-control-allow-origin") == origin


# ---------------------------------------------------------------------------
# Gate 2: the post-read len(body) check.
# ---------------------------------------------------------------------------


def test_chunked_body_without_content_length_is_still_rejected(client: TestClient, echo_route: str, small_cap: int):
    """Gate 2 carries real weight: chunked requests declare no ``Content-Length``.

    The first request establishes the premise rather than assuming it — a small chunked
    body confirms this transport genuinely omits ``Content-Length`` and sets
    ``Transfer-Encoding: chunked``. Without that check, the oversized assertion below
    could be passing via gate 1 and nobody would know.
    """
    probe = client.post(echo_route, content=chunked(b'{"notes":"hi"}'), headers={"content-type": "application/json"})
    assert probe.status_code == status.HTTP_200_OK
    assert probe.json()["content_length_header"] is None, "premise broken: the transport sent a Content-Length"
    assert probe.json()["transfer_encoding_header"] == "chunked"

    oversized = json_body_of_exact_size(small_cap + 1)
    response = client.post(echo_route, content=chunked(oversized), headers={"content-type": "application/json"})

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert str(small_cap + 1) in response.json()["detail"]


# ---------------------------------------------------------------------------
# The two exemptions.
# ---------------------------------------------------------------------------


def test_carrier_webhook_path_is_exempt_from_the_cap(client: TestClient, small_cap: int):
    """Carrier webhooks must not be capped — they HMAC-verify the raw bytes.

    With no carrier account configured the handler drops the delivery with 204. The
    assertion that matters is simply that the middleware did not turn it into a 413:
    a capped webhook path would reject legitimate carrier traffic that the app cannot
    re-request, and 413 is not something a carrier's retry logic recovers from.
    """
    oversized = json_body_of_exact_size(small_cap * 4)

    response = client.post(
        "/api/v1/webhooks/carriers/easypost",
        content=oversized,
        headers={"content-type": "application/json"},
    )

    assert response.status_code != status.HTTP_413_CONTENT_TOO_LARGE
    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_multipart_upload_over_the_cap_is_not_rejected(client: TestClient, echo_route: str, small_cap: int):
    """The cap gates JSON only; ``multipart/form-data`` is untouched.

    Every CSV/XLSX bulk import in the app arrives this way through ``UploadFile``, and
    those files are routinely far larger than the JSON cap. The echo route confirms the
    full body arrived intact rather than merely that no 413 came back.
    """
    payload = b"part_number,qty\n" + b"PN-0001,5\n" * 2000
    assert len(payload) > small_cap

    response = client.post(echo_route, files={"file": ("import.csv", payload, "text/csv")})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["length"] > small_cap
    assert body["length"] >= len(payload)


def test_get_request_with_a_large_json_body_is_not_capped(client: TestClient, echo_route: str, small_cap: int):
    """Only POST/PUT/PATCH are gated, matching where the sanitizer runs.

    A GET is never sanitized, so capping it would add a refusal with no protection
    behind it. There is no GET handler on the echo route, so 405 is the tell that the
    request reached routing rather than being short-circuited at 413.
    """
    response = client.request(
        "GET",
        echo_route,
        content=json_body_of_exact_size(small_cap * 2),
        headers={"content-type": "application/json"},
    )

    assert response.status_code != status.HTTP_413_CONTENT_TOO_LARGE
    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_non_json_content_type_is_not_capped(client: TestClient, echo_route: str, small_cap: int):
    """``text/plain`` over the cap passes: the sanitizer only parses JSON."""
    response = client.post(
        echo_route,
        content=b"x" * (small_cap * 2),
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["length"] == small_cap * 2


# ---------------------------------------------------------------------------
# Two pre-existing ways the sanitizer is skipped, characterized here.
#
# NEITHER is introduced by the size cap, and neither is fixed here. They are
# pinned in this file because the cap's own design note says rejecting beats
# skipping ("a skip would let an attacker bypass the sanitizer entirely just by
# padding the payload past the cap") — and these are two places where the
# surrounding middleware skips anyway. Anyone reasoning about the sanitizer's
# coverage needs both on the record.
# ---------------------------------------------------------------------------


def test_top_level_json_array_body_is_not_sanitized(client: TestClient, echo_route: str, small_cap: int):
    """A JSON body whose top level is an ARRAY bypasses sanitization entirely.

    ``sanitize_input`` only rewrites when ``json.loads(body)`` yields a ``dict``
    (``isinstance(data, dict)``), so ``[{"notes": "<script>..."}]`` reaches the handler
    verbatim. Compounding it, ``sanitize_dict`` does not recurse into dicts nested in
    lists either (see
    ``tests/test_sanitization_golden_corpus.py::test_sanitize_dict_does_not_recurse_into_dicts_inside_lists``),
    so list-of-object payloads are unsanitized whether the list is at the top level or
    one key down.

    Pinned, not fixed: closing it changes what gets persisted on every list-bodied
    endpoint and deserves its own change and review.
    """
    body = b'[{"notes":"<script>alert(1)</script>"}]'
    response = post_json(client, echo_route, body)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["body"] == body.decode(), "top-level arrays are now sanitized — update this characterization"


def test_a_raising_sanitizer_fails_OPEN_and_the_body_is_left_unsanitized(
    client: TestClient, echo_route: str, small_cap: int, monkeypatch: pytest.MonkeyPatch
):
    """If ``sanitize_dict`` raises, the middleware logs a warning and passes the RAW body on.

    The ``except Exception`` around the sanitization block is broad and does not
    re-raise, so a sanitizer failure degrades to no sanitization rather than to a
    refusal. Worth having on the record next to the size gates, which fail *closed* —
    note the implementation comment marking that the size-gate ``return`` sits inside
    that same ``try`` and is deliberately not swallowed by it.

    Characterized as-is. If this is ever changed to fail closed, THIS test is the one
    that should fail.
    """
    import app.core.sanitization as sanitization

    def _raise(data, keys=None):
        raise RuntimeError("sanitizer exploded")

    monkeypatch.setattr(sanitization, "sanitize_dict", _raise)

    body = b'{"notes":"<script>alert(1)</script>"}'
    response = post_json(client, echo_route, body)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["body"] == body.decode()


def test_the_size_gate_still_fails_CLOSED_when_the_sanitizer_would_raise(
    client: TestClient, echo_route: str, small_cap: int, monkeypatch: pytest.MonkeyPatch
):
    """The counterpart to the test above, and the reason it is worth writing.

    The size-gate ``return`` lives inside the same ``try`` as the fail-open handler. A
    ``return`` is not caught by ``except Exception`` — only a ``raise`` is — so the
    rejection really rejects even when the sanitizer downstream is broken. This pins
    that the two behaviors do not interfere.
    """
    import app.core.sanitization as sanitization

    def _raise(data, keys=None):
        raise RuntimeError("sanitizer exploded")

    monkeypatch.setattr(sanitization, "sanitize_dict", _raise)

    response = post_json(client, echo_route, json_body_of_exact_size(small_cap + 1))

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE


# ---------------------------------------------------------------------------
# The setting itself.
# ---------------------------------------------------------------------------


def test_default_cap_is_256_kb():
    """The shipped default, pinned.

    Sized against the largest realistic bodies measured (laser-nest import at 170 nests
    = 183 KB; BOM create at 1000 line items = 201 KB). Raising it raises the worst-case
    CPU per unauthenticated request quadratically, so a change here wants a reason.
    """
    assert settings.MAX_SANITIZED_JSON_BODY_BYTES == DEFAULT_CAP_BYTES
    assert DEFAULT_CAP_BYTES == 256 * 1024


def test_cap_boundary_moves_with_the_setting(client: TestClient, echo_route: str, monkeypatch: pytest.MonkeyPatch):
    """The limit is read from ``settings`` per request, not baked in at import.

    The same 4 KB body is accepted under an 8 KB cap and rejected under a 1 KB one, and
    the 413 detail reports whichever limit is in force — which is what makes the env
    override in ``ENVIRONMENT_VARIABLES`` real rather than decorative.
    """
    body = json_body_of_exact_size(4096)

    monkeypatch.setattr(settings, "MAX_SANITIZED_JSON_BODY_BYTES", 8192)
    generous = post_json(client, echo_route, body)
    assert generous.status_code == status.HTTP_200_OK
    assert received_notes(generous) == "a" * filler_length_for(4096)

    monkeypatch.setattr(settings, "MAX_SANITIZED_JSON_BODY_BYTES", 1024)
    stingy = post_json(client, echo_route, body)
    assert stingy.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert "1024-byte limit" in stingy.json()["detail"], stingy.json()["detail"]


def test_shipped_default_rejects_a_body_above_256_kb(client: TestClient, echo_route: str):
    """One end-to-end check at the real default, with no monkeypatching.

    Deliberately benign filler: gate 1 rejects on the ``Content-Length`` header without
    parsing a byte, so this costs a memcpy rather than the ~4.7 CPU-seconds an
    adversarial payload of this size would burn inside bleach. Reproducing that cost is
    the reason the cap exists, not something worth paying for on every CI run.
    """
    response = post_json(client, echo_route, json_body_of_exact_size(DEFAULT_CAP_BYTES + 1))

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert str(DEFAULT_CAP_BYTES) in response.json()["detail"]
