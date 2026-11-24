"""wo_change/2 -- New Work Order keeps process sheets when the planner edits a step.

``POST /work-orders`` with a SUPPLIED ``operations`` list (what New Work Order sends the
moment a routed step is edited, and always for a restored draft) used to build its rows
with no process-sheet snapshot. Reproduced against the pre-change code: the same part and
the same single routed step gave 1 snapshotted step via the routed path and 0 via the
supplied path, with ``process_sheet_snapshot: []`` on the creation audit row and nothing
saying a sheet had been left behind.

These tests pin the fix: a supplied row linked by ``routing_operation_id`` keeps its sheet
through the same snapshot the routed path runs; an unlinked or removed sheet-bearing step
is allowed and named on the creation audit row; a stale / foreign / mismatched link is a
409 that writes nothing; the preview carries the link; and the Quality gap list is a pure,
tenant-scoped read.
"""

import json

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.part import Part
from app.models.process_sheet import WOOperationStep
from app.models.routing import Routing, RoutingOperation
from app.models.user import UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from tests.api.kiosk_test_helpers import ensure_company, make_user, user_headers
from tests.api.test_process_sheet_capture import MEASUREMENT_CONFIG, _make_routed_part_with_sheet, _make_sheet

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

CREATE_URL = "/api/v1/work-orders/"
PREVIEW_URL = "/api/v1/work-orders/preview-operations/{part_id}"
GAPS_URL = "/api/v1/work-orders/process-sheet-gaps"

_seq = {"n": 0}


def _n() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _sheet(db: Session, **kwargs):
    return _make_sheet(
        db,
        steps=[{"step_type": "measurement", "config": dict(MEASUREMENT_CONFIG), "label": "Bore dia"}],
        **kwargs,
    )


def _routing_op(db: Session, part: Part) -> RoutingOperation:
    return (
        db.query(RoutingOperation)
        .join(Routing, Routing.id == RoutingOperation.routing_id)
        .filter(Routing.part_id == part.id)
        .order_by(RoutingOperation.sequence)
        .first()
    )


def _two_step_part(db: Session, *, company_id: int = 1):
    """A part whose released routing has a sheet-bearing Op 10 and a sheet-free Op 20."""
    sheet = _sheet(db, company_id=company_id)
    part = _make_routed_part_with_sheet(db, f"SUP-{_n():04d}", sheet, company_id=company_id)
    op10 = _routing_op(db, part)
    db.add(
        RoutingOperation(
            routing_id=op10.routing_id,
            sequence=20,
            operation_number="20",
            name="Deburr",
            work_center_id=op10.work_center_id,
            run_hours_per_unit=0.05,
            is_active=True,
            company_id=company_id,
        )
    )
    db.commit()
    op20 = (
        db.query(RoutingOperation)
        .filter(RoutingOperation.routing_id == op10.routing_id, RoutingOperation.sequence == 20)
        .one()
    )
    return part, sheet, op10, op20


def _row(rop: RoutingOperation, *, link: bool = True, name: str = None, **extra) -> dict:
    row = {
        "sequence": rop.sequence,
        "operation_number": str(rop.sequence),
        "name": name or rop.name,
        "work_center_id": rop.work_center_id,
        "setup_time_hours": 0.5,
        "run_time_hours": 1.0,
    }
    if link:
        row["routing_operation_id"] = rop.id
    row.update(extra)
    return row


def _steps_for(db: Session, wo_id: int) -> dict:
    ops = db.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == wo_id).all()
    return {
        op.sequence: db.query(WOOperationStep).filter(WOOperationStep.work_order_operation_id == op.id).count()
        for op in ops
    }


def _creation_extra(db: Session, wo_id: int) -> dict:
    row = db.query(AuditLog).filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo_id).one()
    return json.loads(row.extra_data) if isinstance(row.extra_data, str) else row.extra_data


def _wo_count(db: Session) -> int:
    # The conftest get_db override shares this session with the app and never closes it, so a
    # refused request's flushed-but-uncommitted rows linger; roll back first, as the real
    # request teardown (session close) would -- what remains is what was COMMITTED.
    db.rollback()
    return db.query(WorkOrder).count()


