"""Process Sheets PR 4 — compliance-audit + code-review fix round.

SF-1: the quality-hold one-tap must VERIFY the value is actually out of tolerance —
in-band values are refused (409 ``VALUE_IN_TOLERANCE``, no NCR/blocker), boundary
values count as in-band, and a snapshot config without numeric limits is a 400.

SF-2: FAI pre-fill never overwrites an inspector-entered ``measuring_device`` (only
sets it when blank, reported honestly via ``device_preserved``), and device changes
land in the audit old/new diff alongside the actual values.

N-1: the quality-hold body accepts the gauge as ``equipment_id`` OR ``equipment_code``
(tenant-scoped resolution, both -> 400, unknown -> 404) with NO calibration gating —
the escape hatch never traps the operator behind a stale gauge; the resolved identity
lands server-side in the NCR description + audit extra_data.

Code review: the hold path locks the operation (FOR UPDATE) and re-verifies
IN_PROGRESS after the lock, so a concurrent double-tap can't file a duplicate
NCR/blocker; gauge-code lookup is case-insensitive (scan OR type) and echoes the
canonical stored code.

Deliberately self-contained (test_process_sheet_integrations.py is concurrently owned
by the test-engineer this round); factories mirror that file's.
"""

import json
from datetime import date, timedelta

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.calibration import CalibrationStatus, Equipment
from app.models.process_sheet import ProcessSheet, WOOperationStep
from app.models.quality import FAICharacteristic, FirstArticleInspection, NonConformanceReport
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.schemas.process_sheet import QualityHoldRequest
from app.services import process_sheet_service
from app.services.audit_service import AuditService
from tests.api.kiosk_test_helpers import bearer, make_user, make_wo_with_operation, make_work_center

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

RECORDS_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/records"
QUALITY_HOLD_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/quality-hold"
FAI_PREFILL_URL = "/api/v1/quality/fai/{fai}/prefill-from-steps"

MEASUREMENT_CONFIG = {"lsl": 0.98, "nominal": 1.0, "usl": 1.02, "unit": "in", "decimals": 3}

