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

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_display_token, get_current_user_from_token, verify_token
from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.display_token import DisplayToken
from app.models.downtime import DowntimeCategory, DowntimeEvent
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quality import NCRSource, NCRStatus, NonConformanceReport
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerStatus
from app.services.wallboard_service import central_day_window_utc
from tests.conftest import TEST_PASSWORD_HASH
from tests.lean_phase1_helpers import (
    headers_for,
    make_entry,
    make_op,
    make_part,
    make_shipment,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

DISPLAY_TOKEN_URL = "/api/v1/auth/display-token"
WALLBOARD_URL = "/api/v1/shop-floor/wallboard"


@pytest.fixture(autouse=True)
def _reset_kpi_strip_cache():
    """The Lean Phase 1 kpi_strip rides the wallboard payload behind a module-level
    per-company TTL cache (~5 min) that outlives a test's dropped tables; reset it
    around every test so no assertion ever sees another test's cached strip."""
    from app.services.wallboard_service import reset_kpi_strip_cache

    reset_kpi_strip_cache()
    yield
    reset_kpi_strip_cache()


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
    # expires_at is now UTC-aware (API emits a trailing 'Z'); compare aware-to-aware.
    delta_days = (expires_at - datetime.now(timezone.utc)).days
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


async def test_display_token_cannot_authenticate_websocket(client: TestClient, admin_headers: dict):
    """WS fencing: a display JWT must never pass the WebSocket auth path.

    WebSocket auth goes through ``get_current_user_from_token`` →
    ``verify_token``, which only accepts ``type == "access"`` JWTs. A display
    token (``type == "display"``) must be rejected at the JWT-type check,
    before any DB lookup happens.
    """
    data = _issue_token(client, admin_headers, label="WS fence TV")

    # The payload check the WS auth path relies on rejects the display JWT...
    assert verify_token(data["token"]) is None

    # ...so the async WS auth helper refuses it outright. Rejection happens
    # before the session is touched, hence db=None is safe (and proves it).
    with pytest.raises(Exception, match="Could not validate credentials"):
        await get_current_user_from_token(data["token"], db=None)  # type: ignore[arg-type]


def test_display_token_authenticates_wallboard(client: TestClient, admin_headers: dict):
    data = _issue_token(client, admin_headers)
    response = client.get(WALLBOARD_URL, headers=_display_headers(data["token"]))
    assert response.status_code == 200
    payload = response.json()
    # Lean Phase 1 added kpi_strip; the TV redesign added the true totals and
    # the ship/today/quality blocks — all on the same single payload.
    assert set(payload.keys()) == {
        "work_centers",
        "late_wos",
        "blocked_wos",
        "kpi_strip",
        "late_total",
        "blocked_total",
        "down_total",
        "ship",
        "today",
        "quality",
        "generated_at",
    }


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


def test_new_blocks_are_tenant_isolated(client: TestClient, admin_headers: dict, db_session: Session):
    """A regression that drops a company_id filter from ANY new-block query
    (ship/today/quality/totals) must fail here: company B gets exceptions and
    activity in every block; company A's board must stay all-zero."""
    company_b, admin_b = _make_company_b(db_session)
    b = company_b.id
    central_today = datetime.now(CENTRAL_TIME_ZONE).date()
    _, now_utc = central_day_window_utc()

    part_b = make_part(db_session, company_id=b)
    wc_b = make_work_center(db_session, company_id=b)
    # Late + promised-today + on-hold WOs, downtime, blocker, NCR, receipt,
    # open + completed labor — one of everything the new blocks count.
    late_b = make_wo(db_session, part_b, company_id=b, due_date=central_today - timedelta(days=4))
    make_wo(db_session, part_b, company_id=b, due_date=central_today)  # ship due today
    make_wo(db_session, part_b, company_id=b, status_=WorkOrderStatus.ON_HOLD)
    make_wo(
        db_session, part_b, company_id=b, status_=WorkOrderStatus.COMPLETE, actual_end=now_utc
    )  # wos_completed today
    op_b = make_op(db_session, late_b, wc_b, company_id=b, status_=OperationStatus.COMPLETE)
    op_b.actual_end = now_utc  # ops_completed today
    db_session.add(
        DowntimeEvent(
            company_id=b, work_center_id=wc_b.id, start_time=now_utc - timedelta(hours=1), reported_by=admin_b.id
        )
    )
    db_session.add(
        WorkOrderBlocker(
            company_id=b,
            work_order_id=late_b.id,
            operation_id=op_b.id,
            category=WorkOrderBlockerCategory.MATERIAL_MISSING.value,
            status=WorkOrderBlockerStatus.OPEN.value,
            title="B blocker",
            reported_by=admin_b.id,
        )
    )
    db_session.add(
        NonConformanceReport(
            company_id=b,
            ncr_number="NCR-B-1",
            source=NCRSource.IN_PROCESS,
            status=NCRStatus.OPEN,
            title="B ncr",
            description="B only",
        )
    )
    make_entry(db_session, admin_b, late_b, None, wc_b, company_id=b, open_entry=True, quantity_produced=9)
    db_session.commit()

    # Company B sees its own exceptions...
    token_b = _company_b_display_token(client, db_session, admin_b)
    payload_b = client.get(WALLBOARD_URL, headers=_display_headers(token_b)).json()
    assert payload_b["late_total"] == 1
    assert payload_b["blocked_total"] == 1
    assert payload_b["down_total"] == 1
    assert payload_b["quality"] == {"open_ncr_count": 1, "newest_ncr_age_days": 0, "wos_on_hold": 1}
    assert payload_b["ship"]["due_today"] >= 1
    assert payload_b["today"]["operators_on_clock"] == 1

    # ...and company A's board stays all-zero across every new block.
    token_a = _issue_token(client, admin_headers)["token"]
    payload_a = client.get(WALLBOARD_URL, headers=_display_headers(token_a)).json()
    assert payload_a["late_total"] == 0
    assert payload_a["blocked_total"] == 0
    assert payload_a["down_total"] == 0
    assert payload_a["late_wos"] == []
    assert payload_a["blocked_wos"] == []
    assert payload_a["ship"]["due_today"] == 0
    assert payload_a["ship"]["shipped_today"] == 0
    assert payload_a["ship"]["due_this_week"] == 0
    assert payload_a["quality"] == {"open_ncr_count": 0, "newest_ncr_age_days": None, "wos_on_hold": 0}
    today_a = payload_a["today"]
    assert today_a["ops_completed"] == 0
    assert today_a["pieces_completed"] == 0
    assert today_a["wos_completed"] == 0
    assert today_a["operators_on_clock"] == 0
    assert today_a["receipts"] == 0
    assert today_a["scrap_events"] == 0
    assert today_a["hours_logged"] == 0


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
    # Lateness is judged against the CENTRAL calendar day, so seed from it.
    central_today = datetime.now(CENTRAL_TIME_ZONE).date()
    test_work_order.status = WorkOrderStatus.IN_PROGRESS
    test_work_order.due_date = central_today - timedelta(days=3)
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
    assert job["crew"] == ["Operator U."]
    assert job["crew_count"] == 1
    assert job["is_late"] is True  # promise (due_date) is 3 days past
    assert job["elapsed_minutes"] >= 41
    assert job["qty_done"] == 4.0
    assert job["qty_target"] == float(test_work_order.quantity_ordered)

    late = next(w for w in payload["late_wos"] if w["wo_number"] == test_work_order.work_order_number)
    assert late["days_late"] == 3
    assert late["part_number"]

    blocked = next(b for b in payload["blocked_wos"] if b["wo_number"] == test_work_order.work_order_number)
    assert blocked["category"] == "material_missing"
    assert 1.5 <= blocked["age_hours"] <= 2.5


def test_soft_deleted_wo_excluded_from_active_queued_counts(
    client: TestClient,
    admin_headers: dict,
    db_session: Session,
    test_work_center: WorkCenter,
    test_part,
    admin_user: User,
):
    """Soft-deleted WOs must not inflate active/queued counts on the wallboard
    OR the dashboard (both go through operation_counts_by_work_center)."""
    wo = WorkOrder(
        work_order_number="WO-SOFT-DEL",
        part_id=test_part.id,
        quantity_ordered=5,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        company_id=1,
    )
    db_session.add(wo)
    db_session.flush()
    db_session.add_all(
        [
            WorkOrderOperation(
                work_order_id=wo.id,
                work_center_id=test_work_center.id,
                sequence=10,
                name="Ghost ready op",
                status=OperationStatus.READY,
                company_id=1,
            ),
            WorkOrderOperation(
                work_order_id=wo.id,
                work_center_id=test_work_center.id,
                sequence=20,
                name="Ghost active op",
                status=OperationStatus.IN_PROGRESS,
                company_id=1,
            ),
        ]
    )
    wo.soft_delete(admin_user.id)
    db_session.commit()

    payload = client.get(WALLBOARD_URL, headers=admin_headers).json()
    wc = next(w for w in payload["work_centers"] if w["id"] == test_work_center.id)
    assert wc["queued_count"] == 0

    dashboard = client.get("/api/v1/shop-floor/dashboard", headers=admin_headers).json()
    center = next(item for item in dashboard["work_centers"] if item["id"] == test_work_center.id)
    assert center["queued_operations"] == 0
    assert center["active_operations"] == 0


def test_blocked_count_ignores_deleted_and_terminal_work_orders(
    client: TestClient,
    admin_headers: dict,
    db_session: Session,
    test_work_center: WorkCenter,
    test_part,
    admin_user: User,
    operator_user: User,
):
    """Per-WC blocked_count and the blocked_wos ticker must agree: blockers on
    soft-deleted or terminal (COMPLETE/CLOSED/CANCELLED) WOs are off the board."""

    def make_blocked_wo(number: str, wo_status: WorkOrderStatus, soft_deleted: bool = False) -> WorkOrder:
        wo = WorkOrder(
            work_order_number=number,
            part_id=test_part.id,
            quantity_ordered=1,
            status=wo_status,
            priority=5,
            company_id=1,
        )
        db_session.add(wo)
        db_session.flush()
        op = WorkOrderOperation(
            work_order_id=wo.id,
            work_center_id=test_work_center.id,
            sequence=10,
            name=f"Blocked op {number}",
            company_id=1,
        )
        db_session.add(op)
        db_session.flush()
        db_session.add(
            WorkOrderBlocker(
                work_order_id=wo.id,
                operation_id=op.id,
                category=WorkOrderBlockerCategory.MATERIAL_MISSING.value,
                status=WorkOrderBlockerStatus.OPEN.value,
                title=f"Blocker on {number}",
                reported_by=operator_user.id,
                reported_at=datetime.utcnow(),
                company_id=1,
            )
        )
        if soft_deleted:
            wo.soft_delete(admin_user.id)
        return wo

    make_blocked_wo("WO-BLK-LIVE", WorkOrderStatus.IN_PROGRESS)
    make_blocked_wo("WO-BLK-DEL", WorkOrderStatus.IN_PROGRESS, soft_deleted=True)
    make_blocked_wo("WO-BLK-DONE", WorkOrderStatus.COMPLETE)
    make_blocked_wo("WO-BLK-CXL", WorkOrderStatus.CANCELLED)
    db_session.commit()

    payload = client.get(WALLBOARD_URL, headers=admin_headers).json()
    wc = next(w for w in payload["work_centers"] if w["id"] == test_work_center.id)
    assert wc["blocked_count"] == 1
    # The ticker applies the same exclusions, so the two cannot disagree.
    assert {b["wo_number"] for b in payload["blocked_wos"]} == {"WO-BLK-LIVE"}


def test_open_break_and_downtime_entries_are_not_wallboard_jobs(
    client: TestClient,
    admin_headers: dict,
    db_session: Session,
    test_work_center: WorkCenter,
    test_work_order: WorkOrder,
    operator_user: User,
):
    """Open BREAK/DOWNTIME time entries are clocked time, not jobs — they must
    not render ghost job rows on the TV. Only SETUP/RUN/REWORK/INSPECTION
    entries with an operation count as active jobs."""
    test_work_order.status = WorkOrderStatus.IN_PROGRESS
    operation = test_work_order.operations[0]
    db_session.add_all(
        [
            # Real labor — the only row that may appear on the TV.
            TimeEntry(
                user_id=operator_user.id,
                work_order_id=test_work_order.id,
                operation_id=operation.id,
                work_center_id=test_work_center.id,
                entry_type=TimeEntryType.RUN,
                clock_in=datetime.utcnow() - timedelta(minutes=10),
                company_id=1,
            ),
            # Open break — no operation, non-labor type.
            TimeEntry(
                user_id=operator_user.id,
                work_center_id=test_work_center.id,
                entry_type=TimeEntryType.BREAK,
                clock_in=datetime.utcnow() - timedelta(minutes=5),
                company_id=1,
            ),
            # Open downtime attached to the operation — still not labor.
            TimeEntry(
                user_id=operator_user.id,
                work_order_id=test_work_order.id,
                operation_id=operation.id,
                work_center_id=test_work_center.id,
                entry_type=TimeEntryType.DOWNTIME,
                clock_in=datetime.utcnow() - timedelta(minutes=5),
                company_id=1,
            ),
        ]
    )
    db_session.commit()

    payload = client.get(WALLBOARD_URL, headers=admin_headers).json()
    wc = next(w for w in payload["work_centers"] if w["id"] == test_work_center.id)
    assert len(wc["active_jobs"]) == 1
    assert wc["active_jobs"][0]["wo_number"] == test_work_order.work_order_number


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


# ---------------------------------------------------------------------------
# TV redesign payload: crew grouping, promise-based lateness, dept-scoped
# rails + totals, and the ship / today / quality blocks
# ---------------------------------------------------------------------------


def _payload(client: TestClient, headers: dict, dept: "str | None" = None) -> dict:
    url = f"{WALLBOARD_URL}?dept={dept}" if dept else WALLBOARD_URL
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_crew_grouping_one_row_per_operation(client: TestClient, db_session: Session):
    """Several operators clocked into ONE operation are one job row: crew in
    clock-in order capped at 3 names, crew_count = true headcount, elapsed
    from the EARLIEST clock_in, operator_name = crew[0] for back-compat."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=20)
    op = make_op(db_session, wo, wc, status_=OperationStatus.IN_PROGRESS, quantity_complete=5)

    alice = make_user(db_session, role=UserRole.OPERATOR, first_name="Alice", last_name="Anders")
    bob = make_user(db_session, role=UserRole.OPERATOR, first_name="Bob", last_name="Baker")
    cara = make_user(db_session, role=UserRole.OPERATOR, first_name="Cara", last_name="Cole")
    dave = make_user(db_session, role=UserRole.OPERATOR, first_name="Dave", last_name="Diaz")

    now = datetime.utcnow()
    for user, minutes_ago in ((alice, 50), (bob, 30), (cara, 10), (dave, 5)):
        make_entry(db_session, user, wo, op, wc, open_entry=True, clock_in=now - timedelta(minutes=minutes_ago))

    payload = _payload(client, headers_for(viewer))
    card = next(w for w in payload["work_centers"] if w["id"] == wc.id)
    assert len(card["active_jobs"]) == 1  # ONE row per operation, not four
    job = card["active_jobs"][0]
    assert job["crew"] == ["Alice A.", "Bob B.", "Cara C."]  # clock-in order, capped at 3
    assert job["crew_count"] == 4  # true headcount rides separately for the "+N"
    assert job["operator_name"] == "Alice A."  # back-compat alias of crew[0]
    assert 49 <= job["elapsed_minutes"] <= 52  # EARLIEST clock_in drives elapsed


def test_is_late_uses_promise_precedence_and_central_today(client: TestClient, db_session: Session):
    """Lateness = coalesce(must_ship_by, due_date) < Central today; the per-job
    flag and the late rail share the predicate so they cannot disagree."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    central_today = datetime.now(CENTRAL_TIME_ZONE).date()

    # Past due_date but a future must_ship_by: the promise is NOT late.
    saved = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        due_date=central_today - timedelta(days=4),
        must_ship_by=central_today + timedelta(days=1),
    )
    saved_op = make_op(db_session, saved, wc, status_=OperationStatus.IN_PROGRESS)

    # must_ship_by in the past trumps a comfortable due_date: LATE.
    late = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        due_date=central_today + timedelta(days=7),
        must_ship_by=central_today - timedelta(days=2),
    )
    late_op = make_op(db_session, late, wc, sequence=20, status_=OperationStatus.IN_PROGRESS)

    operator = make_user(db_session, role=UserRole.OPERATOR, first_name="Olga", last_name="Ops")
    make_entry(db_session, operator, saved, saved_op, wc, open_entry=True)
    make_entry(db_session, operator, late, late_op, wc, open_entry=True)

    payload = _payload(client, headers_for(viewer))
    card = next(w for w in payload["work_centers"] if w["id"] == wc.id)
    late_by_wo = {job["wo_number"]: job["is_late"] for job in card["active_jobs"]}
    assert late_by_wo[saved.work_order_number] is False
    assert late_by_wo[late.work_order_number] is True

    late_numbers = {row["wo_number"] for row in payload["late_wos"]}
    assert late.work_order_number in late_numbers
    assert saved.work_order_number not in late_numbers
    late_row = next(row for row in payload["late_wos"] if row["wo_number"] == late.work_order_number)
    assert late_row["days_late"] == 2
    assert late_row["due_date"] == (central_today - timedelta(days=2)).isoformat()  # the PROMISE date
    assert payload["late_total"] == 1


