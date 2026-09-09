"""Read-only recorded-piece resolution; exact identity, no available-stock claims."""

import copy
import json
from datetime import datetime
from decimal import Inexact, Rounded, getcontext, setcontext

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.core.remnant_domain_profile import remnant_profile_identity
from app.core.remnant_evidence import evidence_sha256, target_group_sha256
from app.core.security import create_access_token
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.role_permission import RolePermission
from app.models.stock_piece import StockPieceObservation
from app.models.user import UserRole
from app.schemas.remnant_planning import RemnantSelection
from app.services import remnant_planning as service
from tests.api.test_stock_piece_observations import BASE, append_command, command

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
def stock(db_session, test_part):
    row = InventoryItem(
        company_id=1, part_id=test_part.id, location='SYNTHETIC-RACK', quantity_on_hand=10, unit_cost=25
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def observed(client, admin_headers, stock):
    body = command(client, admin_headers, stock)
    body['evidence']['grade'] = 'A36'
    body['label'] = 'Plaque café 😀'
    response = client.post(BASE, json=body, headers=admin_headers)
    assert response.status_code == 200, response.text
    return body, response.json()


def resolve(client, headers, observed, **patch):
    _, detail = observed
    body = {
        'expected_company_id': detail['company_id'],
        'expected_payload_sha256': detail['payload_sha256'],
        'expected_source_sha256': detail['source_sha256'],
        **patch,
    }
    return client.post(
        f"{BASE}/{detail['piece_id']}/observations/{detail['observation_number']}/planning-snapshot",
        json=body,
        headers=headers,
    )


def selection_for(resolved, quote=None):
    quote = quote or {'material': 'Carbon steel', 'thickness': 0.125, 'parts': [{'id': 'P', 'quantity': 2}]}
    selection = RemnantSelection.model_validate(
        {
            'version': 1,
            'groupId': 'group',
            'snapshot': resolved['snapshot'],
            'snapshotSha256': resolved['snapshot_sha256'],
            'assignment': {
                'version': 1,
                'basis': 'planner_declared_unverified',
                'family': 'Carbon steel',
                'requiredGrade': 'A36',
                'thicknessIn': '0.125',
                'reason': 'Planner declared requirement for this exact group',
                'targetGroupSha256': target_group_sha256('group', 'A36', quote),
            },
            'geometryProfile': remnant_profile_identity(),
            'zoneClearanceIn': '0.375',
            'capacity': 1,
            'planningOnly': True,
            'eligibilityVerified': False,
            'availabilityVerified': False,
        }
    )
    return selection, quote


def test_snapshot_exact_unicode_hash_nonfinancial_subset_and_zero_sql_writes(
    client, admin_headers, observed, db_session
):
    statements = []
    engine = db_session.get_bind()

    def watch(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, 'before_cursor_execute', watch)
    try:
        response = resolve(client, admin_headers, observed)
    finally:
        event.remove(engine, 'before_cursor_execute', watch)
    assert response.status_code == 200, response.text
    result = response.json()
    snapshot = result['snapshot']
    original = observed[1]
    assert result['snapshot_sha256'] == evidence_sha256(snapshot)
    assert snapshot['evidence'] == original['evidence']
    assert snapshot['payloadSha256'] == original['payload_sha256']
    assert snapshot['sourceSha256'] == original['source_sha256']
    assert snapshot['label'] == 'Plaque café 😀'
    assert snapshot['sourceEvidence']['movement_watermark'] == original['source_snapshot']['movement_watermark']
    assert set(snapshot['sourceEvidence']['item']) == set(service.SOURCE_ITEM_FIELDS)
    assert set(snapshot['sourceEvidence']['part']) == set(service.SOURCE_PART_FIELDS)
    for key in (
        'quantity_on_hand',
        'quantity_available',
        'quantity_allocated',
        'unit_cost',
        'standard_cost',
        'material_cost',
    ):
        assert key not in snapshot['sourceEvidence']['item']
        assert key not in snapshot['sourceEvidence']['part']
    assert result['source_status'] == 'unchanged' and result['latest_observation_number'] == 1
    assert 'unverified' in result['advisory']
    assert not set(statements) & {'INSERT', 'UPDATE', 'DELETE'}
    assert db_session.query(StockPieceObservation).count() == 1
    again = resolve(client, admin_headers, observed).json()
    assert again['snapshot'] == snapshot and again['snapshot_sha256'] == result['snapshot_sha256']


@pytest.mark.parametrize('field', ['expected_payload_sha256', 'expected_source_sha256'])
def test_wrong_expected_hash_is_conflict(client, admin_headers, observed, field):
    assert resolve(client, admin_headers, observed, **{field: '0' * 64}).status_code == 409


@pytest.mark.parametrize(
    'mutation',
    ['withdraw', 'correction', 'source_delete', 'source_part_deleted', 'item_inactive', 'part_inactive', 'held'],
)
def test_old_or_unavailable_evidence_refuses_without_rewriting_history(
    client, admin_headers, observed, stock, db_session, mutation
):
    before = copy.deepcopy(observed[1])
    if mutation in ('withdraw', 'correction'):
        body = append_command(observed[0])
        if mutation == 'withdraw':
            for key in ('source_inventory_item_id', 'source_part_id', 'expected_source_sha256', 'evidence'):
                body.pop(key)
            body['state'] = 'WITHDRAWN'
        result = client.post(f"{BASE}/{before['piece_id']}/observations", json=body, headers=admin_headers)
        assert result.status_code == 200, result.text
    elif mutation == 'source_delete':
        db_session.delete(stock)
        db_session.commit()
    else:
        if mutation == 'source_part_deleted':
            stock.part.is_deleted = True
        elif mutation == 'item_inactive':
            stock.is_active = False
        elif mutation == 'part_inactive':
            stock.part.is_active = False
        else:
            stock.status = 'on_hold'
        db_session.commit()
    assert resolve(client, admin_headers, observed).status_code == 409
    historical = client.get(f"{BASE}/{before['piece_id']}/observations/1", headers=admin_headers).json()
    assert historical['evidence'] == before['evidence']
    assert historical['payload_sha256'] == before['payload_sha256']
    assert historical['piece_version'] == 1  # Historical version is NOT latestness.


@pytest.mark.parametrize('unattributed', [False, True])
def test_equal_balance_turnover_is_stale(client, admin_headers, observed, stock, db_session, admin_user, unattributed):
    for amount in (-1, 1):
        db_session.add(
            InventoryTransaction(
                company_id=1,
                inventory_item_id=None if unattributed else stock.id,
                part_id=stock.part_id,
                transaction_type=TransactionType.ADJUST,
                quantity=amount,
                created_by=admin_user.id,
                created_at=datetime(2026, 9, 8),
            )
        )
    db_session.commit()
    assert resolve(client, admin_headers, observed).status_code == 409
    assert stock.quantity_on_hand == 10
    assert db_session.query(InventoryTransaction).count() == 2


@pytest.mark.parametrize(
    'field,value', [('grade', None), ('thickness', None), ('geometry', {'kind': 'unknown'}), ('thickness', '4')]
)
def test_structurally_recordable_unknown_or_oversize_evidence_is_not_selectable(
    client, admin_headers, stock, field, value
):
    body = command(client, admin_headers, stock)
    body['evidence']['grade'] = 'A36'
    body['evidence'][field] = value
    recorded = client.post(BASE, json=body, headers=admin_headers)
    assert recorded.status_code == 200, recorded.text
    assert resolve(client, admin_headers, (body, recorded.json())).status_code == 409


def test_inventory_read_is_required_but_inventory_mutator_role_is_not(
    client, admin_headers, observed, admin_user, db_session
):
    override = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view', 'purchasing:create'])
    db_session.add(override)
    db_session.commit()
    assert resolve(client, admin_headers, observed).status_code == 403
    admin_user.role = UserRole.OPERATOR
    db_session.add(RolePermission(company_id=1, role=UserRole.OPERATOR, permissions=['inventory:view']))
    db_session.commit()
    assert resolve(client, admin_headers, observed).status_code == 200


@pytest.mark.parametrize('claims', [{'read_only': True, 'company_id': 1}, {'scope': 'kiosk'}])
def test_existing_read_only_post_and_kiosk_auth_fences_remain(client, observed, admin_user, claims):
    token = create_access_token(subject=admin_user.id, **claims)
    assert resolve(client, {'Authorization': 'Bearer ' + token}, observed).status_code == 403


def test_expected_company_and_foreign_observation_are_not_disclosed(
    client, admin_headers, observed, db_session, admin_user
):
    assert resolve(client, admin_headers, observed, expected_company_id=2).status_code == 409
    db_session.add(Company(id=2, name='Other synthetic company', slug='other-remnant-test', is_active=True))
    admin_user.role = UserRole.PLATFORM_ADMIN
    db_session.commit()
    token = create_access_token(subject=admin_user.id, company_id=2)
    assert resolve(client, {'Authorization': 'Bearer ' + token}, observed, expected_company_id=2).status_code == 404


@pytest.mark.parametrize(
    'mutate',
    [
        lambda q: q.update(thickness=0.125000001),
        lambda q: q.update(material='Aluminum'),
        lambda q: q.update(grainAxis='x'),
        lambda q: q['parts'][0].update(quantity=3),
        lambda q: q.update(options=[]),
    ],
)
def test_assignment_is_invalidated_by_any_raw_group_change(client, admin_headers, observed, mutate):
    selection, quote = selection_for(resolve(client, admin_headers, observed).json())
    service.verify_assignment(selection, quote)
    mutate(quote)
    with pytest.raises(HTTPException) as exc:
        service.verify_assignment(selection, quote)
    assert exc.value.status_code == 409


def test_exact_grade_and_thickness_rules_ignore_ambient_decimal_context(client, admin_headers, observed):
    resolved = resolve(client, admin_headers, observed).json()
    selection, quote = selection_for(resolved)
    original = getcontext().copy()
    try:
        getcontext().prec = 2
        getcontext().traps[Inexact] = True
        getcontext().traps[Rounded] = True
        service.verify_assignment(selection, quote)
    finally:
        setcontext(original)
    selection.assignment.requiredGrade = 'a36'
    selection.assignment.targetGroupSha256 = target_group_sha256('group', 'a36', quote)
    with pytest.raises(HTTPException):
        service.verify_assignment(selection, quote)


def test_forged_snapshot_with_recomputed_hash_is_refused_against_actual_observation(
    client, admin_headers, observed, admin_user, db_session
):
    selection, quote = selection_for(resolve(client, admin_headers, observed).json())
    service.verify_current_selection(db_session, admin_user, 1, selection, quote)
    selection.snapshot.observerName = 'Different observer'
    selection.snapshotSha256 = evidence_sha256(selection.snapshot.model_dump(mode='json'))
    with pytest.raises(HTTPException) as exc:
        service.verify_current_selection(db_session, admin_user, 1, selection, quote)
    assert exc.value.status_code == 409


@pytest.mark.parametrize('status', ['on_hold', 'quarantine', 'rejected'])
def test_unchanged_held_source_is_still_blocked(client, admin_headers, stock, db_session, status):
    stock.status = status
    db_session.commit()
    body = command(client, admin_headers, stock)
    body['evidence']['grade'] = 'A36'
    recorded = client.post(BASE, json=body, headers=admin_headers)
    assert recorded.status_code == 200
    assert recorded.json()['source_status'] == 'unchanged'
    assert resolve(client, admin_headers, (body, recorded.json())).status_code == 409


def test_api_token_normal_auth_and_revocation_still_apply(client, admin_headers, observed, admin_user):
    response = client.post(
        '/api/v1/api-tokens/',
        json={'user_id': admin_user.id, 'label': 'Synthetic planning read'},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    token = response.json()
    headers = {'Authorization': 'Bearer ' + token['token']}
    assert resolve(client, headers, observed).status_code == 200
    revoked = client.post(
        f"/api/v1/api-tokens/{token['id']}/revoke", json={'reason': 'Synthetic check finished'}, headers=admin_headers
    )
    assert revoked.status_code == 200
    assert resolve(client, headers, observed).status_code in (401, 403)


@pytest.mark.parametrize('value', [-1, 0, 2147483648, 10**80])
def test_route_integer_bounds_precede_database_binding(client, admin_headers, observed, value):
    detail = observed[1]
    body = {
        'expected_company_id': 1,
        'expected_payload_sha256': detail['payload_sha256'],
        'expected_source_sha256': detail['source_sha256'],
    }
    result = client.post(f'{BASE}/{value}/observations/1/planning-snapshot', json=body, headers=admin_headers)
    assert result.status_code == 422


def test_duplicate_json_or_null_expected_identity_is_refused(client, admin_headers, observed):
    detail = observed[1]
    path = f"{BASE}/{detail['piece_id']}/observations/1/planning-snapshot"
    body = {
        'expected_company_id': 1,
        'expected_payload_sha256': detail['payload_sha256'],
        'expected_source_sha256': detail['source_sha256'],
    }
    text = json.dumps(body).replace('"expected_company_id": 1', '"expected_company_id": 1, "expected_company_id": 1')
    assert (
        client.post(path, content=text, headers={**admin_headers, 'Content-Type': 'application/json'}).status_code
        == 422
    )
    assert client.post(path, json={**body, 'expected_source_sha256': None}, headers=admin_headers).status_code == 422


@pytest.mark.parametrize(
    'field,value',
    [
        ('capacity', 2),
        ('capacity', True),
        ('planningOnly', 1),
        ('eligibilityVerified', True),
        ('snapshot', None),
        ('zoneClearanceIn', None),
        ('zoneClearanceIn', '0.0'),
        ('zoneClearanceIn', '-0'),
        ('zoneClearanceIn', '100.000000001'),
    ],
)
def test_selection_cannot_promote_or_change_fixed_claims(client, admin_headers, observed, field, value):
    selection, _ = selection_for(resolve(client, admin_headers, observed).json())
    raw = selection.model_dump(mode='json')
    raw[field] = value
    with pytest.raises(ValueError):
        RemnantSelection.model_validate(raw)


def test_unknown_geometry_topology_is_not_falsely_certified_by_structural_resolution(client, admin_headers, stock):
    body = command(client, admin_headers, stock)
    body['evidence']['grade'] = 'A36'
    body['evidence']['geometry'] = {
        'kind': 'polygon',
        'outer': [{'x': '0', 'y': '0'}, {'x': '2', 'y': '2'}, {'x': '0', 'y': '2'}, {'x': '2', 'y': '0'}],
        'holes': [],
    }
    recorded = client.post(BASE, json=body, headers=admin_headers)
    assert recorded.status_code == 200
    result = resolve(client, admin_headers, (body, recorded.json()))
    assert result.status_code == 200
    assert result.json()['snapshot']['evidence']['geometry'] == body['evidence']['geometry']
    assert any('not shape topology' in issue for issue in result.json()['review_issues'])


def test_future_helper_compiles_tenant_scoped_postgres_shared_header_lock(
    client, admin_headers, observed, admin_user, db_session
):
    from sqlalchemy.dialects import postgresql

    statements = []
    engine = db_session.get_bind()

    def watch(_conn, _cursor, _statement, _params, context, _many):
        if context.compiled is not None:
            statements.append(context.compiled.statement)

    event.listen(engine, 'before_cursor_execute', watch)
    try:
        service.lock_observation_header(db_session, 1, observed[1]['piece_id'])
    finally:
        event.remove(engine, 'before_cursor_execute', watch)
    # SQLite cannot prove PostgreSQL concurrency; this checks emitted dialect SQL only.
    compiled = [str(statement.compile(dialect=postgresql.dialect())) for statement in statements]
    locked = [sql for sql in compiled if 'FOR SHARE' in sql]
    assert len(locked) == 1
    assert 'stock_pieces.company_id =' in locked[0] and 'stock_pieces.id =' in locked[0]
