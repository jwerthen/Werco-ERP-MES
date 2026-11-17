"""Atomic receipts, retry identity, real certificate bytes, and supplier follow-up."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Insert, Select, event
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.purchasing import POReceipt, POStatus, PurchaseOrderLine, ReceivingDeliveryBatch
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.services.audit_service import AuditService
from tests.api.test_receiving_compliance import _ensure_company, headers_for, make_po_line, make_user


@pytest.fixture(autouse=True)
def no_print(monkeypatch):
    enqueue = Mock()
    monkeypatch.setattr('app.api.endpoints.receiving.enqueue_job_best_effort', enqueue)
    return enqueue


def setup_delivery(db):
    user = make_user(db, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db, company_id=1)
    second = PurchaseOrderLine(
        company_id=1,
        purchase_order_id=line.purchase_order_id,
        part_id=line.part_id,
        line_number=2,
        quantity_ordered=5,
        quantity_received=0,
        unit_price=5,
    )
    db.add(second)
    db.commit()
    body = {
        'idempotency_key': 'delivery-key-123',
        'purchase_order_id': line.purchase_order_id,
        'lines': [
            {'po_line_id': line.id, 'quantity_received': 3, 'lot_number': 'LOT-A', 'heat_number': 'HEAT-A'},
            {'po_line_id': second.id, 'quantity_received': 5, 'lot_number': 'LOT-B', 'requires_inspection': True},
        ],
    }
    return user, line, second, body


def test_batch_posts_once_keeps_per_line_inspection_and_replays_original_outcome(client, db_session, no_print):
    user, first, second, body = setup_delivery(db_session)
    response = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body)
    assert response.status_code == 200, response.text
    assert len(response.json()['receipts']) == 2
    assert db_session.query(POReceipt).count() == 2
    assert db_session.query(InventoryTransaction).count() == 1
    stock = db_session.query(InventoryItem).one()
    assert (stock.lot_number, stock.quantity_on_hand) == ('LOT-A', 3)
    assert db_session.query(ReceivingDeliveryBatch).count() == 1
    again = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body)
    assert again.json() == response.json()
    assert no_print.call_count == 2
    body['lines'][0]['quantity_received'] = 4
    assert client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body).status_code == 409
    assert db_session.query(InventoryTransaction).count() == 1


def test_second_line_failure_rolls_back_receipts_stock_and_po(client, db_session):
    user, first, second, body = setup_delivery(db_session)
    body['lines'][1]['quantity_received'] = 6
    response = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body)
    assert response.status_code == 400, response.text
    assert 'Delivery line 2' in response.text
    for model in (POReceipt, InventoryItem, InventoryTransaction, ReceivingDeliveryBatch):
        assert db_session.query(model).count() == 0
    db_session.expire_all()
    assert first.quantity_received == 0 and second.quantity_received == 0
    assert first.purchase_order.status == POStatus.SENT
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type.in_(['receipt', 'receiving_delivery', 'inventory']))
        .count()
        == 0
    )


def test_certificate_bytes_are_retrievable_linked_and_delete_protected(client, db_session, monkeypatch, tmp_path):
    from app.services.storage_service import LocalStorageBackend

    monkeypatch.setattr('app.services.receiving_delivery_service.get_storage', lambda: LocalStorageBackend())
    monkeypatch.setattr('app.services.receiving_delivery_service.resolve_upload_dir', lambda: str(tmp_path))
    user, first, second, body = setup_delivery(db_session)
    pdf = b'%PDF-1.4\n synthetic certificate bytes\n%%EOF'
    upload = client.post(
        '/api/v1/receiving/certificates',
        headers=headers_for(user),
        data={'po_line_id': first.id},
        files={'file': ('mill.pdf', pdf, 'application/pdf')},
    )
    assert upload.status_code == 200, upload.text
    document_id = upload.json()['id']
    body['lines'][0]['certificate_document_id'] = document_id
    received = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body)
    assert received.status_code == 200, received.text
    assert received.json()['receipts'][0]['certificate_document_id'] == document_id
    assert client.get(f'/api/v1/documents/{document_id}/download', headers=headers_for(user)).content == pdf
    assert client.delete(f'/api/v1/documents/{document_id}', headers=headers_for(user)).status_code == 409
    assert client.get(f'/api/v1/documents/{document_id}/download', headers=headers_for(user)).content == pdf
    foreign = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    assert client.get(f'/api/v1/documents/{document_id}/download', headers=headers_for(foreign)).status_code == 404
    wrong = make_po_line(db_session, company_id=2)
    assert (
        client.post(
            '/api/v1/receiving/certificates',
            headers=headers_for(user),
            data={'po_line_id': wrong.id},
            files={'file': ('test.pdf', pdf, 'application/pdf')},
        ).status_code
        == 404
    )


def test_foreign_lines_and_revoked_permissions_cannot_post(client, db_session):
    user, first, second, body = setup_delivery(db_session)
    foreign = make_po_line(db_session, company_id=2)
    body['lines'][1]['po_line_id'] = foreign.id
    assert client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body).status_code == 422
    body['lines'][1]['po_line_id'] = second.id
    db_session.add(RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['receiving:view']))
    db_session.commit()
    assert client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body).status_code == 403
    assert db_session.query(POReceipt).count() == 0


@pytest.mark.parametrize('company_timezone', ['America/Chicago', 'UTC'])
@pytest.mark.parametrize('due_offset,expected_severity', [(-1, 'high'), (0, 'medium')])
def test_confirmation_preserves_requested_dates_rejects_stale_and_appears_in_inbox(
    client, db_session, monkeypatch, company_timezone, due_offset, expected_severity
):
    user, first, second, _ = setup_delivery(db_session)
    company = db_session.get(Company, user.company_id)
    company.timezone = company_timezone
    # At this instant UTC is September 9 while Chicago is still September 8.
    # Fixture deadlines and inbox severity must share the company's calendar.
    inbox_now = datetime(2026, 9, 9, 0, 30, tzinfo=timezone.utc)
    monkeypatch.setattr('app.services.operations_inbox_service.datetime', Mock(now=Mock(return_value=inbox_now)))
    today = inbox_now.astimezone(ZoneInfo(company.timezone)).date()
    po = first.purchase_order
    po.required_date = today + timedelta(days=1)
    po.expected_date = today + timedelta(days=2)
    db_session.commit()
    headers = headers_for(user)
    old = client.get(f'/api/v1/purchasing/purchase-orders/{po.id}', headers=headers).json()
    body = {
        'expected_updated_at': old['updated_at'],
        'acknowledged': True,
        'supplier_confirmed_date': (today + timedelta(days=3)).isoformat(),
        'supplier_confirmation_note': 'Supplier confirms revised arrival after material delay.',
        'follow_up_owner_id': user.id,
        'follow_up_due_date': (today + timedelta(days=due_offset)).isoformat(),
    }
    url = f'/api/v1/purchasing/purchase-orders/{po.id}/supplier-confirmation'
    response = client.put(url, headers=headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['required_date'] == old['required_date'] and result['expected_date'] == old['expected_date']
    assert result['supplier_acknowledged_at'] and result['supplier_confirmed_date'] == body['supplier_confirmed_date']
    assert client.put(url, headers=headers, json=body).status_code == 409
    follow = next(
        item
        for item in client.get('/api/v1/operations-inbox/', headers=headers).json()['items']
        if item['source_kind'] == 'supplier_follow_up' and item['source_id'] == po.id
    )
    assert follow['owner_id'] == user.id and follow['severity'] == expected_severity
    body.update(expected_updated_at=result['updated_at'], acknowledged=False, supplier_confirmed_date=None)
    revoked = client.put(url, headers=headers, json=body)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()['supplier_acknowledged_at'] is None and revoked.json()['supplier_confirmed_date'] is None


def test_followup_denies_foreign_owner_and_company_and_closed_po(client, db_session):
    user, line, _, _ = setup_delivery(db_session)
    foreign = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    po = line.purchase_order
    body = {
        'expected_updated_at': po.updated_at.isoformat(),
        'acknowledged': False,
        'supplier_confirmation_note': 'Chase acknowledgment',
        'follow_up_owner_id': foreign.id,
    }
    url = f'/api/v1/purchasing/purchase-orders/{po.id}/supplier-confirmation'
    assert client.put(url, headers=headers_for(user), json=body).status_code == 422
    assert client.put(url, headers=headers_for(foreign), json=body).status_code == 404
    po.status = POStatus.CLOSED
    db_session.commit()
    assert client.put(url, headers=headers_for(user), json=body).status_code == 409


def test_late_certificate_for_removed_vendor_does_not_repost_inventory(client, db_session, monkeypatch, tmp_path):
    from app.services.storage_service import LocalStorageBackend

    monkeypatch.setattr('app.services.receiving_delivery_service.get_storage', lambda: LocalStorageBackend())
    monkeypatch.setattr('app.services.receiving_delivery_service.resolve_upload_dir', lambda: str(tmp_path))
    user, line, _, body = setup_delivery(db_session)
    result = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body).json()
    receipt_id = result['receipts'][0]['id']
    line.purchase_order.vendor.soft_delete(user.id)
    db_session.commit()
    before = db_session.query(InventoryTransaction).count()
    fields = {'po_line_id': line.id, 'receipt_id': receipt_id}
    upload = client.post(
        '/api/v1/receiving/certificates',
        headers=headers_for(user),
        data=fields,
        files={'file': ('late.pdf', b'%PDF-1.4 historical supplier certificate', 'application/pdf')},
    )
    assert upload.status_code == 200, upload.text
    assert db_session.query(InventoryTransaction).count() == before
    assert db_session.get(POReceipt, receipt_id).certificate_document_id == upload.json()['id']
    again = client.post(
        '/api/v1/receiving/certificates',
        headers=headers_for(user),
        data=fields,
        files={'file': ('replacement.pdf', b'%PDF-1.4 replacement', 'application/pdf')},
    )
    assert again.status_code == 409


def test_uncertain_certificate_commit_preserves_committed_document_and_bytes(client, db_session, monkeypatch, tmp_path):
    from app.services.storage_service import LocalStorageBackend

    monkeypatch.setattr('app.services.receiving_delivery_service.get_storage', lambda: LocalStorageBackend())
    monkeypatch.setattr('app.services.receiving_delivery_service.resolve_upload_dir', lambda: str(tmp_path))
    user, line, _, body = setup_delivery(db_session)
    result = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body).json()
    receipt_id = result['receipts'][0]['id']
    commit = db_session.commit

    def lost_commit_response():
        commit()
        raise OSError('Synthetic lost commit response')

    monkeypatch.setattr(db_session, 'commit', lost_commit_response)
    pdf = b'%PDF-1.4 preserved after uncertain commit'
    upload = client.post(
        '/api/v1/receiving/certificates',
        headers=headers_for(user),
        data={'po_line_id': line.id, 'receipt_id': receipt_id},
        files={'file': ('preserved.pdf', pdf, 'application/pdf')},
    )
    assert upload.status_code == 503, upload.text
    monkeypatch.setattr(db_session, 'commit', commit)
    detail = client.get(f'/api/v1/receiving/receipt/{receipt_id}', headers=headers_for(user))
    assert detail.status_code == 200, detail.text
    document_id = detail.json()['certificate_document_id']
    assert document_id
    assert client.get(f'/api/v1/documents/{document_id}/download', headers=headers_for(user)).content == pdf


def test_supplier_version_compares_equivalent_timezone_instants(client, db_session):
    user, line, _, _ = setup_delivery(db_session)
    po = line.purchase_order
    offset_version = po.updated_at.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=-5))).isoformat()
    response = client.put(
        f'/api/v1/purchasing/purchase-orders/{po.id}/supplier-confirmation',
        headers=headers_for(user),
        json={
            'expected_updated_at': offset_version,
            'acknowledged': False,
            'supplier_confirmation_note': 'Timezone-normalized supplier follow-up',
        },
    )
    assert response.status_code == 200, response.text


def test_receiving_events_snapshot_each_received_line_not_cumulative_po_quantity(client, db_session):
    from app.models.operational_event import OperationalEvent
    from app.services.notification_dispatch import _receipt_email_context

    user, first, second, body = setup_delivery(db_session)
    original_number, original_name = first.part.part_number, first.part.name
    response = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body)
    assert response.status_code == 200, response.text
    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == 1,
            OperationalEvent.event_type == 'purchase_order_received',
        )
        .order_by(OperationalEvent.id)
        .all()
    )
    assert len(events) == 2
    assert [e.event_payload['quantity_received'] for e in events] == [3, 5]
    assert [e.event_payload['lot_number'] for e in events] == ['LOT-A', 'LOT-B']
    assert all(e.event_payload['part_number'] == original_number for e in events)
    assert all(e.event_payload['part_name'] == original_name for e in events)
    assert all(e.event_payload['unit_of_measure'] for e in events)
    first.part.name = 'Renamed after receiving'
    db_session.commit()
    assert _receipt_email_context(events[0])['received_items'][0]['part_name'] == original_name
    # Replaying the delivery cannot create another receipt notification event.
    again = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body)
    assert again.status_code == 200
    assert (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.event_type == 'purchase_order_received',
        )
        .count()
        == 2
    )


def _upload_certificate(client, user, po_line_id, name):
    response = client.post(
        '/api/v1/receiving/certificates',
        headers=headers_for(user),
        data={'po_line_id': po_line_id},
        files={'file': (name, b'%PDF-1.4 synthetic ' + name.encode(), 'application/pdf')},
    )
    assert response.status_code == 200, response.text
    return response.json()['id']


@pytest.fixture
def local_certificates(monkeypatch, tmp_path):
    from app.services.storage_service import LocalStorageBackend

    monkeypatch.setattr('app.services.receiving_delivery_service.get_storage', lambda: LocalStorageBackend())
    monkeypatch.setattr('app.services.receiving_delivery_service.resolve_upload_dir', lambda: str(tmp_path))


def _final_tables(statement):
    return {getattr(table, 'name', None) for table in statement.get_final_froms()}


def _record_lock_order(db_session, monkeypatch):
    """Record, in execution order: each row-locking read of ``documents`` and ``purchase_orders``
    (compiled for Postgres -- SQLite drops the clause), the delivery batch insert, and each take of
    the global audit-chain lock."""
    events = []

    def before_execute(conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG001
        if isinstance(clauseelement, Select):
            compiled = clauseelement.compile(dialect=postgresql.dialect())
            if 'FOR UPDATE' not in str(compiled):
                return
            tables = _final_tables(clauseelement)
            if 'documents' in tables:
                events.append(('document_lock', str(compiled), compiled.params))
            elif 'purchase_orders' in tables:
                events.append(('po_lock', str(compiled), compiled.params))
        elif isinstance(clauseelement, Insert) and clauseelement.table.name == 'receiving_delivery_batches':
            events.append(('batch_insert', None, None))

    monkeypatch.setattr(settings, 'AUDIT_HASH_CHAIN_ENABLED', True)
    original_chain_lock = AuditService._acquire_chain_lock

    def spy_chain_lock(self):
        events.append(('chain_lock', None, None))
        original_chain_lock(self)

    monkeypatch.setattr(AuditService, '_acquire_chain_lock', spy_chain_lock)
    engine = db_session.get_bind()
    event.listen(engine, 'before_execute', before_execute)
    return events, lambda: event.remove(engine, 'before_execute', before_execute)


def _post_recording(client, db_session, monkeypatch, user, body):
    events, stop = _record_lock_order(db_session, monkeypatch)
    try:
        response = client.post('/api/v1/receiving/deliveries', headers=headers_for(user), json=body)
    finally:
        stop()
    return response, events


def test_delivery_locks_every_certificate_in_id_order_before_the_audit_chain_lock(
    client, db_session, monkeypatch, local_certificates
):
    """Line 1's audit row takes the global audit-chain lock, and document writers lock the document
    row first and the chain second. A delivery that locked line 2's certificate after line 1's audit
    row deadlocked against such a writer on Postgres (40P01, answered 409). All of the delivery's
    certificates must be row-locked -- tenant-scoped, ascending id whatever the line order, after the
    PO row as a single receipt locks them -- before the first chain lock, and none after it."""
    user, first, second, body = setup_delivery(db_session)
    lower = _upload_certificate(client, user, first.id, 'lower.pdf')
    higher = _upload_certificate(client, user, second.id, 'higher.pdf')
    assert lower < higher
    # Line order is the reverse of id order, so a per-line lock cannot pass for an id-ordered one.
    body['lines'][0]['certificate_document_id'] = higher
    body['lines'][1]['certificate_document_id'] = lower

    response, events = _post_recording(client, db_session, monkeypatch, user, body)

    assert response.status_code == 200, response.text
    assert [row['certificate_document_id'] for row in response.json()['receipts']] == [higher, lower]
    kinds = [kind for kind, _, _ in events]
    assert 'document_lock' in kinds and 'chain_lock' in kinds, kinds
    assert 'document_lock' not in kinds[kinds.index('chain_lock') :], kinds
    assert kinds.index('po_lock') < kinds.index('document_lock'), kinds
    document_locks = [(sql, bound) for kind, sql, bound in events if kind == 'document_lock']
    assert len(document_locks) == 1, document_locks
    sql, bound = document_locks[0]
    assert 'documents.company_id = ' in sql, sql
    assert 'ORDER BY documents.id' in sql and sql.rstrip().endswith('FOR UPDATE'), sql
    assert [value for value in bound.values() if isinstance(value, list)] == [[lower, higher]], bound


def test_delivery_locks_the_po_before_its_batch_row_and_no_document_without_certificates(
    client, db_session, monkeypatch
):
    """The batch insert takes FOR KEY SHARE on the PO through its foreign key. A delivery that inserted
    it before the receipt-number lock and the PO's FOR UPDATE deadlocked against a single receipt on
    the same PO holding the receipt-number lock and waiting on the PO. Certificates or not, the PO
    row lock comes first; with no certificate named, no document is locked."""
    user, _, _, body = setup_delivery(db_session)

    response, events = _post_recording(client, db_session, monkeypatch, user, body)

    assert response.status_code == 200, response.text
    kinds = [kind for kind, _, _ in events]
    assert 'po_lock' in kinds and 'batch_insert' in kinds, kinds
    assert kinds.index('po_lock') < kinds.index('batch_insert'), kinds
    assert 'document_lock' not in kinds, kinds


def _foreign_company_certificate(client, db_session, user, line, second):
    """A certificate in another company that otherwise matches the line's part and supplier, so only
    the tenant filter refuses it."""
    _ensure_company(db_session, 2)
    document = Document(
        company_id=2,
        document_number='FOREIGN-CERT-1',
        title='Foreign certificate',
        revision='A',
        document_type=DocumentType.MATERIAL_CERT,
        part_id=second.part_id,
        vendor_id=second.purchase_order.vendor_id,
        file_path=db_session.get(Document, _upload_certificate(client, user, line.id, 'own.pdf')).file_path,
        status='released',
    )
    db_session.add(document)
    db_session.commit()
    return document.id


def _drawing(client, db_session, user, line, second):
    document = db_session.get(Document, _upload_certificate(client, user, second.id, 'drawing.pdf'))
    document.document_type = DocumentType.DRAWING
    db_session.commit()
    return document.id


def _missing_file(client, db_session, user, line, second):
    document = db_session.get(Document, _upload_certificate(client, user, second.id, 'gone.pdf'))
    os.remove(document.file_path)
    return document.id


def _other_supplier(client, db_session, user, line, second):
    return _upload_certificate(client, user, make_po_line(db_session, company_id=1).id, 'wrong.pdf')


@pytest.mark.parametrize(
    'make_certificate,reason',
    [
        (_other_supplier, 'Choose a certificate uploaded for this part and supplier'),
        (_foreign_company_certificate, 'Choose a certificate uploaded for this part and supplier'),
        (_drawing, 'The linked document must be a stored certificate'),
        (_missing_file, 'Certificate file is unavailable. Upload it again before receiving.'),
    ],
)
def test_later_line_certificate_refusal_names_the_line_before_any_line_is_received(
    client, db_session, monkeypatch, local_certificates, make_certificate, reason
):
    """Every line's certificate is checked, with the per-receipt checks, before line 1 is received: a
    bad certificate on line 2 is refused with its line number before any audit row, nothing posted."""
    user, first, second, body = setup_delivery(db_session)
    body['lines'][0]['certificate_document_id'] = _upload_certificate(client, user, first.id, 'good.pdf')
    body['lines'][1]['certificate_document_id'] = make_certificate(client, db_session, user, first, second)
    audit_rows = db_session.query(AuditLog).count()

    response, events = _post_recording(client, db_session, monkeypatch, user, body)

    assert response.status_code == 422, response.text
    assert response.json()['detail'] == f'Delivery line 2: {reason}. No delivery lines were posted.'
    assert 'chain_lock' not in [kind for kind, _, _ in events], events
    for model in (POReceipt, InventoryItem, InventoryTransaction, ReceivingDeliveryBatch):
        assert db_session.query(model).count() == 0
    assert db_session.query(AuditLog).count() == audit_rows
