"""Advisory observations: immutable evidence without operational inventory effects."""

import copy
import json
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.role_permission import RolePermission
from app.models.stock_piece import StockPiece, StockPieceObservation
from app.models.user import UserRole
from app.schemas.stock_piece import ObservationEvidence
from app.services.audit_service import AuditService
from app.services.stock_piece import canonical, digest

BASE = '/api/v1/inventory/stock-pieces'
SOURCES = '/api/v1/inventory/stock-piece-sources'
pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
def stock(db_session, test_part):
    row = InventoryItem(
        company_id=1,
        part_id=test_part.id,
        location='SYNTHETIC-RACK',
        quantity_on_hand=10,
        unit_cost=25,
        lot_number='SYNTHETIC-LOT',
        heat_lot='TEST-HEAT',
        cert_number='UNVERIFIED',
    )
    db_session.add(row)
    db_session.commit()
    return row


def evidence():
    return {
        'version': 1,
        'unit': 'in',
        'measurement_method': 'Manual tape measurement',
        'source_units': 'in',
        'geometry': {'kind': 'rectangle', 'width': '24', 'height': '12'},
        'unavailable_zones': [],
        'thickness': '0.125',
        'grade': None,
        'grain_axis': None,
        'location_note': None,
        'ownership_note': None,
        'certification_note': None,
    }


def command(client, headers, stock):
    response = client.get(SOURCES, params={'inventory_item_id': stock.id}, headers=headers)
    assert response.status_code == 200, response.text
    source = response.json()['items'][0]
    return {
        'expected_company_id': 1,
        'request_key': str(uuid4()),
        'state': 'RECORDED',
        'label': 'SYNTHETIC-PIECE',
        'reason': 'Initial measured observation',
        'observed_at': '2026-09-08T12:00:00Z',
        'observer_name': 'Synthetic observer',
        'source_inventory_item_id': stock.id,
        'source_part_id': stock.part_id,
        'expected_source_sha256': source['source_sha256'],
        'evidence': evidence(),
    }


def record(client, headers, stock):
    body = command(client, headers, stock)
    result = client.post(BASE, json=body, headers=headers)
    assert result.status_code == 200, result.text
    return body, result.json()


def append_command(body, *, version=1):
    result = copy.deepcopy(body)
    result.pop('label')
    result.update(expected_version=version, request_key=str(uuid4()), reason='Corrected measured observation')
    return result


def test_record_roundtrip_hash_and_read_purity_do_not_change_inventory(client, admin_headers, stock, db_session):
    stock_before = {column.name: getattr(stock, column.name) for column in stock.__table__.columns}
    body, saved = record(client, admin_headers, stock)
    assert saved['evidence'] == body['evidence']
    assert saved['payload_sha256'] == digest(body['evidence'])
    assert saved['source_sha256'] == digest(saved['source_snapshot'])
    assert saved['source_status'] == 'unchanged'
    assert saved['piece_version'] == saved['observation_number'] == 1
    assert 'unverified' in saved['advisory']
    assert db_session.query(InventoryTransaction).count() == 0
    db_session.refresh(stock)
    assert stock_before == {column.name: getattr(stock, column.name) for column in stock.__table__.columns}
    writes = []

    def observe_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(' ', 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
            writes.append(statement)

    event.listen(db_session.get_bind(), 'before_cursor_execute', observe_sql)
    try:
        for path in (
            SOURCES,
            BASE,
            f"{BASE}/{saved['piece_id']}/observations",
            f"{BASE}/{saved['piece_id']}/observations/1",
        ):
            response = client.get(path, headers=admin_headers)
            assert response.status_code == 200, response.text
    finally:
        event.remove(db_session.get_bind(), 'before_cursor_execute', observe_sql)
    assert writes == []
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == 'stock_piece_observation').count() == 1


