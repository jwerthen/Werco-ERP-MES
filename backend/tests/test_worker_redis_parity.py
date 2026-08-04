"""Regression guard for the defect that made the whole background-job system dead code.

THE BUG (fixed 2026-08). ``app/core/queue.py`` resolved ARQ's Redis from ``REDIS_HOST`` /
``REDIS_PORT`` / ``REDIS_DB`` and never read ``REDIS_URL``. Every OTHER Redis consumer in
the backend -- the response cache, the slowapi limiter storage, the login throttle, the
``/health/ready`` probe -- reads ``REDIS_URL``, and both ``docs/ENVIRONMENT_VARIABLES.md``
and ``backend/.env.example`` promised operators that ``REDIS_URL`` "takes precedence over
individual settings".

So on any deployment provisioned the way the docs describe (Railway hands you one
``redis://default:PASS@host:6379`` URL and nothing else), the queue resolved to
``localhost:6379``. Every enqueue failed with ConnectionRefused. Nothing was queued, no
cron ever fired, and ``/health/ready`` reported ``redis: healthy`` throughout, because
it pinged ``REDIS_URL``. The settings object also had no ``password`` field at all, so even
a correct ``REDIS_HOST`` could not authenticate against a managed Redis.

What these tests lock down:

* the enqueue side and the worker side resolve the SAME target from the SAME settings --
  this is the parity property; if they can ever disagree the system is silently broken;
* ``REDIS_URL`` is honored, credentials and TLS included;
* the host/port/db fallback still works, so an existing correct deployment is unaffected;
* a deployed process with no Redis configured fails LOUDLY instead of idling;
* the password is never in anything we log.
"""

import importlib
from typing import Any

import pytest

pytestmark = [pytest.mark.unit]


_PATCHED_SETTINGS = ("REDIS_URL", "REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD", "ENVIRONMENT")


@pytest.fixture(autouse=True)
def _restore_worker_module():
    """Reload ``app.worker`` after every test in this file, against the REAL settings.

    ``WorkerSettings.redis_settings`` is captured at class-creation time, so a test that
    reloads the module under patched settings would otherwise leave a stale value behind for
    whatever runs next. The values are snapshotted and restored here rather than relying on
    ``monkeypatch`` having already unwound -- fixture teardown order does not guarantee that,
    and getting it wrong makes the reload raise (a patched ENVIRONMENT=production reaches
    ``assert_redis_configured``).
    """
    from app.core.config import settings

    saved = {name: getattr(settings, name) for name in _PATCHED_SETTINGS}
    yield
    for name, value in saved.items():
        setattr(settings, name, value)
    import app.worker

    importlib.reload(app.worker)


def _reload_queue(monkeypatch: pytest.MonkeyPatch, **overrides: Any):
    """Apply settings overrides and return a freshly-resolved queue module.

    ``get_redis_settings`` reads ``settings`` at call time, so a reload is not strictly
    required -- but reloading is what proves the resolution is not frozen at import, which
    is exactly how ``WorkerSettings.redis_settings`` captures it.
    """
    import app.core.queue as queue
    from app.core.config import settings

    for name in ("REDIS_URL", "REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD", "ENVIRONMENT"):
        if name in overrides:
            monkeypatch.setattr(settings, name, overrides[name])
    return queue


