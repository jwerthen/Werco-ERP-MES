"""Response-side export control for narrowed API tokens (Claude-plugin brief P0.1 / P0.3).

PURE ASGI -- not ``BaseHTTPMiddleware``, whose child task would hide the request's state
-- and registered as the INNERMOST user middleware (``main.py`` adds it first), so it sits
inside ``GZipMiddleware`` (it sees uncompressed bodies) and inside
``CorrelationIdMiddleware`` (the correlation id is already in ``scope["state"]``). It
covers both doors at once: a direct ``/api/v1`` call and an MCP tool call, which the door
dispatches in-process through this same stack.

The decision is ``get_current_user``'s, not this module's. For a narrowed API token the
dependency writes an :class:`~app.core.export_control.ExportControlMarker` into
``scope["state"]``; this middleware reads it when the response starts:

* **no marker** (a person, a full token, a kiosk badge, an unauthenticated request) --
  the response passes through untouched, unbuffered: zero cost for the SPA.
* **a refusal marker** (gate 1 / gate 4 refused an AI-external token, or the MCP door
  refused it for AI egress) -- one ``ai_external_refused`` ledger row is written before
  the 403 is sent. A refusal-marked response that is NOT an error (something caught the
  gate's 403 and answered 2xx) is never forwarded: 503.
* **read-only, any** -- a response that is not JSON (a PDF, a CSV, a drawing, a redirect
  to file storage) is replaced by 403 ``"Read-only API token cannot download files."``
  (for an AI-external token, recorded in the ledger). JSON passes.
* **AI-external + JSON** -- the body is buffered; a 2xx has the company's export-control
  index built and the body redacted in a worker thread, the ledger rows COMMITTED, and is
  re-serialized with a corrected ``Content-Length`` and no ``ETag`` -- only then is
  anything sent. A by-number lookup that found a withheld record answers the same 404 a
  number nobody carries gets. A route's own error answers a fixed text per status code
  (no echo of the request: scrubbing an echo would confirm a probe), with fresh headers.

Any failure on a marked response -- the index, the redaction, the ledger write, an
undecodable or encoded body -- answers 503 ``"Export-control check failed; nothing was
sent."`` and not one byte of the original body: fail closed.

Independently, a request carrying a NARROWED API-token bearer (read-only or AI-external)
has its conditional headers (``If-None-Match`` and friends) stripped before routing (a
cheap unverified look at the JWT's ``type`` / ``api_scope`` / ``ai_ext`` claims; no
database): a 304 must never let a cached unredacted body outlive a customer becoming
controlled. A full token keeps them (code review P0, finding 7). Looking only at claims is
safe: a narrowed token whose claims were removed fails ``check_api_token`` (the claim must
equal the row), and a full token that claims to be narrowed only loses its 304s.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Mapping, Optional

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.export_control import (
    EXPORT_CONTROL_FAILED_DETAIL,
    EXPORT_CONTROL_STATE_KEY,
    READ_ONLY_FILE_REFUSED_DETAIL,
    REASON_NON_JSON_RESPONSE,
    ExportControlMarker,
    relative_route_path,
)
from app.core.media_types import is_json_media_type
from app.services import export_control_service

logger = logging.getLogger(__name__)

# Request headers that could earn a 304 (or a precondition answer) from a cached body.
CONDITIONAL_REQUEST_HEADERS = frozenset(
    {b"if-none-match", b"if-match", b"if-modified-since", b"if-unmodified-since", b"if-range"}
)
# Response statuses that carry no body by definition.
_BODYLESS_STATUSES = frozenset({204, 304})


def _unverified_bearer_payload(headers: List[Any]) -> Optional[Dict[str, Any]]:
    """The UNVERIFIED JWT payload of the ``Authorization: Bearer`` header, or None."""
    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        try:
            scheme, _, token = value.decode("latin-1").partition(" ")
        except Exception:  # noqa: BLE001 - a malformed header is simply not an API token
            return None
        if scheme.lower() != "bearer" or token.count(".") != 2:
            return None
        segment = token.split(".")[1]
        try:
            payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        except Exception:  # noqa: BLE001
            return None
        return payload if isinstance(payload, dict) else None
    return None


def bearer_is_api_token(headers: List[Any]) -> bool:
    """Does the ``Authorization`` header carry a JWT whose UNVERIFIED payload says ``type == "api"``?

    Metadata only. Authentication is ``get_current_user``'s.
    """
    payload = _unverified_bearer_payload(headers)
    return payload is not None and payload.get("type") == "api"


def bearer_is_narrowed_api_token(headers: List[Any]) -> bool:
    """An API-token bearer whose UNVERIFIED claims say read-only (``api_scope``) or AI-external (``ai_ext``).

    Metadata only: it only ever causes conditional headers to be dropped. A full token's JWT
    carries neither claim (migration 119), so it keeps its 304s.
    """
    payload = _unverified_bearer_payload(headers)
    if payload is None or payload.get("type") != "api":
        return False
    scope = payload.get("api_scope")
    return payload.get("ai_ext") is True or (scope is not None and scope != "full")


def _json_response(status: int, detail: str) -> List[Message]:
    body = json.dumps({"detail": detail}).encode("utf-8")
    return [
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        },
        {"type": "http.response.body", "body": body, "more_body": False},
    ]


def _header(message: Message, name: bytes) -> Optional[str]:
    for key, value in message.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _client_ip(scope: Scope) -> Optional[str]:
    client = scope.get("client")
    return client[0] if client else None


class ExportControlMiddleware:
    """See the module docstring."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: Dict[str, Any] = scope.setdefault("state", {})
        headers = scope.get("headers") or []
        if bearer_is_narrowed_api_token(headers):
            scope["headers"] = [(k, v) for k, v in headers if k.lower() not in CONDITIONAL_REQUEST_HEADERS]

        responder = _Responder(scope, state, send)
        await self.app(scope, receive, responder.send)


