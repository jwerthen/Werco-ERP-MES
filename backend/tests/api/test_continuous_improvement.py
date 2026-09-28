"""The lean suggestion workflow is tenant-scoped, audited, and conflict-safe."""

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.continuous_improvement import ImprovementActivity, ImprovementSuggestion
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.integration]
URL = '/api/v1/continuous-improvement'


def payload(**changes):
    return (
        dict(
            title='Key the fixture',
            problem='Part can be loaded backwards',
            proposed_solution='Add locating pin',
            expected_benefit='Eliminate reversed parts',
            category='poka_yoke',
            area='Assembly',
        )
        | changes
    )


def create(client, headers, **changes):
    result = client.post(URL + '/', headers=headers, json=payload(**changes))
    assert result.status_code == 201, result.text
    return result.json()


def patch(client, headers, row, **changes):
    return client.patch(f'{URL}/{row["id"]}', headers=headers, json={'expected_version': row['version']} | changes)


def test_full_lifecycle_preserves_utc_actor_history_and_global_audit(
    client, auth_headers, admin_headers, test_user, db_session
):
    row = create(client, auth_headers, owner_id=test_user.id, target_date='2026-12-01')
    assert row['status'] == 'new' and row['version'] == 1
    assert row['created_at'].endswith('Z') and row['created_at'] == row['updated_at']
    assert row['created_by'] == test_user.id and row['owner_name'] == test_user.full_name
    assert row['history'][0]['kind'] == 'submitted'
    original = row['history'][0]
    first_review = None
    for status in ('under_review', 'approved', 'in_progress', 'implemented'):
        result = patch(
            client, admin_headers, row, status=status, implementation_notes='Installed and verified on 20 parts'
        )
        assert result.status_code == 200, result.text
        row = result.json()
        first_review = first_review or row['reviewed_at']
        assert row['reviewed_at'] == first_review
    assert row['implemented_at'].endswith('Z')
    result = client.post(
        f'{URL}/{row["id"]}/comments',
        headers=auth_headers,
        json={'expected_version': row['version'], 'body': 'No reversed parts this week'},
    )
    assert result.status_code == 200
    row = result.json()
    assert row['version'] == 6 and len(row['history']) == 6
    assert row['history'][0] == original
    assert row['history'][-1]['body'] == 'No reversed parts this week'
    assert all(event['created_at'].endswith('Z') for event in row['history'])
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == 'improvement_suggestion').count() == 6
    result = patch(client, auth_headers, row, status='under_review', change_note='Verify the replacement fixture')
    assert result.status_code == 200
    assert result.json()['implemented_at'] is None
    assert result.json()['reviewed_at'] == first_review


def test_metadata_and_read_only_roles(
    client, auth_headers, operator_headers, supervisor_headers, test_user, operator_user
):
    metadata = client.get(URL + '/metadata', headers=auth_headers).json()
    assert metadata['can_manage'] is True
    assert {item['value'] for item in metadata['categories']} >= {'poka_yoke', 'five_s', 'safety_ergonomics'}
    assert metadata['owners'] == [{'id': test_user.id, 'name': test_user.full_name}]
    row = create(client, auth_headers)
    for headers in (operator_headers, supervisor_headers):
        assert client.get(URL + '/metadata', headers=headers).json()['can_manage'] is False
        assert client.get(URL + '/', headers=headers).status_code == 200
        assert client.get(f'{URL}/{row["id"]}', headers=headers).status_code == 200
        assert client.post(URL + '/', headers=headers, json=payload()).status_code == 403
        assert patch(client, headers, row, title='Disallowed').status_code == 403
        assert (
            client.post(
                f'{URL}/{row["id"]}/comments', headers=headers, json={'expected_version': 1, 'body': 'Disallowed'}
            ).status_code
            == 403
        )
    assert client.get(URL + '/').status_code == 401
    assert client.post(URL + '/', json=payload()).status_code == 401


