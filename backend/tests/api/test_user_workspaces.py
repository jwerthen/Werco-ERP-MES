import pytest

from app.core.security import create_access_token
from app.models.company import Company
from app.models.role_permission import RolePermission
from app.models.user import User, UserRole
from app.models.user_workspace import UserWorkspaceRecord

URL = "/api/v1/user-workspaces/work-orders"


def body(version=0, **changes):
    return dict(
        kind="draft",
        name="Unfinished order",
        data={"notes": "Private synthetic note"},
        version=version,
        **changes,
    )


def test_roundtrip_compare_and_swap_and_delete(client, admin_headers):
    first = client.put(URL + "/new", headers=admin_headers, json=body())
    assert first.status_code == 200
    assert first.json()["version"] == 1
    changed = body(1)
    changed["data"] = {"notes": "Revised"}
    updated = client.put(URL + "/new", headers=admin_headers, json=changed)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["data"] == {"notes": "Revised"}
    stale = client.put(URL + "/new", headers=admin_headers, json=body(1))
    assert stale.status_code == 409
    listing = client.get(URL, params={"kind": "draft"}, headers=admin_headers).json()
    assert listing == [updated.json()]
    assert client.delete(URL + "/new?kind=draft&version=1", headers=admin_headers).status_code == 409
    assert client.delete(URL + "/new?kind=draft&version=2", headers=admin_headers).status_code == 204
    assert client.delete(URL + "/new?kind=draft&version=2", headers=admin_headers).status_code == 204
    assert client.put(URL + "/new", headers=admin_headers, json=body(2)).status_code == 409
    assert client.get(URL + "?kind=draft", headers=admin_headers).json() == []


def test_same_key_is_private_to_user_and_company(client, db_session, admin_headers, manager_headers, admin_user):
    client.put(URL + "/new", headers=admin_headers, json=body())
    assert client.get(URL + "?kind=draft", headers=manager_headers).json() == []
    assert client.delete(URL + "/new?kind=draft&version=1", headers=manager_headers).status_code == 204
    assert len(client.get(URL + "?kind=draft", headers=admin_headers).json()) == 1
    db_session.add(Company(id=2, name="Other company", slug="other-workspace-company", is_active=True))
    db_session.flush()
    other = User(
        company_id=2,
        employee_id="OTHER",
        email="private@example.test",
        first_name="Other",
        last_name="User",
        hashed_password="unused",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(other)
    db_session.commit()
    headers = {"Authorization": "Bearer " + create_access_token(subject=other.id, company_id=2)}
    assert client.get(URL + "?kind=draft", headers=headers).json() == []
    assert client.put(URL + "/new", headers=headers, json=body()).status_code == 200
    assert db_session.query(UserWorkspaceRecord).count() == 2
    assert client.get(URL + "?kind=draft", headers=admin_headers).json()[0]["data"]["notes"] == "Private synthetic note"


def test_permission_revocation_hides_existing_drafts(client, db_session, manager_headers, test_user):
    assert client.put(URL + "/new", headers=manager_headers, json=body()).status_code == 200
    db_session.add(RolePermission(company_id=test_user.company_id, role=test_user.role, permissions=[]))
    db_session.commit()
    assert client.get(URL + "?kind=draft", headers=manager_headers).status_code == 403
    assert client.put(URL + "/new", headers=manager_headers, json=body(1)).status_code == 403
    assert client.delete(URL + "/new?kind=draft&version=1", headers=manager_headers).status_code == 403


def test_namespaces_and_kinds_do_not_overlap(client, admin_headers):
    assert client.put(URL + "/new", headers=admin_headers, json=body()).status_code == 200
    view = body()
    view["kind"] = "view"
    assert client.put(URL + "/new", headers=admin_headers, json=view).status_code == 200
    assert len(client.get(URL, headers=admin_headers).json()) == 1
    assert client.get("/api/v1/user-workspaces/quality?kind=draft", headers=admin_headers).json() == []


def test_quota_and_delete_frees_slot(client, admin_headers):
    for number in range(25):
        assert client.put(URL + f"/view-{number}", headers=admin_headers, json=body()).status_code == 200
    assert client.put(URL + "/overflow", headers=admin_headers, json=body()).status_code == 409
    assert client.put(URL + "/view-0", headers=admin_headers, json=body(1)).status_code == 200
    assert client.delete(URL + "/view-1?kind=draft&version=1", headers=admin_headers).status_code == 204
    assert client.put(URL + "/overflow", headers=admin_headers, json=body()).status_code == 200


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "business"),
        ("name", "  "),
        ("name", "a" * 101),
        ("data", []),
        ("data", {"notes": "a" * 200_001}),
        ("version", -1),
        ("company_id", 2),
        ("user_id", 2),
    ],
)
def test_reject_invalid_or_owner_injected_payloads(client, admin_headers, field, value):
    data = body()
    data[field] = value
    assert client.put(URL + "/new", headers=admin_headers, json=data).status_code == 422


def test_auth_required_and_namespace_allowlist(client, admin_headers):
    assert client.get(URL).status_code == 401
    assert client.put(URL + "/bad.key", headers=admin_headers, json=body()).status_code == 422
    assert client.get("/api/v1/user-workspaces/credentials", headers=admin_headers).status_code == 404


def test_cross_company_platform_readonly_cannot_write(client, db_session, admin_user):
    db_session.add(Company(id=2, name="Read only company", slug="workspace-read-only", is_active=True))
    admin_user.role = UserRole.PLATFORM_ADMIN
    db_session.commit()
    headers = {"Authorization": "Bearer " + create_access_token(subject=admin_user.id, company_id=2, read_only=True)}
    assert client.put(URL + "/new", headers=headers, json=body()).status_code == 403
