"""New raw-source receive cap applies before a downstream handler can spool/read."""

import pytest

from app.middleware.nesting_source_body_limit import NestingSourceBodyLimitMiddleware
from app.schemas.quote_nesting_sources import MAX_INTENT_BYTES, MAX_SOURCE_BYTES

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'tail,limit', [('/1/content', MAX_SOURCE_BYTES), ('', MAX_INTENT_BYTES), ('/1/finalize', MAX_INTENT_BYTES)]
)
@pytest.mark.parametrize('declared', [False, True])
async def test_source_limit_refuses_oversize_before_handler(tail, limit, declared):
    called = []
    messages = []

    async def app(scope, receive, send):
        called.append(True)

    async def receive():
        return {'type': 'http.request', 'body': b'x' * (limit + 1), 'more_body': False}

    async def send(message):
        messages.append(message)

    scope = {
        'type': 'http',
        'method': 'POST',
        'path': '/api/v1/quote-nesting/drafts/1/revisions/1/sources' + tail,
        'headers': [(b'content-length', str(limit + 1).encode())] if declared else [],
    }
    await NestingSourceBodyLimitMiddleware(app)(scope, receive, send)
    assert not called
    assert messages[0]['status'] == 413


@pytest.mark.asyncio
async def test_source_chunked_limit_counts_aggregate_before_handler():
    messages = []
    calls = []
    chunks = iter([b'x' * (MAX_SOURCE_BYTES - 1), b'xx'])

    async def app(scope, receive, send):
        calls.append(True)

    async def receive():
        return {'type': 'http.request', 'body': next(chunks), 'more_body': True}

    async def send(message):
        messages.append(message)

    scope = {
        'type': 'http',
        'method': 'POST',
        'headers': [],
        'path': '/api/v1/quote-nesting/drafts/1/revisions/1/sources/1/content',
    }
    await NestingSourceBodyLimitMiddleware(app)(scope, receive, send)
    assert not calls and messages[0]['status'] == 413