def test_dept_scoping_of_rails_and_totals(client: TestClient, db_session: Session):
    """?dept= scopes the late/blocked rails AND the late/blocked/down totals:
    a WO spanning two depts appears on both; a late WO with no open routed
    ops appears only on the unfiltered board; blockers attribute via their
    operation's work center; down via the work center itself."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    mill = make_work_center(db_session)  # machining
    weld = make_work_center(db_session, work_center_type="welding")
    central_today = datetime.now(CENTRAL_TIME_ZONE).date()

    # Late WO with open ops in BOTH depts -> on both dept boards.
    spanning = make_wo(
        db_session, part, status_=WorkOrderStatus.IN_PROGRESS, due_date=central_today - timedelta(days=1)
    )
    span_mill_op = make_op(db_session, spanning, mill, status_=OperationStatus.READY)
    make_op(db_session, spanning, weld, sequence=20, status_=OperationStatus.IN_PROGRESS)

    # Late WO whose only op is COMPLETE: no open routing -> unfiltered board only.
    unrouted = make_wo(
        db_session, part, status_=WorkOrderStatus.IN_PROGRESS, due_date=central_today - timedelta(days=5)
    )
    make_op(db_session, unrouted, mill, status_=OperationStatus.COMPLETE)

    db_session.add(
        WorkOrderBlocker(
            work_order_id=spanning.id,
            operation_id=span_mill_op.id,  # blocker lives at the MACHINING op
            category=WorkOrderBlockerCategory.TOOLING_MISSING.value,
            status=WorkOrderBlockerStatus.OPEN.value,
            title="No fixture",
            reported_at=datetime.utcnow() - timedelta(hours=1),
            company_id=1,
        )
    )
    db_session.add(
        DowntimeEvent(
            work_center_id=weld.id,  # open downtime on the WELDING work center
            start_time=datetime.utcnow() - timedelta(minutes=20),
            category=DowntimeCategory.MECHANICAL,
            reported_by=viewer.id,
            company_id=1,
        )
    )
    db_session.commit()

    unfiltered = _payload(client, headers_for(viewer))
    assert {w["wo_number"] for w in unfiltered["late_wos"]} == {
        spanning.work_order_number,
        unrouted.work_order_number,
    }
    assert unfiltered["late_total"] == 2
    assert unfiltered["blocked_total"] == 1
    assert unfiltered["down_total"] == 1
    # Worst-first ranking: 5 days late outranks 1 day late.
    assert unfiltered["late_wos"][0]["wo_number"] == unrouted.work_order_number

    machining = _payload(client, headers_for(viewer), dept="machining")
    assert {w["wo_number"] for w in machining["late_wos"]} == {spanning.work_order_number}
    assert machining["late_total"] == 1
    assert {b["wo_number"] for b in machining["blocked_wos"]} == {spanning.work_order_number}
    assert machining["blocked_total"] == 1
    assert machining["down_total"] == 0

    welding = _payload(client, headers_for(viewer), dept="Welding")  # case-insensitive
    assert {w["wo_number"] for w in welding["late_wos"]} == {spanning.work_order_number}
    assert welding["late_total"] == 1
    assert welding["blocked_wos"] == []
    assert welding["blocked_total"] == 0
    assert welding["down_total"] == 1


def test_ship_block_happy_path(client: TestClient, db_session: Session):
    """Ship panel: promise = must_ship_by || due_date, Central-day window,
    'fully shipped' via the cumulative-crossing rule, rows ranked by
    qty_remaining, no next_due when something IS due today."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    central_today = datetime.now(CENTRAL_TIME_ZONE).date()

    # Promised today (must_ship_by leg), 4 of 10 shipped -> due today, 6 remaining.
    wo_a = make_wo(db_session, part, must_ship_by=central_today, quantity_ordered=10)
    make_shipment(db_session, wo_a, ship_date=central_today - timedelta(days=1), quantity_shipped=4)
    # Promised today (due_date leg), nothing shipped -> due today, 5 remaining.
    wo_b = make_wo(db_session, part, due_date=central_today, quantity_ordered=5)
    # Promised today AND fully shipped -> in the due_today denominator, counts
    # as shipped_today, but renders no open row.
    wo_c = make_wo(db_session, part, due_date=central_today, quantity_ordered=3)
    make_shipment(db_session, wo_c, ship_date=central_today, quantity_shipped=3)
    # Promised in 3 days -> due_this_week only.
    make_wo(db_session, part, due_date=central_today + timedelta(days=3), quantity_ordered=2)
    # Promised beyond the 7-day window -> not on the panel at all.
    make_wo(db_session, part, due_date=central_today + timedelta(days=10), quantity_ordered=2)

    ship = _payload(client, headers_for(viewer))["ship"]
    # One population (WOs promised today): 3 promised, 1 of them fully shipped
    # -> the TV fraction reads "1 / 3" with "2 TO GO".
    assert ship["due_today"] == 3
    assert ship["shipped_today"] == 1
    assert ship["due_this_week"] == 3
    assert [(row["wo_number"], row["qty_remaining"]) for row in ship["due_today_rows"]] == [
        (wo_a.work_order_number, 6.0),  # largest remaining first
        (wo_b.work_order_number, 5.0),
    ]
    assert ship["due_today_rows"][0]["promise_date"] == central_today.isoformat()
    assert ship["next_due_date"] is None
    assert ship["next_due_count"] == 0


