"""Bound only nesting-draft writes before multipart parsing/spooling."""

import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.schemas.quote_nesting_drafts import MAX_DRAFT_REQUEST_BYTES


class NestingDraftBodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        base = re.escape(settings.API_V1_PREFIX.rstrip("/") + "/quote-nesting/drafts")
        self.path = re.compile(base + r"(?:/[^/]+/revisions)?/?$")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or not self.path.fullmatch(scope["path"]):
            await self.app(scope, receive, send)
            return

        async def reject() -> None:
            response = JSONResponse(
                status_code=413, content={"detail": "Nesting draft upload exceeds the 5 MiB estimate limit"}
            )
            await response(scope, receive, send)

        length = Headers(scope=scope).get("content-length")
        try:
            declared_length = int(length) if length is not None else 0
        except ValueError:
            declared_length = 0
        if declared_length > MAX_DRAFT_REQUEST_BYTES:
            await reject()
            return
        content = bytearray()
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_DRAFT_REQUEST_BYTES:
                await reject()
                return
            content.extend(chunk)
            if not message.get("more_body", False):
                break
        body = bytes(content)
        sent = False

        async def bounded_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
