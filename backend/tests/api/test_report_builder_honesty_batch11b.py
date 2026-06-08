"""Behavior locks for the Batch-11B report-builder honesty changes (G3-content).

Two contracts:

1. ``estimated_hours`` is no longer a selectable WORK_ORDERS column. It has no writer
   anywhere (structurally 0 in every tenant), so offering it would render a phantom
   column. It is dropped from BOTH ``report_builder.FIELD_MAPPINGS['work_orders']`` AND
   the ``/analytics/data-sources`` catalog.

2. Flag-OFF labor honesty. When ``LABOR_COST_ROLLUP_ENABLED`` is OFF (default) and a
   WORK_ORDERS report selects a labor-derived column (``actual_cost`` / ``actual_hours``
   / ``estimated_cost``), those columns render a literal 0 that means "not tracked", not
   a measured zero. ``ReportBuilderService.labor_tracking_note`` returns annotation
   metadata, and ``POST /analytics/custom-report`` surfaces it on the
   ``X-Report-Labor-Not-Tracked`` / ``X-Report-Labor-Note`` response headers WITHOUT
   changing the bare-list body. With no labor-derived column selected (or the flag ON),
   no such header is set and the note is ``None``.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.schemas.analytics import CustomReportRequest, ReportColumn, ReportDataSource
from app.services.report_builder import FIELD_MAPPINGS, ReportBuilderService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
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


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"b11b-g3-{n}@co{company_id}.test",
        employee_id=f"B11BG3-{n:05d}",
        first_name="B11B",
        last_name="G3",
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


def make_work_order(db: Session, *, company_id: int = COMPANY_A) -> WorkOrder:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"B11BG3-P-{n}",
        name=f"Part {n}",
        description="batch11b G3 fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"B11BG3-WO-{n:05d}",
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


# ===========================================================================
# estimated_hours is no longer a selectable WORK_ORDERS column
# ===========================================================================


def test_estimated_hours_not_in_field_mappings():
    """``estimated_hours`` is removed from report_builder.FIELD_MAPPINGS['work_orders']."""
    assert (
        "estimated_hours" not in FIELD_MAPPINGS["work_orders"]
    ), "estimated_hours has no writer (always 0); it must NOT be a selectable report column"
    # Sanity: the genuinely-populated columns are still present.
    assert "actual_hours" in FIELD_MAPPINGS["work_orders"]
    assert "actual_cost" in FIELD_MAPPINGS["work_orders"]


def test_estimated_hours_not_in_data_sources_catalog(client: TestClient, db_session: Session):
    """``estimated_hours`` is removed from the /analytics/data-sources WORK_ORDERS catalog
    so the report builder UI never offers it."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    resp = client.get("/api/v1/analytics/data-sources", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    wo_fields = {f["name"] for f in resp.json()["work_orders"]["fields"]}
    assert "estimated_hours" not in wo_fields, "estimated_hours must not be in the data-sources catalog"
    # The retained labor-derived columns are still offered.
    assert "actual_hours" in wo_fields
    assert "actual_cost" in wo_fields


# ===========================================================================
# Service-level: labor_tracking_note
# ===========================================================================


def _wo_request(fields: list[str]) -> CustomReportRequest:
    return CustomReportRequest(
        data_source=ReportDataSource.WORK_ORDERS,
        columns=[ReportColumn(field=f) for f in fields],
    )


def test_labor_tracking_note_flag_off_with_labor_column(db_session: Session, monkeypatch):
    """Flag OFF + a labor-derived column selected -> note returned naming the not-tracked
    fields."""
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", False)
    service = ReportBuilderService(db_session)
    note = service.labor_tracking_note(_wo_request(["work_order_number", "actual_cost"]), COMPANY_A)
    assert note is not None
    assert note["labor_cost_rollup_enabled"] is False
    assert note["not_tracked_fields"] == ["actual_cost"]
    assert "not tracked" in note["note"].lower()


