"""Consumable issues preserve employee attribution and reconcile to stock atomically."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.consumable import ConsumableCheckout
from app.models.inventory import InventoryItem, InventoryLocation, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService, AuditWriteError
from app.services.consumable_service import stock_rows_query
from tests.api.export_audit_helpers import committed_export_rows

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
URL = '/api/v1/consumables'


def token(user):
    return {'Authorization': f'Bearer {create_access_token(subject=user.id, company_id=user.company_id)}'}


@pytest.fixture
def stock(db_session):
    part = Part(
        company_id=1,
        part_number='DISC-045',
        name='Cutoff wheel 4.5 inch',
        part_type='consumable',
        unit_of_measure='each',
        reorder_point=8,
        is_active=True,
    )
    location = InventoryLocation(company_id=1, code='CRIB-A', warehouse='MAIN', is_active=True, is_pickable=True)
    db_session.add_all([part, location])
    db_session.flush()
    item = InventoryItem(
        company_id=1,
        part_id=part.id,
        warehouse='MAIN',
        location=location.code,
        lot_number='LOT-001',
        quantity_on_hand=20,
        quantity_allocated=0,
        quantity_available=20,
        unit_cost=2.5,
        status='available',
        is_active=True,
    )
    db_session.add(item)
    db_session.commit()
    return item, part, location


def body(stock, employee, **changes):
    return {
        'inventory_item_id': stock[0].id,
        'employee_id': employee.id,
        'quantity': 3,
        'notes': 'Weld bay replenishment',
        'request_id': str(uuid4()),
    } | changes


def checkout(client, headers, payload):
    response = client.post(URL + '/checkouts', headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def assert_stock_unchanged(db, item, quantity=20):
    db.refresh(item)
    assert item.quantity_on_hand == quantity
    assert (
        db.query(InventoryTransaction).filter(InventoryTransaction.reference_type == 'consumable_checkout').count() == 0
    )


def test_checkout_records_employee_and_stock_ledger_atomically(
    client, auth_headers, test_user, operator_user, stock, db_session
):
    result = checkout(client, auth_headers, body(stock, operator_user))
    db_session.refresh(stock[0])
    assert stock[0].quantity_on_hand == 17
    assert stock[0].quantity_available == 17
    ledger = db_session.query(InventoryTransaction).one()
    assert ledger.company_id == 1
    assert ledger.inventory_item_id == stock[0].id
    assert ledger.part_id == stock[1].id
    assert ledger.transaction_type == TransactionType.ISSUE
    assert ledger.quantity == -3
    assert ledger.created_by == test_user.id
    assert ledger.reference_type == 'consumable_checkout'
    assert ledger.reference_id == result['id']
    assert ledger.lot_number == 'LOT-001'
    assert result['employee_id'] == operator_user.id
    assert result['employee_name'] == operator_user.full_name
    assert 'employee_code' not in result
    assert db_session.get(ConsumableCheckout, result['id']).employee_code == operator_user.employee_id
    assert result['issued_by'] == test_user.id
    assert result['issued_by_name'] == test_user.full_name
    assert result['issued_at'].endswith('Z')
    assert result['part_name'] == stock[1].name
    assert result['quantity'] == 3
    assert result['unit_of_measure'] == 'each'
    audits = db_session.query(AuditLog).all()
    assert audits and all(row.company_id == 1 for row in audits)
    assert any(row.resource_type == 'consumable_checkout' for row in audits)
    history = client.get(URL + '/checkouts', headers=auth_headers).json()
    assert history['total'] == 1
    assert history['items'][0] == result


def test_exact_retry_returns_same_checkout_and_cannot_repeat_inventory_effect(
    client, auth_headers, operator_user, stock, db_session
):
    payload = body(stock, operator_user)
    result = checkout(client, auth_headers, payload)
    audit_count = db_session.query(AuditLog).count()
    replay = client.post(URL + '/checkouts', headers=auth_headers, json=payload)
    assert replay.status_code in (200, 201), replay.text
    assert replay.json() == result | {'replayed': True}
    assert db_session.query(InventoryTransaction).count() == 1
    assert db_session.query(AuditLog).count() == audit_count
    db_session.refresh(stock[0])
    assert stock[0].quantity_on_hand == 17
    conflict = client.post(URL + '/checkouts', headers=auth_headers, json=payload | {'quantity': 4})
    assert conflict.status_code == 409
    db_session.refresh(stock[0])
    assert stock[0].quantity_on_hand == 17


def test_retry_key_is_bound_to_issuer(client, auth_headers, supervisor_headers, operator_user, stock, db_session):
    payload = body(stock, operator_user)
    checkout(client, auth_headers, payload)
    replay = client.post(URL + '/checkouts', headers=supervisor_headers, json=payload)
    assert replay.status_code == 409
    assert db_session.query(InventoryTransaction).count() == 1


def test_authentication_and_read_only_role_fences(
    client, operator_headers, supervisor_headers, operator_user, stock, db_session
):
    assert client.get(URL + '/metadata').status_code == 401
    assert client.get(URL + '/stock').status_code == 401
    assert client.get(URL + '/checkouts').status_code == 401
    payload = body(stock, operator_user)
    assert client.post(URL + '/checkouts', json=payload).status_code == 401
    assert client.get(URL + '/metadata', headers=operator_headers).json()['can_manage'] is False
    assert client.get(URL + '/checkouts', headers=operator_headers).status_code == 200
    assert client.post(URL + '/checkouts', headers=operator_headers, json=payload).status_code == 403
    assert_stock_unchanged(db_session, stock[0])
    assert client.get(URL + '/metadata', headers=supervisor_headers).json()['can_manage'] is True
    checkout(client, supervisor_headers, payload)


@pytest.mark.parametrize('quantity', [0, -1, 'NaN', 'Infinity', '-Infinity', None])
def test_invalid_quantity_never_changes_inventory(client, auth_headers, operator_user, stock, db_session, quantity):
    response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user, quantity=quantity))
    assert response.status_code == 422, response.text
    assert_stock_unchanged(db_session, stock[0])


@pytest.mark.parametrize('changes', [{'company_id': 2}, {'issued_at': '2020-01-01T00:00:00Z'}, {'issued_by': 99}])
def test_actor_company_and_timestamp_cannot_be_spoofed(client, auth_headers, operator_user, stock, changes):
    response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user, **changes))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    'change',
    [
        'held',
        'expired',
        'inactive_item',
        'inactive_part',
        'deleted_part',
        'wrong_type',
        'nonpickable',
        'inactive_location',
        'inactive_employee',
    ],
)
def test_unavailable_stock_and_inactive_recipients_cannot_be_issued(
    client, auth_headers, operator_user, stock, db_session, change
):
    item, part, location = stock
    if change == 'held':
        item.status = 'on_hold'
    elif change == 'expired':
        item.expiration_date = datetime.utcnow() - timedelta(days=1)
    elif change == 'inactive_item':
        item.is_active = False
    elif change == 'inactive_part':
        part.is_active = False
    elif change == 'deleted_part':
        part.is_deleted = True
    elif change == 'wrong_type':
        part.part_type = 'purchased'
    elif change == 'nonpickable':
        location.is_pickable = False
    elif change == 'inactive_location':
        location.is_active = False
    else:
        operator_user.is_active = False
    db_session.commit()
    response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user))
    assert response.status_code in (404, 409, 422), response.text
    assert_stock_unchanged(db_session, item)


def test_oversubscribed_quantity_is_refused_using_real_stock_not_stale_available_cache(
    client, auth_headers, operator_user, stock, db_session
):
    stock[0].quantity_on_hand = 2
    stock[0].quantity_available = 99
    db_session.commit()
    response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user))
    assert response.status_code == 409, response.text
    assert_stock_unchanged(db_session, stock[0], quantity=2)


@pytest.mark.parametrize('failed_audit', [1, 2, 3])
def test_audit_failure_rolls_back_stock_and_checkout_and_allows_retry(
    client, auth_headers, operator_user, stock, db_session, monkeypatch, failed_audit
):
    original = AuditService.log_required
    call_count = 0

    def fail(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == failed_audit:
            raise AuditWriteError('audit unavailable')
        return original(*args, **kwargs)

    monkeypatch.setattr(AuditService, 'log_required', fail)
    payload = body(stock, operator_user)
    response = client.post(URL + '/checkouts', headers=auth_headers, json=payload)
    assert response.status_code == 503, response.text
    assert_stock_unchanged(db_session, stock[0])
    assert db_session.query(AuditLog).count() == 0
    assert client.get(URL + '/checkouts', headers=auth_headers).json()['total'] == 0
    monkeypatch.setattr(AuditService, 'log_required', original)
    checkout(client, auth_headers, payload)


def reserve(db, stock, *, quantity, pin, code='WO-CONSUMABLE'):
    item, part, _ = stock
    job = WorkOrder(
        company_id=1, work_order_number=code, part_id=part.id, quantity_ordered=1, status=WorkOrderStatus.RELEASED
    )
    db.add(job)
    db.flush()
    allocation = WorkOrderMaterialAllocation(
        company_id=1,
        work_order_id=job.id,
        part_id=part.id,
        source=AllocationSource.MANUAL,
        status=AllocationStatus.OPEN,
        qty_planned=quantity,
        qty_consumed=0,
        unit_of_measure='each',
        pinned_inventory_item_id=item.id if pin else None,
    )
    db.add(allocation)
    db.commit()
    return allocation


@pytest.mark.parametrize('reservation', ['allocated', 'pinned', 'unpinned'])
def test_open_job_reservations_cannot_be_consumed_at_crib(
    client, auth_headers, operator_user, stock, db_session, reservation
):
    if reservation == 'allocated':
        stock[0].quantity_allocated = 18
        db_session.commit()
    else:
        reserve(db_session, stock, quantity=18, pin=reservation == 'pinned')
    response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user))
    assert response.status_code == 409, response.text
    assert_stock_unchanged(db_session, stock[0])
    checkout(client, auth_headers, body(stock, operator_user, quantity=2))


def test_pinned_demand_does_not_block_a_different_lots_free_stock(
    client, auth_headers, operator_user, stock, db_session
):
    reserve(db_session, stock, quantity=20, pin=True)
    spare = InventoryItem(
        company_id=1,
        part_id=stock[1].id,
        location='CRIB-A',
        warehouse='MAIN',
        lot_number='SPARE',
        quantity_on_hand=5,
        quantity_available=5,
        quantity_allocated=0,
        status='available',
        is_active=True,
    )
    db_session.add(spare)
    db_session.commit()
    checkout(client, auth_headers, body(stock, operator_user, inventory_item_id=spare.id, quantity=5))
    db_session.refresh(stock[0])
    db_session.refresh(spare)
    assert stock[0].quantity_on_hand == 20
    assert spare.quantity_on_hand == 0


def foreign_stock(db, user):
    db.add(Company(id=2, name='Another manufacturer', slug='another', is_active=True))
    person = User(
        company_id=2,
        employee_id='OTHER-1',
        first_name='Other',
        last_name='Employee',
        email='other@example.test',
        hashed_password=user.hashed_password,
        role=UserRole.MANAGER,
        is_active=True,
    )
    part = Part(
        company_id=2,
        part_number='PRIVATE-DISC',
        name='Hidden abrasives',
        part_type='consumable',
        unit_of_measure='each',
        is_active=True,
    )
    db.add_all([person, part])
    db.flush()
    item = InventoryItem(
        company_id=2,
        part_id=part.id,
        location='FOREIGN',
        warehouse='MAIN',
        lot_number='FOREIGN',
        quantity_on_hand=20,
        quantity_available=20,
        quantity_allocated=0,
        status='available',
        is_active=True,
    )
    db.add(item)
    db.commit()
    return person, item, part


def test_tenant_isolation_covers_history_stock_people_and_every_reference(
    client, auth_headers, test_user, operator_user, stock, db_session
):
    person, item, part = foreign_stock(db_session, test_user)
    foreign = checkout(client, token(person), body(stock, person, inventory_item_id=item.id))
    own = checkout(client, auth_headers, body(stock, operator_user))
    history = client.get(URL + '/checkouts', headers=auth_headers).json()
    assert [row['id'] for row in history['items']] == [own['id']]
    assert client.get(URL + '/checkouts', headers=auth_headers, params={'employee_id': person.id}).json()['total'] == 0
    assert client.get(URL + '/checkouts', headers=auth_headers, params={'part_id': part.id}).json()['total'] == 0
    people = client.get(URL + '/metadata', headers=auth_headers).json()['people']
    assert person.id not in [row['id'] for row in people]
    listed_stock = client.get(URL + '/stock', headers=auth_headers).json()['items']
    assert item.id not in [row['inventory_item_id'] for row in listed_stock]
    for changes in ({'employee_id': person.id}, {'inventory_item_id': item.id}):
        response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user, **changes))
        assert response.status_code in (404, 422), response.text
    db_session.refresh(item)
    assert item.quantity_on_hand == 17
    assert foreign['employee_id'] == person.id


def test_poisoned_cross_tenant_part_reference_is_not_selectable(
    client, auth_headers, test_user, operator_user, stock, db_session
):
    _, _, other_part = foreign_stock(db_session, test_user)
    stock[0].part_id = other_part.id
    db_session.commit()
    response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user))
    assert response.status_code in (404, 422), response.text
    assert_stock_unchanged(db_session, stock[0])
    listed = client.get(URL + '/stock', headers=auth_headers).json()['items']
    assert all(row['inventory_item_id'] != stock[0].id for row in listed)


def test_historical_identity_remains_after_rename_deactivation_and_part_deletion(
    client, auth_headers, test_user, operator_user, stock, db_session
):
    result = checkout(client, auth_headers, body(stock, operator_user))
    operator_user.first_name = 'Renamed'
    operator_user.employee_id = 'NEW-BADGE'
    operator_user.is_active = False
    stock[1].name = 'New name'
    stock[1].part_number = 'NEW-SKU'
    stock[1].is_deleted = True
    db_session.commit()
    history = client.get(URL + '/checkouts', headers=auth_headers).json()
    assert history['items'] == [result]
    assert client.get(URL + '/checkouts', headers=auth_headers, params={'q': 'EMP-OP-001'}).json()['total'] == 0
    assert db_session.get(ConsumableCheckout, result['id']).employee_code == 'EMP-OP-001'
    assert (
        client.get(URL + '/checkouts', headers=auth_headers, params={'q': result['part_number']}).json()['total'] == 1
    )


def test_history_filters_pagination_and_csv_preserve_details(client, auth_headers, test_user, operator_user, stock):
    first = checkout(client, auth_headers, body(stock, operator_user, notes='First issue, two stations'))
    second = checkout(client, auth_headers, body(stock, test_user, quantity=1, notes='Manager issue'))
    all_rows = client.get(URL + '/checkouts', headers=auth_headers).json()
    assert [row['id'] for row in all_rows['items']] == [second['id'], first['id']]
    page = client.get(URL + '/checkouts', headers=auth_headers, params={'skip': 1, 'limit': 1}).json()
    assert page['total'] == 2 and [row['id'] for row in page['items']] == [first['id']]
    filters = {'employee_id': operator_user.id, 'part_id': stock[1].id, 'q': 'First issue'}
    filtered = client.get(URL + '/checkouts', headers=auth_headers, params=filters).json()
    assert filtered['items'] == [first]
    exported = client.get(URL + '/checkouts/export.csv', headers=auth_headers, params=filters)
    assert exported.status_code == 200, exported.text
    assert 'text/csv' in exported.headers['content-type']
    assert 'First issue, two stations' in exported.text
    assert 'Manager issue' not in exported.text
    assert operator_user.employee_id not in exported.text
    assert operator_user.full_name in exported.text


def test_history_rejects_invalid_pagination_and_date_ranges(client, auth_headers):
    for params in (
        {'skip': -1},
        {'limit': 0},
        {'date_from': 'nonsense'},
        {'date_from': '2026-10-28', 'date_to': '2026-10-27'},
    ):
        assert client.get(URL + '/checkouts', headers=auth_headers, params=params).status_code == 422


def test_immutable_checkout_cannot_be_edited_or_deleted(client, auth_headers, operator_user, stock, db_session):
    result = checkout(client, auth_headers, body(stock, operator_user))
    row = db_session.get(ConsumableCheckout, result['id'])
    row.quantity = 99
    with pytest.raises(ValueError, match='cannot be modified or deleted'):
        db_session.flush()
    db_session.rollback()
    db_session.delete(db_session.get(ConsumableCheckout, result['id']))
    with pytest.raises(ValueError, match='cannot be modified or deleted'):
        db_session.flush()
    db_session.rollback()
    assert client.get(URL + '/checkouts', headers=auth_headers).json()['items'] == [result]


@pytest.mark.parametrize(
    'day,start,end',
    [
        ('2026-03-08', datetime(2026, 3, 8, 6), datetime(2026, 3, 9, 5)),
        ('2026-11-01', datetime(2026, 11, 1, 5), datetime(2026, 11, 2, 6)),
    ],
)
def test_central_date_filter_respects_23_and_25_hour_days(
    client, auth_headers, test_user, operator_user, stock, db_session, day, start, end
):
    # Seed immutable historical evidence at exact Central-day boundaries. Never
    # rewrite issued_at on a real posted record merely to set a fixture clock.
    expected_ids = []
    times = (start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end)
    for index, issued_at in enumerate(times):
        ledger = InventoryTransaction(
            company_id=1,
            inventory_item_id=stock[0].id,
            part_id=stock[1].id,
            transaction_type=TransactionType.ISSUE,
            quantity=-1,
            reference_type='consumable_checkout',
            created_by=test_user.id,
            created_at=issued_at,
        )
        db_session.add(ledger)
        db_session.flush()
        row = ConsumableCheckout(
            company_id=1,
            inventory_item_id=stock[0].id,
            inventory_transaction_id=ledger.id,
            part_id=stock[1].id,
            part_number=stock[1].part_number,
            part_name=stock[1].name,
            unit_of_measure='each',
            location='CRIB-A',
            employee_id=operator_user.id,
            employee_name=operator_user.full_name,
            employee_code=operator_user.employee_id,
            issued_by=test_user.id,
            issued_by_name=test_user.full_name,
            issued_at=issued_at,
            quantity=1,
            request_id=str(uuid4()),
            request_hash='a' * 64,
        )
        db_session.add(row)
        db_session.flush()
        ledger.reference_id = row.id
        if index in (1, 2):
            expected_ids.append(row.id)
    db_session.commit()
    response = client.get(URL + '/checkouts', headers=auth_headers, params={'date_from': day, 'date_to': day})
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 2
    assert [row['id'] for row in response.json()['items']] == list(reversed(expected_ids))


def test_csv_neutralizes_spreadsheet_formula_text(client, auth_headers, operator_user, stock):
    checkout(client, auth_headers, body(stock, operator_user, notes='=HYPERLINK("https://example.test")'))
    exported = client.get(URL + '/checkouts/export.csv', headers=auth_headers)
    assert exported.status_code == 200, exported.text
    import csv
    from io import StringIO

    rows = list(csv.reader(StringIO(exported.text)))
    formula_cell = next(cell for cell in rows[1] if 'HYPERLINK' in cell)
    assert not formula_cell.startswith('=')


def test_stock_discovery_includes_catalog_items_awaiting_receipt_and_low_stock_filter(
    client, auth_headers, stock, db_session
):
    new_part = Part(
        company_id=1,
        part_number='GLOVE-L',
        name='Nitrile gloves large',
        part_type='consumable',
        unit_of_measure='each',
        reorder_point=10,
        is_active=True,
    )
    db_session.add(new_part)
    db_session.commit()
    result = client.get(URL + '/stock', headers=auth_headers).json()
    assert result['total'] == 2
    stocked = next(row for row in result['items'] if row['part_id'] == stock[1].id)
    assert stocked['quantity_available'] == 20 and stocked['can_checkout'] is True
    assert stocked['is_low_stock'] is False
    empty = next(row for row in result['items'] if row['part_id'] == new_part.id)
    assert empty['inventory_item_id'] is None
    assert empty['quantity_available'] == 0 and empty['can_checkout'] is False
    assert empty['is_low_stock'] is True and empty['unavailable_reason']
    low = client.get(URL + '/stock', headers=auth_headers, params={'low_stock': True}).json()
    assert [row['part_id'] for row in low['items']] == [new_part.id]
    search = client.get(URL + '/stock', headers=auth_headers, params={'q': 'LOT-001'}).json()
    assert search['total'] == 1 and search['items'][0]['inventory_item_id'] == stock[0].id
    assert all('unit_cost' not in row for row in result['items'])


def test_metadata_employee_picker_discloses_only_operational_identity(client, auth_headers, operator_user):
    people = client.get(URL + '/metadata', headers=auth_headers).json()['people']
    operator = next(person for person in people if person['id'] == operator_user.id)
    assert operator['name'] == operator_user.full_name
    assert set(operator) == {'id', 'name', 'department', 'is_active'}


def test_each_requires_whole_units_but_measured_consumables_accept_fractions(
    client, auth_headers, operator_user, stock, db_session
):
    response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user, quantity=0.5))
    assert response.status_code == 422
    assert_stock_unchanged(db_session, stock[0])
    stock[1].unit_of_measure = 'gallons'
    db_session.commit()
    result = checkout(client, auth_headers, body(stock, operator_user, quantity=0.5))
    assert result['quantity'] == 0.5 and result['unit_of_measure'] == 'gallons'
    db_session.refresh(stock[0])
    assert stock[0].quantity_on_hand == 19.5


def test_stock_locks_compile_for_postgres_and_reads_do_not_lock(db_session):
    # SQLite ignores FOR UPDATE, so inspect the actual production SQL dialect.
    locked = stock_rows_query(db_session, 1, 3, for_update=True).statement.compile(dialect=postgresql.dialect())
    plain = stock_rows_query(db_session, 1, 3).statement.compile(dialect=postgresql.dialect())
    assert 'FOR UPDATE' in str(locked)
    assert 'ORDER BY inventory_items.id' in str(locked)
    assert 'inventory_items.company_id' in str(locked)
    assert 'FOR UPDATE' not in str(plain)


def test_read_only_company_context_keeps_manager_from_issuing_stock(
    client, test_user, operator_user, stock, db_session
):
    headers = {'Authorization': f'Bearer {create_access_token(subject=test_user.id, company_id=1, read_only=True)}'}
    assert client.get(URL + '/metadata', headers=headers).json()['can_manage'] is False
    assert client.get(URL + '/stock', headers=headers).status_code == 200
    assert client.post(URL + '/checkouts', headers=headers, json=body(stock, operator_user)).status_code == 403
    assert_stock_unchanged(db_session, stock[0])


def test_mixed_pinned_and_unpinned_demand_is_protected_across_successive_lot_issues(
    client, auth_headers, operator_user, stock, db_session
):
    reserve(db_session, stock, quantity=10, pin=True, code='WO-PINNED')
    reserve(db_session, stock, quantity=7, pin=False, code='WO-UNPINNED')
    spare = InventoryItem(
        company_id=1,
        part_id=stock[1].id,
        location='CRIB-A',
        warehouse='MAIN',
        lot_number='SPARE',
        quantity_on_hand=5,
        quantity_available=5,
        quantity_allocated=0,
        status='available',
        is_active=True,
    )
    db_session.add(spare)
    db_session.commit()
    # Across both lots 25 are on hand, 17 reserved, so issue all eight free units.
    checkout(client, auth_headers, body(stock, operator_user, quantity=8))
    response = client.post(
        URL + '/checkouts',
        headers=auth_headers,
        json=body(stock, operator_user, inventory_item_id=spare.id, quantity=1),
    )
    assert response.status_code == 409, response.text
    db_session.refresh(stock[0])
    db_session.refresh(spare)
    assert stock[0].quantity_on_hand == 12
    assert spare.quantity_on_hand == 5
    assert db_session.query(InventoryTransaction).count() == 1


@pytest.mark.parametrize(
    'role', [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER]
)
def test_login_badge_secrets_are_never_disclosed_or_searchable_in_consumables(
    client, auth_headers, test_user, operator_user, stock, db_session, role
):
    operator_user.employee_id = 'PRIVATE-LOGIN-BADGE-47'
    db_session.commit()
    payload = body(stock, operator_user)
    created = client.post(URL + '/checkouts', headers=auth_headers, json=payload)
    replay = client.post(URL + '/checkouts', headers=auth_headers, json=payload)
    assert created.status_code == 201 and replay.status_code == 201
    for response in (created, replay):
        assert operator_user.employee_id not in response.text
        assert 'employee_code' not in response.json()
        assert response.json()['employee_id'] == operator_user.id  # A record PK is not the login badge.
    test_user.role = role
    db_session.commit()
    for endpoint in ('/metadata', '/checkouts'):
        response = client.get(URL + endpoint, headers=auth_headers)
        assert response.status_code == 200
        assert operator_user.employee_id not in response.text
        assert 'employee_code' not in response.text
    # A guessed badge must not become a valid/invalid login-credential oracle.
    for guess in ('PRIVATE-LOGIN-BADGE-47', 'PRIVATE-LOGIN-BADGE'):
        result = client.get(URL + '/checkouts', headers=auth_headers, params={'q': guess}).json()
        assert result['total'] == 0
    permitted = role in (UserRole.ADMIN, UserRole.MANAGER)
    assert client.get(URL + '/metadata', headers=auth_headers).json()['can_export'] is permitted
    csv = client.get(URL + '/checkouts/export.csv', headers=auth_headers)
    assert csv.status_code == (200 if permitted else 403)
    assert operator_user.employee_id not in csv.text
    assert 'Employee ID' not in csv.text


def test_export_audit_is_committed_before_returning_data(client, auth_headers, test_user, operator_user, stock):
    checkout(client, auth_headers, body(stock, operator_user))
    filters = {'employee_id': operator_user.id}
    response = client.get(URL + '/checkouts/export.csv', headers=auth_headers, params=filters)
    assert response.status_code == 200
    rows = committed_export_rows('consumable_checkout')
    assert len(rows) == 1
    assert rows[0].user_id == test_user.id and rows[0].company_id == 1
    assert rows[0].new_values['format'] == 'csv'
    assert rows[0].new_values['filters'] == filters
    assert 'Employee' in rows[0].new_values['columns']
    assert 'Employee ID' not in rows[0].new_values['columns']


def test_serialized_stock_cannot_be_selected_or_issued_as_an_ordinary_consumable(
    client, auth_headers, operator_user, stock, db_session
):
    stock[0].serial_number = 'SERIAL-0001'
    db_session.commit()
    listed = client.get(URL + '/stock', headers=auth_headers).json()['items']
    row = next(item for item in listed if item['inventory_item_id'] == stock[0].id)
    assert row['can_checkout'] is False
    assert row['quantity_available'] == 0
    assert 'Serialized stock' in row['unavailable_reason']
    response = client.post(URL + '/checkouts', headers=auth_headers, json=body(stock, operator_user, quantity=1))
    assert response.status_code == 409, response.text
    assert 'Serialized stock' in response.json()['detail']
    assert_stock_unchanged(db_session, stock[0])
    assert db_session.query(ConsumableCheckout).count() == 0


def test_csv_is_refused_if_export_audit_cannot_be_recorded(client, auth_headers, operator_user, stock, monkeypatch):
    checkout(client, auth_headers, body(stock, operator_user))
    # AuditService.log deliberately returns None on legacy write failures.
    # Consumable exports must demand evidence before disclosing the file.
    monkeypatch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
    response = client.get(URL + '/checkouts/export.csv', headers=auth_headers)
    assert response.status_code == 503, response.text
    assert 'text/csv' not in response.headers['content-type']
    assert committed_export_rows('consumable_checkout') == []


def test_csv_does_not_reload_every_orm_row_after_committing_export_audit(
    client, auth_headers, operator_user, stock, db_session
):
    for _ in range(3):
        checkout(client, auth_headers, body(stock, operator_user))
    reads = []

    def capture_checkout_reads(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT') and 'FROM consumable_checkouts' in statement:
            reads.append(statement)

    event.listen(db_session.bind, 'before_cursor_execute', capture_checkout_reads)
    try:
        response = client.get(URL + '/checkouts/export.csv', headers=auth_headers)
    finally:
        event.remove(db_session.bind, 'before_cursor_execute', capture_checkout_reads)
    assert response.status_code == 200
    assert len(reads) == 1  # One bulk load, independent of the number of disclosed records.
