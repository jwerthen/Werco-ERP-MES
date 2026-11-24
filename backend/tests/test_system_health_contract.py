"""System Health: the contracts ``tests/test_system_health.py`` does not already pin.

* The after-job hook reads a REAL arq result record (arq's own ``serialize_result`` /
  ``Job.result_info``), not a hand-built namespace, so a renamed ``JobResult`` field cannot slip by.
* The Redis read is bounded: a hung Redis renders "unknown" inside the cap, never a hang or a 500.
* The schedule follows daylight saving (a real IANA zone, not a fixed offset captured at boot).
* Malformed Redis values degrade to "unknown" / no last run, never a 500.
* Credentials: a full Admin API token reads it (it is an MCP tool); read-only and AI-external
  tokens are refused, as is a Viewer.
* A pure read: no audit row, no outbox/digest/delivery-log change.
* The backlog windows (7-day failed email, 30-day stranded, stuck "sending") and the shared
  failed-delivery rule ``GET /notifications/logs?status=failed`` now reads too.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.core.security import create_access_token
from app.core.worker_schedule import (
    CRON_LAST_RUN_KEY,
    SHOP_TIMEZONE_NAME,
    build_cron_registry,
    describe_schedule,
    next_run,
    record_cron_run,
)
from app.models.audit_log import AuditLog
from app.models.notification import DigestQueue, NotificationLog
from app.models.operational_event import OperationalEvent
from app.models.user import User, UserRole
from app.services import system_health_service
from app.services.notification_catalog import SOURCE_EVENT_TYPE_TO_KEY
from app.services.notification_delivery_status import failed_delivery_clause
from app.services.system_health_service import WorkerState, build_system_health

URL = "/api/v1/admin/system-health"
API_TOKENS_URL = "/api/v1/api-tokens/"


# --------------------------------------------------------------------------- a real arq result record


class _ResultStore:
    """Just enough Redis for ``arq.jobs.Job.result_info`` (get) and the hook's write (hset)."""

    def __init__(self):
        self.values = {}
        self.hashes = {}

    async def get(self, key):
        return self.values.get(key)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value


def _store_real_result(store: _ResultStore, job_id: str, *, success: bool, result):
    from arq.constants import result_key_prefix
    from arq.jobs import serialize_result

    start_ms = 1_765_285_200_000  # 2025-12-09T13:00:00Z
    store.values[result_key_prefix + job_id] = serialize_result(
        "cron:check_calibrations_job",
        (),
        {},
        1,
        start_ms,
        success,
        result,
        start_ms,
        start_ms + 4_000,
        "ref",
        "arq:queue",
        job_id=job_id,
    )


