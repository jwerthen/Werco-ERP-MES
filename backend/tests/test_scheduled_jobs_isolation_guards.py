"""Tenant, read-only and edge-case locks for the "scheduled jobs safe to switch on" work.

The behaviour tests live in ``test_scheduled_alerts.py``, ``test_cron_never_creates_supply.py``
and ``api/test_user_departments_and_email_reach.py``. This file holds the invariants those do
not pin:

Low stock (``low_stock_service``):
  * tenant isolation both ways -- another company's part is never on the list, and another
    company's stock row (even one pointing at this company's part) never keeps a part off it;
  * only active, company-owned stock rows count; an inactive part is never low;
  * ordering is critical first, then the largest shortage;
  * the query compiles for Postgres (production) with the per-part SUM and COALESCE;
  * ``GET /inventory/low-stock`` is tenant-scoped and writes nothing.

Departments:
  * ``GET /users/departments`` lists only this company's users and writes nothing;
  * ``POST /auth/register`` refuses an unlisted department with no user and no audit row;
  * an update may clear a department to blank.

Audience:
  * ``catalog_audience`` is tenant-scoped and active-only; an uncataloged key reaches nobody;
  * the Email Recipients reach preview never lists another company's or an inactive user.

Late summary:
  * a soft-deleted work order is never counted;
  * another company's summary row for the same day does not suppress this company's.

Digest:
  * a saved WEEKLY frequency opts out of the daily digest.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

import app.jobs.notification_jobs as notification_jobs
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole
from app.services.low_stock_service import low_stock_count, low_stock_parts, low_stock_query
from app.services.notification_catalog import get_entry
from app.services.notification_dispatch import catalog_audience, catalog_audience_for
from app.services.user_departments import DEPARTMENT_INVALID_DETAIL
from tests.api.test_backflush_lot_policy import headers_for, make_lot
from tests.api.test_backflush_lot_policy import make_user as make_plain_user
from tests.api.test_email_recipients import URL as EMAIL_URL
from tests.api.test_email_recipients import events, headers, make_user
from tests.api.test_owned_property_planning import _lot, _reorder_part
from tests.test_notification_jobs_tenant_isolation import COMPANY_A, COMPANY_B, _ensure_company, _make_part, _make_user
from tests.test_scheduled_alerts import TODAY, _pin_today, _queue, _record, _UnclosableSession, _wo

pytestmark = [pytest.mark.requires_db]


def _audit_rows(db: Session) -> int:
    db.expire_all()
    return db.query(AuditLog).count()


def _ids(rows) -> set:
    return {row.part.id for row in rows}


# ---------------------------------------------------------------------------
# Low stock: one rule, tenant-scoped, owned stock only
# ---------------------------------------------------------------------------


def test_low_stock_is_tenant_scoped_both_ways(db_session: Session):
    mine = _reorder_part(db_session, reorder_point=10)  # company A, no stock: low
    theirs = _make_part(db_session, company_id=COMPANY_B, reorder_point=10)  # company B, no stock: low for B
    # A company-B stock row that points at A's part (a mis-tenanted row) must not count for A.
    make_lot(db_session, mine, qty=50, lot=f"XTEN-{mine.id}", company_id=COMPANY_B)

    a_ids = _ids(low_stock_parts(db_session, COMPANY_A))
    b_ids = _ids(low_stock_parts(db_session, COMPANY_B))
    assert mine.id in a_ids, "another company's stock row kept this company's part off its list"
    assert theirs.id not in a_ids
    assert theirs.id in b_ids and mine.id not in b_ids


def test_only_active_company_owned_rows_count_and_inactive_parts_are_never_low(db_session: Session):
    customer_stock = _reorder_part(db_session, reorder_point=10)
    _lot(db_session, customer_stock, 500, owner="customer")
    supplier_stock = _reorder_part(db_session, reorder_point=10)
    _lot(db_session, supplier_stock, 500, owner="supplier")
    inactive_row = _reorder_part(db_session, reorder_point=10)
    _lot(db_session, inactive_row, 500, is_active=False)
    owned = _reorder_part(db_session, reorder_point=10)
    _lot(db_session, owned, 500)
    retired = _reorder_part(db_session, reorder_point=10)
    retired.is_active = False
    db_session.commit()

    by_id = {row.part.id: row for row in low_stock_parts(db_session, COMPANY_A)}
    for part in (customer_stock, supplier_stock, inactive_row):
        assert part.id in by_id, "stock the shop does not own (or an inactive row) is not supply"
        assert by_id[part.id].quantity_on_hand == 0 and by_id[part.id].out_of_stock is True
    assert owned.id not in by_id
    assert retired.id not in by_id


def test_low_stock_orders_critical_first_then_by_shortage(db_session: Session):
    small_gap = _reorder_part(db_session, reorder_point=10, safety_stock=1)
    _lot(db_session, small_gap, 9)  # shortage 1, not critical
    big_gap = _reorder_part(db_session, reorder_point=100, safety_stock=1)
    _lot(db_session, big_gap, 40)  # shortage 60, not critical
    critical = _reorder_part(db_session, reorder_point=10, safety_stock=5)
    _lot(db_session, critical, 5)  # shortage 5, AT safety stock: critical

    rows = [
        row for row in low_stock_parts(db_session, COMPANY_A) if row.part.id in {small_gap.id, big_gap.id, critical.id}
    ]
    assert [row.part.id for row in rows] == [critical.id, big_gap.id, small_gap.id]
    assert [row.is_critical for row in rows] == [True, False, False]
    assert rows[1].shortage == 60
    assert low_stock_count(db_session, COMPANY_A) == len(low_stock_parts(db_session, COMPANY_A))


def test_the_low_stock_query_compiles_for_postgres(db_session: Session):
    """Production is Postgres: compile, never trust SQLite's execution (CLAUDE.md)."""
    sql = str(
        low_stock_query(db_session, COMPANY_A).statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).lower()
    assert "sum(inventory_items.quantity_on_hand)" in sql
    assert "left outer join" in sql
    assert "group by inventory_items.part_id" in sql
    assert "parts.reorder_point > 0" in sql
    assert "parts.is_deleted is false" in sql
    assert "inventory_items.company_id = 1" in sql and "parts.company_id = 1" in sql


