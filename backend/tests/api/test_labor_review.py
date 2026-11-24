"""Labor Review: GET /shop-floor/time-entries and POST /shop-floor/time-entries/{id}/set-clock-out.

Also the two approval changes that ship with it: approve refuses an entry still clocked in
(409), and approve / unapprove / set-clock-out refuse a standing API token and a kiosk badge.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.mcp.catalog import EXCLUDED_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services.labor_review_service import (
    APPROVED_ENTRY_CLOCK_OUT_DETAIL,
    CLOCK_OUT_BEFORE_CLOCK_IN_DETAIL,
    CLOCK_OUT_IN_FUTURE_DETAIL,
    LABOR_JUDGMENT_API_TOKEN_DETAIL,
    OPEN_ENTRY_APPROVAL_DETAIL,
    OWN_ENTRY_CLOCK_OUT_DETAIL,
    STALE_TIME_ENTRY_DETAIL,
)
from tests.api.test_time_entry_approval_batch11b import (
    COMPANY_A,
    COMPANY_B,
    headers_for,
    make_closed_entry,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

LIST_URL = "/api/v1/shop-floor/time-entries"
API_TOKENS_URL = "/api/v1/api-tokens/"


def _set_url(entry_id: int) -> str:
    return f"/api/v1/shop-floor/time-entries/{entry_id}/set-clock-out"


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def _world(db: Session, *, company_id: int = COMPANY_A):
    operator = make_user(db, role=UserRole.OPERATOR, company_id=company_id)
    supervisor = make_user(db, role=UserRole.SUPERVISOR, company_id=company_id)
    part = make_part(db, company_id=company_id)
    wc = make_work_center(db, company_id=company_id)
    wo = make_wo(db, part, company_id=company_id)
    op = make_op(db, wo, wc, company_id=company_id)
    return operator, supervisor, wo, op


def _open_entry(db: Session, user, wo, op, *, hours_ago: float = 15.0, company_id: int = COMPANY_A) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=hours_ago),
        company_id=company_id,
    )
    db.add(entry)
    db.flush()
    return entry


def _api_token_headers(client: TestClient, db: Session, user) -> dict:
    admin = make_user(db, role=UserRole.ADMIN)
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "labor probe"}, headers=headers_for(admin))
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


# --------------------------------------------------------------------------------------
# The list read
# --------------------------------------------------------------------------------------


def test_list_flags_a_forgotten_clock_and_is_tenant_scoped(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    forgotten = _open_entry(db_session, operator, wo, op, hours_ago=15)
    other_operator, _sup_b, wo_b, op_b = _world(db_session, company_id=COMPANY_B)
    foreign = _open_entry(db_session, other_operator, wo_b, op_b, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get(LIST_URL, params={"status": "open"}, headers=headers_for(supervisor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [row["id"] for row in body["items"]]
    assert forgotten.id in ids
    assert foreign.id not in ids
    row = next(r for r in body["items"] if r["id"] == forgotten.id)
    assert row["is_open"] is True
    assert "long_entry" in row["flags"]
    assert row["can_set_clock_out"] is True
    assert row["can_approve"] is False, "an open entry cannot be approved"
    assert row["work_order_number"] == wo.work_order_number
    assert body["flag_counts"].get("long_entry", 0) >= 1
    assert body["start_date"] is None and body["end_date"] is None


def test_list_flags_overlap_zero_pieces_backfill_and_closed_work(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    first = make_closed_entry(db_session, operator, wo, op, duration_hours=5)
    second = make_closed_entry(db_session, operator, wo, op, duration_hours=1)
    second.source = TimeEntrySource.BACKFILL.value
    second.quantity_produced = 3
    op.status = OperationStatus.COMPLETE
    on_closed = _open_entry(db_session, make_user(db_session, role=UserRole.OPERATOR), wo, op, hours_ago=1)
    db_session.commit()

    resp = client.get(LIST_URL, headers=headers_for(supervisor))
    assert resp.status_code == 200, resp.text
    by_id = {row["id"]: row for row in resp.json()["items"]}
    assert "overlap" in by_id[first.id]["flags"] and "overlap" in by_id[second.id]["flags"]
    assert "long_zero_pieces" in by_id[first.id]["flags"]
    assert "backfill" in by_id[second.id]["flags"]
    assert "open_on_closed_work" in by_id[on_closed.id]["flags"]

    only_backfill = client.get(LIST_URL, params={"flag": "backfill"}, headers=headers_for(supervisor)).json()
    assert [row["id"] for row in only_backfill["items"]] == [second.id]


def test_list_pages_and_filters(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    for _ in range(3):
        make_closed_entry(db_session, operator, wo, op, duration_hours=1)
    db_session.commit()
    page1 = client.get(LIST_URL, params={"page_size": 2, "user_id": operator.id}, headers=headers_for(supervisor))
    assert page1.status_code == 200, page1.text
    body = page1.json()
    assert body["total"] == 3 and len(body["items"]) == 2 and body["has_next"] is True
    page2 = client.get(
        LIST_URL, params={"page_size": 2, "page": 2, "user_id": operator.id}, headers=headers_for(supervisor)
    ).json()
    assert len(page2["items"]) == 1 and page2["has_next"] is False


def test_list_role_gate_range_cap_and_kiosk_fence(client: TestClient, db_session: Session):
    operator, supervisor, _wo, _op = _world(db_session)
    db_session.commit()
    assert client.get(LIST_URL, headers=headers_for(operator)).status_code == 403
    too_long = client.get(
        LIST_URL, params={"start_date": "2026-01-01", "end_date": "2026-06-01"}, headers=headers_for(supervisor)
    )
    assert too_long.status_code == 422
    reversed_range = client.get(
        LIST_URL, params={"start_date": "2026-06-02", "end_date": "2026-06-01"}, headers=headers_for(supervisor)
    )
    assert reversed_range.status_code == 422
    kiosk = create_access_token(subject=supervisor.id, company_id=COMPANY_A, scope="kiosk")
    resp = client.get(LIST_URL, headers={"Authorization": f"Bearer {kiosk}"})
    assert resp.status_code == 403


# --------------------------------------------------------------------------------------
# set-clock-out
# --------------------------------------------------------------------------------------


def test_closing_a_forgotten_clock_books_the_stated_hours_and_audits(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = _open_entry(db_session, operator, wo, op, hours_ago=15)
    db_session.commit()
    stopped = entry.clock_in + timedelta(hours=8)

    resp = client.post(
        _set_url(entry.id),
        json={"clock_out": _iso(stopped), "reason": "Forgot to clock out at shift end", "version": entry.version},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["entry"]["is_open"] is False
    assert body["hours"]["applied"] is True
    assert abs(body["entry"]["duration_hours"] - 8.0) < 0.01

    db_session.expire_all()
    stored = db_session.get(TimeEntry, entry.id)
    assert stored.clock_out is not None and abs(stored.duration_hours - 8.0) < 0.01
    assert float(stored.quantity_produced or 0) == 0
    refreshed_op = stored.operation
    assert abs(float(refreshed_op.actual_run_hours or 0) - 8.0) < 0.01
    assert abs(float(stored.work_order.actual_hours or 0) - 8.0) < 0.01

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "time_entry", AuditLog.resource_id == entry.id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None and audit.action == "TIME_ENTRY_SET_CLOCK_OUT"
    assert audit.extra_data["reason"] == "Forgot to clock out at shift end"
    assert audit.old_values["clock_out"] is None


def test_correcting_a_closed_entry_lowers_stored_hours(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=10)
    op.actual_run_hours = 10.0
    wo.actual_hours = 10.0
    db_session.commit()

    resp = client.post(
        _set_url(entry.id),
        json={
            "clock_out": _iso(entry.clock_in + timedelta(hours=3)),
            "reason": "Stopped at break; clock left running",
            "version": entry.version,
        },
        headers=headers_for(supervisor),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hours"]["applied"] is True
    db_session.expire_all()
    stored = db_session.get(TimeEntry, entry.id)
    assert abs(float(stored.operation.actual_run_hours) - 3.0) < 0.01
    assert abs(float(stored.work_order.actual_hours) - 3.0) < 0.01


def test_manual_operation_hours_are_left_alone(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    # A zero-length closed entry and hand-set stored hours: hours_source reads "manual".
    entry = make_closed_entry(db_session, operator, wo, op, duration_hours=0)
    entry.clock_in = entry.clock_out = datetime.utcnow() - timedelta(hours=3)
    op.actual_run_hours = 6.0
    wo.actual_hours = 6.0
    db_session.commit()

    resp = client.post(
        _set_url(entry.id),
        json={"clock_out": _iso(entry.clock_in + timedelta(hours=1)), "reason": "correct", "version": entry.version},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hours"]["applied"] is False
    assert resp.json()["hours"]["skipped_reason"] == "operation_hours_manual"
    db_session.expire_all()
    stored = db_session.get(TimeEntry, entry.id)
    assert float(stored.operation.actual_run_hours) == 6.0
    assert float(stored.work_order.actual_hours) == 6.0


def test_set_clock_out_refusals(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = _open_entry(db_session, operator, wo, op, hours_ago=5)
    own = _open_entry(db_session, supervisor, wo, op, hours_ago=5)
    approved = make_closed_entry(db_session, operator, wo, op, approved=True)
    db_session.commit()
    good_time = _iso(entry.clock_in + timedelta(hours=1))

    def post(target, **overrides):
        payload = {"clock_out": good_time, "reason": "r", "version": target.version}
        payload.update(overrides)
        return client.post(_set_url(target.id), json=payload, headers=headers_for(supervisor))

    resp = post(own)
    assert resp.status_code == 403 and resp.json()["detail"] == OWN_ENTRY_CLOCK_OUT_DETAIL
    resp = post(approved)
    assert resp.status_code == 409 and resp.json()["detail"] == APPROVED_ENTRY_CLOCK_OUT_DETAIL
    resp = post(entry, version=entry.version + 7)
    assert resp.status_code == 409 and resp.json()["detail"] == STALE_TIME_ENTRY_DETAIL
    resp = post(entry, clock_out=_iso(entry.clock_in - timedelta(minutes=5)))
    assert resp.status_code == 422 and resp.json()["detail"] == CLOCK_OUT_BEFORE_CLOCK_IN_DETAIL
    resp = post(entry, clock_out=_iso(datetime.utcnow() + timedelta(hours=2)))
    assert resp.status_code == 422 and resp.json()["detail"] == CLOCK_OUT_IN_FUTURE_DETAIL
    resp = post(entry, reason="   ")
    assert resp.status_code == 422
    assert (
        client.post(
            _set_url(999999),
            json={"clock_out": good_time, "reason": "r", "version": 1},
            headers=headers_for(supervisor),
        ).status_code
        == 404
    )

    quality = make_user(db_session, role=UserRole.QUALITY)
    resp = client.post(
        _set_url(entry.id), json={"clock_out": good_time, "reason": "r", "version": 1}, headers=headers_for(quality)
    )
    assert resp.status_code == 403, "Quality approves labor but does not set clock-outs"

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).clock_out is None, "no refusal wrote anything"


def test_set_clock_out_refuses_other_tenant(client: TestClient, db_session: Session):
    _operator, supervisor, _wo, _op = _world(db_session)
    operator_b, _sup_b, wo_b, op_b = _world(db_session, company_id=COMPANY_B)
    foreign = _open_entry(db_session, operator_b, wo_b, op_b, company_id=COMPANY_B)
    db_session.commit()
    resp = client.post(
        _set_url(foreign.id),
        json={"clock_out": _iso(foreign.clock_in + timedelta(hours=1)), "reason": "r", "version": foreign.version},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------------------
# Approval changes and credential refusals
# --------------------------------------------------------------------------------------


def test_approve_refuses_an_open_entry(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = _open_entry(db_session, operator, wo, op, hours_ago=1)
    db_session.commit()
    resp = client.post(f"/api/v1/shop-floor/time-entries/{entry.id}/approve", headers=headers_for(supervisor))
    assert resp.status_code == 409 and resp.json()["detail"] == OPEN_ENTRY_APPROVAL_DETAIL
    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).approved is None


@pytest.mark.parametrize("verb", ["approve", "unapprove", "set-clock-out"])
def test_labor_judgments_refuse_an_api_token(client: TestClient, db_session: Session, verb: str):
    operator, supervisor, wo, op = _world(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, approved=(verb == "unapprove"))
    db_session.commit()
    headers = _api_token_headers(client, db_session, supervisor)
    body = {"clock_out": _iso(entry.clock_in + timedelta(minutes=30)), "reason": "r", "version": entry.version}
    resp = client.post(
        f"/api/v1/shop-floor/time-entries/{entry.id}/{verb}",
        json=body if verb == "set-clock-out" else None,
        headers=headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == LABOR_JUDGMENT_API_TOKEN_DETAIL


@pytest.mark.parametrize("verb", ["approve", "unapprove", "set-clock-out"])
def test_labor_judgments_refuse_a_kiosk_badge(client: TestClient, db_session: Session, verb: str):
    operator, supervisor, wo, op = _world(db_session)
    entry = make_closed_entry(db_session, operator, wo, op)
    db_session.commit()
    kiosk = create_access_token(subject=supervisor.id, company_id=COMPANY_A, scope="kiosk")
    resp = client.post(
        f"/api/v1/shop-floor/time-entries/{entry.id}/{verb}", json={}, headers={"Authorization": f"Bearer {kiosk}"}
    )
    assert resp.status_code == 403


def test_labor_judgments_are_not_mcp_tools():
    for path in ("approve", "unapprove", "set-clock-out"):
        assert ("POST", f"/api/v1/shop-floor/time-entries/{{time_entry_id}}/{path}") in EXCLUDED_OPERATIONS


def test_terminal_work_order_closing_does_not_accrue(client: TestClient, db_session: Session):
    operator, supervisor, wo, op = _world(db_session)
    entry = _open_entry(db_session, operator, wo, op, hours_ago=10)
    wo.status = WorkOrderStatus.COMPLETE
    db_session.commit()
    resp = client.post(
        _set_url(entry.id),
        json={"clock_out": _iso(entry.clock_in + timedelta(hours=2)), "reason": "r", "version": entry.version},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hours"]["skipped_reason"] == "terminal_work_order"
    db_session.expire_all()
    stored = db_session.get(TimeEntry, entry.id)
    assert stored.clock_out is not None
    assert float(stored.work_order.actual_hours or 0) == 0
