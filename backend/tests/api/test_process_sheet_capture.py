"""Process Sheets PR 3 tests (docs/PROCESS_SHEETS_SCOPE.md) — snapshot + capture + gating.

Covers: the WO-creation snapshot (family resolution to the currently-RELEASED revision,
the PROCESS_SHEET_UNAVAILABLE 409 with atomic rollback, both callers — POST /work-orders
and the Excel-migration import), the shop-floor capture validation ladder per step type
(OOT refusal with no row, serial validation, equipment passthrough), supersede-once
corrections, operation-completion gating (per-serial, zero-step regression, partials),
the kiosk-token path fence (positive on the new endpoints, negative on /documents/upload),
tenant isolation, PHOTO/FILE evidence upload, and the tamper-evident audit rows.
"""

import json
from io import BytesIO

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.calibration import Equipment
from app.models.document import Document, DocumentType
from app.models.part import Part
from app.models.process_sheet import OperationStepRecord, ProcessSheet, ProcessSheetStep, WOOperationStep
from app.models.routing import Routing, RoutingOperation
from app.models.user import UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from tests.api.kiosk_test_helpers import (
    bearer,
    ensure_company,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

STEPS_URL = "/api/v1/shop-floor/operations/{op}/steps"
RECORDS_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/records"
SUPERSEDE_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/records/{record}/supersede"
ATTACHMENT_URL = "/api/v1/shop-floor/operations/{op}/steps/{step}/attachment"
COMPLETE_URL = "/api/v1/shop-floor/operations/{op}/complete"

_seq = {"n": 100}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_sheet(
    db: Session,
    *,
    company_id: int = 1,
    sheet_number: str = None,
    revision: str = "A",
    sheet_status: str = "released",
    steps: list = None,
    is_deleted: bool = False,
) -> ProcessSheet:
    """Insert a ProcessSheet row (+ optional step definitions) directly."""
    ensure_company(db, company_id)
    sheet = ProcessSheet(
        sheet_number=sheet_number or f"PS-{_next():06d}",
        title="Final inspect",
        revision=revision,
        status=sheet_status,
        is_active=sheet_status != "obsolete",
        company_id=company_id,
    )
    if is_deleted:
        sheet.is_deleted = True
    db.add(sheet)
    db.flush()
    for i, step in enumerate(steps or []):
        db.add(
            ProcessSheetStep(
                process_sheet_id=sheet.id,
                company_id=company_id,
                sequence=step.get("sequence", (i + 1) * 10),
                label=step.get("label", f"Step {(i + 1) * 10}"),
                instruction_text=step.get("instruction_text"),
                step_type=step.get("step_type", "measurement"),
                is_required=step.get("is_required", True),
                config=step.get("config"),
                requires_gauge=step.get("requires_gauge", False),
            )
        )
    db.commit()
    db.refresh(sheet)
    return sheet


MEASUREMENT_CONFIG = {"lsl": 0.98, "nominal": 1.0, "usl": 1.02, "unit": "in", "decimals": 3}


def _make_routed_part_with_sheet(db: Session, part_number: str, sheet: ProcessSheet, company_id: int = 1) -> Part:
    """Manufactured part with a released single-operation routing, sheet attached."""
    work_center = WorkCenter(
        code=f"WC-{part_number}",
        name=f"WC for {part_number}",
        work_center_type="machining",
        is_active=True,
        company_id=company_id,
    )
    db.add(work_center)
    db.flush()
    part = Part(
        part_number=part_number,
        name=f"Part {part_number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    routing = Routing(part_id=part.id, status="released", is_active=True, company_id=company_id)
    db.add(routing)
    db.flush()
    db.add(
        RoutingOperation(
            routing_id=routing.id,
            sequence=10,
            operation_number="Op 10",
            name="Machine",
            work_center_id=work_center.id,
            run_hours_per_unit=0.1,
            is_active=True,
            process_sheet_id=sheet.id,
            company_id=company_id,
        )
    )
    db.commit()
    db.refresh(part)
    return part


def _add_wo_step(
    db: Session,
    operation: WorkOrderOperation,
    *,
    step_type: str = "measurement",
    is_required: bool = True,
    config: dict = None,
    sequence: int = None,
    label: str = None,
    company_id: int = 1,
) -> WOOperationStep:
    """Insert a snapshot step directly onto a WO operation (bypasses the snapshot path)."""
    source = _make_sheet(db, company_id=company_id)
    if config is None and step_type == "measurement":
        config = dict(MEASUREMENT_CONFIG)
    step = WOOperationStep(
        company_id=company_id,
        work_order_operation_id=operation.id,
        source_sheet_id=source.id,
        source_sheet_revision=source.revision,
        sequence=sequence or _next(),
        label=label or f"Check {step_type}",
        step_type=step_type,
        is_required=is_required,
        config=config,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return step


def _capture_fixture(db: Session, *, serials: list = None, op_status=OperationStatus.IN_PROGRESS):
    """Work center + IN_PROGRESS WO/operation + a manager header for capture tests."""
    work_center = make_work_center(db)
    work_order, operation = make_wo_with_operation(
        db,
        work_center=work_center,
        op_status=op_status,
        wo_status=WorkOrderStatus.IN_PROGRESS,
    )
    if serials:
        work_order.serial_numbers = json.dumps(serials)
        db.commit()
    user = make_user(db, role=UserRole.MANAGER)
    headers = bearer(create_access_token(subject=user.id, company_id=1))
    return work_order, operation, user, headers


def _record_rows(db: Session, operation_id: int) -> list:
    return (
        db.query(OperationStepRecord)
        .filter(OperationStepRecord.work_order_operation_id == operation_id)
        .order_by(OperationStepRecord.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Snapshot at WO creation
# ---------------------------------------------------------------------------


class TestSnapshotAtWOCreation:
    def test_snapshot_copies_released_steps_with_traceability(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _make_sheet(
            db_session,
            steps=[
                {"step_type": "measurement", "config": dict(MEASUREMENT_CONFIG), "label": "Bore dia"},
                {"step_type": "instruction", "is_required": False, "label": "Deburr note"},
            ],
        )
        part = _make_routed_part_with_sheet(db_session, "PS3-SNAP-1", sheet)

        response = client.post(
            "/api/v1/work-orders/",
            json={"part_id": part.id, "quantity_ordered": 5},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        wo_id = response.json()["id"]

        operation = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == wo_id).one()
        steps = (
            db_session.query(WOOperationStep)
            .filter(WOOperationStep.work_order_operation_id == operation.id)
            .order_by(WOOperationStep.sequence)
            .all()
        )
        assert len(steps) == 2
        assert steps[0].label == "Bore dia"
        assert steps[0].config == dict(MEASUREMENT_CONFIG)
        assert steps[0].company_id == 1
        assert all(s.source_sheet_id == sheet.id and s.source_sheet_revision == "A" for s in steps)
        assert steps[1].step_type == "instruction" and steps[1].is_required is False

        # WO-creation audit row carries the snapshot summary.
        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo_id)
            .one()
        )
        extra = json.loads(audit_row.extra_data) if isinstance(audit_row.extra_data, str) else audit_row.extra_data
        snapshot = extra["process_sheet_snapshot"]
        assert snapshot == [
            {
                "operation": "Op 10",
                "operation_sequence": 10,
                "attached_sheet_id": sheet.id,
                "sheet_number": sheet.sheet_number,
                "resolved_sheet_id": sheet.id,
                "resolved_revision": "A",
                "step_count": 2,
            }
        ]

    def test_snapshot_resolves_family_to_currently_released_revision(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        number = f"PS-{_next():06d}"
        rev_a = _make_sheet(
            db_session,
            sheet_number=number,
            revision="A",
            sheet_status="obsolete",
            steps=[{"step_type": "checkbox", "label": "Old A step"}],
        )
        rev_b = _make_sheet(
            db_session,
            sheet_number=number,
            revision="B",
            sheet_status="released",
            steps=[{"step_type": "checkbox", "label": "New B step"}],
        )
        # Routing still points at the OLD revision row — family resolution must find B.
        part = _make_routed_part_with_sheet(db_session, "PS3-SNAP-2", rev_a)

        response = client.post(
            "/api/v1/work-orders/",
            json={"part_id": part.id, "quantity_ordered": 1},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text

        step = (
            db_session.query(WOOperationStep)
            .join(WorkOrderOperation, WOOperationStep.work_order_operation_id == WorkOrderOperation.id)
            .filter(WorkOrderOperation.work_order_id == response.json()["id"])
            .one()
        )
        assert step.source_sheet_id == rev_b.id
        assert step.source_sheet_revision == "B"
        assert step.label == "New B step"

    def test_no_released_revision_blocks_creation_atomically(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _make_sheet(db_session, sheet_status="obsolete", steps=[{"step_type": "checkbox", "label": "Orphaned"}])
        part = _make_routed_part_with_sheet(db_session, "PS3-SNAP-3", sheet)
        wo_count_before = db_session.query(WorkOrder).count()

        response = client.post(
            "/api/v1/work-orders/",
            json={"part_id": part.id, "quantity_ordered": 1},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "PROCESS_SHEET_UNAVAILABLE"
        assert detail["operation"] == "Op 10"
        assert detail["sheet_number"] == sheet.sheet_number
        assert sheet.sheet_number in detail["detail"] and "no released revision" in detail["detail"]

        # Atomic rollback: nothing was COMMITTED. The conftest get_db override shares
        # this session with the app and never closes it, so the aborted request's
        # pending (uncommitted) rows linger in the identity map — roll back first,
        # exactly as the real request teardown (session close) would.
        db_session.rollback()
        assert db_session.query(WorkOrder).count() == wo_count_before
        assert db_session.query(WOOperationStep).count() == 0

    def test_soft_deleted_released_revision_does_not_resolve(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _make_sheet(db_session, sheet_status="released", is_deleted=True, steps=[{"step_type": "checkbox"}])
        part = _make_routed_part_with_sheet(db_session, "PS3-SNAP-4", sheet)

        response = client.post(
            "/api/v1/work-orders/",
            json={"part_id": part.id, "quantity_ordered": 1},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"]["code"] == "PROCESS_SHEET_UNAVAILABLE"

    def test_operations_without_sheets_snapshot_nothing(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        # A routed part with NO sheet attached creates zero snapshot rows and succeeds.
        sheet = _make_sheet(db_session, steps=[{"step_type": "checkbox"}])
        part = _make_routed_part_with_sheet(db_session, "PS3-SNAP-5", sheet)
        routing_op = db_session.query(RoutingOperation).filter(RoutingOperation.process_sheet_id == sheet.id).one()
        routing_op.process_sheet_id = None
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            json={"part_id": part.id, "quantity_ordered": 2},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert db_session.query(WOOperationStep).count() == 0

    def test_two_operations_sharing_one_sheet_snapshot_per_operation(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        # One family resolution, but the steps are copied PER OPERATION — each op
        # carries its own independent traveler rows.
        sheet = _make_sheet(
            db_session,
            steps=[{"step_type": "checkbox", "label": "Shared check"}, {"step_type": "value", "label": "Shared value"}],
        )
        part = _make_routed_part_with_sheet(db_session, "PS3-SNAP-6", sheet)
        routing = db_session.query(Routing).filter(Routing.part_id == part.id).one()
        first_op = db_session.query(RoutingOperation).filter(RoutingOperation.routing_id == routing.id).one()
        db_session.add(
            RoutingOperation(
                routing_id=routing.id,
                sequence=20,
                operation_number="Op 20",
                name="Inspect",
                work_center_id=first_op.work_center_id,
                run_hours_per_unit=0.1,
                is_active=True,
                process_sheet_id=sheet.id,
                company_id=1,
            )
        )
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            json={"part_id": part.id, "quantity_ordered": 1},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        wo_id = response.json()["id"]

        operations = (
            db_session.query(WorkOrderOperation)
            .filter(WorkOrderOperation.work_order_id == wo_id)
            .order_by(WorkOrderOperation.sequence)
            .all()
        )
        assert len(operations) == 2
        for operation in operations:
            labels = [
                s.label
                for s in db_session.query(WOOperationStep)
                .filter(WOOperationStep.work_order_operation_id == operation.id)
                .order_by(WOOperationStep.sequence)
                .all()
            ]
            assert labels == ["Shared check", "Shared value"]  # full copy on EACH operation

        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo_id)
            .one()
        )
        extra = json.loads(audit_row.extra_data) if isinstance(audit_row.extra_data, str) else audit_row.extra_data
        snapshot = extra["process_sheet_snapshot"]
        assert [entry["operation"] for entry in snapshot] == ["Op 10", "Op 20"]
        assert {entry["resolved_sheet_id"] for entry in snapshot} == {sheet.id}  # one resolution, two entries

    def test_two_released_revisions_resolve_to_highest_letter_deterministically(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        # Deliberate transition window: A and B both RELEASED -> B wins.
        number = f"PS-{_next():06d}"
        rev_a = _make_sheet(
            db_session, sheet_number=number, revision="A", steps=[{"step_type": "checkbox", "label": "A step"}]
        )
        _make_sheet(db_session, sheet_number=number, revision="B", steps=[{"step_type": "checkbox", "label": "B step"}])
        part = _make_routed_part_with_sheet(db_session, "PS3-SNAP-7", rev_a)

        response = client.post(
            "/api/v1/work-orders/", json={"part_id": part.id, "quantity_ordered": 1}, headers=auth_headers
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        step = (
            db_session.query(WOOperationStep)
            .join(WorkOrderOperation, WOOperationStep.work_order_operation_id == WorkOrderOperation.id)
            .filter(WorkOrderOperation.work_order_id == response.json()["id"])
            .one()
        )
        assert step.source_sheet_revision == "B" and step.label == "B step"

        # Length-then-value ordering: Z and AA both released -> AA wins (NOT plain
        # lexicographic, where "Z" > "AA" would pick the older revision).
        number2 = f"PS-{_next():06d}"
        rev_z = _make_sheet(
            db_session, sheet_number=number2, revision="Z", steps=[{"step_type": "checkbox", "label": "Z step"}]
        )
        _make_sheet(
            db_session, sheet_number=number2, revision="AA", steps=[{"step_type": "checkbox", "label": "AA step"}]
        )
        part2 = _make_routed_part_with_sheet(db_session, "PS3-SNAP-8", rev_z)

        response = client.post(
            "/api/v1/work-orders/", json={"part_id": part2.id, "quantity_ordered": 1}, headers=auth_headers
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        step = (
            db_session.query(WOOperationStep)
            .join(WorkOrderOperation, WOOperationStep.work_order_operation_id == WorkOrderOperation.id)
            .filter(WorkOrderOperation.work_order_id == response.json()["id"])
            .one()
        )
        assert step.source_sheet_revision == "AA" and step.label == "AA step"

    def test_excel_import_snapshots_and_blocks_per_row(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        good_sheet = _make_sheet(db_session, steps=[{"step_type": "checkbox", "label": "Torque check"}])
        _make_routed_part_with_sheet(db_session, "PS3-IMP-OK", good_sheet)
        bad_sheet = _make_sheet(db_session, sheet_status="obsolete", steps=[{"step_type": "checkbox"}])
        _make_routed_part_with_sheet(db_session, "PS3-IMP-BAD", bad_sheet)

        response = client.post(
            "/api/v1/work-orders/import",
            headers=auth_headers,
            files={
                "file": (
                    "import.csv",
                    BytesIO(b"wo_number,part_number,quantity\nWO-PS3-1,PS3-IMP-OK,5\nWO-PS3-2,PS3-IMP-BAD,5\n"),
                    "text/csv",
                )
            },
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["created_count"] == 1
        assert len(body["errors"]) == 1
        assert "no released revision" in body["errors"][0]["reason"]
        assert body["errors"][0]["part_number"] == "PS3-IMP-BAD"

        # The good row snapshotted; the bad row left nothing behind.
        good_wo = db_session.query(WorkOrder).filter_by(work_order_number="WO-PS3-1", company_id=1).one()
        good_op_ids = [op.id for op in good_wo.operations]
        steps = db_session.query(WOOperationStep).all()
        assert {s.work_order_operation_id for s in steps} == set(good_op_ids)
        assert db_session.query(WorkOrder).filter_by(work_order_number="WO-PS3-2").count() == 0

        # Import audit carries the snapshot summary too.
        audit_row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order",
                AuditLog.resource_id == good_wo.id,
                AuditLog.action == "CREATE",
            )
            .one()
        )
        extra = json.loads(audit_row.extra_data) if isinstance(audit_row.extra_data, str) else audit_row.extra_data
        assert extra["process_sheet_snapshot"][0]["sheet_number"] == good_sheet.sheet_number


# ---------------------------------------------------------------------------
# Capture — validation ladder per type
# ---------------------------------------------------------------------------


class TestStepRecordCapture:
    def test_measurement_happy_path_rounds_and_audits(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="measurement")

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.00449},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["value_numeric"] == 1.004  # rounded to config decimals=3
        assert body["is_conforming"] is True
        # PR 4 source trust model (mirrors clock-in): no client hint -> NULL, never
        # guessed; only a kiosk-scoped credential forces a channel.
        assert body["source"] is None
        assert body["recorded_by"] == user.id
        assert body["superseded_by_id"] is None

        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "operation_step_record", AuditLog.resource_id == body["id"])
            .one()
        )
        assert audit_row.action == "CREATE"

    def test_out_of_tolerance_is_409_with_no_row(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="measurement")

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.05},
            headers=headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        detail = response.json()["detail"]
        assert detail["code"] == "OUT_OF_TOLERANCE"
        assert detail["measured"] == 1.05
        assert detail["lsl"] == 0.98 and detail["usl"] == 1.02
        assert _record_rows(db_session, operation.id) == []  # refused: NO record row

    def test_measurement_requires_numeric_and_rejects_stray_fields(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="measurement")

        missing = client.post(RECORDS_URL.format(op=operation.id, step=step.id), json={}, headers=headers)
        assert missing.status_code == 400
        stray = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_numeric": 1.0, "value_bool": True},
            headers=headers,
        )
        assert stray.status_code == 400

    def test_checkbox_list_value_types(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        checkbox = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        list_step = _add_wo_step(db_session, operation, step_type="list", config={"options": ["Pass", "Rework"]})
        value_step = _add_wo_step(db_session, operation, step_type="value", config=None)

        ok = client.post(
            RECORDS_URL.format(op=operation.id, step=checkbox.id), json={"value_bool": True}, headers=headers
        )
        assert ok.status_code == 201
        assert ok.json()["is_conforming"] is True  # the checkbox IS the conformance assertion
        unchecked = client.post(
            RECORDS_URL.format(op=operation.id, step=checkbox.id), json={"value_bool": False}, headers=headers
        )
        assert unchecked.status_code == 201  # honest "not done" evidence is accepted...
        assert unchecked.json()["is_conforming"] is False  # ...but never satisfies the gate
        bad = client.post(
            RECORDS_URL.format(op=operation.id, step=checkbox.id), json={"value_text": "yes"}, headers=headers
        )
        assert bad.status_code == 400

        ok = client.post(
            RECORDS_URL.format(op=operation.id, step=list_step.id), json={"value_text": "Pass"}, headers=headers
        )
        assert ok.status_code == 201
        bad = client.post(
            RECORDS_URL.format(op=operation.id, step=list_step.id), json={"value_text": "Scrap"}, headers=headers
        )
        assert bad.status_code == 400

        ok = client.post(
            RECORDS_URL.format(op=operation.id, step=value_step.id), json={"value_text": "Lot 42A"}, headers=headers
        )
        assert ok.status_code == 201
        bad = client.post(
            RECORDS_URL.format(op=operation.id, step=value_step.id), json={"value_text": "  "}, headers=headers
        )
        assert bad.status_code == 400

    def test_instruction_steps_take_no_records(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="instruction", is_required=False, config=None)

        response = client.post(RECORDS_URL.format(op=operation.id, step=step.id), json={}, headers=headers)
        assert response.status_code == 400
        assert "display-only" in response.json()["detail"]

    def test_photo_requires_tenant_scoped_attachment_document(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="photo", config=None)

        missing = client.post(RECORDS_URL.format(op=operation.id, step=step.id), json={}, headers=headers)
        assert missing.status_code == 400

        unknown = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"attachment_document_id": 999999},
            headers=headers,
        )
        assert unknown.status_code == 404

        # The evidence document must be a QUALITY_RECORD linked to THIS work order —
        # exactly what the step-attachment upload produces. (The laundering negatives —
        # wrong type / another WO's record / unlinked — live in
        # tests/api/test_process_sheet_gate_parity.py::TestAttachmentEvidenceBinding.)
        document = Document(
            document_number=f"QUA-TEST-{_next():04d}",
            title="Evidence",
            document_type=DocumentType.QUALITY_RECORD,
            work_order_id=work_order.id,
            status="released",
            company_id=1,
        )
        db_session.add(document)
        db_session.commit()
        ok = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"attachment_document_id": document.id},
            headers=headers,
        )
        assert ok.status_code == 201
        assert ok.json()["attachment_document_id"] == document.id

    def test_operation_status_and_terminal_wo_gates(self, client: TestClient, db_session: Session):
        # READY operation: 400 — capture happens while running (same rule as /production).
        work_order, operation, user, headers = _capture_fixture(db_session, op_status=OperationStatus.READY)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert response.status_code == 400
        assert "in progress" in response.json()["detail"]

        # Terminal WO: 409 state conflict (same shape as complete_operation's guard).
        work_order.status = WorkOrderStatus.CANCELLED
        operation.status = OperationStatus.IN_PROGRESS
        db_session.commit()
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "cancelled" in response.json()["detail"]

    def test_serialized_wo_requires_valid_serial(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session, serials=["SN-1", "SN-2"])
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        url = RECORDS_URL.format(op=operation.id, step=step.id)

        assert client.post(url, json={"value_bool": True}, headers=headers).status_code == 400  # serial required
        wrong = client.post(url, json={"value_bool": True, "serial_number": "SN-9"}, headers=headers)
        assert wrong.status_code == 400
        ok = client.post(url, json={"value_bool": True, "serial_number": "SN-1"}, headers=headers)
        assert ok.status_code == 201
        assert ok.json()["serial_number"] == "SN-1"

    def test_non_serialized_wo_rejects_serial(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_bool": True, "serial_number": "SN-1"},
            headers=headers,
        )
        assert response.status_code == 400
        assert "not serialized" in response.json()["detail"]

    def test_equipment_passthrough_is_tenant_scoped(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="measurement")
        ensure_company(db_session, 2)
        own_gauge = Equipment(equipment_id=f"GA-{_next()}", name="Caliper", company_id=1)
        foreign_gauge = Equipment(equipment_id=f"GA-{_next()}", name="Foreign", company_id=2)
        db_session.add_all([own_gauge, foreign_gauge])
        db_session.commit()
        url = RECORDS_URL.format(op=operation.id, step=step.id)

        foreign = client.post(url, json={"value_numeric": 1.0, "equipment_id": foreign_gauge.id}, headers=headers)
        assert foreign.status_code == 404  # cross-tenant gauge never resolves

        ok = client.post(url, json={"value_numeric": 1.0, "equipment_id": own_gauge.id}, headers=headers)
        assert ok.status_code == 201
        assert ok.json()["equipment_id"] == own_gauge.id

    def test_step_must_belong_to_operation(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        other_wc = make_work_center(db_session)
        _, other_operation = make_wo_with_operation(
            db_session, work_center=other_wc, op_status=OperationStatus.IN_PROGRESS
        )
        foreign_step = _add_wo_step(db_session, other_operation, step_type="checkbox", config=None)

        response = client.post(
            RECORDS_URL.format(op=operation.id, step=foreign_step.id),
            json={"value_bool": True},
            headers=headers,
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Supersede — corrections are new records, stamped exactly once
# ---------------------------------------------------------------------------


class TestSupersede:
    def _recorded(self, client, db_session, value=1.0):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="measurement")
        created = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": value}, headers=headers
        )
        assert created.status_code == 201
        return work_order, operation, step, created.json(), headers

    def test_supersede_creates_replacement_and_stamps_old_once(self, client: TestClient, db_session: Session):
        work_order, operation, step, original, headers = self._recorded(client, db_session)

        response = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=original["id"]),
            json={"reason": "Mis-read the caliper", "value_numeric": 1.01},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        replacement = response.json()
        assert replacement["id"] != original["id"]
        assert replacement["value_numeric"] == 1.01

        old = db_session.query(OperationStepRecord).get(original["id"])
        db_session.refresh(old)
        assert old.superseded_by_id == replacement["id"]
        assert old.supersede_reason == "Mis-read the caliper"
        # Original value untouched — corrections never mutate evidence.
        assert old.value_numeric == 1.0

        again = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=original["id"]),
            json={"reason": "double correct", "value_numeric": 1.0},
            headers=headers,
        )
        assert again.status_code == status.HTTP_409_CONFLICT  # stamped exactly once

        # Audit: CREATE for the replacement + UPDATE stamping the old row.
        actions = {
            (row.action, row.resource_id)
            for row in db_session.query(AuditLog).filter(AuditLog.resource_type == "operation_step_record").all()
        }
        assert ("CREATE", replacement["id"]) in actions
        assert ("UPDATE", original["id"]) in actions

    def test_replacement_passes_full_ladder_including_oot(self, client: TestClient, db_session: Session):
        work_order, operation, step, original, headers = self._recorded(client, db_session)

        response = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=original["id"]),
            json={"reason": "fat finger", "value_numeric": 9.9},
            headers=headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"]["code"] == "OUT_OF_TOLERANCE"

        old = db_session.query(OperationStepRecord).get(original["id"])
        db_session.refresh(old)
        assert old.superseded_by_id is None  # refused correction never stamps the old row
        assert len(_record_rows(db_session, operation.id)) == 1

    def test_supersede_chain_gate_and_view_use_only_the_latest(self, client: TestClient, db_session: Session):
        # Chain r1 -> r2 -> r3: every non-tip record is stamped exactly once and
        # can never be superseded again; the gate and the view see ONLY the tip.
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None, label="Chained")
        record_url = RECORDS_URL.format(op=operation.id, step=step.id)

        r1 = client.post(record_url, json={"value_bool": True}, headers=headers).json()
        r2 = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=r1["id"]),
            json={"reason": "was not actually done", "value_bool": False},
            headers=headers,
        ).json()
        r3_response = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=r2["id"]),
            json={"reason": "done after re-work", "value_bool": True},
            headers=headers,
        )
        assert r3_response.status_code == 201, r3_response.text
        r3 = r3_response.json()

        rows = {r.id: r for r in _record_rows(db_session, operation.id)}
        for row in rows.values():
            db_session.refresh(row)
        assert rows[r1["id"]].superseded_by_id == r2["id"]
        assert rows[r2["id"]].superseded_by_id == r3["id"]
        assert rows[r3["id"]].superseded_by_id is None

        # r1 cannot be superseded twice; r2 cannot be superseded once r3 exists.
        for stale_id in (r1["id"], r2["id"]):
            again = client.post(
                SUPERSEDE_URL.format(op=operation.id, step=step.id, record=stale_id),
                json={"reason": "stale correction", "value_bool": True},
                headers=headers,
            )
            assert again.status_code == status.HTTP_409_CONFLICT, stale_id

        # View: exactly one live record — the tip.
        view = client.get(STEPS_URL.format(op=operation.id), headers=headers).json()
        assert [r["id"] for r in view["steps"][0]["records"]] == [r3["id"]]

        # Gate: satisfied by r3 alone (r2 was False; superseded, so it no longer blocks).
        done = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert done.status_code == 200, done.text

    def test_supersede_inherits_serial_slot(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session, serials=["SN-1", "SN-2"])
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        created = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"value_bool": True, "serial_number": "SN-2"},
            headers=headers,
        )
        assert created.status_code == 201

        response = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=created.json()["id"]),
            json={"reason": "wrong box ticked", "value_bool": False},
            headers=headers,
        )
        assert response.status_code == 201
        assert response.json()["serial_number"] == "SN-2"


