"""The incident repair must preserve real work and commit compensation atomically."""

from datetime import datetime, timedelta

import pytest

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.shipping import Shipment, ShipmentStatus
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services import completion_receipt_correction_service as receipt_service
from app.services.audit_service import AuditService
from scripts import repair_work_order_106_completion as incident
from tests.api.kiosk_test_helpers import make_user, make_work_center


@pytest.fixture
def corrupted_work_order(db_session):
    actor = make_user(db_session, role=UserRole.ADMIN)
    operator = make_user(db_session)
    center = make_work_center(db_session, name="Press brake")
    part = Part(
        part_number="INCIDENT-106",
        name="Press brake batch",
        part_type="assembly",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(part)
    db_session.flush()
    started = datetime(2026, 9, 23, 14)
    finished = started + timedelta(minutes=10)
    work_order = WorkOrder(
        id=106,
        company_id=1,
        work_order_number="WO-20260923-003",
        part_id=part.id,
        quantity_ordered=8,
        quantity_complete=8,
        sequential_operations=False,
        status=WorkOrderStatus.COMPLETE,
        actual_start=started,
        actual_end=finished,
        actual_hours=1 / 6,
        lot_number="LOT-WO-20260923-003",
    )
    db_session.add(work_order)
    db_session.flush()
    audit = AuditService(db_session, actor)
    for op_id in range(863, 884):
        quantity = 8 if op_id == 882 else 8 * (op_id - 862)
        operation = WorkOrderOperation(
            id=op_id,
            company_id=1,
            work_order_id=106,
            work_center_id=center.id,
            component_part_id=part.id,
            component_quantity=quantity,
            sequence=10,
            operation_number="10",
            name="Inlets out" if op_id == 882 else f"Brake item {op_id}",
            status=OperationStatus.COMPLETE,
            quantity_complete=quantity,
            actual_start=started,
            actual_end=finished,
            started_by=operator.id,
            completed_by=operator.id,
            actual_run_hours=1 / 6 if op_id == 882 else 0,
        )
        db_session.add(operation)
        db_session.flush()
        audit.log_required(
            "COMPLETE_OPERATION" if op_id == 882 else "STATUS_CHANGE",
            "work_order_operation",
            resource_id=op_id,
            old_values={"status": "ready"},
            new_values={"status": "complete"},
            extra_data={"source": "mobile" if op_id == 882 else "reconcile_on_read", "time_entry_ids": []},
        )
    db_session.add(
        TimeEntry(
            id=496,
            company_id=1,
            user_id=operator.id,
            work_order_id=106,
            operation_id=882,
            work_center_id=center.id,
            entry_type=TimeEntryType.RUN,
            clock_in=started,
            clock_out=finished,
            duration_hours=1 / 6,
            quantity_produced=8,
        )
    )
    stock = InventoryItem(
        id=258,
        company_id=1,
        part_id=part.id,
        location="FG",
        lot_number=work_order.lot_number,
        quantity_on_hand=8,
        quantity_available=8,
        quantity_allocated=0,
    )
    db_session.add(stock)
    db_session.flush()
    db_session.add(
        InventoryTransaction(
            id=519,
            company_id=1,
            part_id=part.id,
            inventory_item_id=stock.id,
            transaction_type=TransactionType.RECEIVE,
            quantity=8,
            to_location=stock.location,
            lot_number=stock.lot_number,
            reference_type="work_order",
            reference_id=106,
            reference_number=work_order.work_order_number,
            created_by=actor.id,
        )
    )
    db_session.commit()
    return work_order, actor, stock


def _snapshot(db):
    """Include source labor and immutable audit/ledger records in rollback checks."""
    return {
        model.__tablename__: [
            {column.name: getattr(row, column.name) for column in model.__table__.columns}
            for row in db.query(model).order_by(model.id).all()
        ]
        for model in [WorkOrder, WorkOrderOperation, TimeEntry, InventoryItem, InventoryTransaction, AuditLog]
    }


def _apply(db, actor_id):
    # Match the script's transaction owner, including rollback on any failure.
    # sqlite3 defers BEGIN past SELECT and treats an initial SAVEPOINT as its
    # outer transaction. Begin explicitly to model Postgres, where the script's
    # first SELECT already opens the outer transaction before receipt savepoints.
    connection = db.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    try:
        result = incident.repair(db, apply=True, actor_id=actor_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def test_preview_is_read_only(db_session, corrupted_work_order):
    before = _snapshot(db_session)
    preview = incident.repair(db_session)
    assert preview["applied"] is False
    assert preview["preserved_operation_id"] == 882
    assert len(preview["restored_ready_operation_ids"]) == 20
    assert _snapshot(db_session) == before


def test_repair_preserves_source_history_and_repeats_as_noop(db_session, corrupted_work_order):
    work_order, actor, stock = corrupted_work_order
    before = _snapshot(db_session)
    result = _apply(db_session, actor.id)
    assert result["applied"] is True
    after = _snapshot(db_session)
    assert after["time_entries"] == before["time_entries"]
    assert next(row for row in after["work_order_operations"] if row["id"] == 882) == next(
        row for row in before["work_order_operations"] if row["id"] == 882
    )
    assert after["audit_logs"][: len(before["audit_logs"])] == before["audit_logs"]
    assert after["inventory_transactions"][0] == before["inventory_transactions"][0]
    assert work_order.status == WorkOrderStatus.IN_PROGRESS
    assert work_order.quantity_complete == 0
    assert work_order.actual_end is None
    assert work_order.actual_hours == 1 / 6
    assert work_order.current_operation_id in incident.SIBLING_IDS
    assert stock.quantity_on_hand == stock.quantity_available == 0
    for operation in db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id.in_(incident.SIBLING_IDS)):
        assert operation.status == OperationStatus.READY
        assert operation.quantity_complete == 0
        assert operation.actual_start is None and operation.actual_end is None
        assert operation.started_by is None and operation.completed_by is None
    corrections = db_session.query(AuditLog).filter_by(action=incident.ACTION).all()
    assert len(corrections) == 21
    assert {
        row.resource_id for row in corrections if row.resource_type == "work_order_operation"
    } == incident.SIBLING_IDS
    header_audit = next(row for row in corrections if row.resource_type == "work_order")
    assert header_audit.old_values["status"] == "complete"
    assert header_audit.new_values["status"] == "in_progress"
    assert header_audit.extra_data["inventory_correction_id"] is not None
    assert _apply(db_session, actor.id)["already_repaired"] is True
    assert _snapshot(db_session) == after


@pytest.mark.parametrize("activity", ["labor", "production", "audit", "inventory", "shipment", "backflush"])
def test_repair_refuses_new_activity_without_changes(db_session, corrupted_work_order, activity):
    _work_order, actor, stock = corrupted_work_order
    sibling = db_session.get(WorkOrderOperation, 863)
    if activity == "labor":
        db_session.add(
            TimeEntry(
                company_id=1,
                user_id=actor.id,
                work_order_id=106,
                operation_id=863,
                work_center_id=sibling.work_center_id,
                entry_type=TimeEntryType.RUN,
                clock_in=datetime.utcnow(),
            )
        )
    elif activity == "production":
        sibling.last_reported_at = datetime.utcnow()
    elif activity == "audit":
        AuditService(db_session, actor).log_required("UPDATE", "work_order_operation", resource_id=863)
    elif activity == "shipment":
        db_session.add(
            Shipment(
                company_id=1,
                work_order_id=106,
                shipment_number="INCIDENT-106-PENDING",
                status=ShipmentStatus.PENDING,
                quantity_shipped=8,
                created_by=actor.id,
            )
        )
    elif activity == "backflush":
        db_session.add(
            InventoryTransaction(
                company_id=1,
                part_id=stock.part_id,
                transaction_type=TransactionType.ISSUE,
                quantity=8,
                reference_type="work_order_backflush",
                reference_id=106,
                created_by=actor.id,
            )
        )
    else:
        db_session.add(
            InventoryTransaction(
                company_id=1,
                part_id=stock.part_id,
                inventory_item_id=stock.id,
                transaction_type=TransactionType.ADJUST,
                quantity=0,
                reference_type="manual",
                created_by=actor.id,
            )
        )
    db_session.commit()
    before = _snapshot(db_session)
    with pytest.raises(ValueError):
        _apply(db_session, actor.id)
    assert _snapshot(db_session) == before


@pytest.mark.parametrize("failure", ["receipt", "receipt-audit", "operation-audit", "header-audit"])
def test_repair_rolls_back_every_change_when_a_required_step_fails(
    db_session, corrupted_work_order, monkeypatch, failure
):
    _work_order, actor, _stock = corrupted_work_order
    before = _snapshot(db_session)
    if failure == "receipt":
        original = receipt_service.reverse_erroneous_finished_goods_receipt

        def fail_after_stock_reversal(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("receipt failure after stock and audit writes")

        monkeypatch.setattr(receipt_service, "reverse_erroneous_finished_goods_receipt", fail_after_stock_reversal)
    else:
        original = AuditService.log_required

        def fail_audit(self, action, resource_type, **kwargs):
            if failure == "receipt-audit" and resource_type == "inventory":
                raise RuntimeError("required receipt audit failure")
            resource = "work_order" if failure == "header-audit" else "work_order_operation"
            if action == incident.ACTION and resource_type == resource:
                raise RuntimeError("required audit failure")
            return original(self, action, resource_type, **kwargs)

        monkeypatch.setattr(AuditService, "log_required", fail_audit)
    with pytest.raises(RuntimeError):
        _apply(db_session, actor.id)
    assert _snapshot(db_session) == before