def test_labor_tracking_note_none_without_labor_column(db_session: Session, monkeypatch):
    """Flag OFF but NO labor-derived column selected -> no note (bare-list contract
    otherwise unchanged)."""
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", False)
    service = ReportBuilderService(db_session)
    note = service.labor_tracking_note(_wo_request(["work_order_number", "status"]), COMPANY_A)
    assert note is None


def test_labor_tracking_note_none_when_flag_on(db_session: Session, monkeypatch):
    """Flag ON -> no note even with a labor-derived column (the columns are real now)."""
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", True)
    service = ReportBuilderService(db_session)
    note = service.labor_tracking_note(_wo_request(["work_order_number", "actual_cost"]), COMPANY_A)
    assert note is None


def test_labor_tracking_note_none_for_non_work_order_source(db_session: Session, monkeypatch):
    """Non-WORK_ORDERS data source -> no note (the labor-derived columns are WO-only)."""
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", False)
    service = ReportBuilderService(db_session)
    req = CustomReportRequest(
        data_source=ReportDataSource.PARTS,
        columns=[ReportColumn(field="part_number"), ReportColumn(field="standard_cost")],
    )
    note = service.labor_tracking_note(req, COMPANY_A)
    assert note is None


# ===========================================================================
# Endpoint: X-Report-Labor-* headers
# ===========================================================================


def test_custom_report_sets_labor_headers_flag_off_with_labor_column(
    client: TestClient, db_session: Session, monkeypatch
):
    """Flag OFF + a labor-derived column -> /custom-report sets BOTH
    ``X-Report-Labor-Not-Tracked`` (a JSON list of the not-tracked fields) and
    ``X-Report-Labor-Note`` (human-readable), without altering the bare-list body."""
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", False)
    manager = make_user(db_session, role=UserRole.MANAGER)
    make_work_order(db_session)

    resp = client.post(
        "/api/v1/analytics/custom-report",
        headers=headers_for(manager),
        json={
            "data_source": "work_orders",
            "columns": [{"field": "work_order_number"}, {"field": "actual_cost"}, {"field": "actual_hours"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "X-Report-Labor-Not-Tracked" in resp.headers, "missing not-tracked header flag-OFF"
    assert "X-Report-Labor-Note" in resp.headers, "missing human-readable note header flag-OFF"
    import json

    not_tracked = json.loads(resp.headers["X-Report-Labor-Not-Tracked"])
    assert set(not_tracked) == {"actual_cost", "actual_hours"}
    # Body is still the bare list of row dicts (header-only annotation; contract unchanged).
    assert isinstance(resp.json(), list)


def test_custom_report_no_labor_header_without_labor_column(client: TestClient, db_session: Session, monkeypatch):
    """Flag OFF but NO labor-derived column -> no labor headers."""
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", False)
    manager = make_user(db_session, role=UserRole.MANAGER)
    make_work_order(db_session)

    resp = client.post(
        "/api/v1/analytics/custom-report",
        headers=headers_for(manager),
        json={
            "data_source": "work_orders",
            "columns": [{"field": "work_order_number"}, {"field": "status"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "X-Report-Labor-Not-Tracked" not in resp.headers
    assert "X-Report-Labor-Note" not in resp.headers


def test_custom_report_no_labor_header_when_flag_on(client: TestClient, db_session: Session, monkeypatch):
    """Flag ON -> no labor headers even with a labor-derived column."""
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", True)
    manager = make_user(db_session, role=UserRole.MANAGER)
    make_work_order(db_session)

    resp = client.post(
        "/api/v1/analytics/custom-report",
        headers=headers_for(manager),
        json={
            "data_source": "work_orders",
            "columns": [{"field": "work_order_number"}, {"field": "actual_cost"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "X-Report-Labor-Not-Tracked" not in resp.headers
    assert "X-Report-Labor-Note" not in resp.headers
