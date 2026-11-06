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


# --- The ACTIVE company, not the caller's home company ----------------------------


def test_platform_admin_broadcasts_go_to_the_switched_into_tenant(
    client: TestClient, db_session: Session, recorder: RecordingManager, company_b_job
):
    """A platform admin whose HOME company is A, switched into B, writes B's rows: every
    broadcast goes to B. The other tests use same-home-and-active users, so a call site
    that read ``current_user.company_id`` instead of ``get_current_company_id`` would pass
    them -- and send B's blocker and machine-status events to company A's sockets."""
    center, work_order, operation = company_b_job
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.PLATFORM_ADMIN)
    headers = user_headers(admin, active_company_id=COMPANY_B)

    created = client.post(
        f"/api/v1/work-order-blockers/work-orders/{work_order.id}",
        headers=headers,
        json={"operation_id": operation.id, "category": "material_missing", "title": "no stock"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["company_id"] == COMPANY_B
    settle(client)
    _assert_all_scoped_to(recorder.sent, COMPANY_B, {"work_order_update", "dashboard_update", "shop_floor_update"})

    recorder.sent.clear()
    status_resp = client.post(f"/api/v1/work-centers/{center.id}/status?status=offline", headers=headers)
    assert status_resp.status_code == 200, status_resp.text
    settle(client)
    _assert_all_scoped_to(recorder.sent, COMPANY_B, {"shop_floor_update", "dashboard_update"})


# --- A refused or no-op write broadcasts NOTHING -----------------------------------


def _create_b_blocker(client: TestClient, db_session: Session, work_order, operation) -> int:
    supervisor_b = make_user(db_session, company_id=COMPANY_B, role=UserRole.SUPERVISOR)
    created = client.post(
        f"/api/v1/work-order-blockers/work-orders/{work_order.id}",
        headers=user_headers(supervisor_b),
        json={"operation_id": operation.id, "category": "material_missing", "title": "no stock"},
    )
    assert created.status_code == 200, created.text
    settle(client)
    return created.json()["id"]


@pytest.mark.parametrize(
    "case",
    [
        "cross_tenant_blocker_create",
        "cross_tenant_blocker_update",
        "cross_tenant_blocker_resolve",
        "operator_blocker_update",
        "cross_tenant_work_center_status",
        "operator_work_center_status",
        "invalid_work_center_status",
        "unchanged_work_center_status",
    ],
)
def test_refused_or_noop_write_broadcasts_nothing_and_changes_nothing(
    case: str, client: TestClient, db_session: Session, recorder: RecordingManager, company_b_job
):
    """Broadcasts are post-commit nudges. A write the server refuses -- another tenant's
    id, a role below the gate, a bad value -- or one that changes nothing must not nudge
    ANY tenant: a cross-tenant attempt that broadcast under the caller's company would
    hand company A the ids of company B's rows. The row is re-read to prove the refusal
    left it untouched, and a closing positive control proves the recorder was live."""
    from app.models.work_center import WorkCenter
    from app.models.work_order_blocker import WorkOrderBlocker

    center, work_order, operation = company_b_job
    blocker_id = _create_b_blocker(client, db_session, work_order, operation)
    recorder.sent.clear()
    blocker_count_before = db_session.query(WorkOrderBlocker).count()
    supervisor_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    manager_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    operator_b = make_user(db_session, company_id=COMPANY_B, role=UserRole.OPERATOR)
    manager_b = make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)

    if case == "cross_tenant_blocker_create":
        resp = client.post(
            f"/api/v1/work-order-blockers/work-orders/{work_order.id}",
            headers=user_headers(supervisor_a),
            json={"operation_id": operation.id, "category": "material_missing", "title": "probe"},
        )
        expected = 404
    elif case == "cross_tenant_blocker_update":
        resp = client.put(
            f"/api/v1/work-order-blockers/{blocker_id}",
            headers=user_headers(supervisor_a),
            json={"status": "dismissed"},
        )
        expected = 404
    elif case == "cross_tenant_blocker_resolve":
        resp = client.post(
            f"/api/v1/work-order-blockers/{blocker_id}/resolve",
            headers=user_headers(supervisor_a),
            json={"resolution_note": "probe"},
        )
        expected = 404
    elif case == "operator_blocker_update":
        resp = client.put(
            f"/api/v1/work-order-blockers/{blocker_id}", headers=user_headers(operator_b), json={"status": "dismissed"}
        )
        expected = 403
    elif case == "cross_tenant_work_center_status":
        resp = client.post(f"/api/v1/work-centers/{center.id}/status?status=offline", headers=user_headers(manager_a))
        expected = 404
    elif case == "operator_work_center_status":
        resp = client.post(f"/api/v1/work-centers/{center.id}/status?status=offline", headers=user_headers(operator_b))
        expected = 403
    elif case == "invalid_work_center_status":
        resp = client.post(f"/api/v1/work-centers/{center.id}/status?status=exploded", headers=user_headers(manager_b))
        expected = 400
    else:  # unchanged_work_center_status -- documented: no write, no audit row, no broadcast
        resp = client.post(f"/api/v1/work-centers/{center.id}/status?status=available", headers=user_headers(manager_b))
        expected = 200

    assert resp.status_code == expected, resp.text
    settle(client)
    assert recorder.sent == []

    db_session.expire_all()  # nothing pending here: every write above went through its own request
    blocker = db_session.query(WorkOrderBlocker).filter(WorkOrderBlocker.id == blocker_id).one()
    assert blocker.status == "open"
    assert blocker.company_id == COMPANY_B
    assert db_session.query(WorkOrderBlocker).count() == blocker_count_before
    assert db_session.query(WorkCenter).filter(WorkCenter.id == center.id).one().current_status == "available"

    # Positive control: the same recorder hears a real write by the owning tenant.
    ok = client.post(f"/api/v1/work-centers/{center.id}/status?status=maintenance", headers=user_headers(manager_b))
    assert ok.status_code == 200, ok.text
    settle(client)
    _assert_all_scoped_to(recorder.sent, COMPANY_B, {"shop_floor_update", "dashboard_update"})


# --- An ASYNC route reaches its tenant too ------------------------------------------


def test_async_laser_nest_import_broadcasts_to_the_writing_tenant(
    client: TestClient, db_session: Session, recorder: RecordingManager, tmp_path, monkeypatch
):
    """The nest-package import is an ``async`` route, so ``safe_broadcast`` runs ON the
    event-loop thread, where ``anyio.from_thread`` refuses to run: those two broadcasts
    were silently dropped. Through the real route they now reach the importing tenant,
    and only it. Offline: a CNC-name package (filename inference), extractor forbidden."""
    import io
    import zipfile

    import app.api.endpoints.work_orders as work_orders_endpoint
    from app.models.work_center import WorkCenter

    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(
        work_orders_endpoint,
        "extract_nest_fields_from_pdf",
        lambda *a, **k: pytest.fail("this test must not call the AI extractor"),
    )
    make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)  # a second tenant exists
    admin_b = make_user(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
    laser = WorkCenter(
        name="Laser B",
        code="LASER-RT-B",
        work_center_type="laser",
        hourly_rate=120,
        is_active=True,
        company_id=COMPANY_B,
    )
    db_session.add(laser)
    db_session.commit()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("NEST-A_A36_10ga_60x120_QTY3.nc", "M30")

    resp = client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        headers=user_headers(admin_b),
        data={"work_center_id": str(laser.id)},
        files={"file": ("nests.zip", io.BytesIO(buf.getvalue()), "application/zip")},
    )

    assert resp.status_code == 200, resp.text
    settle(client)
    _assert_all_scoped_to(recorder.sent, COMPANY_B, {"work_order_update", "dashboard_update"})
    assert len(recorder.sent) == 2
    child_id = resp.json()["child_work_order"]["id"]
    assert all(msg["event"] == "laser_nest_package_imported" for _, _, msg in recorder.sent)
    assert {msg["work_order_id"] for _, _, msg in recorder.sent} == {child_id}
