"""The short-lived enqueue helpers must close their pool for real, and report a landed job as landed.

REGRESSION (prod, 2026-11-05). ``enqueue_job_best_effort`` and
``enqueue_job_fire_and_forget_fastfail`` cleaned up with ``pool.close(); await
pool.wait_closed()``. Under redis-py 5, ``ArqRedis`` is a ``redis.asyncio.Redis``. That class
has no ``wait_closed``, and its ``close`` is a deprecated coroutine alias. Every call
therefore:

* enqueued the job, then raised ``AttributeError`` in ``finally``;
* logged ``Failed to enqueue background job ...`` at ERROR (Sentry noise on every shop-floor
  completion) and returned ``False`` for a job that WAS in Redis;
* never closed the connection, because the ``close()`` coroutine was never awaited.

The only earlier test of the helper covered the failure branch, with ``create_pool`` patched
to raise, so the success path's cleanup never ran under test. A ``MagicMock``/``AsyncMock``
pool would not have caught this either: it answers ``wait_closed`` like any other attribute.

So these tests run the REAL ``arq.create_pool`` -> ``ArqRedis`` -> redis-py stack against
``_FakeRedisServer`` below: a few dozen lines of RESP2, just enough Redis for arq's enqueue
transaction. The client side, close path included, is library code. Nothing is mocked on it.
"""

from __future__ import annotations

import asyncio
import logging
import socketserver
import threading
from typing import Dict, List, Optional

import pytest
from arq.connections import ArqRedis
from arq.constants import default_queue_name, job_key_prefix
from arq.jobs import deserialize_job
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core import queue
from app.core.config import settings

pytestmark = [pytest.mark.unit]

_QUEUE_LOGGER = "app.core.queue"
_SETTLE_SECONDS = 5.0


# --------------------------------------------------------------------------- #
# Just enough Redis for arq's enqueue
# --------------------------------------------------------------------------- #
class _FakeRedisServer(socketserver.ThreadingTCPServer):
    """A RESP2 server on 127.0.0.1 covering arq's enqueue and redis-py's handshake.

    Handles PING, CLIENT SETINFO, AUTH, SELECT, WATCH/UNWATCH, EXISTS, MULTI, PSETEX, ZADD
    and EXEC. It keeps the keys and sorted sets it is given, so a test can read the job back
    out, and it counts connections the CLIENT closed (EOF), not ones that timed out.
    """

    daemon_threads = True
    # A leaked client connection would otherwise make server_close() join a handler thread
    # that is blocked reading forever.
    block_on_close = False
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _RespHandler)
        self.strings: Dict[bytes, bytes] = {}
        self.zsets: Dict[bytes, Dict[bytes, float]] = {}
        self.opened = 0
        self.closed_by_client = 0
        self.state = threading.Condition()
        self._thread = threading.Thread(target=self.serve_forever, name="fake-redis", daemon=True)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def wait_until_every_connection_is_closed(self, timeout: float) -> bool:
        with self.state:
            return self.state.wait_for(lambda: self.opened > 0 and self.closed_by_client == self.opened, timeout)

    def execute(self, cmd: List[bytes]) -> bytes:
        """Reply to one command, outside or inside EXEC. Callers hold ``self.state``."""
        name = cmd[0].upper()
        if name == b"PING":
            return b"+PONG\r\n"
        if name in (b"CLIENT", b"AUTH", b"SELECT", b"WATCH", b"UNWATCH", b"MULTI"):
            return b"+OK\r\n"
        if name == b"EXISTS":
            found = sum(1 for key in cmd[1:] if key in self.strings or key in self.zsets)
            return b":%d\r\n" % found
        if name == b"PSETEX":
            self.strings[cmd[1]] = cmd[3]
            return b"+OK\r\n"
        if name == b"ZADD":
            zset = self.zsets.setdefault(cmd[1], {})
            pairs = list(zip(cmd[2::2], cmd[3::2]))
            added = sum(1 for _score, member in pairs if member not in zset)
            for score, member in pairs:
                zset[member] = float(score)
            return b":%d\r\n" % added
        return b"-ERR unknown command '%s'\r\n" % name


