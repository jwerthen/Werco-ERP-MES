"""Production calendar (P1.2): the gaps the test-engineer gate found untested.

Each test here would pass against a plausible broken implementation of the code it covers
if it were missing, which is why it was added:

* **Cross-tenant WRITE through ids.** ``PUT /production-calendar`` keeps rows by ``id``. Company
  B submitting company A's pattern / shift ids must be a 422 that leaves A's rows exactly as
  they were -- a resolve by bare id (``db.get``) would let B re-parent or rename A's shifts.
* **The sync's required audit.** ``dry_run=false`` writes one REQUIRED audit row per work
  center; when one cannot be written the whole sync is a 503 and NO working calendar is saved.
* **The sync is tenant-confined** and covers active work centers only.
* **OEE record UPDATE shift rules** (only CREATE was covered): a record keeps (and may re-send)
  its own shift after that shift is retired, editing other fields never re-validates the shift,
  but moving it to a retired / other-pattern / foreign shift is refused without touching it.
* **The shop-floor dashboard's ``completed_today``** reads the PRODUCTION day (05:00 start), not
  Central midnight -- the payload itself, not only the ETag fingerprint.
* **Downtime summary / by-work-center** read production days like the list does.
"""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.audit_log import AuditLog
from app.models.oee import OEERecord
from app.models.production_calendar import ProductionCalendarSettings, ShiftDefinition, ShiftPattern
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.models.working_calendar import WorkingCalendar
from app.services.audit_service import AuditService, AuditWriteError
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    headers_for,
    make_downtime,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)
from tests.production_calendar_helpers import seed_werco_calendar, shift_id_by_code, werco_payload

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

URL = "/api/v1/production-calendar"
SYNC_URL = f"{URL}/working-calendars/sync"
TUESDAY = date(2026, 11, 17)
WEDNESDAY = date(2026, 11, 18)
WED_0130 = datetime(2026, 11, 18, 7, 30)  # Wednesday 01:30 CST as naive UTC


def central(year, month, day, hour, minute=0):
    return (
        datetime(year, month, day, hour, minute, tzinfo=CENTRAL_TIME_ZONE).astimezone(timezone.utc).replace(tzinfo=None)
    )


def _admin(db, company_id=COMPANY_A):
    return make_user(db, role=UserRole.ADMIN, company_id=company_id)


def _welding(db, company_id=COMPANY_A):
    return make_work_center(db, company_id=company_id, work_center_type="welding")


def _shift_rows(db, company_id):
    return sorted(
        (row.id, row.shift_pattern_id, row.code, row.name, row.start_minute, row.end_minute, row.planned_minutes)
        for row in db.query(ShiftDefinition).filter(ShiftDefinition.company_id == company_id).all()
    )


def _pattern_rows(db, company_id):
    return sorted(
        (row.id, row.name, bool(row.is_default))
        for row in db.query(ShiftPattern).filter(ShiftPattern.company_id == company_id).all()
    )


# --------------------------------------------------------------------------------------
# Cross-tenant writes through ids
# --------------------------------------------------------------------------------------


