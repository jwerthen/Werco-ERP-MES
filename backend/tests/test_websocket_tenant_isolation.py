"""Tenant-isolation tests for the realtime / WebSocket layer (finding EVT-6).

These verify invariant #1 for realtime: a broadcast reaches only the originating
company's connected clients, never other tenants', and a broadcast with no tenant
(``company_id=None``) reaches NOBODY rather than falling back to every tenant. They
exercise the ``ConnectionManager``, the module-level broadcast helpers and
``safe_broadcast`` directly with a fake socket, so no live server is required; a
static guard at the bottom holds every ``app/`` call site to passing ``company_id``.
"""

import ast
import asyncio
import functools
import inspect
import json
import logging
from pathlib import Path

import anyio
import pytest

from app.core import realtime
from app.core import websocket as ws_module
from app.core.config import Settings, settings
from app.core.realtime import safe_broadcast
from app.core.websocket import (
    ConnectionManager,
    broadcast_dashboard_update,
    broadcast_shop_floor_update,
    broadcast_work_order_update,
)

pytestmark = pytest.mark.unit


class FakeSocket:
    """Minimal stand-in for a Starlette WebSocket.

    Records every payload sent and reports whether ``accept`` was called.
    """

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.accepted = False
        self.fail = fail

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        if self.fail:
            raise RuntimeError("socket dead")
        self.sent.append(message)


async def _connect(manager: ConnectionManager, *, user_id: str, company_id: int) -> FakeSocket:
    sock = FakeSocket()
    await manager.connect(sock, user_id=user_id, company_id=company_id)
    return sock


async def drain_broadcasts() -> None:
    """Wait for every broadcast ``safe_broadcast`` scheduled on THIS loop (it is fire-and-forget)."""
    loop = asyncio.get_running_loop()
    pending = [task for task in list(realtime._in_flight) if task.get_loop() is loop]
    await asyncio.gather(*pending, return_exceptions=True)


async def test_broadcast_to_company_only_reaches_that_company():
    manager = ConnectionManager()
    company_a = await _connect(manager, user_id="1", company_id=10)
    company_b = await _connect(manager, user_id="2", company_id=20)

    await manager.broadcast_to_company(10, {"event": "operation_completed", "work_order_id": 5})

    assert len(company_a.sent) == 1
    assert "operation_completed" in company_a.sent[0]
    # The other tenant's socket must receive NOTHING (EVT-6 regression guard).
    assert company_b.sent == []


async def test_broadcast_to_company_unknown_company_is_noop():
    manager = ConnectionManager()
    sock = await _connect(manager, user_id="1", company_id=10)

    await manager.broadcast_to_company(99, {"event": "x"})

    assert sock.sent == []


async def test_completion_helpers_are_company_scoped(monkeypatch):
    """The three completion broadcast helpers, when given company_id, must only
    deliver to the originating company."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)

    company_a = await _connect(manager, user_id="1", company_id=10)
    company_b = await _connect(manager, user_id="2", company_id=20)

    await broadcast_work_order_update(5, {"status": "COMPLETE"}, company_id=10)
    await broadcast_dashboard_update({"event": "work_order_completed"}, company_id=10)
    await broadcast_shop_floor_update(3, {"event": "operation_completed"}, company_id=10)

    assert len(company_a.sent) == 3
    assert company_b.sent == []
    # The work-order payload still carries its id (frontend filters on it).
    assert any('"work_order_id": 5' in m for m in company_a.sent)


async def test_helpers_refuse_a_none_company_id(monkeypatch, caplog):
    """``company_id=None`` used to mean "send to EVERY tenant" -- which is how blocker
    and work-center status events reached other companies' sockets. It now means the
    caller failed to name the tenant: the message reaches NOBODY and a warning is
    logged that names the event but never echoes the payload."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)

    company_a = await _connect(manager, user_id="1", company_id=10)
    company_b = await _connect(manager, user_id="2", company_id=20)

    secret = {"event": "work_order_blocker_created", "blocker_id": 4242, "category": "tenant-a-secret"}
    with caplog.at_level(logging.WARNING, logger="app.core.websocket"):
        await broadcast_dashboard_update(dict(secret), company_id=None)
        await broadcast_work_order_update(5, dict(secret), company_id=None)
        await broadcast_shop_floor_update(3, dict(secret), company_id=None)

    assert company_a.sent == []
    assert company_b.sent == []
    dropped = [r for r in caplog.records if "no company_id" in r.getMessage()]
    assert len(dropped) == 3
    assert all("work_order_blocker_created" in r.getMessage() for r in dropped)
    assert not any("tenant-a-secret" in r.getMessage() or "4242" in r.getMessage() for r in caplog.records)


