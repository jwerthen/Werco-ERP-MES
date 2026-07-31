"""The JSON request-body size cap (``MAX_JSON_BODY_BYTES``) in ``limit_json_body_size``.

**What is being protected.** ``app/main.py``'s ``limit_json_body_size`` middleware
runs on every JSON-bodied ``POST``/``PUT``/``PATCH`` — *before* any route's auth
dependency — and buffers the body so the handler can parse it. Without a cap, an
unauthenticated caller decides how many bytes the app holds in memory and hands to
``json.loads``, and how large the resulting Python object graph gets. General
request-size hygiene, applied at the one place that sees every JSON body.

**Two gates, and both carry weight.**

1. A pre-read check on the declared ``Content-Length``. This is the guard proper:
   it rejects *before* the body is buffered, so an oversized request costs essentially
   nothing. ``test_content_length_gate_rejects_without_ever_reading_the_body`` proves
   the "before" by making ``Request.body()`` explode.
2. A post-read check on ``len(body)``. ``Content-Length`` is absent under chunked
   transfer-encoding and can simply lie, so the bytes that actually arrived are
   re-checked. ``test_chunked_body_without_content_length_is_still_rejected`` proves
   this gate is load-bearing rather than belt-and-suspenders — it establishes first
   that the request really did arrive with no ``Content-Length``.

**This middleware no longer mutates the body.** It used to also rewrite
``request._body`` with a bleach-stripped copy, which mutated *persisted* records —
ASME Y14.5 notation such as ``"Dim is 2.500 <REF> per print"`` was stored as
``"Dim is 2.500  per print"``. That was removed;
``test_body_reaches_the_handler_byte_for_byte`` and
``test_asme_drawing_notation_survives_the_middleware`` are the regression pins, and
``tests/test_frontend_no_raw_html_render_guard.py`` carries the safety argument for
why removing it was sound.

**Two deliberate exemptions, both tested here because both are load-bearing.** Inbound
carrier webhooks (``/api/v1/webhooks/carriers/...``) skip the whole middleware — they
HMAC-verify the *exact raw bytes*, and a carrier's retry logic does not recover from a
413. And the cap gates ``application/json`` only: every CSV/XLSX bulk import arrives as
``multipart/form-data`` through ``UploadFile`` and must not be capped at 256 KB.
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

ECHO_PATH = "/__test__/json-body-cap-echo"

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
    which is what makes the "the middleware does not mutate bodies" assertions
    directly observable — what this route reads is what a real handler would parse
    and persist.

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
    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", SMALL_CAP)
    return SMALL_CAP


_ENVELOPE_BYTES = len(b'{"notes":"' + b'"}')  # 12


def json_body_of_exact_size(size: int) -> bytes:
    """Build a syntactically valid JSON object body of exactly ``size`` bytes."""
    assert size >= _ENVELOPE_BYTES, f"cannot build a JSON body smaller than {_ENVELOPE_BYTES} bytes"
    return b'{"notes":"' + b"a" * (size - _ENVELOPE_BYTES) + b'"}'


def filler_length_for(size: int) -> int:
    """How many filler characters ``json_body_of_exact_size(size)`` carries."""
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
# The middleware passes bodies through untouched.
#
# These are the regression pins for the removal of ingest-time sanitization. The
# middleware buffers the body (gate 2 needs the real byte count) and Starlette
# caches that read on request._body — so "it read the body" and "it changed the
# body" are separable, and only the second is a defect.
# ---------------------------------------------------------------------------


def test_body_reaches_the_handler_byte_for_byte(client: TestClient, echo_route: str, small_cap: int):
    """The exact bytes sent are the exact bytes the handler parses.

    Previously false: the middleware replaced ``request._body`` with
    ``json.dumps(sanitized)``, which both stripped markup and re-serialized with
    ``json``'s ``": "`` separators — so a compact ``{"notes":"clean"}`` arrived as
    ``{"notes": "clean"}``, one byte longer, while ``Content-Length`` still reported
    the original size. Both the mutation and the stale header are gone.
    """
    sent = b'{"notes":"clean"}'
    response = post_json(client, echo_route, sent)

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["body"] == sent.decode()
    assert payload["length"] == len(sent)
    # ...and the Content-Length the client sent now agrees with what arrived.
    assert payload["content_length_header"] == str(len(sent))


def test_asme_drawing_notation_survives_the_middleware(client: TestClient, echo_route: str, small_cap: int):
    """The records-integrity regression this change exists to fix.

    ``<REF>``, ``<TYP>``, ``<MMC>``, ``<BASIC>`` and ``<MIN>`` are ASME Y14.5 drawing
    notation and appear routinely in inspection notes and part descriptions. The old
    bleach pass treated them as HTML tags and deleted them from the *persisted*
    record: ``"Dim is 2.500 <REF> per print"`` became ``"Dim is 2.500  per print"``.
    In an AS9100D / ISO 9001 system that is a records-integrity defect, not a
    cosmetic one.
    """
    notes = "Dim is 2.500 <REF> per print; OD<ID at OP30; R&D signoff <TYP>"
    response = client.post(echo_route, json={"notes": notes})

    assert response.status_code == status.HTTP_200_OK
    assert received_notes(response) == notes


def test_markup_is_stored_verbatim_not_stripped(client: TestClient, echo_route: str, small_cap: int):
    """Deliberate markup is preserved too — storing it is safe, rendering it is the
    thing that is controlled.

    The value here is exactly what the old sanitizer flattened to ``"alert(1)ok"``. It
    now reaches the handler intact, which is only sound because nothing interprets it:
    the SPA renders no raw HTML and reportlab ``Paragraph`` escapes at render. Those
    two claims are pinned by ``tests/test_frontend_no_raw_html_render_guard.py`` and
    ``tests/test_pdf_text_escaping.py``. If either stops holding, fix the sink — do
    not restore body mutation here.
    """
    payload = {"notes": "<b>bold</b> note Runout<TIR", "other": "<script>alert(1)</script>ok"}
    response = client.post(echo_route, json=payload)

    assert response.status_code == status.HTTP_200_OK
    assert json.loads(response.json()["body"]) == payload


def test_top_level_json_array_body_passes_through(client: TestClient, echo_route: str, small_cap: int):
    """List-bodied endpoints behave identically to object-bodied ones.

    Worth pinning because it used to be an *inconsistency*: the sanitizer only
    rewrote when ``json.loads(body)`` produced a ``dict``, so top-level arrays (and
    dicts nested inside lists — BOM lines, PO lines, routing operations) silently
    escaped it. There is no longer a distinction to get wrong.
    """
    body = b'[{"notes":"<script>alert(1)</script>"}]'
    response = post_json(client, echo_route, body)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["body"] == body.decode()


def test_invalid_json_is_left_to_the_validation_layer(client: TestClient, echo_route: str, small_cap: int):
    """The middleware never parses the body, so malformed JSON is not its problem.

    It reaches the route unchanged and fails (or not) wherever it normally would.
    """
    body = b'{"notes": "unterminated'
    response = post_json(client, echo_route, body)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["body"] == body.decode()


# ---------------------------------------------------------------------------
# The cap must not break ordinary traffic.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Gate 1: the Content-Length pre-read check.
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
    """Gate 1 short-circuits *before* the body is buffered.

    Proven by poisoning ``Request.body()`` so that reading the body at all raises.
    ``limit_json_body_size`` is the first thing in the stack that would call it (only
    ``add_security_headers`` sits outside, and it never touches the body), so with
    gate 1 present the poison is never tripped. Demote the pre-read check to a
    post-read one and this test fails loudly.
    """

    async def _explode(self):
        raise AssertionError("request body was buffered despite an oversized Content-Length")

    monkeypatch.setattr(starlette.requests.Request, "body", _explode)

    response = post_json(client, echo_route, json_body_of_exact_size(small_cap * 2))

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE


def test_413_response_carries_cors_headers_for_an_allowed_origin(client: TestClient, echo_route: str, small_cap: int):
    """A browser must see the 413, not an opaque network error.

    ``limit_json_body_size`` is registered after ``CORSMiddleware``, which makes it the
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
# The exemptions.
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
    """Only POST/PUT/PATCH are gated.

    There is no GET handler on the echo route, so 405 is the tell that the request
    reached routing rather than being short-circuited at 413.
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
    """``text/plain`` over the cap passes: only JSON bodies are gated."""
    response = client.post(
        echo_route,
        content=b"x" * (small_cap * 2),
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["length"] == small_cap * 2


# ---------------------------------------------------------------------------
# The setting itself.
# ---------------------------------------------------------------------------


def test_default_cap_is_256_kb():
    """The shipped default, pinned.

    Sized against the largest realistic bodies measured (laser-nest import at 170 nests
    = 183 KB; BOM create at 1000 line items = 201 KB), so a change here wants a reason.
    """
    assert settings.MAX_JSON_BODY_BYTES == DEFAULT_CAP_BYTES
    assert DEFAULT_CAP_BYTES == 256 * 1024


def test_cap_boundary_moves_with_the_setting(client: TestClient, echo_route: str, monkeypatch: pytest.MonkeyPatch):
    """The limit is read from ``settings`` per request, not baked in at import.

    The same 4 KB body is accepted under an 8 KB cap and rejected under a 1 KB one, and
    the 413 detail reports whichever limit is in force — which is what makes the env
    override in ``ENVIRONMENT_VARIABLES`` real rather than decorative.
    """
    body = json_body_of_exact_size(4096)

    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 8192)
    generous = post_json(client, echo_route, body)
    assert generous.status_code == status.HTTP_200_OK
    assert received_notes(generous) == "a" * filler_length_for(4096)

    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 1024)
    stingy = post_json(client, echo_route, body)
    assert stingy.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert "1024-byte limit" in stingy.json()["detail"], stingy.json()["detail"]


def test_shipped_default_rejects_a_body_above_256_kb(client: TestClient, echo_route: str):
    """One end-to-end check at the real default, with no monkeypatching."""
    response = post_json(client, echo_route, json_body_of_exact_size(DEFAULT_CAP_BYTES + 1))

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert str(DEFAULT_CAP_BYTES) in response.json()["detail"]


# ---------------------------------------------------------------------------
# The deprecated env-var name.
# ---------------------------------------------------------------------------


def test_deprecated_env_name_is_still_honoured(monkeypatch: pytest.MonkeyPatch):
    """``MAX_SANITIZED_JSON_BODY_BYTES`` keeps working as a fallback.

    The setting was renamed because "SANITIZED" describes a control this middleware no
    longer performs. The old name shipped, so an environment that set it in the
    meantime must not silently revert to the 256 KB default on deploy — a config that
    stops taking effect without an error is the worst outcome of a rename. Resolved
    through ``AliasChoices``, so this exercises the real settings source.
    """
    from app.core.config import Settings

    monkeypatch.delenv("MAX_JSON_BODY_BYTES", raising=False)
    monkeypatch.setenv("MAX_SANITIZED_JSON_BODY_BYTES", "4096")

    assert Settings().MAX_JSON_BODY_BYTES == 4096


def test_new_env_name_wins_when_both_are_set(monkeypatch: pytest.MonkeyPatch):
    """Precedence is unambiguous: the current name beats the deprecated one."""
    from app.core.config import Settings

    monkeypatch.setenv("MAX_JSON_BODY_BYTES", "8192")
    monkeypatch.setenv("MAX_SANITIZED_JSON_BODY_BYTES", "4096")

    assert Settings().MAX_JSON_BODY_BYTES == 8192
