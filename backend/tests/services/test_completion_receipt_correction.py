"""An erroneous completion can be compensated without losing later real output."""

import pytest

from app.db.ledger_filter import work_order_ledger_filter
from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.work_order import WorkOrderStatus
from app.services.audit_service import AuditService, AuditWriteError
from app.services.completion_inventory_service import (
    receive_finished_goods_for_work_order,
)
from app.services.completion_receipt_correction_service import (
    RECEIPT_CORRECTION_REFERENCE,
    reverse_erroneous_finished_goods_receipt,
)
from tests.api.test_completion_inventory_batch6 import make_part, make_user, make_wo


@pytest.fixture
def received(db_session):
    actor = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(
        db_session,
        part,
        quantity_ordered=8,
        quantity_complete=8,
        status_=WorkOrderStatus.COMPLETE,
    )
    audit = AuditService(db_session, actor)
    receipt = receive_finished_goods_for_work_order(db_session, wo, company_id=1, user_id=actor.id, audit=audit)
    db_session.commit()
    if db_session.get_bind().dialect.name == "sqlite":
        # sqlite3 legacy mode does not BEGIN on SELECT; a SAVEPOINT would otherwise
        # become its own transaction, unlike production PostgreSQL's outer unit.
        db_session.connection().exec_driver_sql("BEGIN")
    stock = db_session.get(InventoryItem, receipt.inventory_item_id)
    return wo, receipt, stock, actor, audit


def reverse(db, received, **overrides):
    wo, receipt, _stock, actor, audit = received
    kwargs = dict(
        expected_receipt_id=receipt.id,
        expected_quantity=8,
        reason="WO106 falsely completed from sibling evidence",
        user_id=actor.id,
        company_id=1,
        audit=audit,
    )
    kwargs.update(overrides)
    return reverse_erroneous_finished_goods_receipt(db, wo, **kwargs)


def receive(db, received):
    wo, _receipt, _stock, actor, audit = received
    return receive_finished_goods_for_work_order(db, wo, company_id=1, user_id=actor.id, audit=audit)


def test_reversal_preserves_original_and_audits_compensating_movement(db_session, received):
    wo, receipt, stock, _actor, _audit = received
    original_notes = receipt.notes
    correction = reverse(db_session, received)
    assert correction.quantity == -8
    assert correction.reference_type == RECEIPT_CORRECTION_REFERENCE
    assert correction.reference_id == wo.id
    assert correction.created_by == received[3].id
    assert stock.quantity_on_hand == stock.quantity_available == 0
    assert receipt.quantity == 8
    assert receipt.notes == original_notes
    assert db_session.query(InventoryTransaction).count() == 2
    assert db_session.query(AuditLog).filter(AuditLog.resource_id == correction.id).count() >= 1
    assert reverse(db_session, received).id == correction.id
    assert db_session.query(InventoryTransaction).count() == 2


@pytest.mark.parametrize("finished_quantity", [8, 6])
def test_genuine_completion_restores_only_real_output_once(db_session, received, finished_quantity):
    wo, receipt, stock, _actor, _audit = received
    reverse(db_session, received)
    wo.status = WorkOrderStatus.IN_PROGRESS
    wo.quantity_complete = 0
    assert receive(db_session, received) is None
    assert stock.quantity_on_hand == 0
    wo.status = WorkOrderStatus.COMPLETE
    wo.quantity_complete = finished_quantity
    restored = receive(db_session, received)
    assert restored.quantity == finished_quantity
    assert restored.reference_type == RECEIPT_CORRECTION_REFERENCE
    assert stock.quantity_on_hand == stock.quantity_available == finished_quantity
    assert receive(db_session, received) is None
    assert db_session.query(InventoryTransaction).count() == 3
    assert db_session.query(InventoryTransaction).filter(work_order_ledger_filter(wo.id, 1)).count() == 3
    assert receipt.quantity == 8