def test_ship_block_next_due_when_nothing_due_today(client: TestClient, db_session: Session):
    viewer = make_user(db_session)
    part = make_part(db_session)
    central_today = datetime.now(CENTRAL_TIME_ZONE).date()

    make_wo(db_session, part, must_ship_by=central_today + timedelta(days=2), quantity_ordered=4)
    make_wo(db_session, part, due_date=central_today + timedelta(days=2), quantity_ordered=1)
    make_wo(db_session, part, due_date=central_today + timedelta(days=5), quantity_ordered=6)
    # Fully shipped WO promised sooner must NOT be the "next due".
    done = make_wo(db_session, part, due_date=central_today + timedelta(days=1), quantity_ordered=2)
    make_shipment(db_session, done, ship_date=central_today, quantity_shipped=2)

    ship = _payload(client, headers_for(viewer))["ship"]
    assert ship["due_today"] == 0
    assert ship["due_today_rows"] == []
    assert ship["next_due_date"] == (central_today + timedelta(days=2)).isoformat()
    assert ship["next_due_count"] == 2
    assert ship["due_this_week"] == 3


def test_today_block_counts_central_day_activity(client: TestClient, db_session: Session):
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    day_start_utc, now_utc = central_day_window_utc()
    # Clock-ins guaranteed inside the live Central day even right after midnight.
    recent = now_utc - min(timedelta(minutes=30), (now_utc - day_start_utc) / 2)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=50)
    make_op(db_session, wo, wc, status_=OperationStatus.COMPLETE, actual_end=now_utc)  # ops_completed

    runner = make_user(db_session, role=UserRole.OPERATOR, first_name="Runa", last_name="Runner")
    idler = make_user(db_session, role=UserRole.OPERATOR, first_name="Ida", last_name="Idle")

    # Closed RUN entry: 7 pieces, 1.5 h.
    make_entry(db_session, runner, wo, None, wc, clock_in=recent, duration_hours=1.5, quantity_produced=7)
    # Backfill/import provenance: excluded from pieces, scrap AND hours.
    make_entry(
        db_session,
        runner,
        wo,
        None,
        wc,
        clock_in=recent,
        duration_hours=1.0,
        quantity_produced=100,
        quantity_scrapped=5,
        source="import",
    )
    # Live scrap event (closed).
    make_entry(db_session, runner, wo, None, wc, clock_in=recent, duration_hours=0.5, quantity_scrapped=2)
    # Open entry -> operators_on_clock + open elapsed hours.
    make_entry(db_session, idler, wo, None, wc, open_entry=True, clock_in=recent)

    # A WO completed today.
    make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE, actual_end=now_utc)

    # One PO receipt received today.
    vendor = Vendor(code="V-TODAY", name="Today Vendor", company_id=1)
    db_session.add(vendor)
    db_session.flush()
    po = PurchaseOrder(po_number="PO-TODAY", vendor_id=vendor.id, company_id=1)
    db_session.add(po)
    db_session.flush()
    line = PurchaseOrderLine(
        purchase_order_id=po.id, line_number=1, part_id=part.id, quantity_ordered=10, unit_price=1.0, company_id=1
    )
    db_session.add(line)
    db_session.flush()
    db_session.add(
        POReceipt(
            receipt_number="RCPT-TODAY",
            po_line_id=line.id,
            quantity_received=10,
            lot_number="LOT-1",
            received_by=viewer.id,
            received_at=now_utc,
            company_id=1,
        )
    )
    db_session.commit()

    today = _payload(client, headers_for(viewer))["today"]
    assert today["ops_completed"] == 1
    assert today["pieces_completed"] == 7  # the 100-piece import row is provenance-excluded
    assert today["wos_completed"] == 1
    assert today["operators_on_clock"] == 1  # only the open entry's operator
    assert today["receipts"] == 1
    assert today["scrap_events"] == 1  # import-sourced scrap is excluded
    # 1.5 + 0.5 closed + the open entry's elapsed time — the 1.0h import row
    # is provenance-excluded like pieces/scrap (backfill must not inflate the TV).
    open_elapsed_hours = (now_utc - recent).total_seconds() / 3600.0
    assert today["hours_logged"] == pytest.approx(2.0 + open_elapsed_hours, abs=0.2)