class _Responder:
    """One request's ``send`` wrapper (a small state machine)."""

    PASS = "pass"
    BUFFER = "buffer"
    DROP = "drop"

    def __init__(self, scope: Scope, state: Mapping[str, Any], send: Send) -> None:
        self.scope = scope
        self.state = state
        self._send = send
        self.mode: Optional[str] = None
        self.start: Optional[Message] = None
        self.marker: Optional[ExportControlMarker] = None
        self.chunks: List[bytes] = []

    # -- helpers

    @property
    def route_template(self) -> Optional[str]:
        route = self.scope.get("route")
        return getattr(route, "path_format", None) or getattr(route, "path", None)

    def _context(self) -> Dict[str, Any]:
        return {
            "request_id": self.state.get("correlation_id"),
            "source_ip": _client_ip(self.scope),
            "route": (self.marker.route if self.marker and self.marker.route else None)
            or self.route_template
            or self.scope.get("path"),
        }

    async def _emit(self, messages: List[Message]) -> None:
        for message in messages:
            await self._send(message)

    async def _fail_closed(self, reason: str) -> None:
        logger.error("Export-control check failed (%s) for %s; answering 503", reason, self.scope.get("path"))
        await self._emit(_json_response(503, EXPORT_CONTROL_FAILED_DETAIL))

    # -- the wrapper

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            await self._on_start(message)
            return
        if self.mode is None:
            # A message before the response starts -- Starlette's ``http.response.debug``,
            # which a template response sends when the server offers that extension. With no
            # marker the request is none of this layer's business: forward it untouched (it
            # used to fall through to the buffering branch and turn a person's response into a
            # 503). With a marker nothing unchecked may pass: it is dropped, and the response
            # that follows is checked as usual.
            if isinstance(self.state.get(EXPORT_CONTROL_STATE_KEY), ExportControlMarker):
                logger.warning("Dropped %s sent before the response started (export control)", message["type"])
                return
            await self._send(message)
            return
        if self.mode == self.PASS:
            await self._send(message)
            return
        if self.mode == self.DROP:
            # The replacement response was already sent: discard EVERYTHING the route still
            # sends -- body chunks, and a ``http.response.pathsend`` file too.
            return
        # BUFFER
        if message["type"] != "http.response.body":
            # A file handed to the server (pathsend) or trailers on a body this layer must
            # read: it cannot be checked, so nothing of it is sent.
            self.mode = self.DROP
            await self._fail_closed(f"unbufferable {message['type']}")
            return
        self.chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            await self._finish()

    async def _on_start(self, message: Message) -> None:
        marker = self.state.get(EXPORT_CONTROL_STATE_KEY)
        if not isinstance(marker, ExportControlMarker):
            self.mode = self.PASS
            await self._send(message)
            return
        self.marker = marker
        status = int(message.get("status", 200))

        if marker.refusal_reason is not None:
            # A gate already refused this request (a fixed-text 403): record it, then send it.
            try:
                await run_in_threadpool(
                    export_control_service.record_refusal, marker, marker.refusal_reason, **self._context()
                )
            except Exception:  # noqa: BLE001 - fail closed: never answer without the ledger row
                logger.exception("Could not record an AI-external refusal")
                self.mode = self.DROP
                await self._fail_closed("refusal ledger write")
                return
            if status < 400:
                # Something caught the gate's 403 and answered anyway: that body was never
                # checked, so none of it goes out (code review P0, finding 5).
                self.mode = self.DROP
                await self._fail_closed(f"refused request answered {status}")
                return
            self.mode = self.PASS
            await self._send(message)
            return

        if status in _BODYLESS_STATUSES or status < 200:
            self.mode = self.PASS
            await self._send(message)
            return

        if not is_json_media_type(_header(message, b"content-type")):
            # File bytes (or a redirect to them) never reach a read-only token.
            self.mode = self.DROP
            if marker.ai_external:
                try:
                    await run_in_threadpool(
                        export_control_service.record_refusal, marker, REASON_NON_JSON_RESPONSE, **self._context()
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Could not record an AI-external non-JSON refusal")
                    await self._fail_closed("non-JSON ledger write")
                    return
            await self._emit(_json_response(403, READ_ONLY_FILE_REFUSED_DETAIL))
            return

        if not marker.ai_external:
            self.mode = self.PASS  # a read-only (not AI-external) token reads JSON as-is
            await self._send(message)
            return

        self.mode = self.BUFFER
        self.start = message

    async def _finish(self) -> None:
        assert self.start is not None and self.marker is not None  # nosec B101 - state machine invariant
        self.mode = self.DROP  # whatever happens next, the original body is never forwarded
        start = self.start
        body = b"".join(self.chunks)
        self.chunks = []
        encoding = (_header(start, b"content-encoding") or "identity").strip().lower()
        if encoding not in ("", "identity"):
            await self._fail_closed(f"encoded body ({encoding})")
            return

        status = int(start.get("status", 200))
        if body.strip():
            try:
                payload = json.loads(body)
                route_path = relative_route_path(self.route_template, settings.API_V1_PREFIX)
                outcome = await run_in_threadpool(
                    export_control_service.apply_export_control,
                    self.marker,
                    payload,
                    status_code=status,
                    route_path=route_path,
                    path_params=dict(self.scope.get("path_params") or {}),
                    **self._context(),
                )
                body = json.dumps(outcome.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except Exception:  # noqa: BLE001 - fail closed on ANY failure
                logger.exception("AI-external export-control pass failed")
                await self._fail_closed("redaction")
                return
            if outcome.rebuilt:
                # A new response: none of the route's headers (an echoing one included).
                await self._send(
                    {
                        "type": "http.response.start",
                        "status": outcome.status_code,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await self._send({"type": "http.response.body", "body": body, "more_body": False})
                return

        headers = MutableHeaders(raw=list(start.get("headers", [])))
        if "etag" in headers:
            del headers["etag"]
        headers["content-length"] = str(len(body))
        await self._send({**start, "headers": headers.raw})
        await self._send({"type": "http.response.body", "body": body, "more_body": False})
