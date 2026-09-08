"""Readiness follows the selected schedule even when Sentry instruments its callable."""

from unittest.mock import AsyncMock

import pytest
from arq import cron
from arq.worker import Worker
from sentry_sdk.integrations.arq import _get_arq_cron_job

from app import worker
from app.jobs import quote_nesting_runs

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def run_startup(monkeypatch, selected):
    start_runtime = AsyncMock()
    monkeypatch.setattr(quote_nesting_runs, "startup_nesting_runtime", start_runtime)
    monkeypatch.setattr(worker.WorkerSettings, "cron_jobs", selected)
    ctx = {}
    await worker.startup(ctx)
    start_runtime.assert_awaited_once_with(ctx)
    return ctx["nesting_relay_enabled"]


async def test_real_sentry_wrapping_preserves_selected_relay_readiness(monkeypatch):
    selected = worker.select_cron_jobs("relay_quote_nesting_runs_job")
    assert selected == [worker.NESTING_RELAY_CRON]
    configured = selected[0]
    original = configured.coroutine
    # Restore the shared CronJob after the actual SDK mutates it in place.
    monkeypatch.setattr(configured, "coroutine", original)
    assert _get_arq_cron_job(configured) is configured
    assert configured.coroutine is not original
    arq_worker = Worker(functions=[], cron_jobs=selected, handle_signals=False)
    assert arq_worker.cron_jobs[0] is configured
    assert await run_startup(monkeypatch, arq_worker.cron_jobs) is True


@pytest.mark.parametrize("selection", ["none", "all,-relay_quote_nesting_runs_job"])
async def test_disabled_or_excluded_relay_remains_unready(monkeypatch, selection):
    assert await run_startup(monkeypatch, worker.select_cron_jobs(selection)) is False


async def test_same_name_unrelated_schedule_cannot_claim_readiness(monkeypatch):
    async def unrelated(ctx):
        return None

    impostor = cron(unrelated, name=worker.NESTING_RELAY_CRON.name, second={0, 30})
    assert await run_startup(monkeypatch, [impostor]) is False


async def test_same_callable_unregistered_schedule_cannot_claim_readiness(monkeypatch):
    replacement = cron(worker.relay_quote_nesting_runs_job, second={0, 30})
    assert replacement is not worker.NESTING_RELAY_CRON
    assert await run_startup(monkeypatch, [replacement]) is False
