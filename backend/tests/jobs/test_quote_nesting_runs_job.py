"""Run the actual bundled shared kernel as a child; no mocked geometry validator."""

import importlib.util
import shutil
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.db.database import atomic_transaction
from app.jobs import quote_nesting_runs as jobs
from app.models.quote_nesting_run import QuoteNestingRunCheckpoint
from app.services import quote_nesting_run_outbox as outbox
from app.services import quote_nesting_runs as service
from app.services.audit_service import AuditService

_helpers_spec = importlib.util.spec_from_file_location(
    "nesting_run_test_helpers", Path(__file__).parents[1] / "services/test_quote_nesting_runs_service.py"
)
helpers = importlib.util.module_from_spec(_helpers_spec)
_helpers_spec.loader.exec_module(helpers)


@pytest.mark.asyncio
async def test_real_node_worker_persists_checked_checkpoint_and_report(db_session, admin_user, monkeypatch):
    bundle = Path(__file__).parents[2] / "nesting-runtime/solver.cjs"
    if not bundle.exists():
        pytest.skip("Run frontend npm run build:nesting-worker for real subprocess integration")
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node 22 is required for the real subprocess integration")
    monkeypatch.setattr(jobs, "NODE_PATH", Path(node))
    monkeypatch.setattr(jobs, "BUNDLE_PATH", bundle)
    monkeypatch.setattr(jobs, "MANIFEST_PATH", bundle.parent / "manifest.json")
    monkeypatch.setattr(jobs, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(outbox, "enqueue_job_fire_and_forget_fastfail", _noop)
    runtime = await jobs.verify_runtime()
    revision = helpers.saved(db_session, admin_user)
    with atomic_transaction(db_session):
        run = service.start_run(
            db_session, admin_user, 1, AuditService(db_session, admin_user), helpers.request_for(revision), runtime
        )
    result = await jobs.run_quote_nesting_task(company_id=1, run_id=run["id"])
    assert result == {"status": "finished", "code": None}
    db_session.expire_all()
    row = service.get_run(db_session, 1, run["id"])
    assert row.status == "COMPLETED" and row.evaluated_count == row.completed_count == 1
    assert row.bundle_sha256 == runtime["bundle_sha256"]
    checkpoint = db_session.query(QuoteNestingRunCheckpoint).one()
    assert checkpoint.result_json["result"]["nest"]["sheets"] == 1
    assert len(checkpoint.result_json["result"]["nest"]["placements"]) == 2
    report = service.export_report(db_session, 1, run["id"])
    assert report["status"] == "UNAPPROVED" and report["estimate"] == helpers.current_estimate()
    assert report["checkpoints"][0]["content_sha256"] == checkpoint.content_sha256
    assert checkpoint.result_json["result"]["leftovers"]["creditUSD"] == 0


async def _noop(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_excluded_relay_never_announces_runtime_readiness(monkeypatch):
    async def unexpected():
        raise AssertionError("An excluded durable relay cannot verify or publish readiness")

    monkeypatch.setattr(jobs, "verify_runtime", unexpected)
    context = {"nesting_relay_enabled": False}
    await jobs.startup_nesting_runtime(context)
    assert "nesting_runtime_heartbeat" not in context
    assert "nesting_runtime_identity" not in context


@pytest.mark.asyncio
@pytest.mark.parametrize("redis_fails", [False, True])
async def test_ready_identity_is_logged_only_after_successful_redis_write(monkeypatch, redis_fails):
    import asyncio

    written = asyncio.Event()
    logged = []

    class Pool:
        async def set(self, key, value, *, ex):
            assert key.startswith("quote-nesting:runtime:v1:") and ex == 90
            written.set()
            if redis_fails:
                raise ConnectionError("synthetic unavailable Redis")
            return True

    monkeypatch.setattr(jobs, "_log_runtime_identity", logged.append)
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "synthetic-deployment")
    context = {"redis": Pool(), "nesting_instance_id": "a" * 36}
    task = asyncio.create_task(jobs._publish_runtime(context, helpers.RUNTIME))
    try:
        await asyncio.wait_for(written.wait(), timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(logged) == (0 if redis_fails else 1)
    if logged:
        assert logged[0]["deployment_id"] == "synthetic-deployment"
        assert logged[0]["observed_at"].endswith("Z")


def test_safe_identity_log_remains_visible_with_warning_only_application_logs(monkeypatch):
    import io
    import json
    import logging

    stream = io.StringIO()
    identity_logger = logging.getLogger("werco.nesting_runtime_identity")
    monkeypatch.setattr(identity_logger, "handlers", [])
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    monkeypatch.setattr(jobs.sys, "stdout", stream)
    jobs._log_runtime_identity(helpers.RUNTIME)
    payload = json.loads(stream.getvalue())
    assert payload == {"event": "nesting_runtime_ready", "identity": helpers.RUNTIME}
    assert identity_logger.propagate is False
