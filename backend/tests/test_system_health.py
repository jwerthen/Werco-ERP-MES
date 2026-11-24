"""System Health: the worker's cron registry + last-run record, and the read-only Admin endpoint.

What is pinned here:
* the worker schedules in SHOP time (America/Chicago), keeps each cron's intended local hour, and
  publishes its registry in a key SEPARATE from the pinned heartbeat;
* the one ``after_job_end`` hook records cron runs only, from arq's own result record, and never raises;
* ``GET /admin/system-health`` is Admin-only, tenant-scoped, a pure read, and renders an
  unreachable Redis as "unknown" -- never a 500.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.core import worker_schedule
from app.core.worker_schedule import (
    CRON_LAST_RUN_KEY,
    CRON_REGISTRY_KEY,
    SHOP_TIMEZONE_NAME,
    build_cron_registry,
    build_run_record,
    describe_schedule,
    next_run,
    record_cron_run,
    summarize_counts,
)
from app.models.company import Company
from app.models.notification import DigestQueue, NotificationLog
from app.models.operational_event import OperationalEvent
from app.services import system_health_service
from app.services.notification_catalog import SOURCE_EVENT_TYPE_TO_KEY
from app.services.system_health_service import WorkerState

CHICAGO = ZoneInfo("America/Chicago")


# --------------------------------------------------------------------------- the worker's schedule


class TestShopTimeSchedule:
    def test_worker_settings_pin_the_shop_zone_and_the_after_job_hook(self):
        from app.worker import WorkerSettings

        assert WorkerSettings.timezone == CHICAGO
        assert WorkerSettings.after_job_end is record_cron_run
        assert WorkerSettings.on_job_start is worker_schedule.record_cron_start

    def test_each_cron_keeps_its_intended_local_hour(self):
        from app.worker import ALL_CRON_JOBS

        hours = {job.name.removeprefix("cron:"): (job.hour, job.minute) for job in ALL_CRON_JOBS}
        # Spot-check the daily alerts and the moved one: maintenance ran 12:15 UTC = 6:15 AM Central.
        assert hours["check_maintenance_due_job"] == (6, 15)
        assert hours["check_calibrations_job"] == (7, 0)
        assert hours["run_oee_auto_calc_job"] == (2, 30)

    def test_startup_log_describes_times_in_the_shop_zone(self):
        from app.worker import ALL_CRON_JOBS, describe_cron_schedule

        line = describe_cron_schedule(ALL_CRON_JOBS[:1])[0]
        parsed = datetime.fromisoformat(line.split("next run ")[1])
        assert parsed.utcoffset() in (timedelta(hours=-5), timedelta(hours=-6))  # CDT / CST

    def test_every_cron_shape_in_the_table_has_a_readable_schedule(self):
        from app.worker import ALL_CRON_JOBS

        registry = build_cron_registry(ALL_CRON_JOBS, ALL_CRON_JOBS)
        texts = {entry["name"]: describe_schedule(entry) for entry in registry["jobs"]}
        assert texts["check_calibrations_job"] == "Daily 07:00"
        assert texts["relay_pending_notifications_job"] == "Every 5 min"
        assert texts["poll_tracking_job"] == "Every 30 min"
        assert texts["cleanup_old_logs_job"] == "Mon 02:00"
        assert texts["archive_aged_audit_logs_job"] == "Day 1 of month 03:00"
        assert all(not text.startswith("{") for text in texts.values())


class TestCronRegistry:
    def test_registry_lists_every_cron_with_its_armed_state(self):
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        registered = select_cron_jobs("relay_pending_notifications_job")
        registry = build_cron_registry(ALL_CRON_JOBS, registered, cron_spec="relay_pending_notifications_job")
        assert registry["timezone"] == SHOP_TIMEZONE_NAME
        assert registry["cron_spec"] == "relay_pending_notifications_job"
        armed = {entry["name"]: entry["armed"] for entry in registry["jobs"]}
        assert len(armed) == len({job.name for job in ALL_CRON_JOBS})
        assert armed.pop("relay_pending_notifications_job") is True
        assert not any(armed.values())
        json.dumps(registry)  # JSON-safe (arq schedules use sets)

    def test_publish_writes_a_separate_key_and_never_raises(self, monkeypatch):
        from app.worker import ALL_CRON_JOBS

        monkeypatch.setenv("WORKER_CRON_JOBS", "none")
        redis = AsyncMock()
        assert asyncio.run(worker_schedule.publish_cron_registry(redis, ALL_CRON_JOBS, [])) is True
        key, payload = redis.set.await_args.args
        assert key == CRON_REGISTRY_KEY and not key.startswith("werco:worker:runtime:")
        assert redis.set.await_args.kwargs == {}  # no expiry: last-known registry
        assert json.loads(payload)["cron_spec"] == "none"

        redis.set.side_effect = ConnectionError("synthetic outage")
        assert asyncio.run(worker_schedule.publish_cron_registry(redis, ALL_CRON_JOBS, [])) is False
        assert asyncio.run(worker_schedule.publish_cron_registry(None, ALL_CRON_JOBS, [])) is False

    def test_startup_publishes_the_registry_after_the_heartbeat(self, monkeypatch):
        import app.worker as worker

        redis = AsyncMock()
        ctx = {"redis": redis}

        async def run():
            await worker.startup(ctx)
            await worker.shutdown(ctx)

        asyncio.run(run())
        keys = [call.args[0] for call in redis.set.await_args_list]
        assert keys[0].startswith("werco:worker:runtime:")
        assert CRON_REGISTRY_KEY in keys

    def test_next_run_is_computed_in_the_published_zone(self):
        fields = {"month": None, "day": None, "weekday": None, "hour": 7, "minute": 0, "second": 0}
        # 2026-12-09 12:00 UTC = 06:00 CST -> next 07:00 CST is 13:00 UTC the same day.
        nxt = next_run(fields, SHOP_TIMEZONE_NAME, datetime(2026, 12, 9, 12, 0, tzinfo=timezone.utc))
        assert nxt is not None
        assert nxt.astimezone(timezone.utc).replace(microsecond=0) == datetime(2026, 12, 9, 13, 0, tzinfo=timezone.utc)
        assert next_run(fields, "Not/AZone") is None


# --------------------------------------------------------------------------- the after-job hook


def _result(success=True, result=None, function="cron:check_calibrations_job"):
    return SimpleNamespace(
        job_id="cron:check_calibrations_job:1700000000000",
        function=function,
        success=success,
        result=result,
        start_time=datetime(2026, 12, 9, 13, 0, tzinfo=timezone.utc),
        finish_time=datetime(2026, 12, 9, 13, 0, 5, tzinfo=timezone.utc),
    )


class TestLastRunHook:
    def test_counts_keep_only_flat_numbers(self):
        counts = summarize_counts({"due": 2, "ok": True, "rate": 0.5, "nested": {"x": 1}, "label": "a", 3: 4})
        assert counts == {"due": 2, "ok": True, "rate": 0.5}
        assert summarize_counts(["not", "a", "dict"]) == {}
        assert summarize_counts({"nan": float("nan")}) == {}

    def test_a_failure_records_only_the_exception_class(self):
        record = build_run_record(_result(success=False, result=ValueError("private detail")))
        assert record["outcome"] == "failed"
        assert record["error_type"] == "ValueError"
        assert "private detail" not in json.dumps(record)
        assert record["counts"] == {}

    def test_hook_records_a_cron_run_from_arqs_result(self, monkeypatch):
        redis = AsyncMock()
        info = _result(result={"due": 3})

        class FakeJob:
            def __init__(self, job_id, redis_):
                assert job_id == info.job_id

            async def result_info(self):
                return info

        monkeypatch.setattr("arq.jobs.Job", FakeJob)
        asyncio.run(record_cron_run({"job_id": info.job_id, "redis": redis}))
        key, name, payload = redis.hset.await_args.args
        assert (key, name) == (CRON_LAST_RUN_KEY, "check_calibrations_job")
        stored = json.loads(payload)
        assert stored["outcome"] == "success" and stored["counts"] == {"due": 3}
        assert stored["started_at"].startswith("2026-12-09T13:00:00")

    def test_hook_ignores_non_cron_jobs_and_never_raises(self, monkeypatch):
        redis = AsyncMock()
        asyncio.run(record_cron_run({"job_id": "a1b2c3", "redis": redis}))  # an enqueued email
        asyncio.run(record_cron_run({"job_id": "cron:x:1", "redis": None}))
        redis.hset.assert_not_awaited()

        class Broken:
            def __init__(self, *_):
                pass

            async def result_info(self):
                raise ConnectionError("synthetic outage")

        monkeypatch.setattr("arq.jobs.Job", Broken)
        asyncio.run(record_cron_run({"job_id": "cron:x:1", "redis": redis}))  # no exception


# --------------------------------------------------------------------------- the endpoint

URL = "/api/v1/admin/system-health"


def _alive_state(now: datetime) -> WorkerState:
    from app.worker import ALL_CRON_JOBS, select_cron_jobs

    registry = build_cron_registry(ALL_CRON_JOBS, select_cron_jobs("check_calibrations_job"), cron_spec="x")
    return WorkerState(
        available=True,
        heartbeats=[{"release": "a" * 40, "observed_at": (now - timedelta(seconds=12)).isoformat()}],
        registry=registry,
        last_runs={"check_calibrations_job": build_run_record(_result(result={"due": 1}))},
    )


@pytest.mark.requires_db
class TestSystemHealthEndpoint:
    def test_admin_sees_worker_crons_and_last_run(self, client, admin_headers, monkeypatch):
        now = datetime.now(timezone.utc)
        monkeypatch.setattr("app.api.endpoints.system_health.read_worker_state", lambda: _alive_state(now))
        response = client.get(URL, headers=admin_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["redis_available"] is True
        assert body["worker"]["status"] == "alive"
        assert body["worker"]["release"] == "a" * 40
        assert body["schedule_timezone"] == SHOP_TIMEZONE_NAME
        jobs = {job["name"]: job for job in body["cron_jobs"]}
        calib = jobs["check_calibrations_job"]
        assert calib["armed"] is True and calib["schedule"] == "Daily 07:00"
        assert calib["next_run_at"].endswith("Z")
        assert calib["last_run"]["outcome"] == "success" and calib["last_run"]["counts"] == {"due": 1}
        assert jobs["run_mrp_review_job"]["armed"] is False
        assert jobs["run_mrp_review_job"]["next_run_at"] is None

    @pytest.mark.parametrize("headers_fixture", ["manager_headers", "supervisor_headers", "operator_headers"])
    def test_non_admins_are_refused(self, client, request, headers_fixture, monkeypatch):
        monkeypatch.setattr(
            "app.api.endpoints.system_health.read_worker_state",
            lambda: pytest.fail("a refused caller must not reach Redis"),
        )
        assert client.get(URL, headers=request.getfixturevalue(headers_fixture)).status_code == 403

    def test_unreachable_redis_is_unknown_not_an_error(self, client, admin_headers, monkeypatch):
        async def outage():
            raise ConnectionError("synthetic Redis outage")

        monkeypatch.setattr(system_health_service, "_read_from_redis", outage)
        response = client.get(URL, headers=admin_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["redis_available"] is False
        assert body["worker"]["status"] == "unknown"
        assert body["cron_jobs"] == []
        assert body["backlog"]["unnotified_events"] == 0

    def test_no_heartbeat_means_not_seen_and_armed_unknown(self, client, admin_headers, monkeypatch):
        now = datetime.now(timezone.utc)
        state = _alive_state(now)
        state.heartbeats = []
        monkeypatch.setattr("app.api.endpoints.system_health.read_worker_state", lambda: state)
        body = client.get(URL, headers=admin_headers).json()
        assert body["worker"]["status"] == "not_seen"
        assert all(job["armed"] is None and job["next_run_at"] is None for job in body["cron_jobs"])
        # The last run is still evidence, shown even while no worker is up.
        jobs = {job["name"]: job for job in body["cron_jobs"]}
        assert jobs["check_calibrations_job"]["last_run"]["outcome"] == "success"

    def test_backlog_counts_only_the_active_company(self, client, admin_headers, admin_user, db_session, monkeypatch):
        monkeypatch.setattr("app.api.endpoints.system_health.read_worker_state", lambda: WorkerState(available=False))
        db_session.add(Company(id=2, name="Fictional Fabrication Co", slug="fictional-fab", is_active=True))
        db_session.flush()
        cataloged = next(iter(SOURCE_EVENT_TYPE_TO_KEY))
        now = datetime.utcnow()
        for company_id in (1, 2):
            db_session.add_all(
                [
                    # waiting for the sweeper
                    OperationalEvent(
                        company_id=company_id,
                        event_type=cataloged,
                        source_module="test",
                        created_at=now - timedelta(minutes=10),
                    ),
                    # too fresh: the after-commit enqueue may still be in flight
                    OperationalEvent(company_id=company_id, event_type=cataloged, source_module="test", created_at=now),
                    # past the sweeper's 24 h floor: stranded
                    OperationalEvent(
                        company_id=company_id,
                        event_type=cataloged,
                        source_module="test",
                        created_at=now - timedelta(days=2),
                    ),
                    # already notified
                    OperationalEvent(
                        company_id=company_id,
                        event_type=cataloged,
                        source_module="test",
                        created_at=now - timedelta(minutes=10),
                        notified_at=now,
                    ),
                    DigestQueue(company_id=company_id, user_id=admin_user.id, event_type="x", event_data={}),
                    DigestQueue(
                        company_id=company_id, user_id=admin_user.id, event_type="x", event_data={}, processed=True
                    ),
                    NotificationLog(
                        company_id=company_id, user_id=admin_user.id, event_type="x", channel="email", sent=False
                    ),
                    NotificationLog(
                        company_id=company_id,
                        user_id=admin_user.id,
                        event_type="x",
                        channel="email",
                        sent=False,
                        provider_status="queued",
                    ),
                    NotificationLog(
                        company_id=company_id, user_id=admin_user.id, event_type="x", channel="sms", sent=False
                    ),
                    NotificationLog(
                        company_id=company_id, user_id=admin_user.id, event_type="x", channel="email", sent=True
                    ),
                ]
            )
        db_session.commit()
        before = db_session.query(OperationalEvent).filter(OperationalEvent.notified_at.is_(None)).count()

        backlog = client.get(URL, headers=admin_headers).json()["backlog"]
        assert backlog == {
            "unnotified_events": 1,
            "unnotified_events_stranded": 1,
            "queued_digest_items": 1,
            "failed_emails": 1,
            "failed_email_window_days": 7,
        }
        # A pure read: nothing was marked notified or processed.
        db_session.expire_all()
        assert db_session.query(OperationalEvent).filter(OperationalEvent.notified_at.is_(None)).count() == before


class TestRedisRead:
    def test_reads_heartbeats_registry_and_last_runs_and_closes_the_pool(self, monkeypatch):
        closed = []
        store = {
            b"werco:worker:runtime:one": json.dumps({"release": "b" * 40, "observed_at": "2026-12-09T13:00:00+00:00"}),
            CRON_REGISTRY_KEY.encode(): json.dumps({"timezone": SHOP_TIMEZONE_NAME, "jobs": []}),
        }

        class FakePool:
            async def scan_iter(self, match, count):
                assert match == "werco:worker:runtime:*"
                yield b"werco:worker:runtime:one"

            async def get(self, key):
                key = key.encode() if isinstance(key, str) else key
                return store.get(key)

            async def hgetall(self, key):
                assert key == CRON_LAST_RUN_KEY
                return {b"check_calibrations_job": b'{"outcome": "success"}', b"bad": b"{not json"}

            async def aclose(self, close_connection_pool):
                closed.append(close_connection_pool)

        async def fake_create_pool(_settings):
            return FakePool()

        monkeypatch.setattr("arq.create_pool", fake_create_pool)
        state = system_health_service.read_worker_state()
        assert state.available is True
        assert state.heartbeats[0]["release"] == "b" * 40
        assert state.registry == {"timezone": SHOP_TIMEZONE_NAME, "jobs": []}
        assert state.last_runs == {"check_calibrations_job": {"outcome": "success"}}
        assert closed == [True]
