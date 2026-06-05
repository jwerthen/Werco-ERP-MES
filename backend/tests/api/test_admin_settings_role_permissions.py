from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import UserRole


def test_role_permissions_are_scoped_to_current_company(
    client: TestClient,
    admin_headers: dict,
    db_session: Session,
):
    other_company = Company(id=2, name="Other Co", slug="other-co", is_active=True)
    db_session.add(other_company)
    db_session.add(
        RolePermission(
            company_id=2,
            role=UserRole.MANAGER,
            permissions=["admin:system"],
            updated_by=1,
        )
    )
    db_session.commit()

    response = client.get("/api/v1/admin/settings/role-permissions", headers=admin_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["role_permissions"]["manager"] == DEFAULT_ROLE_PERMISSIONS[UserRole.MANAGER]

    update_response = client.put(
        "/api/v1/admin/settings/role-permissions/manager",
        headers=admin_headers,
        json=["parts:view"],
    )

    assert update_response.status_code == status.HTTP_200_OK
    assert update_response.json()["permissions"] == ["parts:view"]

    saved = (
        db_session.query(RolePermission)
        .filter(RolePermission.company_id == 1, RolePermission.role == UserRole.MANAGER)
        .one()
    )
    assert saved.permissions == ["parts:view"]

    other_saved = (
        db_session.query(RolePermission)
        .filter(RolePermission.company_id == 2, RolePermission.role == UserRole.MANAGER)
        .one()
    )
    assert other_saved.permissions == ["admin:system"]

    reset_response = client.post(
        "/api/v1/admin/settings/role-permissions/manager/reset",
        headers=admin_headers,
    )

    assert reset_response.status_code == status.HTTP_200_OK
    assert (
        db_session.query(RolePermission)
        .filter(RolePermission.company_id == 1, RolePermission.role == UserRole.MANAGER)
        .first()
        is None
    )
    assert db_session.query(RolePermission).filter(
        RolePermission.company_id == 2, RolePermission.role == UserRole.MANAGER
    ).one().permissions == ["admin:system"]