async def test_helpers_require_company_id_to_be_stated():
    """Keyword-only with NO default: a call that forgets the tenant is a TypeError at the
    call (and a mypy error for a direct await), not a silent global fan-out."""
    with pytest.raises(TypeError):
        await broadcast_dashboard_update({"event": "x"})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await broadcast_work_order_update(1, {"event": "x"})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await broadcast_shop_floor_update(1, {"event": "x"})  # type: ignore[call-arg]


def test_connection_manager_has_no_all_tenant_fanout():
    """The old ``ConnectionManager.broadcast`` sent to every socket regardless of tenant,
    and was reachable only as the ``company_id is None`` fallback. No caller needs a
    global path, so it is gone; re-adding one must be a deliberate, explicitly named
    method for tenant-free messages -- this tripwire makes that a visible decision."""
    assert not hasattr(ConnectionManager, "broadcast")


def test_send_to_user_requires_a_company():
    """A user is not a tenant boundary: a platform principal holds one socket per company
    it has switched into. ``send_to_user`` must therefore name the company, keyword-only
    with no default, like the ``broadcast_*`` helpers -- never "every socket this user has"."""
    param = inspect.signature(ConnectionManager.send_to_user).parameters["company_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


async def test_send_to_user_stays_inside_the_named_company(caplog):
    manager = ConnectionManager()
    in_company_a = await _connect(manager, user_id="1", company_id=10)
    in_company_b = await _connect(manager, user_id="1", company_id=20)  # same platform user, switched

    await manager.send_to_user("1", {"event": "for-a"}, company_id=10)

    assert len(in_company_a.sent) == 1
    assert in_company_b.sent == []

    with caplog.at_level(logging.WARNING, logger="app.core.websocket"):
        await manager.send_to_user("1", {"event": "nowhere", "note": "tenant-a-secret"}, company_id=None)
    assert len(in_company_a.sent) == 1 and in_company_b.sent == []
    assert any("no company_id" in r.getMessage() for r in caplog.records)
    assert not any("tenant-a-secret" in r.getMessage() for r in caplog.records)

    with pytest.raises(TypeError):
        await manager.send_to_user("1", {"event": "x"})  # type: ignore[call-arg]


async def test_safe_broadcast_carries_company_id_into_the_helper(monkeypatch):
    """REGRESSION: ``anyio.from_thread.run`` takes positional arguments only, so
    ``safe_broadcast(helper, ..., company_id=10)`` raised TypeError before the helper
    ran and was swallowed at DEBUG -- every tenant-scoped broadcast in the app was
    silently dropped. Run it the way a sync route does (from an AnyIO worker thread)
    and prove the scoped payload reaches its tenant, and only its tenant."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    company_a = await _connect(manager, user_id="1", company_id=10)
    company_b = await _connect(manager, user_id="2", company_id=20)

    await anyio.to_thread.run_sync(
        functools.partial(safe_broadcast, broadcast_work_order_update, 7, {"event": "x"}, company_id=10)
    )
    await anyio.to_thread.run_sync(
        functools.partial(safe_broadcast, broadcast_dashboard_update, {"event": "y"}, company_id=10)
    )
    await anyio.to_thread.run_sync(
        functools.partial(safe_broadcast, broadcast_shop_floor_update, 3, {"event": "z"}, company_id=10)
    )
    await drain_broadcasts()

    assert len(company_a.sent) == 3
    assert company_b.sent == []
    assert any('"work_order_id": 7' in m for m in company_a.sent)


async def test_safe_broadcast_without_company_id_reaches_nobody(monkeypatch, caplog):
    """A call site that forgets ``company_id`` is refused at the helper (missing
    keyword) and logged at WARNING by ``safe_broadcast`` -- never delivered anywhere."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    company_a = await _connect(manager, user_id="1", company_id=10)
    company_b = await _connect(manager, user_id="2", company_id=20)

    secret = {"event": "y", "blocker_id": 4242, "note": "tenant-a-secret"}
    with caplog.at_level(logging.WARNING, logger="app.core.realtime"):
        await anyio.to_thread.run_sync(functools.partial(safe_broadcast, broadcast_dashboard_update, secret))
        await drain_broadcasts()

    assert company_a.sent == []
    assert company_b.sent == []
    assert any("broadcast_dashboard_update" in r.getMessage() for r in caplog.records)
    # The warning names the coroutine and the error -- never the tenant payload.
    assert not any("tenant-a-secret" in r.getMessage() or "4242" in r.getMessage() for r in caplog.records)


def test_safe_broadcast_failure_is_logged_at_warning(caplog):
    """Best-effort still means never raising into a committed request -- but a broken
    realtime path must be visible. With neither a running loop nor an AnyIO worker
    thread there is nowhere to schedule; that now surfaces at WARNING instead of the
    DEBUG that hid the kwargs drop."""
    with caplog.at_level(logging.WARNING, logger="app.core.realtime"):
        safe_broadcast(broadcast_dashboard_update, {"event": "y"}, company_id=10)

    assert [r.levelno for r in caplog.records if r.name == "app.core.realtime"] == [logging.WARNING]


async def test_safe_broadcast_from_the_event_loop_thread_is_delivered(monkeypatch):
    """An ``async`` route calls ``safe_broadcast`` ON the loop thread, where
    ``from_thread`` refuses to run -- so those broadcasts were dropped too. They are
    now scheduled on the running loop, still scoped to their tenant."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    company_a = await _connect(manager, user_id="1", company_id=10)
    company_b = await _connect(manager, user_id="2", company_id=20)

    safe_broadcast(broadcast_dashboard_update, {"event": "laser_nest_package_imported"}, company_id=10)
    await drain_broadcasts()

    assert len(company_a.sent) == 1
    assert company_b.sent == []


async def test_safe_broadcast_does_not_wait_for_delivery():
    """Fire-and-forget: the request thread must not be held while every recipient
    socket is revalidated against the database. A delivery that has not finished
    still lets ``safe_broadcast`` return (a blocking implementation trips the timeout
    instead of hanging the run)."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_helper(message, *, company_id):
        started.set()
        await release.wait()

    with anyio.fail_after(3):
        await anyio.to_thread.run_sync(
            functools.partial(safe_broadcast, slow_helper, {"event": "x"}, company_id=10), abandon_on_cancel=True
        )

    assert not release.is_set()
    await asyncio.wait_for(started.wait(), timeout=2)
    release.set()
    await drain_broadcasts()


async def test_delivery_failure_is_logged_not_raised(caplog):
    async def broken_helper(message, *, company_id):
        raise RuntimeError("delivery exploded")

    with caplog.at_level(logging.WARNING, logger="app.core.realtime"):
        await anyio.to_thread.run_sync(functools.partial(safe_broadcast, broken_helper, {"event": "x"}, company_id=10))
        await drain_broadcasts()

    assert any("broken_helper" in r.getMessage() and "RuntimeError" in r.getMessage() for r in caplog.records)


def _in_flight_on_this_loop() -> list:
    loop = asyncio.get_running_loop()
    return [task for task in list(realtime._in_flight) if task.get_loop() is loop]


async def test_pending_broadcast_is_strongly_held_then_released():
    """The loop keeps only a WEAK reference to a task, so a fire-and-forget broadcast with
    no other owner can be garbage-collected before it runs (a silent drop). ``_in_flight``
    must hold it while it is pending -- that is also what lets a caller drain it -- and let
    go once it finishes, or every broadcast the process ever sends is retained forever."""
    release = asyncio.Event()

    async def slow_helper(message, *, company_id):
        await release.wait()

    await anyio.to_thread.run_sync(functools.partial(safe_broadcast, slow_helper, {"event": "x"}, company_id=10))

    pending = _in_flight_on_this_loop()
    assert len(pending) == 1 and not pending[0].done()

    release.set()
    await drain_broadcasts()
    await asyncio.sleep(0)  # let the done-callback run

    assert pending[0].done()
    assert _in_flight_on_this_loop() == []


async def test_cancelled_broadcast_is_released_quietly(caplog):
    """A broadcast still pending at shutdown is cancelled with the loop. That is not a
    delivery failure: no WARNING, no 'Exception in callback' from asyncio (reading
    ``task.exception()`` on a cancelled task raises inside the done-callback), and the
    task leaves ``_in_flight``."""
    loop = asyncio.get_running_loop()
    loop_errors: list = []
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    started = asyncio.Event()

    async def stuck_helper(message, *, company_id):
        started.set()
        await asyncio.Event().wait()

    try:
        with caplog.at_level(logging.WARNING, logger="app.core.realtime"):
            safe_broadcast(stuck_helper, {"event": "x"}, company_id=10)
            await asyncio.wait_for(started.wait(), timeout=2)
            (task,) = _in_flight_on_this_loop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)

    assert task.cancelled()
    assert _in_flight_on_this_loop() == []
    assert loop_errors == []
    assert [r for r in caplog.records if r.name == "app.core.realtime"] == []


