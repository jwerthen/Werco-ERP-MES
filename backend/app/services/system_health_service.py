"""Read-only System Health: is the worker alive, which crons are armed, how each last ran, what is backed up.

A PURE READ. It writes nothing to the database or to Redis, takes no ``AuditService`` and enqueues
nothing. Two sources:

* **Redis** (``read_worker_state``): the pinned worker heartbeat keys, the cron registry the worker
  publishes at startup and the last-run hash its ``after_job_end`` hook keeps -- see
  ``app.core.worker_schedule``. Redis being unreachable is an expected state, reported as
  ``redis_available=False`` / worker ``unknown`` / armed ``None``; it is never an error.
* **The database**, tenant-scoped to the ACTIVE company: the notification outbox backlog (the relay
  sweeper's own predicate), queued digest items and failed email deliveries (the delivery log's
  own failed rule).
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.worker_schedule import (
    CRON_LAST_RUN_KEY,
    CRON_REGISTRY_KEY,
    RUNTIME_HEARTBEAT_PATTERN,
    describe_schedule,
    next_run,
)
from app.db.tenant_filter import tenant_query
from app.jobs.notification_jobs import _RELAY_MAX_AGE_HOURS, relay_pending_filters
from app.models.notification import DigestQueue, NotificationLog
from app.models.operational_event import OperationalEvent
from app.schemas.system_health import (
    CompanyBacklog,
    CronJobHealth,
    CronRunResult,
    SystemHealthResponse,
    WorkerHealth,
)
from app.services.notification_catalog import SOURCE_EVENT_TYPE_TO_KEY
from app.services.notification_delivery_status import failed_delivery_clause

logger = logging.getLogger(__name__)

FAILED_EMAIL_WINDOW_DAYS = 7
STRANDED_EVENT_WINDOW_DAYS = 30
# The whole Redis read is bounded: a slow or half-open Redis must not hold an Admin page.
REDIS_READ_TIMEOUT_SECONDS = 3.0
_MAX_HEARTBEAT_KEYS = 20


@dataclass
class WorkerState:
    """What Redis said, raw. ``available`` False means nothing below it could be read."""

    available: bool
    heartbeats: List[Dict[str, Any]] = field(default_factory=list)
    registry: Optional[Dict[str, Any]] = None
    last_runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _decode(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _load_json(value: Any) -> Optional[Any]:
    text = _decode(value)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


async def _read_from_redis() -> WorkerState:
    from arq import create_pool

    from app.core.queue import get_redis_settings

    pool = await create_pool(get_redis_settings(fast_fail=True))
    try:
        heartbeats: List[Dict[str, Any]] = []
        keys: List[Any] = []
        async for key in pool.scan_iter(match=RUNTIME_HEARTBEAT_PATTERN, count=100):
            keys.append(key)
            if len(keys) >= _MAX_HEARTBEAT_KEYS:
                break
        for key in keys:
            identity = _load_json(await pool.get(key))
            if isinstance(identity, dict):
                heartbeats.append(identity)
        registry = _load_json(await pool.get(CRON_REGISTRY_KEY))
        raw_runs = await pool.hgetall(CRON_LAST_RUN_KEY) or {}
        last_runs: Dict[str, Dict[str, Any]] = {}
        for name, value in raw_runs.items():
            record = _load_json(value)
            decoded = _decode(name)
            if decoded and isinstance(record, dict):
                last_runs[decoded] = record
        return WorkerState(
            available=True,
            heartbeats=heartbeats,
            registry=registry if isinstance(registry, dict) else None,
            last_runs=last_runs,
        )
    finally:
        try:
            await pool.aclose(close_connection_pool=True)
        except Exception:  # pragma: no cover - closing must never turn a read into an error
            pass


def read_worker_state() -> WorkerState:
    """Read the worker's Redis state from a SYNC handler. Never raises.

    Must run on a thread with no running event loop (FastAPI runs sync ``def`` routes in a
    threadpool worker), like ``enqueue_job_best_effort``.
    """

    async def _bounded() -> WorkerState:
        return await asyncio.wait_for(_read_from_redis(), timeout=REDIS_READ_TIMEOUT_SECONDS)

    try:
        return asyncio.run(_bounded())
    except Exception as exc:
        logger.warning("System health: worker state could not be read from Redis (%s)", type(exc).__name__)
        return WorkerState(available=False)


def _parse_instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _worker_health(state: WorkerState, now: datetime) -> WorkerHealth:
    if not state.available:
        return WorkerHealth(status="unknown")
    observed = [(_parse_instant(hb.get("observed_at")), hb) for hb in state.heartbeats]
    observed = [(at, hb) for at, hb in observed if at is not None]
    if not observed:
        return WorkerHealth(status="not_seen", instance_count=0)
    newest_at, newest = max(observed, key=lambda pair: pair[0])
    release = newest.get("release")
    return WorkerHealth(
        status="alive",
        heartbeat_age_seconds=round(max((now - newest_at).total_seconds(), 0.0), 1),
        last_heartbeat_at=newest_at,
        release=release if isinstance(release, str) else None,
        instance_count=len(observed),
    )


def _run_result(record: Optional[Dict[str, Any]], include_counts: bool) -> Optional[CronRunResult]:
    if not record or record.get("outcome") not in ("success", "failed", "running"):
        return None
    # A cron's counts are what the job RETURNED, and the jobs fan out over every company on the
    # install: "late_work_orders: 37" or "companies: 6" counts other tenants. Outcome and timing are
    # install-wide but tenant-neutral; the counts go to a platform principal only.
    counts = record.get("counts") if include_counts and isinstance(record.get("counts"), dict) else {}
    clean = {k: v for k, v in counts.items() if isinstance(k, str) and (isinstance(v, (bool, int, float)) and v == v)}
    error_type = record.get("error_type")
    return CronRunResult(
        started_at=_parse_instant(record.get("started_at")),
        finished_at=_parse_instant(record.get("finished_at")),
        outcome=record["outcome"],
        error_type=error_type if isinstance(error_type, str) else None,
        counts=clean,
    )


def _cron_jobs(state: WorkerState, worker: WorkerHealth, now: datetime, include_counts: bool) -> List[CronJobHealth]:
    registry = state.registry or {}
    tz_name = registry.get("timezone") if isinstance(registry.get("timezone"), str) else None
    live = worker.status == "alive"
    jobs: List[CronJobHealth] = []
    seen = set()
    for entry in registry.get("jobs") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        seen.add(name)
        armed = bool(entry.get("armed")) if live and isinstance(entry.get("armed"), bool) else None
        jobs.append(
            CronJobHealth(
                name=name,
                armed=armed,
                schedule=describe_schedule(entry),
                next_run_at=next_run(entry, tz_name, now) if armed and tz_name else None,
                last_run=_run_result(state.last_runs.get(name), include_counts),
            )
        )
    # A cron with a recorded run but missing from the registry (an older worker, or one removed
    # since) is still shown -- its last run is real evidence.
    for name in sorted(set(state.last_runs) - seen):
        jobs.append(
            CronJobHealth(name=name, armed=None, last_run=_run_result(state.last_runs.get(name), include_counts))
        )
    return jobs


def company_backlog(db: Session, company_id: int, now: datetime) -> CompanyBacklog:
    """The active company's notification backlog. Every query is tenant-scoped."""
    naive_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    if SOURCE_EVENT_TYPE_TO_KEY:
        unnotified = (
            tenant_query(db, OperationalEvent, company_id)
            .filter(*relay_pending_filters(naive_now))
            .with_entities(func.count(OperationalEvent.id))
            .scalar()
        )
        stranded = (
            tenant_query(db, OperationalEvent, company_id)
            .filter(
                OperationalEvent.event_type.in_(list(SOURCE_EVENT_TYPE_TO_KEY.keys())),
                OperationalEvent.notified_at.is_(None),
                OperationalEvent.created_at < naive_now - timedelta(hours=_RELAY_MAX_AGE_HOURS),
                OperationalEvent.created_at >= naive_now - timedelta(days=STRANDED_EVENT_WINDOW_DAYS),
            )
            .with_entities(func.count(OperationalEvent.id))
            .scalar()
        )
    else:  # pragma: no cover - the catalog is never empty in this app
        unnotified = stranded = 0
    digest = (
        tenant_query(db, DigestQueue, company_id)
        .filter(DigestQueue.processed == False)  # noqa: E712
        .with_entities(func.count(DigestQueue.id))
        .scalar()
    )
    failed = (
        tenant_query(db, NotificationLog, company_id)
        .filter(
            NotificationLog.channel == "email",
            NotificationLog.sent_at >= naive_now - timedelta(days=FAILED_EMAIL_WINDOW_DAYS),
            failed_delivery_clause(naive_now),
        )
        .with_entities(func.count(NotificationLog.id))
        .scalar()
    )
    return CompanyBacklog(
        unnotified_events=int(unnotified or 0),
        unnotified_events_stranded=int(stranded or 0),
        queued_digest_items=int(digest or 0),
        failed_emails=int(failed or 0),
        failed_email_window_days=FAILED_EMAIL_WINDOW_DAYS,
    )


def build_system_health(
    db: Session,
    company_id: int,
    state: WorkerState,
    now: Optional[datetime] = None,
    *,
    include_run_counts: bool = False,
) -> SystemHealthResponse:
    """Assemble the response from an already-read ``WorkerState`` (pure; testable without Redis).

    ``include_run_counts`` is True only for a platform principal: a cron's last-run counts are
    install-wide (every company the job fanned out over), so a tenant Admin gets ``counts == {}``.
    """
    now = now or datetime.now(timezone.utc)
    worker = _worker_health(state, now)
    registry = state.registry or {}
    tz_name = registry.get("timezone")
    cron_spec = registry.get("cron_spec")
    return SystemHealthResponse(
        generated_at=now,
        redis_available=state.available,
        worker=worker,
        schedule_timezone=tz_name if isinstance(tz_name, str) else None,
        schedule_published_at=_parse_instant(registry.get("published_at")),
        cron_spec=cron_spec if isinstance(cron_spec, str) else None,
        cron_jobs=_cron_jobs(state, worker, now, include_run_counts),
        backlog=company_backlog(db, company_id, now),
    )
