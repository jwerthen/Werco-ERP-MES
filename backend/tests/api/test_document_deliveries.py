"""Real document/transaction tests; SMTP is always replaced by an in-memory fake."""

import hashlib
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader

from app.models.company import Company
from app.models.document_delivery import DocumentDelivery
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.services import document_delivery_service as delivery_module

pytestmark = pytest.mark.requires_db
REAL_SMTP_ADAPTER = delivery_module.dispatch_document_email


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    transport = AsyncMock()
    monkeypatch.setattr(delivery_module, 'dispatch_document_email', transport)
    monkeypatch.setattr(delivery_module, 'smtp_configured', lambda: True)
    return transport


@pytest.fixture
def quote(db_session, admin_user):
    quote = Quote(
        company_id=1,
        quote_number='QT-DELIVERY',
        customer_name='Acme & Sons',
        customer_email='buyer@example.com',
        subtotal=25,
        total=25,
        created_by=admin_user.id,
    )
    db_session.add(quote)
    db_session.flush()
    db_session.add(
        QuoteLine(
            company_id=1,
            quote_id=quote.id,
            line_number=1,
            description='Bracket <REF>',
            quantity=5,
            unit_price=5,
            line_total=25,
            labor_hours=123,
        )
    )
    db_session.commit()
    return quote


@pytest.fixture
def purchase_order(db_session, admin_user, test_part):
    vendor = Vendor(
        company_id=1,
        code='V-DOC',
        name='Metal Supply & Co',
        email='supplier@example.com',
        address_line1='100 Material Way',
        city='Tulsa',
        state='OK',
        postal_code='74103',
        contact_name='Pat Smith',
        phone='555-0100',
    )
    db_session.add(vendor)
    db_session.flush()
    po = PurchaseOrder(
        company_id=1,
        po_number='PO-DELIVERY',
        vendor_id=vendor.id,
        created_by=admin_user.id,
        required_date=date.today() + timedelta(days=10),
        subtotal=100,
        tax=5,
        shipping=10,
        total=115,
        ship_to='Werco Receiving\n415 East Houston Street',
        shipping_method='Ground',
        notes='Include mill certifications.',
    )
    db_session.add(po)
    db_session.flush()
    db_session.add(
        PurchaseOrderLine(
            company_id=1,
            purchase_order_id=po.id,
            line_number=1,
            part_id=test_part.id,
            quantity_ordered=10,
            quantity_received=0,
            unit_price=10,
            line_total=100,
            required_date=po.required_date,
        )
    )
    db_session.commit()
    return po


def prepare(client, headers, source, kind='quote'):
    response = client.post(
        '/api/v1/document-deliveries/preview', headers=headers, json={'entity_type': kind, 'entity_id': source.id}
    )
    assert response.status_code == 200, response.text
    return response.json()


def payload(preview, **changes):
    data = dict(
        expected_version=preview['version'],
        request_key='delivery-request-01',
        recipient=preview['recipient'],
        subject=preview['subject'],
        body=preview['body'],
    )
    data.update(changes)
    return data


def test_review_attachment_send_replay_and_history(client, admin_headers, db_session, quote, fake_smtp):
    review = prepare(client, admin_headers, quote)
    assert fake_smtp.await_count == 0
    attachment = client.get(f"/api/v1/document-deliveries/{review['id']}/attachment", headers=admin_headers)
    assert hashlib.sha256(attachment.content).hexdigest() == review['attachment_sha256']
    assert attachment.headers['cache-control'] == 'private, no-store'
    endpoint = f"/api/v1/document-deliveries/{review['id']}/send"
    data = payload(
        review, recipient='reviewed@example.com', subject='Reviewed quote', body='Please confirm delivery terms.'
    )
    response = client.post(endpoint, headers=admin_headers, json=data)
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted['status'] == 'accepted' and accepted['delivered'] is None
    assert 'has not been confirmed' in accepted['status_detail']
    assert fake_smtp.await_count == 1
    sent = fake_smtp.await_args.args[0]
    assert sent.attachment == attachment.content and sent.recipient == 'reviewed@example.com'
    assert db_session.get(Quote, quote.id).status == QuoteStatus.SENT
    assert client.post(endpoint, headers=admin_headers, json=data).json()['replayed']
    assert fake_smtp.await_count == 1
    assert prepare(client, admin_headers, quote)['id'] == review['id']
    data['recipient'] = 'different@example.com'
    assert client.post(endpoint, headers=admin_headers, json=data).status_code == 409
    history = client.get(
        '/api/v1/document-deliveries', headers=admin_headers, params={'entity_type': 'quote', 'entity_id': quote.id}
    ).json()
    assert history[0]['status'] == 'accepted'


