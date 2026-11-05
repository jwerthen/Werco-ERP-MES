"""Blocker and work-center status broadcasts carry the ACTIVE company's id.

THE DEFECT. ``work_order_blockers._broadcast_blocker`` (create / update / resolve)
and ``POST /work-centers/{id}/status`` called the broadcast helpers with no
``company_id``, which fell through to a fan-out to EVERY tenant's sockets: blocker
ids, categories and statuses, work-order and operation ids, and a machine's
status reached other companies, and refetched their Dashboard, Scheduling and
Shop Floor screens.

These go through the real route -> ``safe_broadcast`` -> helper path (a sync
route runs in an AnyIO worker thread under ``TestClient``, exactly as in
production), with the connection manager swapped for a recorder. Each write is
made by a COMPANY B user, so a hard-coded or defaulted tenant would show up as
the wrong id rather than happening to match company 1.
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import realtime
from app.core import websocket as ws_module
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


class RecordingManager:
    """Stands in for ``ConnectionManager``: records every scoped send, delivers nothing.

    Deliberately has NO ``broadcast`` attribute -- a regression to the all-tenant
    fan-out would raise ``AttributeError`` (logged by ``safe_broadcast``) and leave
    the recorder empty, failing the count assertions below.
    """

    def __init__(self) -> None:
        self.sent: List[Tuple[Optional[int], str, Dict[str, Any]]] = []

    async def broadcast_to_company(self, company_id: int, message: Dict[str, Any], message_type: str = "update"):
        self.sent.append((company_id, message_type, message))


@pytest.fixture
def recorder(monkeypatch) -> RecordingManager:
    manager = RecordingManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    return manager


@pytest.fixture
def company_b_job(db_session: Session):
    center = make_work_center(db_session, company_id=COMPANY_B)
    work_order, operation = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=center)
    return center, work_order, operation


def settle(client: TestClient) -> None:
    """``safe_broadcast`` is fire-and-forget: wait for what it scheduled on the app's loop."""

    async def drain() -> None:
        loop = asyncio.get_running_loop()
        pending = [task for task in list(realtime._in_flight) if task.get_loop() is loop]
        await asyncio.gather(*pending, return_exceptions=True)

    client.portal.call(drain)


def _assert_all_scoped_to(sent, company_id: int, expected_types: set) -> None:
    assert sent, "the write broadcast nothing -- the scoped path is not reaching the manager"
    assert {cid for cid, _, _ in sent} == {company_id}
    assert {message_type for _, message_type, _ in sent} == expected_types


def test_blocker_create_update_resolve_broadcast_to_the_writing_tenant_only(
    client: TestClient, db_session: Session, recorder: RecordingManager, company_b_job
):
    center, work_order, operation = company_b_job
    supervisor_b = make_user(db_session, company_id=COMPANY_B, role=UserRole.SUPERVISOR)
    headers = user_headers(supervisor_b)

    created = client.post(
        f"/api/v1/work-order-blockers/work-orders/{work_order.id}",
        headers=headers,
        json={"operation_id": operation.id, "category": "material_missing", "title": "no stock"},
    )
    assert created.status_code == 200, created.text
    blocker_id = created.json()["id"]
    settle(client)
    _assert_all_scoped_to(recorder.sent, COMPANY_B, {"work_order_update", "dashboard_update", "shop_floor_update"})
    assert all(msg["event"] == "work_order_blocker_created" for _, _, msg in recorder.sent)
    shop_floor = [msg for _, mtype, msg in recorder.sent if mtype == "shop_floor_update"]
    assert shop_floor[0]["work_center_id"] == center.id

    recorder.sent.clear()
    updated = client.put(f"/api/v1/work-order-blockers/{blocker_id}", headers=headers, json={"status": "acknowledged"})
    assert updated.status_code == 200, updated.text
    settle(client)
    _assert_all_scoped_to(recorder.sent, COMPANY_B, {"work_order_update", "dashboard_update", "shop_floor_update"})
    assert all(msg["event"] == "work_order_blocker_updated" for _, _, msg in recorder.sent)

    recorder.sent.clear()
    resolved = client.post(
        f"/api/v1/work-order-blockers/{blocker_id}/resolve", headers=headers, json={"resolution_note": "arrived"}
    )
    assert resolved.status_code == 200, resolved.text
    settle(client)
    _assert_all_scoped_to(recorder.sent, COMPANY_B, {"work_order_update", "dashboard_update", "shop_floor_update"})
    assert all(msg["event"] == "work_order_blocker_resolved" for _, _, msg in recorder.sent)


def test_work_center_status_broadcasts_to_the_writing_tenant_only(
    client: TestClient, db_session: Session, recorder: RecordingManager
):
    center = make_work_center(db_session, company_id=COMPANY_B)
    manager_b = make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)

    resp = client.post(f"/api/v1/work-centers/{center.id}/status?status=maintenance", headers=user_headers(manager_b))

    assert resp.status_code == 200, resp.text
    settle(client)
    _assert_all_scoped_to(recorder.sent, COMPANY_B, {"shop_floor_update", "dashboard_update"})
    assert all(msg["work_center_id"] == center.id and msg["status"] == "maintenance" for _, _, msg in recorder.sent)


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: List[str] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


def test_other_tenant_socket_receives_nothing_from_a_blocker_write(
    client: TestClient, db_session: Session, monkeypatch, company_b_job
):
    """End to end through a REAL ``ConnectionManager``: company A's open socket hears
    nothing when company B reports a blocker, while B's own socket does."""
    manager = ws_module.ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", manager)
    socket_a, socket_b = _FakeSocket(), _FakeSocket()

    async def connect_both() -> None:
        await manager.connect(socket_a, user_id="a", company_id=COMPANY_A)
        await manager.connect(socket_b, user_id="b", company_id=COMPANY_B)

    asyncio.run(connect_both())
    _, work_order, operation = company_b_job
    operator_b = make_user(db_session, company_id=COMPANY_B, role=UserRole.OPERATOR)

    resp = client.post(
        f"/api/v1/work-order-blockers/work-orders/{work_order.id}",
        headers=user_headers(operator_b),
        json={"operation_id": operation.id, "category": "material_missing", "title": "no stock"},
    )

    assert resp.status_code == 200, resp.text
    settle(client)
    assert len(socket_b.sent) == 3
    assert socket_a.sent == []