class _RespHandler(socketserver.StreamRequestHandler):
    timeout = 30  # never hang the suite on a client that neither speaks nor hangs up

    server: _FakeRedisServer

    def _read_command(self) -> Optional[List[bytes]]:
        header = self.rfile.readline()
        if not header:
            return None  # EOF: the client closed its end
        assert header.startswith(b"*"), header
        args = []
        for _ in range(int(header[1:])):
            length = int(self.rfile.readline()[1:])
            args.append(self.rfile.read(length + 2)[:-2])
        return args

    def handle(self) -> None:
        server = self.server
        with server.state:
            server.opened += 1
        queued: Optional[List[List[bytes]]] = None
        while True:
            try:
                cmd = self._read_command()
            except OSError:
                return  # timed out; NOT counted as a client close
            if cmd is None:
                with server.state:
                    server.closed_by_client += 1
                    server.state.notify_all()
                return
            name = cmd[0].upper()
            with server.state:
                if name == b"MULTI":
                    queued = []
                    reply = b"+OK\r\n"
                elif name == b"EXEC":
                    replies = [server.execute(c) for c in (queued or [])]
                    queued = None
                    reply = b"*%d\r\n" % len(replies) + b"".join(replies)
                elif queued is not None:
                    queued.append(cmd)
                    reply = b"+QUEUED\r\n"
                else:
                    reply = server.execute(cmd)
            self.wfile.write(reply)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch):
    """Point the queue's Redis resolution at a fresh ``_FakeRedisServer``."""
    server = _FakeRedisServer()
    server.start()
    monkeypatch.setattr(settings, "REDIS_URL", f"redis://127.0.0.1:{server.port}/0")
    monkeypatch.setattr(settings, "REDIS_PASSWORD", None)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def pool_disconnects(monkeypatch: pytest.MonkeyPatch) -> List[ConnectionPool]:
    """Record every ``ConnectionPool.disconnect`` and then run the REAL one.

    This is a pass-through wrapper, not a stub. It is the deterministic half of "the pool was
    closed": a leaked socket is often closed later by garbage collection anyway, so seeing the
    server get EOF alone cannot tell an explicit close from a leak the GC cleaned up.
    """
    calls: List[ConnectionPool] = []
    real_disconnect = ConnectionPool.disconnect

    async def recording_disconnect(self: ConnectionPool, inuse_connections: bool = True) -> None:
        calls.append(self)
        await real_disconnect(self, inuse_connections)

    monkeypatch.setattr(ConnectionPool, "disconnect", recording_disconnect)
    return calls


def _landed_jobs(server: _FakeRedisServer) -> list:
    """Every job arq put on its default queue, deserialized from the stored job key."""
    with server.state:
        job_ids = list(server.zsets.get(default_queue_name.encode(), {}))
        payloads = [server.strings.get(job_key_prefix.encode() + job_id) for job_id in job_ids]
    assert all(payload is not None for payload in payloads), "queued an id with no job body"
    return [deserialize_job(payload) for payload in payloads if payload is not None]


def _queue_records(caplog: pytest.LogCaptureFixture, level: int) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _QUEUE_LOGGER and r.levelno >= level]


# --------------------------------------------------------------------------- #
# enqueue_job_best_effort (sync handlers)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fast_fail", [False, True], ids=["request-profile", "commit-path-fast-fail"])
def test_best_effort_enqueue_reports_a_landed_job_as_landed_and_closes_its_pool(
    fake_redis: _FakeRedisServer,
    pool_disconnects: List[ConnectionPool],
    caplog: pytest.LogCaptureFixture,
    fast_fail: bool,
):
    caplog.set_level(logging.DEBUG, logger=_QUEUE_LOGGER)

    result = queue.enqueue_job_best_effort("dispatch_notification_job", 7, event_id=1383, fast_fail=fast_fail)

    # The job is really in Redis...
    jobs = _landed_jobs(fake_redis)
    assert [(j.function, j.args, j.kwargs) for j in jobs] == [("dispatch_notification_job", (7,), {"event_id": 1383})]
    # ...so the caller is told it landed, and nothing claims it failed.
    assert result is True
    assert _queue_records(caplog, logging.WARNING) == [], "a landed job was logged as a failure"
    # The pool was closed explicitly and exactly once, and its socket actually went away.
    assert len(pool_disconnects) == 1, "the enqueue pool was never closed"
    assert fake_redis.wait_until_every_connection_is_closed(_SETTLE_SECONDS)


