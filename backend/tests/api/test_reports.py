from datetime import date, datetime, timedelta
from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, ensure_company, make_user


@pytest.mark.api
@pytest.mark.requires_db
class TestReportsAPI:
    def test_employee_time_includes_operation_completion_without_time_entry(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="REPORT-COMP-001",
            name="Report Completion Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-REPORT-COMP",
            name="Report Completion Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-REPORT-COMP",
            part_id=part.id,
            quantity_ordered=5,
            status=WorkOrderStatus.COMPLETE,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        completed_at = datetime.utcnow()
        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="Op 10",
            name="Complete Without Time Entry",
            status=OperationStatus.COMPLETE,
            quantity_complete=5,
            completed_by=operator_user.id,
            actual_end=completed_at,
            company_id=1,
        )
        db_session.add(operation)
        db_session.commit()

        response = client.get(
            "/api/v1/reports/employee-time",
            headers=auth_headers,
            params={
                "start_date": completed_at.date().isoformat(),
                "end_date": completed_at.date().isoformat(),
                "user_id": operator_user.id,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["completed_operations"] == 1
        assert data[0]["quantity_produced"] == 5
        assert data[0]["entries"][0]["source"] == "operation_completion"
        assert data[0]["entries"][0]["work_order_number"] == "WO-REPORT-COMP"

    def test_employee_time_includes_entries_clocked_out_inside_window(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="REPORT-TIME-001",
            name="Report Time Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-REPORT-TIME",
            name="Report Time Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-REPORT-TIME",
            part_id=part.id,
            quantity_ordered=2,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="Op 10",
            name="Crosses Report Window",
            status=OperationStatus.COMPLETE,
            quantity_complete=2,
            completed_by=operator_user.id,
            actual_end=datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=1),
            company_id=1,
        )
        db_session.add(operation)
        db_session.flush()

        entry = TimeEntry(
            user_id=operator_user.id,
            work_order_id=work_order.id,
            operation_id=operation.id,
            work_center_id=work_center.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.combine(date.today() - timedelta(days=1), datetime.max.time()),
            clock_out=datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=1),
            duration_hours=1,
            quantity_produced=2,
            company_id=1,
        )
        db_session.add(entry)
        db_session.commit()

        response = client.get(
            "/api/v1/reports/employee-time",
            headers=auth_headers,
            params={
                "start_date": date.today().isoformat(),
                "end_date": date.today().isoformat(),
                "user_id": operator_user.id,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["total_hours"] == 1
        assert data[0]["completed_operations"] == 1
        assert data[0]["entries"][0]["source"] == "time_entry"

    def test_work_order_costing_returns_200_for_wo_with_part(self, client: TestClient, auth_headers: dict, db_session):
        """Regression: the costing report read the nonexistent ``part.unit_cost``
        column, raising AttributeError -> HTTP 500 and dark-screening Reports.

        This is the exact previously-500ing path: a work order whose part has
        costs. Must now return 200 with a sane shape.
        """
        part = Part(
            part_number="COST-200-001",
            name="Costing 200 Part",
            part_type="manufactured",
            unit_of_measure="each",
            standard_cost=18.0,
            material_cost=10.0,
            labor_cost=5.0,
            overhead_cost=3.0,
            is_active=True,
            company_id=1,
        )
        db_session.add(part)
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-COST-200",
            part_id=part.id,
            quantity_ordered=7,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.commit()

        response = client.get(
            "/api/v1/reports/work-order-costing",
            headers=auth_headers,
            params={"work_order_id": work_order.id},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)
        row = next(item for item in data if item["work_order_number"] == "WO-COST-200")
        assert row["work_order_id"] == work_order.id
        assert row["part_number"] == "COST-200-001"
        assert row["quantity"] == 7
        # Shape sanity: the cost roll-up fields are present and total is consistent.
        for field in ("estimated_material", "actual_material", "actual_labor", "actual_overhead", "actual_total"):
            assert field in row
        assert row["actual_total"] == row["actual_material"] + row["actual_labor"] + row["actual_overhead"]

    def test_work_order_costing_uses_material_component_not_standard_cost(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Pin the no-double-count fix: the material line uses the MATERIAL
        component (``part.material_cost``), NOT the fully-loaded
        ``standard_cost`` (which already bundles labor + overhead). Using
        standard_cost here would double-count labor/overhead, since the report
        adds those separately.
        """
        part = Part(
            part_number="COST-MAT-001",
            name="Material Component Part",
            part_type="manufactured",
            unit_of_measure="each",
            # Distinct values so material vs standard can't be confused.
            material_cost=10.0,
            labor_cost=5.0,
            overhead_cost=3.0,
            standard_cost=18.0,
            is_active=True,
            company_id=1,
        )
        db_session.add(part)
        db_session.flush()

        quantity = 7
        work_order = WorkOrder(
            work_order_number="WO-COST-MAT",
            part_id=part.id,
            quantity_ordered=quantity,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.commit()

        response = client.get(
            "/api/v1/reports/work-order-costing",
            headers=auth_headers,
            params={"work_order_id": work_order.id},
        )

        assert response.status_code == status.HTTP_200_OK
        row = next(item for item in response.json() if item["work_order_number"] == "WO-COST-MAT")

        expected_material = part.material_cost * quantity  # 10 * 7 = 70
        double_counted = part.standard_cost * quantity  # 18 * 7 = 126 (the bug)

        assert row["actual_material"] == expected_material
        assert row["estimated_material"] == expected_material
        assert row["actual_material"] != double_counted

    def test_shop_floor_dashboard_recent_completions_uses_completed_operations(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="DASH-COMP-001",
            name="Dashboard Completion Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-DASH-COMP",
            name="Dashboard Completion Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-DASH-COMP",
            part_id=part.id,
            quantity_ordered=4,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="Op 10",
            name="Dashboard Completed Op",
            status=OperationStatus.COMPLETE,
            quantity_complete=4,
            completed_by=operator_user.id,
            actual_end=datetime.utcnow(),
            company_id=1,
        )
        db_session.add(operation)
        db_session.commit()

        response = client.get("/api/v1/shop-floor/dashboard", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        completions = response.json()["recent_completions"]
        assert any(
            item["work_order_number"] == "WO-DASH-COMP"
            and item["operation_name"] == "Dashboard Completed Op"
            and item["operator_name"] == operator_user.full_name
            for item in completions
        )


def _headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_vendor(db: Session, *, company_id: int, tag: str, deleted_by: Optional[int] = None) -> Vendor:
    ensure_company(db, company_id)
    vendor = Vendor(
        code=f"VND-RPT-{tag}",
        name=f"Reports Tenancy Vendor {tag}",
        is_active=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.flush()
    if deleted_by is not None:
        vendor.soft_delete(deleted_by)
    db.commit()
    return vendor


def _make_po_with_line(
    db: Session,
    *,
    company_id: int,
    vendor: Vendor,
    tag: str,
    quantity_ordered: float,
    quantity_received: float,
    deleted_by: Optional[int] = None,
) -> PurchaseOrder:
    """Seed one PO + one line against ``vendor``.

    ``company_id`` stamps the PO independently of the vendor's company on
    purpose: the cross-tenant test needs a company-B PO pointing at company A's
    vendor, which is exactly the mis-stamped shape the explicit company_id
    predicate has to survive.
    """
    ensure_company(db, company_id)

    part = Part(
        part_number=f"RPT-VP-{tag}",
        name=f"Reports Vendor Part {tag}",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()

    po = PurchaseOrder(
        po_number=f"PO-RPT-{tag}",
        vendor_id=vendor.id,
        status=POStatus.SENT,
        company_id=company_id,
    )
    db.add(po)
    db.flush()

    if deleted_by is not None:
        po.soft_delete(deleted_by)

    db.add(
        PurchaseOrderLine(
            purchase_order_id=po.id,
            line_number=1,
            part_id=part.id,
            quantity_ordered=quantity_ordered,
            quantity_received=quantity_received,
            unit_price=1.0,
            company_id=company_id,
        )
    )
    db.commit()
    return po


def _seed_production(
    db: Session,
    *,
    company_id: int,
    user: User,
    tag: str,
    hours: float,
    produced: float,
    scrapped: float,
    completed_at: datetime,
    is_deleted: bool = False,
) -> WorkOrder:
    """Seed one company's worth of production evidence.

    Creates a part, a work center, a COMPLETE work order, a completed operation
    carrying ``produced``/``scrapped``, and a closed time entry carrying
    ``hours`` -- i.e. exactly the rows the production-summary and daily-output
    aggregates read.
    """
    ensure_company(db, company_id)

    part = Part(
        part_number=f"RPT-TEN-{tag}",
        name=f"Reports Tenancy Part {tag}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    work_center = WorkCenter(
        code=f"WC-RPT-{tag}",
        name=f"Reports Tenancy WC {tag}",
        work_center_type="laser",
        is_active=True,
        company_id=company_id,
    )
    db.add_all([part, work_center])
    db.flush()

    work_order = WorkOrder(
        work_order_number=f"WO-RPT-TEN-{tag}",
        part_id=part.id,
        quantity_ordered=produced,
        status=WorkOrderStatus.COMPLETE,
        priority=5,
        actual_end=completed_at,
        company_id=company_id,
    )
    db.add(work_order)
    db.flush()

    if is_deleted:
        work_order.soft_delete(user.id)

    operation = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=work_center.id,
        sequence=10,
        operation_number="Op 10",
        name=f"Reports Tenancy Op {tag}",
        status=OperationStatus.COMPLETE,
        quantity_complete=produced,
        quantity_scrapped=scrapped,
        completed_by=user.id,
        actual_end=completed_at,
        company_id=company_id,
    )
    db.add(operation)
    db.flush()

    db.add(
        TimeEntry(
            user_id=user.id,
            work_order_id=work_order.id,
            operation_id=operation.id,
            work_center_id=work_center.id,
            entry_type=TimeEntryType.RUN,
            # Fixed offset, NOT ``hours``: the aggregate sums duration_hours but
            # filters on clock_in, and a large duration must not push clock_in
            # outside the report window.
            clock_in=completed_at - timedelta(hours=1),
            clock_out=completed_at,
            duration_hours=hours,
            quantity_produced=produced,
            quantity_scrapped=scrapped,
            company_id=company_id,
        )
    )
    db.commit()
    return work_order


@pytest.mark.api
@pytest.mark.requires_db
class TestReportsTenantIsolation:
    """Regression cover for the cross-tenant aggregate leak in reports.py.

    ``/reports/production-summary`` summed ``TimeEntry.duration_hours`` and
    ``WorkOrderOperation.quantity_complete``/``quantity_scrapped`` with no
    ``company_id`` predicate, and ``/reports/daily-output`` did the same for its
    per-day buckets. Every other tenant's hours, output and scrap landed in the
    caller's headline numbers -- and because scrap_rate_pct divides one leaked
    aggregate by another, the caller's own rate was arithmetically wrong, not
    merely inflated.

    Company B is deliberately seeded with values an order of magnitude larger
    than company A's so any leak is visible as a wrong number, not a rounding
    difference.
    """

    def test_production_summary_excludes_other_company_hours_and_quantities(
        self, client: TestClient, db_session: Session
    ):
        completed_at = datetime.utcnow() - timedelta(hours=2)
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)

        _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="A1",
            hours=4.0,
            produced=10,
            scrapped=2,
            completed_at=completed_at,
        )
        # Company B: values that would swamp the totals if they leaked.
        _seed_production(
            db_session,
            company_id=COMPANY_B,
            user=admin_b,
            tag="B1",
            hours=900.0,
            produced=5000,
            scrapped=1000,
            completed_at=completed_at,
        )

        response = client.get("/api/v1/reports/production-summary", headers=_headers_for(admin_a))

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total_hours_worked"] == 4.0
        assert data["total_produced"] == 10
        assert data["total_scrapped"] == 2
        # 2 / (10 + 2) -- computed from company A's numerator AND denominator.
        assert data["scrap_rate_pct"] == pytest.approx(2 / 12 * 100)
        assert data["total_completed"] == 1

    def test_production_summary_is_symmetric_for_the_second_company(self, client: TestClient, db_session: Session):
        """The mirror case: company B must see only its own numbers either."""
        completed_at = datetime.utcnow() - timedelta(hours=2)
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)

        _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="A2",
            hours=4.0,
            produced=10,
            scrapped=2,
            completed_at=completed_at,
        )
        _seed_production(
            db_session,
            company_id=COMPANY_B,
            user=admin_b,
            tag="B2",
            hours=900.0,
            produced=5000,
            scrapped=1000,
            completed_at=completed_at,
        )

        data = client.get("/api/v1/reports/production-summary", headers=_headers_for(admin_b)).json()

        assert data["total_hours_worked"] == 900.0
        assert data["total_produced"] == 5000
        assert data["total_scrapped"] == 1000
        assert data["total_completed"] == 1

    def test_daily_output_excludes_other_company_operations(self, client: TestClient, db_session: Session):
        # Anchor inside today's bucket so the default 14-day window covers it.
        completed_at = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=9)
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)

        _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="A3",
            hours=4.0,
            produced=10,
            scrapped=2,
            completed_at=completed_at,
        )
        _seed_production(
            db_session,
            company_id=COMPANY_B,
            user=admin_b,
            tag="B3",
            hours=900.0,
            produced=5000,
            scrapped=1000,
            completed_at=completed_at,
        )

        response = client.get("/api/v1/reports/daily-output", headers=_headers_for(admin_a))

        assert response.status_code == status.HTTP_200_OK
        buckets = response.json()
        today = next(row for row in buckets if row["date"] == date.today().isoformat())
        assert today["completed"] == 10
        assert today["scrapped"] == 2
        # And nothing leaked into any other bucket either.
        assert sum(row["completed"] for row in buckets) == 10
        assert sum(row["scrapped"] for row in buckets) == 2

    def test_production_summary_excludes_soft_deleted_work_orders(self, client: TestClient, db_session: Session):
        """A soft-deleted work order is not output.

        Every number in the payload must be computed over the SAME population:
        excluding the deleted WO from total_completed while its operations still
        feed total_produced/total_scrapped/total_hours_worked leaves the response
        self-contradicting ("1 completed work order, 17 pieces produced") and puts
        scrap_rate_pct back on a different population than the headline count.
        """
        completed_at = datetime.utcnow() - timedelta(hours=2)
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)

        _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="LIVE",
            hours=4.0,
            produced=10,
            scrapped=2,
            completed_at=completed_at,
        )
        deleted_wo = _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="DEL",
            hours=1.0,
            produced=7,
            scrapped=1,
            completed_at=completed_at,
            is_deleted=True,
        )
        assert deleted_wo.is_deleted is True

        data = client.get("/api/v1/reports/production-summary", headers=_headers_for(admin_a)).json()

        assert data["total_completed"] == 1  # only the live WO
        assert data["work_orders_by_status"].get(WorkOrderStatus.COMPLETE.value) == 1
        # The quantity aggregates must agree with that headline count.
        assert data["total_produced"] == 10  # not 17
        assert data["total_scrapped"] == 2  # not 3
        assert data["total_hours_worked"] == 4.0  # not 5.0
        # 2 / (10 + 2) = 16.67, the live shop's truth -- not 3/20 = 15.0.
        assert data["scrap_rate_pct"] == pytest.approx(2 / 12 * 100)

    def test_daily_output_excludes_soft_deleted_work_orders(self, client: TestClient, db_session: Session):
        """daily-output and production-summary sit on the same dashboard, so the
        trend must not count output the summary excludes."""
        completed_at = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=9)
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)

        _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="DLIVE",
            hours=4.0,
            produced=10,
            scrapped=2,
            completed_at=completed_at,
        )
        _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="DDEL",
            hours=1.0,
            produced=7,
            scrapped=1,
            completed_at=completed_at,
            is_deleted=True,
        )

        buckets = client.get("/api/v1/reports/daily-output", headers=_headers_for(admin_a)).json()

        today = next(row for row in buckets if row["date"] == date.today().isoformat())
        assert today["completed"] == 10  # not 17
        assert today["scrapped"] == 2  # not 3

    def test_production_summary_keeps_labor_not_tied_to_any_work_order(self, client: TestClient, db_session: Session):
        """The soft-delete join must not drop indirect labor.

        TimeEntry.work_order_id is nullable, so excluding deleted parents with an
        INNER join would silently delete non-job hours from total_hours_worked.
        The outer join keeps them.
        """
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        db_session.add(
            TimeEntry(
                user_id=admin_a.id,
                work_order_id=None,  # indirect / non-job labor
                entry_type=TimeEntryType.RUN,
                clock_in=datetime.utcnow() - timedelta(hours=3),
                clock_out=datetime.utcnow() - timedelta(hours=1),
                duration_hours=2.0,
                company_id=COMPANY_A,
            )
        )
        db_session.commit()

        data = client.get("/api/v1/reports/production-summary", headers=_headers_for(admin_a)).json()

        assert data["total_hours_worked"] == 2.0

    def test_work_center_utilization_excludes_another_companys_time_entries(
        self, client: TestClient, db_session: Session
    ):
        """The utilization aggregate was scoped only through the work-center id.

        That is unsafe for the same reason it was unsafe at vendor-performance:
        TimeEntry.work_center_id is a plain FK, and the operation-CREATE paths did
        not validate that a caller's work_center_id belonged to their company (now
        fixed in work_orders.py, but pre-existing rows persist). One foreign entry
        reported as this shop's hours and pushed utilization_pct past 100%.
        """
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        ensure_company(db_session, COMPANY_B)

        work_center_a = WorkCenter(
            code="WC-UTIL-A",
            name="Utilization WC A",
            work_center_type="laser",
            is_active=True,
            company_id=COMPANY_A,
        )
        db_session.add(work_center_a)
        db_session.flush()

        user_b = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)
        now = datetime.utcnow()
        db_session.add_all(
            [
                TimeEntry(
                    user_id=admin_a.id,
                    work_center_id=work_center_a.id,
                    entry_type=TimeEntryType.RUN,
                    clock_in=now - timedelta(hours=4),
                    clock_out=now,
                    duration_hours=3.0,
                    company_id=COMPANY_A,
                ),
                # Company B's labor, stamped onto company A's work center.
                TimeEntry(
                    user_id=user_b.id,
                    work_center_id=work_center_a.id,
                    entry_type=TimeEntryType.RUN,
                    clock_in=now - timedelta(hours=4),
                    clock_out=now,
                    duration_hours=777.0,
                    company_id=COMPANY_B,
                ),
            ]
        )
        db_session.commit()

        rows = client.get("/api/v1/reports/work-center-utilization", headers=_headers_for(admin_a)).json()

        row = next(r for r in rows if r["work_center_id"] == work_center_a.id)
        assert row["hours_worked"] == 3.0  # not 780.0
        assert row["utilization_pct"] < 100  # not 325.0

    def test_work_order_costing_excludes_soft_deleted_work_orders(self, client: TestClient, db_session: Session):
        """Record-level read leak: a deleted job kept rendering as a full costing
        row, still asserting cost and variance against a live part and customer."""
        completed_at = datetime.utcnow() - timedelta(hours=2)
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)

        live_wo = _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="CLIVE",
            hours=4.0,
            produced=10,
            scrapped=2,
            completed_at=completed_at,
        )
        deleted_wo = _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="CDEL",
            hours=1.0,
            produced=7,
            scrapped=1,
            completed_at=completed_at,
            is_deleted=True,
        )

        rows = client.get("/api/v1/reports/work-order-costing", headers=_headers_for(admin_a)).json()

        ids = {r["work_order_id"] for r in rows}
        assert live_wo.id in ids
        assert deleted_wo.id not in ids

    def test_work_order_costing_excludes_another_companys_work_orders(self, client: TestClient, db_session: Session):
        completed_at = datetime.utcnow() - timedelta(hours=2)
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)

        wo_a = _seed_production(
            db_session,
            company_id=COMPANY_A,
            user=admin_a,
            tag="CTA",
            hours=4.0,
            produced=10,
            scrapped=2,
            completed_at=completed_at,
        )
        wo_b = _seed_production(
            db_session,
            company_id=COMPANY_B,
            user=admin_b,
            tag="CTB",
            hours=900.0,
            produced=5000,
            scrapped=1000,
            completed_at=completed_at,
        )

        rows = client.get("/api/v1/reports/work-order-costing", headers=_headers_for(admin_a)).json()

        ids = {r["work_order_id"] for r in rows}
        assert wo_a.id in ids
        assert wo_b.id not in ids

    def test_vendor_performance_ignores_a_purchase_order_stamped_to_another_company(
        self, client: TestClient, db_session: Session
    ):
        """The vendor scorecard aggregates were scoped only TRANSITIVELY, through
        the vendor_id join. That holds exactly as long as every FK is correctly
        stamped, so this test seeds the shape it does NOT survive: a company-B
        purchase order pointing at company A's vendor. With an explicit
        PurchaseOrder.company_id predicate the foreign PO cannot contribute; with
        transitive-only scoping its 900 units corrupt company A's scorecard.
        """
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        ensure_company(db_session, COMPANY_B)
        vendor_a = _make_vendor(db_session, company_id=COMPANY_A, tag="VP1")

        _make_po_with_line(
            db_session,
            company_id=COMPANY_A,
            vendor=vendor_a,
            tag="A-VP1",
            quantity_ordered=10,
            quantity_received=10,
        )
        # Mis-stamped / hostile row: company B's PO against company A's vendor.
        _make_po_with_line(
            db_session,
            company_id=COMPANY_B,
            vendor=vendor_a,
            tag="B-VP1",
            quantity_ordered=900,
            quantity_received=450,
        )

        rows = client.get("/api/v1/reports/vendor-performance", headers=_headers_for(admin_a)).json()

        row = next(r for r in rows if r["vendor_id"] == vendor_a.id)
        assert row["total_ordered"] == 10  # company B's 900 is excluded
        assert row["total_received"] == 10
        assert row["po_count"] == 1

    def test_vendor_performance_excludes_soft_deleted_purchase_orders(self, client: TestClient, db_session: Session):
        """PurchaseOrder is soft-deletable (migration 071); a deleted PO must not
        feed the vendor scorecard."""
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        vendor_a = _make_vendor(db_session, company_id=COMPANY_A, tag="VP2")

        _make_po_with_line(
            db_session,
            company_id=COMPANY_A,
            vendor=vendor_a,
            tag="A-LIVE",
            quantity_ordered=10,
            quantity_received=10,
        )
        _make_po_with_line(
            db_session,
            company_id=COMPANY_A,
            vendor=vendor_a,
            tag="A-DEL",
            quantity_ordered=500,
            quantity_received=250,
            deleted_by=admin_a.id,
        )

        rows = client.get("/api/v1/reports/vendor-performance", headers=_headers_for(admin_a)).json()

        row = next(r for r in rows if r["vendor_id"] == vendor_a.id)
        assert row["total_ordered"] == 10  # the deleted PO's 500 is excluded
        assert row["po_count"] == 1

    def test_vendor_performance_excludes_soft_deleted_vendors(self, client: TestClient, db_session: Session):
        """A soft-deleted vendor must not appear on the scorecard at all."""
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        vendor_a = _make_vendor(db_session, company_id=COMPANY_A, tag="VP3", deleted_by=admin_a.id)

        _make_po_with_line(
            db_session,
            company_id=COMPANY_A,
            vendor=vendor_a,
            tag="A-VDEL",
            quantity_ordered=10,
            quantity_received=10,
        )

        rows = client.get("/api/v1/reports/vendor-performance", headers=_headers_for(admin_a)).json()

        assert all(r["vendor_id"] != vendor_a.id for r in rows)

    @pytest.mark.parametrize(
        "endpoint,days",
        [
            # daily-output is the resource bug: ``days`` drives a per-day loop
            # issuing two aggregates per iteration, so an unbounded value pins a
            # worker. It must be REJECTED, not executed.
            ("daily-output", 1000000),
            ("daily-output", 366),
            ("daily-output", 0),
            ("daily-output", -1),
            ("production-summary", 1000000),
            ("production-summary", 0),
            ("quality-metrics", 100000),
            ("vendor-performance", 100000),
            ("work-center-utilization", 100000),
            ("work-order-costing", 100000),
        ],
    )
    def test_days_out_of_range_is_rejected(self, client: TestClient, auth_headers: dict, endpoint: str, days: int):
        response = client.get(f"/api/v1/reports/{endpoint}", headers=auth_headers, params={"days": days})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.parametrize(
        "endpoint,params",
        [
            ("vendor-performance", {"skip": -1}),
            ("vendor-performance", {"limit": 0}),
            ("vendor-performance", {"limit": 501}),
            ("work-center-utilization", {"skip": -1}),
            ("work-center-utilization", {"limit": 0}),
            ("work-center-utilization", {"limit": 501}),
        ],
    )
    def test_pagination_bounds_are_enforced(self, client: TestClient, auth_headers: dict, endpoint: str, params: dict):
        response = client.get(f"/api/v1/reports/{endpoint}", headers=auth_headers, params=params)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_days_at_the_ceiling_is_accepted(self, client: TestClient, auth_headers: dict):
        """365 is inside the bound -- the cap must not break a legitimate year."""
        response = client.get("/api/v1/reports/production-summary", headers=auth_headers, params={"days": 365})
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.unit
def test_part_model_has_no_unit_cost_column():
    """Regression guard for the costing-report 500.

    The costing report used to read ``part.unit_cost`` -- a column that never
    existed on ``Part`` -- which raised AttributeError -> HTTP 500. The Part
    cost model is {standard_cost, material_cost, labor_cost, overhead_cost}.
    A future edit that reintroduces ``part.unit_cost`` should fail here.
    """
    assert not hasattr(Part, "unit_cost")
    cost_columns = {c.name for c in Part.__table__.columns if c.name.endswith("_cost")}
    assert cost_columns == {"standard_cost", "material_cost", "labor_cost", "overhead_cost"}
