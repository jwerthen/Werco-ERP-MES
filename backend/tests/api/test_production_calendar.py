"""Production calendar & shifts (P1.2) on the database and over HTTP.

Werco's confirmed rules (tests/production_calendar_helpers.py): day start 05:00 Central,
weld W1 05:00-15:30 / W2 15:30-02:00 (600 min), rest of shop S1 07:30-16:00 / S2
15:30-00:00 (480 min), Mon-Fri. 2026-11-17 is a Tuesday; 2026-11-18 07:30Z is Wednesday
01:30 CST -- the brief's acceptance instant.

Covers the edit verb (CAS, whole-document validation, retire-not-delete, required audit,
RBAC, tenant isolation), resolve, OEE on production days and shifts (endpoint + cron),
the wallboard today pulse, the dashboard ETag, the working-calendar sync, and the
reports / downtime / job-cost / analytics call sites.
"""

import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.audit_log import AuditLog
from app.models.job_costing import CostEntry, JobCost
from app.models.oee import CalculationSource, OEERecord
from app.models.production_calendar import ProductionCalendarSettings, ShiftDefinition, ShiftPattern
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.models.working_calendar import WorkingCalendar
from app.services.audit_service import AuditService, AuditWriteError
from app.services.production_calendar import (
    current_production_day,
    production_day_start_utc,
    unconfigured_calendar,
)
from app.services.production_calendar_service import load_production_calendar
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    headers_for,
    make_downtime,
    make_entry,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)
from tests.production_calendar_helpers import seed_werco_calendar, shift_id_by_code, werco_payload

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

URL = "/api/v1/production-calendar"
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


def _put_body_from(read: dict, expected_version: int) -> dict:
    """Turn a GET body back into the PUT document (ids kept)."""
    return {
        "expected_version": expected_version,
        "production_day_start": read["production_day_start"],
        "patterns": [
            {
                "id": pattern["id"],
                "name": pattern["name"],
                "is_default": pattern["is_default"],
                "sort_order": pattern["sort_order"],
                "work_center_ids": pattern["work_center_ids"],
                "shifts": [
                    {
                        "id": shift["id"],
                        "code": shift["code"],
                        "name": shift["name"],
                        "start": shift["start"],
                        "end": shift["end"],
                        "days": shift["days"],
                        "planned_minutes": shift["planned_minutes"],
                        "is_active": shift["is_active"],
                        "sort_order": shift["sort_order"],
                    }
                    for shift in pattern["shifts"]
                ],
            }
            for pattern in read["patterns"]
        ],
    }


def _calendar_audit_rows(db, company_id=COMPANY_A):
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "production_calendar", AuditLog.company_id == company_id)
        .order_by(AuditLog.id)
        .all()
    )


# --------------------------------------------------------------------------------------
# Read / edit
# --------------------------------------------------------------------------------------


def test_unconfigured_calendar_reads_as_local_midnight_days_and_no_shifts(client: TestClient, db_session: Session):
    viewer = make_user(db_session, role=UserRole.OPERATOR)
    resp = client.get(URL, headers=headers_for(viewer))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is False
    assert body["version"] == 0
    assert body["timezone"] == "America/Chicago"
    assert body["production_day_start"] == "00:00"
    assert body["patterns"] == []
    assert body["today"]["production_day"] == datetime.now(CENTRAL_TIME_ZONE).date().isoformat()
    assert body["today"]["occurrences"] == []
    assert body["today"]["start_utc"].endswith("Z") and body["today"]["end_utc"].endswith("Z")


