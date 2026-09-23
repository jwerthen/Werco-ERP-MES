"""Production belongs to an operation ID, never to its shared routing sequence."""

from datetime import datetime, timedelta

import pytest

from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from tests.api.kiosk_test_helpers import make_user, make_wo_with_operation, make_work_center, user_headers

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def _same_sequence_batch(db, *, identical_labels):
    operator = make_user(db)
    work_center = make_work_center(db, name="Press brake")
    work_order, source = make_wo_with_operation(
        db, work_center=work_center, quantity_ordered=2, sequential_operations=False
    )
    source.name = "Inlets out"
    source.operation_group = "BEND"
    operations = [source]
    for name in ["Side panels", "Bottom panels"]:
        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=source.sequence,
            operation_number=source.operation_number,
            operation_group=source.operation_group,
            name=source.name if identical_labels else name,
            status=OperationStatus.READY,
            company_id=work_order.company_id,
        )
        db.add(operation)
        operations.append(operation)
    if not identical_labels:
        for index, operation in enumerate(operations):
            component = Part(
                part_number=f"BRAKE-{work_order.id}-{index}",
                name=operation.name,
                part_type="manufactured",
                unit_of_measure="each",
                is_active=True,
                company_id=work_order.company_id,
            )
            db.add(component)
            db.flush()
            operation.component_part_id = component.id
            operation.component_quantity = [2, 7, 11][index]
    db.commit()
    headers = {
        **user_headers(operator),
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148 Safari/604.1",
    }
    return operator, work_order, source, operations[1:], headers


def _assert_untouched_siblings(db, siblings):
    for sibling in siblings:
        db.refresh(sibling)
        assert sibling.status == OperationStatus.READY
        assert sibling.quantity_complete == 0
        assert sibling.quantity_scrapped == 0
        assert sibling.actual_start is None
        assert sibling.actual_end is None
        assert sibling.started_by is None
        assert sibling.completed_by is None
        assert db.query(TimeEntry).filter_by(operation_id=sibling.id).count() == 0
        assert (
            db.query(AuditLog)
            .filter_by(resource_type="work_order_operation", resource_id=sibling.id, action="STATUS_CHANGE")
            .count()
            == 0
        )


def _read_repeatedly(client, db, work_order, source, siblings, headers):
    db.refresh(source)
    source_evidence = (source.quantity_complete, source.actual_start, source.actual_end, source.completed_by)
    for _ in range(2):
        detail = client.get(f"/api/v1/work-orders/{work_order.id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "in_progress"
        listing = client.get("/api/v1/work-orders/", headers=headers)
        assert listing.status_code == 200, listing.text
        summary = next(item for item in listing.json() if item["id"] == work_order.id)
        assert summary["operation_count"] == 3
        assert summary["operations_complete"] == 1
        assert summary["operation_progress_percent"] == 33.3
        floor = client.get("/api/v1/shop-floor/operations", headers=headers)
        assert floor.status_code == 200, floor.text
        floor_ids = {operation["id"] for operation in floor.json()["operations"]}
        assert source.id not in floor_ids
        assert {sibling.id for sibling in siblings} <= floor_ids
        _assert_untouched_siblings(db, siblings)
        db.refresh(source)
        assert source.status == OperationStatus.COMPLETE
        assert (
            source.quantity_complete,
            source.actual_start,
            source.actual_end,
            source.completed_by,
        ) == source_evidence
        db.refresh(work_order)
        assert work_order.status == WorkOrderStatus.IN_PROGRESS
        assert work_order.actual_end is None


@pytest.mark.parametrize("identical_labels", [False, True], ids=["distinct-components", "identical-labels"])
@pytest.mark.parametrize("completion_verb", ["complete", "clock-out"])
def test_mobile_completion_and_reads_never_complete_same_sequence_siblings(
    client, db_session, identical_labels, completion_verb
):
    operator, work_order, source, siblings, headers = _same_sequence_batch(
        db_session, identical_labels=identical_labels
    )
    clock_in = client.post(
        "/api/v1/shop-floor/clock-in",
        headers=headers,
        json={
            "work_order_id": work_order.id,
            "operation_id": source.id,
            "work_center_id": source.work_center_id,
            "entry_type": "run",
        },
    )
    assert clock_in.status_code == 200, clock_in.text
    if completion_verb == "complete":
        response = client.post(
            f"/api/v1/shop-floor/operations/{source.id}/complete",
            headers=headers,
            json={"quantity_complete": 2},
        )
    else:
        response = client.post(
            f"/api/v1/shop-floor/clock-out/{clock_in.json()['id']}",
            headers=headers,
            json={"quantity_produced": 2, "quantity_scrapped": 0},
        )
    assert response.status_code == 200, response.text
    _assert_untouched_siblings(db_session, siblings)
    _read_repeatedly(client, db_session, work_order, source, siblings, headers)


@pytest.mark.parametrize("identical_labels", [False, True], ids=["distinct-components", "identical-labels"])
def test_read_reconciles_only_operation_with_its_own_closed_labor(client, db_session, identical_labels):
    operator, work_order, source, siblings, headers = _same_sequence_batch(
        db_session, identical_labels=identical_labels
    )
    now = datetime.utcnow()
    db_session.add(
        TimeEntry(
            user_id=operator.id,
            work_order_id=work_order.id,
            operation_id=source.id,
            work_center_id=source.work_center_id,
            entry_type=TimeEntryType.RUN,
            clock_in=now - timedelta(minutes=10),
            clock_out=now,
            duration_hours=1 / 6,
            quantity_produced=2,
            company_id=work_order.company_id,
        )
    )
    db_session.commit()
    response = client.get(f"/api/v1/work-orders/{work_order.id}", headers=headers)
    assert response.status_code == 200, response.text
    db_session.refresh(source)
    assert source.status == OperationStatus.COMPLETE
    assert source.quantity_complete == 2
    assert source.completed_by == operator.id
    assert source.actual_end == now
    _read_repeatedly(client, db_session, work_order, source, siblings, headers)