def test_another_company_cannot_adopt_rename_or_move_this_companys_patterns_and_shifts(
    client: TestClient, db_session: Session
):
    admin_a = _admin(db_session)
    weld_a = _welding(db_session)
    saved_a = client.put(URL, json=werco_payload([weld_a.id]), headers=headers_for(admin_a))
    assert saved_a.status_code == 200, saved_a.text
    a_patterns = saved_a.json()["patterns"]
    shifts_before, patterns_before = _shift_rows(db_session, COMPANY_A), _pattern_rows(db_session, COMPANY_A)
    audits_before = db_session.query(AuditLog).filter(AuditLog.resource_type == "production_calendar").count()

    admin_b = _admin(db_session, COMPANY_B)
    headers_b = headers_for(admin_b)

    # 1. B submits A's pattern AND shift ids verbatim (renamed), with no work centers.
    hijack = {
        "expected_version": 0,
        "production_day_start": "05:00",
        "patterns": [
            {
                "id": pattern["id"],
                "name": f"B owns {pattern['name']}",
                "is_default": pattern["is_default"],
                "work_center_ids": [],
                "shifts": [
                    {
                        "id": shift["id"],
                        "code": f"B{shift['code']}",
                        "name": "hijacked",
                        "start": shift["start"],
                        "end": shift["end"],
                        "days": shift["days"],
                        "planned_minutes": shift["planned_minutes"],
                    }
                    for shift in pattern["shifts"]
                ],
            }
            for pattern in a_patterns
        ],
    }
    resp = client.put(URL, json=hijack, headers=headers_b)
    assert resp.status_code == 422, resp.text
    assert "not found" in resp.json()["detail"]

    # 2. A NEW pattern of B's claiming one of A's shift ids (would re-parent it into B's pattern).
    a_w2 = next(s for p in a_patterns for s in p["shifts"] if s["code"] == "W2")
    move = {
        "expected_version": 0,
        "production_day_start": "05:00",
        "patterns": [
            {
                "name": "B plant",
                "is_default": True,
                "shifts": [
                    {
                        "id": a_w2["id"],
                        "code": "X2",
                        "name": "moved",
                        "start": "15:30",
                        "end": "02:00",
                        "days": [0, 1, 2, 3, 4],
                        "planned_minutes": 600,
                    }
                ],
            }
        ],
    }
    resp = client.put(URL, json=move, headers=headers_b)
    assert resp.status_code == 422, resp.text
    assert f"Shift {a_w2['id']} not found" in resp.json()["detail"]

    # Nothing of A's moved, and B has no calendar at all.
    db_session.expire_all()
    assert _shift_rows(db_session, COMPANY_A) == shifts_before
    assert _pattern_rows(db_session, COMPANY_A) == patterns_before
    assert db_session.query(ProductionCalendarSettings).filter_by(company_id=COMPANY_B).count() == 0
    assert db_session.query(ShiftDefinition).filter_by(company_id=COMPANY_B).count() == 0
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "production_calendar").count() == audits_before
    assert client.get(URL, headers=headers_for(admin_a)).json()["patterns"] == a_patterns


# --------------------------------------------------------------------------------------
# Working-calendar sync: required audit, tenancy, active centers only
# --------------------------------------------------------------------------------------


def test_sync_commit_audit_failure_is_503_and_saves_no_working_calendar(
    client: TestClient, db_session: Session, monkeypatch
):
    admin = _admin(db_session)
    weld = _welding(db_session)
    rest = make_work_center(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    preview = client.post(SYNC_URL, headers=headers_for(admin)).json()
    assert preview["changed_count"] >= 2

    def refuse(self, *args, **kwargs):
        raise AuditWriteError("audit store down")

    monkeypatch.setattr(AuditService, "log_required", refuse)
    resp = client.post(
        SYNC_URL,
        params={"dry_run": "false", "expected_version": preview["calendar_version"]},
        headers=headers_for(admin),
    )
    assert resp.status_code == 503, resp.text
    assert "nothing was changed" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.query(WorkingCalendar).filter(WorkingCalendar.work_center_id.in_([weld.id, rest.id])).count() == 0
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "working_calendar").count() == 0


