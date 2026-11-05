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

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.quote_config import SettingsAuditLog
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole
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
    # The settings-history row is in the same transaction, so it rolls back with the change.
    assert db_session.query(SettingsAuditLog).filter(SettingsAuditLog.entity_type == "role_permission").count() == 0


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


# ---------------------------------------------------------------------------
# Coverage added in review: tenant attribution, refusals, the admin-only gate
# ---------------------------------------------------------------------------

_SETTINGS_RESOURCE_TYPES = [
    "role_permission",
    "work_center",
    "notification_email_recipients",
    "work_center_types",
]
_seq = {"n": 0}


def _company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Settings co {company_id}", slug=f"settings-co-{company_id}")
        company.is_active = True
        db.add(company)
        db.commit()
    return company


def _make_user(db: Session, *, role: UserRole, company_id: int) -> User:
    _company(db, company_id)
    _seq["n"] += 1
    n = _seq["n"]
    user = User(
        email=f"settings-chain-{n}@co{company_id}.test",
        employee_id=f"SCH-{n:05d}",
        first_name="Settings",
        last_name=role.value,
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User, *, active_company_id: int = None) -> dict:
    """A switched context is simulated by minting the token for another company (see the
    scope note in ``test_admin_settings_audit_tenant.py`` -- production switches are read-only)."""
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _settings_chain_rows(db: Session):
    db.rollback()
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type.in_(_SETTINGS_RESOURCE_TYPES)).all()


def test_switched_platform_admin_attributes_chain_rows_to_the_active_company(client: TestClient, db_session: Session):
    """``role_permission`` passes ``company_id`` explicitly to ``log_required``; the rate row
    resolves it from the request user. Both must land on the ACTIVE tenant (2), never the
    platform admin's home tenant (1) -- otherwise company 2's RBAC history is filed under
    company 1."""
    platform_admin = _make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=1)
    _company(db_session, 2)
    center = WorkCenter(code="RATE-CO2", name="Co2 cell", work_center_type="laser", hourly_rate=40.0, company_id=2)
    db_session.add(center)
    db_session.commit()
    headers = _headers_for(platform_admin, active_company_id=2)

    permissions = client.put(f"{BASE}/role-permissions/viewer", headers=headers, json=["parts:view"])
    assert permissions.status_code == status.HTTP_200_OK, permissions.text
    rate = client.put(f"{BASE}/work-center-rates/{center.id}", headers=headers, json={"hourly_rate": 55.0})
    assert rate.status_code == status.HTTP_200_OK, rate.text

    rows = {row.resource_type: row for row in _settings_chain_rows(db_session)}
    assert set(rows) == {"role_permission", "work_center"}
    for row in rows.values():
        assert row.company_id == 2, row.resource_type
        assert row.user_id == platform_admin.id
    stored = db_session.query(RolePermission).filter(RolePermission.role == UserRole.VIEWER).one()
    assert stored.company_id == 2 and rows["role_permission"].resource_id == stored.id


def _refusal_cases(db: Session):
    """(method, url, body, expected status) for every settings write that must refuse."""
    _company(db, 2)
    foreign_center = WorkCenter(code="RATE-FOREIGN", name="Foreign", work_center_type="laser", company_id=2)
    in_use_center = WorkCenter(code="TYPE-IN-USE", name="In use", work_center_type="welding", company_id=1)
    db.add_all([foreign_center, in_use_center])
    db.commit()
    foreign_user = _make_user(db, role=UserRole.MANAGER, company_id=2)
    return [
        ("put", f"{BASE}/role-permissions/not_a_role", ["parts:view"], status.HTTP_400_BAD_REQUEST),
        ("put", f"{BASE}/role-permissions/viewer", ["not:a_permission"], status.HTTP_400_BAD_REQUEST),
        ("post", f"{BASE}/role-permissions/not_a_role/reset", None, status.HTTP_400_BAD_REQUEST),
        ("put", f"{BASE}/work-center-rates/{foreign_center.id}", {"hourly_rate": 1.0}, status.HTTP_404_NOT_FOUND),
        ("put", f"{BASE}/email-recipients/not.an.event", {"user_ids": None}, status.HTTP_400_BAD_REQUEST),
        (
            "put",
            f"{BASE}/email-recipients/wo.completed",
            {"user_ids": [foreign_user.id]},
            status.HTTP_400_BAD_REQUEST,
        ),
        ("put", f"{BASE}/work-center-types", {"types": ["laser"]}, status.HTTP_400_BAD_REQUEST),
    ]


def test_refused_settings_writes_leave_no_chain_or_history_row(client: TestClient, db_session: Session, admin_headers):
    """Every refusal -- bad role, unknown permission, another tenant's work center or user,
    unknown email event, removing an in-use work-center type -- is raised before any write:
    no chain row, no history row, no override, and the foreign work center's rate untouched."""
    cases = _refusal_cases(db_session)
    for method, url, body, expected in cases:
        response = getattr(client, method)(url, headers=admin_headers, **({} if body is None else {"json": body}))
        assert response.status_code == expected, (url, response.text)

    assert _settings_chain_rows(db_session) == []
    assert db_session.query(SettingsAuditLog).count() == 0
    assert db_session.query(RolePermission).count() == 0
    foreign = db_session.query(WorkCenter).filter(WorkCenter.code == "RATE-FOREIGN").one()
    assert foreign.hourly_rate != 1.0


@pytest.mark.parametrize("role", [UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.VIEWER])
def test_non_admins_cannot_write_rates_recipients_or_types(client: TestClient, db_session: Session, role):
    actor = _make_user(db_session, role=role, company_id=1)
    center = WorkCenter(code=f"RATE-GATE-{role.value}", name="Gate", work_center_type="laser", hourly_rate=40.0)
    center.company_id = 1
    db_session.add(center)
    db_session.commit()
    headers = _headers_for(actor)

    responses = [
        client.put(f"{BASE}/work-center-rates/{center.id}", headers=headers, json={"hourly_rate": 99.0}),
        client.put(f"{BASE}/email-recipients/wo.completed", headers=headers, json={"user_ids": [actor.id]}),
        client.put(f"{BASE}/work-center-types", headers=headers, json={"types": ["laser", "gate_probe"]}),
        client.post(f"{BASE}/role-permissions/viewer/reset", headers=headers),
    ]
    for response in responses:
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text

    assert _settings_chain_rows(db_session) == []
    assert db_session.query(SettingsAuditLog).count() == 0
    assert db_session.query(WorkCenter).filter(WorkCenter.id == center.id).one().hourly_rate == 40.0
