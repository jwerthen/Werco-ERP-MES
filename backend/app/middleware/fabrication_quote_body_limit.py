"""Bound original fabrication source uploads before multipart spooling."""

import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.core.config import settings


class FabricationQuoteBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app
        self.path = re.compile(
            re.escape(settings.API_V1_PREFIX.rstrip("/") + "/fabrication-quotes/") + r"[0-9]+/files/?$"
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not self.path.fullmatch(scope["path"]):
            return await self.app(scope, receive, send)
        limit = 25 * 1024 * 1024 + 65536

        async def reject():
            await JSONResponse({"detail": "Source uploads are limited to 25 MiB"}, status_code=413)(
                scope, receive, send
            )

        try:
            length = int(Headers(scope=scope).get("content-length", "0"))
        except ValueError:
            length = 0
        if length > limit:
            return await reject()
        content = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(content) + len(chunk) > limit:
                return await reject()
            content.extend(chunk)
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(content), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