class TestEnqueueAndWorkerAgree:
    """The parity property. This is the test that stops the root cause recurring."""

    def test_worker_settings_use_the_same_resolver_as_the_enqueue_side(self):
        """``WorkerSettings.redis_settings`` must come from ``get_redis_settings()``.

        If someone ever gives the worker its own resolution (a second ``os.getenv`` block,
        a different env var, a hard-coded host), the API can enqueue to one Redis while the
        worker drains another and every symptom is silence.
        """
        from app.core.queue import get_redis_settings
        from app.worker import WorkerSettings

        assert WorkerSettings.redis_settings == get_redis_settings()

    @pytest.mark.parametrize(
        "overrides",
        [
            {"REDIS_URL": "redis://default:s3cret@redis.railway.internal:6379/0", "REDIS_HOST": "localhost"},
            {"REDIS_URL": None, "REDIS_HOST": "redis", "REDIS_PORT": 6380, "REDIS_DB": 3},
            {"REDIS_URL": "rediss://user:pw@secure.example.com:6380/2", "REDIS_HOST": "localhost"},
            {"REDIS_URL": None, "REDIS_HOST": "redis", "REDIS_PASSWORD": "compose-secret"},
        ],
    )
    def test_both_sides_resolve_identically_for_any_configuration(
        self, monkeypatch: pytest.MonkeyPatch, overrides: dict
    ):
        """Whatever the operator sets, the two sides must land on the same connection."""
        queue = _reload_queue(monkeypatch, **overrides)
        import app.worker as worker

        worker = importlib.reload(worker)
        assert worker.WorkerSettings.redis_settings == queue.get_redis_settings()

    def test_reloaded_worker_carries_the_url_credentials(self, monkeypatch: pytest.MonkeyPatch):
        """The exact shape Railway hands you: one URL with a password, nothing else set."""
        queue = _reload_queue(
            monkeypatch,
            REDIS_URL="redis://default:s3cret@redis.railway.internal:6379/0",
            REDIS_HOST="localhost",
        )
        import app.worker as worker

        worker = importlib.reload(worker)
        resolved = worker.WorkerSettings.redis_settings
        assert resolved.host == "redis.railway.internal"
        assert resolved.password == "s3cret"
        assert resolved.database == 0
        # ...and the enqueue side agrees, which is the whole point.
        assert queue.get_redis_settings().host == "redis.railway.internal"


class TestRedisUrlIsHonored:
    def test_url_wins_over_the_individual_settings(self, monkeypatch: pytest.MonkeyPatch):
        queue = _reload_queue(
            monkeypatch,
            REDIS_URL="redis://cache.internal:6390/4",
            REDIS_HOST="somewhere-else",
            REDIS_PORT=6379,
            REDIS_DB=0,
        )
        target = queue.resolve_redis_target()
        assert (target.host, target.port, target.db) == ("cache.internal", 6390, 4)
        assert target.source == queue.SOURCE_URL

    def test_tls_scheme_is_carried_through(self, monkeypatch: pytest.MonkeyPatch):
        queue = _reload_queue(monkeypatch, REDIS_URL="rediss://user:pw@secure.example.com:6380/2")
        assert queue.get_redis_settings().ssl is True

    def test_a_malformed_url_fails_loudly_instead_of_falling_back(self, monkeypatch: pytest.MonkeyPatch):
        """Silently falling back to localhost on a typo is how this class of bug hides."""
        queue = _reload_queue(monkeypatch, REDIS_URL="http://not-redis:6379")
        with pytest.raises(queue.RedisConfigurationError):
            queue.get_redis_settings()


class TestBackwardCompatibility:
    """An existing correct deployment must not change behavior."""

    def test_host_port_db_still_work_when_no_url_is_set(self, monkeypatch: pytest.MonkeyPatch):
        """This is the docker-compose worker's configuration, verbatim."""
        queue = _reload_queue(monkeypatch, REDIS_URL=None, REDIS_HOST="redis", REDIS_PORT=6379, REDIS_DB=0)
        target = queue.resolve_redis_target()
        assert (target.host, target.port, target.db) == ("redis", 6379, 0)
        assert target.source == queue.SOURCE_PARTS

    def test_redis_password_fills_the_gap_the_trio_cannot_express(self, monkeypatch: pytest.MonkeyPatch):
        """docker-compose passes REDIS_PASSWORD to the worker and runs redis with
        ``--requirepass``, but nothing read it -- so the compose worker could not
        authenticate either."""
        queue = _reload_queue(monkeypatch, REDIS_URL=None, REDIS_HOST="redis", REDIS_PASSWORD="compose-secret")
        assert queue.get_redis_settings().password == "compose-secret"

    def test_a_password_in_the_url_wins_over_redis_password(self, monkeypatch: pytest.MonkeyPatch):
        queue = _reload_queue(
            monkeypatch,
            REDIS_URL="redis://default:from-url@host:6379/0",
            REDIS_PASSWORD="from-env",
        )
        assert queue.get_redis_settings().password == "from-url"

    def test_fast_fail_still_collapses_the_retry_budget(self, monkeypatch: pytest.MonkeyPatch):
        """The outbox's after-commit enqueue must not stall a committing request for ~5s."""
        queue = _reload_queue(monkeypatch, REDIS_URL="redis://default:pw@host:6379/0")
        fast = queue.get_redis_settings(fast_fail=True)
        assert fast.conn_retries == 0
        assert fast.conn_timeout == 1
        # ...and it must not lose the credentials while doing so.
        assert fast.password == "pw"
        assert queue.get_redis_settings().conn_retries == 5


