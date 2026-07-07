"""Process Sheets PR 3 — gate-parity tests for the audit findings (B1 / S1 / S3).

B1: the clock-out auto-complete path must not flip an operation COMPLETE while required
snapshot steps lack conforming records — the TimeEntry always closes normally with its
full quantities (never-trap-an-open-TimeEntry precedent), the operation stays
IN_PROGRESS at target, and the response carries a ``steps_incomplete`` warning block.

S1: the read-time reconcile (``reconcile_work_orders_from_completion_evidence``) must
not auto-complete an operation from closed-TimeEntry evidence while the same gate is
unsatisfied — otherwise B1's refusal is undone by the next WO page load. Quantities
still reconcile; only the COMPLETE flip is withheld.

S3: ``attachment_document_id`` must reference exactly what the in-fence step-attachment
upload produces — a QUALITY_RECORD Document linked to THIS work order — not any
in-tenant document (evidence laundering).

S2 (settled, user decision): ``POST /work-orders/{id}/complete`` stays an UNGATED,
audited evidence-override — the force-complete succeeds, but the required step records
it bypassed are stamped on its audit row (``steps_bypassed_count`` / ``steps_bypassed``)
and summarized on the response (null when nothing was bypassed).

Deliberately self-contained (the capture test file is concurrently owned by the
test-engineer this round); factories mirror tests/api/kiosk_test_helpers.
"""

from datetime import datetime, timedelta
from io import BytesIO

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.document import Document, DocumentType
from app.models.process_sheet import OperationStepRecord, ProcessSheet, WOOperationStep
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services.work_order_state_service import reconcile_work_orders_from_completion_evidence
from tests.api.kiosk_test_helpers import bearer, make_user, make_wo_with_operation, make_work_center

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

CLOCK_IN_URL = "/api/v1/shop-floor/clock-in"
CLOCK_OUT_URL = "/api/v1/shop-floor/clock-out/{entry}"
COMPLETE_URL = "/api/v1/shop-floor/operations/{op}/complete"
RECORDS_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/records"
ATTACHMENT_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/attachment"

_seq = {"n": 5000}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_source_sheet(db: Session, company_id: int = 1) -> ProcessSheet:
    sheet = ProcessSheet(
        sheet_number=f"PS-{_next():06d}",
        title="Gate parity sheet",
        revision="A",
        status="released",
        company_id=company_id,
    )
    db.add(sheet)
    db.commit()
    db.refresh(sheet)
    return sheet


