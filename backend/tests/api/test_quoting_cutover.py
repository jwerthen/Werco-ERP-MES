"""The fabrication approval workflow is the sole writer of quote pricing."""

import pytest

from app.models.quote import Quote
from tests.api.test_fabrication_quotes import BASE, approve, create, customer


@pytest.mark.parametrize(
    'path',
    [
        '/quote-calc/materials',
        '/rfq-packages/',
        '/estimate-workbench/shop-data',
        '/quote-nesting/drafts',
        '/quote-nesting/runs',
        '/quote-nesting/buyer-pdf',
        '/dxf-parser/analyze',
        '/admin/settings/materials',
        '/admin/settings/machines',
        '/admin/settings/finishes',
        '/admin/settings/labor-rates',
        '/admin/settings/outside-services',
        '/admin/settings/overhead',
        '/admin/settings/seed-labor-rates',
        '/admin/settings/seed-outside-services',
    ],
)
def test_retired_quoting_routes_are_not_runnable(client, admin_headers, path):
    assert client.get('/api/v1' + path, headers=admin_headers).status_code == 404
    assert client.post('/api/v1' + path, headers=admin_headers, json={}).status_code == 404


def test_only_approved_fabrication_handoff_creates_customer_quote(client, admin_headers, db_session):
    payload = {'customer_name': 'Bypass', 'lines': [{'description': 'Unreviewed', 'quantity': 1, 'unit_price': 1}]}
    assert client.post('/api/v1/quotes/', headers=admin_headers, json=payload).status_code == 405
    assert db_session.query(Quote).count() == 0
    row = create(client, admin_headers, customer_id=customer(db_session))
    unreviewed = client.post(
        f"{BASE}/{row['id']}/handoff", headers=admin_headers, json={'expected_revision': row['revision']}
    )
    assert unreviewed.status_code == 409
    assert db_session.query(Quote).count() == 0
    approved = approve(client, admin_headers, row)
    handoff = client.post(
        f"{BASE}/{row['id']}/handoff", headers=admin_headers, json={'expected_revision': approved['revision']}
    )
    assert handoff.status_code == 200, handoff.text
    quote_id = handoff.json()['erp_quote_id']
    detail = client.get(f'/api/v1/quotes/{quote_id}', headers=admin_headers).json()
    assert detail['fabrication_quote_id'] == row['id']
    listing = client.get('/api/v1/quotes/', headers=admin_headers).json()
    assert listing[0]['fabrication_quote_id'] == row['id']
    line = detail['lines'][0]
    refused = client.put(
        f'/api/v1/quotes/{quote_id}',
        headers=admin_headers,
        json={'lines': [{**line, 'quantity': 100, 'unit_price': 1}]},
    )
    assert refused.status_code == 409
    assert (
        client.post(f'/api/v1/quotes/{quote_id}/lines', headers=admin_headers, json=payload['lines'][0]).status_code
        == 404
    )
    terms = client.put(f'/api/v1/quotes/{quote_id}', headers=admin_headers, json={'payment_terms': 'Net 30'})
    assert terms.status_code == 200, terms.text
    assert terms.json()['total'] == detail['total']
    assert terms.json()['lines'] == detail['lines']
    assert terms.json()['fabrication_quote_id'] == row['id']