_seq = {"n": 20000}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_source_sheet(db: Session, company_id: int = 1) -> ProcessSheet:
    sheet = ProcessSheet(
        sheet_number=f"PS-{_next():06d}",
        title="Audit-fix sheet",
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
    operation,
    *,
    step_type: str = "measurement",
    config: dict = "DEFAULT",
    label: str = None,
    requires_gauge: bool = False,
) -> WOOperationStep:
    source = _make_source_sheet(db, company_id=operation.company_id)
    if config == "DEFAULT":
        config = dict(MEASUREMENT_CONFIG) if step_type == "measurement" else None
    step = WOOperationStep(
        company_id=operation.company_id,
        work_order_operation_id=operation.id,
        source_sheet_id=source.id,
        source_sheet_revision=source.revision,
        sequence=_next(),
        label=label or f"Check {_next()}",
        step_type=step_type,
        is_required=True,
        config=config,
        requires_gauge=requires_gauge,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return step


def _fixture(db: Session, *, role: UserRole = UserRole.OPERATOR):
    work_center = make_work_center(db)
    work_order, operation = make_wo_with_operation(
        db,
        work_center=work_center,
        op_status=OperationStatus.IN_PROGRESS,
        wo_status=WorkOrderStatus.IN_PROGRESS,
    )
    user = make_user(db, role=role)
    headers = bearer(create_access_token(subject=user.id, company_id=1))
    return work_order, operation, user, headers


def _make_gauge(
    db: Session,
    *,
    company_id: int = 1,
    gauge_status: CalibrationStatus = CalibrationStatus.ACTIVE,
    next_calibration_date=date.today() + timedelta(days=90),
    name: str = "Caliper",
    code: str = None,
) -> Equipment:
    gauge = Equipment(
        equipment_id=code or f"GAF-{_next():05d}",
        name=name,
        status=gauge_status,
        next_calibration_date=next_calibration_date,
        company_id=company_id,
    )
    db.add(gauge)
    db.commit()
    db.refresh(gauge)
    return gauge


def _ncr_count(db: Session) -> int:
    return db.query(NonConformanceReport).count()


def _blocker_count(db: Session) -> int:
    return db.query(WorkOrderBlocker).count()


# ---------------------------------------------------------------------------
# SF-1 — quality-hold verifies the value is genuinely out of tolerance
# ---------------------------------------------------------------------------


class TestQualityHoldVerifiesOOT:
    def test_in_band_value_is_409_with_no_ncr_or_blocker(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        ncrs_before, blockers_before = _ncr_count(db_session), _blocker_count(db_session)

        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.0},  # comfortably in band
            headers=headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "VALUE_IN_TOLERANCE"
        assert detail["measured"] == 1.0
        assert detail["lsl"] == 0.98 and detail["usl"] == 1.02

        assert _ncr_count(db_session) == ncrs_before  # nothing filed
        assert _blocker_count(db_session) == blockers_before
        db_session.refresh(operation)
        assert operation.status == OperationStatus.IN_PROGRESS  # not held

    @pytest.mark.parametrize("boundary", [0.98, 1.02])
    def test_boundary_values_count_as_in_band(self, client: TestClient, db_session: Session, boundary):
        # Conformance at capture is lsl <= v <= usl INCLUSIVE — the hold must agree.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": boundary},
            headers=headers,
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "VALUE_IN_TOLERANCE"

    def test_rounding_matches_capture_before_the_band_check(self, client: TestClient, db_session: Session):
        # 1.0204 rounds to 1.02 at config decimals=3... (capture would have accepted
        # it as conforming), so the hold refuses it as in-band — never both paths.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.0204},
            headers=headers,
        )
        assert response.status_code == 409
        assert response.json()["detail"]["measured"] == 1.02

    def test_config_without_numeric_limits_is_400(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, config={"nominal": 1.0, "unit": "in"})  # no lsl/usl
        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 5.0},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert "no numeric tolerance limits" in response.json()["detail"]

    def test_genuine_oot_still_files(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.31},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        ncr = db_session.query(NonConformanceReport).filter_by(id=response.json()["ncr_id"]).one()
        assert ncr.actual_value == "1.31"
        db_session.refresh(operation)
        assert operation.status == OperationStatus.ON_HOLD


# ---------------------------------------------------------------------------
# SF-2 — FAI pre-fill never overwrites an inspector-entered measuring_device
# ---------------------------------------------------------------------------