@pytest.mark.parametrize('outcome', ['unknown', 'failed'])
def test_smtp_outcome_is_honest_and_never_replayed_as_a_second_send(
    client, admin_headers, db_session, quote, fake_smtp, outcome
):
    fake_smtp.side_effect = (
        TimeoutError('ambiguous DATA timeout')
        if outcome == 'unknown'
        else delivery_module.DefiniteEmailFailure('Recipient rejected')
    )
    review = prepare(client, admin_headers, quote)
    endpoint = f"/api/v1/document-deliveries/{review['id']}/send"
    data = payload(review)
    response = client.post(endpoint, headers=admin_headers, json=data)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == outcome
    assert db_session.get(Quote, quote.id).status == QuoteStatus.DRAFT
    assert client.post(endpoint, headers=admin_headers, json=data).json()['status'] == outcome
    assert fake_smtp.await_count == 1
    if outcome == 'unknown':
        quote.notes = 'Updated while the previous email outcome remains unknown'
        db_session.commit()
        assert prepare(client, admin_headers, quote)['id'] == review['id']


def test_pending_claim_cannot_resend_after_worker_or_request_interruption(
    client, admin_headers, db_session, quote, fake_smtp
):
    review = prepare(client, admin_headers, quote)
    row = db_session.get(DocumentDelivery, review['id'])
    row.status = 'sending'
    row.request_key = 'delivery-request-01'
    row.request_hash = delivery_module.digest(payload(review) | {})
    db_session.commit()
    result = client.post(f"/api/v1/document-deliveries/{row.id}/send", headers=admin_headers, json=payload(review))
    assert result.status_code == 409
    assert prepare(client, admin_headers, quote)['status'] == 'sending'
    assert fake_smtp.await_count == 0


@pytest.mark.parametrize('change', ['price', 'recipient', 'version', 'unconfigured'])
def test_stale_review_and_unconfigured_mail_never_dispatch(
    client, admin_headers, db_session, quote, fake_smtp, monkeypatch, change
):
    review = prepare(client, admin_headers, quote)
    data = payload(review)
    if change == 'price':
        quote.lines[0].quantity = 8
        quote.lines[0].line_total = quote.total = 40
        db_session.commit()
    elif change == 'recipient':
        quote.customer_email = 'newbuyer@example.com'
        db_session.commit()
    elif change == 'version':
        data['expected_version'] += 1
    else:
        monkeypatch.setattr(delivery_module, 'smtp_configured', lambda: False)
    result = client.post(f"/api/v1/document-deliveries/{review['id']}/send", headers=admin_headers, json=data)
    assert result.status_code == (503 if change == 'unconfigured' else 409), result.text
    assert fake_smtp.await_count == 0
    assert db_session.get(DocumentDelivery, review['id']).status == 'prepared'


def test_download_and_delivery_use_current_quote_prices_not_old_estimate(client, admin_headers, db_session, quote):
    from app.models.rfq_quote import QuoteEstimate, QuoteLineSummary, RfqPackage

    package = RfqPackage(company_id=1, rfq_number='RFQ-DELIVERY', customer_name='Acme & Sons')
    db_session.add(package)
    db_session.flush()
    estimate = QuoteEstimate(company_id=1, rfq_package_id=package.id, quote_id=quote.id, grand_total=999)
    db_session.add(estimate)
    db_session.flush()
    db_session.add(
        QuoteLineSummary(
            company_id=1,
            quote_estimate_id=estimate.id,
            part_name='Bracket <REF>',
            quantity=1,
            part_total=999,
            material='Aluminum',
            thickness='0.125',
            finish='Mill',
        )
    )
    quote.lines[0].quantity, quote.lines[0].unit_price, quote.lines[0].line_total = 8, 9, 72
    quote.total = quote.subtotal = 72
    db_session.commit()
    review = prepare(client, admin_headers, quote)
    snapshot = client.get(f"/api/v1/document-deliveries/{review['id']}/attachment", headers=admin_headers).content
    downloaded = client.post(f'/api/v1/quotes/{quote.id}/generate-pdf', headers=admin_headers).content
    assert downloaded == snapshot
    text = ' '.join(page.extract_text() for page in PdfReader(BytesIO(snapshot)).pages)
    assert '$72.00' in text and 'Bracket <REF>' in text and '123' not in text
    assert '$999.00' not in text and 'Aluminum' in text
    from pathlib import Path

    Path('/tmp/werco-delivery-quote-review.pdf').write_bytes(snapshot)