@pytest.mark.parametrize("transaction_type", [TransactionType.ADJUST, TransactionType.SHIP])
def test_ordinary_stock_movement_does_not_enable_another_receipt(db_session, received, transaction_type):
    wo, receipt, stock, actor, _audit = received
    db_session.add(
        InventoryTransaction(
            company_id=1,
            part_id=receipt.part_id,
            inventory_item_id=stock.id,
            transaction_type=transaction_type,
            quantity=-8,
            reference_type="shipment",
            reference_id=wo.id,
            created_by=actor.id,
        )
    )
    stock.quantity_on_hand = stock.quantity_available = 0
    db_session.flush()
    assert receive(db_session, received) is None
    assert stock.quantity_on_hand == 0
    assert db_session.query(InventoryTransaction).count() == 2


def test_ship_after_restoration_does_not_replenish(db_session, received):
    _wo, receipt, stock, actor, _audit = received
    reverse(db_session, received)
    receive(db_session, received)
    db_session.add(
        InventoryTransaction(
            company_id=1,
            part_id=receipt.part_id,
            inventory_item_id=stock.id,
            transaction_type=TransactionType.SHIP,
            quantity=-8,
            reference_type="shipment",
            reference_id=123,
            created_by=actor.id,
        )
    )
    stock.quantity_on_hand = stock.quantity_available = 0
    db_session.flush()
    assert receive(db_session, received) is None
    assert stock.quantity_on_hand == 0


@pytest.mark.parametrize("mismatch", ["quantity", "allocated", "movement", "company", "receipt"])
def test_correction_refuses_changed_or_out_of_scope_stock(db_session, received, mismatch):
    _wo, receipt, stock, actor, _audit = received
    overrides = {}
    if mismatch == "quantity":
        stock.quantity_on_hand = 7
    elif mismatch == "allocated":
        stock.quantity_allocated = 1
    elif mismatch == "movement":
        db_session.add(
            InventoryTransaction(
                company_id=1,
                part_id=receipt.part_id,
                inventory_item_id=stock.id,
                transaction_type=TransactionType.ADJUST,
                quantity=0,
                created_by=actor.id,
            )
        )
    elif mismatch == "company":
        overrides["company_id"] = 2
    else:
        overrides["expected_receipt_id"] = receipt.id + 1
    db_session.flush()
    with pytest.raises(ValueError):
        reverse(db_session, received, **overrides)
    assert (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.reference_type == RECEIPT_CORRECTION_REFERENCE,
        )
        .count()
        == 0
    )


def test_repair_rollback_preserves_receipt_and_stock(db_session, received):
    _wo, receipt, stock, _actor, _audit = received
    stock_id = stock.id
    reverse(db_session, received)
    db_session.rollback()
    assert db_session.get(InventoryItem, stock_id).quantity_on_hand == 8
    assert db_session.get(InventoryTransaction, receipt.id).quantity == 8
    assert db_session.query(InventoryTransaction).count() == 1


@pytest.mark.parametrize("failing_audit", [1, 2])
def test_audit_failure_rolls_back_compensation_even_if_caller_catches(db_session, received, monkeypatch, failing_audit):
    _wo, _receipt, stock, _actor, audit = received
    original_log = audit.log
    calls = 0

    def fail_one(*args, **kwargs):
        nonlocal calls
        calls += 1
        return None if calls == failing_audit else original_log(*args, **kwargs)

    monkeypatch.setattr(audit, "log", fail_one)
    with pytest.raises(AuditWriteError):
        reverse(db_session, received)
    db_session.commit()
    assert db_session.get(InventoryItem, stock.id).quantity_on_hand == 8
    assert db_session.query(InventoryTransaction).count() == 1


@pytest.mark.parametrize("invalid_context", ["actor", "audit"])
def test_correction_requires_actor_and_company_audit(db_session, received, invalid_context):
    overrides = {"user_id": None} if invalid_context == "actor" else {"audit": AuditService(db_session, company_id=2)}
    with pytest.raises(ValueError):
        reverse(db_session, received, **overrides)
    assert db_session.query(InventoryTransaction).count() == 1


def test_malformed_correction_cannot_enable_restock(db_session, received):
    _wo, _receipt, stock, _actor, _audit = received
    correction = reverse(db_session, received)
    correction.quantity = -4  # a ledger shape that this repair service never writes
    db_session.flush()
    with pytest.raises(ValueError):
        receive(db_session, received)
    assert stock.quantity_on_hand == 0
    assert db_session.query(InventoryTransaction).count() == 2
