"""Contract for the custom report builder screen (POST /analytics/custom-report, the
template CRUD + run verbs, the CSV export, and GET /analytics/data-sources).

What this file pins, and why each piece exists:

* ONE catalog. ``report_builder.REPORT_SOURCES`` derives both ``FIELD_MAPPINGS`` and the
  ``/data-sources`` body, and each field's type is derived from its SQLAlchemy column. The
  old hand-written catalog advertised two fields the engine silently dropped and hid
  sixteen it supported.
* Soft delete (invariant 3): a deleted work order / part / NCR / PO never appears; a
  deleted VENDOR still names its PO (a historical record keeps naming who it named).
* Wrong input is a 422, never silently ignored and never a 500.
* A stored template is judged, never trusted: it still lists and opens, but running or
  exporting a broken one is a 409 naming it, and ``definition_errors`` says why.
* Template writes are audited; the access rule for the verbs is enforced.
* The export refuses (400) rather than truncates, and sends an RFC 6266 filename.
"""

import csv
import io
from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import Date, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, Integer, Numeric, String
from sqlalchemy.orm import Session

from app.api.endpoints import analytics as analytics_endpoints
from app.core.security import create_access_token
from app.models.analytics import ReportTemplate
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quality import NonConformanceReport
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.schemas.analytics import ReportDataSource
from app.services.export_safety import attachment_content_disposition
from app.services.report_builder import FIELD_MAPPINGS, REPORT_SOURCES, data_source_catalog
from tests.api.export_audit_helpers import committed_export_rows, fresh_session

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
REPORT = "/api/v1/analytics/custom-report"
TEMPLATES = "/api/v1/analytics/custom-report/templates"
EXPORT = "/api/v1/analytics/custom-report/export"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"rbs-{n}@co{company_id}.test",
        employee_id=f"RBS-{n:05d}",
        first_name="Report",
        last_name=f"Builder{n}",
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


