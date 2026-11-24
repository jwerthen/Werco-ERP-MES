"""What the worker's crons are, when they run, and how each one last went -- published to Redis.

The worker already proves it is alive (``app.core.worker_runtime`` -- a pinned, credential-free
heartbeat the deploy gate reads). This module carries the two things that heartbeat deliberately
does not, each in its OWN Redis key so the heartbeat payload stays byte-identical:

* ``CRON_REGISTRY_KEY`` -- written once at worker startup: every cron in ``ALL_CRON_JOBS``, whether
  this worker ARMED it (``WORKER_CRON_JOBS``), its schedule fields and the zone they are read in. No
  expiry: it is the last-known registry, and ``GET /admin/system-health`` pairs it with the live
  heartbeat to decide whether "armed" is current or merely last-known.
* ``CRON_LAST_RUN_KEY`` -- a no-expiry hash, one field per cron name, written by the worker's ONE
  ``after_job_end`` hook (``record_cron_run``) from arq's own result record: start, finish, outcome
  and the numeric counts the job returned. arq keeps its result for an hour (``keep_cron_results``
  -- ``arq.cron()`` otherwise keeps NONE); this keeps the last one per cron until the next run
  replaces it. The ``on_job_start`` hook (``record_cron_start``) first writes outcome ``running``,
  so a run arq never finishes normally (worker killed mid-run, then "max tries exceeded", which
  skips ``after_job_end``) reads as started-and-unfinished, never as the previous run's result.

Both writers are best-effort: a Redis failure here is logged and never fails a job or the startup.
Both readers (``app.services.system_health_service``) treat a missing or malformed value as
"unknown", never as an error.

Nothing here imports ``app.worker`` -- that module resolves Redis and initializes Sentry for the
worker process at import, which the API must not do. The worker hands its ``CronJob`` objects in.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# The zone every cron hour is read in. arq otherwise defaults to the container's local zone, which
# on Railway is UTC unless TZ is set -- "6 AM" then meant 01:00 Central. Pinned in code (not TZ) so
# the schedule cannot silently move with an environment variable, and a real IANA zone (not the
# fixed offset ``datetime.now().astimezone()`` would capture at boot) so it follows daylight saving.
SHOP_TIMEZONE_NAME = "America/Chicago"
SHOP_TIMEZONE = ZoneInfo(SHOP_TIMEZONE_NAME)

CRON_REGISTRY_KEY = "werco:worker:cron_registry"
CRON_LAST_RUN_KEY = "werco:worker:cron_last_run"
# The pinned heartbeat (app.core.worker_runtime.publish_identity) -- read here, never written.
RUNTIME_HEARTBEAT_PATTERN = "werco:worker:runtime:*"

# How long arq keeps a cron's result record. ``record_cron_run`` reads it back right after the run,
# so this only needs to outlive the hook; an hour matches the worker-wide ``keep_result``.
CRON_RESULT_KEEP_SECONDS = 3600

REGISTRY_VERSION = 1
_SCHEDULE_FIELDS = ("month", "day", "weekday", "hour", "minute", "second")
# Keep the stored counts small and numeric: a job's return value can be any shape, and only flat
# numbers are safe to show an Admin without knowing what the job put there.
_MAX_COUNT_KEYS = 20
_MAX_KEY_LENGTH = 64
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def cron_display_name(name: str) -> str:
    """arq names a cron ``cron:<coroutine>``; the env var and the docs use the bare name."""
    return name.removeprefix("cron:")


def _field_value(value: Any) -> Any:
    """A schedule field as JSON: None, an int, or a sorted list (arq accepts sets)."""
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, str):  # arq allows weekday names
        return value
    return sorted(value)


def schedule_fields(job: Any) -> Dict[str, Any]:
    """The schedule a ``CronJob`` carries, JSON-safe."""
    return {field: _field_value(getattr(job, field, None)) for field in _SCHEDULE_FIELDS}


def build_cron_registry(
    all_jobs: Sequence[Any],
    registered: Sequence[Any],
    *,
    cron_spec: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Every cron, whether this worker armed it, and its schedule -- in ``all_jobs`` order.

    Armed is decided by NAME, the same way ``select_cron_jobs`` subtracts exclusions, so a cron
    scheduled twice reads as armed when either instance is registered.
    """
    armed_names = {job.name for job in registered}
    jobs: List[Dict[str, Any]] = []
    seen: set = set()
    for job in all_jobs:
        name = cron_display_name(job.name)
        if name in seen:
            continue
        seen.add(name)
        jobs.append({"name": name, "armed": job.name in armed_names, **schedule_fields(job)})
    return {
        "version": REGISTRY_VERSION,
        "published_at": (now or datetime.now(timezone.utc)).isoformat(),
        "timezone": SHOP_TIMEZONE_NAME,
        "cron_spec": cron_spec,
        "jobs": jobs,
    }


