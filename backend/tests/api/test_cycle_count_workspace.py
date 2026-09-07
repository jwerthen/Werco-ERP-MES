"""Warehouse count observations and exact reviewed ledger adjustments."""

import pytest

from app.models.inventory import InventoryTransaction, TransactionType
from app.models.user import UserRole
from tests.api.test_inventory_hardening import headers_for, make_inventory_item, make_location, make_part, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


@pytest.fixture
def count_setup(client, db_session):
    manager = make_user(db_session, company_id=1)
    counter = make_user(db_session, company_id=1, role=UserRole.OPERATOR)
    part = make_part(db_session, company_id=1)
    location = make_location(db_session, company_id=1)
    stock = make_inventory_item(db_session, company_id=1, part=part, location=location.code, qty=10)
    created = client.post(
        '/api/v1/inventory/cycle-counts',
        headers=headers_for(manager),
        json={
            'location_code': location.code,
            'scheduled_date': '2026-09-07',
            'assigned_to': counter.id,
        },
    )
    assert created.status_code == 200, created.text
    base = f"/api/v1/inventory/cycle-counts/{created.json()['id']}"
    line = client.get(base, headers=headers_for(manager)).json()['items'][0]
    return manager, counter, stock, base, line


def record(client, counter, base, line, quantity=9):
    assert client.post(f'{base}/start', headers=headers_for(counter)).status_code == 200
    result = client.post(
        f"{base}/items/{line['id']}/count",
        headers=headers_for(counter),
        json={
            'counted_quantity': quantity,
            'notes': 'Physical shelf count',
            'expected_counted_at': None,
        },
    )
    assert result.status_code == 200, result.text


def review(client, manager, base):
    response = client.post(f'{base}/review', headers=headers_for(manager))
    assert response.status_code == 200, response.text
    return response.json()


def post(client, manager, base, reviewed):
    return client.post(
        f'{base}/post-reviewed', headers=headers_for(manager), json={'review_token': reviewed['review_token']}
    )


def test_assigned_workspace_physical_count_and_single_post(client, db_session, count_setup):
    manager, counter, stock, base, line = count_setup
    listing = client.get(
        '/api/v1/inventory/cycle-counts/workspace', headers=headers_for(counter), params={'assigned_to': counter.id}
    ).json()
    assert listing['total'] == 1
    assert listing['items'][0]['assigned_to_name'] == counter.full_name
    assert line['location'] == stock.location
    record(client, counter, base, line)
    db_session.refresh(stock)
    assert stock.quantity_on_hand == 10
    reviewed = review(client, manager, base)
    assert reviewed['items'][0]['posting_delta'] == -1
    response = post(client, manager, base, reviewed)
    assert response.status_code == 200, response.text
    db_session.refresh(stock)
    assert stock.quantity_on_hand == 9
    rows = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.inventory_item_id == stock.id,
            InventoryTransaction.transaction_type == TransactionType.COUNT,
        )
        .all()
    )
    assert len(rows) == 1 and rows[0].quantity == -1
    assert post(client, manager, base, reviewed).status_code == 409


def test_review_requires_all_items_counted(client, count_setup):
    manager, counter, _, base, _ = count_setup
    client.post(f'{base}/start', headers=headers_for(counter))
    assert client.post(f'{base}/review', headers=headers_for(manager)).status_code == 409


def test_operator_counts_but_cannot_review_or_post(client, count_setup):
    manager, counter, _, base, line = count_setup
    record(client, counter, base, line, 0)
    assert client.post(f'{base}/review', headers=headers_for(counter)).status_code == 403
    assert post(client, counter, base, review(client, manager, base)).status_code == 403


def test_stock_movement_invalidates_review_without_posting(client, db_session, count_setup):
    manager, counter, stock, base, line = count_setup
    record(client, counter, base, line)
    reviewed = review(client, manager, base)
    stock.quantity_on_hand = 8
    db_session.commit()
    assert post(client, manager, base, reviewed).status_code == 409
    db_session.refresh(stock)
    assert stock.quantity_on_hand == 8
    assert (
        db_session.query(InventoryTransaction).filter(InventoryTransaction.inventory_item_id == stock.id).count() == 0
    )