def test_sync_is_tenant_confined_and_skips_inactive_work_centers(client: TestClient, db_session: Session):
    admin_a = _admin(db_session)
    weld_a = _welding(db_session)
    rest_a = make_work_center(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin_a.id, [weld_a.id])

    admin_b = _admin(db_session, COMPANY_B)
    weld_b = _welding(db_session, COMPANY_B)
    rest_b = make_work_center(db_session, company_id=COMPANY_B)
    retired_b = make_work_center(db_session, company_id=COMPANY_B)
    retired_b.is_active = False
    db_session.commit()
    seed_werco_calendar(db_session, COMPANY_B, admin_b.id, [weld_b.id])

    dry = client.post(SYNC_URL, headers=headers_for(admin_b)).json()
    rows = {row["work_center_id"]: row for row in dry["rows"]}
    assert set(rows) == {weld_b.id, rest_b.id}, "only B's ACTIVE work centers"
    assert rows[weld_b.id]["new_weekly_hours"] == [20, 20, 20, 20, 20, 0, 0]

    commit = client.post(
        SYNC_URL, params={"dry_run": "false", "expected_version": dry["calendar_version"]}, headers=headers_for(admin_b)
    )
    assert commit.status_code == 200, commit.text
    db_session.expire_all()
    written = {row.work_center_id for row in db_session.query(WorkingCalendar).all()}
    assert written == {weld_b.id, rest_b.id}
    assert not written & {weld_a.id, rest_a.id, retired_b.id}
    assert all(
        row.company_id == COMPANY_B
        for row in db_session.query(AuditLog).filter(AuditLog.resource_type == "working_calendar").all()
    )


# --------------------------------------------------------------------------------------
# OEE record UPDATE and its shift
# --------------------------------------------------------------------------------------


def _record_url(record_id):
    return f"/api/v1/oee/records/{record_id}"


