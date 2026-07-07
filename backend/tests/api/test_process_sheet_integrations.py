"""Process Sheets PR 4 tests (docs/PROCESS_SHEETS_SCOPE.md) — integrations.

Covers the Integrations table: the SPC feed (insert on record + supersede, nothing on a
refused OOT, tenant-scoped degrade when the characteristic vanished), gauge-calibration
enforcement (missing / inactive / overdue / no-due-date / current matrix, optional
passthrough preserved), the OOT -> NCR one-tap quality hold (NCR pre-fill, blocker
``ncr_id``, operation ON_HOLD via the existing hold pathway, kiosk-token access), the
warn-and-record qualification snapshot, FAI pre-fill (label heuristic, spec-mismatch and
ambiguity refusals, unmatched report), and serialized WO creation (validation + capture
reachability end-to-end).

Plus every PR 4 ledger item: single-resolve completion gating (TOCTOU closure), the
step-gated skip in ``_copy_slot_completion_evidence``, the office-complete 404 on a
soft-deleted parent WO, the TimeEntry-mirrored ``source`` trust model, the measurement
``decimals`` authoring guard, the shared document-number generator, and the shared
serial parser behind ``coc_service``.
"""

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.calibration import CalibrationStatus, Equipment
from app.models.document import Document, DocumentType
from app.models.operator_certification import SkillMatrix
from app.models.process_sheet import OperationStepRecord, ProcessSheet, WOOperationStep
from app.models.quality import FAICharacteristic, FirstArticleInspection, NCRSource, NonConformanceReport
from app.models.spc import SPCCharacteristic, SPCMeasurement
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.work_order_state_service import reconcile_work_orders_from_completion_evidence
from tests.api.kiosk_test_helpers import bearer, ensure_company, make_user, make_wo_with_operation, make_work_center

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

RECORDS_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/records"
SUPERSEDE_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/records/{record}/supersede"
QUALITY_HOLD_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/quality-hold"
SF_COMPLETE_URL = "/api/v1/shop-floor/operations/{op}/complete"
OFFICE_COMPLETE_URL = "/api/v1/work-orders/operations/{op}/complete"
FAI_PREFILL_URL = "/api/v1/quality/fai/{fai}/prefill-from-steps"

MEASUREMENT_CONFIG = {"lsl": 0.98, "nominal": 1.0, "usl": 1.02, "unit": "in", "decimals": 3}

_seq = {"n": 9000}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_source_sheet(db: Session, company_id: int = 1) -> ProcessSheet:
    sheet = ProcessSheet(
        sheet_number=f"PS-{_next():06d}",
        title="Integrations sheet",
        revision="A",
        status="released",
        company_id=company_id,
    )
    db.add(sheet)
    db.commit()
    db.refresh(sheet)
    return sheet