def test_quality_block_counts_and_age_only(client: TestClient, db_session: Session):
    viewer = make_user(db_session)
    part = make_part(db_session)
    now = datetime.utcnow()

    db_session.add_all(
        [
            NonConformanceReport(
                ncr_number="NCR-OLD",
                title="TITLE-MUST-NOT-LEAK",
                description="DESC-MUST-NOT-LEAK",
                source=NCRSource.IN_PROCESS,
                status=NCRStatus.OPEN,
                company_id=1,
                created_at=now - timedelta(days=6),
            ),
            NonConformanceReport(
                ncr_number="NCR-NEW",
                title="Newest one",
                description="Newest desc",
                source=NCRSource.FINAL_INSPECTION,
                status=NCRStatus.UNDER_REVIEW,
                company_id=1,
                created_at=now - timedelta(days=1),
            ),
            NonConformanceReport(
                ncr_number="NCR-CLOSED",
                title="Closed one",
                description="Closed desc",
                source=NCRSource.IN_PROCESS,
                status=NCRStatus.CLOSED,
                company_id=1,
                created_at=now - timedelta(days=3),
            ),
        ]
    )
    make_wo(db_session, part, status_=WorkOrderStatus.ON_HOLD)
    db_session.commit()

    response = client.get(WALLBOARD_URL, headers=headers_for(viewer))
    assert response.status_code == 200, response.text
    assert response.json()["quality"] == {"open_ncr_count": 2, "newest_ncr_age_days": 1, "wos_on_hold": 1}
    # Counts and ages ONLY: NCR narrative never reaches the public screen.
    assert "MUST-NOT-LEAK" not in response.text


