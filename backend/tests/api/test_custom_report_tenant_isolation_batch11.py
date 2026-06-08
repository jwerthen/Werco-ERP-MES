"""Tenant-isolation coverage for the custom-report builder (Batch 11A / G3-scope).

``ReportBuilderService.execute_report`` runs a dynamic ``db.query(*columns)`` over
a single base model chosen by ``data_source``. Before the G3-scope fix the query
applied only the user-supplied filters -- no company scope -- so a custom report
(or its CSV export) returned EVERY tenant's rows. Every supported base model
(WorkOrder, Part, InventoryItem, NonConformanceReport, PurchaseOrder, Quote)
carries ``company_id`` via ``TenantMixin``; the fix always applies
``tenant_filter(query, model, company_id)`` before any user filters.

Coverage:
- service ``execute_report(request, company_id)`` over WORK_ORDERS returns ONLY
  the passed company's rows (direct service call -- the tightest assertion).
- ``POST /api/v1/analytics/custom-report`` with rows in two companies returns
  only the caller's company rows (WORK_ORDERS and PARTS sources).
- ``GET /api/v1/analytics/custom-report/export`` (CSV) of a company-A template
  emits only company-A rows in the CSV body.
- a second-tenant caller running the SAME report sees only ITS rows (symmetry --
  proves the scope follows the active company, not a hard-coded id).

Rows for both companies are created directly in the shared ``db_session``
(tests/conftest.py); requests use a directly-minted token for the relevant
company. ``/custom-report`` requires ADMIN/MANAGER (require_role), so callers are
minted as MANAGER.
"""