def test_po_uses_vendor_print_fields_and_only_acceptance_changes_status(
    client, admin_headers, db_session, purchase_order, fake_smtp
):
    review = prepare(client, admin_headers, purchase_order, 'purchase_order')
    pdf = client.get(f"/api/v1/document-deliveries/{review['id']}/attachment", headers=admin_headers).content
    text = ' '.join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    for expected in (
        'Metal Supply & Co',
        '100 Material Way',
        'Pat Smith',
        'Ship To',
        'Ground',
        'Received',
        'Backorder',
        '$115.00',
        'Include mill certifications.',
    ):
        assert expected in text
    result = client.post(
        f"/api/v1/document-deliveries/{review['id']}/send", headers=admin_headers, json=payload(review)
    )
    assert result.status_code == 200, result.text
    assert fake_smtp.await_count == 1
    assert db_session.get(PurchaseOrder, purchase_order.id).status == POStatus.SENT


def test_history_and_attachment_tenant_and_custom_permission_gates(
    client, admin_headers, operator_headers, auth_headers, db_session, quote, purchase_order, test_user
):
    review = prepare(client, admin_headers, quote)
    endpoint = f"/api/v1/document-deliveries/{review['id']}"
    assert client.get(endpoint, headers=operator_headers).status_code == 403
    assert client.get(endpoint + '/attachment', headers=operator_headers).status_code == 403
    db_session.add(Company(id=2, name='Other', slug='other'))
    row = db_session.get(DocumentDelivery, review['id'])
    row.company_id = 2
    db_session.commit()
    assert client.get(endpoint, headers=admin_headers).status_code == 404
    assert client.get(endpoint + '/attachment', headers=admin_headers).status_code == 404
    po_review = prepare(client, admin_headers, purchase_order, 'purchase_order')
    override = RolePermission(company_id=1, role=UserRole.MANAGER, permissions=['purchasing:view'])
    db_session.add(override)
    db_session.commit()
    status = client.get(f"/api/v1/document-deliveries/{po_review['id']}", headers=auth_headers)
    assert status.status_code == 200 and not status.json()['send_available']
    assert (
        client.post(
            f"/api/v1/document-deliveries/{po_review['id']}/send", headers=auth_headers, json=payload(po_review)
        ).status_code
        == 403
    )
    override.permissions = []
    db_session.commit()
    assert (
        client.get(f"/api/v1/document-deliveries/{po_review['id']}/attachment", headers=auth_headers).status_code == 403
    )


def test_edited_recipient_and_message_validation(client, admin_headers, quote, fake_smtp):
    review = prepare(client, admin_headers, quote)
    endpoint = f"/api/v1/document-deliveries/{review['id']}/send"
    for change in (
        {'recipient': 'not-an-email'},
        {'subject': 'Hello\r\nBcc: attacker@example.com'},
        {'body': 'x' * 10001},
    ):
        assert client.post(endpoint, headers=admin_headers, json=payload(review, **change)).status_code == 422
    assert fake_smtp.await_count == 0


def test_deliberate_new_email_uses_new_attempt_and_never_bypasses_unknown(client, admin_headers, quote, fake_smtp):
    first = prepare(client, admin_headers, quote)
    client.post(f"/api/v1/document-deliveries/{first['id']}/send", headers=admin_headers, json=payload(first))
    fresh = client.post(
        '/api/v1/document-deliveries/preview',
        headers=admin_headers,
        json={'entity_type': 'quote', 'entity_id': quote.id, 'new_attempt': True},
    ).json()
    assert fresh['id'] != first['id'] and fresh['status'] == 'prepared'
    assert fresh['attachment_sha256'] == first['attachment_sha256']
    data = payload(fresh, request_key='new-deliberate-email', recipient='otherbuyer@example.com')
    fake_smtp.side_effect = TimeoutError('unknown')
    client.post(f"/api/v1/document-deliveries/{fresh['id']}/send", headers=admin_headers, json=data)
    again = client.post(
        '/api/v1/document-deliveries/preview',
        headers=admin_headers,
        json={'entity_type': 'quote', 'entity_id': quote.id, 'new_attempt': True},
    ).json()
    assert again['id'] == fresh['id'] and again['status'] == 'unknown'
    assert fake_smtp.await_count == 2