def test_new_blocks_zero_state_on_empty_db(client: TestClient, db_session: Session):
    """Empty shop: real zeros (not nulls) for counts, null only where 'no data'
    genuinely differs from zero (next_due_date, newest_ncr_age_days)."""
    viewer = make_user(db_session)
    payload = _payload(client, headers_for(viewer))
    assert payload["late_total"] == 0
    assert payload["blocked_total"] == 0
    assert payload["down_total"] == 0
    assert payload["ship"] == {
        "due_today": 0,
        "shipped_today": 0,
        "due_this_week": 0,
        "due_today_rows": [],
        "next_due_date": None,
        "next_due_count": 0,
    }
    assert payload["today"] == {
        "ops_completed": 0,
        "pieces_completed": 0,
        "wos_completed": 0,
        "operators_on_clock": 0,
        "hours_logged": 0.0,
        "receipts": 0,
        "scrap_events": 0,
    }
    assert payload["quality"] == {"open_ncr_count": 0, "newest_ncr_age_days": None, "wos_on_hold": 0}


def test_block_failure_nulls_that_block_only(client: TestClient, db_session: Session, monkeypatch):
    """The get_kpi_strip best-effort pattern: one panel's compute blowing up
    nulls THAT panel, never the payload."""
    import app.services.wallboard_service as wallboard_service

    def _boom(db, company_id, central_today):
        raise RuntimeError("ship panel exploded")

    monkeypatch.setattr(wallboard_service, "_compute_ship", _boom)

    viewer = make_user(db_session)
    response = client.get(WALLBOARD_URL, headers=headers_for(viewer))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ship"] is None
    assert payload["today"] is not None
    assert payload["quality"] is not None
    assert payload["late_total"] == 0
    assert "work_centers" in payload


