"""Long-lived per-user API tokens -- the Admin verbs (``/api/v1/api-tokens``) and the row.

Every test here fails if the feature were deleted: it asserts real HTTP statuses
and details, ``api_tokens`` rows, ``audit_log`` rows and the wire behaviour of
the token on its NEXT request. The plaintext token is never printed --
wherever a failure message would otherwise echo it, the comparison is bound to
a bare boolean first so pytest's assertion rewriting shows only ``False``.

Companion suites: ``test_api_token_auth.py`` (the ``get_current_user`` seam,
the fence, the throttle, the sibling principals) and ``test_mcp_api_tokens.py``
(the MCP door + the stdio bridge). The migration/model lock-step lives in
``test_migration_088_api_tokens.py``.
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_api_token, verify_api_token, verify_token
from app.db.database import SessionLocal
from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services import api_token_service
from app.services.api_token_service import (
    AUTH_EVENT_ISSUED,
    AUTH_EVENT_REVOKED,
    DEACTIVATION_REVOKE_REASON,
    MAX_API_TOKEN_EXPIRES_DAYS,
    PLATFORM_PRINCIPAL_DETAIL,
    check_api_token,
    is_platform_principal,
)
from app.services.audit_service import AuditService

pytestmark = pytest.mark.api

API_TOKENS_URL = "/api/v1/api-tokens/"
PARTS_URL = "/api/v1/parts/"
USERS_URL = "/api/v1/users/"
ADMIN_ONLY_URL = "/api/v1/admin/settings/materials"
FENCE_DETAIL = "API token cannot access this resource"
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # nosec B105 - not a real credential


# --------------------------------------------------------------------------- helpers


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _audit_text(row: AuditLog) -> str:
    return json.dumps(
        [row.description, row.resource_identifier, row.new_values, row.old_values, row.extra_data, row.error_message],
        default=str,
    )


def _no_secret_in_audit(db: Session, token: str, jti: str) -> bool:
    """Neither the JWT nor the full jti reaches any audit row (only the 8-char prefix may)."""
    rows = db.query(AuditLog).all()
    return all(token not in _audit_text(r) and jti not in _audit_text(r) for r in rows)


def _issue(client: TestClient, headers: dict, user_id: int, label: str = "Werco Assistant", **extra):
    return client.post(API_TOKENS_URL, json={"user_id": user_id, "label": label, **extra}, headers=headers)


def _revoke_url(token_id: int) -> str:
    return f"{API_TOKENS_URL}{token_id}/revoke"


def seed_second_company(db: Session) -> Tuple[Company, User, User]:
    """Company id=2 with its own ADMIN and MANAGER (for cross-tenant refusals)."""
    company = db.query(Company).filter(Company.id == 2).first()
    if company is None:
        company = Company(id=2, name="Other Shop", slug="other-shop", is_active=True)
        db.add(company)
        db.commit()
    admin2 = User(
        email="admin2@other-shop.test",
        employee_id="EMP-C2-ADMIN",
        first_name="Other",
        last_name="Admin",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.ADMIN,
        is_active=True,
        company_id=2,
    )
    manager2 = User(
        email="manager2@other-shop.test",
        employee_id="EMP-C2-MGR",
        first_name="Other",
        last_name="Manager",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.MANAGER,
        is_active=True,
        company_id=2,
    )
    db.add_all([admin2, manager2])
    db.commit()
    db.refresh(admin2)
    db.refresh(manager2)
    return company, admin2, manager2


def headers_for(user: User) -> dict:
    from app.core.security import create_access_token

    return _bearer(create_access_token(subject=user.id, company_id=user.company_id))


def _forge(
    *,
    jti: str,
    user_id: int,
    company_id: int,
    token_type: str = "api",
    secret: Optional[str] = None,
    exp: Optional[datetime] = None,
) -> str:
    """A JWT signed with the app's key (or another) carrying whatever claims the test wants."""
    claims: dict = {"sub": f"api:{jti}", "type": token_type, "jti": jti, "user_id": user_id, "company_id": company_id}
    if exp is not None:
        claims["exp"] = exp
    return jwt.encode(claims, secret or settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# --------------------------------------------------------------------------- issue


class TestIssue:
    def test_admin_issues_a_never_expiring_token_shown_once(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
    ):
        response = _issue(client, admin_headers, test_user.id)
        assert response.status_code == 201, response.text
        body = response.json()

        token = body["token"]
        assert isinstance(token, str) and token.count(".") == 2, "a real three-segment JWT"
        assert body["user_id"] == test_user.id
        assert body["label"] == "Werco Assistant"
        assert body["expires_at"] is None, "the owner's default: never expires"
        assert body["revoked"] is False and body["revoked_at"] is None and body["revoke_reason"] is None
        assert body["last_used_at"] is None
        assert body["created_by"] == admin_user.id
        assert body["created_at"].endswith("Z")
        assert len(body["jti_prefix"]) == 8
        assert "jti" not in body, "only the prefix is disclosed"

        row = db_session.get(ApiToken, body["id"])
        assert row is not None
        assert (row.company_id, row.user_id, row.created_by) == (1, test_user.id, admin_user.id)
        assert row.expires_at is None and row.revoked is False and row.last_used_at is None
        assert row.jti.startswith(body["jti_prefix"]) and len(row.jti) >= 32
        jwt_not_stored = token != row.jti and token not in row.jti
        assert jwt_not_stored, "only the jti is stored, never the JWT"

        # The JWT is an API token whose claims equal the row (the row is what auth trusts).
        claims = verify_api_token(token)
        assert claims == {"jti": row.jti, "user_id": test_user.id, "company_id": 1}
        assert verify_token(token) is None, "verify_token still rejects anything not type=access"
        assert "exp" not in jwt.get_unverified_claims(token), "no exp claim on a never-expiring token"

        # And it authenticates on its very first use.
        assert client.get(PARTS_URL, headers=_bearer(token)).status_code == 200

    def test_issue_writes_audit_rows_without_the_secret(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
    ):
        response = _issue(client, admin_headers, test_user.id, label="Grok Bot")
        assert response.status_code == 201
        body = response.json()
        row = db_session.get(ApiToken, body["id"])

        rows = db_session.query(AuditLog).order_by(AuditLog.id).all()
        create = [r for r in rows if r.resource_type == "api_token" and r.resource_id == body["id"]]
        assert len(create) == 1, "exactly one api_token audit row for the issue"
        create_row = create[0]
        assert create_row.user_id == admin_user.id, "the Admin is the actor"
        assert create_row.company_id == 1
        assert create_row.resource_identifier == "Grok Bot"
        assert create_row.new_values["user_id"] == test_user.id
        assert create_row.new_values["user_email"] == test_user.email
        assert create_row.new_values["expires_at"] is None
        assert create_row.new_values["jti_prefix"] == row.jti_prefix
        assert "token" not in create_row.new_values and "jti" not in create_row.new_values

        issued = [r for r in rows if r.action == AUTH_EVENT_ISSUED]
        assert len(issued) == 1
        assert issued[0].resource_type == "authentication"
        assert issued[0].resource_id == test_user.id, "the auth event names the token's USER"
        assert issued[0].resource_identifier == test_user.email
        assert issued[0].user_id == admin_user.id
        assert issued[0].extra_data["api_token_id"] == body["id"]
        assert issued[0].extra_data["label"] == "Grok Bot"
        assert issued[0].extra_data["jti_prefix"] == row.jti_prefix

        assert _no_secret_in_audit(db_session, body["token"], row.jti)

    def test_expires_days_sets_the_row_expiry_and_the_jwt_exp(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        before = datetime.utcnow()
        response = _issue(client, admin_headers, test_user.id, expires_days=30)
        assert response.status_code == 201, response.text
        body = response.json()
        row = db_session.get(ApiToken, body["id"])
        assert row.expires_at is not None
        assert timedelta(days=30) - timedelta(minutes=1) <= row.expires_at - before <= timedelta(days=30, minutes=1)
        assert body["expires_at"].endswith("Z")

        exp = jwt.get_unverified_claims(body["token"]).get("exp")
        assert isinstance(exp, int)
        assert exp == calendar.timegm(row.expires_at.utctimetuple()), "the JWT exp equals the row's expires_at"
        assert client.get(PARTS_URL, headers=_bearer(body["token"])).status_code == 200

        create = db_session.query(AuditLog).filter(AuditLog.resource_type == "api_token").one()
        assert create.new_values["expires_at"] and create.new_values["expires_at"].endswith("Z")

    @pytest.mark.parametrize("days,expected", [(0, 422), (-1, 422), (3651, 422), (1, 201), (3650, 201)])
    def test_expires_days_bounds_at_the_wire(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User, days: int, expected: int
    ):
        response = _issue(client, admin_headers, test_user.id, expires_days=days)
        assert response.status_code == expected, response.text
        assert db_session.query(ApiToken).count() == (1 if expected == 201 else 0)

    def test_expires_days_bounds_in_the_service_too(self, db_session: Session, admin_user: User, test_user: User):
        """The service guards the range on its own -- a caller that bypasses the schema still cannot."""
        from app.services.audit_service import AuditService

        audit = AuditService(db_session, user=admin_user, company_id=1)
        for bad in (0, MAX_API_TOKEN_EXPIRES_DAYS + 1):
            with pytest.raises(HTTPException) as excinfo:
                api_token_service.issue_api_token(
                    db_session,
                    company_id=1,
                    user_id=test_user.id,
                    label="x",
                    expires_days=bad,
                    created_by=admin_user.id,
                    audit=audit,
                )
            assert excinfo.value.status_code == 422
        assert db_session.query(ApiToken).count() == 0

    def test_the_expires_bound_has_one_source(self):
        """The service re-checks the SCHEMA's constant -- the two bounds cannot drift apart."""
        from app.schemas.api_token import MAX_API_TOKEN_EXPIRES_DAYS as schema_bound

        assert api_token_service.MAX_API_TOKEN_EXPIRES_DAYS is schema_bound
        assert schema_bound == 3650

    @pytest.mark.parametrize(
        "payload",
        [
            {"label": "no user"},
            {"user_id": 0, "label": "zero"},
            {"user_id": 1, "label": ""},
            {"user_id": 1, "label": "   "},
            {"user_id": 1, "label": "x" * 101},
            {"user_id": 1, "label": "ok", "expires_days": "soon"},
        ],
    )
    def test_issue_validation_422_writes_nothing(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User, payload: dict
    ):
        response = client.post(API_TOKENS_URL, json=payload, headers=admin_headers)
        assert response.status_code == 422, response.text
        assert db_session.query(ApiToken).count() == 0
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "api_token").count() == 0

    def test_label_is_stripped(self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User):
        response = _issue(client, admin_headers, test_user.id, label="  Padded  ")
        assert response.status_code == 201
        assert response.json()["label"] == "Padded"
        assert db_session.get(ApiToken, response.json()["id"]).label == "Padded"

    def test_manager_and_operator_cannot_issue(
        self,
        client: TestClient,
        db_session: Session,
        manager_headers: dict,
        operator_headers: dict,
        test_user: User,
    ):
        for headers in (manager_headers, operator_headers):
            response = _issue(client, headers, test_user.id)
            assert response.status_code == 403, response.text
            assert response.json()["detail"] == "Insufficient permissions"
        assert db_session.query(ApiToken).count() == 0

    def test_target_in_another_company_is_404_never_a_hint(
        self, client: TestClient, db_session: Session, admin_headers: dict
    ):
        _company, admin2, manager2 = seed_second_company(db_session)
        response = _issue(client, admin_headers, manager2.id)
        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"
        assert db_session.query(ApiToken).count() == 0

        # Symmetric: company 2's Admin cannot mint for a company-1 user either.
        one = db_session.query(User).filter(User.company_id == 1).first()
        assert one is not None
        response = _issue(client, headers_for(admin2), one.id)
        assert response.status_code == 404
        assert db_session.query(ApiToken).count() == 0

    def test_unknown_user_is_404(self, client: TestClient, db_session: Session, admin_headers: dict):
        response = _issue(client, admin_headers, 999_999)
        assert response.status_code == 404
        assert db_session.query(ApiToken).count() == 0

    def test_inactive_target_is_409(
        self, client: TestClient, db_session: Session, admin_headers: dict, inactive_user: User
    ):
        response = _issue(client, admin_headers, inactive_user.id)
        assert response.status_code == 409
        assert "disabled" in response.json()["detail"]
        assert db_session.query(ApiToken).count() == 0
        assert db_session.query(AuditLog).filter(AuditLog.action == AUTH_EVENT_ISSUED).count() == 0

    @pytest.mark.parametrize("how", ["superuser", "platform_admin"])
    def test_platform_principal_target_is_409_no_row_no_event(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User, how: str
    ):
        """A tenant Admin must never hold a standing credential that acts as a cross-tenant principal.

        ``require_role`` waves a superuser / PLATFORM_ADMIN through every gate and
        ``/platform/*`` addresses other companies by explicit id, so a token bound to
        one would be a never-expiring escalation minted from a tenant path -- the very
        thing every tenant user verb refuses to *assign*.
        """
        if how == "superuser":
            test_user.is_superuser = True
        else:
            test_user.role = UserRole.PLATFORM_ADMIN
        db_session.commit()
        assert is_platform_principal(test_user) is True

        response = _issue(client, admin_headers, test_user.id, label="escalation attempt")
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == PLATFORM_PRINCIPAL_DETAIL
        assert db_session.query(ApiToken).count() == 0
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "api_token").count() == 0
        assert db_session.query(AuditLog).filter(AuditLog.action == AUTH_EVENT_ISSUED).count() == 0

    def test_a_superuser_issuer_can_still_mint_for_an_ordinary_user(
        self, client: TestClient, db_session: Session, admin_user: User, test_user: User
    ):
        """The rule is about the TARGET: the owner's superuser account minting for the bot user is fine."""
        admin_user.is_superuser = True
        db_session.commit()
        response = _issue(client, headers_for(admin_user), test_user.id)
        assert response.status_code == 201, response.text
        assert response.json()["user_id"] == test_user.id

    def test_a_holder_promoted_after_issue_is_refused_at_use(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        """Checked at USE as well as at mint: a promotion never widens a standing token."""
        issued = _issue(client, admin_headers, test_user.id).json()
        api = _bearer(issued["token"])
        assert client.get(PARTS_URL, headers=api).status_code == 200

        test_user.is_superuser = True
        db_session.commit()
        assert check_api_token(db_session, issued["token"]) is None
        refused = client.get(PARTS_URL, headers=api)
        assert refused.status_code == 401
        assert refused.json()["detail"] == "Could not validate credentials"

        test_user.is_superuser = False
        test_user.role = UserRole.PLATFORM_ADMIN
        db_session.commit()
        assert client.get(PARTS_URL, headers=api).status_code == 401
        assert db_session.query(ApiToken).count() == 1, "refused, not revoked -- nothing wrote to the row"


# --------------------------------------------------------------------------- list


class TestList:
    def test_list_hides_secrets_and_is_tenant_scoped(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
    ):
        _company, admin2, manager2 = seed_second_company(db_session)
        mine_a = _issue(client, admin_headers, test_user.id, label="A").json()
        mine_b = _issue(client, admin_headers, admin_user.id, label="B").json()
        theirs = _issue(client, headers_for(admin2), manager2.id, label="Theirs").json()
        assert db_session.query(ApiToken).count() == 3

        listing = client.get(API_TOKENS_URL, headers=admin_headers)
        assert listing.status_code == 200
        entries = listing.json()["api_tokens"]
        assert [e["id"] for e in entries] == [mine_b["id"], mine_a["id"]], "newest first, company 1 only"
        for entry in entries:
            assert "token" not in entry and "jti" not in entry
            assert len(entry["jti_prefix"]) == 8
            row = db_session.get(ApiToken, entry["id"])
            assert row.jti.startswith(entry["jti_prefix"])
            assert entry["created_by"] == admin_user.id

        # user_id filter
        filtered = client.get(API_TOKENS_URL, params={"user_id": test_user.id}, headers=admin_headers).json()
        assert [e["id"] for e in filtered["api_tokens"]] == [mine_a["id"]]
        # a company-2 user id filter from company 1 yields nothing, not their row
        foreign = client.get(API_TOKENS_URL, params={"user_id": manager2.id}, headers=admin_headers).json()
        assert foreign["api_tokens"] == []
        assert client.get(API_TOKENS_URL, params={"user_id": 0}, headers=admin_headers).status_code == 422

        # Company 2 sees only its own.
        other = client.get(API_TOKENS_URL, headers=headers_for(admin2)).json()["api_tokens"]
        assert [e["id"] for e in other] == [theirs["id"]]

    def test_list_excludes_revoked_by_default(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        live = _issue(client, admin_headers, test_user.id, label="live").json()
        gone = _issue(client, admin_headers, test_user.id, label="gone").json()
        assert (
            client.post(_revoke_url(gone["id"]), json={"reason": "rotated"}, headers=admin_headers).status_code == 200
        )

        default = client.get(API_TOKENS_URL, headers=admin_headers).json()["api_tokens"]
        assert [e["id"] for e in default] == [live["id"]]
        everything = client.get(API_TOKENS_URL, params={"include_revoked": "true"}, headers=admin_headers).json()
        assert {e["id"]: e["revoked"] for e in everything["api_tokens"]} == {live["id"]: False, gone["id"]: True}
        revoked_entry = next(e for e in everything["api_tokens"] if e["id"] == gone["id"])
        assert revoked_entry["revoke_reason"] == "rotated" and revoked_entry["revoked_at"].endswith("Z")

    def test_non_admins_cannot_list(self, client: TestClient, manager_headers: dict, operator_headers: dict):
        for headers in (manager_headers, operator_headers):
            response = client.get(API_TOKENS_URL, headers=headers)
            assert response.status_code == 403 and response.json()["detail"] == "Insufficient permissions"


# --------------------------------------------------------------------------- revoke


class TestRevoke:
    @pytest.mark.parametrize(
        "body", [None, {}, {"reason": ""}, {"reason": "   "}, {"reason": "ab"}, {"reason": "  ab  "}]
    )
    def test_revoke_requires_a_real_reason(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User, body
    ):
        issued = _issue(client, admin_headers, test_user.id).json()
        response = client.post(_revoke_url(issued["id"]), json=body, headers=admin_headers)
        assert response.status_code == 422, response.text
        row = db_session.get(ApiToken, issued["id"])
        db_session.refresh(row)
        assert row.revoked is False and row.revoke_reason is None, "a refused revoke leaves the row untouched"
        assert client.get(PARTS_URL, headers=_bearer(issued["token"])).status_code == 200

    def test_revoke_is_one_way_audited_and_effective_on_the_next_request(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
    ):
        issued = _issue(client, admin_headers, test_user.id, label="Bot").json()
        api = _bearer(issued["token"])
        assert client.get(PARTS_URL, headers=api).status_code == 200

        response = client.post(
            _revoke_url(issued["id"]), json={"reason": "  Bot decommissioned  "}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["revoked"] is True
        assert body["revoke_reason"] == "Bot decommissioned", "stripped"
        assert body["revoked_by"] == admin_user.id
        assert body["revoked_at"].endswith("Z")
        assert "token" not in body and "jti" not in body

        row = db_session.get(ApiToken, issued["id"])
        db_session.refresh(row)
        assert row.revoked is True and row.revoked_by == admin_user.id and row.revoked_at is not None
        assert row.revoke_reason == "Bot decommissioned"

        # The holder is 401 on the very next request -- the row is re-read every call.
        refused = client.get(PARTS_URL, headers=api)
        assert refused.status_code == 401
        assert refused.json()["detail"] == "Could not validate credentials"
        assert refused.headers.get("www-authenticate") == "Bearer"

        # A second revoke refuses rather than overwriting the record.
        again = client.post(_revoke_url(issued["id"]), json={"reason": "second time"}, headers=admin_headers)
        assert again.status_code == 409
        assert again.json()["detail"] == "API token is already revoked"
        db_session.refresh(row)
        assert row.revoke_reason == "Bot decommissioned"
        assert db_session.query(ApiToken).count() == 1, "revocation is a tombstone, never a delete"

        rows = db_session.query(AuditLog).order_by(AuditLog.id).all()
        status_rows = [
            r for r in rows if r.resource_type == "api_token" and r.resource_id == issued["id"] and r.action != "CREATE"
        ]
        assert len(status_rows) == 1, "one status-change row for the one revocation (the 409 wrote none)"
        assert status_rows[0].user_id == admin_user.id
        assert status_rows[0].extra_data["reason"] == "Bot decommissioned"
        assert status_rows[0].extra_data["user_id"] == test_user.id
        assert "Bot decommissioned" in status_rows[0].description
        revoked_events = [r for r in rows if r.action == AUTH_EVENT_REVOKED]
        assert len(revoked_events) == 1
        assert revoked_events[0].resource_type == "authentication"
        assert revoked_events[0].resource_id == test_user.id
        assert revoked_events[0].extra_data["api_token_id"] == issued["id"]
        assert revoked_events[0].extra_data["reason"] == "Bot decommissioned"
        assert _no_secret_in_audit(db_session, issued["token"], row.jti)

    def test_a_second_revoker_holding_a_stale_row_gets_409_and_never_overwrites(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
    ):
        """Two revokers, the second having read the row BEFORE the first committed.

        Sessions B and C are second connections to the same shared-cache database
        (the app's own ``SessionLocal``) with the row already in their identity maps
        -- the stale-but-unexpired object the old ``if record.revoked`` check trusted,
        and the interleaving READ COMMITTED produces on Postgres. Two guards, each
        pinned: the public verb re-reads (``populate_existing``) and refuses; a
        caller that skipped the re-read cannot write either, because the flip is a
        conditional ``UPDATE``.
        """
        issued = _issue(client, admin_headers, test_user.id, label="Contended").json()
        session_b, session_c = SessionLocal(), SessionLocal()
        try:
            stale_b = session_b.get(ApiToken, issued["id"])
            stale_c = session_c.get(ApiToken, issued["id"])
            assert stale_b is not None and stale_b.revoked is False
            assert stale_c is not None and stale_c.revoked is False
            admin_b = session_b.get(User, admin_user.id)

            first = client.post(
                _revoke_url(issued["id"]), json={"reason": "A: first revocation"}, headers=admin_headers
            )
            assert first.status_code == 200, first.text

            # (1) the public verb: the stale copy is refreshed from the table, then 409.
            with pytest.raises(HTTPException) as excinfo:
                api_token_service.revoke_api_token(
                    session_b,
                    company_id=1,
                    token_id=issued["id"],
                    revoked_by=admin_user.id,
                    reason="B: second revocation",
                    audit=AuditService(session_b, admin_b),
                )
            assert excinfo.value.status_code == 409
            assert stale_b.revoked is True and stale_b.revoke_reason == "A: first revocation"

            # (2) the flip itself, on a copy nobody re-read: zero rows, and the record is untouched.
            assert stale_c.revoked is False, "still the stale in-memory copy"
            flipped = api_token_service._flip_revoked(
                session_c, stale_c, revoked_by=admin_user.id, reason="C: stale writer", now=datetime.utcnow()
            )
            assert flipped is False
            assert stale_c.revoked is True and stale_c.revoke_reason == "A: first revocation"
            assert stale_c.revoked_by == admin_user.id
        finally:
            session_b.close()
            session_c.close()

        row = db_session.get(ApiToken, issued["id"])
        db_session.refresh(row)
        assert row.revoke_reason == "A: first revocation" and row.revoked_by == admin_user.id
        rows = db_session.query(AuditLog).all()
        assert len([r for r in rows if r.action == AUTH_EVENT_REVOKED]) == 1, "the losing revokers wrote nothing"
        assert len([r for r in rows if r.resource_type == "api_token" and r.action != "CREATE"]) == 1

    def test_revoke_is_tenant_scoped_and_404_on_unknown(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        _company, admin2, _manager2 = seed_second_company(db_session)
        issued = _issue(client, admin_headers, test_user.id).json()

        foreign = client.post(_revoke_url(issued["id"]), json={"reason": "not yours"}, headers=headers_for(admin2))
        assert foreign.status_code == 404
        assert foreign.json()["detail"] == "API token not found"
        row = db_session.get(ApiToken, issued["id"])
        db_session.refresh(row)
        assert row.revoked is False
        assert client.get(PARTS_URL, headers=_bearer(issued["token"])).status_code == 200

        unknown = client.post(_revoke_url(999_999), json={"reason": "nothing"}, headers=admin_headers)
        assert unknown.status_code == 404

    def test_non_admins_cannot_revoke(
        self, client: TestClient, db_session: Session, admin_headers: dict, manager_headers: dict, test_user: User
    ):
        issued = _issue(client, admin_headers, test_user.id).json()
        response = client.post(_revoke_url(issued["id"]), json={"reason": "manager tries"}, headers=manager_headers)
        assert response.status_code == 403 and response.json()["detail"] == "Insufficient permissions"
        row = db_session.get(ApiToken, issued["id"])
        db_session.refresh(row)
        assert row.revoked is False


# --------------------------------------------------------------------------- expiry


class TestExpiry:
    def test_row_expiry_in_the_past_is_401_even_though_the_jwt_is_still_valid(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        issued = _issue(client, admin_headers, test_user.id, expires_days=2).json()
        api = _bearer(issued["token"])
        assert client.get(PARTS_URL, headers=api).status_code == 200

        row = db_session.get(ApiToken, issued["id"])
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db_session.commit()
        assert verify_api_token(issued["token"]) is not None, "the JWT's own exp is still two days out"

        refused = client.get(PARTS_URL, headers=api)
        assert refused.status_code == 401, "the ROW's expiry decides, never the JWT's"
        assert refused.headers.get("www-authenticate") == "Bearer"

    def test_jwt_exp_in_the_past_is_401(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        issued = _issue(client, admin_headers, test_user.id).json()
        row = db_session.get(ApiToken, issued["id"])
        stale = create_api_token(
            jti=row.jti, user_id=test_user.id, company_id=1, expires_at=datetime.utcnow() - timedelta(seconds=5)
        )
        assert verify_api_token(stale) is None
        assert client.get(PARTS_URL, headers=_bearer(stale)).status_code == 401

    def test_check_api_token_honours_an_injected_clock(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        issued = _issue(client, admin_headers, test_user.id, expires_days=10).json()
        row = db_session.get(ApiToken, issued["id"])
        token = issued["token"]

        live = check_api_token(db_session, token, now=row.expires_at - timedelta(seconds=1))
        assert live is not None and live[0].id == test_user.id and live[1].id == row.id
        assert check_api_token(db_session, token, now=row.expires_at) is None, "expires_at itself is expired"
        assert check_api_token(db_session, token, now=row.expires_at + timedelta(days=1)) is None
        db_session.refresh(row)
        assert row.last_used_at is None, "check_api_token is a pure read: no touch"


# --------------------------------------------------------------------------- the row is authoritative


class TestRowIsAuthoritative:
    def test_forged_claims_never_widen_tenancy(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
    ):
        seed_second_company(db_session)
        issued = _issue(client, admin_headers, test_user.id).json()
        row = db_session.get(ApiToken, issued["id"])

        forgeries = {
            "wrong company": _forge(jti=row.jti, user_id=test_user.id, company_id=2),
            "wrong user": _forge(jti=row.jti, user_id=admin_user.id, company_id=1),
            "unknown jti": _forge(jti="not-a-known-jti", user_id=test_user.id, company_id=1),
            "wrong signature": _forge(jti=row.jti, user_id=test_user.id, company_id=1, secret="x" * 64),
            "display type": _forge(jti=row.jti, user_id=test_user.id, company_id=1, token_type="display"),
        }
        for name, forged in forgeries.items():
            response = client.get(PARTS_URL, headers=_bearer(forged))
            assert response.status_code == 401, name
            assert response.json()["detail"] == "Could not validate credentials", name
            assert check_api_token(db_session, forged) is None, name
        db_session.refresh(row)
        assert row.last_used_at is None, "a refused token is never touched"
        # The genuine token still works after all of that.
        assert client.get(PARTS_URL, headers=_bearer(issued["token"])).status_code == 200

    def test_user_moved_to_another_company_is_refused(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        seed_second_company(db_session)
        issued = _issue(client, admin_headers, test_user.id).json()
        api = _bearer(issued["token"])
        assert client.get(PARTS_URL, headers=api).status_code == 200

        test_user.company_id = 2
        db_session.commit()
        assert client.get(PARTS_URL, headers=api).status_code == 401, "the row's company no longer matches the user"


# --------------------------------------------------------------------------- deactivation


class TestDeactivation:
    def test_deactivating_the_holder_revokes_every_live_token_and_reactivation_revives_nothing(
        self,
        client: TestClient,
        db_session: Session,
        admin_headers: dict,
        admin_user: User,
        test_user: User,
        operator_user: User,
    ):
        """Deactivation RETIRES a credential, it never pauses it.

        Before: ``DELETE /users/{id}`` left the tokens ``revoked = false`` (403 while
        disabled, listed as live) and ``POST /users/{id}/activate`` silently re-armed
        every one of them with no token-level trail.
        """
        first = _issue(client, admin_headers, test_user.id, label="Bot A").json()
        second = _issue(client, admin_headers, test_user.id, label="Bot B").json()
        other = _issue(client, admin_headers, operator_user.id, label="Someone else's").json()
        assert client.get(PARTS_URL, headers=_bearer(first["token"])).status_code == 200

        response = client.delete(f"{USERS_URL}{test_user.id}", headers=admin_headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"message": "User deactivated", "api_tokens_revoked": 2}

        for issued in (first, second):
            row = db_session.get(ApiToken, issued["id"])
            db_session.refresh(row)
            assert row.revoked is True and row.revoked_at is not None
            assert row.revoke_reason == DEACTIVATION_REVOKE_REASON and row.revoked_by == admin_user.id
            refused = client.get(PARTS_URL, headers=_bearer(issued["token"]))
            assert refused.status_code == 401, "revoked, not merely disabled"
        untouched = db_session.get(ApiToken, other["id"])
        db_session.refresh(untouched)
        assert untouched.revoked is False
        assert client.get(PARTS_URL, headers=_bearer(other["token"])).status_code == 200

        rows = db_session.query(AuditLog).order_by(AuditLog.id).all()
        status_rows = [r for r in rows if r.resource_type == "api_token" and r.action != "CREATE"]
        assert sorted(r.resource_id for r in status_rows) == sorted([first["id"], second["id"]])
        assert all(r.extra_data["reason"] == DEACTIVATION_REVOKE_REASON for r in status_rows)
        assert all(r.user_id == admin_user.id for r in status_rows), "actor = the deactivating Admin"
        events = [r for r in rows if r.action == AUTH_EVENT_REVOKED]
        assert len(events) == 2 and all(e.resource_id == test_user.id for e in events)
        user_rows = [r for r in rows if r.resource_type == "user" and r.resource_id == test_user.id]
        assert len(user_rows) == 1, "the user's own status-change row is still written"
        assert _no_secret_in_audit(db_session, first["token"], db_session.get(ApiToken, first["id"]).jti)

        # Reactivation restores the account, never a token.
        assert client.post(f"{USERS_URL}{test_user.id}/activate", headers=admin_headers).status_code == 200
        db_session.refresh(test_user)
        assert test_user.is_active is True
        for issued in (first, second):
            assert client.get(PARTS_URL, headers=_bearer(issued["token"])).status_code == 401
        listed = client.get(
            API_TOKENS_URL, params={"user_id": test_user.id, "include_revoked": "true"}, headers=admin_headers
        ).json()["api_tokens"]
        assert {t["id"]: t["revoked"] for t in listed} == {first["id"]: True, second["id"]: True}
        live = client.get(API_TOKENS_URL, params={"user_id": test_user.id}, headers=admin_headers).json()
        assert live["api_tokens"] == []

    def test_put_is_active_false_revokes_too_and_a_holder_without_tokens_is_still_200(
        self,
        client: TestClient,
        db_session: Session,
        admin_headers: dict,
        admin_user: User,
        test_user: User,
        operator_user: User,
    ):
        issued = _issue(client, admin_headers, test_user.id).json()
        response = client.put(
            f"{USERS_URL}{test_user.id}", json={"version": 1, "is_active": False}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is False
        row = db_session.get(ApiToken, issued["id"])
        db_session.refresh(row)
        assert row.revoked is True and row.revoke_reason == DEACTIVATION_REVOKE_REASON
        assert row.revoked_by == admin_user.id
        assert client.get(PARTS_URL, headers=_bearer(issued["token"])).status_code == 401

        # Bringing the account back through PUT revives nothing either.
        response = client.put(
            f"{USERS_URL}{test_user.id}", json={"version": 1, "is_active": True}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert client.get(PARTS_URL, headers=_bearer(issued["token"])).status_code == 401
        assert db_session.query(AuditLog).filter(AuditLog.action == AUTH_EVENT_REVOKED).count() == 1

        # A PUT that does not flip is_active revokes nothing.
        keep = _issue(client, admin_headers, test_user.id, label="kept").json()
        assert (
            client.put(
                f"{USERS_URL}{test_user.id}", json={"version": 1, "department": "Ops"}, headers=admin_headers
            ).status_code
            == 200
        )
        assert client.get(PARTS_URL, headers=_bearer(keep["token"])).status_code == 200

        # Deactivating a user who holds no token is the same verb, reporting zero.
        response = client.delete(f"{USERS_URL}{operator_user.id}", headers=admin_headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"message": "User deactivated", "api_tokens_revoked": 0}


# --------------------------------------------------------------------------- end to end


def test_api_token_lifecycle_end_to_end(
    client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
):
    """The whole lifecycle at the wire, in one sequence (issue -> use -> fence -> role -> revoke -> list)."""
    assert test_user.role.value == "manager"

    issued = _issue(client, admin_headers, test_user.id)
    assert issued.status_code == 201
    body = issued.json()
    token = body["token"]
    row = db_session.get(ApiToken, body["id"])

    api = _bearer(token)
    assert client.get(PARTS_URL, headers=api).status_code == 200
    db_session.refresh(row)
    first_touch = row.last_used_at
    assert first_touch is not None
    assert client.get(PARTS_URL, headers=api).status_code == 200
    db_session.refresh(row)
    assert row.last_used_at == first_touch, "a second call inside five minutes does not rewrite the marker"

    # The fence: every /auth and /api-tokens path through get_current_user is 403 before body validation.
    for method, path in (
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/display-token"),
        ("POST", "/api/v1/auth/display-token"),
        ("GET", API_TOKENS_URL),
        ("POST", API_TOKENS_URL),
        ("POST", _revoke_url(body["id"])),
    ):
        response = client.request(method, path, headers=api, json={} if method == "POST" else None)
        assert response.status_code == 403, (method, path, response.status_code)
        assert response.json()["detail"] == FENCE_DETAIL, (method, path)
    # POST /auth/refresh is a public route keyed on the body; an API token is not a refresh credential.
    refused = client.post("/api/v1/auth/refresh", json={"refresh_token": token}, headers=api)
    assert refused.status_code == 401 and "access_token" not in refused.json()
    # POST /auth/kiosk-badge-token reads the raw bearer as a STATION token; an API token is not one.
    badge = client.post("/api/v1/auth/kiosk-badge-token", json={"employee_id": test_user.employee_id}, headers=api)
    assert badge.status_code == 401 and "access_token" not in badge.json()

    # Role: the Manager's token is refused by RBAC on an Admin route; an Admin-user token is not.
    denied = client.get(ADMIN_ONLY_URL, headers=api)
    assert denied.status_code == 403 and denied.json()["detail"] == "Insufficient permissions"
    admin_issued = _issue(client, admin_headers, admin_user.id, label="Admin bot")
    assert admin_issued.status_code == 201
    admin_api = _bearer(admin_issued.json()["token"])
    assert client.get(ADMIN_ONLY_URL, headers=admin_api).status_code == 200
    assert client.get(API_TOKENS_URL, headers=admin_api).status_code == 403, "even an Admin-user token is fenced"

    # Revoke: 200, one-way, effective immediately.
    revoked = client.post(_revoke_url(body["id"]), json={"reason": "Bot decommissioned"}, headers=admin_headers)
    assert revoked.status_code == 200 and revoked.json()["revoked"] is True
    assert client.get(PARTS_URL, headers=api).status_code == 401
    assert client.post(_revoke_url(body["id"]), json={"reason": "second"}, headers=admin_headers).status_code == 409
    db_session.refresh(row)
    assert row.revoke_reason == "Bot decommissioned"

    # List: secrets never, revoked only on request; the Admin's interactive token still works here.
    listing = client.get(API_TOKENS_URL, headers=admin_headers)
    assert listing.status_code == 200
    entries = listing.json()["api_tokens"]
    assert [e["id"] for e in entries] == [admin_issued.json()["id"]]
    assert "token" not in entries[0] and "jti" not in entries[0]
    with_revoked = client.get(API_TOKENS_URL, params={"include_revoked": "true"}, headers=admin_headers).json()
    assert {e["id"] for e in with_revoked["api_tokens"]} == {body["id"], admin_issued.json()["id"]}

    assert _no_secret_in_audit(db_session, token, row.jti)
    assert _no_secret_in_audit(
        db_session, admin_issued.json()["token"], db_session.get(ApiToken, admin_issued.json()["id"]).jti
    )