def test_get_low_stock_is_tenant_scoped_and_writes_nothing(client, db_session: Session):
    mine = _reorder_part(db_session, reorder_point=10)
    theirs = _make_part(db_session, company_id=COMPANY_B, reorder_point=10)
    viewer_a = make_plain_user(db_session, role=UserRole.VIEWER, company_id=COMPANY_A)
    viewer_b = make_plain_user(db_session, role=UserRole.VIEWER, company_id=COMPANY_B)
    before = _audit_rows(db_session)

    resp_a = client.get("/api/v1/inventory/low-stock", headers=headers_for(viewer_a))
    resp_b = client.get("/api/v1/inventory/low-stock", headers=headers_for(viewer_b))

    assert resp_a.status_code == 200 and resp_b.status_code == 200
    a_ids = {row["part_id"] for row in resp_a.json()}
    b_ids = {row["part_id"] for row in resp_b.json()}
    assert mine.id in a_ids and theirs.id not in a_ids
    assert theirs.id in b_ids and mine.id not in b_ids
    assert _audit_rows(db_session) == before, "a pure read writes no audit row"


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------


def test_the_departments_read_lists_only_this_company_and_writes_nothing(client, db_session: Session, admin_user):
    _ensure_company(db_session, COMPANY_B)
    mine = make_user(db_session, "legacy-mine@example.test", department="Machining")
    theirs = make_user(db_session, "legacy-theirs@example.test", company_id=COMPANY_B, department="Machining")
    before = _audit_rows(db_session)

    response = client.get("/api/v1/users/departments", headers=headers(admin_user))

    assert response.status_code == 200, response.text
    ids = {row["id"] for row in response.json()["unlisted"]}
    assert mine.id in ids and theirs.id not in ids
    assert _audit_rows(db_session) == before


@pytest.mark.parametrize("role", [UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY, UserRole.OPERATOR])
def test_the_departments_read_is_admin_only(client, db_session: Session, role):
    user = make_user(db_session, f"dept-role-{role.value}@example.test", role=role)
    assert client.get("/api/v1/users/departments", headers=headers(user)).status_code == 403


def test_register_refuses_an_unlisted_department_and_writes_nothing(client, db_session: Session, admin_user):
    before = _audit_rows(db_session)
    payload = {
        "email": "register-dept@fictional-shop.com",
        "employee_id": "REG-DEPT-1",
        "first_name": "Reggie",
        "last_name": "Strar",
        "password": "correct horse battery staple 9",
        "role": "operator",
        "department": "Machining",
    }
    refused = client.post("/api/v1/auth/register", headers=headers(admin_user), json=payload)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == DEPARTMENT_INVALID_DETAIL
    assert db_session.query(User).filter(User.employee_id == "REG-DEPT-1").count() == 0
    assert _audit_rows(db_session) == before, "a refused register leaves no audit row"

    accepted = client.post(
        "/api/v1/auth/register", headers=headers(admin_user), json={**payload, "department": " production "}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["department"] == "Production"


def test_an_update_may_clear_the_department(client, db_session: Session, admin_user):
    user = make_user(db_session, "clear-dept@example.test", department="Machining")
    response = client.put(f"/api/v1/users/{user.id}", headers=headers(admin_user), json={"department": ""})
    assert response.status_code == 200, response.text
    db_session.refresh(user)
    assert user.department is None


# ---------------------------------------------------------------------------
# Audience: tenant-scoped, active only
# ---------------------------------------------------------------------------


def test_catalog_audience_is_tenant_scoped_and_active_only(db_session: Session):
    by_role = _make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY)
    by_dept = _make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR, department="Quality")
    inactive = _make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY)
    inactive.is_active = False
    other_tenant = _make_user(db_session, company_id=COMPANY_B, role=UserRole.QUALITY, department="Quality")
    db_session.commit()

    ids = {user.id for user in catalog_audience(db_session, get_entry("calibration.overdue"), COMPANY_A)}
    assert {by_role.id, by_dept.id} <= ids
    assert inactive.id not in ids and other_tenant.id not in ids
    assert catalog_audience_for(db_session, "no.such.event", COMPANY_A) == []


