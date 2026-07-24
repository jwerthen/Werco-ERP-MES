"""API coverage for the in-app notification inbox endpoints (NOTIFICATIONS_PLAN.md §6,
PR1_DESIGN_SPEC.md §G).

All endpoints are SELF + TENANT scoped (``user_id == current_user.id`` AND
``company_id == active company``). Compliance points pinned here:

* a user can only ever read / mark their OWN rows -- another user's or another tenant's
  notification is invisible (list omits it) and un-markable (404);
* mark-read / read-all are deliberately NOT audited (UI state, not domain state, §6);
* filters (unread / category / severity) + server pagination behave;
* ``/catalog`` serves the settings-matrix rows.
"""

import pytest
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.notification import Notification
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api]

BASE = "/api/v1/notifications"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=f"napi-co-{company_id}", is_active=True))
        db.commit()


def _make_user(db: Session, *, company_id: int) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"napi-{n}@co{company_id}.test",
        employee_id=f"NAPI-{n:05d}",
        first_name="NApi",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.OPERATOR,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_notification(
    db: Session,
    *,
    user_id: int,
    company_id: int = 1,
    event_key: str = "wo.released",
    severity: str = "info",
    is_read: bool = False,
    title: str = "Notice",
) -> Notification:
    notif = Notification(
        company_id=company_id,
        user_id=user_id,
        event_key=event_key,
        severity=severity,
        title=title,
        body="body",
        link="/work-orders/1",
        related_type="WorkOrder",
        related_id=1,
        is_read=is_read,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif


# ---------------------------------------------------------------------------
# GET /notifications -- self + tenant scoped, filters, pagination
# ---------------------------------------------------------------------------


def test_list_is_self_and_tenant_scoped(client, db_session, test_user, auth_headers):
    other_user = _make_user(db_session, company_id=1)  # same tenant, different user
    foreign_user = _make_user(db_session, company_id=2)  # different tenant

    mine = _make_notification(db_session, user_id=test_user.id, title="mine")
    _make_notification(db_session, user_id=other_user.id, title="not-mine")
    # A cross-tenant row that (wrongly) points at the test user id in company 2.
    _make_notification(db_session, user_id=test_user.id, company_id=2, title="foreign-tenant")
    _make_notification(db_session, user_id=foreign_user.id, company_id=2, title="foreign-user")

    resp = client.get(BASE, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    ids = {item["id"] for item in body["items"]}
    titles = {item["title"] for item in body["items"]}

    assert mine.id in ids
    assert titles == {"mine"}, "list must return only the caller's own, same-tenant rows"
    assert set(body["pagination"].keys()) >= {"page", "page_size", "total_count", "total_pages", "has_next"}


def test_list_unread_filter(client, db_session, test_user, auth_headers):
    _make_notification(db_session, user_id=test_user.id, is_read=False, title="unread")
    _make_notification(db_session, user_id=test_user.id, is_read=True, title="read")

    unread = client.get(BASE, headers=auth_headers, params={"unread": "true"}).json()
    assert {i["title"] for i in unread["items"]} == {"unread"}

    read = client.get(BASE, headers=auth_headers, params={"unread": "false"}).json()
    assert {i["title"] for i in read["items"]} == {"read"}


def test_list_category_and_severity_filters(client, db_session, test_user, auth_headers):
    # ncr.created -> category "Quality", severity critical; wo.released -> "Production".
    _make_notification(db_session, user_id=test_user.id, event_key="ncr.created", severity="critical", title="quality")
    _make_notification(db_session, user_id=test_user.id, event_key="wo.released", severity="info", title="production")

    quality = client.get(BASE, headers=auth_headers, params={"category": "Quality"}).json()
    assert {i["title"] for i in quality["items"]} == {"quality"}

    critical = client.get(BASE, headers=auth_headers, params={"severity": "critical"}).json()
    assert {i["title"] for i in critical["items"]} == {"quality"}


def test_list_pagination(client, db_session, test_user, auth_headers):
    for i in range(5):
        _make_notification(db_session, user_id=test_user.id, title=f"n{i}")

    page1 = client.get(BASE, headers=auth_headers, params={"page": 1, "page_size": 2}).json()
    assert len(page1["items"]) == 2
    assert page1["pagination"]["total_count"] == 5
    assert page1["pagination"]["total_pages"] == 3
    assert page1["pagination"]["has_next"] is True


# ---------------------------------------------------------------------------
# GET /notifications/unread-count
# ---------------------------------------------------------------------------


def test_unread_count(client, db_session, test_user, auth_headers):
    _make_notification(db_session, user_id=test_user.id, is_read=False)
    _make_notification(db_session, user_id=test_user.id, is_read=False)
    _make_notification(db_session, user_id=test_user.id, is_read=True)
    # A different user's unread row must not inflate the caller's count.
    other = _make_user(db_session, company_id=1)
    _make_notification(db_session, user_id=other.id, is_read=False)

    resp = client.get(f"{BASE}/unread-count", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


# ---------------------------------------------------------------------------
# POST /notifications/{id}/read
# ---------------------------------------------------------------------------


def test_mark_read_marks_and_is_not_audited(client, db_session, test_user, auth_headers):
    notif = _make_notification(db_session, user_id=test_user.id, is_read=False)
    audit_before = db_session.query(AuditLog).count()

    resp = client.post(f"{BASE}/{notif.id}/read", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_read"] is True
    assert body["read_at"] is not None

    db_session.refresh(notif)
    assert notif.is_read is True

    # Mark-read is deliberately NOT audited (UI state, not domain state, §6).
    assert db_session.query(AuditLog).count() == audit_before


def test_mark_read_404_for_another_users_row(client, db_session, test_user, auth_headers):
    other = _make_user(db_session, company_id=1)
    foreign = _make_notification(db_session, user_id=other.id, is_read=False)

    resp = client.post(f"{BASE}/{foreign.id}/read", headers=auth_headers)
    assert resp.status_code == 404
    db_session.refresh(foreign)
    assert foreign.is_read is False, "another user's row must be untouched"


def test_mark_read_404_for_another_tenant_row(client, db_session, test_user, auth_headers):
    # Same user id, different tenant -> not visible to the active company.
    foreign = _make_notification(db_session, user_id=test_user.id, company_id=2, is_read=False)
    resp = client.post(f"{BASE}/{foreign.id}/read", headers=auth_headers)
    assert resp.status_code == 404
    db_session.refresh(foreign)
    assert foreign.is_read is False


# ---------------------------------------------------------------------------
# POST /notifications/read-all
# ---------------------------------------------------------------------------


def test_read_all_marks_only_own_unread(client, db_session, test_user, auth_headers):
    _make_notification(db_session, user_id=test_user.id, is_read=False)
    _make_notification(db_session, user_id=test_user.id, is_read=False)
    other = _make_user(db_session, company_id=1)
    other_notif = _make_notification(db_session, user_id=other.id, is_read=False)

    audit_before = db_session.query(AuditLog).count()
    resp = client.post(f"{BASE}/read-all", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2

    # The other user's unread row is untouched, and read-all is not audited.
    db_session.refresh(other_notif)
    assert other_notif.is_read is False
    assert db_session.query(AuditLog).count() == audit_before

    assert client.get(f"{BASE}/unread-count", headers=auth_headers).json()["count"] == 0


# ---------------------------------------------------------------------------
# GET /notifications/catalog
# ---------------------------------------------------------------------------


def test_catalog_returns_settings_matrix(client, auth_headers):
    resp = client.get(f"{BASE}/catalog", headers=auth_headers)
    assert resp.status_code == 200
    entries = resp.json()
    assert isinstance(entries, list) and entries

    by_key = {e["event_key"]: e for e in entries}
    assert "ncr.created" in by_key
    ncr = by_key["ncr.created"]
    for field in ("event_key", "label", "description", "category", "severity", "default_channels"):
        assert field in ncr
    # ncr.created forces in-app on (mandatory) and is SMS-eligible.
    assert ncr["mandatory_channel"] == "in_app"
    assert ncr["sms_eligible"] is True
    assert "in_app" in ncr["default_channels"]


def test_endpoints_require_auth(client):
    assert client.get(BASE).status_code in (401, 403)
    assert client.get(f"{BASE}/unread-count").status_code in (401, 403)
