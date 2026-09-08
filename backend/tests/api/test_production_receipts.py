"""Production retries return committed evidence without repeating ledger effects."""

from datetime import datetime

import pytest
from sqlalchemy.orm.exc import StaleDataError

from app.models.audit_log import AuditLog
from app.models.production_receipt import ProductionReceipt
from app.models.quality import NonConformanceReport
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.work_order import OperationStatus
from tests.api.kiosk_test_helpers import make_user, user_headers
from tests.api.test_kiosk_scrap_ncr import clocked_in_job, production_url


@pytest.mark.parametrize("rework", [False, True])
def test_replay_preserves_good_scrap_rework_ncr_and_audit_exactly_once(client, db_session, rework):
    operator, headers, _, operation = clocked_in_job(client, db_session)
    entry = db_session.query(TimeEntry).filter(TimeEntry.user_id == operator.id, TimeEntry.clock_out.is_(None)).one()
    if rework:
        entry.entry_type = TimeEntryType.REWORK
        db_session.commit()
    payload = {
        "request_id": "receipt-batch-0001",
        "quantity_complete_delta": 3,
        "quantity_scrapped_delta": 2,
        "scrap_reason": "Weld porosity",
        "open_ncr": True,
    }
    first = client.post(production_url(operation.id), headers=headers, json=payload)
    assert first.status_code == 200, first.text
    audits = db_session.query(AuditLog).count()
    again = client.post(production_url(operation.id), headers=headers, json=payload)
    assert again.status_code == 200, again.text
    assert again.json() == {**first.json(), "replayed": True}
    db_session.rollback()  # Receipt and evidence must survive the request's transaction.
    db_session.refresh(operation)
    db_session.refresh(entry)
    assert operation.quantity_complete == 3
    assert operation.quantity_scrapped == 2
    assert operation.quantity_reworked == (3 if rework else 0)
    assert entry.quantity_produced == 3
    assert entry.quantity_scrapped == 2
    assert db_session.query(NonConformanceReport).count() == 1
    assert db_session.query(AuditLog).count() == audits
    assert db_session.query(ProductionReceipt).count() == 1