def test_oee_record_update_keeps_a_retired_shift_and_refuses_bad_moves(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    headers = headers_for(admin)
    weld = _welding(db_session)
    saved = seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    w1, w2, s1 = (shift_id_by_code(saved, code) for code in ("W1", "W2", "S1"))
    created = client.post(
        "/api/v1/oee/records",
        json={"work_center_id": weld.id, "record_date": TUESDAY.isoformat(), "shift_id": w1},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    record_id = created.json()["id"]

    # Retire W1 and W2 through the calendar editor.
    read = client.get(URL, headers=headers).json()
    for pattern in read["patterns"]:
        for shift in pattern["shifts"]:
            shift["is_active"] = shift["code"] not in {"W1", "W2"}
    body = {
        "expected_version": read["version"],
        "production_day_start": read["production_day_start"],
        "patterns": [
            {
                "id": p["id"],
                "name": p["name"],
                "is_default": p["is_default"],
                "sort_order": p["sort_order"],
                "work_center_ids": p["work_center_ids"],
                "shifts": [
                    {k: s[k] for k in ("id", "code", "name", "start", "end", "days", "planned_minutes", "is_active")}
                    for s in p["shifts"]
                ],
            }
            for p in read["patterns"]
        ],
    }
    assert client.put(URL, json=body, headers=headers).status_code == 200

    # Editing other fields never re-validates the (now retired) shift.
    edited = client.put(_record_url(record_id), json={"planned_production_time_minutes": 300}, headers=headers)
    assert edited.status_code == 200, edited.text
    assert (edited.json()["shift_id"], edited.json()["shift"]) == (w1, "W1")
    # Re-sending the record's own retired shift is allowed.
    resent = client.put(_record_url(record_id), json={"shift_id": w1, "notes": "kept"}, headers=headers)
    assert resent.status_code == 200, resent.text
    assert resent.json()["shift_name"] == "Weld 1st"

    # Moving to ANOTHER retired shift, a shift of another pattern, or a foreign shift is refused.
    to_retired = client.put(_record_url(record_id), json={"shift_id": w2}, headers=headers)
    assert to_retired.status_code == 422 and "retired" in to_retired.json()["detail"]
    other_pattern = client.put(_record_url(record_id), json={"shift_id": s1}, headers=headers)
    assert other_pattern.status_code == 422 and "pattern" in other_pattern.json()["detail"]

    admin_b = _admin(db_session, COMPANY_B)
    weld_b = _welding(db_session, COMPANY_B)
    saved_b = seed_werco_calendar(db_session, COMPANY_B, admin_b.id, [weld_b.id])
    foreign = client.put(_record_url(record_id), json={"shift_id": shift_id_by_code(saved_b, "W1")}, headers=headers)
    assert foreign.status_code == 404, foreign.text
    unknown_label = client.put(_record_url(record_id), json={"shift": "Graveyard"}, headers=headers)
    assert unknown_label.status_code == 422 and "Unknown shift" in unknown_label.json()["detail"]

    db_session.expire_all()
    row = db_session.get(OEERecord, record_id)
    assert (row.shift_id, row.shift) == (w1, "W1"), "a refused move leaves the record's shift alone"

    # An explicit null makes it a whole-production-day record again.
    cleared = client.put(_record_url(record_id), json={"shift_id": None}, headers=headers)
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["shift_id"], cleared.json()["shift"], cleared.json()["shift_name"]) == (None, None, None)


# --------------------------------------------------------------------------------------
# Shop-floor dashboard completed_today
# --------------------------------------------------------------------------------------


# Tuesday 2026-11-17 10:00 CST. Werco's production day started at 05:00 CST (11:00Z);
# Central midnight was 06:00Z.
FROZEN_NOW = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return FROZEN_NOW.astimezone(tz)
        return FROZEN_NOW.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return FROZEN_NOW.replace(tzinfo=None)


def _completed_ops(db, company_id):
    """One closed-out job with four completed operations around the production-day start."""
    part = make_part(db, company_id=company_id)
    wc = make_work_center(db, company_id=company_id)
    wo = make_wo(db, part, company_id=company_id, status_=WorkOrderStatus.COMPLETE)
    ends = [
        central(2026, 11, 16, 23, 0),  # Monday night: neither window
        central(2026, 11, 17, 4, 30),  # after Central midnight, BEFORE the 05:00 production-day start
        central(2026, 11, 17, 6, 0),  # inside the production day
        central(2026, 11, 17, 11, 0),  # after "now"
    ]
    for index, end in enumerate(ends):
        make_op(
            db,
            wo,
            wc,
            company_id=company_id,
            sequence=10 * (index + 1),
            status_=OperationStatus.COMPLETE,
            quantity_complete=10,
            actual_end=end,
        )


def test_dashboard_completed_today_counts_the_production_day_not_central_midnight(
    client: TestClient, db_session: Session, monkeypatch
):
    import app.api.endpoints.shop_floor as shop_floor

    admin_a = _admin(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin_a.id, [])
    _completed_ops(db_session, COMPANY_A)
    admin_b = _admin(db_session, COMPANY_B)  # unconfigured: the Central calendar day
    _completed_ops(db_session, COMPANY_B)

    monkeypatch.setattr(shop_floor, "datetime", _FrozenDateTime)
    configured = client.get("/api/v1/shop-floor/dashboard", headers=headers_for(admin_a))
    assert configured.status_code == 200, configured.text
    assert configured.json()["summary"]["completed_today"] == 1, "04:30 belongs to Monday's production day"

    unconfigured = client.get("/api/v1/shop-floor/dashboard", headers=headers_for(admin_b))
    assert unconfigured.status_code == 200, unconfigured.text
    assert unconfigured.json()["summary"]["completed_today"] == 2, "Central midnight -> now"


# --------------------------------------------------------------------------------------
# Downtime summary / by-work-center read production days
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/downtime/summary", "/api/v1/downtime/by-work-center"])
def test_downtime_aggregates_read_production_days(client: TestClient, db_session: Session, path):
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    make_downtime(db_session, admin, weld, start_time=WED_0130, duration_minutes=30)

    def events(day):
        resp = client.get(path, params={"date_from": day, "date_to": day}, headers=headers_for(admin))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return body["event_count"] if isinstance(body, dict) else sum(row["event_count"] for row in body)

    assert events(TUESDAY.isoformat()) == 1, "01:30 Wednesday is Tuesday's production day"
    assert events(WEDNESDAY.isoformat()) == 0
    # The same instant on an unconfigured company is its Central calendar day (Wednesday).
    admin_b = _admin(db_session, COMPANY_B)
    weld_b = _welding(db_session, COMPANY_B)
    make_downtime(db_session, admin_b, weld_b, start_time=WED_0130, duration_minutes=30, company_id=COMPANY_B)

    def events_b(day):
        resp = client.get(path, params={"date_from": day, "date_to": day}, headers=headers_for(admin_b))
        body = resp.json()
        return body["event_count"] if isinstance(body, dict) else sum(row["event_count"] for row in body)

    assert (events_b(TUESDAY.isoformat()), events_b(WEDNESDAY.isoformat())) == (0, 1)


# --------------------------------------------------------------------------------------
# Nightly OEE cron: one target PER COMPANY, on that company's own production calendar
# --------------------------------------------------------------------------------------


async def test_oee_cron_targets_each_companys_own_previous_production_day(db_session: Session, monkeypatch):
    """At Tuesday 03:00 CST a 05:00-start shop is still in MONDAY's production day (target:
    Sunday) while an unconfigured shop is on the Central calendar day Tuesday (target: Monday).
    One date for every company -- the pre-120 ``date.today() - 1`` shape -- fails this."""
    import app.jobs.oee_jobs as oee_jobs
    from app.services.production_calendar import current_production_day as real_current_production_day

    tuesday_0300 = central(2026, 11, 17, 3, 0)
    monkeypatch.setattr(oee_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(oee_jobs, "current_production_day", lambda cal: real_current_production_day(cal, tuesday_0300))

    admin_a = _admin(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin_a.id, [])
    _admin(db_session, COMPANY_B)  # company B exists and has no calendar

    result = await oee_jobs.run_oee_auto_calc_task()
    assert result["record_dates_by_company"][COMPANY_A] == "2026-11-15"
    assert result["record_dates_by_company"][COMPANY_B] == "2026-11-16"
    assert result["record_date"] is None, "no single date describes a run whose companies differ"

    # An explicit record_date still overrides every company's default.
    rerun = await oee_jobs.run_oee_auto_calc_task(record_date=date(2026, 11, 10))
    assert rerun["record_date"] == "2026-11-10"
    assert {rerun["record_dates_by_company"][cid] for cid in (COMPANY_A, COMPANY_B)} == {"2026-11-10"}


# --------------------------------------------------------------------------------------
# Field-level refusals (schema) write nothing either
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda b: b["patterns"][0]["shifts"][0].update(days=[]), id="no-days"),
        pytest.param(lambda b: b["patterns"][0]["shifts"][0].update(days=[0, 0]), id="duplicate-day"),
        pytest.param(lambda b: b["patterns"][0]["shifts"][0].update(days=[7]), id="day-out-of-range"),
        pytest.param(lambda b: b["patterns"][0]["shifts"][0].update(start="24:00"), id="bad-wall-time"),
        pytest.param(lambda b: b["patterns"][0]["shifts"][0].update(planned_minutes=0), id="zero-planned"),
        pytest.param(lambda b: b["patterns"][0]["shifts"][0].update(code="   "), id="blank-code"),
        pytest.param(lambda b: b.update(production_day_start="5:00"), id="bad-day-start"),
        pytest.param(lambda b: b.update(expected_version=-1), id="negative-version"),
    ],
)
def test_malformed_documents_are_422_and_write_nothing(client: TestClient, db_session: Session, mutate):
    admin = _admin(db_session)
    body = werco_payload([_welding(db_session).id])
    mutate(body)
    resp = client.put(URL, json=body, headers=headers_for(admin))
    assert resp.status_code == 422, resp.text
    assert db_session.query(ProductionCalendarSettings).count() == 0
    assert db_session.query(ShiftDefinition).count() == 0
