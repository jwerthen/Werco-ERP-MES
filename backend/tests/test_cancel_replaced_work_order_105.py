"""Cancellation compensates the replaced job without altering original production."""

from datetime import datetime, timedelta

import pytest

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.shipping import Shipment, ShipmentStatus
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from scripts import cancel_replaced_work_order_105 as incident
from tests.api.kiosk_test_helpers import make_user, make_work_center


@pytest.fixture
def replaced_job(db_session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    operator = make_user(db_session)
    center = make_work_center(db_session)
    part = Part(
        company_id=1,
        part_number="REPLACED-105",
        name="Replaced batch",
        part_type="assembly",
        unit_of_measure="each",
        is_active=True,
    )
    db_session.add(part)
    db_session.flush()
    started = incident.COMPLETION_AT - timedelta(minutes=10)
    wo = WorkOrder(
        id=105,
        company_id=1,
        work_order_number="WO-20260923-002",
        part_id=part.id,
        quantity_ordered=8,
        quantity_complete=8,
        status=WorkOrderStatus.COMPLETE,
        actual_start=started,
        actual_end=incident.COMPLETION_AT,
        actual_hours=1 / 6,
    )
    db_session.add(wo)
    db_session.flush()
    db_session.add_all(
        [
            WorkOrderOperation(
                id=op_id,
                company_id=1,
                work_order_id=105,
                work_center_id=center.id,
                sequence=10,
                operation_number=str((op_id - 841) * 10),
                name=f"Brake item {op_id}",
                component_part_id=part.id,
                component_quantity=incident.OPERATION_QUANTITIES[op_id - 842],
                quantity_complete=incident.OPERATION_QUANTITIES[op_id - 842],
                status=OperationStatus.COMPLETE,
                actual_start=started,
                actual_end=incident.COMPLETION_AT,
                started_by=operator.id,
                completed_by=operator.id,
            )
            for op_id in sorted(incident.OPERATION_IDS)
        ]
    )
    db_session.add(
        TimeEntry(
            id=495,
            company_id=1,
            user_id=operator.id,
            work_order_id=105,
            operation_id=842,
            work_center_id=center.id,
            entry_type=TimeEntryType.RUN,
            quantity_produced=8,
            clock_in=started,
            clock_out=datetime(2026, 9, 23, 14, 41, 7, 496684),
            duration_hours=1 / 6,
        )
    )
    stock = InventoryItem(
        id=257,
        company_id=1,
        part_id=part.id,
        location="FG",
        lot_number="LOT-105",
        quantity_on_hand=8,
        quantity_available=8,
        quantity_allocated=0,
    )
    db_session.add(stock)
    db_session.flush()
    db_session.add(
        InventoryTransaction(
            id=518,
            company_id=1,
            inventory_item_id=257,
            part_id=part.id,
            transaction_type=TransactionType.RECEIVE,
            quantity=8,
            to_location="FG",
            lot_number="LOT-105",
            reference_type="work_order",
            reference_id=105,
            created_by=admin.id,
        )
    )
    AuditService(db_session, operator).log_required("COMPLETE_OPERATION", "work_order_operation", resource_id=842)
    db_session.commit()
    return wo, stock, admin, operator


def _snapshot(db):
    return {
        model.__tablename__: [
            {col.name: getattr(row, col.name) for col in model.__table__.columns}
            for row in db.query(model).order_by(model.id)
        ]
        for model in [WorkOrder, WorkOrderOperation, TimeEntry, InventoryItem, InventoryTransaction, AuditLog, Shipment]
    }


def _apply(db, actor_id):
    # sqlite3 needs explicit BEGIN before initial SAVEPOINT to model PostgreSQL.
    connection = db.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    try:
        result = incident.cancel(db, apply=True, actor_id=actor_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def test_preview_cancel_preserves_production_and_repeat_is_noop(db_session, replaced_job):
    wo, stock, admin, _operator = replaced_job
    before = _snapshot(db_session)
    assert incident.cancel(db_session)["applied"] is False
    assert _snapshot(db_session) == before
    assert _apply(db_session, admin.id)["applied"] is True
    after = _snapshot(db_session)
    assert after["work_order_operations"] == before["work_order_operations"]
    assert after["time_entries"] == before["time_entries"]
    assert after["inventory_transactions"][0] == before["inventory_transactions"][0]
    assert after["audit_logs"][0] == before["audit_logs"][0]
    assert wo.status == WorkOrderStatus.CANCELLED
    assert wo.quantity_complete == 0
    assert wo.actual_end is None and wo.current_operation_id is None
    assert wo.actual_hours == 1 / 6
    assert stock.quantity_on_hand == stock.quantity_available == 0
    correction = after["inventory_transactions"][1]
    assert correction["quantity"] == -8 and correction["created_by"] == admin.id
    audit = db_session.query(AuditLog).filter_by(action=incident.ACTION).one()
    assert audit.old_values["status"] == "complete" and audit.new_values["status"] == "cancelled"
    assert audit.user_id == admin.id and audit.extra_data["inventory_correction_id"] == correction["id"]
    assert _apply(db_session, admin.id)["already_cancelled"] is True
    assert _snapshot(db_session) == after


@pytest.mark.parametrize("change", ["shipment", "backflush", "labor", "stock", "operation"])
def test_changed_dependencies_refuse_preview_and_apply_without_mutation(db_session, replaced_job, change):
    _wo, stock, admin, operator = replaced_job
    if change == "shipment":
        db_session.add(
            Shipment(
                company_id=1,
                work_order_id=105,
                shipment_number="PENDING-105",
                status=ShipmentStatus.PENDING,
                quantity_shipped=8,
                created_by=admin.id,
            )
        )
    elif change == "backflush":
        db_session.add(
            InventoryTransaction(
                company_id=1,
                part_id=stock.part_id,
                transaction_type=TransactionType.ISSUE,
                quantity=8,
                reference_type="work_order_backflush",
                reference_id=105,
                created_by=admin.id,
            )
        )
    elif change == "labor":
        source = db_session.get(WorkOrderOperation, 842)
        db_session.add(
            TimeEntry(
                company_id=1,
                user_id=operator.id,
                work_order_id=105,
                operation_id=842,
                work_center_id=source.work_center_id,
                entry_type=TimeEntryType.RUN,
                clock_in=datetime.utcnow(),
            )
        )
    elif change == "stock":
        stock.quantity_allocated = 1
    else:
        db_session.get(WorkOrderOperation, 843).quantity_complete = 63
    db_session.commit()
    before = _snapshot(db_session)
    with pytest.raises(ValueError):
        incident.cancel(db_session)
    with pytest.raises(ValueError):
        _apply(db_session, admin.id)
    assert _snapshot(db_session) == before


def test_non_admin_cannot_apply(db_session, replaced_job):
    _wo, _stock, _admin, operator = replaced_job
    before = _snapshot(db_session)
    with pytest.raises(ValueError, match="active requesting admin"):
        _apply(db_session, operator.id)
    assert _snapshot(db_session) == before


def test_cancellation_audit_failure_rolls_back_receipt_and_status(db_session, replaced_job, monkeypatch):
    _wo, _stock, admin, _operator = replaced_job
    before = _snapshot(db_session)
    original = AuditService.log_required

    def fail(self, action, resource_type, **kwargs):
        if action == incident.ACTION:
            raise RuntimeError("cancellation audit failed after inventory compensation")
        return original(self, action, resource_type, **kwargs)

    monkeypatch.setattr(AuditService, "log_required", fail)
    with pytest.raises(RuntimeError):
        _apply(db_session, admin.id)
    assert _snapshot(db_session) == before
