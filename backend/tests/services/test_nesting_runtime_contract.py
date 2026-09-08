"""Readiness must fail closed and must not announce an unpublished runtime."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.jobs import quote_nesting_runs as job
from app.services import nesting_runtime as runtime

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def identity(**changes):
    return {
        **dict(
            release='synthetic-release',
            protocol=1,
            solver_version='werco-contour-v4',
            bundle_sha256='a' * 64,
            node_version='v22.20.0',
            instance_id='12345678-1234-4234-8234-123456789abc',
            deployment_id=None,
            observed_at=datetime.now(timezone.utc).isoformat(),
        ),
        **changes,
    }


@pytest.mark.parametrize(
    'changes,reason',
    [
        ({'release': 'old-release'}, 'release_mismatch'),
        ({'protocol': True}, 'invalid_identity'),
        ({'bundle_sha256': 'bad'}, 'invalid_identity'),
        ({'node_version': 'v20.0.0'}, 'invalid_identity'),
        ({'solver_version': 'unknown-solver'}, 'invalid_identity'),
        ({'deployment_id': ['not-a-string']}, 'invalid_identity'),
        ({'observed_at': (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()}, 'stale'),
        ({'observed_at': (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()}, 'stale'),
    ],
)
async def test_untrusted_stale_or_mismatched_identity_never_enables_start(monkeypatch, changes, reason):
    pool = Mock(get=AsyncMock(return_value=json.dumps(identity(**changes))))
    monkeypatch.setattr(runtime, 'get_redis_pool', AsyncMock(return_value=pool))
    monkeypatch.setattr(runtime, 'current_release', lambda: 'synthetic-release')
    result = await runtime.runtime_status()
    assert result == {'schema_version': 1, 'available': False, 'identity': None, 'reason': reason}


@pytest.mark.parametrize('raw', [None, 'not-json', '{}', 'x' * 4097])
async def test_missing_or_malformed_redis_payload_never_enables_start(monkeypatch, raw):
    monkeypatch.setattr(runtime, 'get_redis_pool', AsyncMock(return_value=Mock(get=AsyncMock(return_value=raw))))
    result = await runtime.runtime_status()
    assert result['available'] is False and result['identity'] is None


async def test_ready_identity_is_returned_only_with_current_release_and_live_redis(monkeypatch):
    expected = identity()
    pool = Mock(get=AsyncMock(return_value=json.dumps(expected)))
    monkeypatch.setattr(runtime, 'get_redis_pool', AsyncMock(return_value=pool))
    monkeypatch.setattr(runtime, 'current_release', lambda: 'synthetic-release')
    assert await runtime.runtime_status() == {
        'schema_version': 1,
        'available': True,
        'reason': 'ready',
        'identity': expected,
    }
    pool.get.side_effect = ConnectionError('Synthetic unavailable Redis')
    assert await runtime.runtime_status() == {
        'schema_version': 1,
        'available': False,
        'reason': 'queue_unavailable',
        'identity': None,
    }


@pytest.mark.parametrize('publish_fails', [False, True])
async def test_ready_log_is_emitted_only_after_successful_redis_publication(monkeypatch, publish_fails):
    log = Mock()

    async def store(*args, **kwargs):
        log.assert_not_called()
        if publish_fails:
            raise ConnectionError('Synthetic unavailable Redis')

    async def stop(_delay):
        raise asyncio.CancelledError

    pool = Mock(set=AsyncMock(side_effect=store))
    monkeypatch.setattr(job, '_log_runtime_identity', log)
    monkeypatch.setattr(job.asyncio, 'sleep', stop)
    monkeypatch.setattr(job, 'runtime_key', lambda: 'synthetic-runtime-key')
    monkeypatch.delenv('RAILWAY_DEPLOYMENT_ID', raising=False)
    supplied = {
        key: value for key, value in identity().items() if key not in ('instance_id', 'observed_at', 'deployment_id')
    }
    with pytest.raises(asyncio.CancelledError):
        await job._publish_runtime({'redis': pool, 'nesting_instance_id': 'synthetic-instance'}, supplied)
    pool.set.assert_awaited_once()
    args, kwargs = pool.set.call_args
    assert args[0] == 'synthetic-runtime-key' and kwargs['ex'] == runtime.RUNTIME_TTL
    if publish_fails:
        log.assert_not_called()
    else:
        log.assert_called_once_with(json.loads(args[1]))


@pytest.mark.parametrize('relay_enabled', [None, False, 'true', True])
async def test_worker_cannot_announce_readiness_without_its_recovery_job_enabled(monkeypatch, relay_enabled):
    verify = AsyncMock(return_value=identity())
    publish = AsyncMock()
    monkeypatch.setattr(job, 'verify_runtime', verify)
    monkeypatch.setattr(job, '_publish_runtime', publish)
    ctx = {'nesting_relay_enabled': relay_enabled}
    await job.startup_nesting_runtime(ctx)
    if relay_enabled is True:
        verify.assert_awaited_once()
        await ctx['nesting_runtime_heartbeat']
        publish.assert_awaited_once()
    else:
        verify.assert_not_called()
        publish.assert_not_called()
        assert 'nesting_runtime_heartbeat' not in ctx