def _add_required_checkbox_step(db: Session, operation, label: str = "Torque verified") -> WOOperationStep:
    source = _make_source_sheet(db, company_id=operation.company_id)
    step = WOOperationStep(
        company_id=operation.company_id,
        work_order_operation_id=operation.id,
        source_sheet_id=source.id,
        source_sheet_revision=source.revision,
        sequence=_next(),
        label=label,
        step_type="checkbox",
        is_required=True,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return step


def _fixture(db: Session, *, op_status=OperationStatus.READY, wo_status=WorkOrderStatus.RELEASED):
    """Work center + WO/operation (target qty 10) + operator user headers."""
    work_center = make_work_center(db)
    work_order, operation = make_wo_with_operation(
        db, work_center=work_center, quantity_ordered=10, op_status=op_status, wo_status=wo_status
    )
    user = make_user(db, role=UserRole.OPERATOR)
    headers = bearer(create_access_token(subject=user.id, company_id=1))
    return work_order, operation, user, headers


def _clock_in(client: TestClient, headers: dict, work_order, operation) -> int:
    response = client.post(
        CLOCK_IN_URL,
        json={
            "work_order_id": work_order.id,
            "operation_id": operation.id,
            "work_center_id": operation.work_center_id,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _closed_time_entry(db: Session, work_order, operation, user, quantity: float) -> TimeEntry:
    """Insert a CLOSED labor entry directly (durable completion evidence for reconcile)."""
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


def _record_checkbox_true(client: TestClient, headers: dict, operation, step) -> dict:
    response = client.post(
        RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# B1 — clock-out auto-complete respects the gate
# ---------------------------------------------------------------------------


class TestClockOutGate:
    def test_clock_out_at_target_with_missing_steps_warns_and_does_not_complete(
        self, client: TestClient, db_session: Session
    ):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_required_checkbox_step(db_session, operation, label="Deburr verified")
        entry_id = _clock_in(client, headers, work_order, operation)

        response = client.post(CLOCK_OUT_URL.format(entry=entry_id), json={"quantity_produced": 10}, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()

        # Labor truth: the entry ALWAYS closes normally with its full quantities.
        assert body["clock_out"] is not None
        assert body["quantity_produced"] == 10.0
        # The warning block tells the kiosk why the op did not complete.
        assert body["steps_incomplete"]["code"] == "STEPS_INCOMPLETE"
        assert body["steps_incomplete"]["missing"] == [{"step_id": step.id, "label": "Deburr verified", "serials": []}]

        # The operation stays IN_PROGRESS at target — completion is withheld, not the labor.
        db_session.expire_all()
        assert operation.status == OperationStatus.IN_PROGRESS
        assert float(operation.quantity_complete) == 10.0
        assert operation.actual_end is None and operation.completed_by is None
        assert work_order.status != WorkOrderStatus.COMPLETE

        # No completion status-change lands on the tamper-evident chain.
        completion_audits = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == operation.id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .count()
        )
        assert completion_audits == 0

    def test_clock_out_at_target_with_steps_satisfied_completes_as_today(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_required_checkbox_step(db_session, operation)
        entry_id = _clock_in(client, headers, work_order, operation)
        _record_checkbox_true(client, headers, operation, step)

        response = client.post(CLOCK_OUT_URL.format(entry=entry_id), json={"quantity_produced": 10}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["steps_incomplete"] is None

        db_session.expire_all()
        assert operation.status == OperationStatus.COMPLETE
        assert operation.actual_end is not None

    def test_below_target_clock_out_is_unaffected(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(db_session)
        _add_required_checkbox_step(db_session, operation)
        entry_id = _clock_in(client, headers, work_order, operation)

        response = client.post(CLOCK_OUT_URL.format(entry=entry_id), json={"quantity_produced": 4}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["steps_incomplete"] is None

        db_session.expire_all()
        assert operation.status == OperationStatus.IN_PROGRESS
        assert float(operation.quantity_complete) == 4.0

    def test_gated_clock_out_then_record_then_complete_succeeds(self, client: TestClient, db_session: Session):
        # The documented escape path: gated clock-out -> record the step -> /complete.
        work_order, operation, user, headers = _fixture(db_session)
        step = _add_required_checkbox_step(db_session, operation)
        entry_id = _clock_in(client, headers, work_order, operation)

        gated = client.post(CLOCK_OUT_URL.format(entry=entry_id), json={"quantity_produced": 10}, headers=headers)
        assert gated.status_code == 200
        assert gated.json()["steps_incomplete"] is not None

        _record_checkbox_true(client, headers, operation, step)
        done = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert done.status_code == 200, done.text
        assert done.json()["is_fully_complete"] is True
        db_session.expire_all()
        assert operation.status == OperationStatus.COMPLETE


# ---------------------------------------------------------------------------
# S1 — read-time reconcile respects the gate
# ---------------------------------------------------------------------------


class TestReconcileGate:
    def test_reconcile_with_evidence_at_target_and_missing_steps_stays_in_progress(
        self, client: TestClient, db_session: Session
    ):
        work_order, operation, user, _ = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        _add_required_checkbox_step(db_session, operation)
        _closed_time_entry(db_session, work_order, operation, user, quantity=10)

        changed = reconcile_work_orders_from_completion_evidence(db_session, [work_order])
        db_session.commit()

        db_session.expire_all()
        # Quantities still reconcile from durable labor evidence...
        assert changed is True
        assert float(operation.quantity_complete) == 10.0
        # ...but the COMPLETE flip is withheld while required steps lack records.
        assert operation.status == OperationStatus.IN_PROGRESS
        assert operation.actual_end is None
        assert work_order.status != WorkOrderStatus.COMPLETE

    def test_reconcile_completes_as_today_once_steps_satisfied(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        step = _add_required_checkbox_step(db_session, operation)
        _closed_time_entry(db_session, work_order, operation, user, quantity=10)
        _record_checkbox_true(client, headers, operation, step)

        reconcile_work_orders_from_completion_evidence(db_session, [work_order])
        db_session.commit()

        db_session.expire_all()
        assert operation.status == OperationStatus.COMPLETE

    def test_reconcile_gate_is_per_serial(self, client: TestClient, db_session: Session):
        import json as _json

        work_order, operation, user, headers = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        work_order.serial_numbers = _json.dumps(["SN-1", "SN-2"])
        db_session.commit()
        step = _add_required_checkbox_step(db_session, operation)
        _closed_time_entry(db_session, work_order, operation, user, quantity=10)
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_bool": True, "serial_number": "SN-1"},
            headers=headers,
        )
        assert recorded.status_code == 201

        reconcile_work_orders_from_completion_evidence(db_session, [work_order])
        db_session.commit()
        db_session.expire_all()
        assert operation.status == OperationStatus.IN_PROGRESS  # SN-2 still missing

    def test_reconcile_without_steps_completes_exactly_as_today(self, client: TestClient, db_session: Session):
        # Zero-step regression guard for the reconcile path.
        work_order, operation, user, _ = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        _closed_time_entry(db_session, work_order, operation, user, quantity=10)

        reconcile_work_orders_from_completion_evidence(db_session, [work_order])
        db_session.commit()
        db_session.expire_all()
        assert operation.status == OperationStatus.COMPLETE


# ---------------------------------------------------------------------------
# S3 — attachment evidence must be THIS WO's QUALITY_RECORD
# ---------------------------------------------------------------------------


class TestAttachmentEvidenceBinding:
    def _photo_step(self, db: Session, operation) -> WOOperationStep:
        source = _make_source_sheet(db, company_id=operation.company_id)
        step = WOOperationStep(
            company_id=operation.company_id,
            work_order_operation_id=operation.id,
            source_sheet_id=source.id,
            source_sheet_revision=source.revision,
            sequence=_next(),
            label="Weld photo",
            step_type="photo",
            is_required=True,
        )
        db.add(step)
        db.commit()
        db.refresh(step)
        return step

    def _make_document(self, db: Session, *, document_type: DocumentType, work_order_id=None) -> Document:
        document = Document(
            document_number=f"DOC-GP-{_next():06d}",
            title="Some document",
            document_type=document_type,
            work_order_id=work_order_id,
            status="released",
            company_id=1,
        )
        db.add(document)
        db.commit()
        db.refresh(document)
        return document

    def test_same_tenant_drawing_is_rejected(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        step = self._photo_step(db_session, operation)
        drawing = self._make_document(db_session, document_type=DocumentType.DRAWING, work_order_id=work_order.id)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"attachment_document_id": drawing.id},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert "QUALITY_RECORD" in response.json()["detail"]
        assert (
            db_session.query(OperationStepRecord)
            .filter(OperationStepRecord.work_order_operation_id == operation.id)
            .count()
            == 0
        )

    def test_another_work_orders_quality_record_is_rejected(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        other_wc = make_work_center(db_session)
        other_wo, _ = make_wo_with_operation(db_session, work_center=other_wc)
        step = self._photo_step(db_session, operation)
        foreign_evidence = self._make_document(
            db_session, document_type=DocumentType.QUALITY_RECORD, work_order_id=other_wo.id
        )
        unlinked_evidence = self._make_document(
            db_session, document_type=DocumentType.QUALITY_RECORD, work_order_id=None
        )

        for document in (foreign_evidence, unlinked_evidence):
            response = client.post(
                RECORDS_URL.format(op=operation.id, step=step.id),
                json={"attachment_document_id": document.id},
                headers=headers,
            )
            assert response.status_code == 400, response.text
            assert "belonging to this" in response.json()["detail"]

    def test_two_step_upload_then_record_flow_still_works(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        step = self._photo_step(db_session, operation)

        uploaded = client.post(
            ATTACHMENT_URL.format(op=operation.id, step=step.id),
            headers=headers,
            files={"file": ("weld.png", BytesIO(b"\x89PNG\r\n\x1a\npixels"), "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text

        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"attachment_document_id": uploaded.json()["document_id"]},
            headers=headers,
        )
        assert recorded.status_code == 201, recorded.text
        assert recorded.json()["attachment_document_id"] == uploaded.json()["document_id"]

    def test_unknown_document_stays_404(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        step = self._photo_step(db_session, operation)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"attachment_document_id": 987654},
            headers=headers,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# S2 — WO force-complete is an UNGATED, audited evidence-override
# ---------------------------------------------------------------------------


class TestForceCompleteEvidenceOverride:
    FORCE_COMPLETE_URL = "/api/v1/work-orders/{wo}/complete"

    def _manager_headers(self, db_session: Session) -> dict:
        manager = make_user(db_session, role=UserRole.MANAGER)
        return bearer(create_access_token(subject=manager.id, company_id=1))

    def _wo_audit_extra(self, db_session: Session, work_order_id: int) -> dict:
        row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order",
                AuditLog.resource_id == work_order_id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .one()
        )
        import json as _json

        return _json.loads(row.extra_data) if isinstance(row.extra_data, str) else row.extra_data

    def test_force_complete_bypasses_missing_steps_but_records_the_bypass(
        self, client: TestClient, db_session: Session
    ):
        work_order, operation, user, _ = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        step = _add_required_checkbox_step(db_session, operation, label="Final inspect")
        headers = self._manager_headers(db_session)

        response = client.post(
            self.FORCE_COMPLETE_URL.format(wo=work_order.id),
            params={"quantity_complete": 10},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()

        # UNGATED: the override completes the WO (and force-completes the open op)...
        db_session.expire_all()
        assert work_order.status == WorkOrderStatus.COMPLETE
        assert operation.status == OperationStatus.COMPLETE

        # ...but the bypass is deliberate and visible on the response...
        expected_entry = {
            "operation": operation.operation_number or f"Op {operation.sequence}",
            "step_id": step.id,
            "label": "Final inspect",
            "serials": [],
        }
        assert body["steps_bypassed"] == {"count": 1, "steps": [expected_entry], "truncated": False}

        # ...and stamped on the force-complete audit row's extra_data.
        extra = self._wo_audit_extra(db_session, work_order.id)
        assert extra["steps_bypassed_count"] == 1
        assert extra["steps_bypassed"] == [expected_entry]
        assert extra["steps_bypassed_truncated"] is False

    def test_force_complete_with_steps_satisfied_reports_no_bypass(self, client: TestClient, db_session: Session):
        work_order, operation, user, operator_headers = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        step = _add_required_checkbox_step(db_session, operation)
        _record_checkbox_true(client, operator_headers, operation, step)
        headers = self._manager_headers(db_session)

        response = client.post(
            self.FORCE_COMPLETE_URL.format(wo=work_order.id),
            params={"quantity_complete": 10},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["steps_bypassed"] is None  # backward-compatible null

        db_session.expire_all()
        assert work_order.status == WorkOrderStatus.COMPLETE
        extra = self._wo_audit_extra(db_session, work_order.id)
        assert extra["steps_bypassed_count"] == 0
        assert extra["steps_bypassed"] == []
        assert extra["steps_bypassed_truncated"] is False

    def test_force_complete_bypass_is_per_serial(self, client: TestClient, db_session: Session):
        import json as _json

        work_order, operation, user, operator_headers = _fixture(
            db_session, op_status=OperationStatus.IN_PROGRESS, wo_status=WorkOrderStatus.IN_PROGRESS
        )
        work_order.serial_numbers = _json.dumps(["SN-1", "SN-2"])
        db_session.commit()
        step = _add_required_checkbox_step(db_session, operation, label="Serial check")
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_bool": True, "serial_number": "SN-1"},
            headers=operator_headers,
        )
        assert recorded.status_code == 201
        headers = self._manager_headers(db_session)

        response = client.post(
            self.FORCE_COMPLETE_URL.format(wo=work_order.id),
            params={"quantity_complete": 10},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        bypass = response.json()["steps_bypassed"]
        assert bypass["count"] == 1
        assert bypass["steps"][0]["serials"] == ["SN-2"]  # names exactly which unit lacks evidence
