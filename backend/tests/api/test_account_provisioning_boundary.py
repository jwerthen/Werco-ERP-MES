"""Real HTTP role/tenant boundaries and transactional security audit evidence."""

import json
from datetime import datetime, timedelta
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.core.security import create_access_token, get_password_hash
from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

pytestmark = pytest.mark.api
PASSWORD = "Zephyr9!Quill-Test"


def make_user(db, role=UserRole.ADMIN, *, company_id=1, superuser=False, active=True):
    number = db.query(User).count() + 1
    user = User(
        company_id=company_id,
        email=f"principal{number}@example.com",
        employee_id=f"PRINCIPAL-{number}",
        first_name="Test",
        last_name="Principal",
        role=role,
        is_active=active,
        is_superuser=superuser,
        hashed_password=get_password_hash(PASSWORD),
    )
    db.add(user)
    db.commit()
    return user


def headers(user, company_id=None):
    return {"Authorization": "Bearer " + create_access_token(subject=user.id, company_id=company_id or user.company_id)}


def payload(**changes):
    return dict(
        email="newperson@example.com",
        employee_id="NEW-PERSON",
        first_name="New",
        last_name="Person",
        password=PASSWORD,
        role="operator",
        **changes,
    )


@pytest.mark.parametrize("path", ["/api/v1/auth/register", "/api/v1/users/"])
def test_tenant_creation_refuses_platform_role_without_side_effects(client, db_session, path):
    actor = make_user(db_session)
    body = payload()
    body["role"] = "platform_admin"
    response = client.post(path, headers=headers(actor), json=body)
    assert response.status_code == 400, response.text
    db_session.rollback()
    assert db_session.query(User).count() == 1
    assert db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize(
    "verb,suffix,body",
    [
        ("put", "", {"email": "takeover@example.com"}),
        ("post", "/reset-password", {"new_password": PASSWORD}),
        ("post", "/activate", {}),
        ("post", "/unlock", {}),
        ("delete", "", {}),
    ],
)
@pytest.mark.parametrize(
    "platform_role,superuser",
    [(UserRole.PLATFORM_ADMIN, False), (UserRole.ADMIN, True)],
)
def test_tenant_admin_cannot_take_over_existing_platform_principal(
    client, db_session, verb, suffix, body, platform_role, superuser
):
    actor = make_user(db_session)
    target = make_user(db_session, platform_role, superuser=superuser)
    target.failed_login_attempts = 5
    target.locked_until = datetime.utcnow() + timedelta(minutes=30)
    db_session.commit()
    before = (target.email, target.hashed_password, target.is_active)
    prior_lock = (target.failed_login_attempts, target.locked_until)
    response = client.request(verb, f"/api/v1/users/{target.id}{suffix}", headers=headers(actor), json=body)
    assert response.status_code == 403, response.text
    db_session.rollback()
    db_session.refresh(target)
    assert (target.email, target.hashed_password, target.is_active) == before
    assert (target.failed_login_attempts, target.locked_until) == prior_lock
    assert db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize("path", ["/api/v1/auth/register", "/api/v1/users/"])
def test_user_creation_rolls_back_when_required_audit_fails(client, db_session, monkeypatch, path):
    actor = make_user(db_session)
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    response = client.post(path, headers=headers(actor), json=payload())
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert db_session.query(User).count() == 1
    assert db_session.query(AuditLog).count() == 0


def test_platform_company_change_is_audited_in_target_company(client, db_session):
    actor = make_user(db_session, UserRole.PLATFORM_ADMIN)
    company = Company(name="Other Company", slug="other-company", is_active=True)
    db_session.add(company)
    db_session.commit()
    response = client.put(
        f"/api/v1/platform/companies/{company.id}",
        headers=headers(actor),
        json={"is_active": False},
    )
    assert response.status_code == 200, response.text
    db_session.rollback()
    row = db_session.query(AuditLog).filter_by(resource_type="company", resource_id=company.id).one()
    assert row.company_id == company.id and row.user_id == actor.id
    assert row.old_values["is_active"] is True and row.new_values["is_active"] is False
    assert row.integrity_hash
    assert PASSWORD not in json.dumps(row.extra_data)