def test_fractional_po_quantity_matches_reviewed_document(client, admin_headers, db_session, purchase_order):
    purchase_order.lines[0].quantity_ordered = 2.5
    purchase_order.lines[0].quantity_received = 0.25
    db_session.commit()
    preview = prepare(client, admin_headers, purchase_order, 'purchase_order')
    pdf = client.get(f"/api/v1/document-deliveries/{preview['id']}/attachment", headers=admin_headers).content
    text = ' '.join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert '2.5' in text and '0.25' in text and '2.25' in text


def test_response_in_read_only_context_cannot_offer_send(db_session, admin_user, quote):
    from app.services.audit_service import AuditService

    service = delivery_module.DocumentDeliveryService(db_session, 1, admin_user, AuditService(db_session, admin_user))
    prepared = service.preview('quote', quote.id)
    db_session.commit()
    admin_user._read_only_company_context = True
    response = service.response(service.get(prepared.id))
    assert not response.send_available and 'read-only' in response.unavailable_reason


def test_multipage_po_pdf_repeats_headers_preserves_vendor_data_and_totals(tmp_path):
    from app.services.document_pdf_service import build_purchase_order_document

    data = dict(
        po_number='PO-2026-0907',
        printed_at='09/07/2026',
        vendor_name='Tulsa Metals & Industrial Supply',
        vendor_address='1250 East Pine Street\nTulsa, OK 74106',
        vendor_contact='Morgan Ellis',
        vendor_phone='918-555-0142',
        vendor_email='orders@example.com',
        buyer_name='Taylor Morgan',
        buyer_email='buyer@example.com',
        order_date='09/07/2026',
        required_date='09/21/2026',
        expected_date='09/18/2026',
        ship_to='Werco Manufacturing - Receiving\n415 East Houston Street\nBroken Arrow, OK 74012',
        shipping_method='LTL - supplier arranged',
        subtotal='$18,525.00',
        tax='$0.00',
        shipping='$275.00',
        total='$18,800.00',
        notes='Include material certifications and heat/lot traceability with every shipment. Confirm required delivery dates before dispatch.',
        lines=[],
    )
    for index in range(1, 66):
        data['lines'].append(
            dict(
                line_number=index,
                part_number=f'AL-6061-T6-{index:03}',
                part_name='Aluminum plate 6061-T6, precision cut blanks, mill finish, material certification required',
                quantity_ordered='2.5',
                quantity_received='0',
                unit_price='$114.00',
                line_total='$285.00',
                required_date='09/21/2026' if index < 40 else '09/28/2026',
            )
        )
    pdf = build_purchase_order_document(data)
    reader = PdfReader(BytesIO(pdf))
    assert len(reader.pages) >= 3
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert 'AL-6061-T6-065' in text and '$18,800.00' in text and 'heat/lot traceability' in text
    # Keep a representative, synthetic artifact for the required visual inspection.
    from pathlib import Path

    Path('/tmp/werco-delivery-po-review.pdf').write_bytes(pdf)


@pytest.mark.asyncio
@pytest.mark.parametrize('port', [587, 465])
async def test_smtp_adapter_attaches_exact_reviewed_pdf_without_real_network(monkeypatch, port):
    # Use the real MIME adapter with a fake SMTP context. No socket can open.
    from types import SimpleNamespace

    captured = []
    negotiations = []
    monkeypatch.setattr(delivery_module.settings, "SMTP_PORT", port)

    class FakeSMTP:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.tls = False

        async def __aenter__(self):
            # Faithful to aiosmtplib>=5.1: default start_tls=None upgrades on
            # connect when the server advertises STARTTLS. A second call fails.
            self.tls = self.options.get('use_tls', False) or self.options.get('start_tls') is not False
            assert self.options['start_tls'] is False
            assert self.options['use_tls'] is (port == 465)
            return self

        async def __aexit__(self, *args):
            pass

        async def starttls(self):
            if self.tls:
                raise delivery_module.aiosmtplib.SMTPException('Connection already using TLS')
            negotiations.append('starttls')
            self.tls = True

        async def login(self, *args):
            assert self.tls, 'Credentials must never be sent before TLS'

        async def send_message(self, message):
            captured.append(message)
            return {}, 'accepted'

    monkeypatch.setattr(delivery_module.aiosmtplib, 'SMTP', FakeSMTP)
    record = SimpleNamespace(
        recipient='buyer@example.com',
        subject='Reviewed quote',
        body='Reviewed message',
        provider_message_id='<test@example.com>',
        attachment=b'%PDF-reviewed-content',
        attachment_name='QT-REVIEW.pdf',
    )
    await REAL_SMTP_ADAPTER(record)
    message = captured[0]
    assert negotiations == ([] if port == 465 else ['starttls'])
    assert message['To'] == 'buyer@example.com' and message['Message-ID'] == '<test@example.com>'
    attachment = list(message.iter_attachments())[0]
    assert attachment.get_payload(decode=True) == record.attachment and attachment.get_filename() == 'QT-REVIEW.pdf'