# ---------------------------------------------------------------------------
# Completion gating
# ---------------------------------------------------------------------------


class TestCompletionGating:
    def test_missing_required_step_blocks_completion(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None, label="Torque verified")

        response = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "STEPS_INCOMPLETE"
        assert detail["missing"] == [{"step_id": step.id, "label": "Torque verified", "serials": []}]

        db_session.expire_all()
        assert operation.status == OperationStatus.IN_PROGRESS  # nothing mutated

        # Record it and the same completion succeeds.
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert recorded.status_code == 201
        response = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["is_fully_complete"] is True

    def test_per_serial_gating(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session, serials=["SN-1", "SN-2"])
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None, label="Serial check")
        record_url = RECORDS_URL.format(op=operation.id, step=step.id)

        assert (
            client.post(record_url, json={"value_bool": True, "serial_number": "SN-1"}, headers=headers).status_code
            == 201
        )

        blocked = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert blocked.status_code == status.HTTP_409_CONFLICT
        assert blocked.json()["detail"]["missing"] == [
            {"step_id": step.id, "label": "Serial check", "serials": ["SN-2"]}
        ]

        assert (
            client.post(record_url, json={"value_bool": True, "serial_number": "SN-2"}, headers=headers).status_code
            == 201
        )
        done = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert done.status_code == 200

    def test_zero_step_operation_completes_exactly_as_today(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        response = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["is_fully_complete"] is True

    def test_partial_progress_update_is_not_gated(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        _add_wo_step(db_session, operation, step_type="checkbox", config=None)

        response = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 4}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["is_fully_complete"] is False

    def test_optional_and_instruction_steps_do_not_gate(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        _add_wo_step(db_session, operation, step_type="checkbox", config=None, is_required=False)
        _add_wo_step(db_session, operation, step_type="instruction", config=None, is_required=False)

        response = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert response.status_code == 200

    def test_superseded_and_nonconforming_records_do_not_satisfy_gate(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="measurement")
        # Simulate a live NON-conforming record (the PR 4 NCR path will create these):
        # it must not satisfy the gate.
        db_session.add(
            OperationStepRecord(
                company_id=1,
                wo_operation_step_id=step.id,
                work_order_operation_id=operation.id,
                value_numeric=9.9,
                is_conforming=False,
                recorded_by=user.id,
            )
        )
        db_session.commit()
        blocked = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert blocked.status_code == status.HTTP_409_CONFLICT
        assert blocked.json()["detail"]["code"] == "STEPS_INCOMPLETE"

    def test_unchecked_checkbox_blocks_until_superseded_true(self, client: TestClient, db_session: Session):
        # A required CHECKBOX recorded False is honest evidence but NON-conforming:
        # it must not satisfy the gate. Supersede-to-true is the correction route.
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None, label="Guard installed")
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": False}, headers=headers
        )
        assert recorded.status_code == 201
        assert recorded.json()["is_conforming"] is False

        blocked = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert blocked.status_code == status.HTTP_409_CONFLICT
        detail = blocked.json()["detail"]
        assert detail["code"] == "STEPS_INCOMPLETE"
        assert detail["missing"] == [{"step_id": step.id, "label": "Guard installed", "serials": []}]

        corrected = client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=recorded.json()["id"]),
            json={"reason": "guard installed after re-check", "value_bool": True},
            headers=headers,
        )
        assert corrected.status_code == 201
        assert corrected.json()["is_conforming"] is True

        done = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert done.status_code == 200, done.text

    def test_second_completer_still_rejected_after_completion(self, client: TestClient, db_session: Session):
        # The gate runs on the locked re-fetch; once a first completer wins, the stale
        # second call is rejected by the pre-existing re-checks under the same lock
        # (here the single-op WO went terminal, so the G6-A 409 fires) — the steps
        # gate never lets a stale completer through ahead of them.
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        client.post(RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers)

        first = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert first.status_code == 200
        second = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert second.status_code == status.HTTP_409_CONFLICT
        assert "work order is" in second.json()["detail"]


