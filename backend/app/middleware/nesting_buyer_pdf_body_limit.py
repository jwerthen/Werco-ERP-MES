"""Bound only buyer PDF requests before multipart parsing or file spooling."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.schemas.nesting_buyer_pdf import MAX_REQUEST_BYTES


class NestingBuyerPdfBodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        self.path = settings.API_V1_PREFIX.rstrip('/') + '/quote-nesting/buyer-pdf'

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or scope['method'] != 'POST' or scope['path'].rstrip('/') != self.path:
            await self.app(scope, receive, send)
            return

        async def reject():
            await JSONResponse({"detail": "Buyer PDF upload exceeds the 8 MiB report limit"}, status_code=413)(
                scope, receive, send
            )

        length = Headers(scope=scope).get('content-length')
        try:
            declared = int(length) if length is not None else 0
        except ValueError:
            declared = 0
        if declared > MAX_REQUEST_BYTES:
            await reject()
            return
        content = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(content) + len(chunk) > MAX_REQUEST_BYTES:
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
