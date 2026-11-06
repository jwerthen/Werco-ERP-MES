"""Calibration records save, are scoped, and a FAILED calibration takes the gauge out of service.

``api/endpoints/calibration.py`` had four defects that compounded:

* **Recording a calibration could not save.** ``CalibrationRecord`` carries
  ``TenantMixin.company_id`` (NOT NULL; migration 026 drops the interim default) and
  ``record_calibration`` built one without it -- every ``POST .../calibrate`` was a 500.
* **Had it saved, a failed calibration would have RE-CERTIFIED the gauge.** The handler set
  ``status = ACTIVE`` and advanced ``next_calibration_date`` whatever ``result`` said, and the
  kiosk gauge gate (``process_sheet_service._validate_gauge``) trusts exactly those two fields.
  §2 pins the end-to-end consequence: after a ``fail`` the floor's measurement capture refuses
  the gauge.
* **Any user could put a gauge back in service.** ``PUT /equipment/{id}`` was a bare
  ``get_current_user`` with a blind setattr over ``status``, so a Viewer could flip an
  OUT_OF_SERVICE gauge to ACTIVE with no calibration and no audit row.
* **The history read was cross-tenant.** ``GET /equipment/{id}/history`` took no company.

Every write case asserts the STORED rows and the audit-row count after the call, not just the
status code: a handler that refuses and mutates anyway would pass a status-only test.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query, Session

from app.models.audit_log import AuditLog
from app.models.calibration import CalibrationRecord, CalibrationStatus, Equipment
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, ensure_company, make_user, user_headers
from tests.api.test_process_sheet_integrations import RECORDS_URL, _add_step, _fixture

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

EQUIPMENT_URL = "/api/v1/calibration/equipment"
FOREIGN_CERT = "ACME-SECRET-CERT-7731"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def calibrate_url(equipment_id: int) -> str:
    return f"{EQUIPMENT_URL}/{equipment_id}/calibrate"


def make_gauge(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    status: CalibrationStatus = CalibrationStatus.ACTIVE,
    last_cal: date = None,
    next_cal: date = None,
    interval: int = 365,
) -> Equipment:
    gauge = Equipment(
        equipment_id=f"CAL-INT-{_next():05d}",
        name="Outside micrometer",
        status=status,
        last_calibration_date=last_cal,
        next_calibration_date=next_cal,
        calibration_interval_days=interval,
        company_id=company_id,
    )
    db.add(gauge)
    db.commit()
    db.refresh(gauge)
    return gauge


def make_record(db: Session, gauge: Equipment, *, cal_date: date, result: str = "pass") -> CalibrationRecord:
    record = CalibrationRecord(
        equipment_id=gauge.id,
        calibration_date=cal_date,
        due_date=cal_date + timedelta(days=gauge.calibration_interval_days),
        result=result,
        certificate_number=FOREIGN_CERT,
        company_id=gauge.company_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def audit_count(db: Session) -> int:
    return db.query(AuditLog).count()


def record_count(db: Session, gauge: Equipment) -> int:
    return db.query(CalibrationRecord).filter(CalibrationRecord.equipment_id == gauge.id).count()


def quality_headers(db: Session, company_id: int = COMPANY_A, role: UserRole = UserRole.QUALITY) -> dict:
    return user_headers(make_user(db, company_id=company_id, role=role))


# ---------------------------------------------------------------------------
# 1. Recording a calibration saves, is scoped, and its result decides the gauge's state
# ---------------------------------------------------------------------------


class TestRecordCalibration:
    def test_pass_saves_a_scoped_record_and_certifies_the_gauge(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE, interval=180)
        cal_date = date.today()

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": cal_date.isoformat(), "result": "pass", "certificate_number": "C-1"},
            headers=quality_headers(db_session),
        )

        # Was a NOT NULL 500 before company_id was stamped.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["equipment_status"] == "active"
        assert body["applied_to_equipment"] is True
        record = db_session.query(CalibrationRecord).filter(CalibrationRecord.id == body["id"]).one()
        assert record.company_id == COMPANY_A
        assert record.result == "pass"
        assert record.due_date == cal_date + timedelta(days=180)
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE
        assert gauge.last_calibration_date == cal_date
        assert gauge.next_calibration_date == cal_date + timedelta(days=180)

        created = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "calibration", AuditLog.resource_id == record.id)
            .one()
        )
        assert created.action == "CREATE"
        assert created.company_id == COMPANY_A
        status_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "equipment", AuditLog.resource_id == gauge.id)
            .one()
        )
        assert status_row.action == "STATUS_CHANGE"
        assert status_row.old_values == {"status": "out_of_service"}
        assert status_row.new_values == {"status": "active"}

    def test_adjusted_certifies_like_a_pass(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "adjusted"},
            headers=quality_headers(db_session),
        )
        assert response.status_code == 200, response.text
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE
        assert gauge.next_calibration_date == date.today() + timedelta(days=365)

    @pytest.mark.parametrize("word", ["fail", "FAIL", " Fail "])
    def test_fail_takes_the_gauge_out_of_service_without_extending_its_due_date(
        self, client: TestClient, db_session: Session, word: str
    ):
        last_cal = date.today() - timedelta(days=100)
        next_cal = date.today() + timedelta(days=265)
        gauge = make_gauge(db_session, last_cal=last_cal, next_cal=next_cal)
        cal_date = date.today()

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": cal_date.isoformat(), "result": word, "as_found": "+0.0004 high"},
            headers=quality_headers(db_session),
        )

        assert response.status_code == 200, response.text
        assert response.json()["equipment_status"] == "out_of_service"
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        # Neither date moves: a failed calibration never extends certification.
        assert gauge.last_calibration_date == last_cal
        assert gauge.next_calibration_date == next_cal
        record = db_session.query(CalibrationRecord).filter(CalibrationRecord.equipment_id == gauge.id).one()
        assert record.result == "fail"
        assert record.due_date == cal_date  # due for recalibration now
        status_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "equipment", AuditLog.resource_id == gauge.id)
            .one()
        )
        assert status_row.action == "STATUS_CHANGE"
        assert status_row.new_values == {"status": "out_of_service"}

    @pytest.mark.parametrize("word", ["limited", "passed", "", "ok"])
    def test_unknown_result_is_422_and_writes_nothing(self, client: TestClient, db_session: Session, word: str):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session)
        before = audit_count(db_session)

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": word},
            headers=headers,
        )

        assert response.status_code == 422
        assert record_count(db_session, gauge) == 0
        assert audit_count(db_session) == before
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE

    def test_backdated_pass_is_filed_but_cannot_recertify_a_gauge_that_failed_since(
        self, client: TestClient, db_session: Session
    ):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        make_record(db_session, gauge, cal_date=date.today() - timedelta(days=5), result="fail")

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": (date.today() - timedelta(days=60)).isoformat(), "result": "pass"},
            headers=quality_headers(db_session),
        )

        assert response.status_code == 200, response.text
        assert response.json()["applied_to_equipment"] is False
        assert record_count(db_session, gauge) == 2
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        assert gauge.next_calibration_date is None

    def test_a_retired_gauge_stays_retired(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session, status=CalibrationStatus.RETIRED)
        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "pass"},
            headers=quality_headers(db_session),
        )
        assert response.status_code == 200, response.text
        assert response.json()["applied_to_equipment"] is False
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.RETIRED
        assert gauge.next_calibration_date is None

    def test_lost_audit_evidence_rolls_the_whole_record_back(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session)
        before = audit_count(db_session)
        monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "pass"},
            headers=headers,
        )

        assert response.status_code == 503
        db_session.expire_all()
        assert record_count(db_session, gauge) == 0
        assert audit_count(db_session) == before
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        assert gauge.next_calibration_date is None


# ---------------------------------------------------------------------------
# 2. End to end: a FAILED calibration makes the kiosk gauge check refuse
# ---------------------------------------------------------------------------


class TestFailedCalibrationReachesTheFloor:
    def test_failed_calibration_makes_the_kiosk_gauge_check_refuse(self, client: TestClient, db_session: Session):
        _, operation, _, floor_headers = _fixture(db_session)
        first_step = _add_step(db_session, operation, requires_gauge=True)
        second_step = _add_step(db_session, operation, requires_gauge=True)
        office = quality_headers(db_session)

        created = client.post(
            EQUIPMENT_URL, json={"equipment_id": f"KIOSK-G-{_next()}", "name": "Bore gauge"}, headers=office
        )
        assert created.status_code == 200, created.text
        gauge_id = created.json()["id"]

        passed = client.post(
            calibrate_url(gauge_id),
            json={"calibration_date": date.today().isoformat(), "result": "pass"},
            headers=office,
        )
        assert passed.status_code == 200, passed.text
        captured = client.post(
            RECORDS_URL.format(op=operation.id, step=first_step.id),
            json={"value_numeric": 1.0, "equipment_id": gauge_id},
            headers=floor_headers,
        )
        assert captured.status_code == 201, captured.text

        failed = client.post(
            calibrate_url(gauge_id),
            json={"calibration_date": date.today().isoformat(), "result": "fail"},
            headers=office,
        )
        assert failed.status_code == 200, failed.text

        refused = client.post(
            RECORDS_URL.format(op=operation.id, step=second_step.id),
            json={"value_numeric": 1.0, "equipment_id": gauge_id},
            headers=floor_headers,
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["code"] == "GAUGE_OUT_OF_CAL"
        assert detail["status"] == "out_of_service"


# ---------------------------------------------------------------------------
# 3. PUT may take a gauge OUT of service, never put one back in
# ---------------------------------------------------------------------------


class TestEquipmentStatusEdits:
    @pytest.mark.parametrize("target", ["active", "ACTIVE", "due", "overdue"])
    def test_put_cannot_put_a_gauge_into_service(self, client: TestClient, db_session: Session, target: str):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session)
        before = audit_count(db_session)

        response = client.put(
            f"{EQUIPMENT_URL}/{gauge.id}", json={"status": target, "name": "Renamed"}, headers=headers
        )

        assert response.status_code == 409
        assert f"/calibration/equipment/{gauge.id}/calibrate" in response.json()["detail"]
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        assert gauge.name == "Outside micrometer"
        assert audit_count(db_session) == before

    def test_put_takes_a_gauge_out_of_service_and_audits_the_flip(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session, next_cal=date.today() + timedelta(days=200))
        response = client.put(
            f"{EQUIPMENT_URL}/{gauge.id}",
            json={"status": "out_of_service", "location": "Quarantine shelf"},
            headers=quality_headers(db_session),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "out_of_service"
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "equipment", AuditLog.resource_id == gauge.id)
            .order_by(AuditLog.id)
            .all()
        )
        assert [row.action for row in rows] == ["UPDATE", "STATUS_CHANGE"]
        assert rows[1].new_values == {"status": "out_of_service"}

    def test_resending_the_current_status_is_a_noop_not_a_refusal(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session, next_cal=date.today() + timedelta(days=200))
        response = client.put(
            f"{EQUIPMENT_URL}/{gauge.id}",
            json={"status": "active", "location": "Crib"},
            headers=quality_headers(db_session),
        )
        assert response.status_code == 200, response.text
        db_session.refresh(gauge)
        assert gauge.location == "Crib"
        assert gauge.status == CalibrationStatus.ACTIVE
        # A re-sent status is not a status change: one UPDATE row, no STATUS_CHANGE row.
        actions = [
            row.action
            for row in db_session.query(AuditLog).filter(
                AuditLog.resource_type == "equipment", AuditLog.resource_id == gauge.id
            )
        ]
        assert actions == ["UPDATE"]

    @pytest.mark.parametrize(
        "payload",
        [{"status": "broken"}, {"calibration_interval_days": 0}, {"name": None}, {"status": None}],
    )
    def test_invalid_edits_are_422(self, client: TestClient, db_session: Session, payload: dict):
        gauge = make_gauge(db_session)
        response = client.put(f"{EQUIPMENT_URL}/{gauge.id}", json=payload, headers=quality_headers(db_session))
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# 4. RBAC: Admin / Manager / Quality only, no side effects on refusal
# ---------------------------------------------------------------------------


class TestCalibrationRbac:
    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.SHIPPING])
    def test_non_calibration_roles_are_refused_with_no_side_effects(
        self, client: TestClient, db_session: Session, role: UserRole
    ):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session, role=role)
        equipment_before = db_session.query(Equipment).count()
        audit_before = audit_count(db_session)

        created = client.post(EQUIPMENT_URL, json={"equipment_id": f"RBAC-{_next()}", "name": "x"}, headers=headers)
        updated = client.put(f"{EQUIPMENT_URL}/{gauge.id}", json={"status": "retired"}, headers=headers)
        calibrated = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "pass"},
            headers=headers,
        )

        assert (created.status_code, updated.status_code, calibrated.status_code) == (403, 403, 403)
        assert db_session.query(Equipment).count() == equipment_before
        assert record_count(db_session, gauge) == 0
        assert audit_count(db_session) == audit_before
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE

    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])
    def test_calibration_roles_may_record(self, client: TestClient, db_session: Session, role: UserRole):
        gauge = make_gauge(db_session)
        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "pass"},
            headers=quality_headers(db_session, role=role),
        )
        assert response.status_code == 200, response.text

    def test_reads_stay_open_to_a_viewer(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session)
        headers = quality_headers(db_session, role=UserRole.VIEWER)
        assert client.get(EQUIPMENT_URL, headers=headers).status_code == 200
        assert client.get(f"{EQUIPMENT_URL}/{gauge.id}/history", headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# 5. Tenant isolation: another company's gauge is a flat 404 everywhere
# ---------------------------------------------------------------------------


class TestCalibrationTenantIsolation:
    def _victim(self, db: Session) -> Equipment:
        gauge = make_gauge(db, company_id=COMPANY_A, next_cal=date.today() + timedelta(days=100))
        make_record(db, gauge, cal_date=date.today() - timedelta(days=265))
        return gauge

    def _intruder(self, db: Session) -> dict:
        intruder: User = make_user(db, company_id=COMPANY_B, role=UserRole.ADMIN)
        return user_headers(intruder)

    def test_history_of_another_tenants_gauge_is_404_and_leaks_nothing(self, client: TestClient, db_session: Session):
        gauge = self._victim(db_session)
        headers = self._intruder(db_session)

        history = client.get(f"{EQUIPMENT_URL}/{gauge.id}/history", headers=headers)
        detail = client.get(f"{EQUIPMENT_URL}/{gauge.id}", headers=headers)
        missing = client.get(f"{EQUIPMENT_URL}/999999/history", headers=headers)

        assert history.status_code == 404
        assert detail.status_code == 404
        assert history.json() == missing.json()  # indistinguishable from an absent id
        assert FOREIGN_CERT not in history.text
        assert FOREIGN_CERT not in detail.text

    def test_writes_against_another_tenants_gauge_are_404_with_no_side_effects(
        self, client: TestClient, db_session: Session
    ):
        gauge = self._victim(db_session)
        headers = self._intruder(db_session)
        records_before = record_count(db_session, gauge)
        audit_before = audit_count(db_session)

        updated = client.put(
            f"{EQUIPMENT_URL}/{gauge.id}", json={"status": "retired", "name": "pwned"}, headers=headers
        )
        calibrated = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "fail"},
            headers=headers,
        )

        assert (updated.status_code, calibrated.status_code) == (404, 404)
        assert record_count(db_session, gauge) == records_before
        assert audit_count(db_session) == audit_before
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE
        assert gauge.name == "Outside micrometer"

    def test_lists_do_not_include_another_tenants_gauges(self, client: TestClient, db_session: Session):
        gauge = self._victim(db_session)
        headers = self._intruder(db_session)
        listed = client.get(EQUIPMENT_URL, headers=headers)
        due = client.get(f"{EQUIPMENT_URL}/due-soon", params={"days": 365}, headers=headers)
        assert listed.status_code == 200 and due.status_code == 200
        assert gauge.id not in [row["id"] for row in listed.json()]
        assert gauge.id not in [row["id"] for row in due.json()]


# ---------------------------------------------------------------------------
# 6. Gaps the first pass left open (test-engineer review)
# ---------------------------------------------------------------------------


def _fail_audit_on(monkeypatch, *, action: str, resource_type: str) -> None:
    """Make exactly one kind of audit write return None (what ``log_required`` treats as lost).

    Every other audit write still goes through, so a row written EARLIER in the same
    request is really flushed before the failure -- which is what makes the rollback
    assertion mean something. Failing the very first write (as the 503 test above does)
    leaves nothing flushed to roll back, so it cannot tell a rollback from no rollback
    for the audit table.
    """
    original = AuditService.log

    def flaky(self, *args, **kwargs):
        if kwargs.get("action") == action and kwargs.get("resource_type") == resource_type:
            return None
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, "log", flaky)


class TestCalibrationAtomicity:
    def test_lost_status_change_evidence_rolls_back_the_record_and_its_already_written_create_row(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session)
        before = audit_count(db_session)
        _fail_audit_on(monkeypatch, action="STATUS_CHANGE", resource_type="equipment")

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "pass", "certificate_number": "C-ATOM"},
            headers=headers,
        )

        assert response.status_code == 503, response.text
        assert record_count(db_session, gauge) == 0
        # The CREATE row for the calibration WAS flushed before the STATUS_CHANGE write
        # failed; the rollback has to take it too, or the log claims a record that is gone.
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "calibration").count() == 0
        assert audit_count(db_session) == before
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        assert gauge.last_calibration_date is None
        assert gauge.next_calibration_date is None


class TestSameDayOrdering:
    """Two records on the same calibration date: the one entered LAST decides the gauge.

    ``is_newest`` compares with ``>=``. Tightened to ``>``, a same-day FAIL entered after a
    same-day pass would be filed as history and the failed gauge would stay ACTIVE.
    """

    def _post(self, client: TestClient, gauge: Equipment, headers: dict, result: str):
        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": result},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_a_same_day_fail_after_a_pass_takes_the_gauge_out_of_service(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session)

        self._post(client, gauge, headers, "pass")
        failed = self._post(client, gauge, headers, "fail")

        assert failed["applied_to_equipment"] is True
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE

    def test_a_same_day_pass_after_a_fail_puts_the_gauge_back_in_service(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session)
        headers = quality_headers(db_session)

        self._post(client, gauge, headers, "fail")
        passed = self._post(client, gauge, headers, "adjusted")

        assert passed["applied_to_equipment"] is True
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE
        assert gauge.next_calibration_date == date.today() + timedelta(days=365)


class TestFutureDatedRecords:
    """``calibration_date`` cannot be in the future (422).

    Unbounded, a future-dated pass (a year typo on a certificate) became the "newest record on
    file", so a real FAIL recorded today was filed as history and the failed gauge stayed ACTIVE
    and kiosk-usable. This class shipped as a strict xfail in the first pass of this change.
    """

    def test_a_future_dated_record_cannot_mask_a_real_failure_today(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session)

        # A year typo on the certificate date. Accepted (200) or refused (422) -- either is a
        # defensible fix; what must not happen is that it outranks today's real result.
        client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": (date.today() + timedelta(days=365)).isoformat(), "result": "pass"},
            headers=headers,
        )
        failed = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "fail"},
            headers=headers,
        )

        assert failed.status_code == 200, failed.text
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE

    @pytest.mark.parametrize("days_ahead", [2, 365])
    def test_a_future_calibration_date_is_422_and_writes_nothing(
        self, client: TestClient, db_session: Session, days_ahead: int
    ):
        # +2, not +1: the bound is the later of the server's local and UTC dates, so on a
        # Central machine in the evening "tomorrow" is legitimately still today in UTC.
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session)
        before = audit_count(db_session)

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": (date.today() + timedelta(days=days_ahead)).isoformat(), "result": "pass"},
            headers=headers,
        )

        assert response.status_code == 422, response.text
        assert "future" in response.text
        assert record_count(db_session, gauge) == 0
        assert audit_count(db_session) == before
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        assert gauge.next_calibration_date is None


class TestResultIsRequired:
    def test_an_omitted_result_is_422_not_a_pass(self, client: TestClient, db_session: Session):
        # ``result: str = "pass"`` never ran the validator on its default, so a request that
        # named no outcome certified the gauge (200, ACTIVE, a year of due date).
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        headers = quality_headers(db_session)
        before = audit_count(db_session)

        response = client.post(
            calibrate_url(gauge.id), json={"calibration_date": date.today().isoformat()}, headers=headers
        )

        assert response.status_code == 422, response.text
        assert "result" in response.text
        assert record_count(db_session, gauge) == 0
        assert audit_count(db_session) == before
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        assert gauge.next_calibration_date is None


class TestManualPullFloor:
    """A pass dated before a hand pull (``PUT`` -> ``out_of_service``) cannot put the gauge back.

    The pull writes no calibration record, so "newest record on file" alone let a stale
    certificate -- dated after the last record but before the gauge was dropped -- re-certify it.
    """

    def _pull_by_hand(self, client: TestClient, gauge: Equipment, headers: dict) -> None:
        pulled = client.put(f"{EQUIPMENT_URL}/{gauge.id}", json={"status": "out_of_service"}, headers=headers)
        assert pulled.status_code == 200, pulled.text

    def test_a_pass_dated_before_the_pull_is_filed_as_history(self, client: TestClient, db_session: Session):
        last_cal = date.today() - timedelta(days=100)
        next_cal = last_cal + timedelta(days=365)
        gauge = make_gauge(db_session, last_cal=last_cal, next_cal=next_cal)
        make_record(db_session, gauge, cal_date=last_cal)
        headers = quality_headers(db_session)
        self._pull_by_hand(client, gauge, headers)

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": (date.today() - timedelta(days=50)).isoformat(), "result": "pass"},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["applied_to_equipment"], body["equipment_status"]) == (False, "out_of_service")
        assert record_count(db_session, gauge) == 2  # filed, never discarded
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.OUT_OF_SERVICE
        assert (gauge.last_calibration_date, gauge.next_calibration_date) == (last_cal, next_cal)

    def test_a_pass_dated_on_the_day_of_the_pull_puts_the_gauge_back(self, client: TestClient, db_session: Session):
        gauge = make_gauge(db_session, next_cal=date.today() + timedelta(days=200))
        headers = quality_headers(db_session)
        self._pull_by_hand(client, gauge, headers)

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "pass"},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["applied_to_equipment"] is True
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE
        assert gauge.next_calibration_date == date.today() + timedelta(days=365)

    def test_a_failed_calibration_sets_no_floor_beyond_its_own_record_date(
        self, client: TestClient, db_session: Session
    ):
        # The floor reads only HAND pulls. Out of service because of a recorded FAIL dated 10 days
        # ago (entered today), a recalibration dated 5 days ago is the newest record and must
        # apply -- measuring it against the day the fail was ENTERED would strand the gauge.
        gauge = make_gauge(db_session)
        headers = quality_headers(db_session)
        failed = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": (date.today() - timedelta(days=10)).isoformat(), "result": "fail"},
            headers=headers,
        )
        assert failed.status_code == 200, failed.text
        recal_date = date.today() - timedelta(days=5)

        response = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": recal_date.isoformat(), "result": "pass"},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["applied_to_equipment"] is True
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE
        assert gauge.next_calibration_date == recal_date + timedelta(days=365)

    def test_an_unexplained_out_of_service_state_falls_back_to_the_last_modified_date(
        self, client: TestClient, db_session: Session
    ):
        # Legacy: out of service with no status evidence at all (set before this router was
        # audited). ``updated_at`` can only be later than the real pull, so it refuses more.
        gauge = make_gauge(db_session, status=CalibrationStatus.OUT_OF_SERVICE)
        db_session.execute(
            update(Equipment).where(Equipment.id == gauge.id).values(updated_at=datetime.utcnow() - timedelta(days=10))
        )
        db_session.commit()
        headers = quality_headers(db_session)

        stale = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": (date.today() - timedelta(days=20)).isoformat(), "result": "pass"},
            headers=headers,
        )
        fresh = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": (date.today() - timedelta(days=5)).isoformat(), "result": "pass"},
            headers=headers,
        )

        assert (stale.status_code, fresh.status_code) == (200, 200)
        assert stale.json()["applied_to_equipment"] is False
        assert fresh.json()["applied_to_equipment"] is True
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE


class TestManualStatusChangeEvidence:
    def test_a_lost_status_change_row_rolls_the_whole_edit_back(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        # Taking a gauge out of service by hand starts the 7.1.5.2 suspect-product window and is
        # what the pull floor above reads, so its STATUS_CHANGE row is required evidence. The
        # UPDATE row IS flushed before the failure, so the rollback has to take it too.
        gauge = make_gauge(db_session, next_cal=date.today() + timedelta(days=200))
        headers = quality_headers(db_session)
        before = audit_count(db_session)
        _fail_audit_on(monkeypatch, action="STATUS_CHANGE", resource_type="equipment")

        response = client.put(
            f"{EQUIPMENT_URL}/{gauge.id}",
            json={"status": "out_of_service", "location": "Quarantine shelf"},
            headers=headers,
        )

        assert response.status_code == 503, response.text
        assert audit_count(db_session) == before
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE
        assert gauge.location is None

    def test_a_field_only_edit_is_not_gated_on_status_evidence(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        gauge = make_gauge(db_session)
        _fail_audit_on(monkeypatch, action="STATUS_CHANGE", resource_type="equipment")
        response = client.put(
            f"{EQUIPMENT_URL}/{gauge.id}", json={"location": "Crib"}, headers=quality_headers(db_session)
        )
        assert response.status_code == 200, response.text


class TestCalibrationWritesLockTheGauge:
    """Both write verbs row-lock the gauge before deciding anything.

    ``Equipment`` has no ``version`` column: two calibrations posted together both read the same
    "newest record on file". SQLite renders no ``FOR UPDATE``, so the lock is asserted on the
    query the verb actually built, compiled for Postgres (CLAUDE.md: dialect-compile, never trust
    SQLite to execute engine-specific SQL).
    """

    def _spy(self, monkeypatch) -> list:
        built: list = []
        original = Query.with_for_update

        def spy(self, *args, **kwargs):
            locked = original(self, *args, **kwargs)
            built.append(locked)
            return locked

        monkeypatch.setattr(Query, "with_for_update", spy)
        return built

    @pytest.mark.parametrize("verb", ["calibrate", "put"])
    def test_the_verb_locks_the_gauge_row(self, client: TestClient, db_session: Session, monkeypatch, verb: str):
        gauge = make_gauge(db_session)
        headers = quality_headers(db_session)
        built = self._spy(monkeypatch)

        if verb == "calibrate":
            response = client.post(
                calibrate_url(gauge.id),
                json={"calibration_date": date.today().isoformat(), "result": "pass"},
                headers=headers,
            )
        else:
            response = client.put(f"{EQUIPMENT_URL}/{gauge.id}", json={"location": "Crib"}, headers=headers)

        assert response.status_code == 200, response.text
        compiled = [str(q.statement.compile(dialect=postgresql.dialect())) for q in built]
        assert any("FROM equipment" in sql and "FOR UPDATE OF equipment" in sql for sql in compiled), compiled


class TestEquipmentWritesAudited:
    def test_create_equipment_is_stamped_and_audited(self, client: TestClient, db_session: Session):
        code = f"AUD-CREATE-{_next()}"
        response = client.post(
            EQUIPMENT_URL, json={"equipment_id": code, "name": "Height gauge"}, headers=quality_headers(db_session)
        )

        assert response.status_code == 200, response.text
        gauge = db_session.query(Equipment).filter(Equipment.equipment_id == code).one()
        assert gauge.company_id == COMPANY_A
        row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "equipment", AuditLog.resource_id == gauge.id)
            .one()
        )
        assert (row.action, row.company_id, row.resource_identifier) == ("CREATE", COMPANY_A, code)

    def test_a_code_another_tenant_already_uses_is_400_not_500_and_writes_nothing(
        self, client: TestClient, db_session: Session
    ):
        # ``equipment.equipment_id`` is still GLOBALLY unique (no per-company compound yet),
        # so the tenant-scoped probe misses it and the flush is what refuses.
        code = f"SHARED-CODE-{_next()}"
        make_gauge(db_session, company_id=COMPANY_A).equipment_id = code
        db_session.commit()
        headers = quality_headers(db_session, company_id=COMPANY_B)
        audit_before = audit_count(db_session)

        response = client.post(EQUIPMENT_URL, json={"equipment_id": code, "name": "Mine"}, headers=headers)

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Equipment ID already exists"
        assert db_session.query(Equipment).filter(Equipment.company_id == COMPANY_B).count() == 0
        assert audit_count(db_session) == audit_before
        # The refused flush must not poison the request session for the next call.
        follow_up = client.post(EQUIPMENT_URL, json={"equipment_id": f"{code}-B", "name": "Mine"}, headers=headers)
        assert follow_up.status_code == 200, follow_up.text


class TestRetiredGauges:
    def test_the_list_shows_a_retired_gauge_as_retired_not_by_its_dates(self, client: TestClient, db_session: Session):
        # Before: only out_of_service survived the date recompute, so a retired gauge with a
        # future due date was listed as ACTIVE.
        gauge = make_gauge(db_session, status=CalibrationStatus.RETIRED, next_cal=date.today() + timedelta(days=200))
        listed = client.get(EQUIPMENT_URL, params={"include_inactive": True}, headers=quality_headers(db_session))
        assert listed.status_code == 200, listed.text
        row = next(r for r in listed.json() if r["id"] == gauge.id)
        assert row["status"] == "retired"

    def test_a_retired_gauge_returns_to_service_only_through_out_of_service_and_a_passing_calibration(
        self, client: TestClient, db_session: Session
    ):
        # RETIRED must not be a dead end, and the only way back is a calibration record.
        gauge = make_gauge(db_session, status=CalibrationStatus.RETIRED)
        headers = quality_headers(db_session)

        refused = client.put(f"{EQUIPMENT_URL}/{gauge.id}", json={"status": "active"}, headers=headers)
        assert refused.status_code == 409
        parked = client.put(f"{EQUIPMENT_URL}/{gauge.id}", json={"status": "out_of_service"}, headers=headers)
        assert parked.status_code == 200, parked.text
        passed = client.post(
            calibrate_url(gauge.id),
            json={"calibration_date": date.today().isoformat(), "result": "pass"},
            headers=headers,
        )
        assert passed.status_code == 200, passed.text
        assert passed.json()["applied_to_equipment"] is True
        db_session.refresh(gauge)
        assert gauge.status == CalibrationStatus.ACTIVE


class TestMisTenantedRecordsStayHidden:
    def test_a_foreign_record_on_an_own_gauge_is_not_rendered_by_detail_or_history(
        self, client: TestClient, db_session: Session
    ):
        # A legacy row tagged to company B but pointing at A's gauge: the gauge resolves for A,
        # the record must not. Both record reads now go through tenant_query.
        ensure_company(db_session, COMPANY_B)
        gauge = make_gauge(db_session, company_id=COMPANY_A)
        foreign = make_record(db_session, gauge, cal_date=date.today() - timedelta(days=10))
        foreign.company_id = COMPANY_B
        db_session.commit()
        headers = quality_headers(db_session)

        detail = client.get(f"{EQUIPMENT_URL}/{gauge.id}", headers=headers)
        history = client.get(f"{EQUIPMENT_URL}/{gauge.id}/history", headers=headers)

        assert detail.status_code == 200, detail.text
        assert history.status_code == 200, history.text
        assert detail.json()["calibration_history"] == []
        assert history.json() == []
        assert FOREIGN_CERT not in detail.text + history.text