def test_a_failing_pool_close_does_not_turn_a_landed_job_into_a_failure(
    fake_redis: _FakeRedisServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Cleanup runs after the job is in Redis. Its failure must not decide the result."""
    caplog.set_level(logging.DEBUG, logger=_QUEUE_LOGGER)
    real_disconnect = ConnectionPool.disconnect

    async def disconnect_then_fail(self: ConnectionPool, inuse_connections: bool = True) -> None:
        await real_disconnect(self, inuse_connections)  # still release the socket
        raise RedisConnectionError("socket torn down mid-close")

    monkeypatch.setattr(ConnectionPool, "disconnect", disconnect_then_fail)

    result = queue.enqueue_job_best_effort("print_receiving_label_job", receipt_id=55)

    assert [j.function for j in _landed_jobs(fake_redis)] == ["print_receiving_label_job"]
    assert result is True
    assert _queue_records(caplog, logging.ERROR) == []
    warnings = _queue_records(caplog, logging.WARNING)
    assert [r.getMessage() for r in warnings] == [
        "Closing the Redis pool used to enqueue print_receiving_label_job failed"
    ]


def test_a_real_enqueue_failure_is_still_reported_and_still_closes_the_pool(
    fake_redis: _FakeRedisServer,
    pool_disconnects: List[ConnectionPool],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """The best-effort contract is unchanged: a failed enqueue returns False and never raises.

    The ERROR record must carry the ENQUEUE's exception. Before the fix the ``finally``
    raised ``AttributeError`` over it, so the log named the cleanup and buried the cause.
    This test is also the positive control proving ``caplog`` sees this logger at all, which
    keeps the "nothing was logged" assertions above from passing vacuously.
    """
    caplog.set_level(logging.DEBUG, logger=_QUEUE_LOGGER)

    async def broken_enqueue(self: ArqRedis, *args, **kwargs):
        raise RuntimeError("redis refused the transaction")

    monkeypatch.setattr(ArqRedis, "enqueue_job", broken_enqueue)

    result = queue.enqueue_job_best_effort("dispatch_work_order_completion_signals_job", work_order_id=1)

    assert result is False
    errors = _queue_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].getMessage() == (
        "Failed to enqueue background job dispatch_work_order_completion_signals_job; continuing without it"
    )
    assert errors[0].exc_info is not None
    assert isinstance(errors[0].exc_info[1], RuntimeError)
    assert len(pool_disconnects) == 1, "a failed enqueue must still close its pool"
    assert fake_redis.wait_until_every_connection_is_closed(_SETTLE_SECONDS)


# --------------------------------------------------------------------------- #
# enqueue_job_fire_and_forget_fastfail (async handlers, the outbox's running-loop branch)
# --------------------------------------------------------------------------- #
def test_fire_and_forget_enqueue_closes_its_pool_without_reporting_failure(
    fake_redis: _FakeRedisServer,
    pool_disconnects: List[ConnectionPool],
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG, logger=_QUEUE_LOGGER)

    asyncio.run(queue.enqueue_job_fire_and_forget_fastfail("dispatch_notification_job", event_id=9))

    assert [(j.function, j.kwargs) for j in _landed_jobs(fake_redis)] == [
        ("dispatch_notification_job", {"event_id": 9})
    ]
    assert _queue_records(caplog, logging.WARNING) == [], "a landed job was logged as a failure"
    assert len(pool_disconnects) == 1, "the enqueue pool was never closed"
    assert fake_redis.wait_until_every_connection_is_closed(_SETTLE_SECONDS)