class TestFAIPrefillDevicePreservation:
    def _fai_with_char(self, db_session, work_order, *, measuring_device=None) -> FirstArticleInspection:
        fai = FirstArticleInspection(
            fai_number=f"FAI-AFX-{_next():05d}",
            part_id=work_order.part_id,
            work_order_id=work_order.id,
            company_id=1,
        )
        db_session.add(fai)
        db_session.flush()
        db_session.add(
            FAICharacteristic(
                fai_id=fai.id,
                char_number=1,
                characteristic="Bore dia",
                nominal="1.0",
                measuring_device=measuring_device,
                company_id=1,
            )
        )
        db_session.commit()
        db_session.refresh(fai)
        return fai

    def _record_measurement(self, client, db_session, operation, headers, *, gauge=None):
        step = _add_step(db_session, operation, label="Bore dia")
        payload = {"value_numeric": 1.001}
        if gauge is not None:
            payload["equipment_id"] = gauge.id
        response = client.post(RECORDS_URL.format(op=operation.id, step=step.id), json=payload, headers=headers)
        assert response.status_code == 201, response.text
        return step

    def test_inspector_entered_device_is_kept_and_reported(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        gauge = _make_gauge(db_session, name="Prefill mic 9")
        self._record_measurement(client, db_session, operation, headers, gauge=gauge)
        # Device entered by the inspector, value still blank -> value fills, device stays.
        fai = self._fai_with_char(db_session, work_order, measuring_device="CMM program 4")

        response = client.post(FAI_PREFILL_URL.format(fai=fai.id), headers=headers)
        assert response.status_code == 200, response.text
        entry = response.json()["prefilled"][0]
        assert entry["actual_value"] == "1.001"
        assert entry["measuring_device"] == "CMM program 4"  # NOT the gauge's name
        assert entry["device_preserved"] is True  # the honest report shape

        char = db_session.query(FAICharacteristic).filter(FAICharacteristic.fai_id == fai.id).one()
        assert char.measuring_device == "CMM program 4"
        assert char.actual_value == "1.001"

        # No device change happened -> the audit diff carries NO measuring_devices key.
        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "fai", AuditLog.resource_id == fai.id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        new_values = json.loads(audit_row.new_values) if isinstance(audit_row.new_values, str) else audit_row.new_values
        assert "measuring_devices" not in (new_values or {})

    def test_blank_device_is_set_and_audited_with_old_new_pairs(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        gauge = _make_gauge(db_session, name="Prefill mic 10")
        self._record_measurement(client, db_session, operation, headers, gauge=gauge)
        fai = self._fai_with_char(db_session, work_order, measuring_device=None)

        response = client.post(FAI_PREFILL_URL.format(fai=fai.id), headers=headers)
        assert response.status_code == 200, response.text
        entry = response.json()["prefilled"][0]
        assert entry["measuring_device"] == "Prefill mic 10"
        assert entry["device_preserved"] is False

        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "fai", AuditLog.resource_id == fai.id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row is not None
        old_values = json.loads(audit_row.old_values) if isinstance(audit_row.old_values, str) else audit_row.old_values
        new_values = json.loads(audit_row.new_values) if isinstance(audit_row.new_values, str) else audit_row.new_values
        assert old_values["measuring_devices"] == {"1": None}
        assert new_values["measuring_devices"] == {"1": "Prefill mic 10"}
        assert new_values["actual_values"] == {"1": "1.001"}


# ---------------------------------------------------------------------------
# N-1 — gauge identity on the quality hold (no calibration gating)
# ---------------------------------------------------------------------------


class TestQualityHoldGaugeIdentity:
    def test_hold_with_equipment_code_lands_identity_in_ncr_and_audit(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        gauge = _make_gauge(db_session, name="Height gauge 2")

        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.31, "equipment_code": gauge.equipment_id, "notes": "Bent tip?"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        ncr = db_session.query(NonConformanceReport).filter_by(id=response.json()["ncr_id"]).one()
        # Server-resolved identity, not client prose — and the notes still land.
        assert f"Measured with gauge {gauge.equipment_id} — Height gauge 2." in ncr.description
        assert "Bent tip?" in ncr.description

        audit_row = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "ncr", AuditLog.resource_id == ncr.id).one()
        )
        extra = json.loads(audit_row.extra_data) if isinstance(audit_row.extra_data, str) else audit_row.extra_data
        assert extra["equipment_id"] == gauge.id
        assert extra["equipment_code"] == gauge.equipment_id

    def test_stale_gauge_is_accepted_no_calibration_gating(self, client: TestClient, db_session: Session):
        # The escape hatch must NEVER trap the operator behind an out-of-cal gauge —
        # an OOT reading from a stale gauge is precisely what needs the hold.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, requires_gauge=True)
        stale = _make_gauge(db_session, next_calibration_date=date.today() - timedelta(days=30))

        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.31, "equipment_code": stale.equipment_id},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        ncr = db_session.query(NonConformanceReport).filter_by(id=response.json()["ncr_id"]).one()
        assert stale.equipment_id in ncr.description

    def test_both_fields_400_and_unknown_code_404(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        gauge = _make_gauge(db_session)
        url = QUALITY_HOLD_URL.format(op=operation.id, step=step.id)

        both = client.post(
            url,
            json={"measured_value": 1.31, "equipment_id": gauge.id, "equipment_code": gauge.equipment_id},
            headers=headers,
        )
        assert both.status_code == 400
        assert "not both" in both.json()["detail"]

        unknown = client.post(url, json={"measured_value": 1.31, "equipment_code": "GAF-NOPE"}, headers=headers)
        assert unknown.status_code == 404
        assert "No gauge with identifier 'GAF-NOPE'" in unknown.json()["detail"]


# ---------------------------------------------------------------------------
# Code review 1 — concurrent double-tap cannot file a duplicate NCR/blocker
# ---------------------------------------------------------------------------


class TestQualityHoldDoubleTap:
    def test_second_sequential_tap_is_refused_end_to_end(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        url = QUALITY_HOLD_URL.format(op=operation.id, step=step.id)

        first = client.post(url, json={"measured_value": 1.31}, headers=headers)
        assert first.status_code == 201, first.text
        ncrs_after_first, blockers_after_first = _ncr_count(db_session), _blocker_count(db_session)

        second = client.post(url, json={"measured_value": 1.31}, headers=headers)
        assert second.status_code == 400  # op is ON_HOLD now
        assert _ncr_count(db_session) == ncrs_after_first  # no duplicate NCR
        assert _blocker_count(db_session) == blockers_after_first  # no duplicate blocker

    def test_post_lock_recheck_refuses_a_stale_caller_directly(self, client: TestClient, db_session: Session):
        # TRUE concurrency isn't reliably testable here (SQLite + the shared test
        # session make FOR UPDATE a no-op), so this asserts the SERVICE-level
        # post-lock re-check directly: a caller holding a stale IN_PROGRESS view of
        # an operation that is ALREADY ON_HOLD in the DB (exactly what the second
        # concurrent tap sees after the first commits) is refused inside
        # create_quality_hold — independent of the endpoint's unlocked pre-check.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        operation.status = OperationStatus.ON_HOLD  # the DB truth the lock re-reads
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            process_sheet_service.create_quality_hold(
                db_session,
                1,
                work_order=work_order,
                operation=operation,
                step=step,
                data=QualityHoldRequest(measured_value=1.31),
                user=user,
                audit=AuditService(db_session, user),
                source=None,
            )
        assert exc_info.value.status_code == 400
        assert "must be in progress to raise a quality hold" in exc_info.value.detail
        db_session.rollback()
        assert db_session.query(NonConformanceReport).filter_by(work_order_id=work_order.id).count() == 0


# ---------------------------------------------------------------------------
# Code review 2 — gauge code lookup is case-insensitive (scan OR type)
# ---------------------------------------------------------------------------


class TestGaugeCodeCaseInsensitive:
    def test_mixed_case_typed_code_resolves_and_echoes_canonical_code(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation, requires_gauge=True)
        gauge = _make_gauge(db_session, name="Depth mic 1", code=f"GAF-Mixed-{_next()}")

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "equipment_code": gauge.equipment_id.lower()},  # typed lowercase
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["equipment_id"] == gauge.id
        assert body["gauge"]["equipment_code"] == gauge.equipment_id  # CANONICAL stored code

    def test_case_insensitivity_applies_to_the_hold_path_too(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_step(db_session, operation)
        gauge = _make_gauge(db_session, name="Bore gauge 5", code=f"GAF-Case-{_next()}")

        response = client.post(
            QUALITY_HOLD_URL.format(op=operation.id, step=step.id),
            json={"measured_value": 1.31, "equipment_code": gauge.equipment_id.upper()},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        ncr = db_session.query(NonConformanceReport).filter_by(id=response.json()["ncr_id"]).one()
        assert gauge.equipment_id in ncr.description  # canonical code in the identity line