@pytest.mark.unit
class TestHookAgainstRealArqResults:
    JOB_ID = "cron:check_calibrations_job:1765285200000"

    def test_success_round_trips_through_arqs_own_serializer(self):
        store = _ResultStore()
        _store_real_result(store, self.JOB_ID, success=True, result={"due": 2, "emails": 1, "note": "x"})
        asyncio.run(record_cron_run({"job_id": self.JOB_ID, "redis": store}))

        stored = json.loads(store.hashes[CRON_LAST_RUN_KEY]["check_calibrations_job"])
        assert stored["outcome"] == "success"
        assert stored["counts"] == {"due": 2, "emails": 1}
        assert stored["started_at"] == "2025-12-09T13:00:00+00:00"
        assert stored["finished_at"] == "2025-12-09T13:00:04+00:00"

    def test_failure_round_trips_and_keeps_only_the_exception_class(self):
        store = _ResultStore()
        _store_real_result(store, self.JOB_ID, success=False, result=RuntimeError("customer Fictional Co detail"))
        asyncio.run(record_cron_run({"job_id": self.JOB_ID, "redis": store}))

        raw = store.hashes[CRON_LAST_RUN_KEY]["check_calibrations_job"]
        stored = json.loads(raw)
        assert stored["outcome"] == "failed"
        assert stored["error_type"] == "RuntimeError"
        assert "Fictional" not in raw

    def test_a_retried_run_with_no_result_yet_records_nothing(self):
        store = _ResultStore()  # arq stores no result while the job will be retried
        asyncio.run(record_cron_run({"job_id": self.JOB_ID, "redis": store}))
        assert store.hashes == {}

    def test_the_recorded_run_reaches_the_endpoint_shape(self, db_session):
        store = _ResultStore()
        _store_real_result(store, self.JOB_ID, success=True, result={"due": 2})
        asyncio.run(record_cron_run({"job_id": self.JOB_ID, "redis": store}))
        last_runs = {name: json.loads(value) for name, value in store.hashes[CRON_LAST_RUN_KEY].items()}
        response = build_system_health(db_session, 1, WorkerState(available=True, last_runs=last_runs))
        (job,) = response.cron_jobs
        assert job.name == "check_calibrations_job"
        assert job.armed is None  # not in any registry
        assert job.last_run is not None and job.last_run.counts == {"due": 2}
        assert job.last_run.started_at == datetime(2025, 12, 9, 13, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- arq's real run_job path


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return queue

    async def execute(self):
        return [await getattr(self.redis, name)(*args, **kwargs) for name, args, kwargs in self.calls]


class _ArqRedis(_ResultStore):
    """Just enough Redis for ``arq.worker.Worker.run_job`` plus both System Health hooks."""

    def __init__(self):
        super().__init__()
        self.hset_log = []

    def pipeline(self, transaction=True):
        return _Pipeline(self)

    async def hset(self, key, field, value):
        self.hset_log.append(json.loads(value))
        await super().hset(key, field, value)

    async def set(self, key, value, px=None, **kwargs):
        self.values[key] = value

    async def setex(self, key, ttl, value):
        self.values[key] = value

    async def incr(self, key):
        self.values[key] = int(self.values.get(key) or 0) + 1
        return self.values[key]

    async def expire(self, *args, **kwargs):
        return True

    async def pexpire(self, *args, **kwargs):
        return True

    async def zrem(self, *args, **kwargs):
        return 0

    async def zincrby(self, *args, **kwargs):
        return 0

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


async def _check_calibrations_stub(ctx):
    return {"due": 3}


async def _ran_through_arq(cron_job, *, keep_results: bool):
    """Run ``cron_job`` once through arq's own ``Worker.run_job`` with the worker's real hooks."""
    from arq.constants import job_key_prefix
    from arq.jobs import serialize_job
    from arq.worker import Worker

    from app.core.worker_schedule import keep_cron_results, record_cron_start
    from app.worker import WorkerSettings

    if keep_results:
        keep_cron_results([cron_job])
    redis = _ArqRedis()
    job_id = f"{cron_job.name}:1765285200000"
    redis.values[job_key_prefix + job_id] = serialize_job(cron_job.name, (), {}, 1, int(time.time() * 1000))
    worker = Worker(
        cron_jobs=[cron_job],
        redis_pool=redis,
        handle_signals=False,
        on_job_start=record_cron_start,
        after_job_end=record_cron_run,
        keep_result=WorkerSettings.keep_result,  # the worker-wide value a cron's own 0 overrides
    )
    worker.ctx["redis"] = redis
    await worker.run_job(job_id, int(time.time() * 1000))
    return redis


@pytest.mark.unit
class TestHookOnArqsRealRunPath:
    """The hooks against arq's real ``run_job``: a hand-built result record cannot show that arq
    never WRITES one for a ``cron()`` job (``keep_result=0`` per function beats the worker's)."""

    @staticmethod
    def _cron():
        from arq import cron

        return cron(_check_calibrations_stub, name="cron:check_calibrations_job", hour=7, minute=0)

    def test_every_cron_in_the_table_keeps_its_result(self):
        from app.worker import ALL_CRON_JOBS

        assert ALL_CRON_JOBS
        assert all((job.keep_result_s or 0) > 0 or job.keep_result_forever for job in ALL_CRON_JOBS)

    def test_a_bare_arq_cron_keeps_no_result_so_nothing_would_be_recorded(self):
        redis = asyncio.run(_ran_through_arq(self._cron(), keep_results=False))
        stored = json.loads(redis.hashes[CRON_LAST_RUN_KEY]["check_calibrations_job"])
        assert stored["outcome"] == "running"  # only the start hook got anything to write

    def test_a_kept_cron_run_is_recorded_with_its_counts(self):
        redis = asyncio.run(_ran_through_arq(self._cron(), keep_results=True))
        stored = json.loads(redis.hashes[CRON_LAST_RUN_KEY]["check_calibrations_job"])
        assert stored["outcome"] == "success"
        assert stored["counts"] == {"due": 3}
        assert [entry["outcome"] for entry in redis.hset_log] == ["running", "success"]

    def test_a_run_cut_off_mid_run_reads_as_running_not_the_previous_success(self):
        from arq.constants import job_key_prefix, retry_key_prefix
        from arq.jobs import serialize_job
        from arq.worker import Worker

        cron_job = self._cron()
        redis = asyncio.run(_ran_through_arq(cron_job, keep_results=True))  # yesterday: success
        # Today's run starts, then the worker dies; arq redelivers it past max_tries=1 and fails it
        # in finish_failed_job, which never calls after_job_end.
        job_id = f"{cron_job.name}:1765371600000"

        async def cut_off():
            from app.core.worker_schedule import record_cron_start

            await record_cron_start({"job_id": job_id, "redis": redis})
            redis.values[job_key_prefix + job_id] = serialize_job(cron_job.name, (), {}, 1, int(time.time() * 1000))
            redis.values[retry_key_prefix + job_id] = 1  # the killed attempt
            worker = Worker(
                cron_jobs=[cron_job],
                redis_pool=redis,
                handle_signals=False,
                on_job_start=record_cron_start,
                after_job_end=record_cron_run,
            )
            worker.ctx["redis"] = redis
            await worker.run_job(job_id, int(time.time() * 1000))

        asyncio.run(cut_off())
        stored = json.loads(redis.hashes[CRON_LAST_RUN_KEY]["check_calibrations_job"])
        assert stored["outcome"] == "running"
        assert stored["job_id"] == job_id

    def test_a_running_record_reaches_the_endpoint(self, db_session):
        last_runs = {
            "check_calibrations_job": {
                "job_id": "cron:check_calibrations_job:1",
                "started_at": "2025-12-09T13:00:00+00:00",
                "finished_at": None,
                "outcome": "running",
                "error_type": None,
                "counts": {},
            }
        }
        response = build_system_health(db_session, 1, WorkerState(available=True, last_runs=last_runs))
        (job,) = response.cron_jobs
        assert job.last_run is not None and job.last_run.outcome == "running"
        assert job.last_run.finished_at is None


# --------------------------------------------------------------------------- the Redis read is bounded


@pytest.mark.unit
class TestRedisReadIsBounded:
    def test_a_hung_redis_is_unknown_within_the_cap(self, monkeypatch):
        async def hung():
            await asyncio.sleep(30)

        monkeypatch.setattr(system_health_service, "_read_from_redis", hung)
        monkeypatch.setattr(system_health_service, "REDIS_READ_TIMEOUT_SECONDS", 0.2)
        started = time.monotonic()
        state = system_health_service.read_worker_state()
        assert state.available is False
        assert time.monotonic() - started < 5

    def test_a_pool_that_cannot_connect_is_unknown(self, monkeypatch):
        async def refuse(_settings):
            raise ConnectionRefusedError("synthetic")

        monkeypatch.setattr("arq.create_pool", refuse)
        assert system_health_service.read_worker_state() == WorkerState(available=False)

    def test_the_cap_is_a_few_seconds(self):
        assert 0 < system_health_service.REDIS_READ_TIMEOUT_SECONDS <= 5


# --------------------------------------------------------------------------- shop time follows DST


@pytest.mark.unit
class TestShopTimeFollowsDaylightSaving:
    FIELDS = {"month": None, "day": None, "weekday": None, "hour": 7, "minute": 0, "second": 0}

    def test_seven_am_is_noon_utc_in_summer_and_one_pm_in_winter(self):
        summer = next_run(self.FIELDS, SHOP_TIMEZONE_NAME, datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc))
        winter = next_run(self.FIELDS, SHOP_TIMEZONE_NAME, datetime(2026, 12, 9, 10, 0, tzinfo=timezone.utc))
        assert summer.astimezone(timezone.utc).replace(microsecond=0) == datetime(
            2026, 7, 9, 12, 0, tzinfo=timezone.utc
        )
        assert winter.astimezone(timezone.utc).replace(microsecond=0) == datetime(
            2026, 12, 9, 13, 0, tzinfo=timezone.utc
        )

    def test_the_worker_zone_is_a_named_zone_not_a_fixed_offset(self):
        from app.worker import WorkerSettings

        assert getattr(WorkerSettings.timezone, "key", None) == SHOP_TIMEZONE_NAME

    def test_maintenance_runs_at_six_fifteen_shop_time_not_utc(self):
        from app.worker import ALL_CRON_JOBS

        (job,) = [job for job in ALL_CRON_JOBS if job.name == "cron:check_maintenance_due_job"]
        nxt = next_run(
            {"month": None, "day": None, "weekday": None, "hour": job.hour, "minute": job.minute, "second": 0},
            SHOP_TIMEZONE_NAME,
            datetime(2026, 12, 9, 6, 0, tzinfo=timezone.utc),  # 00:00 CST
        )
        # 06:15 CST = 12:15 UTC in winter (it used to be 12:15 UTC = 06:15 CST only by accident of TZ).
        assert nxt.astimezone(timezone.utc).replace(microsecond=0) == datetime(2026, 12, 9, 12, 15, tzinfo=timezone.utc)

    def test_registry_reports_the_shop_zone_and_every_cron_once(self):
        from app.worker import ALL_CRON_JOBS

        registry = build_cron_registry(ALL_CRON_JOBS, [])
        names = [entry["name"] for entry in registry["jobs"]]
        assert registry["timezone"] == SHOP_TIMEZONE_NAME
        assert len(names) == len(set(names))
        assert not any(entry["armed"] for entry in registry["jobs"])

    def test_unusual_shapes_fall_back_to_raw_fields(self):
        assert describe_schedule({"minute": 15}) == "Hourly at :15"
        assert describe_schedule({"month": 3, "day": 1, "hour": 1, "minute": 0}).startswith("{")


