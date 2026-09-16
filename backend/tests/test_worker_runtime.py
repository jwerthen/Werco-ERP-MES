"""A release heartbeat is published only after a successful Redis write."""

import json
from unittest.mock import AsyncMock

import pytest

from app.core import worker_runtime


@pytest.mark.asyncio
async def test_successful_redis_write_precedes_readiness_event(monkeypatch, caplog):
    redis = AsyncMock()
    monkeypatch.setattr(worker_runtime.settings, 'APP_RELEASE', 'a' * 40)
    monkeypatch.setenv('RAILWAY_DEPLOYMENT_ID', 'synthetic-deployment')
    with caplog.at_level('INFO'):
        identity = await worker_runtime.publish_identity(redis, 'synthetic-instance')
    redis.set.assert_awaited_once_with('werco:worker:runtime:synthetic-instance', json.dumps(identity), ex=90)
    assert identity['release'] == 'a' * 40
    assert identity['deployment_id'] == 'synthetic-deployment'
    assert json.loads(caplog.records[-1].message) == {'event': 'worker_runtime_ready', 'identity': identity}


@pytest.mark.asyncio
async def test_failed_redis_write_cannot_publish_readiness(caplog):
    redis = AsyncMock()
    redis.set.side_effect = ConnectionError('synthetic Redis outage')
    with caplog.at_level('INFO'), pytest.raises(ConnectionError):
        await worker_runtime.publish_identity(redis, 'synthetic-instance')
    assert not any('worker_runtime_ready' in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_startup_failure_does_not_leave_a_refresh_task():
    redis = AsyncMock()
    redis.set.side_effect = ConnectionError('synthetic Redis outage')
    ctx = {'redis': redis}
    with pytest.raises(ConnectionError):
        await worker_runtime.start_runtime_heartbeat(ctx)
    assert 'runtime_heartbeat_task' not in ctx


@pytest.mark.asyncio
async def test_shutdown_cancels_only_its_own_refresh_task():
    ctx = {'redis': AsyncMock()}
    await worker_runtime.start_runtime_heartbeat(ctx)
    task = ctx['runtime_heartbeat_task']
    assert not task.done()
    await worker_runtime.stop_runtime_heartbeat(ctx)
    assert task.cancelled()
    assert 'runtime_heartbeat_task' not in ctx
    await worker_runtime.stop_runtime_heartbeat(ctx)
