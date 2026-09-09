"""Cap original-CAD request bytes before any parser/spooling, including chunked uploads."""

import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.schemas.quote_nesting_sources import MAX_INTENT_BYTES, MAX_SOURCE_BYTES


class NestingSourceBodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        base = re.escape(settings.API_V1_PREFIX.rstrip('/') + '/quote-nesting/drafts')
        self.path = re.compile(base + r'/[^/]+/revisions/[^/]+/sources(?:/[^/]+/(content|finalize))?/?$')

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        match = self.path.fullmatch(scope.get('path', ''))
        if scope['type'] != 'http' or scope['method'] != 'POST' or match is None:
            await self.app(scope, receive, send)
            return
        limit = MAX_SOURCE_BYTES if match[1] == 'content' else MAX_INTENT_BYTES

        async def reject():
            await JSONResponse({'detail': 'Original source request exceeds its byte limit'}, status_code=413)(
                scope, receive, send
            )

        try:
            length = int(Headers(scope=scope).get('content-length', '0'))
        except ValueError:
            length = 0
        if length > limit:
            await reject()
            return
        content = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(content) + len(chunk) > limit:
                await reject()
                return
            content.extend(chunk)
            if not message.get('more_body', False):
                break
        body = bytes(content)
        sent = False

        async def bounded_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()

        await self.app(scope, bounded_receive, send)