def test_admin_saves_werco_calendar_and_everyone_reads_it(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = [_welding(db_session).id, _welding(db_session).id]
    resp = client.put(URL, json=werco_payload(weld), headers=headers_for(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True and body["version"] == 1
    assert body["production_day_start"] == "05:00" and body["production_day_start_minute"] == 300
    patterns = {p["name"]: p for p in body["patterns"]}
    assert patterns["Weld shop"]["work_center_ids"] == sorted(weld)
    assert patterns["Rest of shop"]["is_default"] is True
    w2 = {s["code"]: s for s in patterns["Weld shop"]["shifts"]}["W2"]
    assert (w2["start"], w2["end"], w2["start_minute"], w2["end_minute"]) == ("15:30", "02:00", 930, 1560)
    assert w2["ends_next_day"] is True and w2["planned_minutes"] == 600 and w2["days"] == [0, 1, 2, 3, 4]
    for occ in body["today"]["occurrences"]:
        assert occ["code"] in {"W1", "W2", "S1", "S2"}
        assert occ["start_utc"].endswith("Z")

    operator = make_user(db_session, role=UserRole.OPERATOR)
    read = client.get(URL, headers=headers_for(operator)).json()
    assert read["version"] == 1 and len(read["patterns"]) == 2

    rows = _calendar_audit_rows(db_session)
    assert len(rows) == 1 and rows[0].action == "CREATE"
    assert rows[0].new_values["production_day_start_minute"] == 300


def test_put_is_compare_and_swap_and_an_identical_document_is_a_no_op(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = [_welding(db_session).id]
    assert client.put(URL, json=werco_payload(weld), headers=headers_for(admin)).status_code == 200

    stale = client.put(URL, json=werco_payload(weld), headers=headers_for(admin))
    assert stale.status_code == 409
    assert stale.json()["detail"] == "This production calendar changed. Reload it before saving."

    read = client.get(URL, headers=headers_for(admin)).json()
    same = client.put(URL, json=_put_body_from(read, 1), headers=headers_for(admin))
    assert same.status_code == 200 and same.json()["version"] == 1
    assert len(_calendar_audit_rows(db_session)) == 1  # no fake change

    edited = _put_body_from(read, 1)
    edited["patterns"][0]["shifts"][0]["planned_minutes"] = 570
    changed = client.put(URL, json=edited, headers=headers_for(admin))
    assert changed.status_code == 200 and changed.json()["version"] == 2
    rows = _calendar_audit_rows(db_session)
    assert [row.action for row in rows] == ["CREATE", "UPDATE"]
    assert rows[1].old_values["version"] == 1 and rows[1].new_values["version"] == 2


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda b: b["patterns"][0]["shifts"][0].update(start="04:00"), "must start at or after"),
        (lambda b: b["patterns"][0]["shifts"][1].update(end="06:00"), "must start at or after"),
        (lambda b: b["patterns"][1].update(is_default=False) or b["patterns"][0].update(is_default=True), None),
        (lambda b: b["patterns"][1]["work_center_ids"].append(b["patterns"][0]["work_center_ids"][0]), "at most one"),
        (lambda b: b["patterns"][0].update(is_default=True), "Only one shift pattern"),
        (lambda b: b["patterns"][1]["shifts"][0].update(code="W1"), "used twice"),
        (lambda b: b["patterns"][1]["shifts"][0].update(planned_minutes=600), "only 510 minutes long"),
        (lambda b: b["patterns"][0]["work_center_ids"].append(999999), "Unknown work center"),
        (lambda b: b["patterns"][1].update(name="weld SHOP"), "used twice"),
    ],
)
def test_whole_document_validation_refuses_before_writing(client: TestClient, db_session: Session, mutate, fragment):
    admin = _admin(db_session)
    body = werco_payload([_welding(db_session).id])
    mutate(body)
    resp = client.put(URL, json=body, headers=headers_for(admin))
    if fragment is None:  # moving the default between patterns is fine
        assert resp.status_code == 200, resp.text
        return
    assert resp.status_code == 422, resp.text
    assert fragment in str(resp.json()["detail"])
    assert db_session.query(ProductionCalendarSettings).count() == 0
    assert db_session.query(ShiftDefinition).count() == 0


def test_other_companys_work_center_is_unknown(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    foreign = _welding(db_session, company_id=COMPANY_B)
    resp = client.put(URL, json=werco_payload([foreign.id]), headers=headers_for(admin))
    assert resp.status_code == 422 and "Unknown work center" in resp.json()["detail"]


def test_a_referenced_shift_is_retired_never_removed_and_keeps_its_code(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    saved = client.put(URL, json=werco_payload([weld.id]), headers=headers_for(admin)).json()
    w1 = next(s for p in saved["patterns"] for s in p["shifts"] if s["code"] == "W1")
    record = client.post(
        "/api/v1/oee/records",
        json={"work_center_id": weld.id, "record_date": TUESDAY.isoformat(), "shift_id": w1["id"]},
        headers=headers_for(admin),
    )
    assert record.status_code == 200, record.text
    assert (record.json()["shift"], record.json()["shift_name"]) == ("W1", "Weld 1st")

    removed = _put_body_from(saved, 1)
    removed["patterns"][0]["shifts"] = [s for s in removed["patterns"][0]["shifts"] if s["code"] != "W1"]
    resp = client.put(URL, json=removed, headers=headers_for(admin))
    assert resp.status_code == 422 and "retire it" in resp.json()["detail"]

    recoded = _put_body_from(saved, 1)
    recoded["patterns"][0]["shifts"][0]["code"] = "X1"
    resp = client.put(URL, json=recoded, headers=headers_for(admin))
    assert resp.status_code == 422 and "cannot change" in resp.json()["detail"]

    retired = _put_body_from(saved, 1)
    next(s for s in retired["patterns"][0]["shifts"] if s["code"] == "W1")["is_active"] = False
    assert client.put(URL, json=retired, headers=headers_for(admin)).status_code == 200

    again = client.post(
        "/api/v1/oee/records",
        json={"work_center_id": weld.id, "record_date": WEDNESDAY.isoformat(), "shift_id": w1["id"]},
        headers=headers_for(admin),
    )
    assert again.status_code == 422 and "retired" in again.json()["detail"]
    kept = client.get(f"/api/v1/oee/records/{record.json()['id']}", headers=headers_for(admin)).json()
    assert (kept["shift"], kept["shift_id"], kept["shift_name"]) == ("W1", w1["id"], "Weld 1st")


def test_patterns_and_codes_can_be_swapped_and_removed_in_one_save(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    saved = client.put(URL, json=werco_payload([weld.id]), headers=headers_for(admin)).json()
    body = _put_body_from(saved, 1)
    weld_pattern, rest_pattern = body["patterns"]
    weld_pattern["name"], rest_pattern["name"] = rest_pattern["name"], weld_pattern["name"]
    weld_pattern["shifts"][0]["code"], weld_pattern["shifts"][1]["code"] = "W2", "W1"
    rest_pattern["shifts"] = rest_pattern["shifts"][:1]  # S2 removed
    resp = client.put(URL, json=body, headers=headers_for(admin))
    assert resp.status_code == 200, resp.text
    assert {s.code for s in db_session.query(ShiftDefinition).all()} == {"W1", "W2", "S1"}
    assert db_session.query(ShiftPattern).count() == 2


def test_audit_failure_is_503_and_nothing_is_saved(client: TestClient, db_session: Session, monkeypatch):
    admin = _admin(db_session)

    def refuse(self, *args, **kwargs):
        raise AuditWriteError("down")

    monkeypatch.setattr(AuditService, "log_required", refuse)
    resp = client.put(URL, json=werco_payload([_welding(db_session).id]), headers=headers_for(admin))
    assert resp.status_code == 503
    assert "nothing was changed" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.query(ProductionCalendarSettings).count() == 0
    assert db_session.query(ShiftPattern).count() == 0


@pytest.mark.parametrize("role", [UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER])
def test_only_admins_edit_the_calendar(client: TestClient, db_session: Session, role):
    user = make_user(db_session, role=role)
    resp = client.put(URL, json=werco_payload([]), headers=headers_for(user))
    assert resp.status_code == 403


def test_tenant_isolation(client: TestClient, db_session: Session):
    admin_a = _admin(db_session)
    weld_a = _welding(db_session)
    saved = client.put(URL, json=werco_payload([weld_a.id]), headers=headers_for(admin_a)).json()
    w2_a = next(s for p in saved["patterns"] for s in p["shifts"] if s["code"] == "W2")

    admin_b = _admin(db_session, COMPANY_B)
    read_b = client.get(URL, headers=headers_for(admin_b)).json()
    assert read_b["configured"] is False and read_b["patterns"] == []
    resolve = client.get(f"{URL}/resolve", params={"work_center_id": weld_a.id}, headers=headers_for(admin_b))
    assert resolve.status_code == 404

    wc_b = _welding(db_session, COMPANY_B)
    foreign = client.post(
        "/api/v1/oee/records",
        json={"work_center_id": wc_b.id, "record_date": TUESDAY.isoformat(), "shift_id": w2_a["id"]},
        headers=headers_for(admin_b),
    )
    assert foreign.status_code == 404
    calc = client.post(
        f"/api/v1/oee/calculate/{wc_b.id}", params={"shift_id": w2_a["id"]}, headers=headers_for(admin_b)
    )
    assert calc.status_code == 404
    # B saving its own calendar does not touch A's.
    assert client.put(URL, json=werco_payload([wc_b.id]), headers=headers_for(admin_b)).status_code == 200
    assert client.get(URL, headers=headers_for(admin_a)).json()["patterns"] == saved["patterns"]


# --------------------------------------------------------------------------------------
# Resolve
# --------------------------------------------------------------------------------------


def test_resolve_the_acceptance_instants(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    rest = make_work_center(db_session)
    saved = seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    viewer = make_user(db_session, role=UserRole.VIEWER)

    for at in ("2026-11-18T07:30:00Z", "2026-11-18T01:30:00-06:00", "2026-11-18T07:30:00"):
        body = client.get(
            f"{URL}/resolve", params={"at": at, "work_center_id": weld.id}, headers=headers_for(viewer)
        ).json()
        assert body["production_day"] == "2026-11-17", at
        assert body["shift"]["code"] == "W2" and body["shift"]["production_day"] == "2026-11-17"
        assert body["at"] == "2026-11-18T07:30:00Z"
        assert body["production_day_start_utc"] == "2026-11-17T11:00:00Z"

    overlap = client.get(
        f"{URL}/resolve", params={"at": "2026-11-17T21:45:00Z", "work_center_id": rest.id}, headers=headers_for(viewer)
    ).json()
    assert overlap["shift"]["code"] == "S1"  # outgoing shift wins the 15:30-16:00 hand-off
    assert [o["code"] for o in overlap["overlapping"]] == ["S1", "S2"]
    assert overlap["pattern_id"] == next(p.id for p in saved.patterns if p.is_default)


# --------------------------------------------------------------------------------------
# OEE on production days and shifts
# --------------------------------------------------------------------------------------


def _weld_day(db: Session):
    """Werco calendar + a weld WC with a W1 entry on Tuesday and a 01:30 Wednesday W2 entry."""
    admin = _admin(db)
    weld = _welding(db)
    saved = seed_werco_calendar(db, COMPANY_A, admin.id, [weld.id])
    part = make_part(db)
    wo = make_wo(db, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db, wo, weld, status_=OperationStatus.IN_PROGRESS, run_time_per_piece=0.05)
    make_entry(db, admin, wo, op, weld, clock_in=central(2026, 11, 17, 8, 0), duration_hours=2, quantity_produced=10)
    make_entry(db, admin, wo, op, weld, clock_in=WED_0130, duration_hours=0.5, quantity_produced=5)
    return admin, weld, saved


def test_0130_wednesday_weld_entry_counts_in_tuesdays_record_and_second_shift(client: TestClient, db_session: Session):
    admin, weld, saved = _weld_day(db_session)
    headers = headers_for(admin)
    url = f"/api/v1/oee/calculate/{weld.id}"

    tuesday = client.post(url, params={"record_date": TUESDAY.isoformat()}, headers=headers).json()
    assert tuesday["total_parts"] == 15
    assert tuesday["planned_production_time_minutes"] == pytest.approx(150.0)
    assert tuesday["shift"] is None and tuesday["shift_id"] is None

    wednesday = client.post(url, params={"record_date": WEDNESDAY.isoformat()}, headers=headers).json()
    assert wednesday["total_parts"] == 0

    w2_id = shift_id_by_code(saved, "W2")
    w2 = client.post(url, params={"record_date": TUESDAY.isoformat(), "shift_id": w2_id}, headers=headers).json()
    assert (w2["total_parts"], w2["shift"], w2["shift_id"], w2["shift_name"]) == (5, "W2", w2_id, "Weld 2nd")
    assert w2["planned_production_time_minutes"] == pytest.approx(30.0)
    w1 = client.post(
        url, params={"record_date": TUESDAY.isoformat(), "shift_id": shift_id_by_code(saved, "W1")}, headers=headers
    ).json()
    assert w1["total_parts"] == 10
    # Shift-scoped records partition the whole-day record.
    assert w1["total_parts"] + w2["total_parts"] == tuesday["total_parts"]
    assert w1["planned_production_time_minutes"] + w2["planned_production_time_minutes"] == pytest.approx(
        tuesday["planned_production_time_minutes"]
    )

    # The legacy text form of the same shift resolves to it and recomputes the same record.
    again = client.post(url, params={"record_date": TUESDAY.isoformat(), "shift": "W2"}, headers=headers)
    assert again.status_code == 200 and again.json()["id"] == w2["id"]
    # A rest-of-shop shift is not in the weld pattern.
    wrong = client.post(url, params={"shift_id": shift_id_by_code(saved, "S1")}, headers=headers)
    assert wrong.status_code == 422 and "pattern" in wrong.json()["detail"]
    listed = client.get("/api/v1/oee/records", params={"shift_id": w2_id}, headers=headers).json()
    assert [r["id"] for r in listed] == [w2["id"]]


def test_unknown_free_text_shift_is_refused_and_legacy_labels_stay_readable(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    legacy = OEERecord(work_center_id=weld.id, record_date=TUESDAY, shift="2nd", calculation_source="manual")
    legacy.company_id = COMPANY_A
    db_session.add(legacy)
    db_session.commit()

    refused = client.post(
        "/api/v1/oee/records",
        json={"work_center_id": weld.id, "record_date": WEDNESDAY.isoformat(), "shift": "1st"},
        headers=headers_for(admin),
    )
    assert refused.status_code == 422 and "Unknown shift '1st'" in refused.json()["detail"]
    rows = client.get("/api/v1/oee/records", params={"shift": "2nd"}, headers=headers_for(admin)).json()
    assert [(r["shift"], r["shift_id"], r["shift_name"]) for r in rows] == [("2nd", None, None)]
    by_code = client.post(
        "/api/v1/oee/records",
        json={"work_center_id": weld.id, "record_date": WEDNESDAY.isoformat(), "shift": "W1"},
        headers=headers_for(admin),
    )
    assert by_code.status_code == 200 and by_code.json()["shift_name"] == "Weld 1st"


def test_calculate_defaults_to_the_current_production_day(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    expected = current_production_day(load_production_calendar(db_session, COMPANY_A))
    body = client.post(f"/api/v1/oee/calculate/{weld.id}", headers=headers_for(admin)).json()
    assert body["record_date"] == expected.isoformat()


@pytest.fixture
def cron_session(db_session, monkeypatch):
    import app.jobs.oee_jobs as oee_jobs

    monkeypatch.setattr(oee_jobs, "SessionLocal", lambda: db_session)
    return db_session


async def test_cron_targets_the_previous_production_day_and_still_skips_manual(cron_session):
    from app.jobs.oee_jobs import run_oee_auto_calc_task
    from app.models.work_center import WorkCenter

    db = cron_session
    admin = _admin(db)
    weld = _welding(db)
    weld_id, admin_id = weld.id, admin.id
    seed_werco_calendar(db, COMPANY_A, admin.id, [weld.id])
    calendar = load_production_calendar(db, COMPANY_A)
    target = current_production_day(calendar) - timedelta(days=1)
    clock_in = production_day_start_utc(target, calendar) + timedelta(hours=3)
    make_entry(db, admin, None, None, weld, clock_in=clock_in, duration_hours=1, quantity_produced=3)

    result = await run_oee_auto_calc_task(company_id=COMPANY_A)
    assert result["record_date"] == target.isoformat()
    assert result["record_dates_by_company"] == {COMPANY_A: target.isoformat()}
    assert result["computed"] == 1
    record = db.query(OEERecord).filter(OEERecord.work_center_id == weld_id).one()
    assert record.record_date == target and record.calculation_source == CalculationSource.AUTO.value
    assert record.shift_id is None and record.total_parts == 3

    # A manual record for the day is still authoritative.
    db.query(OEERecord).delete()
    manual = OEERecord(work_center_id=weld_id, record_date=target, calculation_source="manual", oee_pct=12.0)
    manual.company_id = COMPANY_A
    db.add(manual)
    db.commit()
    assert db.get(WorkCenter, weld_id) is not None and admin_id
    rerun = await run_oee_auto_calc_task(company_id=COMPANY_A)
    assert rerun["skipped_manual"] == 1 and rerun["computed"] == 0


# --------------------------------------------------------------------------------------
# Wallboard today pulse + dashboard ETag
# --------------------------------------------------------------------------------------


def _seed_pulse_entries(db, company_id, weld, rest):
    user = make_user(db, company_id=company_id)
    # 04:00 Tuesday at a rest-of-shop WC: Monday's production day on a 05:00-start shop.
    make_entry(
        db,
        user,
        None,
        None,
        rest,
        company_id=company_id,
        clock_in=central(2026, 11, 17, 4, 0),
        duration_hours=0.5,
        quantity_produced=3,
    )
    make_entry(
        db,
        user,
        None,
        None,
        rest,
        company_id=company_id,
        clock_in=central(2026, 11, 17, 8, 0),
        duration_hours=1,
        quantity_produced=4,
    )
    # 04:55 early weld clock-in: mostly W1, so Tuesday.
    make_entry(
        db,
        user,
        None,
        None,
        weld,
        company_id=company_id,
        clock_in=central(2026, 11, 17, 4, 55),
        duration_hours=1,
        quantity_produced=2,
    )


def test_wallboard_today_pulse_reads_the_production_day(db_session: Session):
    from app.services.wallboard_service import _compute_today

    now = central(2026, 11, 17, 10, 0)
    admin = _admin(db_session)
    weld, rest = _welding(db_session), make_work_center(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    _seed_pulse_entries(db_session, COMPANY_A, weld, rest)
    assert _compute_today(db_session, COMPANY_A, now=now).pieces_completed == 6  # 04:00 entry excluded

    # Unconfigured company: the Central calendar day, so 04:00 counts.
    weld_b, rest_b = _welding(db_session, COMPANY_B), make_work_center(db_session, company_id=COMPANY_B)
    _seed_pulse_entries(db_session, COMPANY_B, weld_b, rest_b)
    assert _compute_today(db_session, COMPANY_B, now=now).pieces_completed == 9


def test_dashboard_etag_moves_with_the_production_day_and_the_calendar_version(db_session: Session, monkeypatch):
    import app.api.endpoints.shop_floor as shop_floor

    calendar = unconfigured_calendar()
    monkeypatch.setattr(shop_floor, "current_production_day", lambda cal, now=None: date(2026, 11, 16))
    before = shop_floor._dashboard_state_fingerprint(db_session, COMPANY_A, set(), {}, calendar)
    monkeypatch.setattr(shop_floor, "current_production_day", lambda cal, now=None: date(2026, 11, 17))
    after_rollover = shop_floor._dashboard_state_fingerprint(db_session, COMPANY_A, set(), {}, calendar)
    assert before != after_rollover
    edited = shop_floor._dashboard_state_fingerprint(
        db_session, COMPANY_A, set(), {}, dataclasses.replace(calendar, version=2)
    )
    assert edited != after_rollover


# --------------------------------------------------------------------------------------
# Working-calendar sync
# --------------------------------------------------------------------------------------


def test_sync_dry_run_writes_nothing_and_commit_writes_weekly_hours(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    weld_1, weld_2 = _welding(db_session), _welding(db_session)
    rest = make_work_center(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld_1.id, weld_2.id])
    # weld_1 already has a saved calendar with a dated override (through the scheduling editor).
    override = {"date": "2026-12-24", "hours": 4, "reason": "Christmas Eve"}
    put = client.put(
        f"/api/v1/scheduling/work-centers/{weld_1.id}/calendar",
        json={"expected_version": 0, "weekly_hours": [8] * 7, "overrides": [override]},
        headers=headers_for(supervisor),
    )
    assert put.status_code == 200, put.text
    audits_before = db_session.query(AuditLog).filter(AuditLog.resource_type == "working_calendar").count()

    dry = client.post(f"{URL}/working-calendars/sync", headers=headers_for(supervisor))
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dry_run"] is True and body["calendar_version"] == 1
    rows = {row["work_center_id"]: row for row in body["rows"]}
    assert rows[weld_1.id]["new_weekly_hours"] == [20, 20, 20, 20, 20, 0, 0]
    assert rows[weld_1.id]["current_weekly_hours"] == [8] * 7 and rows[weld_1.id]["overrides_kept"] == 1
    assert rows[rest.id]["new_weekly_hours"] == [16, 16, 16, 16, 16, 0, 0]
    assert rows[rest.id]["current_version"] == 0 and rows[rest.id]["changed"] is True
    db_session.expire_all()
    assert db_session.query(WorkingCalendar).count() == 1
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "working_calendar").count() == audits_before

    stale = client.post(
        f"{URL}/working-calendars/sync", params={"dry_run": "false", "expected_version": 7}, headers=headers_for(admin)
    )
    assert stale.status_code == 409

    commit = client.post(
        f"{URL}/working-calendars/sync", params={"dry_run": "false", "expected_version": 1}, headers=headers_for(admin)
    )
    assert commit.status_code == 200, commit.text
    changed = commit.json()["changed_count"]
    assert changed == len(commit.json()["rows"]) >= 3
    db_session.expire_all()
    saved_weld = db_session.query(WorkingCalendar).filter(WorkingCalendar.work_center_id == weld_1.id).one()
    assert saved_weld.weekly_hours == [20, 20, 20, 20, 20, 0, 0]
    assert saved_weld.overrides == [override] and saved_weld.version == 2
    saved_rest = db_session.query(WorkingCalendar).filter(WorkingCalendar.work_center_id == rest.id).one()
    assert saved_rest.weekly_hours == [16, 16, 16, 16, 16, 0, 0] and saved_rest.version == 1
    sync_audits = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "working_calendar")
        .order_by(AuditLog.id)
        .all()[audits_before:]
    )
    assert len(sync_audits) == changed
    assert all(row.extra_data.get("source") == "production_calendar_sync" for row in sync_audits)

    again = client.post(f"{URL}/working-calendars/sync", headers=headers_for(admin)).json()
    assert again["changed_count"] == 0


def test_sync_rbac_and_unconfigured_company(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    assert client.post(f"{URL}/working-calendars/sync", headers=headers_for(operator)).status_code == 403
    manager = make_user(db_session, role=UserRole.MANAGER)
    make_work_center(db_session)
    body = client.post(f"{URL}/working-calendars/sync", headers=headers_for(manager)).json()
    assert body["configured"] is False and body["rows"] == []


# --------------------------------------------------------------------------------------
# Reports, downtime, job cost, analytics
# --------------------------------------------------------------------------------------


def test_employee_time_dates_entries_by_production_day_and_names_the_shift(client: TestClient, db_session: Session):
    admin, weld, _ = _weld_day(db_session)
    tuesday = client.get(
        "/api/v1/reports/employee-time",
        params={"start_date": TUESDAY.isoformat(), "end_date": TUESDAY.isoformat()},
        headers=headers_for(admin),
    ).json()
    entries = {entry["clock_in"]: entry for emp in tuesday for entry in emp["entries"]}
    assert entries["2026-11-18T07:30:00Z"]["date"] == "2026-11-17"
    assert entries["2026-11-18T07:30:00Z"]["shift_code"] == "W2"
    assert entries["2026-11-17T14:00:00Z"]["shift_code"] == "W1"
    wednesday = client.get(
        "/api/v1/reports/employee-time",
        params={"start_date": WEDNESDAY.isoformat(), "end_date": WEDNESDAY.isoformat()},
        headers=headers_for(admin),
    ).json()
    assert [e for emp in wednesday for e in emp["entries"]] == []


def test_daily_output_buckets_by_production_day(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    calendar = load_production_calendar(db_session, COMPANY_A)
    day = current_production_day(calendar) - timedelta(days=2)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, weld, status_=OperationStatus.COMPLETE)
    # 01:30 the next calendar morning: still production day ``day``.
    op.actual_end = production_day_start_utc(day, calendar) + timedelta(hours=20, minutes=30)
    op.quantity_complete = 7
    db_session.commit()
    rows = client.get("/api/v1/reports/daily-output", params={"days": 5}, headers=headers_for(admin)).json()
    by_date = {row["date"]: row["completed"] for row in rows}
    assert by_date[day.isoformat()] == 7
    assert by_date[(day + timedelta(days=1)).isoformat()] == 0


def test_downtime_filters_read_production_days(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    event = make_downtime(db_session, admin, weld, start_time=WED_0130, duration_minutes=20)

    def ids(day):
        resp = client.get("/api/v1/downtime/", params={"date_from": day, "date_to": day}, headers=headers_for(admin))
        assert resp.status_code == 200, resp.text
        return [row["id"] for row in resp.json()]

    assert ids(TUESDAY.isoformat()) == [event.id]
    assert ids(WEDNESDAY.isoformat()) == []
    reversed_range = client.get(
        "/api/v1/downtime/", params={"date_from": "2026-11-18", "date_to": "2026-11-17"}, headers=headers_for(admin)
    )
    assert reversed_range.status_code == 422


def test_job_cost_labor_entry_date_is_the_production_day(db_session: Session):
    from app.models.time_entry import TimeEntry
    from app.services.job_costing_service import recompute_from_time_entries

    admin, _weld, _ = _weld_day(db_session)
    entry = db_session.query(TimeEntry).filter(TimeEntry.clock_in == WED_0130).one()
    job_cost = JobCost(work_order_id=entry.work_order_id, company_id=COMPANY_A)
    db_session.add(job_cost)
    db_session.flush()
    recompute_from_time_entries(db_session, job_cost=job_cost, company_id=COMPANY_A, user_id=admin.id)
    db_session.commit()
    dates = {row.reference: row.entry_date for row in db_session.query(CostEntry).all()}
    assert dates[f"TE-{entry.id}"] == TUESDAY


def test_analytics_oee_reads_production_days_and_agrees_with_the_record(client: TestClient, db_session: Session):
    from app.api.date_window import company_period
    from app.services.analytics_service import AnalyticsService

    admin, weld, _ = _weld_day(db_session)
    service = AnalyticsService(db_session, COMPANY_A)
    tuesday = service._get_oee_value(TUESDAY, TUESDAY, weld.id)
    assert tuesday is not None
    assert service._get_oee_value(WEDNESDAY, WEDNESDAY, weld.id) is None
    record = client.post(
        f"/api/v1/oee/calculate/{weld.id}", params={"record_date": TUESDAY.isoformat()}, headers=headers_for(admin)
    ).json()
    assert tuesday == pytest.approx(record["oee_pct"], abs=0.01)
    assert service._get_oee_sparkline(TUESDAY, WEDNESDAY, weld.id)[1] == 0.0

    start, end, _zone = company_period(db_session, COMPANY_A, "today", None, None)
    assert start == end == current_production_day(load_production_calendar(db_session, COMPANY_A))


def test_today_lists_every_active_shift_of_every_pattern_as_utc_instants(db_session: Session):
    from app.schemas.production_calendar import ProductionCalendarUpdate
    from app.services.production_calendar_service import calendar_response, save_production_calendar

    admin = _admin(db_session)
    body = werco_payload([_welding(db_session).id])
    body["patterns"].append(
        {
            "name": "Paint (no work centers yet)",
            "shifts": [
                {"code": "P1", "name": "Paint", "start": "06:00", "end": "14:00", "days": [1], "planned_minutes": 450},
                {
                    "code": "P0",
                    "name": "Retired",
                    "start": "06:00",
                    "end": "07:00",
                    "days": [1],
                    "planned_minutes": 60,
                    "is_active": False,
                },
            ],
        }
    )
    audit = AuditService(db_session, user=None, company_id=COMPANY_A)
    save_production_calendar(db_session, COMPANY_A, admin.id, ProductionCalendarUpdate.model_validate(body), audit)
    db_session.commit()

    today = calendar_response(db_session, COMPANY_A, now=central(2026, 11, 17, 10, 0)).today
    assert today.production_day == TUESDAY
    assert (today.start_utc, today.end_utc) == (datetime(2026, 11, 17, 11, 0), datetime(2026, 11, 18, 11, 0))
    assert [occ.code for occ in today.occurrences] == ["W1", "P1", "S1", "W2", "S2"]
    w2 = next(occ for occ in today.occurrences if occ.code == "W2")
    assert (w2.start_utc, w2.end_utc) == (datetime(2026, 11, 17, 21, 30), datetime(2026, 11, 18, 8, 0))
    # Saturday: nothing runs.
    assert calendar_response(db_session, COMPANY_A, now=central(2026, 11, 21, 10, 0)).today.occurrences == []