def test_reviewed_zero_enrollment_variance_still_uses_current_stock(client, db_session, count_setup):
    manager, counter, stock, base, line = count_setup
    record(client, counter, base, line, 10)
    stock.quantity_on_hand = 8
    db_session.commit()
    reviewed = review(client, manager, base)
    assert reviewed['items'][0]['stock_changed'] is True
    assert reviewed['items'][0]['posting_delta'] == 2
    assert post(client, manager, base, reviewed).status_code == 200
    db_session.refresh(stock)
    assert stock.quantity_on_hand == 10
    assert (
        db_session.query(InventoryTransaction).filter(InventoryTransaction.inventory_item_id == stock.id).one().quantity
        == 2
    )


def test_concurrent_count_cannot_overwrite_observation(client, count_setup):
    _, counter, _, base, line = count_setup
    record(client, counter, base, line)
    url = f"{base}/items/{line['id']}/count"
    assert (
        client.post(
            url, headers=headers_for(counter), json={'counted_quantity': 7, 'expected_counted_at': None}
        ).status_code
        == 409
    )
    detail = client.get(base, headers=headers_for(counter)).json()
    assert detail['items'][0]['counted_quantity'] == 9
    response = client.post(
        url,
        headers=headers_for(counter),
        json={'counted_quantity': 7, 'expected_counted_at': detail['items'][0]['counted_at']},
    )
    assert response.status_code == 200, response.text


def test_picker_assignment_and_review_tenant_scope(client, db_session, count_setup):
    manager, counter, _, base, line = count_setup
    foreign = make_user(db_session, company_id=2)
    picker = client.get('/api/v1/inventory/cycle-counts/counters', headers=headers_for(manager)).json()
    assert counter.id in [row['id'] for row in picker]
    assert foreign.id not in [row['id'] for row in picker]
    assert (
        client.put(f'{base}/assignment', headers=headers_for(manager), json={'assigned_to': foreign.id}).status_code
        == 404
    )
    assert client.get(base, headers=headers_for(foreign)).status_code == 404
    assert client.post(f'{base}/review', headers=headers_for(foreign)).status_code == 404
    record(client, counter, base, line)
    reviewed = review(client, manager, base)
    other_manager = make_user(db_session, company_id=1)
    assert post(client, other_manager, base, reviewed).status_code == 409


def test_invalid_status_and_paging_rejected(client, count_setup):
    manager, _, _, _, _ = count_setup
    for params in ({'status': 'invalid'}, {'limit': 101}, {'offset': -1}):
        assert (
            client.get(
                '/api/v1/inventory/cycle-counts/workspace', headers=headers_for(manager), params=params
            ).status_code
            == 422
        )


def test_recount_after_review_invalidates_posting_and_preserves_stock(client, db_session, count_setup):
    manager, counter, stock, base, line = count_setup
    record(client, counter, base, line)
    reviewed = review(client, manager, base)
    current = client.get(base, headers=headers_for(counter)).json()['items'][0]
    revised = client.post(
        f"{base}/items/{line['id']}/count",
        headers=headers_for(counter),
        json={'counted_quantity': 8, 'expected_counted_at': current['counted_at']},
    )
    assert revised.status_code == 200, revised.text
    assert post(client, manager, base, reviewed).status_code == 409
    db_session.refresh(stock)
    assert stock.quantity_on_hand == 10
    assert (
        db_session.query(InventoryTransaction).filter(InventoryTransaction.inventory_item_id == stock.id).count() == 0
    )


def test_assignment_audit_survives_transaction_close(client, db_session, count_setup):
    from app.models.audit_log import AuditLog

    manager, counter, _, base, _ = count_setup
    changed = client.put(f'{base}/assignment', headers=headers_for(manager), json={'assigned_to': manager.id})
    assert changed.status_code == 200, changed.text
    count_id = changed.json()['id']
    db_session.rollback()
    audits = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == 'cycle_count', AuditLog.resource_id == count_id, AuditLog.action == 'UPDATE')
        .all()
    )
    assert len(audits) == 1
    assert audits[0].company_id == 1
    assert audits[0].old_values == {'assigned_to': counter.id}
    assert audits[0].new_values == {'assigned_to': manager.id}