def make_part(db: Session, *, company_id: int = COMPANY_A, part_number: str = "") -> Part:
    _ensure_company(db, company_id)
    part = Part(
        part_number=part_number or f"RBS-P-{_next():05d}",
        name="Fixture bracket",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_order(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    number: str = "",
    quantity: float = 10,
    customer: str = "Northwind Fixtures",
    status_: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
) -> WorkOrder:
    part = make_part(db, company_id=company_id)
    wo = WorkOrder(
        work_order_number=number or f"RBS-WO-{_next():05d}",
        customer_name=customer,
        part_id=part.id,
        quantity_ordered=quantity,
        status=status_,
        priority=5,
        due_date=date(2026, 6, 1),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_vendor(db: Session, *, company_id: int = COMPANY_A, name: str = "Contoso Metals") -> Vendor:
    _ensure_company(db, company_id)
    vendor = Vendor(code=f"RBS-V-{_next():05d}", name=name, is_approved=True, company_id=company_id)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def make_po(db: Session, vendor: Vendor, *, company_id: int = COMPANY_A, number: str = "") -> PurchaseOrder:
    po = PurchaseOrder(
        po_number=number or f"RBS-PO-{_next():05d}",
        vendor_id=vendor.id,
        order_date=date(2026, 5, 1),
        company_id=company_id,
    )
    db.add(po)
    db.commit()
    db.refresh(po)
    return po


def make_ncr(db: Session, *, company_id: int = COMPANY_A, number: str = "") -> NonConformanceReport:
    _ensure_company(db, company_id)
    ncr = NonConformanceReport(
        ncr_number=number or f"RBS-NCR-{_next():05d}",
        source="in_process",
        title="Fixture finding",
        description="Fixture finding",
        quantity_affected=1,
        detected_date=date(2026, 5, 2),
        company_id=company_id,
    )
    db.add(ncr)
    db.commit()
    db.refresh(ncr)
    return ncr


def make_inventory(db: Session, part: Part, *, company_id: int = COMPANY_A, lot: str = "") -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location="A-01",
        quantity_on_hand=4,
        quantity_available=4,
        lot_number=lot or f"RBS-LOT-{_next():05d}",
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def make_template(db: Session, owner: User, *, shared: bool = False, **definition) -> ReportTemplate:
    """Insert a template ROW directly (as a legacy/stored template would exist)."""
    template = ReportTemplate(
        name=definition.pop("name", f"RBS Template {_next()}"),
        description=None,
        data_source=definition.pop("data_source", "work_orders"),
        columns=definition.pop("columns", [{"field": "work_order_number"}, {"field": "status"}]),
        filters=definition.pop("filters", []),
        group_by=definition.pop("group_by", []),
        sort=definition.pop("sort", []),
        is_shared=shared,
        created_by=owner.id,
        company_id=owner.company_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def run(client: TestClient, user: User, body: dict):
    return client.post(REPORT, headers=headers_for(user), json=body)


def wo_numbers(resp) -> set:
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {row["work_order_number"] for row in resp.json()["rows"]}


def committed_template_audit_rows(action: str):
    with fresh_session() as verify:
        rows = (
            verify.query(AuditLog)
            .filter(AuditLog.resource_type == "report_template", AuditLog.action == action)
            .order_by(AuditLog.id)
            .all()
        )
        for row in rows:
            _ = (row.resource_id, row.resource_identifier, row.old_values, row.new_values, row.user_id, row.company_id)
        verify.expunge_all()
        return rows


# ===========================================================================
# One catalog: parity, derived types, the /data-sources body
# ===========================================================================


def test_catalog_and_field_mappings_have_identical_keys_per_source():
    catalog = data_source_catalog()
    assert set(catalog) == {source.value for source in FIELD_MAPPINGS}
    for source, mapped in FIELD_MAPPINGS.items():
        advertised = [field["name"] for field in catalog[source.value]["fields"]]
        assert len(advertised) == len(set(advertised)), f"{source.value}: duplicate field names"
        assert set(advertised) == set(mapped), f"{source.value}: catalog and engine disagree"


def test_production_is_not_a_catalog_source_but_stays_an_enum_member():
    assert "production" not in data_source_catalog()
    assert ReportDataSource.PRODUCTION not in REPORT_SOURCES
    assert ReportDataSource("production") is ReportDataSource.PRODUCTION


def test_previously_broken_fields_are_now_real():
    assert "part_number" in FIELD_MAPPINGS["inventory"]
    assert "vendor_name" in FIELD_MAPPINGS["purchasing"]


_EXPECTED_SA_TYPES = {
    "enum": (SAEnum,),
    "datetime": (DateTime,),
    "date": (Date,),
    "number": (Integer, Float, Numeric),
    "string": (String,),
}


def test_every_declared_type_matches_its_column():
    for source in REPORT_SOURCES.values():
        for spec in source.fields.values():
            sa_type = spec.column.property.columns[0].type
            assert isinstance(sa_type, _EXPECTED_SA_TYPES[spec.type]), f"{source.key.value}.{spec.name}"
            if spec.type == "enum":
                assert spec.enum_class is not None
                assert spec.options == [str(m.value) for m in sa_type.enum_class]
            else:
                assert not isinstance(sa_type, SAEnum), f"{source.key.value}.{spec.name} is an enum column"
                assert spec.options is None


def test_data_sources_endpoint_is_the_derived_catalog(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    resp = client.get("/api/v1/analytics/data-sources", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body == data_source_catalog()
    wo_fields = {f["name"]: f for f in body["work_orders"]["fields"]}
    assert wo_fields["status"]["type"] == "enum"
    assert "in_progress" in wo_fields["status"]["options"]
    assert "options" not in wo_fields["quantity_ordered"], "options only on enum fields"
    assert wo_fields["actual_start"]["type"] == "datetime"
    assert wo_fields["due_date"]["type"] == "date"


# ===========================================================================
# Soft delete (invariant 3)
# ===========================================================================


def test_deleted_work_order_is_excluded(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    live = make_work_order(db_session)
    gone = make_work_order(db_session)
    gone.soft_delete(manager.id)
    db_session.commit()
    numbers = wo_numbers(
        run(client, manager, {"data_source": "work_orders", "columns": [{"field": "work_order_number"}]})
    )
    assert live.work_order_number in numbers
    assert gone.work_order_number not in numbers


def test_deleted_part_is_excluded(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    live = make_part(db_session)
    gone = make_part(db_session)
    gone.soft_delete(manager.id)
    db_session.commit()
    resp = run(client, manager, {"data_source": "parts", "columns": [{"field": "part_number"}]})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    numbers = {row["part_number"] for row in resp.json()["rows"]}
    assert live.part_number in numbers
    assert gone.part_number not in numbers


def test_deleted_ncr_is_excluded(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    live = make_ncr(db_session)
    voided = make_ncr(db_session)
    voided.soft_delete(manager.id)
    db_session.commit()
    resp = run(client, manager, {"data_source": "quality", "columns": [{"field": "ncr_number"}]})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    numbers = {row["ncr_number"] for row in resp.json()["rows"]}
    assert live.ncr_number in numbers
    assert voided.ncr_number not in numbers


def test_deleted_po_is_excluded_but_a_deleted_vendor_still_names_its_po(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    removed_vendor = make_vendor(db_session, name="Fabrikam Plating")
    po_kept = make_po(db_session, removed_vendor)
    po_gone = make_po(db_session, removed_vendor)
    po_gone.soft_delete(manager.id)
    removed_vendor.soft_delete(manager.id)
    removed_vendor.is_active = False
    db_session.commit()

    resp = run(
        client,
        manager,
        {"data_source": "purchasing", "columns": [{"field": "po_number"}, {"field": "vendor_name"}]},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = {row["po_number"]: row for row in resp.json()["rows"]}
    assert po_gone.po_number not in rows
    assert rows[po_kept.po_number]["vendor_name"] == "Fabrikam Plating"


# ===========================================================================
# Join fields: values, and tenant scope inside the ON clause
# ===========================================================================


def test_inventory_part_number_join(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    part = make_part(db_session, part_number="RBS-JOIN-PART")
    item = make_inventory(db_session, part)
    resp = run(
        client,
        manager,
        {
            "data_source": "inventory",
            "columns": [{"field": "lot_number"}, {"field": "part_number"}],
            "filters": [{"field": "part_number", "operator": "eq", "value": "RBS-JOIN-PART"}],
            "sort": [{"field": "part_number"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["rows"] == [{"lot_number": item.lot_number, "part_number": "RBS-JOIN-PART"}]


def test_join_never_reaches_another_company(client: TestClient, db_session: Session):
    """A company-A row whose FK points at a company-B part/vendor (a cross-tenant id) joins
    NOTHING: the ON clause carries the company, so B's name never appears in A's report."""
    manager = make_user(db_session)
    foreign_part = make_part(db_session, company_id=COMPANY_B, part_number="RBS-FOREIGN-PART")
    foreign_vendor = make_vendor(db_session, company_id=COMPANY_B, name="Foreign Vendor Ltd")
    item = make_inventory(db_session, foreign_part, company_id=COMPANY_A)
    po = make_po(db_session, foreign_vendor, company_id=COMPANY_A)

    inv = run(
        client, manager, {"data_source": "inventory", "columns": [{"field": "lot_number"}, {"field": "part_number"}]}
    )
    assert inv.status_code == status.HTTP_200_OK, inv.text
    assert {"lot_number": item.lot_number, "part_number": None} in inv.json()["rows"]
    assert "RBS-FOREIGN-PART" not in inv.text

    pos = run(
        client, manager, {"data_source": "purchasing", "columns": [{"field": "po_number"}, {"field": "vendor_name"}]}
    )
    assert pos.status_code == status.HTTP_200_OK, pos.text
    assert {"po_number": po.po_number, "vendor_name": None} in pos.json()["rows"]
    assert "Foreign Vendor Ltd" not in pos.text


# ===========================================================================
# Result shape, typed filters, aggregates, truncation
# ===========================================================================


def test_result_shape_aggregates_labels_and_sort_by_output_key(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    make_work_order(db_session, customer="Alpha Gear", quantity=5)
    make_work_order(db_session, customer="Alpha Gear", quantity=7)
    make_work_order(db_session, customer="Beta Tooling", quantity=30)
    resp = run(
        client,
        manager,
        {
            "data_source": "work_orders",
            "columns": [
                {"field": "customer_name"},
                {"field": "quantity_ordered", "aggregate": "sum"},
                {"field": "work_order_number", "aggregate": "count", "alias": "jobs"},
                {"field": "due_date", "aggregate": "max"},
            ],
            "group_by": [{"field": "customer_name"}],
            "sort": [{"field": "quantity_ordered", "direction": "DESC"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["columns"] == [
        {"key": "customer_name", "label": "Customer", "type": "string"},
        {"key": "quantity_ordered", "label": "Sum of Qty Ordered", "type": "number"},
        {"key": "jobs", "label": "jobs", "type": "number"},
        {"key": "due_date", "label": "Maximum of Due Date", "type": "date"},
    ]
    assert body["rows"] == [
        {"customer_name": "Beta Tooling", "quantity_ordered": 30, "jobs": 1, "due_date": "2026-06-01"},
        {"customer_name": "Alpha Gear", "quantity_ordered": 12, "jobs": 2, "due_date": "2026-06-01"},
    ]
    assert body["row_count"] == 2
    assert body["truncated"] is False
    assert body["notes"] == []


def test_truncated_flag(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    for _ in range(3):
        make_work_order(db_session)
    body = {"data_source": "work_orders", "columns": [{"field": "work_order_number"}]}
    cut = run(client, manager, {**body, "limit": 2})
    assert cut.status_code == status.HTTP_200_OK, cut.text
    assert cut.json()["truncated"] is True
    assert cut.json()["row_count"] == 2 == len(cut.json()["rows"])
    exact = run(client, manager, {**body, "limit": 3})
    assert exact.json()["truncated"] is False
    assert exact.json()["row_count"] == 3


def test_enum_filter_binds_the_member_and_matches(client: TestClient, db_session: Session):
    """Most status columns store the member NAME; the old engine bound the bare value
    string and silently matched nothing."""
    manager = make_user(db_session)
    running = make_work_order(db_session, status_=WorkOrderStatus.IN_PROGRESS)
    draft = make_work_order(db_session, status_=WorkOrderStatus.DRAFT)
    numbers = wo_numbers(
        run(
            client,
            manager,
            {
                "data_source": "work_orders",
                "columns": [{"field": "work_order_number"}],
                "filters": [{"field": "status", "operator": "eq", "value": "in_progress"}],
            },
        )
    )
    assert numbers == {running.work_order_number}
    numbers_in = wo_numbers(
        run(
            client,
            manager,
            {
                "data_source": "work_orders",
                "columns": [{"field": "work_order_number"}],
                "filters": [{"field": "status", "operator": "in", "value": ["draft", "complete"]}],
            },
        )
    )
    assert numbers_in == {draft.work_order_number}


def test_between_with_value2_zero_is_applied(client: TestClient, db_session: Session):
    """``value2`` 0 is a real bound. The old engine's ``and f.value2`` truthiness check
    dropped the whole filter, so this returned every work order."""
    manager = make_user(db_session)
    untouched = make_work_order(db_session)
    untouched.quantity_complete = 0
    partial = make_work_order(db_session)
    partial.quantity_complete = 3
    done = make_work_order(db_session)
    done.quantity_complete = 10
    db_session.commit()
    numbers = wo_numbers(
        run(
            client,
            manager,
            {
                "data_source": "work_orders",
                "columns": [{"field": "work_order_number"}],
                "filters": [{"field": "quantity_complete", "operator": "between", "value": -1, "value2": 0}],
            },
        )
    )
    assert numbers == {untouched.work_order_number}


def test_like_escapes_wildcards(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    literal = make_work_order(db_session, customer="50% Off Fasteners")
    make_work_order(db_session, customer="500 Off Fasteners")
    underscore = make_work_order(db_session, customer="Gear_Works")
    make_work_order(db_session, customer="GearXWorks")

    def like(value: str) -> set:
        return wo_numbers(
            run(
                client,
                manager,
                {
                    "data_source": "work_orders",
                    "columns": [{"field": "work_order_number"}],
                    "filters": [{"field": "customer_name", "operator": "like", "value": value}],
                },
            )
        )

    assert like("50%") == {literal.work_order_number}
    assert like("r_w") == {underscore.work_order_number}
    assert like("FASTENERS") == like("fasteners")  # still case-insensitive


def test_eq_null_means_is_null(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    blank = make_work_order(db_session)
    blank.customer_po = None
    filled = make_work_order(db_session)
    filled.customer_po = "CPO-1"
    db_session.commit()
    base = {"data_source": "work_orders", "columns": [{"field": "work_order_number"}]}
    is_null = wo_numbers(
        run(client, manager, {**base, "filters": [{"field": "customer_po", "operator": "eq", "value": None}]})
    )
    not_null = wo_numbers(
        run(client, manager, {**base, "filters": [{"field": "customer_po", "operator": "ne", "value": None}]})
    )
    assert blank.work_order_number in is_null and filled.work_order_number not in is_null
    assert filled.work_order_number in not_null and blank.work_order_number not in not_null


def test_datetime_filter_takes_an_instant_with_a_zone(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    early = make_work_order(db_session)
    early.actual_start = datetime(2026, 3, 1, 12, 0)
    late = make_work_order(db_session)
    late.actual_start = datetime(2026, 3, 2, 12, 0)
    db_session.commit()
    numbers = wo_numbers(
        run(
            client,
            manager,
            {
                "data_source": "work_orders",
                "columns": [{"field": "work_order_number"}],
                # 2026-03-02T06:00-06:00 is 12:00 UTC: inclusive bound on ``late``.
                "filters": [{"field": "actual_start", "operator": "gte", "value": "2026-03-02T06:00:00-06:00"}],
            },
        )
    )
    assert numbers == {late.work_order_number}


# ===========================================================================
# 422 for wrong input
# ===========================================================================

_WO = "work_orders"
_COL = [{"field": "work_order_number"}]


def _f(field, operator, value, value2=None):
    return {"field": field, "operator": operator, "value": value, "value2": value2}


INVALID_DEFINITIONS = {
    "production source": {"data_source": "production", "columns": [{"field": "x"}]},
    "unknown source": {"data_source": "nope", "columns": _COL},
    "no columns": {"data_source": _WO, "columns": []},
    "unknown column field": {"data_source": _WO, "columns": [{"field": "estimated_hours"}]},
    "unknown filter field": {"data_source": _WO, "columns": _COL, "filters": [_f("nope", "eq", "x")]},
    "unknown group field": {"data_source": _WO, "columns": _COL, "group_by": [{"field": "nope"}]},
    "unknown sort field": {"data_source": _WO, "columns": _COL, "sort": [{"field": "nope"}]},
    "unknown operator": {"data_source": _WO, "columns": _COL, "filters": [_f("customer_name", "contains", "x")]},
    "operator wrong for enum": {"data_source": _WO, "columns": _COL, "filters": [_f("status", "gt", "draft")]},
    "like on number": {"data_source": _WO, "columns": _COL, "filters": [_f("quantity_ordered", "like", "1")]},
    "in on date": {"data_source": _WO, "columns": _COL, "filters": [_f("due_date", "in", ["2026-01-01"])]},
    "string for number": {"data_source": _WO, "columns": _COL, "filters": [_f("quantity_ordered", "gt", "5")]},
    "bool for number": {"data_source": _WO, "columns": _COL, "filters": [_f("quantity_ordered", "eq", True)]},
    "bad date": {"data_source": _WO, "columns": _COL, "filters": [_f("due_date", "gt", "2026/01/01")]},
    "impossible date": {"data_source": _WO, "columns": _COL, "filters": [_f("due_date", "gt", "2026-02-30")]},
    "datetime without time": {"data_source": _WO, "columns": _COL, "filters": [_f("actual_start", "gt", "2026-01-01")]},
    "bad enum option": {"data_source": _WO, "columns": _COL, "filters": [_f("status", "eq", "IN_PROGRESS")]},
    "number for string": {"data_source": _WO, "columns": _COL, "filters": [_f("customer_name", "eq", 5)]},
    "null value with gt": {"data_source": _WO, "columns": _COL, "filters": [_f("quantity_ordered", "gt", None)]},
    "between without value2": {
        "data_source": _WO,
        "columns": _COL,
        "filters": [_f("quantity_ordered", "between", 1, None)],
    },
    "in with scalar": {"data_source": _WO, "columns": _COL, "filters": [_f("customer_name", "in", "x")]},
    "in with empty list": {"data_source": _WO, "columns": _COL, "filters": [_f("customer_name", "in", [])]},
    "in over cap": {"data_source": _WO, "columns": _COL, "filters": [_f("quantity_ordered", "in", list(range(501)))]},
    "in with null item": {"data_source": _WO, "columns": _COL, "filters": [_f("customer_name", "in", ["a", None])]},
    "sum on string": {"data_source": _WO, "columns": [{"field": "customer_name", "aggregate": "sum"}]},
    "avg on date": {"data_source": _WO, "columns": [{"field": "due_date", "aggregate": "avg"}]},
    "max on enum": {"data_source": _WO, "columns": [{"field": "status", "aggregate": "max"}]},
    "duplicate output key": {
        "data_source": _WO,
        "columns": [{"field": "status"}, {"field": "customer_name", "alias": "status"}],
    },
    "plain column beside an aggregate": {
        "data_source": _WO,
        "columns": [{"field": "customer_name"}, {"field": "quantity_ordered", "aggregate": "sum"}],
    },
    "plain column not grouped": {
        "data_source": _WO,
        "columns": [{"field": "customer_name"}, {"field": "status"}],
        "group_by": [{"field": "customer_name"}],
    },
    "granularity": {
        "data_source": _WO,
        "columns": [{"field": "due_date"}],
        "group_by": [{"field": "due_date", "granularity": "month"}],
    },
    "bad sort direction": {"data_source": _WO, "columns": _COL, "sort": [{"field": "status", "direction": "down"}]},
    "grouped sort off the grouping": {
        "data_source": _WO,
        "columns": [{"field": "customer_name"}],
        "group_by": [{"field": "customer_name"}],
        "sort": [{"field": "due_date"}],
    },
    "too many columns": {"data_source": _WO, "columns": [{"field": "status", "alias": f"s{i}"} for i in range(41)]},
    "extra top-level key": {"data_source": _WO, "columns": _COL, "page": 2},
    "extra nested key": {"data_source": _WO, "columns": [{"field": "status", "agregate": "sum"}]},
    "limit null": {"data_source": _WO, "columns": _COL, "limit": None},
    "limit zero": {"data_source": _WO, "columns": _COL, "limit": 0},
    "limit negative": {"data_source": _WO, "columns": _COL, "limit": -1},
    "limit over max": {"data_source": _WO, "columns": _COL, "limit": 10_001},
}


@pytest.mark.parametrize("case", sorted(INVALID_DEFINITIONS))
def test_wrong_input_is_422(client: TestClient, db_session: Session, case: str):
    manager = make_user(db_session)
    make_work_order(db_session)
    resp = run(client, manager, INVALID_DEFINITIONS[case])
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, f"{case}: {resp.status_code} {resp.text}"


def test_one_422_lists_every_problem(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    resp = run(
        client,
        manager,
        {
            "data_source": _WO,
            "columns": [{"field": "nope"}],
            "filters": [_f("status", "gt", "draft")],
            "sort": [{"field": "status", "direction": "sideways"}],
        },
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    message = " ".join(err["msg"] for err in resp.json()["detail"])
    assert "Column 1" in message and "Filter 1 (Status)" in message and "Sort 1" in message


def test_valid_edge_inputs_are_accepted(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    make_work_order(db_session)
    resp = run(
        client,
        manager,
        {
            "data_source": _WO,
            "columns": [{"field": "status"}, {"field": "quantity_ordered", "aggregate": "count", "alias": "n"}],
            "group_by": [{"field": "status"}],
            "sort": [{"field": "n", "direction": "Desc"}, {"field": "status"}],
            "filters": [_f("quantity_ordered", "in", [10, 2.5]), _f("due_date", "lte", "2026-12-31")],
            "limit": 10_000,
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["rows"] == [{"status": "in_progress", "n": 1}]


# ===========================================================================
# Templates: CRUD, normalization, audit, access
# ===========================================================================


def _definition(**overrides):
    body = {
        "data_source": "work_orders",
        "columns": [{"field": "work_order_number"}, {"field": "status"}],
        "filters": [_f("quantity_ordered", "gte", 1)],
        "sort": [{"field": "work_order_number", "direction": "DESC"}],
    }
    body.update(overrides)
    return body


def test_create_template_validates_normalizes_and_audits(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    resp = client.post(
        TEMPLATES,
        headers=headers_for(manager),
        json={"name": "  Open jobs  ", "description": "Weekly review", **_definition()},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["name"] == "Open jobs"
    assert body["sort"] == [{"field": "work_order_number", "direction": "desc"}]
    assert body["definition_errors"] == []
    assert body["created_at"].endswith("Z") and body["updated_at"].endswith("Z")

    stored = db_session.query(ReportTemplate).filter(ReportTemplate.id == body["id"]).one()
    assert stored.company_id == COMPANY_A and stored.created_by == manager.id
    assert stored.columns == [
        {"field": "work_order_number", "alias": None, "aggregate": None},
        {"field": "status", "alias": None, "aggregate": None},
    ]

    rows = committed_template_audit_rows("CREATE")
    assert len(rows) == 1
    assert rows[0].resource_id == body["id"]
    assert rows[0].resource_identifier == "Open jobs"
    assert rows[0].user_id == manager.id and rows[0].company_id == COMPANY_A
    assert rows[0].new_values["data_source"] == "work_orders"
    assert rows[0].new_values["filters"][0]["operator"] == "gte"


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "   "},
        {"name": "x" * 256},
        {"description": "x" * 2001},
        {"columns": [{"field": "nope"}]},
        {"surprise": True},
    ],
)
def test_create_template_refuses_bad_input(client: TestClient, db_session: Session, bad: dict):
    manager = make_user(db_session)
    resp = client.post(TEMPLATES, headers=headers_for(manager), json={"name": "T", **_definition(), **bad})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert db_session.query(ReportTemplate).count() == 0
    assert committed_template_audit_rows("CREATE") == []


def test_update_template_replaces_definition_and_audits_old_and_new(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    template = make_template(db_session, manager, name="Before")
    resp = client.patch(
        f"{TEMPLATES}/{template.id}",
        headers=headers_for(manager),
        json={
            "name": "After",
            "is_shared": True,
            "definition": {"data_source": "parts", "columns": [{"field": "part_number"}]},
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["name"] == "After" and body["is_shared"] is True
    assert body["data_source"] == "parts"
    assert body["columns"] == [{"field": "part_number", "alias": None, "aggregate": None}]
    assert body["filters"] == [] and body["sort"] == []

    rows = committed_template_audit_rows("UPDATE")
    assert len(rows) == 1
    assert rows[0].old_values["name"] == "Before" and rows[0].new_values["name"] == "After"
    assert rows[0].old_values["data_source"] == "work_orders" and rows[0].new_values["data_source"] == "parts"


def test_update_template_description_null_clears_it(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    template = make_template(db_session, manager)
    template.description = "old"
    db_session.commit()
    resp = client.patch(f"{TEMPLATES}/{template.id}", headers=headers_for(manager), json={"description": None})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["description"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"name": None},
        {"is_shared": None},
        {"definition": None},
        {"name": ""},
        {"definition": {"data_source": "work_orders", "columns": [{"field": "nope"}]}},
        {"definition": {"data_source": "production", "columns": [{"field": "x"}]}},
        {"definition": {"data_source": "work_orders", "columns": _COL, "limit": 5}},
        {"owner": 3},
    ],
)
def test_update_template_refuses_bad_input(client: TestClient, db_session: Session, bad: dict):
    manager = make_user(db_session)
    template = make_template(db_session, manager, name="Unchanged")
    resp = client.patch(f"{TEMPLATES}/{template.id}", headers=headers_for(manager), json=bad)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    db_session.refresh(template)
    assert template.name == "Unchanged"
    assert committed_template_audit_rows("UPDATE") == []


def test_delete_template_audits_a_snapshot(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    template = make_template(db_session, manager, name="Doomed", filters=[_f("quantity_ordered", "gt", 0)])
    template_id = template.id
    resp = client.delete(f"{TEMPLATES}/{template_id}", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.query(ReportTemplate).filter(ReportTemplate.id == template_id).first() is None

    rows = committed_template_audit_rows("DELETE")
    assert len(rows) == 1
    snapshot = rows[0].old_values
    assert rows[0].resource_id == template_id and rows[0].resource_identifier == "Doomed"
    assert snapshot["data_source"] == "work_orders"
    assert snapshot["columns"] == [{"field": "work_order_number"}, {"field": "status"}]
    assert snapshot["filters"] == [_f("quantity_ordered", "gt", 0)]


def test_get_and_run_template(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    wo = make_work_order(db_session)
    template = make_template(db_session, manager)
    got = client.get(f"{TEMPLATES}/{template.id}", headers=headers_for(manager))
    assert got.status_code == status.HTTP_200_OK, got.text
    assert got.json()["id"] == template.id and got.json()["definition_errors"] == []

    ran = client.post(f"{TEMPLATES}/{template.id}/run?limit=5", headers=headers_for(manager))
    assert ran.status_code == status.HTTP_200_OK, ran.text
    assert ran.json()["rows"] == [{"work_order_number": wo.work_order_number, "status": "in_progress"}]

    for bad_limit in (0, 10_001):
        resp = client.post(f"{TEMPLATES}/{template.id}/run?limit={bad_limit}", headers=headers_for(manager))
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text


def test_access_rule_for_template_verbs(client: TestClient, db_session: Session):
    author = make_user(db_session)
    other_manager = make_user(db_session)
    admin = make_user(db_session, role=UserRole.ADMIN)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    foreign_manager = make_user(db_session, company_id=COMPANY_B)
    make_work_order(db_session)
    private = make_template(db_session, author, name="Private")
    shared = make_template(db_session, author, shared=True, name="Shared")

    # Read/run: own or shared only.
    assert client.get(f"{TEMPLATES}/{shared.id}", headers=headers_for(other_manager)).status_code == 200
    assert client.post(f"{TEMPLATES}/{shared.id}/run", headers=headers_for(other_manager)).status_code == 200
    assert client.get(f"{TEMPLATES}/{private.id}", headers=headers_for(other_manager)).status_code == 403
    assert client.post(f"{TEMPLATES}/{private.id}/run", headers=headers_for(other_manager)).status_code == 403

    # Change/delete: the author or an admin -- shared does not mean editable.
    patch = {"name": "Renamed"}
    assert client.patch(f"{TEMPLATES}/{shared.id}", headers=headers_for(other_manager), json=patch).status_code == 403
    assert client.delete(f"{TEMPLATES}/{shared.id}", headers=headers_for(other_manager)).status_code == 403
    assert client.patch(f"{TEMPLATES}/{shared.id}", headers=headers_for(admin), json=patch).status_code == 200
    assert client.patch(f"{TEMPLATES}/{private.id}", headers=headers_for(author), json=patch).status_code == 200

    # Below ADMIN/MANAGER: refused by the role gate on every new verb.
    sup = headers_for(supervisor)
    assert client.get(f"{TEMPLATES}/{shared.id}", headers=sup).status_code == 403
    assert client.post(f"{TEMPLATES}/{shared.id}/run", headers=sup).status_code == 403
    assert client.patch(f"{TEMPLATES}/{shared.id}", headers=sup, json=patch).status_code == 403

    # Another company: the same 404 as a missing id, on every verb.
    foreign = headers_for(foreign_manager)
    for resp in (
        client.get(f"{TEMPLATES}/{shared.id}", headers=foreign),
        client.post(f"{TEMPLATES}/{shared.id}/run", headers=foreign),
        client.patch(f"{TEMPLATES}/{shared.id}", headers=foreign, json=patch),
        client.delete(f"{TEMPLATES}/{shared.id}", headers=foreign),
        client.get(f"{TEMPLATES}/999999", headers=headers_for(author)),
    ):
        assert resp.status_code == 404, resp.text
    assert [t["name"] for t in client.get(TEMPLATES, headers=foreign).json()] == []


# ===========================================================================
# Legacy stored templates: list/get render, run/export 409
# ===========================================================================


def test_legacy_invalid_template_lists_and_opens_but_refuses_to_run(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    make_work_order(db_session)
    legacy = make_template(
        db_session,
        manager,
        name="Legacy Labor",
        columns=[{"field": "work_order_number"}, {"field": "estimated_hours"}],
        filters=[{"field": "status", "operator": "eq", "value": "IN_PROGRESS", "value2": None}],
    )
    retired = make_template(
        db_session, manager, name="Retired Source", data_source="production", columns=[{"field": "x"}]
    )
    good = make_template(db_session, manager, name="Good")

    listed = client.get(TEMPLATES, headers=headers_for(manager))
    assert listed.status_code == status.HTTP_200_OK, listed.text
    by_name = {t["name"]: t for t in listed.json()}
    assert by_name["Good"]["definition_errors"] == []
    errors = by_name["Legacy Labor"]["definition_errors"]
    assert any("estimated_hours" in e for e in errors) and any("IN_PROGRESS" in e for e in errors)
    assert by_name["Retired Source"]["data_source"] == "production"
    assert by_name["Retired Source"]["definition_errors"]

    got = client.get(f"{TEMPLATES}/{legacy.id}", headers=headers_for(manager))
    assert got.status_code == status.HTTP_200_OK, got.text
    assert got.json()["definition_errors"] == errors

    for template in (legacy, retired):
        ran = client.post(f"{TEMPLATES}/{template.id}/run", headers=headers_for(manager))
        assert ran.status_code == status.HTTP_409_CONFLICT, ran.text
        detail = ran.json()["detail"]
        assert detail.startswith(f'Saved report "{template.name}" can no longer run: ')
        assert detail.endswith("Edit it and save it again.")
        exported = client.get(f"{EXPORT}?template_id={template.id}&format=csv", headers=headers_for(manager))
        assert exported.status_code == status.HTTP_409_CONFLICT, exported.text
    assert committed_export_rows() == []

    assert client.post(f"{TEMPLATES}/{good.id}/run", headers=headers_for(manager)).status_code == 200

    # Repairing it through PATCH clears the errors.
    fixed = client.patch(
        f"{TEMPLATES}/{legacy.id}",
        headers=headers_for(manager),
        json={"definition": {"data_source": "work_orders", "columns": [{"field": "work_order_number"}]}},
    )
    assert fixed.status_code == status.HTTP_200_OK, fixed.text
    assert fixed.json()["definition_errors"] == []


# ===========================================================================
# Export: cap refusal, full result (not the 1,000 default), filename header
# ===========================================================================


def test_export_refuses_over_the_cap_and_logs_nothing(client: TestClient, db_session: Session, monkeypatch):
    monkeypatch.setattr(analytics_endpoints, "MAX_EXPORT_ROWS", 2)
    manager = make_user(db_session)
    for _ in range(3):
        make_work_order(db_session)
    template = make_template(db_session, manager)
    resp = client.get(f"{EXPORT}?template_id={template.id}", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "This export matches more than 2 rows. Add a filter, then export again."
    assert committed_export_rows() == []


def test_export_is_not_cut_at_the_run_default(client: TestClient, db_session: Session, monkeypatch):
    """The run default (1000) does not apply: at a cap of 3, three rows export whole."""
    monkeypatch.setattr(analytics_endpoints, "MAX_EXPORT_ROWS", 3)
    manager = make_user(db_session)
    numbers = {make_work_order(db_session).work_order_number for _ in range(3)}
    template = make_template(db_session, manager, columns=[{"field": "work_order_number"}])
    resp = client.get(f"{EXPORT}?template_id={template.id}", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert {row["work_order_number"] for row in csv.DictReader(io.StringIO(resp.text))} == numbers
    assert committed_export_rows("custom_report")[0].description == "Exported 3 custom report record(s) to CSV"


def test_export_content_disposition_carries_filename_star(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    make_work_order(db_session)
    name = 'Q3 Résumé/Plan "draft"'
    template = make_template(db_session, manager, name=name)
    resp = client.get(f"{EXPORT}?template_id={template.id}&format=csv", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    disposition = resp.headers["content-disposition"]
    assert disposition == attachment_content_disposition(f"{name}.csv")
    assert "filename*=UTF-8''Q3%20R%C3%A9sum%C3%A9%2FPlan%20%22draft%22.csv" in disposition
    assert 'filename="Q3 R_sum__Plan _draft_.csv"' in disposition


def test_content_disposition_fallback_is_plain_ascii():
    header = attachment_content_disposition("a\\b\x07cé.csv")
    assert header.startswith('attachment; filename="a_b_c_.csv"; ')
    assert header.endswith("filename*=UTF-8''a%5Cb%07c%C3%A9.csv")
    header.encode("latin-1")  # Starlette sends headers as latin-1


def test_export_default_format_is_csv(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    make_work_order(db_session)
    template = make_template(db_session, manager)
    resp = client.get(f"{EXPORT}?template_id={template.id}", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.headers["content-type"].startswith("text/csv")


def test_due_date_filter_is_a_bare_date(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    soon = make_work_order(db_session)
    soon.due_date = date.today() + timedelta(days=1)
    later = make_work_order(db_session)
    later.due_date = date.today() + timedelta(days=90)
    db_session.commit()
    cutoff = (date.today() + timedelta(days=30)).isoformat()
    numbers = wo_numbers(
        run(
            client,
            manager,
            {"data_source": _WO, "columns": _COL, "filters": [_f("due_date", "lt", cutoff)]},
        )
    )
    assert numbers == {soon.work_order_number}
