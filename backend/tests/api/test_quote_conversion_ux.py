"""User-intent regressions for quote review, conversion and complete history."""

from app.models.part import Part
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.work_order import WorkOrder


def make_quote(db_session, client, headers, part_ids, quantities, *, custom_first=False):
    """Seed a historical customer document; new quotes use fabrication approval."""
    lines = [
        dict(part_id=part, description=f"Quoted part {index}", quantity=qty, unit_price=10)
        for index, (part, qty) in enumerate(zip(part_ids, quantities), 1)
    ]
    if custom_first:
        lines.insert(0, dict(description="Setup service", quantity=1, unit_price=25))
    quote = Quote(
        company_id=1,
        quote_number=f"ARCHIVE-{db_session.query(Quote).count()+1}",
        customer_name='Audit fixture customer',
        payment_terms='Net 30',
        notes='Review full terms',
        subtotal=sum(line['quantity'] * line['unit_price'] for line in lines),
        total=sum(line['quantity'] * line['unit_price'] for line in lines),
    )
    quote.lines = [
        QuoteLine(company_id=1, line_number=i, line_total=line['quantity'] * line['unit_price'], **line)
        for i, line in enumerate(lines, 1)
    ]
    db_session.add(quote)
    db_session.commit()
    response = client.get(f'/api/v1/quotes/{quote.id}', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def mark_sent(client, headers, quote):
    response = client.post(f"/api/v1/quotes/{quote['id']}/send", headers=headers)
    assert response.status_code == 200, response.text


def test_custom_first_line_never_supplies_part_quantity(client, admin_headers, test_part, db_session):
    quote = make_quote(db_session, client, admin_headers, [test_part.id], [20], custom_first=True)
    mark_sent(client, admin_headers, quote)
    plan = client.get(f"/api/v1/quotes/{quote['id']}/conversion-plan", headers=admin_headers).json()
    assert [(line['quantity'], line['eligible']) for line in plan['lines']] == [(1, False), (20, True)]
    part_line = quote['lines'][1]['id']
    needs_review = client.post(
        f"/api/v1/quotes/{quote['id']}/convert", headers=admin_headers, json={'line_ids': [part_line]}
    )
    assert needs_review.status_code == 422
    response = client.post(
        f"/api/v1/quotes/{quote['id']}/convert",
        headers=admin_headers,
        json={'line_ids': [part_line], 'acknowledge_unlinked': True},
    )
    assert response.status_code == 200, response.text
    wo = db_session.get(WorkOrder, response.json()['work_order_id'])
    assert wo.quantity_ordered == 20
    assert wo.part_id == test_part.id and wo.company_id == 1
    detail = client.get(f"/api/v1/quotes/{quote['id']}", headers=admin_headers).json()
    assert detail['lines'][1]['work_order_id'] == wo.id
    assert detail['lines'][0]['work_order_id'] is None
    assert detail['status'] == 'converted'


def test_multiple_lines_convert_explicitly_and_remaining_scope_stays_open(client, admin_headers, test_part, db_session):
    second = Part(
        company_id=1,
        part_number='UX-SECOND',
        name='Second part',
        part_type='manufactured',
        unit_of_measure='each',
        is_active=True,
    )
    db_session.add(second)
    db_session.commit()
    quote = make_quote(db_session, client, admin_headers, [test_part.id, second.id], [5, 12])
    mark_sent(client, admin_headers, quote)
    one = client.post(
        f"/api/v1/quotes/{quote['id']}/convert", headers=admin_headers, json={'line_ids': [quote['lines'][1]['id']]}
    )
    assert one.status_code == 200, one.text
    assert one.json()['remaining_line_ids'] == [quote['lines'][0]['id']]
    assert db_session.get(Quote, quote['id']).status == QuoteStatus.ACCEPTED
    repeated = client.post(
        f"/api/v1/quotes/{quote['id']}/convert", headers=admin_headers, json={'line_ids': [quote['lines'][1]['id']]}
    )
    assert repeated.status_code == 422
    two = client.post(
        f"/api/v1/quotes/{quote['id']}/convert", headers=admin_headers, json={'line_ids': [quote['lines'][0]['id']]}
    )
    assert two.status_code == 200, two.text
    assert two.json()['remaining_line_ids'] == []
    orders = db_session.query(WorkOrder).order_by(WorkOrder.id).all()
    assert [(wo.part_id, wo.quantity_ordered) for wo in orders] == [(second.id, 12), (test_part.id, 5)]
    assert len({wo.work_order_number for wo in orders}) == 2


def test_archive_line_edit_is_frozen_but_commercial_terms_remain_editable(client, admin_headers, test_part, db_session):
    quote = make_quote(db_session, client, admin_headers, [test_part.id], [4])
    line = quote['lines'][0]
    response = client.put(
        f"/api/v1/quotes/{quote['id']}",
        headers=admin_headers,
        json={
            'notes': 'Corrected terms',
            'lines': [{**line, 'quantity': 7, 'notes': 'Retain this instruction'}],
        },
    )
    assert response.status_code == 409, response.text
    response = client.put(f"/api/v1/quotes/{quote['id']}", headers=admin_headers, json={'notes': 'Corrected terms'})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated['lines'][0]['id'] == line['id']
    assert updated['total'] == 40 and updated['notes'] == 'Corrected terms'
    mark_sent(client, admin_headers, quote)
    denied = client.put(f"/api/v1/quotes/{quote['id']}", headers=admin_headers, json={'notes': 'Change issued content'})
    assert denied.status_code == 409


def test_pending_rfq_quote_has_send_next_step_and_history_is_explicit(client, admin_headers, test_part, db_session):
    quote = make_quote(db_session, client, admin_headers, [test_part.id], [2])
    row = db_session.get(Quote, quote['id'])
    row.status = QuoteStatus.PENDING
    db_session.commit()
    mark_sent(client, admin_headers, quote)
    assert db_session.get(Quote, quote['id']).status == QuoteStatus.SENT
    row.status = QuoteStatus.EXPIRED
    db_session.commit()
    assert client.get('/api/v1/quotes/', headers=admin_headers).json() == []
    history = client.get(
        '/api/v1/quotes/', headers=admin_headers, params={'status': 'all', 'search': quote['quote_number']}
    )
    assert [q['id'] for q in history.json()] == [quote['id']]


def test_nonproduction_lines_do_not_leave_fully_converted_quote_stuck_open(
    client, admin_headers, test_part, db_session
):
    purchased = Part(
        company_id=1,
        part_number='UX-PURCHASED',
        name='Purchased hardware',
        part_type='purchased',
        unit_of_measure='each',
        is_active=True,
    )
    db_session.add(purchased)
    db_session.commit()
    quote = make_quote(db_session, client, admin_headers, [test_part.id, purchased.id], [3, 10])
    mark_sent(client, admin_headers, quote)
    plan = client.get(f"/api/v1/quotes/{quote['id']}/conversion-plan", headers=admin_headers).json()
    assert plan['lines'][1]['eligible'] is False
    response = client.post(
        f"/api/v1/quotes/{quote['id']}/convert", headers=admin_headers, json={'line_ids': [quote['lines'][0]['id']]}
    )
    assert response.status_code == 200, response.text
    assert response.json()['remaining_line_ids'] == []
    assert db_session.get(Quote, quote['id']).status == QuoteStatus.CONVERTED
    assert db_session.query(WorkOrder).filter(WorkOrder.part_id == purchased.id).count() == 0


def test_conversion_uses_same_tenant_number_sequence_and_lock_as_normal_creation(
    client, admin_headers, test_part, db_session
):
    from datetime import datetime
    from unittest.mock import patch

    from app.models.company import Company

    other_company = Company(name='Other tenant', slug='ux-number-other')
    db_session.add(other_company)
    db_session.flush()
    other_part = Part(
        company_id=other_company.id,
        part_number='UX-OTHER',
        name='Other part',
        part_type='manufactured',
        unit_of_measure='each',
        is_active=True,
    )
    db_session.add(other_part)
    db_session.flush()
    prefix = datetime.now().strftime('WO-%Y%m%d-')
    db_session.add(
        WorkOrder(
            company_id=other_company.id, work_order_number=prefix + '900', part_id=other_part.id, quantity_ordered=1
        )
    )
    db_session.commit()
    quote = make_quote(db_session, client, admin_headers, [test_part.id], [3])
    mark_sent(client, admin_headers, quote)
    with patch('app.api.endpoints.work_orders.acquire_generator_lock') as lock:
        response = client.post(
            f"/api/v1/quotes/{quote['id']}/convert", headers=admin_headers, json={'line_ids': [quote['lines'][0]['id']]}
        )
    assert response.status_code == 200, response.text
    assert response.json()['work_order_number'] == prefix + '001'
    lock.assert_called_once_with(db_session, 'work_order_number', 1)


def test_inactive_production_line_remains_open_for_resolution(client, admin_headers, test_part, db_session):
    inactive = Part(
        company_id=1,
        part_number='UX-INACTIVE',
        name='Inactive production',
        part_type='manufactured',
        unit_of_measure='each',
        is_active=False,
    )
    db_session.add(inactive)
    db_session.commit()
    quote = make_quote(db_session, client, admin_headers, [test_part.id, inactive.id], [3, 2])
    mark_sent(client, admin_headers, quote)
    response = client.post(
        f"/api/v1/quotes/{quote['id']}/convert", headers=admin_headers, json={'line_ids': [quote['lines'][0]['id']]}
    )
    assert response.status_code == 200, response.text
    assert response.json()['remaining_line_ids'] == [quote['lines'][1]['id']]
    assert db_session.get(Quote, quote['id']).status == QuoteStatus.ACCEPTED


def test_partial_conversion_cannot_be_reopened_to_delete_linked_lines(client, admin_headers, test_part, db_session):
    quote = make_quote(db_session, client, admin_headers, [test_part.id, test_part.id], [3, 5])
    mark_sent(client, admin_headers, quote)
    response = client.post(
        f"/api/v1/quotes/{quote['id']}/convert", headers=admin_headers, json={'line_ids': [quote['lines'][0]['id']]}
    )
    assert response.status_code == 200, response.text
    reopened = client.put(f"/api/v1/quotes/{quote['id']}", headers=admin_headers, json={'status': 'draft'})
    assert reopened.status_code == 409
    record = db_session.get(Quote, quote['id'])
    assert record.status == QuoteStatus.ACCEPTED
    assert record.lines[0].work_order_id == response.json()['work_order_id']
    assert len(record.lines) == 2


def test_stale_commercial_snapshot_cannot_overwrite_another_editors_changes(
    client, admin_headers, test_part, db_session
):
    quote = make_quote(db_session, client, admin_headers, [test_part.id], [4])
    edited = client.put(
        f"/api/v1/quotes/{quote['id']}",
        headers=admin_headers,
        json={'expected_updated_at': quote['updated_at'], 'notes': 'Reviewed terms'},
    )
    assert edited.status_code == 200, edited.text
    stale = client.put(
        f"/api/v1/quotes/{quote['id']}",
        headers=admin_headers,
        json={'expected_updated_at': quote['updated_at'], 'notes': 'Stale terms'},
    )
    assert stale.status_code == 409, stale.text
    current = client.get(f"/api/v1/quotes/{quote['id']}", headers=admin_headers).json()
    assert current['total'] == 40 and current['notes'] == 'Reviewed terms'