def test_tenancy_fences_records_owners_counts_and_history(client, auth_headers, db_session, test_user):
    db_session.add(Company(id=2, name='Other company', slug='other', is_active=True))
    other = User(
        company_id=2,
        employee_id='OTHER',
        email='other@example.com',
        first_name='Other',
        last_name='Manager',
        hashed_password=test_user.hashed_password,
        role=UserRole.MANAGER,
        is_active=True,
    )
    db_session.add(other)
    db_session.commit()
    headers = {'Authorization': f'Bearer {create_access_token(subject=other.id, company_id=2)}'}
    own_row = create(client, auth_headers)
    foreign = create(client, headers, title='Hidden suggestion', owner_id=other.id)
    listed = client.get(URL + '/', headers=auth_headers).json()
    assert [item['id'] for item in listed['items']] == [own_row['id']]
    assert listed['status_counts']['new'] == 1
    assert client.get(f'{URL}/{foreign["id"]}', headers=auth_headers).status_code == 404
    assert patch(client, auth_headers, foreign, title='Forbidden').status_code == 404
    assert (
        client.post(
            f'{URL}/{foreign["id"]}/comments', headers=auth_headers, json={'expected_version': 1, 'body': 'Forbidden'}
        ).status_code
        == 404
    )
    assert client.post(URL + '/', headers=auth_headers, json=payload(owner_id=other.id)).status_code == 422
    assert patch(client, auth_headers, own_row, owner_id=other.id).status_code == 422
    assert other.id not in [item['id'] for item in client.get(URL + '/metadata', headers=auth_headers).json()['owners']]
    assert client.get(URL + '/', headers=auth_headers, params={'q': f'CI-{foreign["id"]:04d}'}).json()['total'] == 0


def test_stale_update_and_comment_leave_no_partial_changes(client, auth_headers, db_session):
    row = create(client, auth_headers)
    saved = patch(client, auth_headers, row, title='Key both fixtures').json()
    assert patch(client, auth_headers, row, status='under_review').status_code == 409
    assert (
        client.post(
            f'{URL}/{row["id"]}/comments', headers=auth_headers, json={'expected_version': 1, 'body': 'Stale'}
        ).status_code
        == 409
    )
    latest = client.get(f'{URL}/{row["id"]}', headers=auth_headers).json()
    assert latest == saved
    assert db_session.query(ImprovementActivity).count() == 2


def test_orm_concurrency_checks_version_at_database_write(client, auth_headers, db_session):
    row = create(client, auth_headers)
    with Session(db_session.bind) as stale_session:
        stale = stale_session.get(ImprovementSuggestion, row['id'])
        assert patch(client, auth_headers, row, title='Winner').status_code == 200
        stale.title = 'Lost update'
        with pytest.raises(StaleDataError):
            stale_session.flush()
        stale_session.rollback()


@pytest.mark.parametrize(
    'changes',
    [
        {'status': 'implemented'},
        {'status': 'on_hold'},
        {'status': 'declined'},
        {'title': None},
        {'title': ' '},
        {'category': 'invented'},
        {'priority': 'critical'},
        {'status': None},
        {'company_id': 2},
        {'created_at': '2026-01-01T00:00:00Z'},
    ],
)
def test_invalid_commands_fail_without_history(client, auth_headers, changes):
    row = create(client, auth_headers)
    assert patch(client, auth_headers, row, **changes).status_code == 422
    assert len(client.get(f'{URL}/{row["id"]}', headers=auth_headers).json()['history']) == 1


def test_required_reasons_implementation_and_reopen(client, auth_headers):
    row = create(client, auth_headers)
    row = patch(client, auth_headers, row, status='on_hold', change_note='Waiting for fixture material').json()
    row = patch(client, auth_headers, row, status='declined', change_note='Alternative fixture approved').json()
    row = patch(
        client, auth_headers, row, status='implemented', implementation_notes='Key installed and checked'
    ).json()
    assert patch(client, auth_headers, row, implementation_notes=' ').status_code == 422
    assert patch(client, auth_headers, row, status='in_progress').status_code == 422
    assert patch(client, auth_headers, row, status='new', change_note='Try again').status_code == 422
    assert patch(client, auth_headers, row, status='under_review', change_note='New part geometry').status_code == 200


def test_filters_pagination_literal_search_and_unfiltered_counts(client, auth_headers, test_user):
    one = create(client, auth_headers, owner_id=test_user.id)
    two = create(client, auth_headers, title='5% fewer chips', category='five_s', priority='high')
    patch(client, auth_headers, one, status='under_review')
    response = client.get(
        URL + '/', headers=auth_headers, params={'category': 'five_s', 'priority': 'high', 'status': 'new', 'q': '%'}
    ).json()
    assert response['total'] == 1 and response['items'][0]['id'] == two['id']
    assert response['status_counts']['new'] == response['status_counts']['under_review'] == 1
    assert client.get(URL + '/', headers=auth_headers, params={'owner_id': test_user.id}).json()['total'] == 1
    page = client.get(URL + '/', headers=auth_headers, params={'skip': 1, 'limit': 1}).json()
    assert page['total'] == 2 and page['items'][0]['id'] == one['id']
    assert client.get(URL + '/', headers=auth_headers, params={'q': f'CI-{one["id"]:04d}'}).json()['total'] == 1


