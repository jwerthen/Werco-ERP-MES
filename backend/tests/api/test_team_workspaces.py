import pytest

from app.core.security import create_access_token
from app.models.company import Company
from app.models.role_permission import RolePermission
from app.models.user import User, UserRole
from app.models.user_workspace import TeamWorkspaceRecord

URL = '/api/v1/user-workspaces/team/work-orders'


def body(version=0):
    return {
        'kind': 'view',
        'name': 'Priority jobs',
        'data': {'table': 'orders', 'layout': {'dense': True}, 'filters': {'status': 'released'}},
        'version': version,
    }


def test_shared_view_private_draft_and_version_conflicts(client, admin_headers, manager_headers):
    private = '/api/v1/user-workspaces/work-orders/draft'
    assert (
        client.put(
            private,
            headers=admin_headers,
            json={'kind': 'draft', 'name': 'Secret', 'data': {'notes': 'Private'}, 'version': 0},
        ).status_code
        == 200
    )
    created = client.put(URL + '/priority', headers=admin_headers, json=body())
    assert created.status_code == 200, created.text
    listing = client.get(URL, headers=manager_headers).json()
    assert listing['items'] == [created.json()]
    assert listing['can_manage']
    assert client.get('/api/v1/user-workspaces/work-orders?kind=draft', headers=manager_headers).json() == []
    assert client.put(URL + '/priority', headers=manager_headers, json=body(1)).json()['version'] == 2
    assert client.put(URL + '/priority', headers=admin_headers, json=body(1)).status_code == 409
    assert client.delete(URL + '/priority?version=1', headers=admin_headers).status_code == 409
    assert client.delete(URL + '/priority?version=2', headers=admin_headers).status_code == 204
    assert client.put(URL + '/priority', headers=admin_headers, json=body(2)).status_code == 409


def test_reader_cannot_mutate_and_revocation_applies(client, db_session, admin_headers, test_user, manager_headers):
    assert client.put(URL + '/priority', headers=admin_headers, json=body()).status_code == 200
    test_user.role = UserRole.OPERATOR
    db_session.add(
        RolePermission(company_id=test_user.company_id, role=UserRole.OPERATOR, permissions=['work_orders:view'])
    )
    db_session.commit()
    listing = client.get(URL, headers=manager_headers)
    assert listing.status_code == 200
    assert len(listing.json()['items']) == 1
    assert not listing.json()['can_manage']
    assert client.put(URL + '/priority', headers=manager_headers, json=body(1)).status_code == 403
    assert client.delete(URL + '/priority?version=1', headers=manager_headers).status_code == 403
    db_session.query(RolePermission).filter(RolePermission.role == UserRole.OPERATOR).update({'permissions': []})
    db_session.commit()
    assert client.get(URL, headers=manager_headers).status_code == 403


def test_company_scope_and_platform_readonly(client, db_session, admin_headers, admin_user):
    assert client.put(URL + '/priority', headers=admin_headers, json=body()).status_code == 200
    db_session.add(Company(id=2, name='Other', slug='team-other', is_active=True))
    other = User(
        company_id=2,
        employee_id='OTHER',
        email='other-team@example.test',
        first_name='Other',
        last_name='User',
        hashed_password='unused',
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(other)
    db_session.commit()
    headers = {'Authorization': 'Bearer ' + create_access_token(subject=other.id, company_id=2)}
    assert client.get(URL, headers=headers).json()['items'] == []
    assert client.delete(URL + '/priority?version=1', headers=headers).status_code == 204
    assert client.put(URL + '/priority', headers=headers, json=body()).status_code == 200
    assert db_session.query(TeamWorkspaceRecord).count() == 2
    admin_user.role = UserRole.PLATFORM_ADMIN
    db_session.commit()
    readonly = {'Authorization': 'Bearer ' + create_access_token(subject=admin_user.id, company_id=2, read_only=True)}
    assert client.get(URL, headers=readonly).status_code == 200
    assert client.put(URL + '/priority', headers=readonly, json=body(1)).status_code == 403


@pytest.mark.parametrize(
    'change', [{'kind': 'draft'}, {'data': {'notes': 'private'}}, {'company_id': 2}, {'user_id': 2}]
)
def test_reject_draft_or_scope_payload(client, admin_headers, change):
    assert client.put(URL + '/priority', headers=admin_headers, json={**body(), **change}).status_code == 422


@pytest.mark.parametrize('namespace', ['inventory', 'parts', 'shipping'])
def test_remaining_namespaces(client, admin_headers, namespace):
    assert (
        client.put(f'/api/v1/user-workspaces/{namespace}/private', headers=admin_headers, json=body()).status_code
        == 200
    )
    assert (
        client.put(f'/api/v1/user-workspaces/team/{namespace}/shared', headers=admin_headers, json=body()).status_code
        == 200
    )