# --- Delivery is serialized, bounded and switchable ---------------------------------------
#
# Each delivery revalidates every recipient socket through ``run_in_threadpool`` and a
# fresh ``SessionLocal`` -- the same worker-thread limiter and connection pool every
# request uses. One write can schedule dozens of broadcasts; run concurrently they would
# compete with unrelated requests for both. Realtime must hold at most one at a time.


async def _connect_checked(manager: ConnectionManager, authorize, *, user_id: str, company_id: int) -> FakeSocket:
    sock = FakeSocket()
    await manager.connect(sock, user_id=user_id, company_id=company_id, authorize=authorize)
    return sock


class ConcurrencyProbe:
    """A stand-in recipient check that records how many run at the same time."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.calls = 0

    async def __call__(self) -> bool:
        self.active += 1
        self.calls += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        return True


async def test_burst_from_sync_routes_runs_one_recipient_check_at_a_time(monkeypatch):
    """Fifty writes broadcast at once from fifty worker threads (fifty sync requests): the
    recipient checks -- each a worker-thread token plus a pooled connection in production
    -- must never overlap, and every broadcast must still be delivered."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    probe = ConcurrencyProbe()
    sockets = [await _connect_checked(manager, probe, user_id=str(n), company_id=10) for n in range(3)]
    other_tenant = await _connect_checked(manager, probe, user_id="99", company_id=20)

    async with anyio.create_task_group() as group:
        for n in range(50):
            group.start_soon(
                anyio.to_thread.run_sync,
                functools.partial(safe_broadcast, broadcast_dashboard_update, {"event": "e", "n": n}, company_id=10),
            )
    await drain_broadcasts()

    assert probe.peak == 1
    assert probe.calls == 50 * len(sockets)
    for sock in sockets:
        assert sorted(json.loads(m)["data"]["n"] for m in sock.sent) == list(range(50))
    assert other_tenant.sent == []


