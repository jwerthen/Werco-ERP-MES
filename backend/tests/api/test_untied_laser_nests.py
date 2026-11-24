"""Untied laser nests: the worklist, coverage, sheet report and per-job summary.

Pinned here:

1. A nest is UNTIED when no OPEN operation-scoped tie names its operation; a CANCELLED
   tie does not count, a soft-deleted nest and another tenant's nest never surface.
2. Open nests and nests completed inside the window are listed; a nest completed before
   the window is not. A nest on a finished job is listed but not tieable.
3. ``proposed_tie`` is the exact body the EXISTING tie endpoint accepts, and posting it
   takes the nest off the list.
4. Every read is PURE: no audit row, no ledger row, no operational event.
5. Only Admin / Manager / Supervisor read these (the trio that can tie a sheet).
"""

from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import CycleCount, CycleCountStatus, InventoryItem, InventoryTransaction
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/laser-nests"
LIST_URL = f"{BASE}/untied"
COVERAGE_URL = f"{BASE}/untied/coverage"
REPORT_URL = f"{BASE}/untied/sheet-report"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_nest(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    material: Optional[str] = "A36",
    thickness: Optional[str] = "0.250",
    sheet_size: Optional[str] = "60x120",
    planned_runs: int = 4,
    op_status: OperationStatus = OperationStatus.READY,
    wo_status: WorkOrderStatus = WorkOrderStatus.RELEASED,
    completed_at: Optional[datetime] = None,
    quantity_complete: float = 0.0,
    quantity_scrapped: float = 0.0,
    is_deleted: bool = False,
    parent: Optional[WorkOrder] = None,
) -> Tuple[WorkOrder, WorkOrderOperation, LaserNest]:
    n = _next()
    wc = make_work_center(db, company_id=company_id, name=f"Laser {n}")
    work_order, operation = make_wo_with_operation(
        db, company_id=company_id, work_center=wc, op_status=op_status, wo_status=wo_status
    )
    work_order.work_order_type = WorkOrderType.LASER_CUTTING.value
    work_order.parent_work_order_id = parent.id if parent is not None else None
    operation.operation_number = "10"
    operation.name = f"Laser Cut - PRG{n:04d}"
    operation.quantity_complete = quantity_complete
    operation.quantity_scrapped = quantity_scrapped
    if completed_at is not None:
        operation.actual_end = completed_at
    package = LaserNestPackage(
        company_id=company_id,
        parent_work_order_id=parent.id if parent is not None else None,
        child_work_order_id=work_order.id,
        package_name=f"Package {n}",
        import_status="imported",
    )
    db.add(package)
    db.flush()
    nest = LaserNest(
        company_id=company_id,
        package_id=package.id,
        work_order_operation_id=operation.id,
        nest_name=f"PRG{n:04d}",
        cnc_number=f"PRG{n:04d}",
        planned_runs=planned_runs,
        completed_runs=quantity_complete,
        material=material,
        thickness=thickness,
        sheet_size=sheet_size,
        is_deleted=is_deleted,
    )
    db.add(nest)
    db.commit()
    db.refresh(work_order)
    db.refresh(operation)
    db.refresh(nest)
    return work_order, operation, nest


