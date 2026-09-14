"""Tenant-isolation tests for the realtime / WebSocket layer (finding EVT-6).

These verify the AS9100D/CMMC invariant #1 fix: completion broadcasts must reach
only the originating company's connected clients, never other tenants'. They
exercise the ``ConnectionManager`` and the module-level broadcast helpers
directly with a fake socket, so no live server is required.
"""

import pytest

from app.core import websocket as ws_module
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


async def test_helpers_without_company_id_broadcast_globally(monkeypatch):
    """Backward compatibility: omitting company_id keeps the legacy global fan-out
    so non-tenant-scoped callers are unaffected."""
    manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)

    company_a = await _connect(manager, user_id="1", company_id=10)
    company_b = await _connect(manager, user_id="2", company_id=20)

    await broadcast_dashboard_update({"event": "global_ping"})

    assert len(company_a.sent) == 1
    assert len(company_b.sent) == 1


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