def test_historical_receipt_recovers_after_operator_checkout_and_job_completion(client, db_session):
    operator, headers, _, operation = clocked_in_job(client, db_session)
    payload = {"request_id": "receipt-batch-0002", "quantity_complete_delta": 3}
    first = client.post(production_url(operation.id), headers=headers, json=payload)
    assert first.status_code == 200
    entry = db_session.query(TimeEntry).filter(TimeEntry.user_id == operator.id, TimeEntry.clock_out.is_(None)).one()
    entry.clock_out = datetime.utcnow()
    operation.status = OperationStatus.COMPLETE
    db_session.commit()
    replay = client.post(production_url(operation.id), headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.json() == {**first.json(), "replayed": True}
    db_session.refresh(operation)
    assert operation.status == OperationStatus.COMPLETE
    assert operation.quantity_complete == 3


def test_changed_payload_or_operator_cannot_reuse_committed_request(client, db_session):
    _, headers, _, operation = clocked_in_job(client, db_session)
    payload = {"request_id": "receipt-batch-0003", "quantity_complete_delta": 3}
    assert client.post(production_url(operation.id), headers=headers, json=payload).status_code == 200
    assert (
        client.post(
            production_url(operation.id), headers=headers, json={**payload, "quantity_complete_delta": 4}
        ).status_code
        == 409
    )
    another = make_user(db_session)
    assert client.post(production_url(operation.id), headers=user_headers(another), json=payload).status_code == 409
    db_session.refresh(operation)
    assert operation.quantity_complete == 3


def test_new_request_and_legacy_keyless_reports_remain_separate_submissions(client, db_session):
    _, headers, _, operation = clocked_in_job(client, db_session)
    for request_id in ["receipt-new-0001", "receipt-new-0002", None, None]:
        payload = {"quantity_complete_delta": 1}
        if request_id:
            payload["request_id"] = request_id
        result = client.post(production_url(operation.id), headers=headers, json=payload)
        assert result.status_code == 200, result.text
        if not request_id:
            assert "request_id" not in result.json()
    db_session.refresh(operation)
    assert operation.quantity_complete == 4
    assert db_session.query(ProductionReceipt).count() == 2


def test_failed_commit_rolls_back_receipt_and_quantity_together(client, db_session, monkeypatch):
    _, headers, _, operation = clocked_in_job(client, db_session)
    operation_id = operation.id
    original = db_session.commit

    def reject_commit():
        raise StaleDataError("concurrent operation update")

    monkeypatch.setattr(db_session, "commit", reject_commit)
    payload = {"request_id": "receipt-rollback-0001", "quantity_complete_delta": 2}
    response = client.post(production_url(operation_id), headers=headers, json=payload)
    assert response.status_code == 409, response.text
    monkeypatch.setattr(db_session, "commit", original)
    assert db_session.query(ProductionReceipt).count() == 0
    db_session.refresh(operation)
    assert operation.quantity_complete == 0
    retry = client.post(production_url(operation_id), headers=headers, json=payload)
    assert retry.status_code == 200, retry.text
    assert db_session.query(ProductionReceipt).count() == 1


def test_receipt_from_another_company_is_not_replayed(client, db_session):
    _, headers, _, operation = clocked_in_job(client, db_session)
    payload = {"request_id": "receipt-tenant-0001", "quantity_complete_delta": 1}
    assert client.post(production_url(operation.id), headers=headers, json=payload).status_code == 200
    another_company_operator = make_user(db_session, company_id=2)
    result = client.post(production_url(operation.id), headers=user_headers(another_company_operator), json=payload)
    assert result.status_code == 404, result.text
    assert db_session.query(ProductionReceipt).count() == 1


def test_identical_request_ids_are_independent_between_companies(client, db_session):
    from tests.api.kiosk_test_helpers import make_wo_with_operation, make_work_center

    _, headers_a, _, op_a = clocked_in_job(client, db_session)
    operator_b = make_user(db_session, company_id=2)
    center_b = make_work_center(db_session, company_id=2)
    wo_b, op_b = make_wo_with_operation(db_session, company_id=2, work_center=center_b)
    headers_b = user_headers(operator_b)
    assert (
        client.post(
            '/api/v1/shop-floor/clock-in',
            headers=headers_b,
            json={"work_order_id": wo_b.id, "operation_id": op_b.id, "work_center_id": center_b.id},
        ).status_code
        == 200
    )
    payload = {"request_id": "same-request-across-companies", "quantity_complete_delta": 2}
    for op, headers in [(op_a, headers_a), (op_b, headers_b)]:
        response = client.post(production_url(op.id), headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['replayed'] is False
        assert client.post(production_url(op.id), headers=headers, json=payload).json()['replayed'] is True
    db_session.expire_all()
    assert op_a.quantity_complete == 2 and op_b.quantity_complete == 2
    assert (
        db_session.query(ProductionReceipt).filter(ProductionReceipt.request_id == payload['request_id']).count() == 2
    )


def test_receipt_lookup_takes_company_scoped_transaction_lock_before_reading(db_session, monkeypatch):
    from app.api.endpoints.shop_floor import ProductionReportRequest
    from app.services import production_receipt_service

    calls = []
    monkeypatch.setattr(
        production_receipt_service,
        'acquire_generator_lock',
        lambda db, namespace, company: calls.append((db, namespace, company)),
    )
    payload = ProductionReportRequest(request_id='lock-contract-receipt', quantity_complete_delta=1)
    assert production_receipt_service.find_production_replay(db_session, 1, 7, 31, payload) is None
    assert calls == [(db_session, 'production_receipt:lock-contract-receipt', 1)]