async def test_each_socket_receives_events_in_broadcast_order(monkeypatch):
    """Serialized delivery keeps call order per socket: the lock wakes waiters first-come,
    first-served, so a later event can never overtake an earlier one."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    probe = ConcurrencyProbe()
    sockets = [await _connect_checked(manager, probe, user_id=str(n), company_id=10) for n in range(2)]

    for n in range(30):
        safe_broadcast(broadcast_work_order_update, n, {"event": "e"}, company_id=10)
    await drain_broadcasts()

    assert probe.peak == 1
    for sock in sockets:
        assert [json.loads(m)["data"]["work_order_id"] for m in sock.sent] == list(range(30))


async def test_backlog_past_the_cap_is_dropped_with_a_warning(monkeypatch, caplog):
    """Serialized delivery queues; the queue is bounded. Past the cap a new broadcast is
    dropped and logged -- by name, never by payload -- rather than growing without limit."""
    monkeypatch.setattr(realtime, "MAX_PENDING_BROADCASTS", 3)
    release = asyncio.Event()
    delivered: list = []

    async def held_helper(message, *, company_id):
        await release.wait()
        delivered.append(message["n"])

    with caplog.at_level(logging.WARNING, logger="app.core.realtime"):
        for n in range(5):
            safe_broadcast(held_helper, {"n": n, "note": "tenant-a-secret"}, company_id=10)

    assert len(_in_flight_on_this_loop()) == 3
    dropped = [r for r in caplog.records if "dropped" in r.getMessage()]
    assert len(dropped) == 2
    assert all("held_helper" in r.getMessage() for r in dropped)
    assert not any("tenant-a-secret" in r.getMessage() for r in caplog.records)

    release.set()
    await drain_broadcasts()
    assert delivered == [0, 1, 2]


async def test_kill_switch_schedules_nothing(monkeypatch):
    """``REALTIME_BROADCAST_ENABLED=false`` turns every push off, from sync and async routes
    alike -- nothing is queued, nothing reaches a socket."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    sock = await _connect(manager, user_id="1", company_id=10)
    monkeypatch.setattr(settings, "REALTIME_BROADCAST_ENABLED", False)

    safe_broadcast(broadcast_dashboard_update, {"event": "x"}, company_id=10)
    await anyio.to_thread.run_sync(
        functools.partial(safe_broadcast, broadcast_dashboard_update, {"event": "y"}, company_id=10)
    )

    assert _in_flight_on_this_loop() == []
    await drain_broadcasts()
    assert sock.sent == []

    monkeypatch.setattr(settings, "REALTIME_BROADCAST_ENABLED", True)
    safe_broadcast(broadcast_dashboard_update, {"event": "z"}, company_id=10)
    await drain_broadcasts()
    assert len(sock.sent) == 1


