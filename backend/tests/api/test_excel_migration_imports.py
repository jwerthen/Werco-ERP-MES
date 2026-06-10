"""API tests for the A0.2 Excel migration kit.

Covers:
* XLSX acceptance + dry-run on the existing master-data imports (parts shown
  as the representative; all six share the same shared-parser path),
* the new open work-order import (happy path, paper-completed operation
  seeding without fabricated labor evidence, row errors, tenant isolation,
  dry-run zero-write guarantee, RBAC),
* the new open purchase-order import (grouped lines, sent status, audit and
  operational events, dry-run, row errors).
"""

import io
from datetime import date, timedelta
from io import BytesIO

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.customer import Customer
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.routing import Routing, RoutingOperation
from app.models.time_entry import TimeEntry
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus


def _csv_file(text: str):
    return {"file": ("import.csv", BytesIO(text.encode("utf-8")), "text/csv")}


def _xlsx_file(rows, filename="import.xlsx"):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    out.seek(0)
    return {"file": (filename, out, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}


def _make_routed_part(db: Session, part_number: str, sequences=(10, 20, 30)) -> Part:
    """Create a manufactured part with a released, active routing."""
    work_center = WorkCenter(
        code=f"WC-{part_number}",
        name=f"WC for {part_number}",
        work_center_type="machining",
        is_active=True,
        company_id=1,
    )
    db.add(work_center)
    db.flush()
    part = Part(
        part_number=part_number,
        name=f"Part {part_number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(part)
    db.flush()
    routing = Routing(part_id=part.id, status="released", is_active=True, company_id=1)
    db.add(routing)
    db.flush()
    for seq in sequences:
        db.add(
            RoutingOperation(
                routing_id=routing.id,
                sequence=seq,
                operation_number=f"Op {seq}",
                name=f"Operation {seq}",
                work_center_id=work_center.id,
                run_hours_per_unit=0.1,
                is_active=True,
                company_id=1,
            )
        )
    db.commit()
    db.refresh(part)
    return part


@pytest.mark.api
@pytest.mark.requires_db
class TestXlsxAcceptanceOnExistingImports:
    def test_parts_import_accepts_xlsx_with_typed_cells_and_guidance_row(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        response = client.post(
            "/api/v1/parts/import-csv",
            headers=auth_headers,
            files=_xlsx_file(
                [
                    ["Part Number", "Name", "Part Type", "Lead Time Days", "Standard Cost"],
                    ["# REQUIRED", "# REQUIRED", "# manufactured or assembly", "# Optional", "# Optional"],
                    ["xl-001", "Excel Part", "manufactured", 10.0, 12.5],
                ]
            ),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["imported_count"] == 1
        assert body["errors"] == []
        part = db_session.query(Part).filter_by(part_number="XL-001", company_id=1).one()
        assert part.lead_time_days == 10
        assert part.standard_cost == 12.5
        audit = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "part", AuditLog.resource_id == part.id, AuditLog.action == "CREATE")
            .all()
        )
        assert len(audit) == 1

    def test_parts_import_dry_run_writes_nothing(self, client: TestClient, auth_headers: dict, db_session: Session):
        before_audit = db_session.query(AuditLog).count()
        response = client.post(
            "/api/v1/parts/import-csv?dry_run=true",
            headers=auth_headers,
            files=_csv_file("part_number,name,part_type\nDRY-001,Dry Part,manufactured\nDRY-001,Dupe,manufactured\n"),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["dry_run"] is True
        assert body["imported_count"] == 1
        assert body["created_ids"] == []
        # In-file duplicate detection still works in dry-run.
        assert len(body["errors"]) == 1 and body["errors"][0]["row"] == 3
        assert db_session.query(Part).filter(Part.part_number.like("DRY-%")).count() == 0
        assert db_session.query(AuditLog).count() == before_audit

    def test_users_import_xlsx_and_dry_run(self, client: TestClient, admin_headers: dict, db_session: Session):
        from app.models.user import User

        rows = [
            ["Employee ID", "First Name", "Last Name", "Role"],
            ["EMP-X1", "Exa", "Lopez", "operator"],
        ]
        dry = client.post("/api/v1/users/import-csv?dry_run=true", headers=admin_headers, files=_xlsx_file(rows))
        assert dry.status_code == status.HTTP_200_OK
        assert dry.json()["created_count"] == 1
        assert dry.json()["created_ids"] == []
        assert db_session.query(User).filter_by(employee_id="EMP-X1").count() == 0

        commit = client.post("/api/v1/users/import-csv", headers=admin_headers, files=_xlsx_file(rows))
        assert commit.status_code == status.HTTP_200_OK
        assert commit.json()["created_count"] == 1
        assert db_session.query(User).filter_by(employee_id="EMP-X1", company_id=1).count() == 1

    def test_legacy_csv_still_works_on_all_entities(self, client: TestClient, auth_headers: dict):
        """Backward compatibility: the original CSV payloads keep working."""
        vendor = client.post(
            "/api/v1/purchasing/vendors/import-csv",
            headers=auth_headers,
            files=_csv_file("code,name,is_approved\nVXL1,Excel Vendor,true\n"),
        )
        customer = client.post(
            "/api/v1/customers/import-csv",
            headers=auth_headers,
            files=_csv_file("code,name\nCXL1,Excel Customer\n"),
        )
        work_center = client.post(
            "/api/v1/work-centers/import-csv",
            headers=auth_headers,
            files=_csv_file("code,name,work_center_type\nWCXL1,Excel WC,fabrication\n"),
        )
        material = client.post(
            "/api/v1/materials/import-csv",
            headers=auth_headers,
            files=_csv_file("part_number,name,part_type\nMXL-1,Excel Material,raw_material\n"),
        )
        for response in (vendor, customer, work_center, material):
            assert response.status_code == status.HTTP_200_OK
            assert response.json()["imported_count"] == 1

    def test_wrong_file_type_rejected(self, client: TestClient, auth_headers: dict):
        response = client.post(
            "/api/v1/parts/import-csv",
            headers=auth_headers,
            files={"file": ("parts.pdf", BytesIO(b"%PDF"), "application/pdf")},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.api
@pytest.mark.requires_db
class TestOpenWorkOrderImport:
    def test_happy_path_released_with_ready_first_op(self, client: TestClient, auth_headers: dict, db_session: Session):
        part = _make_routed_part(db_session, "WOIMP-100")
        customer = Customer(name="Borealis Defense", code="BOR001", is_active=True, company_id=1)
        db_session.add(customer)
        db_session.commit()

        overdue = (date.today() - timedelta(days=30)).isoformat()  # past due dates must be allowed
        response = client.post(
            "/api/v1/work-orders/import",
            headers=auth_headers,
            files=_csv_file(
                "wo_number,part_number,quantity,due_date,customer,customer_po,priority\n"
                f"WO-LEG-1,WOIMP-100,25,{overdue},bor001,PO-9912,3\n"
            ),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["created_count"] == 1 and body["errors"] == []
        result = body["results"][0]
        assert result["wo_number"] == "WO-LEG-1"
        assert result["status"] == "released"
        assert result["operation_count"] == 3
        assert result["next_operation_sequence"] == 10

        wo = db_session.query(WorkOrder).filter_by(work_order_number="WO-LEG-1", company_id=1).one()
        assert wo.status == WorkOrderStatus.RELEASED
        assert wo.customer_name == "Borealis Defense"  # canonical name resolved from code
        assert wo.customer_po == "PO-9912"
        assert wo.priority == 3
        assert wo.released_by is not None and wo.released_at is not None
        ops = sorted(wo.operations, key=lambda op: op.sequence)
        assert [op.status for op in ops] == [OperationStatus.READY, OperationStatus.PENDING, OperationStatus.PENDING]
        assert wo.current_operation_id == ops[0].id

        create_audit = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo.id)
            .all()
        )
        assert {row.action for row in create_audit} == {"CREATE", "STATUS_CHANGE"}

    def test_completed_through_seq_seeds_paper_progress_without_fabricated_evidence(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        _make_routed_part(db_session, "WOIMP-200")
        response = client.post(
            "/api/v1/work-orders/import",
            headers=auth_headers,
            files=_csv_file("wo_number,part_number,quantity,completed_through_seq\nWO-LEG-2,WOIMP-200,10,20\n"),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["created_count"] == 1 and body["errors"] == []
        assert body["results"][0]["completed_operation_count"] == 2
        assert body["results"][0]["next_operation_sequence"] == 30
        assert body["results"][0]["status"] == "in_progress"

        wo = db_session.query(WorkOrder).filter_by(work_order_number="WO-LEG-2", company_id=1).one()
        assert wo.status == WorkOrderStatus.IN_PROGRESS
        ops = sorted(wo.operations, key=lambda op: op.sequence)
        assert [op.status for op in ops] == [
            OperationStatus.COMPLETE,
            OperationStatus.COMPLETE,
            OperationStatus.READY,
        ]
        # Honest provenance: paper-completed ops carry quantity but NO fabricated
        # timestamps, operators, or labor evidence.
        for op in ops[:2]:
            assert float(op.quantity_complete) == 10.0
            assert op.actual_start is None and op.actual_end is None
            assert op.started_by is None and op.completed_by is None
        assert wo.current_operation_id == ops[2].id
        op_ids = [op.id for op in ops]
        assert db_session.query(TimeEntry).filter(TimeEntry.operation_id.in_(op_ids)).count() == 0

        events = (
            db_session.query(OperationalEvent)
            .filter(
                OperationalEvent.event_type == "operation_completed",
                OperationalEvent.work_order_id == wo.id,
            )
            .all()
        )
        assert len(events) == 2
        assert all(event.event_payload.get("source") == "import" for event in events)
        assert all(event.source_module == "import" for event in events)

    def test_row_errors(self, client: TestClient, auth_headers: dict, db_session: Session):
        _make_routed_part(db_session, "WOIMP-300")
        before = db_session.query(WorkOrder).count()
        response = client.post(
            "/api/v1/work-orders/import",
            headers=auth_headers,
            files=_csv_file(
                "wo_number,part_number,quantity,completed_through_seq\n"
                "WO-E1,NOPE-1,5,\n"  # unknown part
                "WO-E2,WOIMP-300,zero,\n"  # bad quantity
                "WO-E3,WOIMP-300,5,30\n"  # paper-complete through the LAST op -> not open
                "WO-E4,WOIMP-300,5,\n"  # valid row still imports
                "WO-E4,WOIMP-300,5,\n"  # duplicate number within the file
            ),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["created_count"] == 1
        reasons = {error["row"]: error["reason"] for error in body["errors"]}
        assert "not found" in reasons[2]
        assert "quantity" in reasons[3]
        assert "OPEN work orders" in reasons[4]
        assert "more than once" in reasons[6]
        assert db_session.query(WorkOrder).count() == before + 1

    def test_part_without_released_routing_is_rejected(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part = Part(
            part_number="NOROUTE-1",
            name="No routing",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add(part)
        db_session.commit()
        response = client.post(
            "/api/v1/work-orders/import",
            headers=auth_headers,
            files=_csv_file("part_number,quantity\nNOROUTE-1,5\n"),
        )
        body = response.json()
        assert body["created_count"] == 0
        assert "released routing" in body["errors"][0]["reason"]

    def test_tenant_isolation_other_company_part_not_visible(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        other = Company(id=2, name="Other Co", slug="other", is_active=True)
        db_session.add(other)
        db_session.flush()
        foreign_part = Part(
            part_number="FOREIGN-1",
            name="Foreign part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=2,
        )
        db_session.add(foreign_part)
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/import",
            headers=auth_headers,  # company 1 token
            files=_csv_file("part_number,quantity\nFOREIGN-1,5\n"),
        )
        body = response.json()
        assert body["created_count"] == 0
        assert "not found" in body["errors"][0]["reason"]
        assert db_session.query(WorkOrder).filter_by(company_id=2).count() == 0

    def test_soft_deleted_part_not_matched(self, client: TestClient, auth_headers: dict, db_session: Session):
        part = _make_routed_part(db_session, "WOIMP-DEL")
        part.soft_delete(1)
        db_session.commit()
        response = client.post(
            "/api/v1/work-orders/import",
            headers=auth_headers,
            files=_csv_file("part_number,quantity\nWOIMP-DEL,5\n"),
        )
        assert "not found" in response.json()["errors"][0]["reason"]

    def test_dry_run_validates_everything_and_writes_nothing(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        _make_routed_part(db_session, "WOIMP-400")
        before_wo = db_session.query(WorkOrder).count()
        before_audit = db_session.query(AuditLog).count()
        before_events = db_session.query(OperationalEvent).count()

        response = client.post(
            "/api/v1/work-orders/import?dry_run=true",
            headers=auth_headers,
            files=_xlsx_file(
                [
                    ["wo_number", "part_number", "quantity", "completed_through_seq"],
                    ["", "WOIMP-400", 10.0, 20],
                    ["", "MISSING-PART", 5, ""],
                ]
            ),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["dry_run"] is True
        assert body["created_count"] == 1
        assert body["created_ids"] == []
        # Preview still exercises full routing expansion inside the savepoint.
        assert body["results"][0]["operation_count"] == 3
        assert body["results"][0]["completed_operation_count"] == 2
        assert body["results"][0]["wo_number"] is None  # number only assigned at commit
        assert len(body["errors"]) == 1

        assert db_session.query(WorkOrder).count() == before_wo
        assert db_session.query(AuditLog).count() == before_audit
        assert db_session.query(OperationalEvent).count() == before_events

    def test_rbac_operator_forbidden(self, client: TestClient, operator_headers: dict):
        response = client.post(
            "/api/v1/work-orders/import",
            headers=operator_headers,
            files=_csv_file("part_number,quantity\nX,1\n"),
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.api
@pytest.mark.requires_db
class TestOpenPurchaseOrderImport:
    @pytest.fixture
    def vendor_and_parts(self, db_session: Session):
        vendor = Vendor(code="APX001", name="Apex Metals", is_active=True, company_id=1)
        db_session.add(vendor)
        for part_number in ("RM-1", "RM-2"):
            db_session.add(
                Part(
                    part_number=part_number,
                    name=f"Material {part_number}",
                    part_type="raw_material",
                    unit_of_measure="each",
                    is_active=True,
                    company_id=1,
                )
            )
        db_session.commit()
        return vendor

    def test_grouped_lines_create_one_sent_po(
        self, client: TestClient, auth_headers: dict, db_session: Session, vendor_and_parts
    ):
        response = client.post(
            "/api/v1/purchasing/purchase-orders/import",
            headers=auth_headers,
            files=_csv_file(
                "po_number,vendor_code,part_number,quantity,unit_price,promised_date\n"
                "PO-LEG-1,APX001,RM-1,500,3.10,2026-06-20\n"
                "PO-LEG-1,APX001,RM-2,200,2.00,2026-06-25\n"
            ),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["created_count"] == 1
        assert body["created_line_count"] == 2
        assert body["errors"] == []
        assert body["results"][0]["status"] == "sent"

        po = db_session.query(PurchaseOrder).filter_by(po_number="PO-LEG-1", company_id=1).one()
        assert po.status == POStatus.SENT
        assert po.order_date is None  # unknown pre-migration date is NOT fabricated
        assert po.expected_date == date(2026, 6, 25)
        assert po.total == pytest.approx(500 * 3.10 + 200 * 2.00)
        lines = (
            db_session.query(PurchaseOrderLine)
            .filter_by(purchase_order_id=po.id)
            .order_by(PurchaseOrderLine.line_number)
            .all()
        )
        assert len(lines) == 2
        assert lines[0].required_date == date(2026, 6, 20)
        assert all(line.company_id == 1 for line in lines)

        audit = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "purchase_order",
                AuditLog.resource_id == po.id,
                AuditLog.action == "CREATE",
            )
            .all()
        )
        assert len(audit) == 1
        events = (
            db_session.query(OperationalEvent)
            .filter(
                OperationalEvent.event_type == "purchase_order_created",
                OperationalEvent.entity_id == po.id,
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].event_payload.get("source") == "import"

    def test_row_and_group_errors(self, client: TestClient, auth_headers: dict, db_session: Session, vendor_and_parts):
        before = db_session.query(PurchaseOrder).count()
        response = client.post(
            "/api/v1/purchasing/purchase-orders/import",
            headers=auth_headers,
            files=_csv_file(
                "po_number,vendor_code,part_number,quantity,unit_price\n"
                "PO-G1,APX001,RM-1,10,1.00\n"
                "PO-G1,APX001,RM-2,oops,1.00\n"  # breaks the whole PO-G1 group
                "PO-G2,NOVENDOR,RM-1,10,1.00\n"  # unknown vendor
                ",APX001,RM-1,5,2.00\n"  # blank po_number -> own single-line PO
            ),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["created_count"] == 1  # only the blank-number single-line PO
        rows_with_errors = {error["row"] for error in body["errors"]}
        assert rows_with_errors == {2, 3, 4}
        assert db_session.query(PurchaseOrder).count() == before + 1

    def test_dry_run_writes_nothing(
        self, client: TestClient, auth_headers: dict, db_session: Session, vendor_and_parts
    ):
        before_po = db_session.query(PurchaseOrder).count()
        before_audit = db_session.query(AuditLog).count()
        response = client.post(
            "/api/v1/purchasing/purchase-orders/import?dry_run=true",
            headers=auth_headers,
            files=_xlsx_file(
                [
                    ["po_number", "vendor_code", "part_number", "quantity", "unit_price", "promised_date"],
                    ["", "APX001", "RM-1", 500.0, 3.1, "2026-06-20"],
                ]
            ),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["dry_run"] is True
        assert body["created_count"] == 1
        assert body["created_ids"] == []
        assert body["results"][0]["po_number"] is None
        assert db_session.query(PurchaseOrder).count() == before_po
        assert db_session.query(AuditLog).count() == before_audit

    def test_existing_po_number_rejected(
        self, client: TestClient, auth_headers: dict, db_session: Session, vendor_and_parts
    ):
        po = PurchaseOrder(po_number="PO-DUPE", vendor_id=vendor_and_parts.id, company_id=1)
        db_session.add(po)
        db_session.commit()
        response = client.post(
            "/api/v1/purchasing/purchase-orders/import",
            headers=auth_headers,
            files=_csv_file("po_number,vendor_code,part_number,quantity,unit_price\nPO-DUPE,APX001,RM-1,10,1.00\n"),
        )
        assert "already exists" in response.json()["errors"][0]["reason"]