def _add_step(
    db: Session,
    operation: WorkOrderOperation,
    *,
    step_type: str = "measurement",
    is_required: bool = True,
    config: dict = None,
    label: str = None,
    requires_gauge: bool = False,
    spc_characteristic_id: int = None,
    company_id: int = 1,
) -> WOOperationStep:
    source = _make_source_sheet(db, company_id=company_id)
    if config is None and step_type == "measurement":
        config = dict(MEASUREMENT_CONFIG)
    step = WOOperationStep(
        company_id=company_id,
        work_order_operation_id=operation.id,
        source_sheet_id=source.id,
        source_sheet_revision=source.revision,
        sequence=_next(),
        label=label or f"Check {_next()}",
        step_type=step_type,
        is_required=is_required,
        config=config,
        requires_gauge=requires_gauge,
        spc_characteristic_id=spc_characteristic_id,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return step


def _fixture(db: Session, *, serials: list = None, role: UserRole = UserRole.OPERATOR, quantity: float = 10):
    """Work center + IN_PROGRESS WO/operation + user headers."""
    work_center = make_work_center(db)
    work_order, operation = make_wo_with_operation(
        db,
        work_center=work_center,
        quantity_ordered=quantity,
        op_status=OperationStatus.IN_PROGRESS,
        wo_status=WorkOrderStatus.IN_PROGRESS,
    )
    if serials:
        work_order.serial_numbers = json.dumps(serials)
        db.commit()
    user = make_user(db, role=role)
    headers = bearer(create_access_token(subject=user.id, company_id=1))
    return work_order, operation, user, headers


def _make_characteristic(db: Session, part_id: int, *, company_id: int = 1) -> SPCCharacteristic:
    characteristic = SPCCharacteristic(
        name=f"Bore dia {_next()}",
        part_id=part_id,
        characteristic_type="dimensional",
        unit_of_measure="in",
        specification_nominal=1.0,
        specification_lsl=0.98,
        specification_usl=1.02,
        company_id=company_id,
    )
    db.add(characteristic)
    db.commit()
    db.refresh(characteristic)
    return characteristic


def _make_gauge(
    db: Session,
    *,
    company_id: int = 1,
    gauge_status: CalibrationStatus = CalibrationStatus.ACTIVE,
    next_calibration_date=date.today() + timedelta(days=90),
    name: str = "Caliper",
) -> Equipment:
    gauge = Equipment(
        equipment_id=f"GA-{_next():05d}",
        name=name,
        status=gauge_status,
        next_calibration_date=next_calibration_date,
        company_id=company_id,
    )
    db.add(gauge)
    db.commit()
    db.refresh(gauge)
    return gauge


def _spc_rows(db: Session, characteristic_id: int) -> list:
    return (
        db.query(SPCMeasurement)
        .filter(SPCMeasurement.characteristic_id == characteristic_id)
        .order_by(SPCMeasurement.id)
        .all()
    )


def _audit_extra(db: Session, resource_type: str, resource_id: int) -> dict:
    row = (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    if row.extra_data is None:
        return {}
    return json.loads(row.extra_data) if isinstance(row.extra_data, str) else row.extra_data


def _closed_time_entry(db: Session, work_order, operation, user, quantity: float) -> TimeEntry:
    now = datetime.utcnow()
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=work_order.id,
        operation_id=operation.id,
        work_center_id=operation.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=now - timedelta(hours=1),
        clock_out=now,
        duration_hours=1.0,
        quantity_produced=quantity,
        company_id=work_order.company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# 1. SPC feed
# ---------------------------------------------------------------------------


class TestSPCFeed:
    def test_conforming_record_inserts_spc_point_in_same_transaction(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        work_order.lot_number = "LOT-77"
        db_session.commit()
        characteristic = _make_characteristic(db_session, work_order.part_id)
        step = _add_step(db_session, operation, spc_characteristic_id=characteristic.id, label="Bore dia")

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.001}, headers=headers
        )
        assert response.status_code == 201, response.text
        record_id = response.json()["id"]

        rows = _spc_rows(db_session, characteristic.id)
        assert len(rows) == 1
        point = rows[0]
        assert point.measurement_value == 1.001
        assert point.work_order_id == work_order.id
        assert point.operation_id == operation.id  # step-level traceability (migration 058 column)
        assert point.lot_number == "LOT-77"
        assert point.serial_number is None
        assert point.measured_by == user.id
        assert point.measured_at is not None
        assert point.company_id == 1
        assert point.subgroup_number == 1 and point.sample_number == 1

        extra = _audit_extra(db_session, "operation_step_record", record_id)
        assert extra["spc_measurement_id"] == point.id
        assert extra["spc_characteristic_id"] == characteristic.id

    def test_supersede_inserts_a_new_spc_point(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        characteristic = _make_characteristic(db_session, work_order.part_id)
        step = _add_step(db_session, operation, spc_characteristic_id=characteristic.id)

        created = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.0}, headers=headers
        )
        assert created.status_code == 201
        superseded = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=created.json()["id"]),
            json={"reason": "Mis-read", "value_numeric": 1.01},
            headers=headers,
        )
        assert superseded.status_code == 201, superseded.text

        rows = _spc_rows(db_session, characteristic.id)
        # SPC sees reality: BOTH measurements are points (a time series, not a ledger).
        assert [row.measurement_value for row in rows] == [1.0, 1.01]
        assert [row.subgroup_number for row in rows] == [1, 2]

    def test_refused_oot_inserts_nothing(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        characteristic = _make_characteristic(db_session, work_order.part_id)
        step = _add_step(db_session, operation, spc_characteristic_id=characteristic.id)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.5}, headers=headers
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"]["code"] == "OUT_OF_TOLERANCE"
        assert _spc_rows(db_session, characteristic.id) == []  # no record row, no SPC point

    def test_gauge_refusal_inserts_nothing(self, client: TestClient, db_session: Session):
        # An IN-tolerance value measured with a stale gauge: the gauge refusal
        # alone must keep the untrustworthy point out of the SPC series.
        work_order, operation, user, headers = _fixture(db_session)
        characteristic = _make_characteristic(db_session, work_order.part_id)
        step = _add_step(db_session, operation, spc_characteristic_id=characteristic.id, requires_gauge=True)
        stale = _make_gauge(db_session, next_calibration_date=date.today() - timedelta(days=1))

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_id": stale.id},
            headers=headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"]["code"] == "GAUGE_OUT_OF_CAL"
        assert _spc_rows(db_session, characteristic.id) == []  # no record row, no SPC point
        assert db_session.query(OperationStepRecord).filter_by(work_order_operation_id=operation.id).count() == 0

    def test_missing_characteristic_degrades_to_note_never_fails(self, client: TestClient, db_session: Session):
        # Sheets outlive characteristics: the snapshot points at a characteristic that
        # no longer resolves in THIS company (cross-tenant id doubles as the vanished
        # case AND proves the record-time tenant re-validation).
        ensure_company(db_session, 2)
        work_order, operation, user, headers = _fixture(db_session)
        foreign = _make_characteristic(db_session, work_order.part_id, company_id=2)
        step = _add_step(db_session, operation, spc_characteristic_id=foreign.id)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.0}, headers=headers
        )
        assert response.status_code == 201, response.text  # never a record failure
        assert _spc_rows(db_session, foreign.id) == []

        extra = _audit_extra(db_session, "operation_step_record", response.json()["id"])
        assert "spc_note" in extra and str(foreign.id) in extra["spc_note"]
        assert "spc_measurement_id" not in extra

    def test_step_without_characteristic_feeds_nothing(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)  # no spc_characteristic_id

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.0}, headers=headers
        )
        assert response.status_code == 201
        assert db_session.query(SPCMeasurement).filter(SPCMeasurement.work_order_id == work_order.id).count() == 0

    def test_serialized_record_carries_serial_onto_spc_point(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session, serials=["SN-1", "SN-2"])
        characteristic = _make_characteristic(db_session, work_order.part_id)
        step = _add_step(db_session, operation, spc_characteristic_id=characteristic.id)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "serial_number": "SN-2"},
            headers=headers,
        )
        assert response.status_code == 201
        assert _spc_rows(db_session, characteristic.id)[0].serial_number == "SN-2"


# ---------------------------------------------------------------------------
# 2. Gauge calibration enforcement
# ---------------------------------------------------------------------------