def test_realtime_pushes_ship_dark(monkeypatch):
    """Pushes ship OFF: arming them is a deliberate deploy setting, never a side effect of a
    merge. Restoring delivery made ~85 dormant call sites live, and every push triggers
    refetches on every listening tab -- an unmeasured load on the API the kiosks share.
    The suite itself runs with pushes ON (tests/conftest.py), so this pins the shipped
    default from the field declaration and from a Settings built without the variable."""
    assert Settings.model_fields["REALTIME_BROADCAST_ENABLED"].default is False
    monkeypatch.delenv("REALTIME_BROADCAST_ENABLED", raising=False)
    assert Settings().REALTIME_BROADCAST_ENABLED is False


class StalledSocket(FakeSocket):
    """A client that has stopped reading: ``send_text`` never completes."""

    async def send_text(self, message: str) -> None:
        await asyncio.Event().wait()


async def test_stalled_socket_does_not_hold_up_the_others(monkeypatch):
    """Delivery is one-at-a-time, so a socket whose send never completes would freeze every
    other socket's events behind it. The send is bounded; the stalled socket is dropped
    from delivery and the rest are served."""
    monkeypatch.setattr(ws_module, "WS_SEND_TIMEOUT_SECONDS", 0.05)
    manager = ConnectionManager()
    stalled = StalledSocket()
    await manager.connect(stalled, user_id="1", company_id=10)
    healthy = await _connect(manager, user_id="2", company_id=10)

    with anyio.fail_after(2):
        await manager.broadcast_to_company(10, {"event": "ping"})

    assert len(healthy.sent) == 1
    assert stalled not in manager.connection_company
    assert manager.company_connections.get(10) == [healthy]


async def test_disconnect_cleans_up_company_registry():
    manager = ConnectionManager()
    sock = await _connect(manager, user_id="1", company_id=10)

    assert manager.company_connections.get(10) == [sock]
    assert manager.connection_company.get(sock) == 10

    manager.disconnect(sock, user_id="1")

    assert 10 not in manager.company_connections
    assert sock not in manager.connection_company
    # Subsequent company broadcast reaches nobody and does not raise.
    # (no assertion target needed; absence of exception + empty registry is the contract)