class TestMisconfigurationIsLoud:
    def test_a_deployed_worker_with_no_redis_refuses_to_start(self, monkeypatch: pytest.MonkeyPatch):
        """ "Started fine, consumed nothing" is the failure this whole change exists to kill."""
        queue = _reload_queue(monkeypatch, REDIS_URL=None, REDIS_HOST="localhost", ENVIRONMENT="production")
        with pytest.raises(queue.RedisConfigurationError) as exc:
            queue.assert_redis_configured("arq worker")
        assert "REDIS_URL" in str(exc.value)

    def test_local_development_is_not_broken_by_the_guard(self, monkeypatch: pytest.MonkeyPatch):
        queue = _reload_queue(monkeypatch, REDIS_URL=None, REDIS_HOST="localhost", ENVIRONMENT="development")
        target = queue.assert_redis_configured("arq worker")
        assert target.is_configured is False

    def test_disagreeing_sources_are_warned_about(self, monkeypatch: pytest.MonkeyPatch):
        queue = _reload_queue(
            monkeypatch,
            REDIS_URL="redis://cache.internal:6379/0",
            REDIS_HOST="queue.internal",
            REDIS_PORT=6379,
            REDIS_DB=0,
        )
        warnings = queue.redis_config_warnings()
        assert warnings and "REDIS_URL wins" in warnings[0]

    def test_agreeing_sources_produce_no_noise(self, monkeypatch: pytest.MonkeyPatch):
        queue = _reload_queue(
            monkeypatch,
            REDIS_URL="redis://redis:6379/0",
            REDIS_HOST="redis",
            REDIS_PORT=6379,
            REDIS_DB=0,
        )
        assert queue.redis_config_warnings() == []

    def test_a_loopback_url_in_production_is_warned_about(self, monkeypatch: pytest.MonkeyPatch):
        """The one hole left in the fail-fast guard.

        ``is_configured`` is source-based, so ``REDIS_URL=redis://localhost:6379/0`` -- which
        is exactly what ``backend/.env.example`` ships -- reports "configured" and does NOT
        refuse to start. It is deliberately not fatal (a single-box self-host with Redis on
        localhost is legitimate), so this warning is the only thing standing between that
        value and a silently dead queue.
        """
        queue = _reload_queue(
            monkeypatch, REDIS_URL="redis://localhost:6379/0", REDIS_HOST="localhost", ENVIRONMENT="production"
        )
        assert queue.resolve_redis_target().is_configured is True  # the hole
        warnings = queue.redis_config_warnings()
        assert warnings and "LOOPBACK" in warnings[0]
        # ...and it must not raise: refusing here would break a legitimate single-box self-host.
        queue.assert_redis_configured("arq worker")

    def test_a_loopback_url_outside_production_is_silent(self, monkeypatch: pytest.MonkeyPatch):
        """Local dev sets exactly this value and must stay quiet."""
        queue = _reload_queue(
            monkeypatch, REDIS_URL="redis://localhost:6379/0", REDIS_HOST="localhost", ENVIRONMENT="development"
        )
        assert queue.redis_config_warnings() == []


class TestTheCredentialIsNeverLogged:
    def test_describe_omits_the_password(self, monkeypatch: pytest.MonkeyPatch):
        """``describe()`` goes to the startup log and to ``/health/ready``'s job_queue_redis."""
        queue = _reload_queue(monkeypatch, REDIS_URL="redis://default:sup3rs3cret@host.internal:6379/0")
        described = queue.resolve_redis_target().describe()
        assert "sup3rs3cret" not in described
        assert "host.internal" in described
        assert "auth=password" in described

    def test_log_redis_target_never_emits_the_password(self, monkeypatch: pytest.MonkeyPatch, caplog):
        queue = _reload_queue(monkeypatch, REDIS_URL="redis://default:sup3rs3cret@host.internal:6379/0")
        with caplog.at_level("INFO"):
            queue.log_redis_target("test")
        assert "sup3rs3cret" not in caplog.text
        assert "host.internal" in caplog.text