def test_inactive_or_lower_role_owner_rejected(client, auth_headers, operator_user, inactive_user):
    for owner in (operator_user, inactive_user):
        assert client.post(URL + '/', headers=auth_headers, json=payload(owner_id=owner.id)).status_code == 422


def test_audit_failure_rolls_back_entire_command(client, auth_headers, monkeypatch, db_session):
    monkeypatch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
    assert client.post(URL + '/', headers=auth_headers, json=payload()).status_code == 503
    assert db_session.query(ImprovementSuggestion).count() == db_session.query(ImprovementActivity).count() == 0


def test_activity_cannot_be_edited_or_deleted(client, auth_headers, db_session):
    create(client, auth_headers)
    activity = db_session.query(ImprovementActivity).one()
    activity.body = 'Rewritten history'
    with pytest.raises(ValueError, match='cannot be modified'):
        db_session.flush()
    db_session.rollback()
    activity = db_session.query(ImprovementActivity).one()
    db_session.delete(activity)
    with pytest.raises(ValueError, match='cannot be modified'):
        db_session.flush()
    db_session.rollback()


def test_note_only_edit_is_saved_in_history(client, auth_headers, db_session):
    row = create(client, auth_headers)
    result = patch(client, auth_headers, row, title=row['title'], change_note='Discuss at tomorrow morning meeting')
    assert result.status_code == 200
    saved = result.json()
    assert saved['version'] == 2 and len(saved['history']) == 2
    assert saved['history'][-1]['kind'] == 'comment'
    assert saved['history'][-1]['body'] == 'Discuss at tomorrow morning meeting'
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == 'improvement_suggestion').count() == 2


def test_read_only_company_session_can_read_but_cannot_write(client, test_user, auth_headers):
    row = create(client, auth_headers)
    headers = {'Authorization': f'Bearer {create_access_token(subject=test_user.id, company_id=1, read_only=True)}'}
    assert client.get(URL + '/metadata', headers=headers).json()['can_manage'] is False
    assert client.get(f'{URL}/{row["id"]}', headers=headers).status_code == 200
    assert client.post(URL + '/', headers=headers, json=payload()).status_code == 403
    assert patch(client, headers, row, title='Forbidden').status_code == 403


@pytest.mark.parametrize('role,superuser', [(UserRole.PLATFORM_ADMIN, False), (UserRole.OPERATOR, True)])
def test_platform_and_superuser_management_access(client, db_session, test_user, auth_headers, role, superuser):
    test_user.role, test_user.is_superuser = role, superuser
    db_session.commit()
    assert client.get(URL + '/metadata', headers=auth_headers).json()['can_manage'] is True
    row = create(client, auth_headers, owner_id=test_user.id)
    assert patch(client, auth_headers, row, status='under_review').status_code == 200


def test_switched_platform_context_is_read_only(client, db_session, test_user, auth_headers):
    create(client, auth_headers)
    test_user.role = UserRole.PLATFORM_ADMIN
    db_session.add(Company(id=2, name='Viewed company', slug='viewed', is_active=True))
    db_session.commit()
    token = create_access_token(subject=test_user.id, company_id=2, read_only=True)
    headers = {'Authorization': f'Bearer {token}'}
    assert client.get(URL + '/metadata', headers=headers).json()['can_manage'] is False
    assert client.get(URL + '/', headers=headers).json()['total'] == 0
    assert client.post(URL + '/', headers=headers, json=payload()).status_code == 403


def test_kiosk_token_cannot_access_suggestions(client, test_user):
    token = create_access_token(subject=test_user.id, company_id=1, scope='kiosk')
    headers = {'Authorization': f'Bearer {token}'}
    assert client.get(URL + '/metadata', headers=headers).status_code == 403
    assert client.get(URL + '/', headers=headers).status_code == 403
    assert client.post(URL + '/', headers=headers, json=payload()).status_code == 403


def test_existing_inactive_owner_does_not_block_unrelated_edits(client, db_session, auth_headers, admin_user):
    row = create(client, auth_headers, owner_id=admin_user.id)
    admin_user.is_active = False
    db_session.commit()
    result = patch(client, auth_headers, row, owner_id=admin_user.id, title='Improve the fixture key')
    assert result.status_code == 200
    saved = result.json()
    assert saved['owner_name'] == row['owner_name'] and saved['title'] == 'Improve the fixture key'
    unassigned = patch(client, auth_headers, saved, owner_id=None).json()
    assert unassigned['owner_id'] is None
    assert patch(client, auth_headers, unassigned, owner_id=admin_user.id).status_code == 422