# --------------------------------------------------------------------------- malformed Redis values


@pytest.mark.unit
class TestMalformedRedisValuesDegrade:
    def test_bad_heartbeat_and_registry_entries_never_raise(self, db_session):
        state = WorkerState(
            available=True,
            heartbeats=[{"observed_at": "not a time", "release": 7}],
            registry={"timezone": 5, "cron_spec": ["x"], "published_at": "nope", "jobs": ["x", {"name": 3}]},
            last_runs={"orphan_job": {"outcome": "exploded"}, "other_job": {"outcome": "success", "counts": "x"}},
        )
        response = build_system_health(db_session, 1, state)
        assert response.worker.status == "not_seen"
        assert response.schedule_timezone is None and response.cron_spec is None
        assert response.schedule_published_at is None
        jobs = {job.name: job for job in response.cron_jobs}
        assert set(jobs) == {"orphan_job", "other_job"}
        assert jobs["orphan_job"].last_run is None  # an unknown outcome is not shown as a result
        assert jobs["other_job"].last_run.counts == {}

    def test_newest_heartbeat_wins_and_instances_are_counted(self, db_session):
        now = datetime(2026, 12, 9, 13, 0, tzinfo=timezone.utc)
        state = WorkerState(
            available=True,
            heartbeats=[
                {"release": "a" * 40, "observed_at": (now - timedelta(seconds=60)).isoformat()},
                {"release": "b" * 40, "observed_at": (now - timedelta(seconds=5)).isoformat()},
            ],
        )
        worker = build_system_health(db_session, 1, state, now=now).worker
        assert (worker.status, worker.release, worker.instance_count) == ("alive", "b" * 40, 2)
        assert worker.heartbeat_age_seconds == 5.0

    def test_an_armed_job_with_a_live_worker_but_no_zone_has_no_next_run(self, db_session):
        now = datetime(2026, 12, 9, 13, 0, tzinfo=timezone.utc)
        state = WorkerState(
            available=True,
            heartbeats=[{"observed_at": now.isoformat()}],
            registry={"jobs": [{"name": "relay_pending_notifications_job", "armed": True, "minute": [0, 30]}]},
        )
        (job,) = build_system_health(db_session, 1, state, now=now).cron_jobs
        assert job.armed is True
        assert job.next_run_at is None