async def publish_cron_registry(redis: Any, all_jobs: Sequence[Any], registered: Sequence[Any]) -> bool:
    """Write the registry. Best-effort: returns False (and logs) instead of raising."""
    if redis is None:
        return False  # unit-test lifecycle contexts build no Redis pool
    payload = build_cron_registry(all_jobs, registered, cron_spec=os.getenv("WORKER_CRON_JOBS"))
    try:
        await redis.set(CRON_REGISTRY_KEY, json.dumps(payload))
        return True
    except Exception:
        logger.warning("Worker cron registry could not be published")
        return False


def _as_set(value: Any) -> Optional[set]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return set(value)
    return {value}


def next_run(fields: Mapping[str, Any], tz_name: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """The next time a cron with these fields fires, computed with arq's own ``next_cron``.

    Returns an aware datetime, or None when the fields cannot be evaluated.
    """
    from arq.cron import next_cron

    try:
        tz = ZoneInfo(tz_name)
        reference = (now or datetime.now(timezone.utc)).astimezone(tz)
        kwargs: Dict[str, Any] = {}
        for field in _SCHEDULE_FIELDS:
            value = fields.get(field)
            if field == "weekday" and isinstance(value, str):
                kwargs[field] = value
            elif field == "second" and value is None:
                kwargs[field] = 0  # arq's own default for second
            else:
                kwargs[field] = value if value is None or isinstance(value, int) else _as_set(value)
        return next_cron(reference, **kwargs)
    except Exception:
        return None


def _join_times(hours: Iterable[int], minutes: Iterable[int]) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for h in hours for m in minutes)


def describe_schedule(fields: Mapping[str, Any]) -> str:
    """A short human schedule ("Daily 06:00", "Every 5 min", "Sun 02:00", "Day 1 of month 03:00").

    Covers the shapes the cron table uses; anything else falls back to the raw fields.
    """
    hour = _as_set(fields.get("hour"))
    minute = _as_set(fields.get("minute"))
    weekday = fields.get("weekday")
    day = _as_set(fields.get("day"))
    month = fields.get("month")
    if month is None and minute is not None:
        if hour is None and weekday is None and day is None:
            mins = sorted(minute)
            if len(mins) > 1:
                step = mins[1] - mins[0]
                evenly_spaced = all(b - a == step for a, b in zip(mins, mins[1:]))
                if evenly_spaced and mins[0] == 0 and mins[-1] + step == 60:
                    return f"Every {step} min"
            return "Hourly at :" + ", :".join(f"{m:02d}" for m in mins)
        if hour is not None:
            times = _join_times(sorted(hour), sorted(minute))
            if weekday is None and day is None:
                return f"Daily {times}"
            if day is None and weekday is not None:
                if isinstance(weekday, str):
                    names = [weekday.capitalize()[:3]]
                else:
                    names = [_WEEKDAY_NAMES[d] for d in sorted(_as_set(weekday) or set()) if 0 <= d < 7]
                return f"{', '.join(names)} {times}"
            if weekday is None and day is not None:
                days = ", ".join(str(d) for d in sorted(day))
                return f"Day {days} of month {times}"
    shown = {k: v for k, v in fields.items() if v is not None}
    return json.dumps(shown, sort_keys=True)