class TestGaugeEnforcement:
    def _gauge_step(self, db_session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, requires_gauge=True)
        return operation, step, headers

    def test_requires_gauge_missing_equipment_is_400(self, client: TestClient, db_session: Session):
        operation, step, headers = self._gauge_step(db_session)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.0}, headers=headers
        )
        assert response.status_code == 400
        assert "equipment_id or equipment_code is required" in response.json()["detail"]
        assert db_session.query(OperationStepRecord).filter_by(work_order_operation_id=operation.id).count() == 0

    @pytest.mark.parametrize(
        "gauge_status,next_cal",
        [
            (CalibrationStatus.OUT_OF_SERVICE, date.today() + timedelta(days=90)),  # inactive status
            (CalibrationStatus.ACTIVE, date.today() - timedelta(days=1)),  # overdue
            (CalibrationStatus.ACTIVE, None),  # no due date -> fails closed
            (CalibrationStatus.OVERDUE, date.today() - timedelta(days=30)),  # both stale
        ],
    )
    def test_out_of_cal_gauge_is_409_with_payload_and_no_row(
        self, client: TestClient, db_session: Session, gauge_status, next_cal
    ):
        operation, step, headers = self._gauge_step(db_session)
        gauge = _make_gauge(db_session, gauge_status=gauge_status, next_calibration_date=next_cal)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_id": gauge.id},
            headers=headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "GAUGE_OUT_OF_CAL"
        assert detail["equipment_id"] == gauge.id
        assert detail["status"] == gauge_status.value
        assert detail["next_calibration_date"] == (next_cal.isoformat() if next_cal else None)
        assert db_session.query(OperationStepRecord).filter_by(work_order_operation_id=operation.id).count() == 0

    def test_current_gauge_records_and_stores_gauge_identity(self, client: TestClient, db_session: Session):
        operation, step, headers = self._gauge_step(db_session)
        gauge = _make_gauge(db_session)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_id": gauge.id},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["equipment_id"] == gauge.id
        # Addendum: the resolved gauge is echoed on the id path too.
        assert response.json()["gauge"] == {
            "equipment_id": gauge.id,
            "equipment_code": gauge.equipment_id,
            "name": gauge.name,
        }

    def test_gauge_check_runs_before_tolerance(self, client: TestClient, db_session: Session):
        # An OOT value measured with a stale gauge refuses on the GAUGE, not the
        # tolerance — an untrustworthy measurement must not reach the OOT/NCR path.
        operation, step, headers = self._gauge_step(db_session)
        gauge = _make_gauge(db_session, next_calibration_date=date.today() - timedelta(days=1))
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.5, "equipment_id": gauge.id},
            headers=headers,
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "GAUGE_OUT_OF_CAL"

    def test_non_requires_gauge_step_keeps_optional_passthrough(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, requires_gauge=False)
        # No gauge at all: fine.
        ok = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.0}, headers=headers
        )
        assert ok.status_code == 201
        # A stale gauge on a non-requires_gauge step: still a passthrough (no currency check).
        stale = _make_gauge(db_session, next_calibration_date=date.today() - timedelta(days=1))
        also_ok = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_id": stale.id},
            headers=headers,
        )
        assert also_ok.status_code == 201

    def test_supersede_enforces_gauge_too(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, requires_gauge=True)
        gauge = _make_gauge(db_session)
        created = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_id": gauge.id},
            headers=headers,
        )
        assert created.status_code == 201
        missing = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=created.json()["id"]),
            json={"reason": "Re-measure", "value_numeric": 1.01},
            headers=headers,
        )
        assert missing.status_code == 400  # the correction runs the FULL ladder


class TestGaugeByCode:
    """Addendum: kiosk operators can't list /equipment (path fence) — the gauge is
    scanned/typed as its MARKED identifier and resolved server-side."""

    def _gauge_step(self, db_session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, requires_gauge=True)
        return operation, step, headers

    def test_equipment_code_resolves_and_echoes_the_gauge(self, client: TestClient, db_session: Session):
        operation, step, headers = self._gauge_step(db_session)
        gauge = _make_gauge(db_session, name="Bore mic 3")

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_code": gauge.equipment_id},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["equipment_id"] == gauge.id  # stored as the FK, same as the id path
        assert body["gauge"] == {
            "equipment_id": gauge.id,
            "equipment_code": gauge.equipment_id,
            "name": "Bore mic 3",
        }
        row = db_session.query(OperationStepRecord).filter_by(id=body["id"]).one()
        assert row.equipment_id == gauge.id

    def test_unknown_code_is_404_with_the_identifier_named(self, client: TestClient, db_session: Session):
        operation, step, headers = self._gauge_step(db_session)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_code": "GA-NOPE"},
            headers=headers,
        )
        assert response.status_code == 404, response.text
        assert "No gauge with identifier 'GA-NOPE'" in response.json()["detail"]
        assert db_session.query(OperationStepRecord).filter_by(work_order_operation_id=operation.id).count() == 0

    def test_both_id_and_code_is_400(self, client: TestClient, db_session: Session):
        operation, step, headers = self._gauge_step(db_session)
        gauge = _make_gauge(db_session)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_id": gauge.id, "equipment_code": gauge.equipment_id},
            headers=headers,
        )
        assert response.status_code == 400
        assert "not both" in response.json()["detail"]

    def test_code_resolving_a_stale_gauge_is_409_out_of_cal(self, client: TestClient, db_session: Session):
        operation, step, headers = self._gauge_step(db_session)
        stale = _make_gauge(db_session, next_calibration_date=date.today() - timedelta(days=5))
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_code": stale.equipment_id},
            headers=headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "GAUGE_OUT_OF_CAL"
        assert detail["equipment_id"] == stale.id  # resolved gauge flows through the same check

    def test_cross_tenant_code_never_resolves(self, client: TestClient, db_session: Session):
        ensure_company(db_session, 2)
        operation, step, headers = self._gauge_step(db_session)
        foreign = _make_gauge(db_session, company_id=2)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_code": foreign.equipment_id},
            headers=headers,
        )
        assert response.status_code == 404

    def test_code_match_is_case_insensitive_and_whitespace_tolerant(self, client: TestClient, db_session: Session):
        # Code-review overrule: the kiosk field is scan-OR-TYPE, so a hand-typed
        # lowercase code must resolve (the column is globally unique, so a
        # case-folded match cannot be ambiguous). The record echoes the CANONICAL
        # stored code, scanner padding is trimmed, and the two normalizations
        # compose. (The capture/hold-path canonical-echo variants also live in
        # tests/api/test_process_sheet_audit_fixes.py.)
        operation, step, headers = self._gauge_step(db_session)
        gauge = _make_gauge(db_session)  # code shaped GA-#####
        url = RECORDS_URL.format(op=operation.id, step=step.id)

        wrong_case = gauge.equipment_id.lower()
        assert wrong_case != gauge.equipment_id
        typed = client.post(url, json={"value_numeric": 1.0, "equipment_code": wrong_case}, headers=headers)
        assert typed.status_code == 201, typed.text
        assert typed.json()["equipment_id"] == gauge.id
        assert typed.json()["gauge"]["equipment_code"] == gauge.equipment_id  # canonical echo, never the typo

        padded = client.post(
            url, json={"value_numeric": 1.0, "equipment_code": f"  {gauge.equipment_id}  "}, headers=headers
        )
        assert padded.status_code == 201, padded.text
        assert padded.json()["equipment_id"] == gauge.id

        padded_and_lowercase = client.post(
            url, json={"value_numeric": 1.0, "equipment_code": f"  {wrong_case}  "}, headers=headers
        )
        assert padded_and_lowercase.status_code == 201, padded_and_lowercase.text
        assert padded_and_lowercase.json()["gauge"]["equipment_code"] == gauge.equipment_id

    def test_supersede_accepts_equipment_code(self, client: TestClient, db_session: Session):
        operation, step, headers = self._gauge_step(db_session)
        gauge = _make_gauge(db_session)
        created = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_code": gauge.equipment_id},
            headers=headers,
        )
        assert created.status_code == 201
        superseded = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=created.json()["id"]),
            json={"reason": "Re-measure", "value_numeric": 1.01, "equipment_code": gauge.equipment_id},
            headers=headers,
        )
        assert superseded.status_code == 201, superseded.text
        assert superseded.json()["gauge"]["equipment_code"] == gauge.equipment_id


