"""A0.5 TV wallboard: scoped display tokens + wallboard data endpoint.

Compliance assertions covered here:
- Display tokens authenticate ONLY GET /shop-floor/wallboard; they 401 on
  /shop-floor/dashboard, /work-orders, /users, and the display-token
  management endpoints themselves.
- Normal user auth is unaffected (access tokens still work everywhere,
  including the wallboard).
- Issuance / revocation are ADMIN/MANAGER-gated and audit-logged.
- Tenant isolation: a company-A display token can never read company B's
  board; management endpoints are tenant-scoped too.
- Operator names on the public wallboard are truncated to "First L.".
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_display_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.display_token import DisplayToken
from app.models.downtime import DowntimeCategory, DowntimeEvent
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerStatus
from tests.conftest import TEST_PASSWORD_HASH

DISPLAY_TOKEN_URL = "/api/v1/auth/display-token"
WALLBOARD_URL = "/api/v1/shop-floor/wallboard"


def _issue_token(client: TestClient, headers: dict, label: str = "North wall TV", **body) -> dict:
    response = client.post(DISPLAY_TOKEN_URL, json={"label": label, **body}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _display_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


# ---------------------------------------------------------------------------
# Issuance / listing / revocation (RBAC + audit)
# ---------------------------------------------------------------------------


def test_admin_can_issue_display_token(client: TestClient, admin_headers: dict, db_session: Session):
    data = _issue_token(client, admin_headers)

    assert data["label"] == "North wall TV"
    assert data["revoked"] is False
    assert data["token"]  # the one-time JWT
    # Default lifetime ~90 days
    expires_at = datetime.fromisoformat(data["expires_at"])
    delta_days = (expires_at - datetime.utcnow()).days
    assert 88 <= delta_days <= 91

    record = db_session.query(DisplayToken).filter(DisplayToken.id == data["id"]).first()
    assert record is not None
    assert record.company_id == 1
    assert record.jti  # stored revocation handle
    assert record.jti not in data["token"][:20]  # raw jti is not the token itself

    # Issuance is audit-logged on the tamper-evident chain
    audit_row = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "display_token", AuditLog.action == "CREATE")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit_row is not None
    assert audit_row.resource_id == data["id"]
    # The JWT must never land in the audit trail
    assert data["token"] not in str(audit_row.new_values)


def test_manager_can_issue_display_token(client: TestClient, manager_headers: dict):
    data = _issue_token(client, manager_headers, label="Weld bay monitor")
    assert data["label"] == "Weld bay monitor"


def test_operator_cannot_issue_display_token(client: TestClient, operator_headers: dict):
    response = client.post(DISPLAY_TOKEN_URL, json={"label": "nope"}, headers=operator_headers)
    assert response.status_code == 403


def test_expires_days_is_capped_at_365(client: TestClient, admin_headers: dict):
    response = client.post(DISPLAY_TOKEN_URL, json={"label": "TV", "expires_days": 366}, headers=admin_headers)
    assert response.status_code == 422


def test_list_display_tokens_never_returns_jwt(client: TestClient, admin_headers: dict):
    _issue_token(client, admin_headers, label="Listable TV")
    response = client.get(DISPLAY_TOKEN_URL, headers=admin_headers)
    assert response.status_code == 200
    tokens = response.json()["display_tokens"]
    assert any(t["label"] == "Listable TV" for t in tokens)
    assert all("token" not in t and "jti" not in t for t in tokens)


def test_revoke_display_token_is_audited_and_kills_access(client: TestClient, admin_headers: dict, db_session: Session):
    data = _issue_token(client, admin_headers, label="Doomed TV")
    display_headers = _display_headers(data["token"])

    # Works before revocation
    assert client.get(WALLBOARD_URL, headers=display_headers).status_code == 200

    response = client.delete(f"{DISPLAY_TOKEN_URL}/{data['id']}", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["revoked"] is True

    # Revocation is audit-logged as a status change
    audit_row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "display_token",
            AuditLog.action == "STATUS_CHANGE",
            AuditLog.resource_id == data["id"],
        )
        .first()
    )
    assert audit_row is not None

    # The TV loses access on its next poll
    assert client.get(WALLBOARD_URL, headers=display_headers).status_code == 401

    # Revoke is idempotent
    again = client.delete(f"{DISPLAY_TOKEN_URL}/{data['id']}", headers=admin_headers)
    assert again.status_code == 200
    assert again.json()["revoked"] is True


def test_revoke_is_tenant_scoped(client: TestClient, admin_headers: dict, db_session: Session, admin_user: User):
    company_b, admin_b = _make_company_b(db_session)
    token_b = _company_b_display_token(client, db_session, admin_b)

    record_b = db_session.query(DisplayToken).filter(DisplayToken.company_id == company_b.id).first()
    # Company-A admin cannot revoke company B's token
    response = client.delete(f"{DISPLAY_TOKEN_URL}/{record_b.id}", headers=admin_headers)
    assert response.status_code == 404
    # ...and B's token still works on B's board
    assert client.get(WALLBOARD_URL, headers=_display_headers(token_b)).status_code == 200


# ---------------------------------------------------------------------------
# Fencing: a display token authenticates ONLY the wallboard read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/shop-floor/dashboard"),
        ("GET", "/api/v1/work-orders/"),
        ("GET", "/api/v1/users/"),
        ("GET", "/api/v1/users/me"),
        ("GET", DISPLAY_TOKEN_URL),  # cannot manage display tokens with one
        ("POST", DISPLAY_TOKEN_URL),  # cannot mint more tokens with one
        ("POST", "/api/v1/shop-floor/clock-in"),  # cannot write labor
    ],
)
def test_display_token_rejected_everywhere_but_wallboard(
    client: TestClient, admin_headers: dict, method: str, path: str
):
    data = _issue_token(client, admin_headers)
    display_headers = _display_headers(data["token"])

    if method == "GET":
        response = client.get(path, headers=display_headers)
    else:
        response = client.post(path, json={}, headers=display_headers)
    assert response.status_code == 401, f"{method} {path} -> {response.status_code} (expected 401)"


def test_display_token_authenticates_wallboard(client: TestClient, admin_headers: dict):
    data = _issue_token(client, admin_headers)
    response = client.get(WALLBOARD_URL, headers=_display_headers(data["token"]))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"work_centers", "late_wos", "blocked_wos", "generated_at"}


def test_user_token_still_works_on_wallboard_and_dashboard(client: TestClient, auth_headers: dict):
    assert client.get(WALLBOARD_URL, headers=auth_headers).status_code == 200
    assert client.get("/api/v1/shop-floor/dashboard", headers=auth_headers).status_code == 200


def test_expired_display_token_rejected_even_if_jwt_still_valid(
    client: TestClient, admin_headers: dict, db_session: Session
):
    data = _issue_token(client, admin_headers, label="Expiring TV")
    # Age the DB row past expiry — the JWT's own exp is still in the future,
    # proving the dependency checks the authoritative DB expires_at.
    record = db_session.query(DisplayToken).filter(DisplayToken.id == data["id"]).first()
    record.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()

    assert client.get(WALLBOARD_URL, headers=_display_headers(data["token"])).status_code == 401


def test_forged_display_jwt_without_db_row_rejected(client: TestClient, db_session: Session):
    # Correctly signed display JWT whose jti has no display_tokens row.
    token = create_display_token(
        jti="no-such-jti", company_id=1, label="forged", expires_at=datetime.utcnow() + timedelta(days=1)
    )
    assert client.get(WALLBOARD_URL, headers=_display_headers(token)).status_code == 401


def test_display_jwt_company_claim_must_match_db_row(client: TestClient, admin_headers: dict, db_session: Session):
    data = _issue_token(client, admin_headers, label="Claim mismatch TV")
    record = db_session.query(DisplayToken).filter(DisplayToken.id == data["id"]).first()
    # Re-mint a signed JWT for the same jti but claiming a different company.
    forged = create_display_token(
        jti=record.jti, company_id=999, label=record.label, expires_at=datetime.utcnow() + timedelta(days=1)
    )
    assert client.get(WALLBOARD_URL, headers=_display_headers(forged)).status_code == 401


# ---------------------------------------------------------------------------
# Tenant isolation of the board itself
# ---------------------------------------------------------------------------


def _make_company_b(db_session: Session):
    company_b = db_session.query(Company).filter(Company.slug == "other-co").first()
    if not company_b:
        company_b = Company(name="Other Co", slug="other-co", is_active=True)
        db_session.add(company_b)
        db_session.flush()
    admin_b = User(
        email="admin-b@other.co",
        employee_id="EMP-B-ADMIN",
        first_name="Bea",
        last_name="Boss",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.ADMIN,
        is_active=True,
        company_id=company_b.id,
    )
    db_session.add(admin_b)
    db_session.commit()
    db_session.refresh(admin_b)
    return company_b, admin_b


def _company_b_display_token(client: TestClient, db_session: Session, admin_b: User) -> str:
    from app.core.security import create_access_token

    headers_b = {
        "Authorization": f"Bearer {create_access_token(subject=admin_b.id, company_id=admin_b.company_id)}",
        "X-Requested-With": "XMLHttpRequest",
    }
    return _issue_token(client, headers_b, label="B-side TV")["token"]


def test_wallboard_tenant_isolation(
    client: TestClient, admin_headers: dict, db_session: Session, test_work_center: WorkCenter
):
    company_b, admin_b = _make_company_b(db_session)
    wc_b = WorkCenter(
        name="B Lathe",
        code="B-LATHE",
        work_center_type="machining",
        is_active=True,
        company_id=company_b.id,
    )
    db_session.add(wc_b)
    db_session.commit()

    # Company-A display token sees only company A's work centers
    token_a = _issue_token(client, admin_headers)["token"]
    payload_a = client.get(WALLBOARD_URL, headers=_display_headers(token_a)).json()
    names_a = {wc["name"] for wc in payload_a["work_centers"]}
    assert test_work_center.name in names_a
    assert "B Lathe" not in names_a

    # Company-B display token sees only B's
    token_b = _company_b_display_token(client, db_session, admin_b)
    payload_b = client.get(WALLBOARD_URL, headers=_display_headers(token_b)).json()
    names_b = {wc["name"] for wc in payload_b["work_centers"]}
    assert names_b == {"B Lathe"}


# ---------------------------------------------------------------------------
# Payload shape / contents
# ---------------------------------------------------------------------------


def test_wallboard_payload_contents(
    client: TestClient,
    admin_headers: dict,
    db_session: Session,
    test_work_center: WorkCenter,
    test_work_order: WorkOrder,
    operator_user: User,
):
    # Make the WO late + in progress, its op READY (queued), with one live job.
    test_work_order.status = WorkOrderStatus.IN_PROGRESS
    test_work_order.due_date = date.today() - timedelta(days=3)
    operation = test_work_order.operations[0]
    operation.status = OperationStatus.READY
    operation.quantity_complete = 4

    db_session.add(
        TimeEntry(
            user_id=operator_user.id,
            work_order_id=test_work_order.id,
            operation_id=operation.id,
            work_center_id=test_work_center.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow() - timedelta(minutes=42),
            company_id=1,
        )
    )
    db_session.add(
        WorkOrderBlocker(
            work_order_id=test_work_order.id,
            operation_id=operation.id,
            category=WorkOrderBlockerCategory.MATERIAL_MISSING.value,
            status=WorkOrderBlockerStatus.OPEN.value,
            title="Out of 4140 bar stock",
            reported_by=operator_user.id,
            reported_at=datetime.utcnow() - timedelta(hours=2),
            company_id=1,
        )
    )
    db_session.add(
        DowntimeEvent(
            work_center_id=test_work_center.id,
            start_time=datetime.utcnow() - timedelta(minutes=15),
            category=DowntimeCategory.MECHANICAL,
            reported_by=operator_user.id,
            company_id=1,
        )
    )
    db_session.commit()

    payload = client.get(WALLBOARD_URL, headers=admin_headers).json()

    wc = next(w for w in payload["work_centers"] if w["id"] == test_work_center.id)
    assert wc["queued_count"] == 1
    assert wc["blocked_count"] == 1
    assert wc["down"] is not None and wc["down"]["category"] == "mechanical"
    assert wc["down"]["minutes"] >= 14

    job = wc["active_jobs"][0]
    assert job["wo_number"] == test_work_order.work_order_number
    assert job["op_name"] == "Test Operation"
    # PRIVACY: public screen shows first name + last initial only
    assert job["operator_name"] == "Operator U."
    assert "Operator User" not in str(payload)
    assert job["elapsed_minutes"] >= 41
    assert job["qty_done"] == 4.0
    assert job["qty_target"] == float(test_work_order.quantity_ordered)

    late = next(w for w in payload["late_wos"] if w["wo_number"] == test_work_order.work_order_number)
    assert late["days_late"] == 3
    assert late["part_number"]

    blocked = next(b for b in payload["blocked_wos"] if b["wo_number"] == test_work_order.work_order_number)
    assert blocked["category"] == "material_missing"
    assert 1.5 <= blocked["age_hours"] <= 2.5


def test_wallboard_dept_filter(client: TestClient, admin_headers: dict, db_session: Session):
    db_session.add_all(
        [
            WorkCenter(name="Mill 1", code="MILL-1", work_center_type="machining", is_active=True, company_id=1),
            WorkCenter(name="Weld 1", code="WELD-1", work_center_type="welding", is_active=True, company_id=1),
        ]
    )
    db_session.commit()

    payload = client.get(f"{WALLBOARD_URL}?dept=Machining", headers=admin_headers).json()
    names = {wc["name"] for wc in payload["work_centers"]}
    assert "Mill 1" in names
    assert "Weld 1" not in names
