"""Receipt email snapshots, template selection, and escaped received-item detail."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import notification_dispatch as dispatch
from app.services.email_service import EmailService


def receipt_event(**overrides):
    payload = {
        'receipt_number': 'RCV-001',
        'po_number': 'PO-123',
        'po_id': 123,
        'part_number': 'AL-6061',
        'part_name': 'Aluminum plate',
        'quantity_received': 2.5,
        'unit_of_measure': 'lb',
        'lot_number': 'LOT-007',
        'status': 'pending_inspection',
        **overrides,
    }
    return SimpleNamespace(
        event_type='purchase_order_received',
        event_payload=payload,
        company_id=1,
        user_id=7,
        entity_type='po_receipt',
        entity_id=100,
        work_order_id=None,
    )


def test_receipt_context_keeps_fractional_received_quantity_and_only_explicit_fields():
    context = dispatch._receipt_email_context(
        receipt_event(
            quantity_ordered=200,
            quantity_previously_received=75,
            customer_name='Not for email',
            raw_text='Not for email',
        )
    )
    assert context['received_items'] == [
        {
            'part_number': 'AL-6061',
            'part_name': 'Aluminum plate',
            'quantity': '2.5',
            'unit': 'lb',
            'lot_number': 'LOT-007',
        }
    ]
    assert context['receipt_status'] == 'Pending inspection'
    assert 'Not for email' not in str(context)
    assert (
        dispatch._receipt_email_context(receipt_event(quantity_received=20.0))['received_items'][0]['quantity'] == '20'
    )


@pytest.mark.parametrize('quantity', [None, 'invalid', True, -1, 0, 'NaN', 'Infinity'])
def test_invalid_snapshot_falls_back_without_crashing_dispatch(quantity):
    assert dispatch._receipt_email_context(receipt_event(quantity_received=quantity)) is None


def test_old_events_and_other_event_types_keep_generic_template():
    assert dispatch._receipt_email_context(receipt_event(part_number=None)) is None
    event = receipt_event()
    event.event_type = 'receipt_voided'
    assert dispatch._receipt_email_context(event) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('has_snapshot', [True, False])
async def test_outbox_passes_receipt_snapshot_to_email_template(monkeypatch, has_snapshot):
    fan_out = AsyncMock(return_value=1)
    monkeypatch.setattr(dispatch, '_fan_out', fan_out)
    monkeypatch.setattr(dispatch, '_recipients_for_entry', lambda *args: [])
    event = receipt_event(part_number='AL-6061' if has_snapshot else None)
    assert await dispatch.dispatch_for_event(None, event) == 1
    kwargs = fan_out.call_args.kwargs
    assert kwargs['template'] == ('receipt_received' if has_snapshot else None)
    assert ('received_items' in kwargs['context']) == has_snapshot
    assert kwargs['link'] == '/purchasing?po=123'
    assert kwargs['sms_identifier'] == 'RCV-001'
    assert kwargs['sms_detail'] is None


def test_receipt_template_renders_materials_and_escapes_user_content():
    context = dispatch._receipt_email_context(receipt_event(part_name='<script>bad</script> & plate'))
    html = EmailService()._render_template(
        'receipt_received',
        {
            **context,
            'title': 'Material received: RCV-001',
            'year': 2026,
            'notification_link': 'https://erp.test/purchasing?po=123',
            'base_url': 'https://erp.test',
        },
    )
    for value in ['AL-6061', '2.5', 'lb', 'LOT-007', 'PO-123', 'Pending inspection']:
        assert value in html
    assert '<script>bad</script>' not in html
    assert '&lt;script&gt;bad&lt;/script&gt; &amp; plate' in html
    assert 'scope="col"' in html
    assert 'https://erp.test/purchasing?po=123' in html
