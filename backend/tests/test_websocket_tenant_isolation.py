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
import logging
from pathlib import Path

import anyio
import pytest

from app.core import realtime
from app.core import websocket as ws_module
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

    with caplog.at_level(logging.WARNING, logger="app.core.realtime"):
        await anyio.to_thread.run_sync(functools.partial(safe_broadcast, broadcast_dashboard_update, {"event": "y"}))
        await drain_broadcasts()

    assert company_a.sent == []
    assert company_b.sent == []
    assert any("broadcast_dashboard_update" in r.getMessage() for r in caplog.records)


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
