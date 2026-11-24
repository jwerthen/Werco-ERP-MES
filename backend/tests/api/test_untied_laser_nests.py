"""Untied laser nests: the worklist, coverage, sheet report and per-job summary.

Pinned here:

1. A nest is UNTIED when no live (OPEN or the reserved CLOSED) operation-scoped tie names
   its operation; a CANCELLED tie does not count, a soft-deleted nest and another tenant's nest never surface.
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


# ---------------------------------------------------------------------------
# Test-engineer additions: pagination, filters, matcher states, windows, refusals
# ---------------------------------------------------------------------------


def _row(payload: dict, operation_id: int) -> dict:
    return next(r for r in payload["items"] if r["work_order_operation_id"] == operation_id)


class TestListPaginationAndFilters:
    def test_limit_offset_and_total_page_without_overlap(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        ops = {make_nest(db_session)[1].id for _ in range(3)}
        headers = user_headers(manager)

        first = client.get(LIST_URL, params={"status": "open", "limit": 2, "offset": 0}, headers=headers).json()
        second = client.get(LIST_URL, params={"status": "open", "limit": 2, "offset": 2}, headers=headers).json()
        assert first["total"] == second["total"] == 3
        assert (first["limit"], first["offset"]) == (2, 0)
        assert len(first["items"]) == 2 and len(second["items"]) == 1
        assert _ids(first) | _ids(second) == ops
        assert not (_ids(first) & _ids(second))

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}, {"status": "bogus"}])
    def test_out_of_range_paging_and_unknown_status_are_422(
        self, client: TestClient, db_session: Session, params: dict
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        assert client.get(LIST_URL, params=params, headers=user_headers(manager)).status_code == 422

    def test_work_center_filter(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        _, op_a, _ = make_nest(db_session)
        _, op_b, _ = make_nest(db_session)
        ids = _ids(
            client.get(LIST_URL, params={"work_center_id": op_a.work_center_id}, headers=user_headers(manager)).json()
        )
        assert ids == {op_a.id}
        assert op_b.id not in ids

    def test_another_tenants_work_order_filter_returns_nothing(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)
        foreign_wo, _, _ = make_nest(db_session, company_id=COMPANY_B)
        make_nest(db_session)
        payload = client.get(LIST_URL, params={"work_order_id": foreign_wo.id}, headers=user_headers(manager)).json()
        assert payload["items"] == [] and payload["total"] == 0

    def test_reversed_date_window_is_422_on_list_and_report(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        params = {"completed_from": "2026-03-10", "completed_to": "2026-03-09"}
        for url in (LIST_URL, REPORT_URL):
            resp = client.get(url, params=params, headers=user_headers(manager))
            assert resp.status_code == 422, url

    def test_completed_window_is_a_company_local_day(self, client: TestClient, db_session: Session):
        """04:00 UTC on 11 March 2026 is 23:00 CDT on 10 March: it belongs to the 10th."""
        from app.models.company import Company

        manager = make_user(db_session, role=UserRole.MANAGER)
        db_session.get(Company, COMPANY_A).timezone = "America/Chicago"
        db_session.commit()
        _, op, _ = make_nest(
            db_session,
            op_status=OperationStatus.COMPLETE,
            completed_at=datetime(2026, 3, 11, 4, 0, 0),
            quantity_complete=4,
        )
        headers = user_headers(manager)
        on_tenth = client.get(
            LIST_URL, params={"completed_from": "2026-03-10", "completed_to": "2026-03-10"}, headers=headers
        ).json()
        on_eleventh = client.get(
            LIST_URL,
            params={"status": "completed", "completed_from": "2026-03-11", "completed_to": "2026-03-11"},
            headers=headers,
        ).json()
        assert op.id in _ids(on_tenth)
        assert op.id not in _ids(on_eleventh)
        assert _row(on_tenth, op.id)["completed_at"].endswith("Z")

    def test_completion_falls_back_to_updated_at(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        _, op, _ = make_nest(db_session, op_status=OperationStatus.COMPLETE, quantity_complete=4)
        assert op.actual_end is None
        row = _row(client.get(LIST_URL, headers=user_headers(manager)).json(), op.id)
        assert row["is_completed"] is True
        assert row["completed_at"] is not None


class TestMatcherStates:
    def test_ambiguous_match_lists_the_shortlist_and_proposes_nothing(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        north = make_sheet(db_session, part_number="0.250-60X120-A36-NORTH")
        south = make_sheet(db_session, part_number="0.250-60X120-A36-SOUTH")
        _, op, _ = make_nest(db_session)

        row = _row(client.get(LIST_URL, headers=user_headers(manager)).json(), op.id)
        assert row["suggestion"]["status"] == "ambiguous"
        assert row["suggestion"]["candidate"] is None
        assert {c["part_id"] for c in row["suggestion"]["candidates"]} == {north.id, south.id}
        assert row["suggestion"]["diagnostic"]
        assert row["tieable"] is True, "the planner can still pick a shortlist part"
        assert row["proposed_tie"] is None, "the matcher never guesses between two fits"

    def test_unmatched_nest_carries_no_candidate(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        _, op, _ = make_nest(db_session, material="Inconel 718", thickness="0.063", sheet_size="36x48")
        row = _row(client.get(LIST_URL, headers=user_headers(manager)).json(), op.id)
        assert row["suggestion"]["status"] == "unmatched"
        assert row["suggestion"]["candidates"] == []
        assert row["proposed_tie"] is None

    def test_blank_descriptors_render_as_question_marks(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        _, op, _ = make_nest(db_session, material=None, thickness=None, sheet_size=None)
        row = _row(client.get(LIST_URL, headers=user_headers(manager)).json(), op.id)
        assert row["sheet_descriptor"] == "? / ? / ?"

    def test_a_matcher_failure_still_returns_the_rows(self, client: TestClient, db_session: Session, monkeypatch):
        from app.services import untied_nest_service

        def _boom(*_args, **_kwargs):
            raise RuntimeError("matcher exploded")

        monkeypatch.setattr(untied_nest_service, "match_sheet_parts", _boom)
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_sheet(db_session)
        _, op, _ = make_nest(db_session)
        make_nest(db_session, op_status=OperationStatus.COMPLETE, completed_at=datetime.utcnow(), quantity_complete=1)
        headers = user_headers(manager)

        payload = client.get(LIST_URL, headers=headers).json()
        assert payload["matcher_available"] is False
        row = _row(payload, op.id)
        assert row["suggestion"] is None and row["proposed_tie"] is None

        report = client.get(REPORT_URL, headers=headers).json()
        assert report["matcher_available"] is False
        assert report["nest_count"] >= 1


class TestCoverageBuckets:
    def test_months_follow_the_company_time_zone(self, db_session: Session):
        """03:00 UTC on 1 Feb 2025 is 21:00 CST on 31 Jan: a January completion."""
        from datetime import date

        from app.models.company import Company
        from app.services.untied_nest_service import nest_tie_coverage

        make_user(db_session, role=UserRole.MANAGER)
        db_session.get(Company, COMPANY_A).timezone = "America/Chicago"
        db_session.commit()
        sheet = make_sheet(db_session)
        make_nest(
            db_session,
            op_status=OperationStatus.COMPLETE,
            completed_at=datetime(2025, 2, 1, 3, 0, 0),
            quantity_complete=1,
        )
        _, tied_op, _ = make_nest(
            db_session,
            op_status=OperationStatus.COMPLETE,
            completed_at=datetime(2025, 2, 15, 12, 0, 0),
            quantity_complete=1,
        )
        tie(db_session, tied_op, sheet)

        result = nest_tie_coverage(db_session, COMPANY_A, date(2025, 1, 1), 2, None)
        by_month = {m.month: m for m in result.months}
        assert list(by_month) == ["2025-01", "2025-02"]
        assert (by_month["2025-01"].completed_nests, by_month["2025-01"].tied_nests) == (1, 0)
        assert by_month["2025-01"].tied_percent == 0.0
        assert (by_month["2025-02"].completed_nests, by_month["2025-02"].tied_nests) == (1, 1)
        assert by_month["2025-02"].tied_percent == 100.0
        assert result.tied_percent == 50.0

    def test_a_month_with_no_completions_reads_null_percent(self, db_session: Session):
        from datetime import date

        from app.services.untied_nest_service import nest_tie_coverage

        make_user(db_session, role=UserRole.MANAGER)
        result = nest_tie_coverage(db_session, COMPANY_A, date(2024, 1, 1), 1, None)
        assert [(m.month, m.completed_nests, m.tied_percent) for m in result.months] == [("2024-01", 0, None)]
        assert result.tied_percent is None


class TestSheetReportEdges:
    def test_truncation_is_reported(self, client: TestClient, db_session: Session, monkeypatch):
        from app.services import untied_nest_service

        monkeypatch.setattr(untied_nest_service, "SHEET_REPORT_MAX_ROWS", 1)
        manager = make_user(db_session, role=UserRole.MANAGER)
        now = datetime.utcnow()
        for _ in range(2):
            make_nest(db_session, op_status=OperationStatus.COMPLETE, completed_at=now, quantity_complete=1)
        payload = client.get(REPORT_URL, headers=user_headers(manager)).json()
        assert payload["truncated"] is True
        assert payload["nest_count"] == 1

    def test_window_excludes_older_completions(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_nest(
            db_session,
            thickness="0.500",
            op_status=OperationStatus.COMPLETE,
            completed_at=datetime.utcnow() - timedelta(days=200),
            quantity_complete=9,
        )
        payload = client.get(REPORT_URL, headers=user_headers(manager)).json()
        assert not any(g["thickness"] == "0.500" for g in payload["groups"])
        assert payload["truncated"] is False

    def test_open_nests_are_not_in_the_report(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_nest(db_session, thickness="0.625", quantity_complete=2)
        payload = client.get(REPORT_URL, headers=user_headers(manager)).json()
        assert not any(g["thickness"] == "0.625" for g in payload["groups"])

    def test_ambiguous_group_lists_the_open_count_on_the_top_shortlist_part(
        self, client: TestClient, db_session: Session
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        north = make_sheet(db_session, part_number="0.750-60X120-A36-NORTH")
        make_sheet(db_session, part_number="0.750-60X120-A36-SOUTH")
        now = datetime.utcnow()
        make_nest(
            db_session, thickness="0.750", op_status=OperationStatus.COMPLETE, completed_at=now, quantity_complete=1
        )
        count = CycleCount(
            count_number=f"CC-TEST-{_next():03d}",
            part_id=north.id,
            scheduled_date=now.date(),
            status=CycleCountStatus.IN_PROGRESS,
            company_id=COMPANY_A,
        )
        closed = CycleCount(
            count_number=f"CC-TEST-{_next():03d}",
            part_id=north.id,
            scheduled_date=now.date(),
            status=CycleCountStatus.COMPLETED,
            company_id=COMPANY_A,
        )
        db_session.add_all([count, closed])
        db_session.commit()

        group = next(
            g
            for g in client.get(REPORT_URL, headers=user_headers(manager)).json()["groups"]
            if g["thickness"] == "0.750"
        )
        assert group["suggestion"]["status"] == "ambiguous"
        assert group["suggestion"]["candidates"][0]["part_id"] == north.id
        assert [c["count_number"] for c in group["open_cycle_counts"]] == [
            count.count_number
        ], "a finished count is not open"


class TestSummaryEdges:
    def test_singular_message(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        laser_wo, _, _ = make_nest(db_session)
        payload = client.get(f"{BASE}/work-orders/{laser_wo.id}/tie-summary", headers=user_headers(manager)).json()
        assert payload["message"] == "1 of 1 nest has no sheet tied"

    def test_deleted_and_missing_jobs_are_404(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        laser_wo, _, _ = make_nest(db_session)
        laser_wo.is_deleted = True
        db_session.commit()
        headers = user_headers(manager)
        for wo_id in (laser_wo.id, 999_999):
            resp = client.get(f"{BASE}/work-orders/{wo_id}/tie-summary", headers=headers)
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Work order not found"

    def test_deleted_nest_is_not_counted(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        assembly, _ = make_wo_with_operation(db_session, work_center=wc)
        make_nest(db_session, parent=assembly)
        make_nest(db_session, parent=assembly, is_deleted=True)
        payload = client.get(f"{BASE}/work-orders/{assembly.id}/tie-summary", headers=user_headers(manager)).json()
        assert payload["nest_count"] == 1


class TestAccess:
    def test_admin_may_read_and_shipping_may_not(self, client: TestClient, db_session: Session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        shipping = make_user(db_session, role=UserRole.SHIPPING)
        assert client.get(LIST_URL, headers=user_headers(admin)).status_code == status.HTTP_200_OK
        assert client.get(LIST_URL, headers=user_headers(shipping)).status_code == status.HTTP_403_FORBIDDEN

    def test_a_kiosk_station_token_is_refused(self, client: TestClient, db_session: Session):
        from tests.api.kiosk_test_helpers import bearer, kiosk_token_for, make_kiosk_station

        station = make_kiosk_station(db_session)
        work_order, _, _ = make_nest(db_session)
        headers = bearer(kiosk_token_for(station))
        for url in (LIST_URL, COVERAGE_URL, REPORT_URL, f"{BASE}/work-orders/{work_order.id}/tie-summary"):
            assert client.get(url, headers=headers).status_code in (401, 403), url

    def test_reads_create_no_tie_and_no_cycle_count_and_move_no_status(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_sheet(db_session)
        work_order, operation, _ = make_nest(db_session, op_status=OperationStatus.PENDING)
        make_nest(db_session, op_status=OperationStatus.COMPLETE, completed_at=datetime.utcnow(), quantity_complete=2)
        headers = user_headers(manager)
        before = (
            db_session.query(WorkOrderMaterialAllocation).count(),
            db_session.query(CycleCount).count(),
        )
        for url in (LIST_URL, COVERAGE_URL, REPORT_URL, f"{BASE}/work-orders/{work_order.id}/tie-summary"):
            assert client.get(url, headers=headers).status_code == status.HTTP_200_OK, url
        db_session.expire_all()
        assert (
            db_session.query(WorkOrderMaterialAllocation).count(),
            db_session.query(CycleCount).count(),
        ) == before
        assert db_session.get(WorkOrderOperation, operation.id).status == OperationStatus.PENDING


class TestReviewFixes:
    """Second-review fixes: the count-versus-tie split, the job filter's window, the savepoint."""

    def test_sheet_report_splits_sheets_still_tieable_on_open_jobs(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        make_sheet(db_session, part_number="0.500-60X120-A36")
        now = datetime.utcnow()
        make_nest(
            db_session,
            thickness="0.500",
            op_status=OperationStatus.COMPLETE,
            completed_at=now,
            quantity_complete=4,
        )
        make_nest(
            db_session,
            thickness="0.500",
            op_status=OperationStatus.COMPLETE,
            wo_status=WorkOrderStatus.COMPLETE,
            completed_at=now,
            quantity_complete=2,
        )
        payload = client.get(REPORT_URL, headers=user_headers(manager)).json()
        group = next(g for g in payload["groups"] if g["thickness"] == "0.500")
        assert group["sheets_burned"] == 6.0
        assert group["tieable_nest_count"] == 1
        assert group["tieable_sheets_burned"] == 4.0
        assert payload["tieable_sheets_burned"] == 4.0

    def test_worklist_flags_a_part_counted_after_the_nest_completed(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        counted = make_sheet(db_session, part_number="0.625-60X120-A36")
        earlier = make_sheet(db_session, part_number="0.750-60X120-A36")
        now = datetime.utcnow()
        _, counted_op, _ = make_nest(
            db_session,
            thickness="0.625",
            op_status=OperationStatus.COMPLETE,
            completed_at=now - timedelta(days=3),
            quantity_complete=4,
        )
        _, earlier_op, _ = make_nest(
            db_session,
            thickness="0.750",
            op_status=OperationStatus.COMPLETE,
            completed_at=now - timedelta(days=3),
            quantity_complete=4,
        )
        for part, completed_at in ((counted, now - timedelta(days=1)), (earlier, now - timedelta(days=5))):
            db_session.add(
                CycleCount(
                    count_number=f"CC-TEST-{_next():03d}",
                    part_id=part.id,
                    scheduled_date=completed_at.date(),
                    status=CycleCountStatus.COMPLETED,
                    completed_at=completed_at,
                    company_id=COMPANY_A,
                )
            )
        db_session.commit()

        payload = client.get(LIST_URL, headers=user_headers(manager)).json()
        assert _row(payload, counted_op.id)["counted_since_completion_part_ids"] == [counted.id]
        assert _row(payload, earlier_op.id)["counted_since_completion_part_ids"] == []

    def test_job_filter_lists_old_completions_and_only_laser_children(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        assembly, _ = make_wo_with_operation(db_session, work_center=wc)
        _, old_op, _ = make_nest(
            db_session,
            parent=assembly,
            op_status=OperationStatus.COMPLETE,
            completed_at=datetime.utcnow() - timedelta(days=200),
            quantity_complete=3,
        )
        production_child, stray_op, _ = make_nest(db_session, parent=assembly)
        production_child.work_order_type = WorkOrderType.PRODUCTION.value
        db_session.commit()
        headers = user_headers(manager)

        payload = client.get(LIST_URL, params={"work_order_id": assembly.id}, headers=headers).json()
        assert _ids(payload) == {old_op.id}
        assert payload["completed_from"] is None
        summary = client.get(f"{BASE}/work-orders/{assembly.id}/tie-summary", headers=headers).json()
        assert summary["untied_count"] == len(payload["items"])
        # Without the job filter the 90-day default still applies.
        assert old_op.id not in _ids(client.get(LIST_URL, headers=headers).json())

    def test_a_closed_tie_counts_as_tied(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        sheet = make_sheet(db_session)
        _, op, _ = make_nest(db_session)
        tie(db_session, op, sheet, status_=AllocationStatus.CLOSED)
        assert op.id not in _ids(client.get(LIST_URL, headers=user_headers(manager)).json())

    def test_open_rows_page_in_job_order_not_by_last_write(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        first_wo, _, _ = make_nest(db_session)
        second_wo, second_op, _ = make_nest(db_session)
        second_op.updated_at = datetime.utcnow() + timedelta(hours=1)
        db_session.commit()
        numbers = [
            row["work_order_number"]
            for row in client.get(LIST_URL, params={"status": "open"}, headers=user_headers(manager)).json()["items"]
        ]
        assert numbers == sorted(numbers)
        assert {first_wo.work_order_number, second_wo.work_order_number} <= set(numbers)

    def test_matcher_runs_inside_a_savepoint(self, client: TestClient, db_session: Session, monkeypatch):
        """A database error in the matcher must roll back only its savepoint (Postgres aborts otherwise)."""
        from sqlalchemy import text

        from app.services import untied_nest_service

        seen = []

        def _db_error(db, **_kwargs):
            seen.append(db.in_nested_transaction())
            db.execute(text("SELECT * FROM no_such_table_for_untied_nests"))

        monkeypatch.setattr(untied_nest_service, "match_sheet_parts", _db_error)
        manager = make_user(db_session, role=UserRole.MANAGER)
        _, op, _ = make_nest(db_session)
        make_nest(db_session, op_status=OperationStatus.COMPLETE, completed_at=datetime.utcnow(), quantity_complete=1)
        headers = user_headers(manager)

        payload = client.get(LIST_URL, headers=headers).json()
        assert payload["matcher_available"] is False
        assert _row(payload, op.id)["job_level_tie_count"] == 0
        report = client.get(REPORT_URL, headers=headers)
        assert report.status_code == status.HTTP_200_OK
        assert report.json()["matcher_available"] is False
        assert seen and all(seen)