async def test_dead_socket_is_pruned_from_company_registry():
    manager = ConnectionManager()
    good = await _connect(manager, user_id="1", company_id=10)
    dead = FakeSocket(fail=True)
    await manager.connect(dead, user_id="2", company_id=10)

    await manager.broadcast_to_company(10, {"event": "ping"})

    # Good socket received it; the failing one was disconnected/pruned.
    assert len(good.sent) == 1
    assert dead not in manager.connection_company
    assert manager.company_connections.get(10) == [good]


def test_identity_from_token_rejects_invalid_token():
    from app.api.websocket import _identity_from_token

    assert _identity_from_token("not-a-real-jwt") is None


@pytest.mark.requires_db
def test_identity_from_token_uses_token_company_id(db_session, monkeypatch):
    """A persisted platform principal may switch to another active tenant."""
    from sqlalchemy.orm import Session

    from app.api import websocket as ws_api
    from app.core.security import create_access_token
    from app.models.user import UserRole
    from tests.api.kiosk_test_helpers import ensure_company, make_user

    platform = make_user(db_session, company_id=1, role=UserRole.PLATFORM_ADMIN)
    ensure_company(db_session, 2)
    monkeypatch.setattr(ws_api, 'SessionLocal', lambda: Session(bind=db_session.get_bind()))
    token = create_access_token(platform.id, company_id=2)

    assert ws_api._identity_from_token(token) == (str(platform.id), 2)


# --- Static guard: every broadcast call site names its tenant -------------------------

_TENANT_HELPERS = {"broadcast_dashboard_update", "broadcast_work_order_update", "broadcast_shop_floor_update"}
_APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


@functools.lru_cache(maxsize=1)
def _parsed_app_modules() -> tuple:
    """(path, AST) for every ``app/`` module that mentions a broadcast -- parsed once."""
    modules = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        source = path.read_text()
        if "broadcast" in source:
            modules.append((path, ast.parse(source)))
    return tuple(modules)


def _calls(name: str):
    for path, tree in _parsed_app_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) == name:
                yield path, node


def _unscoped_broadcast_calls() -> list[str]:
    offenders = []
    for path, tree in _parsed_app_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name == "safe_broadcast" and node.args and _call_name(node.args[0]) in _TENANT_HELPERS:
                target = _call_name(node.args[0])
            elif name in _TENANT_HELPERS:
                target = name
            else:
                continue
            if "company_id" not in {kw.arg for kw in node.keywords}:
                offenders.append(f"{path.relative_to(_APP_ROOT.parent)}:{node.lineno} {target}")
    return offenders


def test_guard_finds_the_call_sites_it_is_meant_to_check():
    """Keep the guard below honest: it must actually be looking at the ~90 real call
    sites, or an empty offender list proves nothing."""
    assert len(list(_calls("safe_broadcast"))) >= 50


def test_every_broadcast_call_site_passes_company_id():
    """Every ``safe_broadcast(<tenant helper>, ...)`` and every direct helper call in
    ``app/`` must pass ``company_id=`` -- the work-order blocker router and the
    work-center status route omitted it and broadcast tenant data to every company."""
    assert _unscoped_broadcast_calls() == []