class TestSuppliedOperationsKeepSheets:
    def test_unlinked_supplied_rows_snapshot_nothing_and_the_audit_row_names_the_dropped_sheet(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """The reproduced defect, now RECORDED: a legacy client that sends no link still gets
        no snapshot (nothing to match by id), but the creation row names what was left out."""
        part, sheet, op10, op20 = _two_step_part(db_session)
        response = client.post(
            CREATE_URL,
            json={
                "part_id": part.id,
                "quantity_ordered": 5,
                "operations": [_row(op10, link=False, name="Machine (edited)"), _row(op20, link=False)],
            },
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        wo_id = response.json()["id"]
        assert _steps_for(db_session, wo_id) == {10: 0, 20: 0}
        extra = _creation_extra(db_session, wo_id)
        assert extra["process_sheet_snapshot"] == []
        assert extra["operations_supplied"] is True
        assert extra["linked_routing_operation_ids"] == []
        assert [r["routing_operation_id"] for r in extra["removed_process_sheet_steps"]] == [op10.id]
        assert extra["removed_process_sheet_steps"][0]["sheet_number"] == sheet.sheet_number

    def test_an_edited_linked_row_keeps_its_process_sheet(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part, sheet, op10, op20 = _two_step_part(db_session)
        response = client.post(
            CREATE_URL,
            json={
                "part_id": part.id,
                "quantity_ordered": 5,
                "operations": [_row(op10, name="Machine (edited)"), _row(op20)],
            },
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        wo_id = response.json()["id"]
        assert _steps_for(db_session, wo_id) == {10: 1, 20: 0}
        edited = (
            db_session.query(WorkOrderOperation)
            .filter(WorkOrderOperation.work_order_id == wo_id, WorkOrderOperation.sequence == 10)
            .one()
        )
        assert edited.name == "Machine (edited)", "the planner's edit stands"
        step = db_session.query(WOOperationStep).filter(WOOperationStep.work_order_operation_id == edited.id).one()
        assert step.source_sheet_id == sheet.id and step.source_sheet_revision == "A" and step.company_id == 1
        extra = _creation_extra(db_session, wo_id)
        assert extra["linked_routing_operation_ids"] == [op10.id, op20.id]
        assert extra["removed_process_sheet_steps"] == []
        assert [s["sheet_number"] for s in extra["process_sheet_snapshot"]] == [sheet.sheet_number]

    def test_a_linked_row_is_matched_by_id_not_by_sequence(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """Re-sequenced: the sheet follows the routing operation id onto the row at 30."""
        part, _sheet_row, op10, op20 = _two_step_part(db_session)
        response = client.post(
            CREATE_URL,
            json={
                "part_id": part.id,
                "quantity_ordered": 2,
                "operations": [
                    _row(op20, sequence=10, operation_number="10"),
                    _row(op10, sequence=30, operation_number="30"),
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert _steps_for(db_session, response.json()["id"]) == {10: 0, 30: 1}

    def test_removing_a_sheet_bearing_step_is_allowed_and_recorded(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part, sheet, op10, op20 = _two_step_part(db_session)
        response = client.post(
            CREATE_URL,
            json={"part_id": part.id, "quantity_ordered": 1, "operations": [_row(op20)]},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        removed = _creation_extra(db_session, response.json()["id"])["removed_process_sheet_steps"]
        assert removed == [
            {
                "routing_operation_id": op10.id,
                "operation_number": "Op 10",
                "operation_sequence": 10,
                "name": "Machine",
                "component_part_id": None,
                "component_part_number": None,
                "process_sheet_id": sheet.id,
                "sheet_number": sheet.sheet_number,
            }
        ]

    def test_a_routed_create_audit_row_is_unchanged(self, client: TestClient, auth_headers: dict, db_session: Session):
        part, _sheet_row, _op10, _op20 = _two_step_part(db_session)
        response = client.post(CREATE_URL, json={"part_id": part.id, "quantity_ordered": 1}, headers=auth_headers)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        extra = _creation_extra(db_session, response.json()["id"])
        assert "removed_process_sheet_steps" not in extra and "operations_supplied" not in extra


class TestStaleOrForeignLinksAreRefused:
    def _assert_refused(self, response, reason: str, routing_operation_id: int):
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "ROUTING_OPERATION_UNMATCHED"
        assert detail["operations"] == [
            {"sequence": 10, "operation_number": "10", "routing_operation_id": routing_operation_id, "reason": reason}
        ]

    def test_a_routing_operation_of_another_part_is_refused_and_nothing_is_written(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part, _s, _op10, _op20 = _two_step_part(db_session)
        _other_part, _s2, other_op10, _o20 = _two_step_part(db_session)
        before = _wo_count(db_session)
        response = client.post(
            CREATE_URL,
            json={"part_id": part.id, "quantity_ordered": 1, "operations": [_row(other_op10)]},
            headers=auth_headers,
        )
        self._assert_refused(response, "not_on_released_routing", other_op10.id)
        assert _wo_count(db_session) == before

    def test_a_stale_id_from_a_superseded_routing_is_refused(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """A restored draft from before a re-release names an operation that is no longer copied."""
        part, _s, op10, _op20 = _two_step_part(db_session)
        op10.is_active = False
        db_session.commit()
        response = client.post(
            CREATE_URL,
            json={"part_id": part.id, "quantity_ordered": 1, "operations": [_row(op10)]},
            headers=auth_headers,
        )
        self._assert_refused(response, "not_on_released_routing", op10.id)

    def test_another_companys_routing_operation_reads_as_not_found(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        ensure_company(db_session, 2)
        part, _s, op10, _op20 = _two_step_part(db_session)
        _foreign_part, _fs, foreign_op10, _f20 = _two_step_part(db_session, company_id=2)
        response = client.post(
            CREATE_URL,
            json={
                "part_id": part.id,
                "quantity_ordered": 1,
                "operations": [_row(foreign_op10, work_center_id=op10.work_center_id)],
            },
            headers=auth_headers,
        )
        self._assert_refused(response, "not_found", foreign_op10.id)

    def test_a_missing_id_reads_as_not_found(self, client: TestClient, auth_headers: dict, db_session: Session):
        part, _s, op10, _op20 = _two_step_part(db_session)
        row = _row(op10)
        row["routing_operation_id"] = 987654
        response = client.post(
            CREATE_URL, json={"part_id": part.id, "quantity_ordered": 1, "operations": [row]}, headers=auth_headers
        )
        self._assert_refused(response, "not_found", 987654)

    def test_a_linked_row_whose_family_has_no_released_revision_is_the_routed_409(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _sheet(db_session, sheet_status="draft")
        part = _make_routed_part_with_sheet(db_session, f"SUP-{_n():04d}", sheet)
        op10 = _routing_op(db_session, part)
        before = _wo_count(db_session)
        response = client.post(
            CREATE_URL,
            json={"part_id": part.id, "quantity_ordered": 1, "operations": [_row(op10)]},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"]["code"] == "PROCESS_SHEET_UNAVAILABLE"
        assert _wo_count(db_session) == before


def _assembly_with_sheeted_component(db: Session):
    """Assembly with an active BOM whose one component has a released routing carrying a sheet."""
    sheet = _sheet(db)
    component = _make_routed_part_with_sheet(db, f"CMP-{_n():04d}", sheet)
    assembly = Part(
        part_number=f"ASM-{_n():04d}",
        name="Weldment",
        part_type="assembly",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(assembly)
    db.flush()
    bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
    db.add(bom)
    db.flush()
    db.add(
        BOMItem(
            bom_id=bom.id, component_part_id=component.id, item_number=10, quantity=2, item_type="make", company_id=1
        )
    )
    db.commit()
    return assembly, component, sheet, _routing_op(db, component)


class TestBomComponentLinks:
    def test_a_component_step_keeps_its_sheet_and_must_name_its_component(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        assembly, component, _sheet_row, comp_op = _assembly_with_sheeted_component(db_session)
        good = client.post(
            CREATE_URL,
            json={
                "part_id": assembly.id,
                "quantity_ordered": 3,
                "operations": [_row(comp_op, component_part_id=component.id, component_quantity=6)],
            },
            headers=auth_headers,
        )
        assert good.status_code == status.HTTP_201_CREATED, good.text
        assert _steps_for(db_session, good.json()["id"]) == {10: 1}

        bad = client.post(
            CREATE_URL,
            json={"part_id": assembly.id, "quantity_ordered": 3, "operations": [_row(comp_op)]},
            headers=auth_headers,
        )
        assert bad.status_code == status.HTTP_409_CONFLICT, bad.text
        assert bad.json()["detail"]["operations"][0]["reason"] == "component_mismatch"


class TestPreviewCarriesTheLink:
    def test_simple_part_preview_rows_carry_routing_operation_and_sheet(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part, sheet, op10, op20 = _two_step_part(db_session)
        response = client.get(PREVIEW_URL.format(part_id=part.id), headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        rows = response.json()["operations_preview"]
        assert [(r["routing_operation_id"], r["sequence"], r["operation_number"]) for r in rows] == [
            (op10.id, 10, "Op 10"),
            (op20.id, 20, "20"),
        ]
        assert rows[0]["process_sheet_id"] == sheet.id
        assert rows[0]["process_sheet_number"] == sheet.sheet_number
        assert rows[0]["process_sheet_released"] is True
        assert rows[1]["process_sheet_id"] is None and rows[1]["process_sheet_released"] is None

    def test_assembly_preview_rows_carry_the_component_link(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        assembly, component, sheet, comp_op = _assembly_with_sheeted_component(db_session)
        rows = client.get(PREVIEW_URL.format(part_id=assembly.id), headers=auth_headers).json()["operations_preview"]
        assert len(rows) == 1
        assert rows[0]["routing_operation_id"] == comp_op.id
        assert rows[0]["component_part_id"] == component.id
        assert rows[0]["sequence"] == 10 and rows[0]["operation_number"] == "10"
        assert rows[0]["process_sheet_number"] == sheet.sheet_number

    def test_draft_sheet_is_flagged_unreleased(self, client: TestClient, auth_headers: dict, db_session: Session):
        sheet = _sheet(db_session, sheet_status="draft")
        part = _make_routed_part_with_sheet(db_session, f"SUP-{_n():04d}", sheet)
        rows = client.get(PREVIEW_URL.format(part_id=part.id), headers=auth_headers).json()["operations_preview"]
        assert rows[0]["process_sheet_released"] is False


class TestProcessSheetGapList:
    def _gap_ids(self, client, headers) -> list:
        response = client.get(GAPS_URL, headers=headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["total"] == len(body["items"])
        return [item["work_order_id"] for item in body["items"]]

    def test_lists_live_jobs_without_their_sheets_and_writes_nothing(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part, sheet, op10, op20 = _two_step_part(db_session)
        gap = client.post(
            CREATE_URL,
            json={"part_id": part.id, "quantity_ordered": 1, "operations": [_row(op10, link=False)]},
            headers=auth_headers,
        ).json()["id"]
        routed = client.post(CREATE_URL, json={"part_id": part.id, "quantity_ordered": 1}, headers=auth_headers).json()[
            "id"
        ]
        cancelled = client.post(
            CREATE_URL,
            json={"part_id": part.id, "quantity_ordered": 1, "operations": [_row(op20, link=False)]},
            headers=auth_headers,
        ).json()["id"]
        db_session.query(WorkOrder).filter(WorkOrder.id == cancelled).update({"status": WorkOrderStatus.CANCELLED})
        db_session.commit()
        audit_before = db_session.query(AuditLog).count()

        quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
        response = client.get(GAPS_URL, headers=user_headers(quality))
        assert response.status_code == status.HTTP_200_OK, response.text
        items = {item["work_order_id"]: item for item in response.json()["items"]}
        assert gap in items
        assert routed not in items and cancelled not in items
        assert items[gap]["expected_process_sheet_steps"][0]["routing_operation_id"] == op10.id
        assert items[gap]["expected_process_sheet_steps"][0]["sheet_number"] == sheet.sheet_number
        assert items[gap]["created_at"].endswith("Z")
        db_session.expire_all()
        assert db_session.query(AuditLog).count() == audit_before, "a pure read writes no audit row"

    def test_another_company_never_sees_the_row(self, client: TestClient, auth_headers: dict, db_session: Session):
        part, _sheet_row, op10, _op20 = _two_step_part(db_session)
        gap = client.post(
            CREATE_URL,
            json={"part_id": part.id, "quantity_ordered": 1, "operations": [_row(op10, link=False)]},
            headers=auth_headers,
        ).json()["id"]
        outsider = make_user(db_session, role=UserRole.QUALITY, company_id=2)
        assert gap not in self._gap_ids(client, user_headers(outsider))

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.VIEWER])
    def test_other_roles_are_refused(self, client: TestClient, db_session: Session, role):
        user = make_user(db_session, role=role, company_id=1)
        assert client.get(GAPS_URL, headers=user_headers(user)).status_code == status.HTTP_403_FORBIDDEN
