"""Cancelled nest tombstones must not keep finished laser work orders open."""

from datetime import datetime, timedelta

import pytest

from app.models.audit_log import AuditLog
from app.models.laser_nest import LaserNest
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.work_order_state_service import (
    pooled_quantity_complete,
    reduce_operation_produced_quantity,
)
from tests.api import test_laser_nest_pool_quantity_rollup as pool_fixtures

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
upload_dir = pool_fixtures.upload_dir
_no_ai = pool_fixtures._no_ai


@pytest.fixture
def nest_pool(client, db_session):
    admin = pool_fixtures.make_user(db_session)
    work_center = pool_fixtures.make_laser_work_center(db_session)
    work_order = pool_fixtures._import_three_nest_wo(client, admin, work_center)
    return admin, work_center, work_order, pool_fixtures._ops_by_sequence(work_order)


def _cancel(client, admin, operation):
    response = client.delete(
        f"/api/v1/laser-nests/{operation['laser_nest']['id']}",
        headers=pool_fixtures.headers_for(admin),
    )
    assert response.status_code == 200, response.text


def _complete(
    client,
    admin,
    operation,
    *,
    surface="office",
    work_order_id=None,
    work_center_id=None,
):
    quantity = operation["laser_nest"]["planned_runs"]
    headers = pool_fixtures.headers_for(admin)
    if surface == "shop_floor":
        clocked = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=headers,
            json={
                "work_order_id": work_order_id,
                "operation_id": operation["id"],
                "work_center_id": work_center_id,
                "entry_type": "run",
            },
        )
        assert clocked.status_code == 200, clocked.text
        response = client.post(
            f"/api/v1/shop-floor/operations/{operation['id']}/complete",
            headers=headers,
            json={"quantity_complete": quantity},
        )
    else:
        response = client.post(
            f"/api/v1/work-orders/operations/{operation['id']}/complete",
            headers=headers,
            params={"quantity_complete": quantity},
        )
    assert response.status_code == 200, response.text


def _assert_cancelled_nest_unchanged(db, operation):
    nest = db.get(LaserNest, operation["laser_nest"]["id"])
    tombstone = db.get(WorkOrderOperation, operation["id"])
    assert nest.is_deleted is True
    assert nest.deleted_at is not None
    assert nest.work_order_operation_id == tombstone.id
    assert tombstone.status == OperationStatus.ON_HOLD
    assert float(tombstone.quantity_complete or 0) == 0
    assert float(nest.completed_runs or 0) == 0
    assert tombstone.actual_end is None
    assert tombstone.completed_by is None


@pytest.mark.parametrize("surface", ["office", "shop_floor"])
def test_last_live_nest_completes_work_order_without_completing_cancelled_nest(client, db_session, nest_pool, surface):
    admin, work_center, work_order, operations = nest_pool
    _cancel(client, admin, operations[2])

    for operation in operations[:2]:
        _complete(
            client,
            admin,
            operation,
            surface=surface,
            work_order_id=work_order["id"],
            work_center_id=work_center.id,
        )

    db_session.expire_all()
    completed = db_session.get(WorkOrder, work_order["id"])
    assert completed.status == WorkOrderStatus.COMPLETE
    assert float(completed.quantity_complete) == 5
    assert float(completed.quantity_ordered) == 5
    assert completed.current_operation_id is None
    assert completed.actual_end is not None
    _assert_cancelled_nest_unchanged(db_session, operations[2])