def make_sheet(db: Session, *, company_id: int = COMPANY_A, part_number: str = "0.250-60X120-A36") -> Part:
    part = Part(
        part_number=part_number,
        name=f"Plate {part_number}",
        part_type="raw_material",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    db.add(
        InventoryItem(
            part_id=part.id,
            location="RAW-A",
            warehouse="MAIN",
            quantity_on_hand=12.0,
            quantity_allocated=0.0,
            quantity_available=12.0,
            lot_number=f"HEAT-{_next():05d}",
            unit_cost=80.0,
            status="available",
            is_active=True,
            company_id=company_id,
        )
    )
    db.commit()
    db.refresh(part)
    return part


def tie(db: Session, operation: WorkOrderOperation, part: Part, *, status_: AllocationStatus = AllocationStatus.OPEN):
    db.add(
        WorkOrderMaterialAllocation(
            company_id=operation.company_id,
            work_order_id=operation.work_order_id,
            work_order_operation_id=operation.id,
            part_id=part.id,
            source=AllocationSource.NEST,
            status=status_,
            qty_per_run=1.0,
            qty_planned=4.0,
            unit_of_measure="each",
            qty_consumed=0.0,
        )
    )
    db.commit()


def _ids(payload: dict) -> set:
    return {row["work_order_operation_id"] for row in payload["items"]}


def _writes(db: Session) -> Tuple[int, int, int]:
    return (
        db.query(AuditLog).count(),
        db.query(InventoryTransaction).count(),
        db.query(OperationalEvent).count(),
    )


class TestUntiedList:
    def test_untied_open_nest_is_listed_with_the_matched_sheet(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session)
        work_order, operation, nest = make_nest(db_session)

        resp = client.get(LIST_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        row = next(r for r in resp.json()["items"] if r["work_order_operation_id"] == operation.id)
        assert row["laser_nest_id"] == nest.id
        assert row["cnc_number"] == nest.cnc_number
        assert row["work_order_number"] == work_order.work_order_number
        assert row["sheet_descriptor"] == "A36 / 0.250 / 60 x 120"
        assert row["is_completed"] is False and row["tieable"] is True
        assert row["suggestion"]["status"] == "matched"
        assert row["suggestion"]["candidate"]["part_id"] == sheet.id
        assert row["proposed_tie"] == {
            "work_order_id": work_order.id,
            "part_id": sheet.id,
            "work_order_operation_id": operation.id,
            "source": "nest",
            "qty_per_run": 1.0,
            "qty_planned": 4.0,
        }

    def test_tied_deleted_and_other_tenant_nests_are_not_listed(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session)
        _, tied_op, _ = make_nest(db_session)
        tie(db_session, tied_op, sheet)
        _, cancelled_op, _ = make_nest(db_session)
        tie(db_session, cancelled_op, sheet, status_=AllocationStatus.CANCELLED)
        _, deleted_op, _ = make_nest(db_session, is_deleted=True)
        make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)
        _, foreign_op, _ = make_nest(db_session, company_id=COMPANY_B)

        ids = _ids(client.get(LIST_URL, headers=user_headers(manager)).json())
        assert tied_op.id not in ids
        assert cancelled_op.id in ids, "a CANCELLED tie consumes nothing, so the nest is untied"
        assert deleted_op.id not in ids
        assert foreign_op.id not in ids

    def test_completed_window_and_terminal_jobs(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        now = datetime.utcnow()
        _, recent_op, _ = make_nest(
            db_session, op_status=OperationStatus.COMPLETE, completed_at=now - timedelta(days=3), quantity_complete=4
        )
        _, old_op, _ = make_nest(
            db_session, op_status=OperationStatus.COMPLETE, completed_at=now - timedelta(days=200), quantity_complete=4
        )
        closed_wo, closed_op, _ = make_nest(
            db_session,
            op_status=OperationStatus.COMPLETE,
            wo_status=WorkOrderStatus.COMPLETE,
            completed_at=now - timedelta(days=1),
            quantity_complete=4,
        )
        _, open_op, _ = make_nest(db_session)

        payload = client.get(LIST_URL, headers=user_headers(manager)).json()
        ids = _ids(payload)
        assert {recent_op.id, closed_op.id, open_op.id} <= ids
        assert old_op.id not in ids
        closed_row = next(r for r in payload["items"] if r["work_order_operation_id"] == closed_op.id)
        assert closed_row["tieable"] is False
        assert closed_row["proposed_tie"] is None
        assert closed_wo.work_order_number in closed_row["tie_blocked_reason"]
        # Open nests come first.
        assert payload["items"][0]["is_completed"] is False

        only_open = _ids(client.get(LIST_URL, params={"status": "open"}, headers=user_headers(manager)).json())
        assert open_op.id in only_open and recent_op.id not in only_open
        only_done = _ids(client.get(LIST_URL, params={"status": "completed"}, headers=user_headers(manager)).json())
        assert recent_op.id in only_done and open_op.id not in only_done

    def test_work_order_filter_includes_laser_children(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        assembly, _ = make_wo_with_operation(db_session, work_center=wc)
        _, child_op, _ = make_nest(db_session, parent=assembly)
        _, other_op, _ = make_nest(db_session)

        ids = _ids(client.get(LIST_URL, params={"work_order_id": assembly.id}, headers=user_headers(manager)).json())
        assert ids == {child_op.id}
        assert other_op.id not in ids

    def test_proposed_tie_posts_through_the_existing_endpoint(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_sheet(db_session)
        _, operation, _ = make_nest(db_session)
        headers = user_headers(manager)

        row = next(
            r
            for r in client.get(LIST_URL, headers=headers).json()["items"]
            if r["work_order_operation_id"] == operation.id
        )
        proposed = dict(row["proposed_tie"])
        work_order_id = proposed.pop("work_order_id")
        resp = client.post(f"/api/v1/work-orders/{work_order_id}/material-allocations", json=proposed, headers=headers)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        assert operation.id not in _ids(client.get(LIST_URL, headers=headers).json())

    def test_reads_write_nothing(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_sheet(db_session)
        work_order, _, _ = make_nest(db_session)
        make_nest(db_session, op_status=OperationStatus.COMPLETE, completed_at=datetime.utcnow(), quantity_complete=2)
        headers = user_headers(manager)

        before = _writes(db_session)
        for url in (LIST_URL, COVERAGE_URL, REPORT_URL, f"{BASE}/work-orders/{work_order.id}/tie-summary"):
            assert client.get(url, headers=headers).status_code == status.HTTP_200_OK, url
        db_session.expire_all()
        assert _writes(db_session) == before

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY])
    def test_floor_and_read_only_roles_are_refused(self, client: TestClient, db_session: Session, role: UserRole):
        user = make_user(db_session, role=role)
        work_order, _, _ = make_nest(db_session)
        for url in (LIST_URL, COVERAGE_URL, REPORT_URL, f"{BASE}/work-orders/{work_order.id}/tie-summary"):
            assert client.get(url, headers=user_headers(user)).status_code == status.HTTP_403_FORBIDDEN, url

    def test_supervisor_may_read(self, client: TestClient, db_session: Session):
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        assert client.get(LIST_URL, headers=user_headers(supervisor)).status_code == status.HTTP_200_OK


class TestCoverage:
    def test_current_month_counts_tied_and_untied(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session)
        now = datetime.utcnow()
        before = client.get(COVERAGE_URL, params={"months": 3}, headers=user_headers(manager)).json()

        _, tied_op, _ = make_nest(db_session, op_status=OperationStatus.COMPLETE, completed_at=now, quantity_complete=1)
        tie(db_session, tied_op, sheet)
        make_nest(db_session, op_status=OperationStatus.COMPLETE, completed_at=now, quantity_complete=1)
        make_nest(db_session)  # open: not counted

        after = client.get(COVERAGE_URL, params={"months": 3}, headers=user_headers(manager)).json()
        assert len(after["months"]) == 3
        assert after["completed_nests"] - before["completed_nests"] == 2
        assert after["tied_nests"] - before["tied_nests"] == 1
        current = after["months"][-1]
        assert current["completed_nests"] == current["tied_nests"] + current["untied_nests"]

    def test_months_is_bounded(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        assert client.get(COVERAGE_URL, params={"months": 0}, headers=user_headers(manager)).status_code == 422
        assert client.get(COVERAGE_URL, params={"months": 25}, headers=user_headers(manager)).status_code == 422


class TestSheetReport:
    def test_groups_spelling_variants_and_sums_sheets(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session, part_number="0.188-48X96-A36")
        now = datetime.utcnow()
        make_nest(
            db_session,
            thickness="0.188",
            sheet_size="48x96",
            op_status=OperationStatus.COMPLETE,
            completed_at=now,
            quantity_complete=3,
            quantity_scrapped=1,
        )
        make_nest(
            db_session,
            thickness="0.188",
            sheet_size="96 X 48",
            op_status=OperationStatus.COMPLETE,
            completed_at=now,
            quantity_complete=2,
        )
        count = CycleCount(
            count_number=f"CC-TEST-{_next():03d}",
            part_id=sheet.id,
            scheduled_date=now.date(),
            status=CycleCountStatus.SCHEDULED,
            company_id=COMPANY_A,
        )
        db_session.add(count)
        db_session.commit()

        payload = client.get(REPORT_URL, headers=user_headers(manager)).json()
        group = next(
            g
            for g in payload["groups"]
            if g["suggestion"] and g["suggestion"]["candidate"] and g["suggestion"]["candidate"]["part_id"] == sheet.id
        )
        assert group["nest_count"] == 2
        assert group["sheets_burned"] == 6.0
        assert group["work_order_count"] == 2
        assert [c["count_number"] for c in group["open_cycle_counts"]] == [count.count_number]

    def test_tied_completed_nests_are_not_in_the_report(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session, part_number="0.375-60X120-A36")
        _, op, _ = make_nest(
            db_session,
            thickness="0.375",
            op_status=OperationStatus.COMPLETE,
            completed_at=datetime.utcnow(),
            quantity_complete=5,
        )
        tie(db_session, op, sheet)
        payload = client.get(REPORT_URL, headers=user_headers(manager)).json()
        assert not any(g["thickness"] == "0.375" for g in payload["groups"])


class TestWorkOrderSummary:
    def test_counts_own_and_child_nests(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session)
        wc = make_work_center(db_session)
        assembly, _ = make_wo_with_operation(db_session, work_center=wc)
        _, tied_op, _ = make_nest(db_session, parent=assembly)
        tie(db_session, tied_op, sheet)
        _, untied_op, _ = make_nest(db_session, parent=assembly)
        _, other_untied, _ = make_nest(db_session, parent=assembly)

        payload = client.get(f"{BASE}/work-orders/{assembly.id}/tie-summary", headers=user_headers(manager)).json()
        assert payload["nest_count"] == 3
        assert payload["tied_count"] == 1
        assert payload["untied_count"] == 2
        assert payload["message"] == "2 of 3 nests have no sheet tied"
        assert {r["work_order_operation_id"] for r in payload["untied"]} == {untied_op.id, other_untied.id}

    def test_fully_tied_and_nestless_jobs_carry_no_message(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session)
        laser_wo, op, _ = make_nest(db_session)
        tie(db_session, op, sheet)
        tied = client.get(f"{BASE}/work-orders/{laser_wo.id}/tie-summary", headers=user_headers(manager)).json()
        assert tied["nest_count"] == 1 and tied["untied_count"] == 0 and tied["message"] is None

        wc = make_work_center(db_session)
        plain, _ = make_wo_with_operation(db_session, work_center=wc)
        none = client.get(f"{BASE}/work-orders/{plain.id}/tie-summary", headers=user_headers(manager)).json()
        assert none["nest_count"] == 0 and none["message"] is None

    def test_job_level_tie_is_reported(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session)
        laser_wo, _, _ = make_nest(db_session)
        db_session.add(
            WorkOrderMaterialAllocation(
                company_id=COMPANY_A,
                work_order_id=laser_wo.id,
                work_order_operation_id=None,
                part_id=sheet.id,
                source=AllocationSource.MANUAL,
                status=AllocationStatus.OPEN,
                qty_per_run=None,
                qty_planned=4.0,
                unit_of_measure="each",
                qty_consumed=0.0,
            )
        )
        db_session.commit()
        payload = client.get(f"{BASE}/work-orders/{laser_wo.id}/tie-summary", headers=user_headers(manager)).json()
        assert payload["job_level_tie_count"] == 1
        row = next(
            r
            for r in client.get(LIST_URL, headers=user_headers(manager)).json()["items"]
            if r["work_order_id"] == laser_wo.id
        )
        assert row["job_level_tie_count"] == 1

    def test_other_tenants_job_is_404(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)
        foreign_wo, _, _ = make_nest(db_session, company_id=COMPANY_B)
        resp = client.get(f"{BASE}/work-orders/{foreign_wo.id}/tie-summary", headers=user_headers(manager))
        assert resp.status_code == status.HTTP_404_NOT_FOUND
