"""Reviewed plans are read-only, exact, tenant-bound, and safe to retry."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.audit_log import AuditLog
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderOperation
from tests.api.kiosk_test_helpers import make_user, make_wo_with_operation, make_work_center, user_headers

PREVIEW = "/api/v1/scheduling/impact-preview"
APPLY = "/api/v1/scheduling/impact-apply"


def fixture_job(db, *, center=None, start=None, hours=8, company_id=1):
    center = center or make_work_center(db, company_id=company_id)
    wo, op = make_wo_with_operation(db, company_id=company_id, work_center=center)
    center.capacity_hours_per_day = 8
    op.setup_time_hours = 0
    op.run_time_hours = hours
    op.scheduled_start = op.scheduled_end = start
    op.status = OperationStatus.PENDING
    db.commit()
    return wo, op, center


def preview(client, headers, ids, **options):
    response = client.post(
        PREVIEW,
        headers=headers,
        json={
            "action": "shift",
            "shift_days": 2,
            "work_order_ids": ids,
            **options,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def snapshot(db):
    return [
        (op.id, op.version, op.scheduled_start, op.scheduled_end, op.status)
        for op in db.query(WorkOrderOperation).order_by(WorkOrderOperation.id).all()
    ]


def test_preview_is_read_only_and_apply_preserves_reviewed_dates_and_completed_history(
    client, auth_headers, db_session
):
    start = datetime(2026, 9, 7)
    wo, first, center = fixture_job(db_session, start=start)
    first.status = OperationStatus.COMPLETE
    current = WorkOrderOperation(
        company_id=1,
        work_order_id=wo.id,
        work_center_id=center.id,
        sequence=20,
        operation_number="20",
        name="Inspect",
        status=OperationStatus.PENDING,
        scheduled_start=start + timedelta(days=2),
        scheduled_end=start + timedelta(days=3),
        setup_time_hours=0,
        run_time_hours=8,
    )
    later = WorkOrderOperation(
        company_id=1,
        work_order_id=wo.id,
        work_center_id=center.id,
        sequence=30,
        operation_number="30",
        name="Pack",
        status=OperationStatus.PENDING,
        scheduled_start=start + timedelta(days=6),
        scheduled_end=start + timedelta(days=6),
        setup_time_hours=0,
        run_time_hours=2,
    )
    db_session.add_all([current, later])
    db_session.commit()
    before = snapshot(db_session)
    audits = db_session.query(AuditLog).count()
    plan = preview(client, auth_headers, [wo.id])
    db_session.expire_all()
    assert snapshot(db_session) == before
    assert db_session.query(AuditLog).count() == audits
    assert {op["operation_id"] for op in plan["jobs"][0]["operations"]} == {current.id, later.id}
    result = client.post(APPLY, headers=auth_headers, json={"plan_token": plan["plan_token"]})
    assert result.status_code == 200, result.text
    db_session.expire_all()
    assert first.scheduled_start == start
    assert current.scheduled_start == start + timedelta(days=4)
    assert current.scheduled_end == start + timedelta(days=5)
    assert later.scheduled_start == start + timedelta(days=8)
    assert current.status == OperationStatus.READY
    assert db_session.query(AuditLog).filter(AuditLog.description == "Applied reviewed scheduling plan").count() == 2


def test_earliest_preview_accounts_for_selected_jobs_and_reports_delivery_risk(client, auth_headers, db_session):
    wo1, op1, center = fixture_job(db_session)
    wo2, op2, _ = fixture_job(db_session, center=center)
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    wo1.due_date = wo2.due_date = today
    db_session.commit()
    plan = preview(client, auth_headers, [wo1.id, wo2.id], action="earliest", shift_days=0)
    assert [job["after_finish"] for job in plan["jobs"]] == [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    assert plan["jobs"][1]["late_days"] == 1
    assert plan["summary"]["late_jobs"] == 1
    assert plan["summary"]["overloaded_days"] == 0
    db_session.expire_all()
    assert op1.scheduled_start is None and op2.scheduled_start is None


def test_shift_preview_names_other_jobs_affected_by_overload(client, auth_headers, db_session):
    wo, _, center = fixture_job(db_session, start=datetime(2026, 9, 7))
    other, _, _ = fixture_job(db_session, center=center, start=datetime(2026, 9, 9))
    plan = preview(client, auth_headers, [wo.id])
    row = next(row for row in plan["capacity"] if row["date"] == "2026-09-09")
    assert (row["before_hours"], row["after_hours"], row["overload_hours"]) == (8, 16, 8)
    assert {job["work_order_id"] for job in row["affected_jobs"]} == {wo.id, other.id}


def test_affected_overload_remains_visible_when_different_jobs_replace_the_same_hours(client, auth_headers, db_session):
    first, _, center = fixture_job(db_session, start=datetime(2026, 9, 7), hours=12)
    second, _, _ = fixture_job(db_session, center=center, start=datetime(2026, 9, 9), hours=12)
    plan = preview(client, auth_headers, [first.id, second.id])
    row = next(row for row in plan["capacity"] if row["date"] == "2026-09-09")
    assert (row["before_hours"], row["after_hours"], row["overload_hours"]) == (12, 12, 4)
    assert [job["work_order_id"] for job in row["affected_jobs"]] == [first.id]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "earliest", "work_order_ids": []},
        {"action": "earliest", "work_order_ids": [1, 1]},
        {"action": "earliest", "work_order_ids": list(range(1, 52))},
        {"action": "shift", "work_order_ids": [1], "shift_days": 0},
        {"action": "shift", "work_order_ids": [1], "shift_days": 31},
    ],
)
def test_invalid_scope_is_rejected_before_planning(client, auth_headers, body):
    assert client.post(PREVIEW, headers=auth_headers, json=body).status_code == 422


@pytest.mark.parametrize("change", ["date", "priority", "capacity", "other_job", "completed", "new_operation"])
def test_apply_rejects_stale_dependencies_without_partial_changes(client, auth_headers, db_session, change):
    start = datetime(2026, 9, 7)
    wo, op, center = fixture_job(db_session, start=start)
    second, _, _ = fixture_job(db_session, center=center, start=start + timedelta(days=4))
    plan = preview(client, auth_headers, [wo.id, second.id])
    if change == "date":
        op.scheduled_start -= timedelta(days=1)
    elif change == "priority":
        wo.priority = 1
    elif change == "capacity":
        center.capacity_hours_per_day = 4
    elif change == "other_job":
        fixture_job(db_session, center=center, start=start + timedelta(days=2))
    elif change == "completed":
        op.status = OperationStatus.COMPLETE
    else:
        db_session.add(
            WorkOrderOperation(
                company_id=1,
                work_order_id=wo.id,
                work_center_id=center.id,
                sequence=20,
                operation_number="20",
                name="Added step",
            )
        )
    db_session.commit()
    before = snapshot(db_session)
    result = client.post(APPLY, headers=auth_headers, json={"plan_token": plan["plan_token"]})
    assert result.status_code == 409, result.text
    db_session.expire_all()
    assert snapshot(db_session) == before


def test_exact_applied_token_replay_does_not_shift_twice_or_duplicate_audits(client, auth_headers, db_session):
    wo, _, _ = fixture_job(db_session, start=datetime(2026, 9, 7))
    plan = preview(client, auth_headers, [wo.id])
    first = client.post(APPLY, headers=auth_headers, json={"plan_token": plan["plan_token"]})
    assert first.status_code == 200, first.text
    before = snapshot(db_session)
    audits = db_session.query(AuditLog).count()
    second = client.post(APPLY, headers=auth_headers, json={"plan_token": plan["plan_token"]})
    assert second.status_code == 200, second.text
    assert second.json()["already_applied"] is True
    db_session.expire_all()
    assert snapshot(db_session) == before
    assert db_session.query(AuditLog).count() == audits


def test_preview_and_apply_enforce_tenant_actor_and_role(client, auth_headers, db_session):
    foreign, _, _ = fixture_job(db_session, company_id=2, start=datetime(2026, 9, 7))
    assert (
        client.post(
            PREVIEW, headers=auth_headers, json={"action": "shift", "shift_days": 1, "work_order_ids": [foreign.id]}
        ).status_code
        == 404
    )
    wo, _, _ = fixture_job(db_session, start=datetime(2026, 9, 7))
    plan = preview(client, auth_headers, [wo.id])
    for company in (1, 2):
        other = make_user(db_session, company_id=company, role=UserRole.MANAGER)
        assert (
            client.post(APPLY, headers=user_headers(other), json={"plan_token": plan["plan_token"]}).status_code == 403
        )
    operator = make_user(db_session)
    assert (
        client.post(
            PREVIEW, headers=user_headers(operator), json={"action": "earliest", "work_order_ids": [wo.id]}
        ).status_code
        == 403
    )
    assert (
        client.post(APPLY, headers=user_headers(operator), json={"plan_token": plan["plan_token"]}).status_code == 403
    )


def test_expired_and_tampered_plans_do_not_write(client, auth_headers, db_session, monkeypatch):
    wo, _, _ = fixture_job(db_session, start=datetime(2026, 9, 7))
    before = snapshot(db_session)
    plan = preview(client, auth_headers, [wo.id])
    token = "x" + plan["plan_token"][1:]
    assert client.post(APPLY, headers=auth_headers, json={"plan_token": token}).status_code == 409
    monkeypatch.setattr("app.services.scheduling_impact_service.PLAN_TTL_SECONDS", -1)
    expired = preview(client, auth_headers, [wo.id])
    assert client.post(APPLY, headers=auth_headers, json={"plan_token": expired["plan_token"]}).status_code == 409
    db_session.expire_all()
    assert snapshot(db_session) == before


def test_blocked_job_is_reported_and_only_reviewed_applicable_subset_is_applied(client, auth_headers, db_session):
    wo, _, _ = fixture_job(db_session, start=datetime(2026, 9, 7))
    blocked, blocked_op, center = fixture_job(db_session, start=datetime(2026, 9, 7))
    center.is_active = False
    db_session.commit()
    plan = preview(client, auth_headers, [wo.id, blocked.id])
    assert plan["summary"]["changed_jobs"] == plan["summary"]["blocked_jobs"] == 1
    assert "active work center" in plan["jobs"][1]["reason"]
    response = client.post(APPLY, headers=auth_headers, json={"plan_token": plan["plan_token"]})
    assert response.status_code == 200, response.text
    assert response.json()["applied_work_order_ids"] == [wo.id]
    db_session.expire_all()
    assert blocked_op.scheduled_start == datetime(2026, 9, 7)