# ---------------------------------------------------------------------------
# 3. OOT -> NCR one-tap quality hold
# ---------------------------------------------------------------------------


class TestQualityHold:
    def test_quality_hold_creates_ncr_blocker_and_holds_operation(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        work_order.lot_number = "LOT-13"
        db_session.commit()
        step = _add_step(db_session, operation, label="Bore dia")
        open_entry = TimeEntry(
            user_id=user.id,
            work_order_id=work_order.id,
            operation_id=operation.id,
            work_center_id=operation.work_center_id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow(),
            company_id=1,
        )
        db_session.add(open_entry)
        db_session.commit()

        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.31, "notes": "Way over"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["operation_status"] == "on_hold"
        assert body["ncr_number"].startswith("NCR-")
        assert open_entry.id in body["closed_time_entry_ids"]

        ncr = db_session.query(NonConformanceReport).filter_by(id=body["ncr_id"]).one()
        assert ncr.source == NCRSource.IN_PROCESS
        assert ncr.work_order_id == work_order.id
        assert ncr.part_id == work_order.part_id
        assert ncr.lot_number == "LOT-13"
        assert ncr.serial_number is None
        assert ncr.actual_value == "1.31"
        assert ncr.required_value == "0.98 to 1.02 in"
        assert "Bore dia" in ncr.title and "Bore dia" in ncr.specification
        assert "nominal 1.0" in ncr.specification
        assert "Way over" in ncr.description
        assert ncr.detected_by == user.id
        assert ncr.company_id == 1

        blocker = db_session.query(WorkOrderBlocker).filter_by(id=body["blocker_id"]).one()
        assert blocker.category == "quality_hold"
        assert blocker.ncr_id == ncr.id  # the migration-058 FK, populated
        assert blocker.operation_id == operation.id
        assert blocker.status == "open"

        db_session.refresh(operation)
        assert operation.status == OperationStatus.ON_HOLD
        db_session.refresh(open_entry)
        assert open_entry.clock_out is not None  # held ops accrue no labor

        # Tamper-evident trail: NCR create + blocker create + op status change.
        assert (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "ncr", AuditLog.resource_id == ncr.id, AuditLog.action == "CREATE")
            .count()
            == 1
        )
        assert (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order_blocker", AuditLog.resource_id == blocker.id)
            .count()
            == 1
        )
        status_rows = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == operation.id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .all()
        )
        assert len(status_rows) == 1

    def test_serialized_wo_requires_valid_serial(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session, serials=["SN-1", "SN-2"])
        step = _add_step(db_session, operation)
        url = QUALITY_HOLD_URL.format(op=operation.id, step=step.id)

        assert client.post(url, json={"measured_value": 1.5}, headers=headers).status_code == 400
        assert (
            client.post(url, json={"measured_value": 1.5, "serial_number": "SN-9"}, headers=headers).status_code == 400
        )
        ok = client.post(url, json={"measured_value": 1.5, "serial_number": "SN-1"}, headers=headers)
        assert ok.status_code == 201, ok.text
        ncr = db_session.query(NonConformanceReport).filter_by(id=ok.json()["ncr_id"]).one()
        assert ncr.serial_number == "SN-1"

    def test_non_serialized_wo_forbids_serial(self, client: TestClient, db_session: Session):
        # Same serial rules as capture: a serial on a non-serialized WO is a 400.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.5, "serial_number": "SN-1"},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert "not serialized" in response.json()["detail"]
        assert db_session.query(NonConformanceReport).filter_by(work_order_id=work_order.id).count() == 0
        db_session.refresh(operation)
        assert operation.status == OperationStatus.IN_PROGRESS  # nothing held

    def test_hold_closes_every_open_crew_entry(self, client: TestClient, db_session: Session):
        # Crew-station reality: several operators are clocked into the held op
        # at once — the hold closes ALL of them and touches nothing else.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        crew_a = make_user(db_session, role=UserRole.OPERATOR)
        crew_b = make_user(db_session, role=UserRole.OPERATOR)
        now = datetime.utcnow()
        open_entries = []
        for crew_member in (crew_a, crew_b):
            entry = TimeEntry(
                user_id=crew_member.id,
                work_order_id=work_order.id,
                operation_id=operation.id,
                work_center_id=operation.work_center_id,
                entry_type=TimeEntryType.RUN,
                clock_in=now - timedelta(minutes=30),
                company_id=1,
            )
            db_session.add(entry)
            open_entries.append(entry)
        db_session.commit()
        already_closed = _closed_time_entry(db_session, work_order, operation, user, quantity=0)
        closed_stamp = already_closed.clock_out

        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.5},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        assert set(response.json()["closed_time_entry_ids"]) == {entry.id for entry in open_entries}
        for entry in open_entries:
            db_session.refresh(entry)
            assert entry.clock_out is not None
            assert entry.duration_hours == pytest.approx(0.5, abs=0.05)
        db_session.refresh(already_closed)
        assert already_closed.clock_out == closed_stamp  # untouched

    def test_ncr_strings_come_verbatim_from_the_snapshot_config(self, client: TestClient, db_session: Session):
        # The NCR's spec strings are built from the SNAPSHOT step config, exactly.
        work_order, operation, user, headers = _fixture(db_session)
        config = {"lsl": 9.5, "nominal": 9.75, "usl": 10.0, "unit": "mm", "decimals": 2}
        step = _add_step(db_session, operation, config=config, label="Slot depth")

        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 12.34},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        ncr = db_session.query(NonConformanceReport).filter_by(id=response.json()["ncr_id"]).one()
        assert ncr.specification == "Slot depth: nominal 9.75, LSL 9.5, USL 10.0 mm"
        assert ncr.required_value == "9.5 to 10.0 mm"
        assert ncr.actual_value == "12.34"

    def test_non_measurement_step_is_400(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)
        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id), json={"measured_value": 1.5}, headers=headers
        )
        assert response.status_code == 400
        assert "MEASUREMENT" in response.json()["detail"]

    def test_state_gates_match_capture(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        url = QUALITY_HOLD_URL.format(op=operation.id, step=step.id)

        operation.status = OperationStatus.READY
        db_session.commit()
        assert client.post(url, json={"measured_value": 1.5}, headers=headers).status_code == 400

        operation.status = OperationStatus.IN_PROGRESS
        work_order.status = WorkOrderStatus.CANCELLED
        db_session.commit()
        assert client.post(url, json={"measured_value": 1.5}, headers=headers).status_code == 409

    def test_kiosk_scoped_operator_token_can_file_a_hold(self, client: TestClient, db_session: Session):
        # Kiosk operators file these: the endpoint sits under /shop-floor (in-fence).
        work_order, operation, _, _ = _fixture(db_session)
        step = _add_step(db_session, operation)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        kiosk_headers = bearer(create_access_token(subject=operator.id, company_id=1, scope="kiosk"))

        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.5},
            headers=kiosk_headers,
        )
        assert response.status_code == 201, response.text
        db_session.refresh(operation)
        assert operation.status == OperationStatus.ON_HOLD

    def test_full_oot_journey_refusal_then_hold(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        url = RECORDS_URL.format(op=operation.id, step=step.id)

        refused = client.post(url, json={"value_numeric": 1.5}, headers=headers)
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "OUT_OF_TOLERANCE"

        hold = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.5},
            headers=headers,
        )
        assert hold.status_code == 201
        # No record row exists — the refused value lives on the NCR only.
        assert db_session.query(OperationStepRecord).filter_by(work_order_operation_id=operation.id).count() == 0


