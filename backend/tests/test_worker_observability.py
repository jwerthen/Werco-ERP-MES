"""The worker must be legible from its log alone.

Nobody watches a background worker. It has no HTTP surface to curl, no UI, and its most
consequential jobs run at 02:30. Everything below exists so a human reading the first twenty
lines of the container log can answer, without guessing: which Redis did it connect to,
which commit is running, which crons are armed, and when each next fires -- and so that a
crash is reported somewhere other than a log line nobody reads.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict

import pytest

pytestmark = [pytest.mark.unit]


class TestSentryIsWiredInTheWorkerProcess:
    """The worker never imports ``app.main``, where the only ``sentry_sdk.init`` used to be,
    so until this change every worker traceback died in the container log."""

    def test_worker_module_initializes_sentry(self):
        import inspect

        import app.worker as worker

        source = inspect.getsource(worker)
        assert "init_sentry(component=" in source

    def test_shared_init_tags_the_component(self, monkeypatch: pytest.MonkeyPatch):
        """API and worker events land in one Sentry project; the tag is what separates them."""
        import sentry_sdk

        from app.core.config import settings
        from app.core.observability import init_sentry

        captured: Dict[str, Any] = {}
        tags: Dict[str, Any] = {}
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
        monkeypatch.setattr(sentry_sdk, "set_tag", lambda k, v: tags.update({k: v}))
        monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@example.ingest.sentry.io/1")

        init_sentry(component="worker")

        assert tags["component"] == "worker"
        assert captured["release"] == settings.APP_RELEASE
        assert captured["environment"] == settings.ENVIRONMENT
        # No FastAPI integration in a process that runs no FastAPI app.
        assert captured["integrations"] == []

    def test_a_bad_dsn_cannot_stop_the_worker_from_starting(self, monkeypatch: pytest.MonkeyPatch):
        import sentry_sdk
        from sentry_sdk.utils import BadDsn

        from app.core.config import settings
        from app.core.observability import init_sentry

        def boom(**kwargs: Any) -> None:
            raise BadDsn("Unsupported scheme ''")

        monkeypatch.setattr(sentry_sdk, "init", boom)
        monkeypatch.setattr(settings, "SENTRY_DSN", "not-a-dsn")

        init_sentry(component="worker")  # must not raise

    def test_the_api_still_gets_its_fastapi_integration(self, monkeypatch: pytest.MonkeyPatch):
        """Refactoring the shared init must not have quietly dropped the API's integration."""
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        from app.core.config import settings
        from app.main import init_sentry as api_init_sentry

        captured: Dict[str, Any] = {}
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
        monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@example.ingest.sentry.io/1")

        api_init_sentry()

        assert any(isinstance(i, FastApiIntegration) for i in captured["integrations"])


class TestStartupSaysWhatItConnectedTo:
    @pytest.mark.asyncio
    async def test_startup_logs_the_redis_target_the_queue_name_and_the_release(self, caplog):
        import app.worker as worker

        with caplog.at_level(logging.INFO):
            await worker.startup({})

        text = caplog.text
        assert "ARQ worker Redis:" in text
        assert worker.WorkerSettings.queue_name in text
        assert "environment=" in text and "release=" in text

    @pytest.mark.asyncio
    async def test_startup_never_logs_the_redis_password(self, monkeypatch: pytest.MonkeyPatch, caplog):
        import importlib

        from app.core.config import settings

        monkeypatch.setattr(settings, "REDIS_URL", "redis://default:sup3rs3cret@host.internal:6379/0")
        import app.worker as worker

        worker = importlib.reload(worker)
        try:
            with caplog.at_level(logging.INFO):
                await worker.startup({})
            assert "sup3rs3cret" not in caplog.text
            assert "host.internal" in caplog.text
        finally:
            monkeypatch.undo()
            importlib.reload(worker)

    @pytest.mark.asyncio
    async def test_startup_logs_every_armed_cron_with_a_next_run_time(self, caplog):
        import app.worker as worker

        with caplog.at_level(logging.INFO):
            await worker.startup({})

        for job in worker.WorkerSettings.cron_jobs:
            assert job.name in caplog.text
        assert "next run" in caplog.text