def test_prior_prepared_snapshot_cannot_bypass_an_unresolved_attempt(
    client, admin_headers, db_session, quote, fake_smtp
):
    first = prepare(client, admin_headers, quote)
    quote.notes = 'A revised commercial note'
    db_session.commit()
    second = prepare(client, admin_headers, quote)
    assert first['id'] != second['id']
    row = db_session.get(DocumentDelivery, first['id'])
    row.status = 'unknown'
    db_session.commit()
    result = client.post(
        f"/api/v1/document-deliveries/{second['id']}/send", headers=admin_headers, json=payload(second)
    )
    assert result.status_code == 409 and 'unresolved SMTP outcome' in result.json()['detail']
    assert fake_smtp.await_count == 0


def test_edit_during_smtp_keeps_sent_snapshot_and_retains_changed_workflow(
    client, admin_headers, db_session, quote, fake_smtp
):
    first = prepare(client, admin_headers, quote)

    async def update_during_transport(record):
        quote.lines[0].quantity = 12
        quote.lines[0].line_total = 60
        quote.total = 60
        db_session.commit()

    fake_smtp.side_effect = update_during_transport
    response = client.post(
        f"/api/v1/document-deliveries/{first['id']}/send", headers=admin_headers, json=payload(first)
    )
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'accepted'
    assert 'source changed' in response.json()['status_detail']
    assert db_session.get(Quote, quote.id).status == QuoteStatus.DRAFT
    assert response.json()['attachment_sha256'] == first['attachment_sha256']