# --------------------------------------------------------------------------- credentials


def _issue(client, admin_headers, user, **extra):
    response = client.post(
        API_TOKENS_URL, json={"user_id": user.id, "label": "health bot", **extra}, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def _redis_down(monkeypatch):
    monkeypatch.setattr("app.api.endpoints.system_health.read_worker_state", lambda: WorkerState(available=False))


@pytest.mark.requires_db
@pytest.mark.api
@pytest.mark.usefixtures("_redis_down")
class TestCredentials:
    def test_a_full_admin_api_token_reads_it(self, client, admin_headers, admin_user):
        headers = _issue(client, admin_headers, admin_user, scope="full", ai_external=False)
        response = client.get(URL, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["worker"]["status"] == "unknown"

    def test_a_read_only_token_is_refused_admin_settings_is_a_denied_tag(self, client, admin_headers, admin_user):
        headers = _issue(client, admin_headers, admin_user, scope="read_only", ai_external=False)
        assert client.get(URL, headers=headers).status_code == 403

    def test_an_ai_external_token_is_refused(self, client, admin_headers, admin_user):
        headers = _issue(client, admin_headers, admin_user, scope="read_only", ai_external=True)
        assert client.get(URL, headers=headers).status_code == 403

    def test_a_viewer_is_refused(self, client, db_session):
        viewer = User(
            email="viewer@example.test",
            employee_id="EMP-VIEW-SH",
            first_name="Vera",
            last_name="Viewer",
            hashed_password="x",
            role=UserRole.VIEWER,
            is_active=True,
            company_id=1,
        )
        db_session.add(viewer)
        db_session.commit()
        token = create_access_token(subject=viewer.id, company_id=1)
        headers = {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}
        assert client.get(URL, headers=headers).status_code == 403

    def test_unauthenticated_is_refused(self, client):
        assert client.get(URL).status_code == 401


# --------------------------------------------------------------------------- a pure read; backlog windows


@pytest.mark.requires_db
@pytest.mark.api
@pytest.mark.usefixtures("_redis_down")
class TestPureReadAndBacklogWindows:
    def test_reading_writes_no_audit_row_and_changes_no_backlog_row(
        self, client, admin_headers, admin_user, db_session
    ):
        cataloged = next(iter(SOURCE_EVENT_TYPE_TO_KEY))
        now = datetime.utcnow()
        db_session.add_all(
            [
                OperationalEvent(
                    company_id=1, event_type=cataloged, source_module="test", created_at=now - timedelta(minutes=10)
                ),
                DigestQueue(company_id=1, user_id=admin_user.id, event_type="x", event_data={}),
            ]
        )
        db_session.commit()

        def snapshot():
            db_session.expire_all()
            return (
                db_session.query(AuditLog).count(),
                db_session.query(OperationalEvent).filter(OperationalEvent.notified_at.is_(None)).count(),
                db_session.query(DigestQueue).filter(DigestQueue.processed == False).count(),  # noqa: E712
                db_session.query(NotificationLog).count(),
            )

        before = snapshot()
        for _ in range(2):
            assert client.get(URL, headers=admin_headers).status_code == 200
        assert snapshot() == before

    def test_backlog_windows(self, client, admin_headers, admin_user, db_session):
        cataloged = next(iter(SOURCE_EVENT_TYPE_TO_KEY))
        uncataloged = "system_health_test_uncataloged_event"
        assert uncataloged not in SOURCE_EVENT_TYPE_TO_KEY
        now = datetime.utcnow()

        def event(event_type, age):
            return OperationalEvent(company_id=1, event_type=event_type, source_module="test", created_at=now - age)

        def log(**fields):
            return NotificationLog(company_id=1, user_id=admin_user.id, event_type="x", channel="email", **fields)

        db_session.add_all(
            [
                event(uncataloged, timedelta(minutes=10)),  # never relayed: not backlog
                event(cataloged, timedelta(days=40)),  # past the 30-day stranded window
                event(cataloged, timedelta(days=3)),  # stranded
                log(sent=False, sent_at=now - timedelta(days=1)),  # failed
                log(sent=False, sent_at=now - timedelta(days=9)),  # failed but outside 7 days
                log(sent=False, provider_status="sending", sent_at=now - timedelta(minutes=30)),  # stuck: failed
                log(sent=False, provider_status="sending", sent_at=now - timedelta(minutes=2)),  # in flight
                log(sent=False, provider_status="retrying", sent_at=now - timedelta(hours=1)),  # in flight
                log(sent=False, provider_status="bounced", sent_at=now - timedelta(hours=1)),  # failed
            ]
        )
        db_session.commit()
        backlog = client.get(URL, headers=admin_headers).json()["backlog"]
        assert backlog["unnotified_events"] == 0
        assert backlog["unnotified_events_stranded"] == 1
        assert backlog["failed_emails"] == 3


# --------------------------------------------------------------------------- the shared failed-delivery rule


@pytest.mark.unit
class TestFailedDeliveryClause:
    def test_compiles_to_the_delivery_logs_rule_on_postgres(self):
        sql = str(
            failed_delivery_clause(datetime(2026, 12, 9, 12, 0)).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "notification_logs.sent = false" in sql
        assert "notification_logs.provider_status IS NULL" in sql
        assert "NOT IN ('queued', 'sending', 'retrying')" in sql
        assert "notification_logs.provider_status = 'sending'" in sql
        assert "'2026-12-09 11:50:00'" in sql  # ten minutes before "now"


@pytest.mark.requires_db
@pytest.mark.api
class TestNotificationLogsFailedFilterUnchanged:
    def test_status_failed_lists_exactly_the_failed_rows(self, client, admin_headers, admin_user, db_session):
        now = datetime.utcnow()
        rows = {
            "plain": dict(sent=False),
            "bounced": dict(sent=False, provider_status="bounced"),
            "stuck": dict(sent=False, provider_status="sending", sent_at=now - timedelta(minutes=30)),
            "sending": dict(sent=False, provider_status="sending", sent_at=now - timedelta(minutes=1)),
            "queued": dict(sent=False, provider_status="queued"),
            "sent": dict(sent=True),
        }
        for label, fields in rows.items():
            db_session.add(
                NotificationLog(company_id=1, user_id=admin_user.id, event_type=label, channel="email", **fields)
            )
        db_session.commit()
        response = client.get("/api/v1/notifications/logs", params={"status": "failed"}, headers=admin_headers)
        assert response.status_code == 200, response.text
        assert {row["event_type"] for row in response.json()} == {"plain", "bounced", "stuck"}