class TestCronScheduleDescription:
    def test_next_run_is_computed_for_every_cron(self):
        from app.worker import ALL_CRON_JOBS, describe_cron_schedule

        lines = describe_cron_schedule(ALL_CRON_JOBS)
        assert len(lines) == len(ALL_CRON_JOBS)
        assert all("next run" in line and "unknown" not in line for line in lines)

    def test_describing_the_schedule_does_not_mutate_the_cron_jobs(self):
        """arq computes ``next_run`` itself at worker construction; pre-setting it from a
        logging helper would interfere with the real scheduling."""
        from app.worker import ALL_CRON_JOBS, describe_cron_schedule

        before = [job.next_run for job in ALL_CRON_JOBS]
        describe_cron_schedule(ALL_CRON_JOBS)
        assert [job.next_run for job in ALL_CRON_JOBS] == before

    def test_times_are_reported_in_the_containers_local_zone(self):
        """arq defaults its timezone to ``datetime.now().astimezone().tzinfo``. On Railway
        that is UTC unless TZ is set -- so "6 AM" is 06:00 UTC, i.e. 01:00 Central. The log
        must print the times that will actually happen, not naive ones."""
        from app.worker import ALL_CRON_JOBS, describe_cron_schedule

        reference = datetime.now().astimezone()
        lines = describe_cron_schedule(ALL_CRON_JOBS[:1], now=reference)
        stamp = lines[0].split("next run ")[1]
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None
        assert reference <= parsed <= reference + timedelta(days=32)


class TestCronSelection:
    """``WORKER_CRON_JOBS`` narrows the schedule for a controlled first boot. It must default
    to today's behavior -- this change enables nothing that was previously off."""

    def test_default_registers_every_cron(self):
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        assert select_cron_jobs("") == ALL_CRON_JOBS
        assert select_cron_jobs("all") == ALL_CRON_JOBS

    def test_worker_settings_defaults_to_the_full_schedule(self, monkeypatch: pytest.MonkeyPatch):
        import importlib

        monkeypatch.delenv("WORKER_CRON_JOBS", raising=False)
        import app.worker as worker

        worker = importlib.reload(worker)
        try:
            assert len(worker.WorkerSettings.cron_jobs) == len(worker.ALL_CRON_JOBS)
        finally:
            importlib.reload(worker)

    def test_none_arms_nothing_but_leaves_the_job_functions_registered(self, monkeypatch: pytest.MonkeyPatch):
        """The safe first boot: drain the enqueue-driven queue (notifications, webhooks,
        labels) without firing any bulk scheduled work."""
        import importlib

        monkeypatch.setenv("WORKER_CRON_JOBS", "none")
        import app.worker as worker

        worker = importlib.reload(worker)
        try:
            assert worker.WorkerSettings.cron_jobs == []
            assert len(worker.WorkerSettings.functions) >= 20
        finally:
            monkeypatch.delenv("WORKER_CRON_JOBS", raising=False)
            importlib.reload(worker)

    def test_a_subset_can_be_armed_by_name_in_either_spelling(self):
        from app.worker import select_cron_jobs

        selected = select_cron_jobs("check_low_stock_job,cron:poll_tracking_job")
        assert [job.name for job in selected] == ["cron:check_low_stock_job", "cron:poll_tracking_job"]

    def test_a_name_repeated_in_both_spellings_arms_the_cron_once(self):
        """Registering the same cron twice would double every run of it."""
        from app.worker import select_cron_jobs

        selected = select_cron_jobs("check_low_stock_job,cron:check_low_stock_job")
        assert len(selected) == 1

    def test_an_unknown_name_is_a_hard_error(self):
        """ "I enabled the cron and nothing happened" is the exact failure mode this whole
        change exists to eliminate; a silent skip would recreate it."""
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("check_low_stock_job,typo_job")
        assert "typo_job" in str(exc.value)


class TestTheCronInventoryIsStable:
    """These crons fire the moment a worker process runs. If the list changes, the go-live
    runbook's blast-radius counts (docs/WORKER_SERVICE.md) are stale."""

    def test_the_declared_cron_set_is_exactly_what_the_runbook_documents(self):
        from app.worker import ALL_CRON_JOBS

        names = {job.name.removeprefix("cron:") for job in ALL_CRON_JOBS}
        assert names == {
            "run_mrp_auto_draft_job",
            "send_daily_digest_job",
            "check_calibrations_job",
            "check_late_work_orders_job",
            "check_low_stock_job",
            "check_quote_expiring_job",
            "aggregate_ai_learning_job",
            "run_oee_auto_calc_job",
            "cleanup_old_logs_job",
            "archive_aged_audit_logs_job",
            "poll_tracking_job",
            "relay_pending_notifications_job",
        }