def test_the_email_reach_preview_never_lists_another_company_or_an_inactive_user(
    client, db_session: Session, admin_user
):
    _ensure_company(db_session, COMPANY_B)
    buyer = make_user(db_session, "buyer-reach@fictional-shop.com", department="Purchasing")
    gone = make_user(db_session, "gone-reach@fictional-shop.com", department="Purchasing", active=False)
    other = make_user(db_session, "other-reach@fictional-shop.com", company_id=COMPANY_B, department="Purchasing")

    expedite = events(client.get(EMAIL_URL, headers=headers(admin_user)))["mrp.expedite_required"]

    assert expedite["audience_source"] == "automatic"
    assert buyer.id in expedite["reaches_user_ids"]
    assert gone.id not in expedite["reaches_user_ids"]
    assert other.id not in expedite["reaches_user_ids"]
    assert expedite["reaches_nobody"] is False


# ---------------------------------------------------------------------------
# Late summary
# ---------------------------------------------------------------------------


def test_a_soft_deleted_work_order_is_never_counted_late(db_session: Session, monkeypatch):
    _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    live = _wo(db_session, due=TODAY - timedelta(days=3))
    deleted = _wo(db_session, due=TODAY - timedelta(days=40))
    deleted.is_deleted = True
    db_session.commit()
    _pin_today(monkeypatch)
    calls = _record(monkeypatch, db_session)

    asyncio.run(notification_jobs.check_late_work_orders_task())

    (call,) = [c for c in calls if c["company_id"] == COMPANY_A]
    assert live.work_order_number in call["body"]
    assert deleted.work_order_number not in call["body"]


def test_another_companys_summary_does_not_suppress_this_one(db_session: Session, monkeypatch):
    from app.models.notification import Notification

    _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    other = _make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)
    _wo(db_session, due=TODAY - timedelta(days=2))
    db_session.add(
        Notification(
            company_id=COMPANY_B,
            user_id=other.id,
            event_key="wo.late",
            severity="warning",
            title="Late work orders: 1",
            related_type="LateWorkOrderSummary",
            related_id=notification_jobs.late_summary_related_id(TODAY),
        )
    )
    db_session.commit()
    _pin_today(monkeypatch)
    calls = _record(monkeypatch, db_session)

    asyncio.run(notification_jobs.check_late_work_orders_task())

    assert [c["related_id"] for c in calls if c["company_id"] == COMPANY_A] == [20310312]


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def test_a_weekly_digest_preference_is_not_sent_the_daily_digest(db_session: Session, monkeypatch):
    import app.jobs.email_jobs as email_jobs
    from app.models.notification import NotificationPreference

    weekly = _make_user(db_session, company_id=COMPANY_A, department="Purchasing")
    daily = _make_user(db_session, company_id=COMPANY_A, department="Purchasing")
    db_session.add(
        NotificationPreference(
            company_id=COMPANY_A, user_id=weekly.id, preferences={}, digest_enabled=True, digest_frequency="WEEKLY"
        )
    )
    db_session.add(
        NotificationPreference(
            company_id=COMPANY_A, user_id=daily.id, preferences={}, digest_enabled=True, digest_frequency="DAILY"
        )
    )
    db_session.commit()
    for user in (weekly, daily):
        _queue(db_session, user, "stock.low", title="Low stock: 1 part", body="1 part is low.", link="/warehouse")
    monkeypatch.setattr(email_jobs, "SessionLocal", lambda: _UnclosableSession(db_session))
    sent = []

    async def _smtp(**kwargs):
        sent.append(kwargs["to"])
        return True

    monkeypatch.setattr(email_jobs.email_service, "send_email", _smtp)

    asyncio.run(email_jobs.send_daily_digest_task())

    assert daily.email in sent and weekly.email not in sent