def test_tenant_helper_set_is_every_public_broadcast_helper():
    """The two guards above only inspect calls to the helpers named in ``_TENANT_HELPERS``.
    A new ``broadcast_*`` helper added to ``app.core.websocket`` would be invisible to them,
    so the set is pinned to the module's actual public helpers -- and every one of them must
    take ``company_id`` keyword-only with no default, like the three that exist today."""
    public = {
        name
        for name, fn in vars(ws_module).items()
        if name.startswith("broadcast_") and inspect.iscoroutinefunction(fn)
    }
    assert public == _TENANT_HELPERS
    for name in public:
        param = inspect.signature(getattr(ws_module, name)).parameters["company_id"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert param.default is inspect.Parameter.empty, name


def test_every_safe_broadcast_targets_a_tenant_helper():
    """``safe_broadcast(<anything else>, ...)`` -- a lambda, a new private coroutine, a
    manager method -- would escape the ``company_id`` guard entirely."""
    unknown = [
        f"{path.relative_to(_APP_ROOT.parent)}:{node.lineno} {ast.unparse(node.args[0]) if node.args else '<none>'}"
        for path, node in _calls("safe_broadcast")
        if not node.args or _call_name(node.args[0]) not in _TENANT_HELPERS
    ]
    assert unknown == []


# --- Static guard: the tenant is the ACTIVE company (``get_current_company_id``) -----------
#
# Passing ``company_id=`` is not enough: ``company_id=current_user.company_id`` passes the
# guard above and sends a switched-in platform admin's writes to their HOME company's
# sockets, and ``company_id=None`` passes it and is dropped. CLAUDE.md: derive the company
# from ``get_current_company_id``, never ``current_user.company_id``. So every broadcast's
# ``company_id`` must be a parameter declared ``Depends(get_current_company_id)`` --
# directly, or through a private wrapper (``_broadcast_blocker``, ``broadcast_dispatch_changes``,
# ...) whose EVERY caller passes such a parameter. Extend this deliberately if a genuine
# non-request broadcaster (e.g. a background job) ever appears; don't loosen it to "any name".

_ACTIVE_COMPANY_DEPENDENCY = "Depends(get_current_company_id)"
_MAX_WRAPPER_DEPTH = 4


@functools.lru_cache(maxsize=1)
def _all_app_modules() -> tuple:
    """(path, source) for every ``app/`` module -- wrappers can be called from anywhere."""
    return tuple((path, path.read_text()) for path in sorted(_APP_ROOT.rglob("*.py")))


@functools.lru_cache(maxsize=None)
def _parsed(path: Path) -> tuple:
    tree = ast.parse(path.read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    return tree, parents


def _function_chain(path: Path, node: ast.AST) -> list:
    """Enclosing function defs of ``node``, innermost first."""
    _, parents = _parsed(path)
    chain, cursor = [], parents.get(node)
    while cursor is not None:
        if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chain.append(cursor)
        cursor = parents.get(cursor)
    return chain


def _params_with_sources(fn) -> dict:
    args = fn.args
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    pairs = list(zip(positional, defaults)) + list(zip(args.kwonlyargs, args.kw_defaults))
    return {arg.arg: (arg, default) for arg, default in pairs}


def _rebinds(fn, name: str) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr)):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
    return False


def _calls_in(modules, name: str):
    for path, source in modules:
        if name not in source:
            continue
        tree, _ = _parsed(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) == name:
                yield path, node


def _bound_argument(call: ast.Call, fn, param: str):
    for kw in call.keywords:
        if kw.arg == param:
            return kw.value
    positional = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if param in positional and positional.index(param) < len(call.args):
        return call.args[positional.index(param)]
    return None


def _tenant_problems(modules, path: Path, value, at: ast.AST, depth: int = 0) -> list:
    where = f"{path.name}:{getattr(at, 'lineno', '?')}"
    if not isinstance(value, ast.Name):
        shown = ast.unparse(value) if value is not None else "<missing>"
        return [f"{where} company_id={shown} is not the get_current_company_id parameter"]
    for fn in _function_chain(path, at):
        params = _params_with_sources(fn)
        if value.id not in params:
            if _rebinds(fn, value.id):
                return [f"{where} {value.id} is bound locally in {fn.name}(), not by the dependency"]
            continue  # a closure: look in the next enclosing function
        if _rebinds(fn, value.id):
            return [f"{where} parameter {value.id} is reassigned inside {fn.name}()"]
        arg, default = params[value.id]
        declared = " ".join(ast.unparse(x) for x in (default, arg.annotation) if x is not None)
        if _ACTIVE_COMPANY_DEPENDENCY in declared:
            return []
        if default is not None:
            return [f"{where} {fn.name}({value.id}={ast.unparse(default)}) is not the active-company dependency"]
        if depth >= _MAX_WRAPPER_DEPTH:
            return [f"{where} {fn.name}() wrapper chain too deep to trace"]
        # A private wrapper: EVERY caller must hand it the active company.
        callers = list(_calls_in(modules, fn.name))
        if not callers:
            return [f"{where} {fn.name}() receives {value.id} but has no caller to trace"]
        problems: list = []
        for caller_path, call in callers:
            bound = _bound_argument(call, fn, value.id)
            problems += _tenant_problems(modules, caller_path, bound, call, depth + 1)
        return problems
    return [f"{where} {value.id} is not a parameter of any enclosing function"]