def test_platform_company_change_rolls_back_when_audit_fails(client, db_session, monkeypatch):
    actor = make_user(db_session, UserRole.PLATFORM_ADMIN)
    company = db_session.get(Company, 1)
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    response = client.put(
        "/api/v1/platform/companies/1",
        headers=headers(actor),
        json={"is_active": False},
    )
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert company.is_active is True


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
@pytest.mark.parametrize("role", ["platform_admin", "admin"])
def test_import_role_policy_is_shared_across_file_formats(client, db_session, extension, role):
    actor = make_user(db_session)
    columns = [
        "employee_id",
        "first_name",
        "last_name",
        "email",
        "password",
        "role",
        "is_superuser",
        "company_id",
    ]
    values = [
        "IMPORTED",
        "Imported",
        "Person",
        "imported@example.com",
        PASSWORD,
        role,
        "true",
        "999",
    ]
    if extension == "csv":
        content = (",".join(columns) + "\n" + ",".join(values) + "\n").encode()
    else:
        workbook = Workbook()
        workbook.active.append(columns)
        workbook.active.append(values)
        stream = BytesIO()
        workbook.save(stream)
        content = stream.getvalue()
    response = client.post(
        "/api/v1/users/import-csv",
        headers=headers(actor),
        files={"file": (f"users.{extension}", content)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["created_count"] == (0 if role == "platform_admin" else 1)
    db_session.rollback()
    created = db_session.query(User).filter_by(employee_id="IMPORTED").first()
    if role == "platform_admin":
        assert created is None and db_session.query(AuditLog).count() == 0
    else:
        assert created.company_id == actor.company_id and created.is_superuser is False
        assert created.role == UserRole.ADMIN
        assert (
            db_session.query(AuditLog).filter_by(resource_id=created.id, resource_type="user").one().user_id == actor.id
        )


@pytest.mark.parametrize("path", ["/api/v1/auth/register", "/api/v1/users/"])
def test_platform_context_creates_only_a_tenant_user_and_audits_actor(client, db_session, path):
    actor = make_user(db_session, UserRole.PLATFORM_ADMIN)
    company = Company(name="Active Tenant", slug="active-tenant", is_active=True)
    db_session.add(company)
    db_session.commit()
    body = payload(company_id=1, is_superuser=True)
    body["role"] = "admin"
    response = client.post(path, headers=headers(actor, company.id), json=body)
    assert response.status_code == 200, response.text
    db_session.rollback()
    user = db_session.get(User, response.json()["id"])
    assert user.company_id == company.id and user.role == UserRole.ADMIN and user.is_superuser is False
    audit = db_session.query(AuditLog).filter_by(resource_id=user.id).one()
    assert audit.user_id == actor.id and audit.company_id == company.id


@pytest.mark.parametrize(
    "verb,suffix,body",
    [
        ("put", "", {"role": "platform_admin"}),
        ("post", "/approve", {"role": "platform_admin"}),
    ],
)
def test_alternate_role_grants_refuse_platform_without_side_effects(client, db_session, verb, suffix, body):
    actor = make_user(db_session)
    target = make_user(db_session, UserRole.VIEWER, active=False)
    response = client.request(verb, f"/api/v1/users/{target.id}{suffix}", headers=headers(actor), json=body)
    assert response.status_code == 400, response.text
    db_session.rollback()
    assert target.role == UserRole.VIEWER and target.is_active is False
    assert db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.MANAGER, UserRole.OPERATOR])
@pytest.mark.parametrize("path", ["/api/v1/users/", "/api/v1/auth/register"])
def test_non_admin_cannot_provision_an_account(client, db_session, role, path):
    actor = make_user(db_session, role)
    response = client.post(path, headers=headers(actor), json=payload())
    assert response.status_code == 403, response.text
    db_session.rollback()
    assert db_session.query(User).count() == 1 and db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize(
    "verb,suffix,body",
    [
        ("put", "", {"role": "manager"}),
        ("post", "/approve", {"role": "operator"}),
        ("post", "/reset-password", {"new_password": "Another9!Quill-Test"}),
        ("post", "/activate", {}),
        ("post", "/unlock", {}),
        ("delete", "", {}),
    ],
)
def test_account_mutations_are_tenant_scoped_and_fail_closed_on_audit_loss(
    client, db_session, monkeypatch, verb, suffix, body
):
    actor = make_user(db_session)
    target = make_user(db_session, UserRole.VIEWER, active=False)
    target.failed_login_attempts = 5
    target.locked_until = datetime.utcnow() + timedelta(minutes=30)
    db_session.commit()
    prior_lock = (target.failed_login_attempts, target.locked_until)
    original = (target.role, target.is_active, target.hashed_password)
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    response = client.request(verb, f"/api/v1/users/{target.id}{suffix}", headers=headers(actor), json=body)
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert (target.role, target.is_active, target.hashed_password) == original
    assert (target.failed_login_attempts, target.locked_until) == prior_lock
    assert db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize("platform", [False, True])
