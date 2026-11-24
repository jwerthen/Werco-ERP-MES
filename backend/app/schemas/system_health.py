"""Response contract for ``GET /admin/system-health`` (read-only)."""

from datetime import datetime
from typing import Dict, List, Literal, Optional, Union

from app.schemas.base import UTCModel

# alive     -- a worker heartbeat key exists (each one lives 90 s and is refreshed every 30 s).
# not_seen  -- Redis answered and holds no heartbeat: no worker is running against this Redis.
# unknown   -- Redis could not be read, so nothing can be said either way.
WorkerStatus = Literal["alive", "not_seen", "unknown"]


class WorkerHealth(UTCModel):
    status: WorkerStatus
    heartbeat_age_seconds: Optional[float] = None
    last_heartbeat_at: Optional[datetime] = None
    release: Optional[str] = None
    instance_count: int = 0


class CronRunResult(UTCModel):
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    outcome: Literal["success", "failed"]
    # The exception CLASS only on a failure; details are in the worker log / Sentry.
    error_type: Optional[str] = None
    # Flat numeric/boolean values the job returned (e.g. {"enqueued": 3}).
    counts: Dict[str, Union[bool, int, float]] = {}


class CronJobHealth(UTCModel):
    name: str
    # True / False as the running worker registered it; None when no worker is alive or Redis
    # cannot be read -- "unknown", never guessed.
    armed: Optional[bool] = None
    schedule: Optional[str] = None
    next_run_at: Optional[datetime] = None
    last_run: Optional[CronRunResult] = None


class CompanyBacklog(UTCModel):
    # Events the notification re-send sweeper would still pick up (its own predicate).
    unnotified_events: int
    # Cataloged events older than the sweeper's 24 h floor that never got a notification (last 30 days).
    unnotified_events_stranded: int
    # Digest items waiting for the daily digest (DigestQueue.processed is false).
    queued_digest_items: int
    # Failed email delivery attempts in the window (same rule as GET /notifications/logs?status=failed).
    failed_emails: int
    failed_email_window_days: int


class SystemHealthResponse(UTCModel):
    generated_at: datetime
    redis_available: bool
    worker: WorkerHealth
    schedule_timezone: Optional[str] = None
    # When the running worker last published its cron registry (worker startup).
    schedule_published_at: Optional[datetime] = None
    # The worker's WORKER_CRON_JOBS value as it read it (None when unset = every cron).
    cron_spec: Optional[str] = None
    cron_jobs: List[CronJobHealth]
    backlog: CompanyBacklog