def _broadcast_tenant_problems(modules) -> tuple:
    """(problems, number of broadcast call sites examined)."""
    problems, examined = [], 0
    for path, _source in modules:
        tree, _ = _parsed(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            is_safe = name == "safe_broadcast" and node.args and _call_name(node.args[0]) in _TENANT_HELPERS
            if not (is_safe or name in _TENANT_HELPERS):
                continue
            examined += 1
            value = next((kw.value for kw in node.keywords if kw.arg == "company_id"), None)
            problems += _tenant_problems(modules, path, value, node)
    return problems, examined


def test_every_broadcast_tenant_is_the_active_company_dependency():
    problems, examined = _broadcast_tenant_problems(_all_app_modules())
    assert examined >= 50  # it is looking at the real call sites, not an empty set
    assert problems == []


_PROBE = '''
from fastapi import Depends

def route_ok(company_id: int = Depends(get_current_company_id)):
    safe_broadcast(broadcast_dashboard_update, {}, company_id=company_id)

def route_home_company(current_user=Depends(get_current_user)):
    safe_broadcast(broadcast_dashboard_update, {}, company_id=current_user.company_id)

def route_none():
    safe_broadcast(broadcast_dashboard_update, {}, company_id=None)

def route_rebound(work_order, company_id: int = Depends(get_current_company_id)):
    company_id = work_order.company_id
    safe_broadcast(broadcast_dashboard_update, {}, company_id=company_id)

def route_wrong_dependency(company_id: int = Depends(get_home_company_id)):
    safe_broadcast(broadcast_dashboard_update, {}, company_id=company_id)

def _wrapper(event, company_id):
    safe_broadcast(broadcast_dashboard_update, {"event": event}, company_id=company_id)

def route_wrapped_ok(company_id: int = Depends(get_current_company_id)):
    _wrapper("ok", company_id=company_id)

def route_wrapped_positional_ok(company_id: int = Depends(get_current_company_id)):
    _wrapper("ok", company_id)

def route_wrapped_home(current_user=Depends(get_current_user)):
    _wrapper("bad", current_user.company_id)
'''


def test_active_company_guard_rejects_the_wrong_tenant_sources(tmp_path):
    """Keep the guard above honest: it must refuse each shape it exists to catch, accept a
    direct dependency and a wrapper fed by one (by keyword or position), and blame only the
    bad wrapper CALLER -- or an empty problem list over app/ proves nothing."""
    module = tmp_path / "probe.py"
    module.write_text(_PROBE)
    modules = ((module, _PROBE),)
    problems, examined = _broadcast_tenant_problems(modules)
    assert examined == 6  # five routes plus the wrapper; wrapper CALLS are traced, not counted

    def flagged(fn_name: str) -> bool:
        return any(f"probe.py:{line}" in p for p in problems for line in _call_lines(module, fn_name))

    assert not flagged("route_ok")
    assert flagged("route_home_company")
    assert flagged("route_none")
    assert flagged("route_rebound")
    assert flagged("route_wrong_dependency")
    assert not flagged("route_wrapped_ok")
    assert not flagged("route_wrapped_positional_ok")
    assert flagged("route_wrapped_home")
    # Exactly the five bad shapes, each reported once -- nothing else.
    assert len(problems) == 5, problems


def _probe_functions(module: Path) -> dict:
    tree, _ = _parsed(module)
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _call_lines(module: Path, fn_name: str) -> list:
    fn = _probe_functions(module)[fn_name]
    return [node.lineno for node in ast.walk(fn) if isinstance(node, ast.Call) and node.func is not None]
