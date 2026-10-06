"""Nest hold reasons remain visible without creating a completion blocker."""

import pytest

from app.models.time_entry import TimeEntry
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from tests.api import test_laser_nest_pool_quantity_rollup as pool_fixtures
from tests.api.kiosk_test_helpers import bearer, kiosk_token_for, make_kiosk_station

upload_dir = pool_fixtures.upload_dir
_no_ai = pool_fixtures._no_ai
pytestmark = [pytest.mark.api, pytest.mark.requires_db]

HOLD_REASON = {
    "category": "material_missing",
    "severity": "high",
    "note": "Waiting for the replacement sheet from the rack",
}


def _held_nest(client, db_session):
    admin = pool_fixtures.make_user(db_session)
    work_center = pool_fixtures.make_laser_work_center(db_session)
    work_order = pool_fixtures._import_three_nest_wo(client, admin, work_center)
    operation = pool_fixtures._ops_by_sequence(work_order)[-1]
    headers = pool_fixtures.headers_for(admin)
    response = client.put(
        f"/api/v1/shop-floor/operations/{operation['id']}/hold",
        headers=headers,
        json=HOLD_REASON,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "on_hold"
    return admin, work_center, work_order, operation, headers


@pytest.mark.parametrize("surface", ["office", "shop_floor", "identified_queue"])
def test_simple_nest_hold_reason_is_visible_to_identified_users(client, db_session, surface):
    admin, work_center, work_order, operation, headers = _held_nest(client, db_session)
    operation_id = operation["id"]

    if surface == "office":
        response = client.get(f"/api/v1/work-orders/{work_order['id']}", headers=headers)
        assert response.status_code == 200, response.text
        row = next(row for row in response.json()["operations"] if row["id"] == operation_id)
        hold = row["hold_context"]
    elif surface == "shop_floor":
        response = client.get(
            "/api/v1/shop-floor/operations",
            headers=headers,
            params={"status": "on_hold", "work_center_id": work_center.id},
        )
        assert response.status_code == 200, response.text
        row = next(row for row in response.json()["operations"] if row["id"] == operation_id)
        hold = row["hold"]
    else:
        response = client.get(f"/api/v1/shop-floor/work-center-queue/{work_center.id}", headers=headers)
        assert response.status_code == 200, response.text
        row = next(row for row in response.json()["held"] if row["operation_id"] == operation_id)
        hold = row["hold"]

    assert hold["blocker"] is None
    assert hold["held_by_user_id"] == admin.id
    assert hold["held_by_name"]
    assert hold["held_at"]
    for field, value in HOLD_REASON.items():
        assert hold[field] == value
    assert hold["has_note"] is True
    assert hold["free_text_withheld"] is False
    assert db_session.query(WorkOrderBlocker).filter_by(operation_id=operation_id).count() == 0


def test_station_queue_withholds_simple_nest_hold_note(client, db_session):
    _, work_center, _, operation, _ = _held_nest(client, db_session)
    station = make_kiosk_station(db_session, work_center=work_center)

    response = client.get(
        f"/api/v1/shop-floor/work-center-queue/{work_center.id}",
        headers=bearer(kiosk_token_for(station)),
    )

    assert response.status_code == 200, response.text
    row = next(row for row in response.json()["held"] if row["operation_id"] == operation["id"])
    hold = row["hold"]
    assert hold["blocker"] is None
    assert hold["category"] == HOLD_REASON["category"]
    assert hold["severity"] == HOLD_REASON["severity"]
    assert hold["has_note"] is True
    assert hold["free_text_withheld"] is True
    assert "note" not in hold
    assert HOLD_REASON["note"] not in response.text


def test_clearing_simple_nest_hold_is_enough_to_complete_the_work_order(client, db_session):
    _, _, work_order, held_operation, headers = _held_nest(client, db_session)
    operation_id = held_operation["id"]

    still_held = client.post(
        f"/api/v1/work-orders/operations/{operation_id}/complete",
        params={"quantity_complete": held_operation["component_quantity"]},
        headers=headers,
    )
    assert still_held.status_code == 409, still_held.text

    resumed = client.put(f"/api/v1/shop-floor/operations/{operation_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "ready"
    assert resumed.json()["open_blockers"] == []

    for operation in pool_fixtures._ops_by_sequence(work_order):
        completed = client.post(
            f"/api/v1/work-orders/operations/{operation['id']}/complete",
            params={"quantity_complete": operation["component_quantity"]},
            headers=headers,
        )
        assert completed.status_code == 200, completed.text

    db_session.expire_all()
    persisted = db_session.get(WorkOrder, work_order["id"])
    assert persisted.status == WorkOrderStatus.COMPLETE
    assert float(persisted.quantity_complete) == 9.0
    assert db_session.get(WorkOrderOperation, operation_id).status == OperationStatus.COMPLETE
    assert db_session.query(WorkOrderBlocker).filter_by(work_order_id=work_order["id"]).count() == 0


def test_clearing_nest_hold_preserves_a_separately_reported_blocker(client, db_session):
    _, _, work_order, operation, headers = _held_nest(client, db_session)
    reported = client.post(
        f"/api/v1/work-order-blockers/work-orders/{work_order['id']}",
        headers=headers,
        json={
            "operation_id": operation["id"],
            "category": "quality_hold",
            "note": "Separate quality issue requires disposition",
        },
    )
    assert reported.status_code == 200, reported.text
    blocker_id = reported.json()["id"]

    resumed = client.put(f"/api/v1/shop-floor/operations/{operation['id']}/resume", headers=headers)

    assert resumed.status_code == 200, resumed.text
    assert [blocker["id"] for blocker in resumed.json()["open_blockers"]] == [blocker_id]
    db_session.expire_all()
    blocker = db_session.get(WorkOrderBlocker, blocker_id)
    assert blocker.status == "open"
    assert blocker.note == "Separate quality issue requires disposition"
    assert db_session.query(WorkOrderBlocker).filter_by(operation_id=operation["id"]).count() == 1


def test_read_does_not_clear_plain_nest_hold_after_all_quantity_is_reported(client, db_session):
    admin = pool_fixtures.make_user(db_session)
    work_center = pool_fixtures.make_laser_work_center(db_session)
    work_order = pool_fixtures._import_three_nest_wo(client, admin, work_center)
    operations = pool_fixtures._ops_by_sequence(work_order)
    headers = pool_fixtures.headers_for(admin)
    for operation in operations[:-1]:
        completed = client.post(
            f"/api/v1/work-orders/operations/{operation['id']}/complete",
            params={"quantity_complete": operation["component_quantity"]},
            headers=headers,
        )
        assert completed.status_code == 200, completed.text

    operation = operations[-1]
    operation_id = operation["id"]
    clocked = client.post(
        "/api/v1/shop-floor/clock-in",
        headers=headers,
        json={
            "work_order_id": work_order["id"],
            "operation_id": operation_id,
            "work_center_id": work_center.id,
            "entry_type": "run",
        },
    )
    assert clocked.status_code == 200, clocked.text
    produced = client.post(
        f"/api/v1/shop-floor/operations/{operation_id}/production",
        headers=headers,
        json={"quantity_complete_delta": operation["component_quantity"]},
    )
    assert produced.status_code == 200, produced.text
    held = client.put(f"/api/v1/shop-floor/operations/{operation_id}/hold", headers=headers)
    assert held.status_code == 200, held.text

    db_session.expire_all()
    entry = db_session.query(TimeEntry).filter_by(operation_id=operation_id).one()
    assert entry.clock_out is not None
    assert float(entry.quantity_produced) == float(operation["component_quantity"])

    for _ in range(2):
        response = client.get(f"/api/v1/work-orders/{work_order['id']}", headers=headers)
        assert response.status_code == 200, response.text
        row = next(row for row in response.json()["operations"] if row["id"] == operation_id)
        assert row["status"] == "on_hold"
        assert row["hold_context"]["blocker"] is None
        assert response.json()["status"] != "complete"
        assert float(response.json()["quantity_complete"]) == 9.0
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, operation_id).status == OperationStatus.ON_HOLD

    resumed = client.put(f"/api/v1/shop-floor/operations/{operation_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["open_blockers"] == []
    completed = client.post(
        f"/api/v1/work-orders/operations/{operation_id}/complete",
        params={"quantity_complete": operation["component_quantity"]},
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    db_session.expire_all()
    assert db_session.get(WorkOrder, work_order["id"]).status == WorkOrderStatus.COMPLETE
    assert db_session.query(WorkOrderBlocker).filter_by(work_order_id=work_order["id"]).count() == 0
