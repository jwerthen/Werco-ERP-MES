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

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.calibration import CalibrationRecord, CalibrationStatus, Equipment
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, make_user, user_headers
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
