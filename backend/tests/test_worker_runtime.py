"""A release heartbeat is published only after a successful Redis write."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core import worker_runtime


@pytest.mark.asyncio
async def test_successful_redis_write_precedes_readiness_event(monkeypatch):
    redis = AsyncMock()
    monkeypatch.setattr(worker_runtime.settings, 'APP_RELEASE', 'a' * 40)
    monkeypatch.setenv('RAILWAY_DEPLOYMENT_ID', 'synthetic-deployment')
    receipts = []

    def record(identity):
        assert redis.set.await_count == 1
        receipts.append(identity)

    monkeypatch.setattr(worker_runtime, '_log_runtime_identity', record)
    identity = await worker_runtime.publish_identity(redis, 'synthetic-instance')
    redis.set.assert_awaited_once_with('werco:worker:runtime:synthetic-instance', json.dumps(identity), ex=90)
    assert identity['release'] == 'a' * 40
    assert identity['deployment_id'] == 'synthetic-deployment'
    assert receipts == [identity]


@pytest.mark.asyncio
async def test_failed_redis_write_cannot_publish_readiness(monkeypatch):
    redis = AsyncMock()
    redis.set.side_effect = ConnectionError('synthetic Redis outage')
    monkeypatch.setattr(
        worker_runtime, '_log_runtime_identity', lambda _: pytest.fail('Failed writes cannot log readiness')
    )
    with pytest.raises(ConnectionError):
        await worker_runtime.publish_identity(redis, 'synthetic-instance')


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


@pytest.mark.parametrize('redis_fails', [False, True])
def test_real_arq_logging_emits_only_successful_fixed_receipts_with_root_warning(tmp_path, redis_fails):
    script = """
import asyncio
import logging
import logging.config
from arq.logs import default_log_config
from app.core.worker_runtime import publish_identity

logging.getLogger().setLevel(logging.WARNING)
logging.config.dictConfig(default_log_config(False))

class Redis:
    async def set(self, *args, **kwargs):
        if REDIS_FAILS:
            raise ConnectionError('synthetic-private-exception-do-not-log')

async def check():
    for _ in range(2):
        try:
            await publish_identity(Redis(), '12345678-1234-1234-1234-123456789abc')
        except ConnectionError:
            pass

asyncio.run(check())
""".replace('REDIS_FAILS', repr(redis_fails))
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=tmp_path,
        env={
            'PATH': os.environ.get('PATH', ''),
            'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
            'ENVIRONMENT': 'test',
            'DATABASE_URL': 'sqlite:///:memory:',
            'SECRET_KEY': 'synthetic-release-test-key-not-real-0123456789',
            'REFRESH_TOKEN_SECRET_KEY': 'synthetic-refresh-test-key-not-real-0123456789',
            'APP_RELEASE': 'a' * 40,
            'RAILWAY_DEPLOYMENT_ID': 'synthetic-deployment',
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    receipts = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(receipts) == (0 if redis_fails else 2)
    for receipt in receipts:
        assert set(receipt) == {'event', 'identity'}
        assert receipt['event'] == 'worker_runtime_ready'
        identity = receipt['identity']
        assert set(identity) == {'release', 'instance_id', 'deployment_id', 'observed_at'}
        assert identity['release'] == 'a' * 40
        assert identity['deployment_id'] == 'synthetic-deployment'
    assert 'synthetic-private' not in result.stdout + result.stderr
