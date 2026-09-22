"""Hank follow-ups run on the opted-in schedule without blocking ARQ's loop."""

import threading

import pytest

pytestmark = [pytest.mark.unit]


def test_hank_cron_is_registered_once_at_five_minute_intervals():
    from app.worker import ALL_CRON_JOBS, WorkerSettings, check_hank_watches_job

    matches = [job for job in ALL_CRON_JOBS if job.coroutine is check_hank_watches_job]
    assert len(matches) == 1
    assert matches[0].minute == set(range(0, 60, 5))
    assert matches[0].hour is None
    assert WorkerSettings.functions.count(check_hank_watches_job) == 1


def test_production_hank_only_allowlist_preserves_other_suppressed_jobs():
    from app.worker import ALL_CRON_JOBS, check_hank_watches_job, select_cron_jobs

    selected = select_cron_jobs('check_hank_watches_job')
    assert len(selected) == 1
    assert selected[0].coroutine is check_hank_watches_job
    assert len(ALL_CRON_JOBS) > len(selected)
    assert select_cron_jobs('none') == []


@pytest.mark.asyncio
async def test_worker_delegates_to_the_bounded_hank_task(monkeypatch):
    from app.jobs import hank_jobs
    from app.worker import check_hank_watches_job

    calls = []
    receipt = {'checked': 2, 'completed': 1}

    async def fake_task():
        calls.append(True)
        return receipt

    monkeypatch.setattr(hank_jobs, 'check_hank_watches_task', fake_task)
    assert await check_hank_watches_job({'job_id': 'hank-test'}) is receipt
    assert calls == [True]


@pytest.mark.asyncio
async def test_hank_task_runs_service_in_another_thread_and_returns_its_result(monkeypatch):
    from app.jobs.hank_jobs import check_hank_watches_task
    from app.services import hank_watch_service

    event_loop_thread = threading.get_ident()
    calls = []
    receipt = {'checked': 4, 'completed': 2}

    def fake_service(*, limit):
        calls.append((limit, threading.get_ident()))
        return receipt

    monkeypatch.setattr(hank_watch_service, 'process_hank_watches', fake_service)
    assert await check_hank_watches_task() is receipt
    assert len(calls) == 1
    assert calls[0][0] == 100
    assert calls[0][1] != event_loop_thread


@pytest.mark.asyncio
async def test_hank_service_failure_reaches_arq_instead_of_a_success_receipt(monkeypatch):
    from app.jobs.hank_jobs import check_hank_watches_task
    from app.services import hank_watch_service

    def failed_service(*, limit):
        raise RuntimeError('batch unavailable')

    monkeypatch.setattr(hank_watch_service, 'process_hank_watches', failed_service)
    with pytest.raises(RuntimeError, match='batch unavailable'):
        await check_hank_watches_task()


def test_pdf_analysis_is_request_driven_and_registered_once():
    from app.worker import ALL_CRON_JOBS, WorkerSettings, process_hank_intake_file_job

    assert WorkerSettings.functions.count(process_hank_intake_file_job) == 1
    assert not any(job.coroutine is process_hank_intake_file_job for job in ALL_CRON_JOBS)


@pytest.mark.asyncio
async def test_pdf_worker_delegates_the_exact_saved_file(monkeypatch):
    from app.jobs import hank_intake_jobs
    from app.worker import process_hank_intake_file_job

    calls = []
    result = {'id': 42, 'status': 'awaiting_review'}

    async def fake_task(file_id):
        calls.append(file_id)
        return result

    monkeypatch.setattr(hank_intake_jobs, 'process_hank_intake_file_task', fake_task)
    assert await process_hank_intake_file_job({'job_id': 'hank-intake:42:1'}, 42) is result
    assert calls == [42]


@pytest.mark.asyncio
async def test_pdf_extraction_releases_event_loop_and_propagates_failure(monkeypatch):
    from app.jobs.hank_intake_jobs import process_hank_intake_file_task
    from app.services import hank_intake_service

    event_loop_thread = threading.get_ident()
    calls = []

    def fake_service(file_id):
        calls.append((file_id, threading.get_ident()))
        raise RuntimeError('intake unavailable')

    monkeypatch.setattr(hank_intake_service, 'process_intake_file', fake_service)
    with pytest.raises(RuntimeError, match='intake unavailable'):
        await process_hank_intake_file_task(42)
    assert len(calls) == 1
    assert calls[0][0] == 42
    assert calls[0][1] != event_loop_thread
