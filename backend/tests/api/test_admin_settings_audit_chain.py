"""Admin-settings writes are evidenced on the tamper-evident audit chain, not only a side table.

Role-permission (RBAC), labor-rate, email-recipient and work-center-type changes used to be
recorded ONLY in ``settings_audit_log`` -- a plain mutable tenant table outside the hash
chain and outside the ``008``/``060`` UPDATE/DELETE triggers. That table stays (the Admin
page's history tab reads it), but every write now ALSO goes through ``AuditService``:

* role permissions via ``log_required`` inside ``atomic_security_write`` -- an RBAC change
  whose audit row cannot be written is rolled back and answered 503;
* rates / recipients / types via the ordinary ``log_update`` helper, in the same transaction.

Audit rows are read COMMITTED-only (roll back, then query).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.quote_config import SettingsAuditLog
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import UserRole
from app.models.work_center import WorkCenter
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/admin/settings"


def _committed(db: Session, resource_type: str):
    db.rollback()
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == resource_type).order_by(AuditLog.sequence_number).all()


def _stored(db: Session, role: UserRole):
    db.rollback()
    db.expire_all()
    return db.query(RolePermission).filter(RolePermission.company_id == 1, RolePermission.role == role).first()


# ---------------------------------------------------------------------------
# Role permissions (RBAC) -- required evidence
# ---------------------------------------------------------------------------


def test_first_customization_writes_required_row_with_grants_and_real_id(
    client: TestClient, db_session: Session, admin_user, admin_headers
):
    defaults = set(DEFAULT_ROLE_PERMISSIONS[UserRole.SUPERVISOR])
    new_permissions = sorted((defaults - {"purchasing:create"}) | {"admin:audit_logs"})

    response = client.put(f"{BASE}/role-permissions/supervisor", headers=admin_headers, json=new_permissions)
    assert response.status_code == status.HTTP_200_OK, response.text

    stored = _stored(db_session, UserRole.SUPERVISOR)
    (row,) = _committed(db_session, "role_permission")
    assert row.action == "UPDATE"
    assert row.resource_id == stored.id and row.resource_id != 0
    assert row.resource_identifier == "supervisor"
    assert row.user_id == admin_user.id and row.company_id == 1
    assert row.old_values == {"permissions": sorted(defaults), "is_customized": False}
    assert row.new_values == {"permissions": new_permissions, "is_customized": True}
    assert row.extra_data["action"] == "update"
    assert row.extra_data["granted"] == ["admin:audit_logs"]
    assert row.extra_data["revoked"] == ["purchasing:create"]

    # The Admin page's history table keeps working -- and no longer says entity_id 0.
    history = db_session.query(SettingsAuditLog).filter(SettingsAuditLog.entity_type == "role_permission").all()
    assert [entry.entity_id for entry in history] == [stored.id]


def test_reset_writes_required_row_back_to_defaults(client: TestClient, db_session: Session, admin_headers):
    assert (
        client.put(f"{BASE}/role-permissions/viewer", headers=admin_headers, json=["parts:view"]).status_code
        == status.HTTP_200_OK
    )
    stored_id = _stored(db_session, UserRole.VIEWER).id

    response = client.post(f"{BASE}/role-permissions/viewer/reset", headers=admin_headers)
    assert response.status_code == status.HTTP_200_OK, response.text

    assert _stored(db_session, UserRole.VIEWER) is None
    rows = _committed(db_session, "role_permission")
    assert [row.extra_data["action"] for row in rows] == ["update", "reset"]
    reset = rows[1]
    assert reset.resource_id == stored_id
    assert reset.old_values == {"permissions": ["parts:view"], "is_customized": True}
    assert reset.new_values == {
        "permissions": sorted(DEFAULT_ROLE_PERMISSIONS[UserRole.VIEWER]),
        "is_customized": False,
    }


def test_reset_of_an_uncustomized_role_writes_nothing(client: TestClient, db_session: Session, admin_headers):
    response = client.post(f"{BASE}/role-permissions/operator/reset", headers=admin_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _committed(db_session, "role_permission") == []


def test_rbac_change_rolls_back_when_its_audit_row_cannot_be_written(
    client: TestClient, db_session: Session, admin_headers, monkeypatch
):
    original = ["parts:view", "boms:view"]
    db_session.add(RolePermission(company_id=1, role=UserRole.MANAGER, permissions=original, updated_by=None))
    db_session.commit()

    monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)
    response = client.put(f"{BASE}/role-permissions/manager", headers=admin_headers, json=["admin:system"])
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text

    assert _stored(db_session, UserRole.MANAGER).permissions == original
    assert db_session.query(SettingsAuditLog).filter(SettingsAuditLog.entity_type == "role_permission").count() == 0

    reset = client.post(f"{BASE}/role-permissions/manager/reset", headers=admin_headers)
    assert reset.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, reset.text
    assert _stored(db_session, UserRole.MANAGER).permissions == original


def test_non_admin_cannot_change_role_permissions(client: TestClient, db_session: Session, manager_headers):
    response = client.put(f"{BASE}/role-permissions/viewer", headers=manager_headers, json=["parts:view"])
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert _stored(db_session, UserRole.VIEWER) is None
    assert _committed(db_session, "role_permission") == []


# ---------------------------------------------------------------------------
# Labor rates, recipients, work-center types
# ---------------------------------------------------------------------------


def test_labor_rate_change_writes_work_center_update_row(
    client: TestClient, db_session: Session, admin_user, admin_headers
):
    center = WorkCenter(
        code="RATE-AUD-1", name="Rate audit", work_center_type="welding", hourly_rate=80.0, company_id=1
    )
    db_session.add(center)
    db_session.commit()

    response = client.put(f"{BASE}/work-center-rates/{center.id}", headers=admin_headers, json={"hourly_rate": 95.5})
    assert response.status_code == status.HTTP_200_OK, response.text

    (row,) = _committed(db_session, "work_center")
    assert row.action == "UPDATE"
    assert row.resource_id == center.id and row.resource_identifier == "RATE-AUD-1"
    assert row.user_id == admin_user.id and row.company_id == 1
    assert row.extra_data["changes"] == {"hourly_rate": {"old": 80.0, "new": 95.5}}
    assert row.extra_data["source"] == "admin_settings"


def test_email_recipient_change_writes_chain_row(
    client: TestClient, db_session: Session, admin_user, admin_headers, test_user
):
    response = client.put(
        f"{BASE}/email-recipients/wo.completed", headers=admin_headers, json={"user_ids": [test_user.id]}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    restored = client.put(f"{BASE}/email-recipients/wo.completed", headers=admin_headers, json={"user_ids": None})
    assert restored.status_code == status.HTTP_200_OK, restored.text

    rows = _committed(db_session, "notification_email_recipients")
    assert len(rows) == 2
    first, second = rows
    assert first.resource_identifier == "wo.completed" and first.user_id == admin_user.id
    assert first.new_values == {"user_ids": [test_user.id], "uses_default": False}
    assert first.old_values["uses_default"] is True
    assert second.new_values == {"user_ids": None, "uses_default": True}


def test_work_center_types_change_writes_chain_row(client: TestClient, db_session: Session, admin_headers):
    current = client.get(f"{BASE}/work-center-types", headers=admin_headers).json()["types"]
    new_types = [*current, "waterjet_audit"]

    response = client.put(f"{BASE}/work-center-types", headers=admin_headers, json={"types": new_types})
    assert response.status_code == status.HTTP_200_OK, response.text

    (row,) = _committed(db_session, "work_center_types")
    assert row.action == "UPDATE"
    assert "waterjet_audit" in row.new_values["types"]
    assert "waterjet_audit" not in (row.old_values.get("types") or [])