def test_same_key_replays_original_after_source_changes_and_cas_preserves_history(
    client, admin_headers, stock, db_session
):
    body, saved = record(client, admin_headers, stock)
    update = append_command(body)
    update['evidence']['geometry']['width'] = '23.999999999'
    changed = client.post(f"{BASE}/{saved['piece_id']}/observations", json=update, headers=admin_headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()['observation_number'] == 2
    assert (
        client.post(
            f"{BASE}/{saved['piece_id']}/observations", json=append_command(body), headers=admin_headers
        ).status_code
        == 409
    )
    stock.quantity_on_hand = 7
    db_session.commit()
    replay = client.post(BASE, json=body, headers=admin_headers)
    assert replay.status_code == 200
    assert replay.json()['payload_sha256'] == saved['payload_sha256'] and replay.json()['piece_version'] == 1
    assert replay.json()['source_status'] == 'changed'
    conflict = copy.deepcopy(body)
    conflict['reason'] = 'Different command'
    assert client.post(BASE, json=conflict, headers=admin_headers).status_code == 409
    assert db_session.query(StockPieceObservation).count() == 2


@pytest.mark.parametrize('unattributed', [False, True])
def test_same_balance_turnover_watermark_flags_stale_without_ledger_writes(
    client, admin_headers, admin_user, stock, db_session, unattributed
):
    body, saved = record(client, admin_headers, stock)
    for quantity in (-1, 1):
        db_session.add(
            InventoryTransaction(
                company_id=1,
                inventory_item_id=None if unattributed else stock.id,
                part_id=stock.part_id,
                transaction_type=TransactionType.ADJUST,
                quantity=quantity,
                created_by=admin_user.id,
                created_at=datetime(2026, 9, 8),
            )
        )
    db_session.commit()
    response = client.get(f"{BASE}/{saved['piece_id']}/observations/1", headers=admin_headers).json()
    assert response['source_status'] == 'changed'
    assert response['source_snapshot']['movement_watermark']['count'] == 0
    current = client.get(SOURCES, headers=admin_headers).json()['items'][0]
    assert current['snapshot']['movement_watermark']['count'] == 2
    assert current['snapshot']['movement_watermark']['coverage'] == 'direct_item_and_unattributed_same_part'
    assert current['snapshot']['item']['quantity_on_hand'] == saved['source_snapshot']['item']['quantity_on_hand']
    assert (
        client.post(
            BASE, json={**body, 'request_key': str(uuid4()), 'label': 'SECOND'}, headers=admin_headers
        ).status_code
        == 409
    )
    assert db_session.query(InventoryTransaction).count() == 2


def test_withdraw_after_source_disappears_copies_exact_historical_measurement(client, admin_headers, stock, db_session):
    _, saved = record(client, admin_headers, stock)
    db_session.delete(stock)
    db_session.commit()
    withdrawal = {
        'state': 'WITHDRAWN',
        'expected_company_id': 1,
        'expected_version': 1,
        'request_key': str(uuid4()),
        'reason': 'Physical identity no longer confirmed',
        'observed_at': '2026-09-08T13:00:00Z',
        'observer_name': 'Synthetic observer',
    }
    response = client.post(f"{BASE}/{saved['piece_id']}/observations", json=withdrawal, headers=admin_headers)
    assert response.status_code == 200, response.text
    withdrawn = response.json()
    assert withdrawn['state'] == 'WITHDRAWN' and withdrawn['source_status'] == 'missing'
    for field in (
        'evidence',
        'payload_sha256',
        'payload_bytes',
        'source_snapshot',
        'source_sha256',
        'source_inventory_item_id',
        'source_part_id',
    ):
        assert withdrawn[field] == saved[field]
    assert (
        client.post(f"{BASE}/{saved['piece_id']}/observations", json=withdrawal, headers=admin_headers).status_code
        == 200
    )
    assert (
        client.post(
            f"{BASE}/{saved['piece_id']}/observations",
            json={**withdrawal, 'expected_version': 2, 'request_key': str(uuid4())},
            headers=admin_headers,
        ).status_code
        == 409
    )


@pytest.mark.parametrize('operation', ['create', 'correct', 'withdraw'])
def test_required_audit_failure_rolls_back_whole_command(
    client, admin_headers, stock, db_session, monkeypatch, operation
):
    body = command(client, admin_headers, stock)
    path = BASE
    expected = 0
    if operation != 'create':
        _, saved = record(client, admin_headers, stock)
        path += f"/{saved['piece_id']}/observations"
        expected = 1
        body = append_command(body)
        if operation == 'withdraw':
            body = {
                key: value
                for key, value in body.items()
                if key not in {'source_inventory_item_id', 'source_part_id', 'expected_source_sha256', 'evidence'}
            }
            body['state'] = 'WITHDRAWN'
    monkeypatch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
    result = client.post(path, json=body, headers=admin_headers)
    assert result.status_code == 503, result.text
    assert db_session.query(StockPieceObservation).count() == expected
    assert db_session.query(StockPiece).count() == (1 if expected else 0)
    if expected:
        db_session.expire_all()
        assert db_session.query(StockPiece).one().version == 1


def test_tenant_and_exact_part_fences_do_not_disclose_or_mutate(client, admin_headers, stock, db_session, test_part):
    body = command(client, admin_headers, stock)
    db_session.add(Company(id=2, name='Synthetic foreign', slug='synthetic-foreign'))
    db_session.commit()
    foreign = InventoryItem(company_id=2, part_id=test_part.id, location='FOREIGN', quantity_on_hand=1)
    db_session.add(foreign)
    db_session.commit()
    assert client.get(SOURCES, params={'inventory_item_id': foreign.id}, headers=admin_headers).json()['items'] == []
    assert (
        client.post(BASE, json={**body, 'source_inventory_item_id': foreign.id}, headers=admin_headers).status_code
        == 404
    )
    assert client.post(BASE, json={**body, 'source_part_id': 999999}, headers=admin_headers).status_code == 404
    assert client.post(BASE, json={**body, 'expected_company_id': 2}, headers=admin_headers).status_code == 409
    for path in (f'{BASE}/999999/observations', f'{BASE}/999999/observations/1'):
        assert client.get(path, headers=admin_headers).status_code == 404
    assert db_session.query(StockPieceObservation).count() == 0


@pytest.mark.parametrize(
    'headers_fixture,allowed',
    [('admin_headers', True), ('manager_headers', True), ('supervisor_headers', True), ('operator_headers', False)],
)
def test_write_capability_matches_existing_mutator_roles(
    client, request, headers_fixture, allowed, admin_headers, stock
):
    headers = request.getfixturevalue(headers_fixture)
    page = client.get(SOURCES, headers=headers)
    if page.status_code == 200:
        assert page.json()['can_record'] is allowed
        assert page.json()['company_id'] == 1
    response = client.post(BASE, json=command(client, admin_headers, stock), headers=headers)
    assert response.status_code == (200 if allowed else 403), response.text


def test_effective_permission_override_read_only_and_kiosk_fences(client, admin_headers, admin_user, stock, db_session):
    body = command(client, admin_headers, stock)
    override = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view', 'purchasing:create'])
    db_session.add(override)
    db_session.commit()
    assert client.get(SOURCES, headers=admin_headers).status_code == 403
    assert client.post(BASE, json=body, headers=admin_headers).status_code == 403
    override.permissions = ['inventory:view']
    db_session.commit()
    assert client.get(SOURCES, headers=admin_headers).json()['can_record'] is True
    for claims in ({'read_only': True, 'company_id': 1}, {'scope': 'kiosk'}):
        token = create_access_token(subject=admin_user.id, **claims)
        headers = {'Authorization': 'Bearer ' + token}
        assert client.post(BASE, json=body, headers=headers).status_code == 403
    assert db_session.query(StockPieceObservation).count() == 0


@pytest.mark.parametrize(
    'value', [None, True, 6, 6.0, '6.0', 'NaN', 'Infinity', '1e3', '-0', '0', '0.0000000001', '100001']
)
def test_exact_positive_decimal_dimensions_reject_lossy_or_ambiguous_inputs(client, admin_headers, stock, value):
    body = command(client, admin_headers, stock)
    body['evidence']['geometry']['width'] = value
    result = client.post(BASE, json=body, headers=admin_headers)
    assert result.status_code == 422, result.text


@pytest.mark.parametrize(
    'shape',
    [
        {'kind': 'unknown'},
        {'kind': 'circle', 'cx': '4', 'cy': '4', 'r': '4'},
        {
            'kind': 'polygon',
            'outer': [{'x': '0', 'y': '0'}, {'x': '4', 'y': '0'}, {'x': '2', 'y': '2'}, {'x': '0', 'y': '4'}],
            'holes': [[{'x': '1', 'y': '1'}, {'x': '1.1', 'y': '1'}, {'x': '1', 'y': '1.1'}]],
        },
    ],
)
def test_unknown_and_actual_nonrectangular_geometry_retained_unverified(client, admin_headers, stock, shape):
    body = command(client, admin_headers, stock)
    body['evidence']['geometry'] = shape
    body['evidence']['thickness'] = None
    result = client.post(BASE, json=body, headers=admin_headers)
    assert result.status_code == 200, result.text
    assert result.json()['evidence']['geometry'] == shape
    assert any('topology has not been checked' in note for note in result.json()['review_issues'])


def test_duplicate_json_and_unknown_fields_refused(client, admin_headers, stock):
    body = command(client, admin_headers, stock)
    encoded = json.dumps(body).replace('"unit": "in"', '"unit":"mm","unit":"in"')
    assert (
        client.post(BASE, content=encoded, headers={**admin_headers, 'Content-Type': 'application/json'}).status_code
        == 422
    )
    body['available'] = True
    assert client.post(BASE, json=body, headers=admin_headers).status_code == 422


def test_combined_vertex_zone_and_canonical_payload_budgets():
    value = evidence()
    value['geometry'] = {'kind': 'polygon', 'outer': [{'x': str(n), 'y': '0'} for n in range(2000)], 'holes': []}
    ObservationEvidence.model_validate(value)
    value['unavailable_zones'] = [
        {
            'id': 'z',
            'label': 'Reported damage',
            'reason': 'Observed edge',
            'outline': {'kind': 'circle', 'cx': '1', 'cy': '1', 'r': '1'},
        }
    ]
    with pytest.raises(ValueError, match='2000 source vertices'):
        ObservationEvidence.model_validate(value)
    value = evidence()
    zone = {
        'id': 'z',
        'label': 'Damage',
        'reason': 'Observed',
        'outline': {'kind': 'circle', 'cx': '1', 'cy': '1', 'r': '1'},
    }
    value['unavailable_zones'] = [dict(zone, id=str(n)) for n in range(17)]
    with pytest.raises(ValueError):
        ObservationEvidence.model_validate(value)
    assert json.loads(canonical(evidence())) == evidence()


def test_api_token_attribution_replay_is_bound_to_exact_credential_and_revocation(
    client, admin_headers, admin_user, stock, db_session
):
    body = command(client, admin_headers, stock)
    tokens = []
    for label in ('Synthetic observer A', 'Synthetic observer B'):
        result = client.post(
            '/api/v1/api-tokens/', json={'user_id': admin_user.id, 'label': label}, headers=admin_headers
        )
        assert result.status_code == 201, result.text
        tokens.append(result.json())
    headers = {'Authorization': 'Bearer ' + tokens[0]['token']}
    response = client.post(BASE, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['submitted_api_token_id'] == tokens[0]['id']
    assert client.post(BASE, json=body, headers=headers).status_code == 200
    for other in (admin_headers, {'Authorization': 'Bearer ' + tokens[1]['token']}):
        assert client.post(BASE, json=body, headers=other).status_code == 409
    audit = db_session.query(AuditLog).filter(AuditLog.resource_type == 'stock_piece_observation').one()
    assert audit.extra_data['credential']['api_token_id'] == tokens[0]['id']
    revoked = client.post(
        f"/api/v1/api-tokens/{tokens[0]['id']}/revoke",
        json={'reason': 'Synthetic test complete'},
        headers=admin_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert client.post(BASE, json=body, headers=headers).status_code in (401, 403)
    assert db_session.query(StockPieceObservation).count() == 1


@pytest.mark.parametrize(
    'field,value',
    [
        ('observed_at', '2026-02-30T12:00:00Z'),
        ('observed_at', '2026-09-08T12:00:00+00:00'),
        ('observed_at', '2026-09-08'),
        ('observer_name', ' '),
        ('reason', ''),
        ('expected_company_id', True),
        ('source_part_id', None),
        ('request_key', 'no-uuid'),
    ],
)
def test_strict_observer_instant_company_and_reference_fields(client, admin_headers, stock, field, value):
    body = command(client, admin_headers, stock)
    body[field] = value
    assert client.post(BASE, json=body, headers=admin_headers).status_code == 422


def test_payload_and_receive_limits_reject_before_any_observation(client, admin_headers, stock, db_session):
    body = command(client, admin_headers, stock)
    body['evidence']['geometry'] = {
        'kind': 'polygon',
        'outer': [{'x': '99999.123456789', 'y': '99999.123456789'} for _ in range(1984)],
        'holes': [],
    }
    body['evidence']['unavailable_zones'] = [
        {
            'id': str(n),
            'label': 'Reported',
            'reason': '🙂' * 1000,
            'outline': {'kind': 'circle', 'cx': '1', 'cy': '1', 'r': '1'},
        }
        for n in range(16)
    ]
    assert len(canonical(body['evidence']).encode()) > 131072
    raw = json.dumps(body, ensure_ascii=False).encode()
    assert len(raw) < 262144
    response = client.post(BASE, content=raw, headers={**admin_headers, 'Content-Type': 'application/json'})
    assert response.status_code == 413, response.text
    response = client.post(
        BASE, content=b'{' + b' ' * 262144 + b'}', headers={**admin_headers, 'Content-Type': 'application/json'}
    )
    assert response.status_code == 413
    assert db_session.query(StockPieceObservation).count() == 0


def test_case_sensitive_label_unique_company_scope_and_unknown_fields_are_not_defaulted(
    client, admin_headers, stock, db_session
):
    body, saved = record(client, admin_headers, stock)
    assert client.post(BASE, json={**body, 'request_key': str(uuid4())}, headers=admin_headers).status_code == 409
    unknown = copy.deepcopy(body)
    unknown.update(request_key=str(uuid4()), label='synthetic-piece')
    unknown['evidence']['geometry'] = {'kind': 'unknown'}
    unknown['evidence']['thickness'] = None
    assert client.post(BASE, json=unknown, headers=admin_headers).status_code == 200
    for field in ('geometry', 'unavailable_zones', 'version'):
        malformed = copy.deepcopy(unknown)
        malformed['evidence'][field] = None
        assert client.post(BASE, json=malformed, headers=admin_headers).status_code == 422
    assert (
        client.get(BASE, params={'page': 2, 'per_page': 1}, headers=admin_headers).json()['items'][0]['piece_id']
        == saved['piece_id']
    )
    assert client.get(SOURCES).status_code == 401


@pytest.mark.parametrize('value', ['0', '-1', '2147483648', '999999999999999999999999999999999999'])
def test_path_ids_are_bounded_before_database_integer_binding(client, admin_headers, value):
    assert client.get(f'{BASE}/{value}/observations', headers=admin_headers).status_code == 422
    assert client.get(f'{BASE}/1/observations/{value}', headers=admin_headers).status_code == 422


def _read_with_query_budget(client, headers, db, path, per_page):
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().split(' ', 1)[0].upper())

    db.expire_all()
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        response = client.get(path, params={'per_page': per_page}, headers=headers)
        assert response.status_code == 200, response.text
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert not {'INSERT', 'UPDATE', 'DELETE'}.intersection(statements)
    return response.json(), sum(verb in {'SELECT', 'WITH'} for verb in statements)


def test_larger_register_page_keeps_read_queries_bounded_and_distinct_source_drift_correct(
    client, admin_headers, stock, db_session
):
    sources = [stock]
    for number in range(2):
        source = InventoryItem(company_id=1, part_id=stock.part_id, location=f'SYNTHETIC-{number}', quantity_on_hand=10)
        db_session.add(source)
        sources.append(source)
    db_session.commit()
    bodies = [command(client, admin_headers, item) for item in sources]
    for number in range(18):
        body = {**bodies[number % 3], 'label': f'SYNTHETIC-BATCH-{number}', 'request_key': str(uuid4())}
        response = client.post(BASE, json=body, headers=admin_headers)
        assert response.status_code == 200, response.text
    source_ids = [item.id for item in sources]
    sources[1].status = 'on_hold'
    db_session.delete(sources[2])
    db_session.commit()
    one, one_queries = _read_with_query_budget(client, admin_headers, db_session, BASE, 1)
    page, page_queries = _read_with_query_budget(client, admin_headers, db_session, BASE, 100)
    assert one['total'] == page['total'] == 18
    assert len(page['items']) == 18
    # A materially larger real API page must not add one or two SQL round trips
    # per observation. Allow fixed authentication/framework overhead to vary.
    assert page_queries <= one_queries + 2
    assert page_queries <= 12
    states = {source_ids[0]: 'unchanged', source_ids[1]: 'changed', source_ids[2]: 'missing'}
    for item in page['items']:
        assert item['source_status'] == states[item['source_inventory_item_id']]
        assert item['company_id'] == 1
        assert 'evidence' not in item and 'source_snapshot' not in item
        assert (
            item['current_source_sha256'] is None
            if item['source_status'] == 'missing'
            else item['current_source_sha256']
        )


def test_long_piece_history_reuses_current_source_read_but_keeps_each_historical_hash_and_version(
    client, admin_headers, stock, db_session
):
    body, first = record(client, admin_headers, stock)
    path = f"{BASE}/{first['piece_id']}/observations"
    hashes = {first['observation_number']: first['payload_sha256']}
    for version in range(1, 16):
        correction = append_command(body, version=version)
        correction['evidence']['geometry']['width'] = str(24 + version)
        response = client.post(path, json=correction, headers=admin_headers)
        assert response.status_code == 200, response.text
        hashes[response.json()['observation_number']] = response.json()['payload_sha256']
    stock.quantity_on_hand = 9
    db_session.commit()
    _, one_queries = _read_with_query_budget(client, admin_headers, db_session, path, 1)
    history, history_queries = _read_with_query_budget(client, admin_headers, db_session, path, 100)
    assert history['total'] == len(history['items']) == 16
    assert history_queries <= one_queries + 2
    assert history_queries <= 13
    assert [item['observation_number'] for item in history['items']] == list(range(16, 0, -1))
    assert all(item['payload_sha256'] == hashes[item['observation_number']] for item in history['items'])
    assert all(item['piece_version'] == item['observation_number'] for item in history['items'])
    assert all(item['source_status'] == 'changed' for item in history['items'])
    assert len({item['current_source_sha256'] for item in history['items']}) == 1
    db_session.delete(stock)
    db_session.commit()
    missing, missing_queries = _read_with_query_budget(client, admin_headers, db_session, path, 100)
    assert missing_queries <= history_queries + 2
    assert all(
        item['source_status'] == 'missing' and item['current_source_sha256'] is None for item in missing['items']
    )
