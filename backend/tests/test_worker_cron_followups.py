"""Behavior locks for the ARQ worker cron/kwargs fix
(fix/wo-remediation-followups, FIX 3).

``WorkerSettings.cron_jobs`` previously built with a ``TypeError`` because a cron entry tried
to pass ``kwargs=`` through ``arq.cron`` for the MRP AUTO_DRAFT pass (cron coroutines are fired
with only ``ctx``). The fix introduces ``run_mrp_auto_draft_job(ctx)`` -- a thin wrapper that
pins ``mode="AUTO_DRAFT"`` and delegates to ``run_mrp_job`` -- and registers it in BOTH
``cron_jobs`` (6 AM) and ``functions``.

Guards:
  - importing ``WorkerSettings`` and reading ``cron_jobs`` no longer raises, and the list is
    fully built (>= 9 entries),
  - ``run_mrp_auto_draft_job`` is a registered worker function, and
  - calling it delegates to ``run_mrp_job`` with ``mode="AUTO_DRAFT"``.
"""

import pytest

pytestmark = [pytest.mark.unit]


def test_worker_settings_cron_jobs_build_without_error():
    # The import + class body construct the cron list; a kwargs= TypeError would raise here.
    from app.worker import WorkerSettings

    cron_jobs = WorkerSettings.cron_jobs
    assert isinstance(cron_jobs, list)
    assert len(cron_jobs) >= 9


def test_run_mrp_auto_draft_job_registered_as_function():
    from app.worker import WorkerSettings, run_mrp_auto_draft_job

    assert run_mrp_auto_draft_job in WorkerSettings.functions


def test_run_mrp_auto_draft_job_delegates_with_auto_draft_mode(monkeypatch):
    import app.worker as worker

    captured = {}

    async def fake_run_mrp_job(ctx, mode="REVIEW", company_id=None):
        captured["ctx"] = ctx
        captured["mode"] = mode
        captured["company_id"] = company_id
        return {"ok": True}

    monkeypatch.setattr(worker, "run_mrp_job", fake_run_mrp_job)

    import asyncio

    ctx = {"job_id": "test"}
    result = asyncio.run(worker.run_mrp_auto_draft_job(ctx))

    assert result == {"ok": True}
    assert captured["ctx"] is ctx
    assert captured["mode"] == "AUTO_DRAFT"


def test_run_mrp_job_default_mode_is_review_not_auto_draft(monkeypatch):
    """Contrast lock: the bare ``run_mrp_job`` defaults to REVIEW; only the wrapper pins
    AUTO_DRAFT. Pins that AUTO_DRAFT is opt-in, not the request default."""
    import app.worker as worker

    captured = {}

    async def fake_run_mrp_task(mode, company_id=None):
        captured["mode"] = mode
        return {"ok": True}

    # run_mrp_job imports run_mrp_task lazily from app.jobs.mrp_jobs; patch it there.
    import app.jobs.mrp_jobs as mrp_jobs

    monkeypatch.setattr(mrp_jobs, "run_mrp_task", fake_run_mrp_task)

    import asyncio

    asyncio.run(worker.run_mrp_job({"job_id": "t"}))
    assert captured["mode"] == "REVIEW"