def test_company_provisioning_never_grants_platform_authority_and_is_audited(client, db_session, platform):
    actor = make_user(db_session, UserRole.PLATFORM_ADMIN if platform else UserRole.ADMIN)
    body = dict(
        admin_email="newadmin@example.com",
        admin_first_name="New",
        admin_last_name="Admin",
        admin_password=PASSWORD,
        role="platform_admin",
        is_superuser=True,
    )
    body["name" if platform else "company_name"] = "New Company"
    path = "/api/v1/platform/companies" if platform else "/api/v1/companies/register"
    response = client.post(path, headers=headers(actor), json=body)
    assert response.status_code == 200, response.text
    db_session.rollback()
    user = db_session.query(User).filter_by(email="newadmin@example.com").one()
    assert user.role == UserRole.ADMIN and user.is_superuser is False and user.company_id != actor.company_id
    assert db_session.query(AuditLog).filter_by(company_id=user.company_id).count() == 2


def test_tenant_admin_cannot_invoke_platform_company_mutation(client, db_session):
    actor = make_user(db_session)
    response = client.put(
        "/api/v1/platform/companies/1",
        headers=headers(actor),
        json={"is_active": False},
    )
    assert response.status_code == 403, response.text
    assert db_session.get(Company, 1).is_active is True
    assert db_session.query(AuditLog).count() == 0


def test_platform_admin_can_still_manage_platform_account(client, db_session):
    actor = make_user(db_session, UserRole.PLATFORM_ADMIN)
    target = make_user(db_session, UserRole.PLATFORM_ADMIN)
    response = client.post(
        f"/api/v1/users/{target.id}/reset-password",
        headers=headers(actor),
        json={"new_password": "Another9!Quill-Test"},
    )
    assert response.status_code == 200, response.text
    assert (
        db_session.query(AuditLog).filter_by(action="PASSWORD_CHANGE", resource_id=target.id).one().user_id == actor.id
    )


@pytest.mark.parametrize("minutes", [None, -30, 30])
@pytest.mark.parametrize("audit_loss", [False, True])
def test_platform_unlock_preserves_lock_evidence_and_requires_audit(
    client, db_session, monkeypatch, minutes, audit_loss
):
    actor = make_user(db_session, UserRole.PLATFORM_ADMIN)
    target = make_user(db_session, UserRole.PLATFORM_ADMIN)
    target.failed_login_attempts = 5
    target.locked_until = datetime.utcnow() + timedelta(minutes=minutes) if minutes is not None else None
    db_session.commit()
    prior_lock = (target.failed_login_attempts, target.locked_until)
    if audit_loss:
        monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    response = client.post(f"/api/v1/users/{target.id}/unlock", headers=headers(actor))
    assert response.status_code == (503 if audit_loss else 200), response.text
    db_session.rollback()
    if audit_loss:
        assert (target.failed_login_attempts, target.locked_until) == prior_lock
        assert db_session.query(AuditLog).count() == 0
    else:
        assert (target.failed_login_attempts, target.locked_until) == (0, None)
        row = db_session.query(AuditLog).filter_by(resource_type="user", resource_id=target.id).one()
        assert row.user_id == actor.id and row.company_id == target.company_id
        assert row.action == ("STATUS_CHANGE" if minutes == 30 else "UPDATE")
        evidence = row.extra_data if minutes == 30 else row.old_values
        assert evidence["failed_login_attempts"] == 5
        assert evidence["locked_until"] == (prior_lock[1].isoformat() if prior_lock[1] else None)