# ---------------------------------------------------------------------------
# 4. Qualification snapshot (warn-and-record, never blocks)
# ---------------------------------------------------------------------------


class TestQualificationSnapshot:
    def test_unqualified_recorder_still_records_with_snapshot(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert response.status_code == 201, response.text  # NEVER blocks
        snapshot = response.json()["qualification_snapshot"]
        assert snapshot["qualified"] is False  # no SkillMatrix entry for this WC
        assert snapshot["user_id"] == user.id
        assert snapshot["work_center_id"] == operation.work_center_id
        assert snapshot["evaluated_at"].endswith("Z")
        assert any(exc["code"] == "operator_not_skill_qualified" for exc in snapshot["exceptions"])

        row = db_session.query(OperationStepRecord).filter_by(id=response.json()["id"]).one()
        assert row.qualification_snapshot["qualified"] is False

        # Unqualified capture is discoverable on the audit row too.
        extra = _audit_extra(db_session, "operation_step_record", row.id)
        assert extra["qualification_exceptions"]

    def test_qualified_recorder_snapshot_is_clean(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        db_session.add(
            SkillMatrix(
                user_id=user.id,
                work_center_id=operation.work_center_id,
                skill_level=3,
                is_active=True,
                company_id=1,
            )
        )
        db_session.commit()
        step = _add_step(db_session, operation, step_type="checkbox", config=None)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert response.status_code == 201
        snapshot = response.json()["qualification_snapshot"]
        assert snapshot["qualified"] is True and snapshot["exceptions"] == []
        extra = _audit_extra(db_session, "operation_step_record", response.json()["id"])
        assert "qualification_exceptions" not in extra  # nothing to flag

    def test_supersede_snapshots_the_correcting_operator(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)
        created = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": False}, headers=headers
        )
        assert created.status_code == 201

        corrector = make_user(db_session, role=UserRole.OPERATOR)
        corrector_headers = bearer(create_access_token(subject=corrector.id, company_id=1))
        superseded = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=created.json()["id"]),
            json={"reason": "Done now", "value_bool": True},
            headers=corrector_headers,
        )
        assert superseded.status_code == 201, superseded.text
        assert superseded.json()["qualification_snapshot"]["user_id"] == corrector.id


# ---------------------------------------------------------------------------
# 5. FAI pre-fill
# ---------------------------------------------------------------------------