@pytest.mark.parametrize('outcome', ['accepted', 'failed'])
def test_manager_reconciles_unknown_without_smtp_or_provider_confirmation(
    client, admin_headers, db_session, quote, fake_smtp, outcome
):
    review = prepare(client, admin_headers, quote)
    row = db_session.get(DocumentDelivery, review['id'])
    row.status = 'unknown'
    db_session.commit()
    response = client.post(
        f"/api/v1/document-deliveries/{row.id}/reconcile",
        headers=admin_headers,
        json=dict(
            expected_version=row.version,
            outcome=outcome,
            verification_note='Checked SMTP message logs with the mail administrator.',
        ),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == outcome and result['manually_verified']
    assert result['delivered'] is None and result['accepted_at'] is None
    assert result['verified_at'] and 'Manually verified' in result['status_detail']
    assert fake_smtp.await_count == 0
    assert db_session.get(Quote, quote.id).status == (QuoteStatus.SENT if outcome == 'accepted' else QuoteStatus.DRAFT)
    if outcome == 'failed':
        assert prepare(client, admin_headers, quote)['id'] != row.id


def test_reconciliation_waits_for_active_send_and_checks_version(client, admin_headers, db_session, quote, fake_smtp):
    from datetime import datetime

    review = prepare(client, admin_headers, quote)
    row = db_session.get(DocumentDelivery, review['id'])
    row.status = 'sending'
    row.attempted_at = datetime.utcnow()
    db_session.commit()
    data = dict(
        expected_version=row.version,
        outcome='failed',
        verification_note='Checked mail server logs and recipient delivery queue.',
    )
    endpoint = f"/api/v1/document-deliveries/{row.id}/reconcile"
    assert client.post(endpoint, headers=admin_headers, json=data).status_code == 409
    row.attempted_at = datetime.utcnow() - timedelta(minutes=11)
    db_session.commit()
    assert client.post(endpoint, headers=admin_headers, json={**data, 'expected_version': 99}).status_code == 409
    assert client.post(endpoint, headers=admin_headers, json=data).status_code == 200
    assert fake_smtp.await_count == 0


def test_reconciliation_preserves_changed_source_and_requires_verification_note(
    client, admin_headers, db_session, quote, fake_smtp
):
    review = prepare(client, admin_headers, quote)
    row = db_session.get(DocumentDelivery, review['id'])
    row.status = 'unknown'
    quote.lines[0].line_total = 99
    quote.total = 99
    db_session.commit()
    endpoint = f"/api/v1/document-deliveries/{row.id}/reconcile"
    data = dict(expected_version=row.version, outcome='accepted', verification_note='          ')
    assert client.post(endpoint, headers=admin_headers, json=data).status_code == 422
    data['verification_note'] = 'Verified the earlier PDF was accepted in mail server logs.'
    response = client.post(endpoint, headers=admin_headers, json=data)
    assert response.status_code == 200, response.text
    assert 'source changed' in response.json()['status_detail']
    assert db_session.get(Quote, quote.id).status == QuoteStatus.DRAFT
    assert fake_smtp.await_count == 0


def test_supervisor_cannot_reconcile_an_unresolved_quote(
    client, admin_headers, supervisor_headers, db_session, quote, fake_smtp
):
    review = prepare(client, admin_headers, quote)
    row = db_session.get(DocumentDelivery, review['id'])
    row.status = 'unknown'
    db_session.commit()
    response = client.post(
        f"/api/v1/document-deliveries/{row.id}/reconcile",
        headers=supervisor_headers,
        json=dict(
            expected_version=row.version,
            outcome='failed',
            verification_note='Checked the mail server logs for this message.',
        ),
    )
    assert response.status_code == 403
    assert fake_smtp.await_count == 0


def test_delivery_history_defers_pdf_blobs(client, admin_headers, db_session, quote):
    from sqlalchemy import inspect

    review = prepare(client, admin_headers, quote)
    source_id = quote.id
    db_session.expunge_all()
    record = db_session.query(DocumentDelivery).filter_by(id=review['id']).one()
    assert 'attachment' in inspect(record).unloaded
    assert record.attachment_size > 0
    history = client.get(
        '/api/v1/document-deliveries', headers=admin_headers, params=dict(entity_type='quote', entity_id=source_id)
    )
    assert history.status_code == 200
    assert 'attachment' in inspect(record).unloaded


def test_smtp_uses_detached_message_without_holding_a_db_transaction(
    client, admin_headers, db_session, quote, fake_smtp
):
    from dataclasses import FrozenInstanceError

    review = prepare(client, admin_headers, quote)

    async def transport(message):
        assert not db_session.in_transaction()
        assert isinstance(message, delivery_module.DeliveryMessage)
        assert message.attachment.startswith(b'%PDF-')
        with pytest.raises(FrozenInstanceError):
            message.subject = 'Unexpected mutation'

    fake_smtp.side_effect = transport
    response = client.post(
        f"/api/v1/document-deliveries/{review['id']}/send", headers=admin_headers, json=payload(review)
    )
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'accepted'


def test_po_issue_date_is_reviewed_and_same_date_is_persisted_on_acceptance(
    client, admin_headers, db_session, purchase_order, monkeypatch
):
    reviewed_date = date(2026, 10, 5)
    monkeypatch.setattr(delivery_module, 'current_issue_date', lambda: reviewed_date)
    assert purchase_order.order_date is None
    review = prepare(client, admin_headers, purchase_order, 'purchase_order')
    assert review['issue_date'] == str(reviewed_date)
    pdf = client.get(f"/api/v1/document-deliveries/{review['id']}/attachment", headers=admin_headers).content
    text = ' '.join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert 'Order Date: 10/05/2026' in text
    response = client.post(
        f"/api/v1/document-deliveries/{review['id']}/send", headers=admin_headers, json=payload(review)
    )
    assert response.status_code == 200, response.text
    assert db_session.get(PurchaseOrder, purchase_order.id).order_date == reviewed_date
    assert prepare(client, admin_headers, purchase_order, 'purchase_order')['id'] == review['id']


def test_next_day_undated_po_requires_a_new_issue_date_review(
    client, admin_headers, db_session, purchase_order, fake_smtp, monkeypatch
):
    monkeypatch.setattr(delivery_module, 'current_issue_date', lambda: date(2026, 10, 5))
    review = prepare(client, admin_headers, purchase_order, 'purchase_order')
    monkeypatch.setattr(delivery_module, 'current_issue_date', lambda: date(2026, 10, 6))
    result = client.post(
        f"/api/v1/document-deliveries/{review['id']}/send", headers=admin_headers, json=payload(review)
    )
    assert result.status_code == 409
    assert fake_smtp.await_count == 0
    assert db_session.get(PurchaseOrder, purchase_order.id).order_date is None
    refreshed = prepare(client, admin_headers, purchase_order, 'purchase_order')
    assert refreshed['id'] != review['id'] and refreshed['issue_date'] == '2026-10-06'


@pytest.mark.parametrize(
    'verified,late,expected',
    [
        ('failed', 'accepted', 'unknown'),
        ('accepted', 'failed', 'unknown'),
        ('accepted', 'accepted', 'accepted'),
        ('failed', 'unknown', 'failed'),
    ],
)
def test_late_smtp_worker_preserves_verification_and_exposes_conflicts(
    client, admin_headers, admin_user, db_session, quote, fake_smtp, verified, late, expected
):
    from datetime import datetime

    from app.schemas.document_delivery import DocumentDeliveryReconcile
    from app.services.audit_service import AuditService

    review = prepare(client, admin_headers, quote)
    note = 'Mail administrator verified this attempt against SMTP logs.'

    async def delayed_transport(message):
        row = db_session.get(DocumentDelivery, review['id'])
        row.attempted_at = datetime.utcnow() - timedelta(minutes=11)
        db_session.commit()
        service = delivery_module.DocumentDeliveryService(
            db_session, 1, admin_user, AuditService(db_session, admin_user)
        )
        service.reconcile(
            row.id, DocumentDeliveryReconcile(expected_version=row.version, outcome=verified, verification_note=note)
        )
        db_session.commit()
        if late == 'failed':
            raise delivery_module.DefiniteEmailFailure('Late explicit rejection')
        if late == 'unknown':
            raise TimeoutError('Late ambiguous timeout')

    fake_smtp.side_effect = delayed_transport
    response = client.post(
        f"/api/v1/document-deliveries/{review['id']}/send", headers=admin_headers, json=payload(review)
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == expected and result['verification_note'] == note and result['manually_verified']
    assert fake_smtp.await_count == 1
    if expected == 'unknown':
        assert 'Conflict: late SMTP' in result['status_detail']
        assert prepare(client, admin_headers, quote)['id'] == review['id']


def test_failed_deliberate_resend_can_prepare_again_despite_older_acceptance(client, admin_headers, quote, fake_smtp):
    original = prepare(client, admin_headers, quote)
    client.post(f"/api/v1/document-deliveries/{original['id']}/send", headers=admin_headers, json=payload(original))
    second = client.post(
        '/api/v1/document-deliveries/preview',
        headers=admin_headers,
        json={'entity_type': 'quote', 'entity_id': quote.id, 'new_attempt': True},
    ).json()
    fake_smtp.side_effect = delivery_module.DefiniteEmailFailure('Recipient rejected')
    client.post(
        f"/api/v1/document-deliveries/{second['id']}/send",
        headers=admin_headers,
        json=payload(second, request_key='second-email-key'),
    )
    third = prepare(client, admin_headers, quote)
    assert third['status'] == 'prepared' and third['id'] not in (original['id'], second['id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['connect', 'starttls'])
async def test_pre_submission_transport_failures_are_definite_and_need_no_manual_reconciliation(monkeypatch, phase):
    class FakeSMTP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            if phase == 'connect':
                raise OSError('Connection refused')
            return self

        async def __aexit__(self, *args):
            pass

        async def starttls(self):
            raise OSError('TLS handshake refused')

    monkeypatch.setattr(delivery_module.aiosmtplib, 'SMTP', FakeSMTP)
    monkeypatch.setattr(delivery_module.settings, 'SMTP_PORT', 587)
    message = delivery_module.DeliveryMessage(
        'buyer@example.com', 'Quote', 'Body', '<test@example.com>', b'%PDF-review', 'quote.pdf'
    )
    with pytest.raises(delivery_module.DefiniteEmailFailure, match='before message submission'):
        await REAL_SMTP_ADAPTER(message)