@pytest.mark.parametrize(
    "verb,suffix,body",
    [
        ("put", "", {"role": "manager"}),
        ("post", "/approve", {"role": "operator"}),
        ("post", "/reset-password", {"new_password": PASSWORD}),
        ("post", "/activate", {}),
        ("post", "/unlock", {}),
        ("delete", "", {}),
    ],
)
def test_account_mutations_hide_wrong_tenant_targets(client, db_session, verb, suffix, body):
    actor = make_user(db_session)
    company = Company(name="Other Tenant", slug="other-tenant", is_active=True)
    db_session.add(company)
    db_session.commit()
    target = make_user(db_session, UserRole.VIEWER, active=False, company_id=company.id)
    original = (target.role, target.is_active, target.hashed_password)
    response = client.request(verb, f"/api/v1/users/{target.id}{suffix}", headers=headers(actor), json=body)
    assert response.status_code == 404, response.text
    db_session.rollback()
    assert (target.role, target.is_active, target.hashed_password) == original
    assert db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize("path", ["/api/v1/auth/register", "/api/v1/users/"])
def test_readonly_platform_context_cannot_provision(client, db_session, path):
    actor = make_user(db_session, UserRole.PLATFORM_ADMIN)
    token = create_access_token(subject=actor.id, company_id=actor.company_id, read_only=True)
    response = client.post(path, headers={"Authorization": "Bearer " + token}, json=payload())
    assert response.status_code == 403, response.text
    assert db_session.query(User).count() == 1 and db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize("bootstrap", [True, False])
def test_public_registration_audit_loss_leaves_no_account(client, db_session, monkeypatch, bootstrap):
    if not bootstrap:
        make_user(db_session)
    count = db_session.query(User).count()
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    response = client.post("/api/v1/auth/register-public", json=payload())
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert db_session.query(User).count() == count and db_session.query(AuditLog).count() == 0


def test_public_registration_ignores_caller_platform_and_company_claims(client, db_session):
    make_user(db_session)
    body = payload(is_superuser=True, company_id=999)
    body["role"] = "platform_admin"
    response = client.post("/api/v1/auth/register-public", json=body)
    assert response.status_code == 200, response.text
    user = db_session.query(User).filter_by(email=body["email"]).one()
    assert user.role == UserRole.VIEWER and user.is_superuser is False and user.is_active is False
    assert user.company_id == 1


@pytest.mark.parametrize("path", ["/api/v1/companies/register", "/api/v1/platform/companies"])
def test_company_onboarding_rolls_back_even_when_second_audit_fails(client, db_session, monkeypatch, path):
    actor = make_user(db_session, UserRole.PLATFORM_ADMIN)
    original = AuditService.log

    def lose_user_audit(self, *args, **kwargs):
        return None if kwargs.get("resource_type") == "user" else original(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, "log", lose_user_audit)
    body = dict(
        admin_email="newadmin@example.com",
        admin_first_name="New",
        admin_last_name="Admin",
        admin_password=PASSWORD,
    )
    body["name" if path.endswith("/companies") else "company_name"] = "New Company"
    response = client.post(path, headers=headers(actor), json=body)
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert db_session.query(Company).count() == 1 and db_session.query(User).count() == 1
    assert db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize("verb,body", [("delete", {}), ("put", {"is_active": False})])
def test_deactivation_rolls_back_when_companion_token_audit_fails(client, db_session, monkeypatch, verb, body):
    actor = make_user(db_session)
    target = make_user(db_session, UserRole.OPERATOR)
    token = ApiToken(
        company_id=1,
        user_id=target.id,
        label="Synthetic bot",
        jti="synthetic-revocation-jti",
        created_by=actor.id,
        revoked=False,
    )
    db_session.add(token)
    db_session.commit()
    original = AuditService.log

    def lose_token_audit(self, *args, **kwargs):
        return None if kwargs.get("resource_type") == "api_token" else original(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, "log", lose_token_audit)
    response = client.request(verb, f"/api/v1/users/{target.id}", headers=headers(actor), json=body)
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert target.is_active is True and token.revoked is False
    assert token.revoked_by is None and token.revoke_reason is None
    assert db_session.query(AuditLog).count() == 0