class TestFAIPrefill:
    def _fai(self, db_session, work_order, chars: list, serial_number=None) -> FirstArticleInspection:
        fai = FirstArticleInspection(
            fai_number=f"FAI-TEST-{_next():05d}",
            part_id=work_order.part_id,
            work_order_id=work_order.id,
            serial_number=serial_number,
            company_id=1,
        )
        db_session.add(fai)
        db_session.flush()
        for i, char in enumerate(chars):
            db_session.add(
                FAICharacteristic(
                    fai_id=fai.id,
                    char_number=char.get("char_number", i + 1),
                    characteristic=char["characteristic"],
                    nominal=char.get("nominal"),
                    tolerance_plus=char.get("tolerance_plus"),
                    tolerance_minus=char.get("tolerance_minus"),
                    actual_value=char.get("actual_value"),
                    company_id=1,
                )
            )
        db_session.commit()
        db_session.refresh(fai)
        return fai

    def test_prefill_populates_matching_chars_and_reports_unmatched(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        gauge = _make_gauge(db_session, name="Mitutoyo caliper 7")
        step = _add_step(db_session, operation, label="Bore dia")
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0041, "equipment_id": gauge.id},
            headers=headers,
        )
        assert recorded.status_code == 201, recorded.text

        fai = self._fai(
            db_session,
            work_order,
            [
                # Matches: label + parseable nominal/tolerances agreeing with the config.
                {"characteristic": "Bore dia", "nominal": "1.0", "tolerance_plus": "0.02", "tolerance_minus": "0.02"},
                # No matching step label.
                {"characteristic": "Flatness", "char_number": 2},
                # Label matches but the nominal contradicts the step config.
                {"characteristic": "Bore dia", "char_number": 3, "nominal": "2.0"},
                # Already recorded — never overwritten.
                {"characteristic": "Bore dia", "char_number": 4, "actual_value": "0.999"},
            ],
        )

        response = client.post(FAI_PREFILL_URL.format(fai=fai.id), headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["prefilled_count"] == 1  # only char 1 fills; 2/3/4 are reported
        prefilled_numbers = {entry["char_number"] for entry in body["prefilled"]}
        unmatched = {entry["char_number"]: entry["reason"] for entry in body["unmatched"]}
        assert prefilled_numbers == {1}
        assert "no conforming measurement" in unmatched[2]
        assert "nominal mismatch" in unmatched[3]
        assert "already recorded" in unmatched[4]

        char_1 = (
            db_session.query(FAICharacteristic)
            .filter(FAICharacteristic.fai_id == fai.id, FAICharacteristic.char_number == 1)
            .one()
        )
        assert char_1.actual_value == "1.004"  # the rounded recorded value
        assert char_1.measuring_device == "Mitutoyo caliper 7"
        char_4 = (
            db_session.query(FAICharacteristic)
            .filter(FAICharacteristic.fai_id == fai.id, FAICharacteristic.char_number == 4)
            .one()
        )
        assert char_4.actual_value == "0.999"  # untouched

        # Audited on the FAI with the full report.
        extra = _audit_extra(db_session, "fai", fai.id)
        assert {entry["char_number"] for entry in extra["prefilled"]} == {1}

    def test_ambiguous_step_label_is_reported_not_guessed(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step_a = _add_step(db_session, operation, label="Width")
        step_b = _add_step(db_session, operation, label="Width")
        for step in (step_a, step_b):
            assert (
                client.post(
                    RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.0}, headers=headers
                ).status_code
                == 201
            )
        fai = self._fai(db_session, work_order, [{"characteristic": "Width"}])

        response = client.post(FAI_PREFILL_URL.format(fai=fai.id), headers=headers)
        assert response.status_code == 200
        assert response.json()["prefilled"] == []
        assert "ambiguous" in response.json()["unmatched"][0]["reason"]

    def test_second_prefill_is_idempotent(self, client: TestClient, db_session: Session):
        # Running prefill twice must not overwrite, duplicate, or re-audit:
        # the second call reports the filled char as already recorded.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, label="Bore dia")
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.001}, headers=headers
        )
        assert recorded.status_code == 201, recorded.text
        fai = self._fai(db_session, work_order, [{"characteristic": "Bore dia", "nominal": "1.0"}])

        first = client.post(FAI_PREFILL_URL.format(fai=fai.id), headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["prefilled_count"] == 1

        second = client.post(FAI_PREFILL_URL.format(fai=fai.id), headers=headers)
        assert second.status_code == 200, second.text
        assert second.json()["prefilled_count"] == 0 and second.json()["prefilled"] == []
        assert "already recorded" in second.json()["unmatched"][0]["reason"]

        char = db_session.query(FAICharacteristic).filter(FAICharacteristic.fai_id == fai.id).one()
        assert char.actual_value == "1.001"  # first fill, untouched
        # Only the FIRST call wrote anything — exactly one audit row.
        assert (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "fai", AuditLog.resource_id == fai.id).count()
            == 1
        )

    def test_fai_without_work_order_is_400(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        fai = FirstArticleInspection(fai_number=f"FAI-TEST-{_next():05d}", part_id=work_order.part_id, company_id=1)
        db_session.add(fai)
        db_session.commit()
        response = client.post(FAI_PREFILL_URL.format(fai=fai.id), headers=headers)
        assert response.status_code == 400

    def test_kiosk_scoped_token_cannot_reach_quality_prefill(self, client: TestClient, db_session: Session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        kiosk_headers = bearer(create_access_token(subject=operator.id, company_id=1, scope="kiosk"))
        response = client.post(FAI_PREFILL_URL.format(fai=1), headers=kiosk_headers)
        assert response.status_code == status.HTTP_403_FORBIDDEN  # path-fenced to /shop-floor

    def test_tenant_isolation_foreign_fai_404s(self, client: TestClient, db_session: Session):
        ensure_company(db_session, 2)
        foreign_wc = make_work_center(db_session, company_id=2)
        foreign_wo, _ = make_wo_with_operation(db_session, company_id=2, work_center=foreign_wc)
        foreign_fai = FirstArticleInspection(
            fai_number=f"FAI-TEST-{_next():05d}",
            part_id=foreign_wo.part_id,
            work_order_id=foreign_wo.id,
            company_id=2,
        )
        db_session.add(foreign_fai)
        db_session.commit()

        user = make_user(db_session, role=UserRole.QUALITY)
        headers = bearer(create_access_token(subject=user.id, company_id=1))
        assert client.post(FAI_PREFILL_URL.format(fai=foreign_fai.id), headers=headers).status_code == 404


# ---------------------------------------------------------------------------
# 6. Serialized WO creation
# ---------------------------------------------------------------------------


class TestSerializedWOCreation:
    def _part_payload(self, db_session, quantity: float):
        # A plain part with no routing: creation succeeds with zero operations.
        from app.models.part import Part

        part = Part(
            part_number=f"SER-{_next():05d}",
            name="Serialized part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add(part)
        db_session.commit()
        return {"part_id": part.id, "quantity_ordered": quantity}

    def test_create_serialized_wo_stores_and_returns_serials(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        payload = {**self._part_payload(db_session, 2), "serial_numbers": ["SN-100", "SN-101"]}
        response = client.post("/api/v1/work-orders/", json=payload, headers=auth_headers)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()
        assert body["serial_numbers"] == ["SN-100", "SN-101"]

        from app.models.work_order import WorkOrder

        work_order = db_session.query(WorkOrder).filter_by(id=body["id"]).one()
        assert json.loads(work_order.serial_numbers) == ["SN-100", "SN-101"]

    @pytest.mark.parametrize(
        "serials,quantity",
        [
            (["SN-1"], 2),  # count != quantity
            (["SN-1", "SN-1"], 2),  # duplicates
            (["SN-1", "  "], 2),  # blank entry
        ],
    )
    def test_invalid_serial_sets_are_422(
        self, client: TestClient, auth_headers: dict, db_session: Session, serials, quantity
    ):
        payload = {**self._part_payload(db_session, quantity), "serial_numbers": serials}
        response = client.post("/api/v1/work-orders/", json=payload, headers=auth_headers)
        assert response.status_code == 422, response.text

    def test_omitting_serials_stays_non_serialized(self, client: TestClient, auth_headers: dict, db_session: Session):
        response = client.post("/api/v1/work-orders/", json=self._part_payload(db_session, 3), headers=auth_headers)
        assert response.status_code == 201
        assert response.json()["serial_numbers"] is None

    def test_per_serial_capture_reachable_end_to_end(self, client: TestClient, auth_headers: dict, db_session: Session):
        # Office-created serialized WO -> per-serial step records work without any
        # direct DB fiddling of serial_numbers (the PR 4 reachability deliverable).
        payload = {**self._part_payload(db_session, 2), "serial_numbers": ["SN-A", "SN-B"]}
        created = client.post("/api/v1/work-orders/", json=payload, headers=auth_headers)
        assert created.status_code == 201
        wo_id = created.json()["id"]

        work_center = make_work_center(db_session)
        operation = WorkOrderOperation(
            work_order_id=wo_id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="OP10",
            name="Inspect",
            status=OperationStatus.IN_PROGRESS,
            company_id=1,
        )
        db_session.add(operation)
        db_session.commit()
        db_session.refresh(operation)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)

        url = RECORDS_URL.format(op=operation.id, step=step.id)
        assert client.post(url, json={"value_bool": True}, headers=auth_headers).status_code == 400  # serial required
        ok = client.post(url, json={"value_bool": True, "serial_number": "SN-A"}, headers=auth_headers)
        assert ok.status_code == 201
        assert ok.json()["serial_number"] == "SN-A"


# ---------------------------------------------------------------------------
# 7-13. Ledger items
# ---------------------------------------------------------------------------


class TestCompletionResolveOnce:
    def test_gate_fires_on_evidence_floored_quantity_not_the_raw_request(self, client: TestClient, db_session: Session):
        # Item 7 (TOCTOU closure): the gate and the store share ONE resolved quantity.
        # A partial request (1) whose TimeEntry evidence already reaches target (10)
        # RESOLVES to full completion — so the steps gate must fire.
        work_order, operation, user, headers = _fixture(db_session)
        _add_step(db_session, operation, step_type="checkbox", config=None)
        _closed_time_entry(db_session, work_order, operation, user, quantity=10)

        response = client.post(SF_COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 1}, headers=headers)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "STEPS_INCOMPLETE"

    def test_zero_step_completion_still_stores_the_resolved_quantity(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        _closed_time_entry(db_session, work_order, operation, user, quantity=10)

        response = client.post(SF_COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 1}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["is_fully_complete"] is True
        assert response.json()["operation"]["quantity_complete"] == 10  # evidence-floored, same value the gate saw

    def test_office_twin_gate_uses_the_same_single_resolve(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session, role=UserRole.MANAGER)
        _add_step(db_session, operation, step_type="checkbox", config=None)
        _closed_time_entry(db_session, work_order, operation, user, quantity=10)

        response = client.post(
            OFFICE_COMPLETE_URL.format(op=operation.id), params={"quantity_complete": 1}, headers=headers
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "STEPS_INCOMPLETE"


class TestCopySlotSkipsGatedOps:
    def test_step_gated_target_op_is_not_flipped_by_slot_copy(self, client: TestClient, db_session: Session):
        # Item 8: two rows share a progress key (regenerated ops); the source has
        # completion evidence, the TARGET is step-gated -> the copy must skip it.
        work_center = make_work_center(db_session)
        work_order, source_op = make_wo_with_operation(
            db_session, work_center=work_center, op_status=OperationStatus.COMPLETE
        )
        now = datetime.utcnow()
        source_op.quantity_complete = 10
        source_op.actual_end = now
        source_op.completed_by = 1
        target_op = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=source_op.sequence,  # same progress key
            operation_number=source_op.operation_number,
            name=source_op.name,
            status=OperationStatus.IN_PROGRESS,
            company_id=1,
        )
        db_session.add(target_op)
        db_session.commit()
        db_session.refresh(target_op)
        _add_step(db_session, target_op, step_type="checkbox", config=None)  # required, no record

        db_session.refresh(work_order)
        reconcile_work_orders_from_completion_evidence(db_session, [work_order])

        assert target_op.status != OperationStatus.COMPLETE  # gate held
        assert target_op.actual_end is None and target_op.completed_by is None  # no evidence stamped either

    def test_ungated_target_op_still_receives_the_copy(self, client: TestClient, db_session: Session):
        work_center = make_work_center(db_session)
        work_order, source_op = make_wo_with_operation(
            db_session, work_center=work_center, op_status=OperationStatus.COMPLETE
        )
        source_op.quantity_complete = 10
        source_op.actual_end = datetime.utcnow()
        source_op.completed_by = 1
        target_op = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=source_op.sequence,
            operation_number=source_op.operation_number,
            name=source_op.name,
            status=OperationStatus.IN_PROGRESS,
            company_id=1,
        )
        db_session.add(target_op)
        db_session.commit()

        db_session.refresh(work_order)
        reconcile_work_orders_from_completion_evidence(db_session, [work_order])
        assert target_op.status == OperationStatus.COMPLETE  # the pre-PR-4 behavior, preserved


class TestOfficeCompleteSoftDeletedWO:
    def test_office_complete_404s_on_soft_deleted_parent(self, client: TestClient, db_session: Session):
        # Item 9: align with the shop-floor twin instead of completing an orphaned op.
        work_order, operation, user, headers = _fixture(db_session, role=UserRole.MANAGER)
        work_order.is_deleted = True
        db_session.commit()

        response = client.post(
            OFFICE_COMPLETE_URL.format(op=operation.id), params={"quantity_complete": 10}, headers=headers
        )
        assert response.status_code == 404, response.text
        db_session.refresh(operation)
        assert operation.status != OperationStatus.COMPLETE


class TestRecordSourceTrustModel:
    def test_client_hint_is_stored_verbatim_for_normal_tokens(self, client: TestClient, db_session: Session):
        # Item 10: TimeEntry's exact posture — hint stored, never guessed.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_bool": True, "source": "scanner"},
            headers=headers,
        )
        assert response.status_code == 201
        assert response.json()["source"] == "scanner"

    def test_no_hint_stores_null_never_guessed(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert response.status_code == 201
        assert response.json()["source"] is None

    def test_unknown_hint_is_422(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_bool": True, "source": "fax"},
            headers=headers,
        )
        assert response.status_code == 422  # fenced to the TimeEntrySource vocabulary

    def test_kiosk_credential_wins_over_any_hint(self, client: TestClient, db_session: Session):
        work_order, operation, _, _ = _fixture(db_session)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        kiosk_headers = bearer(create_access_token(subject=operator.id, company_id=1, scope="kiosk"))
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_bool": True, "source": "desktop"},  # lying hint
            headers=kiosk_headers,
        )
        assert response.status_code == 201
        assert response.json()["source"] == "kiosk"  # server-derived wins

    def test_supersede_accepts_the_same_hint(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, step_type="checkbox", config=None)
        created = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_bool": False, "source": "scanner"},
            headers=headers,
        )
        assert created.status_code == 201
        superseded = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=created.json()["id"]),
            json={"reason": "Fixed", "value_bool": True, "source": "backfill"},
            headers=headers,
        )
        assert superseded.status_code == 201
        assert superseded.json()["source"] == "backfill"