def test_wallboard_build_is_zero_write(db_session: Session):
    """The builder must stay ZERO-WRITE: no new/dirty/deleted ORM state and no
    audit rows after building a fully-populated payload."""
    from app.services.wallboard_service import build_wallboard_payload

    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    central_today = datetime.now(CENTRAL_TIME_ZONE).date()
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, due_date=central_today - timedelta(days=1))
    op = make_op(db_session, wo, wc, status_=OperationStatus.IN_PROGRESS)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    make_entry(db_session, operator, wo, op, wc, open_entry=True)
    assert viewer.company_id == 1

    audit_before = db_session.query(AuditLog).count()

    payload = build_wallboard_payload(db_session, 1)
    assert payload.late_total == 1  # the build really ran against the seeded data

    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted
    assert db_session.query(AuditLog).count() == audit_before


def test_payload_privacy_no_identities_costs_or_customers(client: TestClient, db_session: Session):
    """Serialize a fully-populated payload and prove the public-TV contract:
    no customer identity, no ship-to, no dollars, no NCR narrative, no full
    last names — every rendered name is 'First L.'-shaped."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    central_today = datetime.now(CENTRAL_TIME_ZONE).date()

    wo = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        customer_name="Sensitive Customer Co",
        must_ship_by=central_today,
        due_date=central_today - timedelta(days=2),
        quantity_ordered=10,
    )
    wo.estimated_cost = 1234.56
    wo.actual_cost = 789.01
    op = make_op(db_session, wo, wc, status_=OperationStatus.IN_PROGRESS)
    operator = make_user(db_session, role=UserRole.OPERATOR, first_name="Priya", last_name="Rockefeller")
    make_entry(db_session, operator, wo, op, wc, open_entry=True)
    db_session.add(
        NonConformanceReport(
            ncr_number="NCR-PRIV",
            title="Priv title SECRET",
            description="Priv desc SECRET",
            source=NCRSource.IN_PROCESS,
            status=NCRStatus.OPEN,
            company_id=1,
        )
    )
    make_shipment(db_session, wo, ship_date=central_today, quantity_shipped=4)
    db_session.commit()

    response = client.get(WALLBOARD_URL, headers=headers_for(viewer))
    assert response.status_code == 200, response.text
    raw = response.text
    for forbidden in (
        "Sensitive Customer",  # customer identity is OMITTED (product ruling)
        "customer_name",
        "ship_to",
        "estimated_cost",
        "actual_cost",
        "hourly_rate",
        "employee_id",
        "Rockefeller",  # full last names never leave the server
        "SECRET",  # NCR narrative
    ):
        assert forbidden not in raw, f"public wallboard payload leaked {forbidden!r}"

    payload = response.json()
    name_shape = re.compile(r"^\S+ [A-Z]\.$")
    names = []
    for card in payload["work_centers"]:
        for job in card["active_jobs"]:
            if job["operator_name"]:
                names.append(job["operator_name"])
            names.extend(job["crew"])
    assert names, "expected at least one crew name on the seeded board"
    for name in names:
        assert name_shape.match(name), f"operator name {name!r} is not 'First L.'-shaped"