import csv
import io
from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.analytics import ReportTemplate
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.schemas.analytics import (
    CustomReportRequest,
    ReportColumn,
    ReportDataSource,
)
from app.services.report_builder import ReportBuilderService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, company_id: int, role: UserRole = UserRole.MANAGER) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"crpt-{n}@co{company_id}.test",
        employee_id=f"CRPT-{n:05d}",
        first_name="CRpt",
        last_name=f"C{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session, *, company_id: int, part_number: str) -> Part:
    _ensure_company(db, company_id)
    part = Part(
        part_number=part_number,
        name=f"Part {part_number}",
        description="custom-report fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_order(db: Session, *, company_id: int, wo_number: str) -> WorkOrder:
    part = make_part(db, company_id=company_id, part_number=f"{wo_number}-P")
    wo = WorkOrder(
        work_order_number=wo_number,
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _wo_report_request() -> CustomReportRequest:
    return CustomReportRequest(
        data_source=ReportDataSource.WORK_ORDERS,
        columns=[ReportColumn(field="work_order_number"), ReportColumn(field="status")],
    )


# ---------------------------------------------------------------------------
# Service-level: execute_report scopes to the passed company.
# ---------------------------------------------------------------------------


def test_execute_report_returns_only_passed_company_rows(db_session: Session):
    """Direct service call: a WORK_ORDERS report for company A must contain A's WO
    number and NOT company B's."""
    wo_a = make_work_order(db_session, company_id=COMPANY_A, wo_number="CRPT-A-0001")
    wo_b = make_work_order(db_session, company_id=COMPANY_B, wo_number="CRPT-B-0001")

    service = ReportBuilderService(db_session)
    rows = service.execute_report(_wo_report_request(), COMPANY_A)

    numbers = {r["work_order_number"] for r in rows}
    assert wo_a.work_order_number in numbers, "company A's WO must appear in its own report"
    assert wo_b.work_order_number not in numbers, "company B's WO must NOT leak into company A's report"


def test_execute_report_is_symmetric_per_company(db_session: Session):
    """The same report run for company B returns ONLY B's rows -- the scope follows
    the passed company, not a hard-coded tenant."""
    wo_a = make_work_order(db_session, company_id=COMPANY_A, wo_number="CRPT-A-0002")
    wo_b = make_work_order(db_session, company_id=COMPANY_B, wo_number="CRPT-B-0002")

    service = ReportBuilderService(db_session)
    rows_b = service.execute_report(_wo_report_request(), COMPANY_B)

    numbers = {r["work_order_number"] for r in rows_b}
    assert wo_b.work_order_number in numbers
    assert wo_a.work_order_number not in numbers


# ---------------------------------------------------------------------------
# Endpoint: POST /custom-report scopes to the caller's active company.
# ---------------------------------------------------------------------------


def test_custom_report_endpoint_excludes_other_tenant_work_orders(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    wo_a = make_work_order(db_session, company_id=COMPANY_A, wo_number="CRPT-A-0003")
    wo_b = make_work_order(db_session, company_id=COMPANY_B, wo_number="CRPT-B-0003")

    resp = client.post(
        "/api/v1/analytics/custom-report",
        headers=headers_for(a_user),
        json={
            "data_source": "work_orders",
            "columns": [{"field": "work_order_number"}, {"field": "status"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    numbers = {row["work_order_number"] for row in resp.json()}
    assert wo_a.work_order_number in numbers, "caller's own WO must be present"
    assert wo_b.work_order_number not in numbers, "another tenant's WO must NOT be in the report"


def test_custom_report_endpoint_excludes_other_tenant_parts(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    part_a = make_part(db_session, company_id=COMPANY_A, part_number="CRPT-PA-0001")
    part_b = make_part(db_session, company_id=COMPANY_B, part_number="CRPT-PB-0001")

    resp = client.post(
        "/api/v1/analytics/custom-report",
        headers=headers_for(a_user),
        json={
            "data_source": "parts",
            "columns": [{"field": "part_number"}, {"field": "name"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    part_numbers = {row["part_number"] for row in resp.json()}
    assert part_a.part_number in part_numbers
    assert part_b.part_number not in part_numbers


def test_custom_report_endpoint_symmetric_for_second_tenant(client: TestClient, db_session: Session):
    """A company-B manager running the same report sees only B's rows."""
    b_user = make_user(db_session, company_id=COMPANY_B)
    wo_a = make_work_order(db_session, company_id=COMPANY_A, wo_number="CRPT-A-0004")
    wo_b = make_work_order(db_session, company_id=COMPANY_B, wo_number="CRPT-B-0004")

    resp = client.post(
        "/api/v1/analytics/custom-report",
        headers=headers_for(b_user),
        json={
            "data_source": "work_orders",
            "columns": [{"field": "work_order_number"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    numbers = {row["work_order_number"] for row in resp.json()}
    assert wo_b.work_order_number in numbers
    assert wo_a.work_order_number not in numbers


# ---------------------------------------------------------------------------
# Endpoint: GET /custom-report/export (CSV) scopes data to the caller's company.
# ---------------------------------------------------------------------------


def _make_wo_template(db: Session, *, company_id: int, created_by: int) -> ReportTemplate:
    _ensure_company(db, company_id)
    n = _next()
    tmpl = ReportTemplate(
        name=f"CRPT WO Export {n}",
        description="custom-report export fixture",
        data_source=ReportDataSource.WORK_ORDERS.value,
        columns=[{"field": "work_order_number"}, {"field": "status"}],
        filters=[],
        group_by=[],
        sort=[],
        is_shared=False,
        created_by=created_by,
        company_id=company_id,
    )
    db.add(tmpl)
    db.commit()
    db.refresh(tmpl)
    return tmpl


def test_custom_report_export_csv_excludes_other_tenant_rows(client: TestClient, db_session: Session):
    """The CSV export of a company-A template contains company-A's WO number but not
    company-B's -- the export data query is tenant-scoped too."""
    a_user = make_user(db_session, company_id=COMPANY_A)
    wo_a = make_work_order(db_session, company_id=COMPANY_A, wo_number="CRPT-A-0005")
    wo_b = make_work_order(db_session, company_id=COMPANY_B, wo_number="CRPT-B-0005")
    template = _make_wo_template(db_session, company_id=COMPANY_A, created_by=a_user.id)

    resp = client.get(
        f"/api/v1/analytics/custom-report/export?template_id={template.id}&format=csv",
        headers=headers_for(a_user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.text

    reader = csv.DictReader(io.StringIO(body))
    numbers = {row["work_order_number"] for row in reader}
    assert wo_a.work_order_number in numbers, "company A's WO must be in its own export"
    assert wo_b.work_order_number not in numbers, "company B's WO must NOT be in company A's export"