def summarize_counts(result: Any) -> Dict[str, Any]:
    """The flat numeric/boolean values a job returned, capped -- everything else is dropped."""
    if not isinstance(result, Mapping):
        return {}
    counts: Dict[str, Any] = {}
    for key, value in result.items():
        if len(counts) >= _MAX_COUNT_KEYS:
            break
        if not isinstance(key, str) or len(key) > _MAX_KEY_LENGTH:
            continue
        if isinstance(value, bool) or (isinstance(value, (int, float)) and value == value):  # NaN excluded
            counts[key] = value
    return counts


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return None


def build_run_record(info: Any) -> Dict[str, Any]:
    """One last-run record from an arq ``JobResult``.

    A failure stores only the exception's CLASS name: the message can carry whatever the job was
    touching, and the Admin screen needs "it failed, look at the worker log / Sentry", not the text.
    """
    success = bool(getattr(info, "success", False))
    result = getattr(info, "result", None)
    record: Dict[str, Any] = {
        "job_id": str(getattr(info, "job_id", "") or ""),
        "started_at": _iso(getattr(info, "start_time", None)),
        "finished_at": _iso(getattr(info, "finish_time", None)),
        "outcome": "success" if success else "failed",
        "error_type": None if success else (type(result).__name__ if result is not None else "Unknown"),
        "counts": summarize_counts(result) if success else {},
    }
    return record


def keep_cron_results(jobs: Iterable[Any], seconds: int = CRON_RESULT_KEEP_SECONDS) -> None:
    """Make arq store each cron's result, which ``record_cron_run`` reads back.

    ``arq.cron()`` defaults every cron to ``keep_result=0``, and a per-function 0 wins over the
    worker-wide ``WorkerSettings.keep_result`` (arq 0.28 ``Worker.run_job``: ``result_timeout_s =
    function.keep_result_s`` whenever that is not None). Left alone, no cron result is ever
    written, ``Job.result_info()`` returns None and no run is ever recorded. Mutates the jobs in
    place so the cron table itself (order, schedule) is untouched. A cron that already asks for a
    positive TTL or to be kept forever keeps it.
    """
    for job in jobs:
        if not getattr(job, "keep_result_forever", None) and not (getattr(job, "keep_result_s", None) or 0) > 0:
            job.keep_result_s = seconds


async def record_cron_start(ctx: Mapping[str, Any]) -> None:
    """The worker's ``on_job_start`` hook: mark a CRON run as started (outcome ``running``).

    ``record_cron_run`` replaces this when the run ends normally. arq skips ``after_job_end`` when
    a run is cut off -- a worker killed or redeployed mid-run is redelivered and fails as "max tries
    exceeded" in ``finish_failed_job``, which returns before that hook -- so without this a cut-off
    run would leave the PREVIOUS run's success showing. Now it shows as a run that started and
    never finished. Never raises.
    """
    job_id = ctx.get("job_id")
    redis = ctx.get("redis")
    if redis is None or not isinstance(job_id, str) or not job_id.startswith("cron:"):
        return
    name = cron_display_name(job_id.rsplit(":", 1)[0])
    record = {
        "job_id": job_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "outcome": "running",
        "error_type": None,
        "counts": {},
    }
    try:
        await redis.hset(CRON_LAST_RUN_KEY, name, json.dumps(record))
    except Exception:
        logger.warning("Worker cron start record could not be written for %s", name)


async def record_cron_run(ctx: Mapping[str, Any]) -> None:
    """The worker's single ``after_job_end`` hook: remember how a CRON run went.

    Runs after arq has stored the job's result, so the result record is read back by job id. Only
    cron runs are recorded -- arq gives every cron run the id ``cron:<name>:<ms>``, and nothing else
    the API enqueues uses that prefix. A job that will be retried has no result yet and is skipped
    until its final try. Never raises.
    """
    job_id = ctx.get("job_id")
    redis = ctx.get("redis")
    if redis is None or not isinstance(job_id, str) or not job_id.startswith("cron:"):
        return
    try:
        from arq.jobs import Job

        info = await Job(job_id, redis).result_info()
        if info is None:
            return
        name = cron_display_name(str(getattr(info, "function", "") or job_id.rsplit(":", 1)[0]))
        await redis.hset(CRON_LAST_RUN_KEY, name, json.dumps(build_run_record(info)))
    except Exception:
        logger.warning("Worker cron last-run record could not be written for %s", job_id.rsplit(":", 1)[0])