def test_repeated_reads_complete_stuck_header_from_live_nests_only(client, db_session, nest_pool):
    admin, _, work_order, operations = nest_pool
    _cancel(client, admin, operations[2])
    completed_at = datetime.utcnow() - timedelta(hours=1)
    for operation in operations[:2]:
        row = db_session.get(WorkOrderOperation, operation["id"])
        row.status = OperationStatus.COMPLETE
        row.quantity_complete = operation["laser_nest"]["planned_runs"]
        row.actual_start = completed_at - timedelta(hours=1)
        row.actual_end = completed_at
        row.completed_by = admin.id
        db_session.get(LaserNest, operation["laser_nest"]["id"]).completed_runs = row.quantity_complete
    stuck = db_session.get(WorkOrder, work_order["id"])
    stuck.status = WorkOrderStatus.IN_PROGRESS
    stuck.quantity_complete = 5
    stuck.actual_end = None
    stuck.current_operation_id = operations[2]["id"]
    db_session.commit()

    for _ in range(2):
        response = client.get(
            f"/api/v1/work-orders/{work_order['id']}",
            headers=pool_fixtures.headers_for(admin),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "complete"
        assert response.json()["quantity_complete"] == 5
        assert response.json()["operation_count"] == 2
        assert response.json()["operations_complete"] == 2
        assert response.json()["operation_progress_percent"] == 100
        cancelled = next(row for row in response.json()["operations"] if row["id"] == operations[2]["id"])
        assert cancelled["status"] == "on_hold"
        assert cancelled["cancelled_nest_id"] == operations[2]["laser_nest"]["id"]
        assert cancelled["laser_nest"] is None
        db_session.expire_all()
        completed = db_session.get(WorkOrder, work_order["id"])
        assert completed.status == WorkOrderStatus.COMPLETE
        assert completed.actual_end == completed_at
        assert completed.current_operation_id is None
        _assert_cancelled_nest_unchanged(db_session, operations[2])

    completion_audits = [
        row
        for row in db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == admin.company_id,
            AuditLog.resource_type == "work_order",
            AuditLog.resource_id == work_order["id"],
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
        if (row.new_values or {}).get("status") == "complete"
    ]
    assert len(completion_audits) == 1
    assert completion_audits[0].extra_data["source"] == "reconcile_on_read"


def test_cancelling_every_nest_does_not_vacuously_complete_work_order(client, db_session, nest_pool):
    admin, _, work_order, operations = nest_pool
    for operation in operations:
        _cancel(client, admin, operation)

    response = client.get(
        f"/api/v1/work-orders/{work_order['id']}",
        headers=pool_fixtures.headers_for(admin),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] != "complete"
    assert response.json()["quantity_complete"] == 0
    manual = client.post(
        f"/api/v1/work-orders/{work_order['id']}/complete",
        headers=pool_fixtures.headers_for(admin),
        params={"quantity_complete": 1},
    )
    assert manual.status_code == 409, manual.text
    assert "no active nests" in manual.json()["detail"]
    db_session.expire_all()
    assert db_session.get(WorkOrder, work_order["id"]).actual_end is None
    for operation in operations:
        _assert_cancelled_nest_unchanged(db_session, operation)


def test_live_held_nest_stays_on_hold_and_still_requires_resolution(client, db_session, nest_pool):
    admin, _, work_order, operations = nest_pool
    headers = pool_fixtures.headers_for(admin)
    held = client.put(f"/api/v1/shop-floor/operations/{operations[2]['id']}/hold", headers=headers)
    assert held.status_code == 200, held.text
    for operation in operations[:2]:
        _complete(client, admin, operation)

    response = client.get(f"/api/v1/work-orders/{work_order['id']}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_progress"
    assert response.json()["quantity_complete"] == 5
    force_complete = client.post(
        f"/api/v1/work-orders/{work_order['id']}/complete",
        headers=headers,
        params={"quantity_complete": 5},
    )
    assert force_complete.status_code == 409, force_complete.text
    assert "on hold" in force_complete.json()["detail"]
    db_session.expire_all()
    live_nest = db_session.get(LaserNest, operations[2]["laser_nest"]["id"])
    assert live_nest.is_deleted is False
    assert live_nest.operation.status == OperationStatus.ON_HOLD
    assert db_session.query(WorkOrderBlocker).filter(WorkOrderBlocker.work_order_id == work_order["id"]).count() == 0


def test_manual_work_order_completion_skips_cancelled_nest_tombstone(client, db_session, nest_pool):
    admin, _, work_order, operations = nest_pool
    _cancel(client, admin, operations[2])
    response = client.post(
        f"/api/v1/work-orders/{work_order['id']}/complete",
        headers=pool_fixtures.headers_for(admin),
        params={"quantity_complete": 5},
    )
    assert response.status_code == 200, response.text
    no_labor_operation_ids = {
        exception["reference_id"]
        for exception in response.json()["quality_exceptions"]
        if exception["code"] == "no_labor_recorded"
    }
    assert no_labor_operation_ids == {operation["id"] for operation in operations[:2]}
    db_session.expire_all()
    completed = db_session.get(WorkOrder, work_order["id"])
    assert completed.status == WorkOrderStatus.COMPLETE
    assert float(completed.quantity_complete) == 5
    assert completed.current_operation_id is None
    for operation in operations[:2]:
        assert db_session.get(WorkOrderOperation, operation["id"]).status == OperationStatus.COMPLETE
    _assert_cancelled_nest_unchanged(db_session, operations[2])


def test_cancelled_nest_partial_production_is_history_only_in_pool_rollup_and_reduction():
    work_order, operations = pool_fixtures._pool_wo(
        planned=[2, 3, 4],
        done=[1, 1, 2],
        ordered=5,
        existing=2,
    )
    cancelled = operations[2]
    cancelled.status = OperationStatus.ON_HOLD
    cancelled.laser_nest = LaserNest(is_deleted=True, planned_runs=4, completed_runs=2)
    entry = TimeEntry(
        company_id=work_order.company_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow(),
        quantity_produced=1,
    )

    assert pooled_quantity_complete(work_order, operations) == 2
    reduction = reduce_operation_produced_quantity(operations[0], work_order, [entry], 1, operations)

    assert reduction.work_order_quantity_complete_before == 2
    assert reduction.work_order_quantity_complete_after == 1
    assert float(work_order.quantity_complete) == 1
    assert pooled_quantity_complete(work_order, operations) == 1
    assert float(operations[0].quantity_complete) == 0
    assert float(entry.quantity_produced) == 0
    assert cancelled.status == OperationStatus.ON_HOLD
    assert float(cancelled.quantity_complete) == 2
    assert float(cancelled.laser_nest.completed_runs) == 2