class TestOfficeCompletionGating:
    """Gate parity: the office/admin twin POST /work-orders/operations/{id}/complete
    (query-param API) runs the SAME required-steps gate as the shop-floor endpoint —
    an ungated second completion path would bypass the evidence requirement."""

    OFFICE_COMPLETE_URL = "/api/v1/work-orders/operations/{op}/complete"

    def test_office_completion_blocked_with_missing_steps(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None, label="Torque verified")

        response = client.post(
            self.OFFICE_COMPLETE_URL.format(op=operation.id),
            params={"quantity_complete": 10},
            headers=headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "STEPS_INCOMPLETE"
        assert detail["missing"] == [{"step_id": step.id, "label": "Torque verified", "serials": []}]

        db_session.expire_all()
        assert operation.status == OperationStatus.IN_PROGRESS  # nothing mutated

    def test_office_completion_succeeds_when_steps_satisfied(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert recorded.status_code == 201

        response = client.post(
            self.OFFICE_COMPLETE_URL.format(op=operation.id),
            params={"quantity_complete": 10},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        db_session.expire_all()
        assert operation.status == OperationStatus.COMPLETE

    def test_office_partial_progress_update_is_not_gated(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        _add_wo_step(db_session, operation, step_type="checkbox", config=None)

        response = client.post(
            self.OFFICE_COMPLETE_URL.format(op=operation.id),
            params={"quantity_complete": 4},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        db_session.expire_all()
        assert operation.status == OperationStatus.IN_PROGRESS  # partial: not gated, not complete

    def test_office_per_serial_gating(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session, serials=["SN-1", "SN-2"])
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None, label="Serial check")
        record_url = RECORDS_URL.format(op=operation.id, step=step.id)
        assert (
            client.post(record_url, json={"value_bool": True, "serial_number": "SN-1"}, headers=headers).status_code
            == 201
        )

        blocked = client.post(
            self.OFFICE_COMPLETE_URL.format(op=operation.id),
            params={"quantity_complete": 10},
            headers=headers,
        )
        assert blocked.status_code == status.HTTP_409_CONFLICT
        assert blocked.json()["detail"]["missing"] == [
            {"step_id": step.id, "label": "Serial check", "serials": ["SN-2"]}
        ]

    def test_gate_consistency_blocked_on_one_endpoint_completes_on_the_other(
        self, client: TestClient, db_session: Session
    ):
        # Same WO, same gate: both endpoints refuse with the IDENTICAL structured
        # payload; once the step is recorded, EITHER endpoint completes the op.
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None, label="Cross gate")
        record_url = RECORDS_URL.format(op=operation.id, step=step.id)

        shop_blocked = client.post(
            COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers
        )
        office_blocked = client.post(
            self.OFFICE_COMPLETE_URL.format(op=operation.id), params={"quantity_complete": 10}, headers=headers
        )
        assert shop_blocked.status_code == office_blocked.status_code == status.HTTP_409_CONFLICT
        assert shop_blocked.json()["detail"] == office_blocked.json()["detail"]  # byte-for-byte parity

        assert client.post(record_url, json={"value_bool": True}, headers=headers).status_code == 201
        done = client.post(
            self.OFFICE_COMPLETE_URL.format(op=operation.id), params={"quantity_complete": 10}, headers=headers
        )
        assert done.status_code == 200, done.text
        db_session.expire_all()
        assert operation.status == OperationStatus.COMPLETE

        # Mirror direction: blocked via office, satisfied, completed via shop-floor.
        work_order2, operation2, user2, headers2 = _capture_fixture(db_session)
        step2 = _add_wo_step(db_session, operation2, step_type="checkbox", config=None, label="Cross gate 2")
        blocked2 = client.post(
            self.OFFICE_COMPLETE_URL.format(op=operation2.id), params={"quantity_complete": 10}, headers=headers2
        )
        assert blocked2.status_code == status.HTTP_409_CONFLICT
        assert blocked2.json()["detail"]["code"] == "STEPS_INCOMPLETE"
        assert (
            client.post(
                RECORDS_URL.format(op=operation2.id, step=step2.id), json={"value_bool": True}, headers=headers2
            ).status_code
            == 201
        )
        done2 = client.post(COMPLETE_URL.format(op=operation2.id), json={"quantity_complete": 10}, headers=headers2)
        assert done2.status_code == 200, done2.text
        assert done2.json()["is_fully_complete"] is True


# ---------------------------------------------------------------------------
# Steps view + queue chip
# ---------------------------------------------------------------------------


class TestStepsView:
    def test_view_orders_steps_and_maps_completeness(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session, serials=["SN-1", "SN-2"])
        second = _add_wo_step(db_session, operation, step_type="checkbox", config=None, sequence=20, label="Later")
        first = _add_wo_step(db_session, operation, step_type="measurement", sequence=10, label="Earlier")
        client.post(
            RECORDS_URL.format(op=operation.id, step=first.id),
            json={"value_numeric": 1.0, "serial_number": "SN-1"},
            headers=headers,
        )

        response = client.get(STEPS_URL.format(op=operation.id), headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["work_order_number"] == work_order.work_order_number
        assert body["is_serialized"] is True and body["serial_numbers"] == ["SN-1", "SN-2"]
        assert [s["sequence"] for s in body["steps"]] == [10, 20]  # ordered by sequence
        assert body["steps_total"] == 2 and body["steps_recorded"] == 0  # SN-2 still missing
        assert body["completeness"][str(first.id)] == {"SN-1": True, "SN-2": False}
        assert body["completeness"][str(second.id)] == {"SN-1": False, "SN-2": False}
        step_one = body["steps"][0]
        assert step_one["complete"] is False and step_one["missing_serials"] == ["SN-2"]
        assert len(step_one["records"]) == 1
        assert step_one["records"][0]["recorded_by_name"]
        assert step_one["records"][0]["recorded_at"].endswith("Z")  # UTCModel serialization

    def test_superseded_records_are_excluded_from_view(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="measurement")
        created = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_numeric": 1.0}, headers=headers
        )
        client.post(
            SUPERSEDE_URL.format(op=operation.id, step=step.id, record=created.json()["id"]),
            json={"reason": "re-measured", "value_numeric": 1.001},
            headers=headers,
        )
        response = client.get(STEPS_URL.format(op=operation.id), headers=headers)
        records = response.json()["steps"][0]["records"]
        assert len(records) == 1
        assert records[0]["value_numeric"] == 1.001

    def test_queue_payload_carries_step_counts(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        _add_wo_step(db_session, operation, step_type="instruction", config=None, is_required=False)
        # An OPTIONAL non-instruction step is excluded from the chip too — it never gates.
        _add_wo_step(db_session, operation, step_type="checkbox", config=None, is_required=False)
        client.post(RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers)

        response = client.get(f"/api/v1/shop-floor/work-center-queue/{operation.work_center_id}", headers=headers)
        assert response.status_code == 200
        item = next(i for i in response.json()["queue"] if i["operation_id"] == operation.id)
        assert item["steps_total"] == 1  # instruction/optional steps don't count
        assert item["steps_recorded"] == 1

    def test_steps_view_readable_on_a_complete_operation(self, client: TestClient, db_session: Session):
        # Evidence review after the fact: the record trail stays readable once the
        # operation (and the single-op WO) have gone COMPLETE.
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None, label="Reviewed later")
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert recorded.status_code == 201
        done = client.post(COMPLETE_URL.format(op=operation.id), json={"quantity_complete": 10}, headers=headers)
        assert done.status_code == 200, done.text

        response = client.get(STEPS_URL.format(op=operation.id), headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["operation_status"] == "complete"
        assert body["steps"][0]["complete"] is True
        assert [r["id"] for r in body["steps"][0]["records"]] == [recorded.json()["id"]]

        # Recording anything further against the completed operation is refused
        # (the WO went terminal), so the trail is effectively frozen.
        late = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert late.status_code in (400, status.HTTP_409_CONFLICT)


# ---------------------------------------------------------------------------
# Kiosk-token fence
# ---------------------------------------------------------------------------


class TestKioskTokenFence:
    def _kiosk_scoped_headers(self, db_session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        token = create_access_token(subject=operator.id, company_id=1, scope="kiosk")
        return operator, bearer(token)

    def test_kiosk_scoped_operator_token_reads_and_records(self, client: TestClient, db_session: Session):
        work_order, operation, user, _ = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        operator, headers = self._kiosk_scoped_headers(db_session)

        view = client.get(STEPS_URL.format(op=operation.id), headers=headers)
        assert view.status_code == 200  # positive: steps view is inside the fence

        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=headers
        )
        assert recorded.status_code == 201
        assert recorded.json()["source"] == "kiosk"  # channel derived from the credential
        assert recorded.json()["recorded_by"] == operator.id

    def test_kiosk_scoped_token_cannot_reach_documents_upload(self, client: TestClient, db_session: Session):
        _, headers = self._kiosk_scoped_headers(db_session)
        response = client.post(
            "/api/v1/documents/upload",
            headers=headers,
            data={"title": "sneak", "document_type": "quality_record"},
            files={"file": ("x.png", BytesIO(b"\x89PNG fake"), "image/png")},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN  # fence holds: use /steps/{id}/attachment

    def test_kiosk_station_token_is_rejected_on_step_endpoints(self, client: TestClient, db_session: Session):
        work_order, operation, user, _ = _capture_fixture(db_session)
        station = make_kiosk_station(db_session)
        headers = bearer(kiosk_token_for(station))
        response = client.get(STEPS_URL.format(op=operation.id), headers=headers)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED  # station tokens only read the queue


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def _company2_headers(self, db_session):
        user = make_user(db_session, company_id=2, role=UserRole.MANAGER)
        return bearer(create_access_token(subject=user.id, company_id=2))

    def test_foreign_company_cannot_see_or_record_steps(self, client: TestClient, db_session: Session):
        work_order, operation, user, _ = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)
        foreign_headers = self._company2_headers(db_session)

        assert client.get(STEPS_URL.format(op=operation.id), headers=foreign_headers).status_code == 404
        response = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id), json={"value_bool": True}, headers=foreign_headers
        )
        assert response.status_code == 404
        assert _record_rows(db_session, operation.id) == []

    def test_snapshot_never_resolves_another_companys_released_revision(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        ensure_company(db_session, 2)
        number = f"PS-{_next():06d}"
        obsolete_own = _make_sheet(
            db_session, company_id=1, sheet_number=number, sheet_status="obsolete", steps=[{"step_type": "checkbox"}]
        )
        # Same family number RELEASED in company 2 must never satisfy company 1.
        _make_sheet(
            db_session, company_id=2, sheet_number=number, sheet_status="released", steps=[{"step_type": "checkbox"}]
        )
        part = _make_routed_part_with_sheet(db_session, "PS3-TEN-1", obsolete_own)

        response = client.post(
            "/api/v1/work-orders/",
            json={"part_id": part.id, "quantity_ordered": 1},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"]["code"] == "PROCESS_SHEET_UNAVAILABLE"


# ---------------------------------------------------------------------------
# PHOTO/FILE evidence upload
# ---------------------------------------------------------------------------


class TestStepAttachmentUpload:
    def test_photo_upload_creates_quality_record_document(self, client: TestClient, db_session: Session):
        work_order, operation, user, headers = _capture_fixture(db_session)
        step = _add_wo_step(db_session, operation, step_type="photo", config=None)

        response = client.post(
            ATTACHMENT_URL.format(op=operation.id, step=step.id),
            headers=headers,
            files={"file": ("weld.png", BytesIO(b"\x89PNG\r\n\x1a\nfakepixels"), "image/png")},
        )
        assert response.status_code == 201, response.text
        body = response.json()

        document = db_session.query(Document).filter(Document.id == body["document_id"]).one()
        assert document.document_type == DocumentType.QUALITY_RECORD
        assert document.work_order_id == work_order.id
        assert document.company_id == 1
        assert document.mime_type == "image/png"

        # The returned id closes the loop as the record's evidence.
        recorded = client.post(
            RECORDS_URL.format(op=operation.id, step=step.id),
            json={"attachment_document_id": body["document_id"]},
            headers=headers,
        )
        assert recorded.status_code == 201

        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "document", AuditLog.resource_id == document.id)
            .one()
        )
        assert audit_row.action == "CREATE"

    def test_upload_validates_mime_size_and_step_type(self, client: TestClient, db_session: Session, monkeypatch):
        from app.services import process_sheet_service

        work_order, operation, user, headers = _capture_fixture(db_session)
        photo_step = _add_wo_step(db_session, operation, step_type="photo", config=None)
        checkbox_step = _add_wo_step(db_session, operation, step_type="checkbox", config=None)

        bad_mime = client.post(
            ATTACHMENT_URL.format(op=operation.id, step=photo_step.id),
            headers=headers,
            files={"file": ("evil.exe", BytesIO(b"MZ"), "application/x-msdownload")},
        )
        assert bad_mime.status_code == 400

        # PDFs are FILE-step evidence, not PHOTO-step evidence.
        pdf_on_photo = client.post(
            ATTACHMENT_URL.format(op=operation.id, step=photo_step.id),
            headers=headers,
            files={"file": ("doc.pdf", BytesIO(b"%PDF-1.4"), "application/pdf")},
        )
        assert pdf_on_photo.status_code == 400

        wrong_step = client.post(
            ATTACHMENT_URL.format(op=operation.id, step=checkbox_step.id),
            headers=headers,
            files={"file": ("weld.png", BytesIO(b"\x89PNG"), "image/png")},
        )
        assert wrong_step.status_code == 400

        monkeypatch.setattr(process_sheet_service, "MAX_STEP_ATTACHMENT_BYTES", 8)
        too_big = client.post(
            ATTACHMENT_URL.format(op=operation.id, step=photo_step.id),
            headers=headers,
            files={"file": ("weld.png", BytesIO(b"\x89PNG123456789"), "image/png")},
        )
        assert too_big.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE

    def test_upload_rejected_on_instruction_and_measurement_steps(self, client: TestClient, db_session: Session):
        # Attachments are PHOTO/FILE evidence only — every other step type refuses
        # the upload outright (not just checkbox, which the mixed test above covers).
        work_order, operation, user, headers = _capture_fixture(db_session)
        instruction_step = _add_wo_step(db_session, operation, step_type="instruction", config=None, is_required=False)
        measurement_step = _add_wo_step(db_session, operation, step_type="measurement")

        for step in (instruction_step, measurement_step):
            response = client.post(
                ATTACHMENT_URL.format(op=operation.id, step=step.id),
                headers=headers,
                files={"file": ("weld.png", BytesIO(b"\x89PNG"), "image/png")},
            )
            assert response.status_code == 400, step.step_type
            assert "Only PHOTO and FILE steps take attachments" in response.json()["detail"]

    def test_file_step_accepts_pdf_and_kiosk_scoped_token_can_upload(self, client: TestClient, db_session: Session):
        work_order, operation, user, _ = _capture_fixture(db_session)
        file_step = _add_wo_step(db_session, operation, step_type="file", config=None)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        kiosk_headers = bearer(create_access_token(subject=operator.id, company_id=1, scope="kiosk"))

        response = client.post(
            ATTACHMENT_URL.format(op=operation.id, step=file_step.id),
            headers=kiosk_headers,
            files={"file": ("cert.pdf", BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        )
        assert response.status_code == 201, response.text  # in-fence evidence path for kiosks