class TestDecimalsAuthoringGuard:
    def _draft_sheet(self, client, headers) -> int:
        response = client.post("/api/v1/process-sheets/", json={"title": "Guard sheet"}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["id"]

    def _step_payload(self, config: dict) -> dict:
        return {
            "sequence": 10,
            "label": "Bore dia",
            "step_type": "measurement",
            "config": config,
        }

    def test_decimals_too_coarse_to_resolve_band_is_400(self, client: TestClient, db_session: Session):
        # Item 11: band = 0.04; decimals=1 rounds at 0.1 > band -> refuse at authoring.
        user = make_user(db_session, role=UserRole.MANAGER)
        headers = bearer(create_access_token(subject=user.id, company_id=1))
        sheet_id = self._draft_sheet(client, headers)

        bad = self._step_payload({"lsl": 0.98, "nominal": 1.0, "usl": 1.02, "decimals": 1})
        response = client.post(f"/api/v1/process-sheets/{sheet_id}/steps", json=bad, headers=headers)
        assert response.status_code == 400, response.text
        assert "decimals" in response.json()["detail"]

        ok = self._step_payload({"lsl": 0.98, "nominal": 1.0, "usl": 1.02, "decimals": 2})
        response = client.post(f"/api/v1/process-sheets/{sheet_id}/steps", json=ok, headers=headers)
        assert response.status_code == 200, response.text

    def test_invalid_decimals_type_is_400(self, client: TestClient, db_session: Session):
        user = make_user(db_session, role=UserRole.MANAGER)
        headers = bearer(create_access_token(subject=user.id, company_id=1))
        sheet_id = self._draft_sheet(client, headers)
        for bad_decimals in (-1, 2.5, True):
            payload = self._step_payload({"lsl": 0.98, "nominal": 1.0, "usl": 1.02, "decimals": bad_decimals})
            response = client.post(f"/api/v1/process-sheets/{sheet_id}/steps", json=payload, headers=headers)
            assert response.status_code == 400, (bad_decimals, response.text)

    def test_omitted_decimals_stays_valid(self, client: TestClient, db_session: Session):
        user = make_user(db_session, role=UserRole.MANAGER)
        headers = bearer(create_access_token(subject=user.id, company_id=1))
        sheet_id = self._draft_sheet(client, headers)
        payload = self._step_payload({"lsl": 0.98, "nominal": 1.0, "usl": 1.02})
        response = client.post(f"/api/v1/process-sheets/{sheet_id}/steps", json=payload, headers=headers)
        assert response.status_code == 200, response.text


class TestSharedDocumentNumberGenerator:
    def test_shared_generator_increments_within_prefix_and_month(self, db_session: Session):
        # Item 12: one implementation behind all four call sites.
        from app.services.document_numbering import generate_document_number

        ensure_company(db_session, 1)
        first = generate_document_number(db_session, DocumentType.QUALITY_RECORD.value)
        prefix = first.rsplit("-", 1)[0]
        document = Document(
            document_number=first,
            title="Gen test",
            document_type=DocumentType.QUALITY_RECORD,
            status="released",
            company_id=1,
        )
        db_session.add(document)
        db_session.commit()

        second = generate_document_number(db_session, DocumentType.QUALITY_RECORD.value)
        assert second.rsplit("-", 1)[0] == prefix
        assert int(second.rsplit("-", 1)[1]) == int(first.rsplit("-", 1)[1]) + 1

    def test_call_sites_delegate_to_the_shared_generator(self, db_session: Session):
        from app.api.endpoints.documents import generate_document_number as documents_generate
        from app.services.document_numbering import generate_document_number as shared_generate

        assert documents_generate(db_session, "drawing") == shared_generate(db_session, "drawing")


class TestSharedSerialParser:
    def test_coc_parser_delegates_to_the_shared_parser(self):
        # Item 13: one parser; the CoC wrapper simply delegates.
        from app.services import process_sheet_service
        from app.services.coc_service import _parse_serial_numbers

        assert _parse_serial_numbers('["A", "B"]') == ["A", "B"]
        assert _parse_serial_numbers("not json") == []
        assert _parse_serial_numbers(None) == []
        assert _parse_serial_numbers(["X", 2]) == ["X", "2"]
        assert process_sheet_service.parse_serial_numbers('["A", "B"]') == ["A", "B"]
