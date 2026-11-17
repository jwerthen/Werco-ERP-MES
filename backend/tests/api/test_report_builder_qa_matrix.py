"""QA matrix for the custom report builder: every field of every source, end to end.

``test_report_builder_screen_contract.py`` pins the contract case by case. This file
closes the gaps a case-by-case file leaves open:

* **Every advertised field binds and returns, in every role it can play.** For each data
  source and each catalog field: as a column (over HTTP, so the response serializes), as
  a filter under every operator its type allows, as a group-by, as a sort, and under every
  aggregate its type allows. A field the catalog advertises but the engine cannot bind --
  the defect class the one-catalog refactor exists to end -- fails here by name.
* **Tenant scope and soft delete hold on every one of those query shapes**, not only on a
  bare select. Each source is seeded with a target row, a decoy row, a FOREIGN twin
  (company B, the target's values) and -- where the model is soft-deletable -- a DELETED
  twin (company A, the target's values). Every filter that matches the target would match
  the twins too if a scope were missing, and every sum/count would double.
* **Enum fields of both storage forms** (member NAME, e.g. ``WorkOrder.status``; member
  VALUE via ``values_callable``, e.g. ``Part.part_type``) filter correctly.
* Sorting by an aggregate's output key orders by the AGGREGATE, not by the raw column.
* ``truncated`` is exact for a grouped report too.
* A voided NCR (through the real ``void_ncr`` path) never appears, even when the filter
  asks for status ``void``.
* The CSV header row (tenant-authored aliases) is formula-neutralized and still lines up.
* The saved-template access rule on every verb, for every kind of caller, including a
  Platform Admin, a superuser, a demoted author and a switched Platform Admin.
* The documented API-token posture on these routes: a full token builds, saves (audited as
  the token) and exports; a read-only token reads but cannot run or download.
* The client's copy of the server's caps and operator/aggregate tables
  (``frontend/.../reportBuilder/reportDefinition.ts``) matches the server.

Engine note: tests run on SQLite. Sorting an enum column is engine-specific (Postgres
orders a native enum by declaration, SQLite by stored text), so the enum sort case
asserts only that both rows come back, never their order.
"""

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.analytics import ReportTemplate
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.part import Part, PartType
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.models.quality import NCRDisposition, NCRSource, NCRStatus, NonConformanceReport
from app.models.quote import Quote, QuoteStatus
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.schemas.analytics import (
    REPORT_DEFAULT_LIMIT,
    REPORT_MAX_COLUMNS,
    REPORT_MAX_FILTERS,
    REPORT_MAX_GROUP_BY,
    REPORT_MAX_LIMIT,
    REPORT_MAX_SORT,
    CustomReportRequest,
    ReportDataSource,
)
from app.services import report_builder, report_template_service
from app.services.audit_service import AuditService
from app.services.quality_record_service import void_ncr
from app.services.report_builder import AGGREGATE_TYPES, OPERATORS_BY_TYPE, REPORT_SOURCES, ReportBuilderService
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


def make_user(
    db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A, is_superuser: bool = False
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"rbq-{n}@co{company_id}.test",
        employee_id=f"RBQ-{n:05d}",
        first_name="Report",
        last_name=f"Matrix{n}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=is_superuser,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User, *, company_id: Optional[int] = None) -> dict:
    token = create_access_token(subject=user.id, company_id=company_id or user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


# ===========================================================================
# Seeding: one row per (source, values, company)
# ===========================================================================


def _stored(field_type: str, wire: Any) -> Any:
    """The ORM value for a wire value of ``field_type`` (enums are converted by the seeder)."""
    if wire is None:
        return None
    if field_type == "date":
        return date.fromisoformat(wire)
    if field_type == "datetime":
        # Timestamps are stored as naive UTC.
        return datetime.fromisoformat(wire.replace("Z", "+00:00")).replace(tzinfo=None)
    return wire


def _part(db: Session, company_id: int, part_number: str, **values) -> Part:
    _ensure_company(db, company_id)
    part = Part(
        part_number=part_number,
        name=values.pop("name", f"Part {part_number}"),
        part_type=values.pop("part_type", PartType.MANUFACTURED),
        company_id=company_id,
        **values,
    )
    db.add(part)
    db.flush()
    return part


def _vendor(db: Session, company_id: int, name: str) -> Vendor:
    _ensure_company(db, company_id)
    vendor = Vendor(code=f"RBQ-V-{_next():05d}", name=name, is_approved=True, company_id=company_id)
    db.add(vendor)
    db.flush()
    return vendor


def seed_work_order(db: Session, company_id: int, v: Dict[str, Any]) -> WorkOrder:
    part = _part(db, company_id, f"RBQ-WOP-{_next():05d}")
    wo = WorkOrder(
        part_id=part.id,
        priority=5,
        company_id=company_id,
        status=WorkOrderStatus(v.pop("status")),
        **v,
    )
    db.add(wo)
    db.flush()
    return wo


def seed_part(db: Session, company_id: int, v: Dict[str, Any]) -> Part:
    v["part_type"] = PartType(v["part_type"])
    return _part(db, company_id, v.pop("part_number"), **v)


def seed_inventory(db: Session, company_id: int, v: Dict[str, Any]) -> InventoryItem:
    # The joined Part: reuse one of this company with that number, else create it.
    part_number = v.pop("part_number")
    part = db.query(Part).filter(Part.company_id == company_id, Part.part_number == part_number).first()
    if part is None:
        part = _part(db, company_id, part_number)
    item = InventoryItem(part_id=part.id, company_id=company_id, **v)
    db.add(item)
    db.flush()
    return item


def seed_ncr(db: Session, company_id: int, v: Dict[str, Any]) -> NonConformanceReport:
    _ensure_company(db, company_id)
    ncr = NonConformanceReport(
        title="Matrix finding",
        description="Matrix finding",
        company_id=company_id,
        status=NCRStatus(v.pop("status")),
        source=NCRSource(v.pop("source")),
        disposition=NCRDisposition(v.pop("disposition")),
        **v,
    )
    db.add(ncr)
    db.flush()
    return ncr


def seed_po(db: Session, company_id: int, v: Dict[str, Any]) -> PurchaseOrder:
    vendor_name = v.pop("vendor_name")
    vendor = db.query(Vendor).filter(Vendor.company_id == company_id, Vendor.name == vendor_name).first()
    if vendor is None:
        vendor = _vendor(db, company_id, vendor_name)
    po = PurchaseOrder(vendor_id=vendor.id, company_id=company_id, status=POStatus(v.pop("status")), **v)
    db.add(po)
    db.flush()
    return po


def seed_quote(db: Session, company_id: int, v: Dict[str, Any]) -> Quote:
    _ensure_company(db, company_id)
    quote = Quote(company_id=company_id, status=QuoteStatus(v.pop("status")), **v)
    db.add(quote)
    db.flush()
    return quote


@dataclass(frozen=True)
class SourceCase:
    """A data source's seed: the identity field and, per catalog field, (target, decoy) wire values.

    Every ordered field has target > decoy (also lexically for strings and enum names and
    values), and every string target contains its ``like`` probe while its decoy does not.
    """

    source: ReportDataSource
    identity: str
    values: Dict[str, Tuple[Any, Any]]
    seed: Callable[[Session, int, Dict[str, Any]], Any]
    # The foreign twin's identity, when the identity is unique across companies.
    foreign_identity: Optional[str] = None
    like: Optional[Dict[str, str]] = None


CASES: Dict[ReportDataSource, SourceCase] = {
    case.source: case
    for case in (
        SourceCase(
            source=ReportDataSource.WORK_ORDERS,
            identity="work_order_number",
            seed=seed_work_order,
            values={
                "work_order_number": ("RBQ-WO-Z", "RBQ-WO-A"),
                # Stored as the member NAME (no values_callable).
                "status": ("released", "draft"),
                "quantity_ordered": (20, 10),
                "quantity_complete": (8, 2),
                "quantity_scrapped": (3, 1),
                "due_date": ("2026-06-15", "2026-06-01"),
                "actual_start": ("2026-03-02T12:00:00Z", "2026-03-01T12:00:00Z"),
                "actual_end": ("2026-03-05T08:30:00Z", "2026-03-04T08:30:00Z"),
                "customer_name": ("Zephyr Gear", "Alpha Tooling"),
                "customer_po": ("CPO-Z", "CPO-A"),
                "actual_hours": (7.5, 2.5),
                "estimated_cost": (900.5, 100.25),
                "actual_cost": (950.75, 120.5),
                "created_at": ("2026-02-02T09:00:00Z", "2026-02-01T09:00:00Z"),
            },
        ),
        SourceCase(
            source=ReportDataSource.PARTS,
            identity="part_number",
            seed=seed_part,
            values={
                "part_number": ("RBQ-P-Z", "RBQ-P-A"),
                "name": ("Zinc bracket", "Alpha bracket"),
                # Stored as the member VALUE (values_callable).
                "part_type": ("purchased", "manufactured"),
                "standard_cost": (12.5, 2.5),
                "material_cost": (6.5, 1.5),
                "labor_cost": (4.25, 0.75),
                "lead_time_days": (14, 3),
                "safety_stock": (9, 2),
                "reorder_point": (11, 4),
            },
        ),
        SourceCase(
            source=ReportDataSource.INVENTORY,
            identity="lot_number",
            seed=seed_inventory,
            values={
                "part_number": ("RBQ-INV-Z", "RBQ-INV-A"),  # the joined Part
                "quantity_on_hand": (40, 5),
                "quantity_allocated": (6, 1),
                "quantity_available": (34, 4),
                "location": ("Z-09", "A-01"),
                "lot_number": ("LOT-Z", "LOT-A"),
                "unit_cost": (3.5, 1.25),
                "status": ("quarantine", "available"),
            },
            like={"status": "quar"},
        ),
        SourceCase(
            source=ReportDataSource.QUALITY,
            identity="ncr_number",
            seed=seed_ncr,
            values={
                "ncr_number": ("RBQ-NCR-Z", "RBQ-NCR-A"),
                "status": ("under_review", "open"),
                "source": ("in_process", "final_inspection"),
                "disposition": ("scrap", "rework"),
                "quantity_affected": (5, 2),
                "quantity_rejected": (3, 1),
                "detected_date": ("2026-05-10", "2026-05-02"),
                "closed_date": ("2026-05-20", "2026-05-12"),
                "estimated_cost": (250.5, 50.25),
                "actual_cost": (300.75, 60.5),
            },
        ),
        SourceCase(
            source=ReportDataSource.PURCHASING,
            identity="po_number",
            seed=seed_po,
            values={
                "po_number": ("RBQ-PO-Z", "RBQ-PO-A"),
                "vendor_name": ("Zenith Supply", "Acme Metals"),  # the joined Vendor
                "status": ("sent", "draft"),
                "order_date": ("2026-05-09", "2026-05-01"),
                "required_date": ("2026-06-20", "2026-06-10"),
                "subtotal": (500.5, 100.25),
                "total": (540.75, 108.5),
            },
        ),
        SourceCase(
            source=ReportDataSource.QUOTES,
            identity="quote_number",
            seed=seed_quote,
            # ``quotes.quote_number`` is unique across companies.
            foreign_identity="RBQ-Q-Z-FOREIGN",
            values={
                "quote_number": ("RBQ-Q-Z", "RBQ-Q-A"),
                "customer_name": ("Zephyr Labs", "Alpha Labs"),
                "status": ("sent", "draft"),
                "quote_date": ("2026-04-09", "2026-04-01"),
                "subtotal": (800.5, 200.25),
                "total": (864.75, 216.5),
                "lead_time_days": (21, 7),
            },
        ),
    )
}


def _row_values(case: SourceCase, which: int, identity: Optional[str] = None) -> Dict[str, Any]:
    fields = REPORT_SOURCES[case.source].fields
    values = {name: _stored(fields[name].type, pair[which]) for name, pair in case.values.items()}
    if identity is not None:
        values[case.identity] = identity
    return values


@dataclass
class Seeded:
    case: SourceCase
    target: str
    decoy: str


def seed_source(db: Session, case: SourceCase) -> Seeded:
    """Decoy, then target (so id order is [decoy, target]), then the two twins."""
    actor = make_user(db)
    _ensure_company(db, COMPANY_B)
    decoy = case.seed(db, COMPANY_A, _row_values(case, 1))
    case.seed(db, COMPANY_A, _row_values(case, 0))

    # FOREIGN twin: company B, every value the target's (its joined Part / Vendor too).
    case.seed(db, COMPANY_B, _row_values(case, 0, case.foreign_identity))

    model = REPORT_SOURCES[case.source].model
    if hasattr(model, "soft_delete"):
        # DELETED twin: company A, every value the target's but the identity (unique per company).
        twin = case.seed(db, COMPANY_A, _row_values(case, 0, case.values[case.identity][0] + "-DEL"))
        twin.soft_delete(actor.id)

    # A joined row is a historical record and keeps naming its row after it is deleted.
    if case.source == ReportDataSource.INVENTORY:
        db.query(Part).filter(Part.id == decoy.part_id).one().soft_delete(actor.id)
    if case.source == ReportDataSource.PURCHASING:
        db.query(Vendor).filter(Vendor.id == decoy.vendor_id).one().soft_delete(actor.id)
    db.commit()
    target_id, decoy_id = case.values[case.identity]
    return Seeded(case=case, target=target_id, decoy=decoy_id)


def run_service(db: Session, case: SourceCase, **body) -> List[Dict[str, Any]]:
    request = CustomReportRequest(data_source=case.source.value, **body)
    result = ReportBuilderService(db).execute_report(request, COMPANY_A)
    # The result must serialize exactly as the endpoint sends it.
    json.dumps(result.model_dump(mode="json"))
    return result.rows


def ids(rows: List[Dict[str, Any]], identity: str) -> List[Any]:
    return [row[identity] for row in rows]


def _f(field: str, operator: str, value: Any, value2: Any = None) -> dict:
    return {"field": field, "operator": operator, "value": value, "value2": value2}


def _filter_cases(field_type: str, target: Any, decoy: Any, like: str) -> List[Tuple[dict, str]]:
    """(filter, expected) for every operator ``field_type`` allows. Expected is "T", "D" or "DT"."""
    cases: Dict[str, List[Tuple[dict, str]]] = {
        "string": [
            (("eq", target, None), "T"),
            (("ne", decoy, None), "T"),
            (("in", [target], None), "T"),
            (("like", like, None), "T"),
        ],
        "enum": [
            (("eq", target, None), "T"),
            (("ne", decoy, None), "T"),
            (("in", [target], None), "T"),
        ],
        "number": [
            (("eq", target, None), "T"),
            (("ne", target, None), "D"),
            (("gt", decoy, None), "T"),
            (("gte", target, None), "T"),
            (("lt", target, None), "D"),
            (("lte", decoy, None), "D"),
            (("in", [target], None), "T"),
            (("between", target, target), "T"),
            (("between", decoy, target), "DT"),
        ],
    }
    cases["date"] = cases["datetime"] = [c for c in cases["number"] if c[0][0] != "in"]
    out = [
        ({"operator": op, "value": value, "value2": value2}, expected)
        for (op, value, value2), expected in cases[field_type]
    ]
    # Every field type also takes the two null tests (all seeded values are non-null).
    out += [
        ({"operator": "eq", "value": None, "value2": None}, ""),
        ({"operator": "ne", "value": None, "value2": None}, "DT"),
    ]
    # The table above must cover exactly what the engine allows (plus eq/ne null).
    assert {c["operator"] for c, _ in out} == set(OPERATORS_BY_TYPE[field_type])
    return out


# ===========================================================================
# The matrix
# ===========================================================================


def test_matrix_covers_every_catalog_source_and_field():
    """A new source or field must be added to CASES -- the matrix cannot silently skip one."""
    assert set(CASES) == set(REPORT_SOURCES)
    for source, case in CASES.items():
        assert set(case.values) == set(REPORT_SOURCES[source].fields), source.value


@pytest.mark.parametrize("source", sorted(CASES, key=lambda s: s.value), ids=lambda s: s.value)
def test_every_field_as_a_column_over_http(client: TestClient, db_session: Session, source: ReportDataSource):
    """Every advertised field, all at once, through the endpoint: each binds, each serializes,
    each column carries the catalog's label and type, and neither twin appears."""
    case = CASES[source]
    seed_source(db_session, case)
    manager = make_user(db_session)
    fields = list(REPORT_SOURCES[source].fields.values())
    resp = client.post(
        REPORT,
        headers=headers_for(manager),
        json={
            "data_source": source.value,
            "columns": [{"field": spec.name} for spec in fields],
            "sort": [{"field": case.identity, "direction": "asc"}],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["columns"] == [{"key": spec.name, "label": spec.label, "type": spec.type} for spec in fields]
    expected = [{name: pair[which] for name, pair in case.values.items()} for which in (1, 0)]
    assert body["rows"] == expected
    assert body["row_count"] == 2 and body["truncated"] is False


@pytest.mark.parametrize("source", sorted(CASES, key=lambda s: s.value), ids=lambda s: s.value)
def test_every_field_filters_under_every_allowed_operator(db_session: Session, source: ReportDataSource):
    """Each filter selects exactly the seeded company-A live rows it should. The twins carry the
    target's values, so a missing tenant scope or soft-delete filter -- or a join that only
    a filter pulls in -- shows up as an extra row."""
    case = CASES[source]
    seeded = seed_source(db_session, case)
    expected_rows = {"T": [seeded.target], "D": [seeded.decoy], "DT": [seeded.decoy, seeded.target], "": []}
    for name, (target, decoy) in case.values.items():
        spec = REPORT_SOURCES[source].fields[name]
        like = (case.like or {}).get(name, "z")
        for report_filter, expected in _filter_cases(spec.type, target, decoy, like):
            rows = run_service(
                db_session, case, columns=[{"field": case.identity}], filters=[{"field": name, **report_filter}]
            )
            assert ids(rows, case.identity) == expected_rows[expected], f"{source.value}.{name} {report_filter}"


@pytest.mark.parametrize("source", sorted(CASES, key=lambda s: s.value), ids=lambda s: s.value)
def test_every_field_under_every_allowed_aggregate(db_session: Session, source: ReportDataSource):
    """An ungrouped aggregate is one row over the live company-A rows: a twin leaking in would
    double the count and the sum."""
    case = CASES[source]
    seed_source(db_session, case)
    for name, (target, decoy) in case.values.items():
        spec = REPORT_SOURCES[source].fields[name]
        for aggregate, allowed in AGGREGATE_TYPES.items():
            if spec.type not in allowed:
                continue
            rows = run_service(db_session, case, columns=[{"field": name, "aggregate": aggregate.value, "alias": "v"}])
            expected = {
                "count": 2,
                "sum": pytest.approx(round(target + decoy, 2)) if spec.type == "number" else None,
                "avg": pytest.approx(round((target + decoy) / 2, 2)) if spec.type == "number" else None,
                "min": decoy,
                "max": target,
            }[aggregate.value]
            assert rows == [{"v": expected}], f"{source.value}.{name} {aggregate.value}"


@pytest.mark.parametrize("source", sorted(CASES, key=lambda s: s.value), ids=lambda s: s.value)
def test_every_field_groups_and_sorts(db_session: Session, source: ReportDataSource):
    """Group by each field (with a count) and sort by each field in both directions. The sort
    case selects only the identity, so a joined field is reached through the sort alone."""
    case = CASES[source]
    seeded = seed_source(db_session, case)
    for name, (target, decoy) in case.values.items():
        spec = REPORT_SOURCES[source].fields[name]
        grouped = run_service(
            db_session,
            case,
            columns=[{"field": name}, {"field": case.identity, "aggregate": "count", "alias": "n"}],
            group_by=[{"field": name}],
        )
        assert sorted(grouped, key=lambda r: str(r[name])) == sorted(
            [{name: target, "n": 1}, {name: decoy, "n": 1}], key=lambda r: str(r[name])
        ), f"{source.value}.{name} group"

        for direction, expected in (("desc", [seeded.target, seeded.decoy]), ("asc", [seeded.decoy, seeded.target])):
            rows = run_service(
                db_session, case, columns=[{"field": case.identity}], sort=[{"field": name, "direction": direction}]
            )
            got = ids(rows, case.identity)
            if spec.type == "enum":
                # Engine-specific order (see the module docstring): both rows, nothing else.
                assert sorted(got) == sorted(expected), f"{source.value}.{name} sort {direction}"
            else:
                assert got == expected, f"{source.value}.{name} sort {direction}"


@pytest.mark.parametrize("source", sorted(CASES, key=lambda s: s.value), ids=lambda s: s.value)
def test_a_joined_or_plain_group_field_need_not_be_a_column(db_session: Session, source: ReportDataSource):
    """Group by every field while selecting only a count: a joined field reached through
    group_by alone still gets its (tenant-scoped) join, and each group counts one live row."""
    case = CASES[source]
    seed_source(db_session, case)
    for name in case.values:
        rows = run_service(
            db_session,
            case,
            columns=[{"field": case.identity, "aggregate": "count", "alias": "n"}],
            group_by=[{"field": name}],
            sort=[{"field": name}],
        )
        assert rows == [{"n": 1}, {"n": 1}], f"{source.value}.{name}"


# ===========================================================================
# Sort by an aggregate output key; grouped truncation
# ===========================================================================


def _wo(db: Session, *, customer: str, quantity: float, company_id: int = COMPANY_A, **extra) -> WorkOrder:
    wo = seed_work_order(
        db,
        company_id,
        {
            "work_order_number": f"RBQ-AGG-{_next():05d}",
            "status": "in_progress",
            "quantity_ordered": quantity,
            "customer_name": customer,
            "due_date": date(2026, 6, 1),
            **extra,
        },
    )
    db.commit()
    return wo


def test_sort_by_an_aggregate_output_key_orders_by_the_aggregate(db_session: Session):
    """Many small jobs outsum one big one. Sorting by the RAW column inside a group (what an
    engine does with a bare column) would put the big single job first; sorting by the
    aggregate puts the bigger total first -- in both directions, and for a count too."""
    for _ in range(3):
        _wo(db_session, customer="Many Small", quantity=10)
    _wo(db_session, customer="One Big", quantity=25)
    case = CASES[ReportDataSource.WORK_ORDERS]

    def ordered(sort: list) -> List[Tuple[str, Any, Any]]:
        rows = run_service(
            db_session,
            case,
            columns=[
                {"field": "customer_name"},
                {"field": "quantity_ordered", "aggregate": "sum", "alias": "total"},
                {"field": "work_order_number", "aggregate": "count", "alias": "jobs"},
            ],
            group_by=[{"field": "customer_name"}],
            sort=sort,
        )
        return [(r["customer_name"], r["total"], r["jobs"]) for r in rows]

    assert ordered([{"field": "total", "direction": "desc"}]) == [("Many Small", 30, 3), ("One Big", 25, 1)]
    assert ordered([{"field": "total", "direction": "asc"}]) == [("One Big", 25, 1), ("Many Small", 30, 3)]
    assert ordered([{"field": "jobs", "direction": "desc"}])[0][0] == "Many Small"
    # An aggregate whose output key IS its field name: the output wins over the raw field.
    rows = run_service(
        db_session,
        case,
        columns=[{"field": "customer_name"}, {"field": "quantity_ordered", "aggregate": "sum"}],
        group_by=[{"field": "customer_name"}],
        sort=[{"field": "quantity_ordered", "direction": "desc"}],
    )
    assert [r["customer_name"] for r in rows] == ["Many Small", "One Big"]


def test_truncated_is_exact_for_a_grouped_report(db_session: Session):
    for customer in ("Group A", "Group B", "Group C"):
        _wo(db_session, customer=customer, quantity=1)
    request = {
        "data_source": "work_orders",
        "columns": [{"field": "customer_name"}, {"field": "quantity_ordered", "aggregate": "sum"}],
        "group_by": [{"field": "customer_name"}],
        "sort": [{"field": "customer_name"}],
    }
    service = ReportBuilderService(db_session)
    cut = service.execute_report(CustomReportRequest(**request, limit=2), COMPANY_A)
    assert (cut.row_count, cut.truncated, [r["customer_name"] for r in cut.rows]) == (2, True, ["Group A", "Group B"])
    whole = service.execute_report(CustomReportRequest(**request, limit=3), COMPANY_A)
    assert (whole.row_count, whole.truncated) == (3, False)


# ===========================================================================
# Refusal branches the screen-contract table does not reach
# ===========================================================================

_RAW_REFUSALS = {
    "text value over the cap": (
        json.dumps(
            {
                "data_source": "work_orders",
                "columns": [{"field": "work_order_number"}],
                "filters": [_f("customer_name", "eq", "x" * (report_builder.MAX_TEXT_VALUE_LENGTH + 1))],
            }
        ),
        f"at most {report_builder.MAX_TEXT_VALUE_LENGTH} characters",
    ),
    "alias over the cap": (
        json.dumps(
            {
                "data_source": "work_orders",
                "columns": [{"field": "status", "alias": "a" * (report_builder.MAX_ALIAS_LENGTH + 1)}],
            }
        ),
        f"an alias may be at most {report_builder.MAX_ALIAS_LENGTH} characters",
    ),
    "impossible datetime": (
        json.dumps(
            {
                "data_source": "work_orders",
                "columns": [{"field": "work_order_number"}],
                "filters": [_f("actual_start", "gte", "2026-02-30T10:00:00Z")],
            }
        ),
        "is not a valid date and time",
    ),
}


@pytest.mark.parametrize("case", sorted(_RAW_REFUSALS))
def test_remaining_value_refusals_are_one_422(client: TestClient, db_session: Session, case: str):
    body, message = _RAW_REFUSALS[case]
    manager = make_user(db_session)
    headers = {**headers_for(manager), "Content-Type": "application/json"}
    resp = client.post(REPORT, headers=headers, content=body)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, f"{case}: {resp.status_code} {resp.text}"
    assert message in " ".join(err["msg"] for err in resp.json()["detail"]), resp.text


# Python's json parser (what a request body is decoded with) accepts the non-standard NaN /
# Infinity literals, so a client can send them even though no well-behaved encoder emits them.
_NON_FINITE_BODIES = {
    "NaN": '{"field":"quantity_ordered","operator":"gt","value":NaN}',
    "-Infinity in an in list": '{"field":"quantity_ordered","operator":"in","value":[1,-Infinity]}',
    "Infinity as value2": '{"field":"quantity_ordered","operator":"between","value":1,"value2":Infinity}',
}


@pytest.mark.parametrize("case", sorted(_NON_FINITE_BODIES))
@pytest.mark.parametrize("door", ["run", "save"])
def test_a_non_finite_number_is_a_422_not_a_500(client: TestClient, db_session: Session, case: str, door: str):
    """DEFECT REPRODUCTION (fails until fixed). ``coerce_value`` refuses a non-finite number
    ("is not a finite number"), but the 422 echoes the request body in the error's
    ``input``, and the JSON response cannot encode NaN / Infinity ("Out of range float
    values are not JSON compliant") -- so the refusal itself answers 500. Same class as the
    unencodable-text 422 that ``main._encodable_validation_errors`` now scrubs; non-finite
    floats are not scrubbed."""
    manager = make_user(db_session)
    filters = _NON_FINITE_BODIES[case]
    definition = f'"data_source":"work_orders","columns":[{{"field":"work_order_number"}}],"filters":[{filters}]'
    path, body = (REPORT, "{" + definition + "}") if door == "run" else (TEMPLATES, '{"name":"T",' + definition + "}")
    # The ``client`` fixture has installed the test database; this client reports a 500
    # as a response instead of re-raising it.
    lenient = TestClient(client.app, raise_server_exceptions=False)
    resp = lenient.post(path, headers={**headers_for(manager), "Content-Type": "application/json"}, content=body)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, f"{door} {case}: {resp.status_code} {resp.text}"
    assert "is not a finite number" in resp.text
    assert db_session.query(ReportTemplate).count() == 0


def test_a_decimal_aggregate_is_a_rounded_json_number():
    """Postgres answers ``avg()`` over an integer column with ``numeric`` (a ``Decimal``);
    SQLite never does, so the suite would not otherwise reach this branch."""
    service = ReportBuilderService(None)  # _format_value touches no session
    for raw, expected in ((Decimal("8.5000000000000000"), 8.5), (Decimal("1000.756"), 1000.76), (Decimal("3"), 3)):
        formatted = service._format_value(raw)
        assert type(formatted) is float and formatted == expected
        json.dumps(formatted)


# ===========================================================================
# A voided NCR, through the real void path
# ===========================================================================


def test_an_ncr_voided_through_void_ncr_never_appears(db_session: Session):
    """``void_ncr`` soft-deletes AND sets status VOID. The catalog still offers ``void`` as a
    status option, so the strongest check is that asking for it finds nothing."""
    manager = make_user(db_session)
    case = CASES[ReportDataSource.QUALITY]
    live = seed_ncr(
        db_session,
        COMPANY_A,
        {"ncr_number": "RBQ-NCR-LIVE", "status": "open", "source": "in_process", "disposition": "pending"},
    )
    voided = seed_ncr(
        db_session,
        COMPANY_A,
        {"ncr_number": "RBQ-NCR-VOID", "status": "open", "source": "in_process", "disposition": "pending"},
    )
    db_session.commit()
    void_ncr(db_session, COMPANY_A, voided.id, "entered twice", manager, AuditService(db_session, manager))
    db_session.expire_all()
    assert db_session.query(NonConformanceReport).filter_by(id=voided.id).one().status == NCRStatus.VOID

    def numbers(**body) -> List[str]:
        return ids(run_service(db_session, case, columns=[{"field": "ncr_number"}], **body), "ncr_number")

    assert numbers() == [live.ncr_number]
    assert numbers(filters=[_f("status", "eq", "void")]) == []
    assert numbers(filters=[_f("status", "in", ["void", "open"])]) == [live.ncr_number]
    statuses = run_service(
        db_session,
        case,
        columns=[{"field": "status"}, {"field": "ncr_number", "aggregate": "count", "alias": "n"}],
        group_by=[{"field": "status"}],
    )
    assert statuses == [{"status": "open", "n": 1}]


# ===========================================================================
# CSV export: header row from tenant-authored aliases
# ===========================================================================


def _template(db: Session, owner: User, *, shared: bool = False, **definition) -> ReportTemplate:
    template = ReportTemplate(
        name=definition.pop("name", f"RBQ Template {_next()}"),
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


def test_export_header_neutralizes_formula_aliases_and_keeps_every_row_aligned(client: TestClient, db_session: Session):
    manager = make_user(db_session)
    _wo(db_session, customer="=HYPERLINK(\"http://evil.test\")", quantity=1, actual_cost=4)
    _wo(db_session, customer="Plain Customer", quantity=1, actual_cost=-2.5)
    template = _template(
        db_session,
        manager,
        columns=[
            {"field": "customer_name", "alias": "=cmd|' /C calc'!A0"},
            {"field": "actual_cost", "alias": "Cost, \"actual\""},
            {"field": "status", "alias": "@status"},
        ],
        sort=[{"field": "actual_cost", "direction": "asc"}],
    )
    resp = client.get(f"{EXPORT}?template_id={template.id}", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    table = list(csv.reader(io.StringIO(resp.text)))
    # Header: formula-initiating aliases gain a leading quote; an ordinary one with a comma
    # and quotes survives RFC 4180 quoting byte-exact. The header is the output keys, not labels.
    assert table[0] == ["'=cmd|' /C calc'!A0", 'Cost, "actual"', "'@status"]
    # Rows line up under the header, values neutralized the same way; a plain negative
    # number is a number, not a formula.
    assert table[1:] == [
        ["Plain Customer", "-2.5", "in_progress"],
        ["'=HYPERLINK(\"http://evil.test\")", "4.0", "in_progress"],
    ]


# ===========================================================================
# Saved-template access: every verb, every kind of caller
# ===========================================================================

READ = report_template_service.READ_DENIED_DETAIL
MODIFY = report_template_service.MODIFY_DENIED_DETAIL
ROLE = "Insufficient permissions"
GONE = report_template_service.NOT_FOUND_DETAIL


@dataclass(frozen=True)
class Caller:
    role: UserRole
    company_id: int = COMPANY_A
    is_superuser: bool = False
    # A platform principal's token may name another active company.
    active_company_id: Optional[int] = None
    # The caller IS the templates' author (with this role).
    is_author: bool = False


# (caller, expected outcome per verb on the PRIVATE and the SHARED template).
# An outcome is 200 or (status, detail). Read verbs: get, run, export. Write verbs: patch, delete.
_OWN = {"read": (200, 200), "write": (200, 200)}
_PEER = {"read": ((403, READ), 200), "write": ((403, MODIFY), (403, MODIFY))}
# One admin in both rules: an admin who may change any template (and be answered with its
# whole definition) may read, run and export any template too. The list is unchanged:
# an admin still LISTS only its own and the shared ones, and opens a colleague's by id.
_ADMIN = {"read": (200, 200), "write": (200, 200)}
_ROLE_GATE = {"read": ((403, ROLE), (403, ROLE)), "write": ((403, ROLE), (403, ROLE))}
_FOREIGN = {"read": ((404, GONE), (404, GONE)), "write": ((404, GONE), (404, GONE))}

ACCESS_MATRIX: Dict[str, Tuple[Caller, dict, Tuple[bool, bool]]] = {
    # name: (caller, outcomes, (lists the private template, lists the shared one))
    "author manager": (Caller(UserRole.MANAGER, is_author=True), _OWN, (True, True)),
    "author admin": (Caller(UserRole.ADMIN, is_author=True), _OWN, (True, True)),
    "another manager": (Caller(UserRole.MANAGER), _PEER, (False, True)),
    "admin": (Caller(UserRole.ADMIN), _ADMIN, (False, True)),
    "platform admin": (Caller(UserRole.PLATFORM_ADMIN), _ADMIN, (False, True)),
    "superuser manager": (Caller(UserRole.MANAGER, is_superuser=True), _ADMIN, (False, True)),
    # A Platform Admin whose home is B, switched INTO company A, is an admin there.
    "platform admin switched in": (
        Caller(UserRole.PLATFORM_ADMIN, company_id=COMPANY_B, active_company_id=COMPANY_A),
        _ADMIN,
        (False, True),
    ),
    # The listing is open to every role; nothing else is.
    "supervisor": (Caller(UserRole.SUPERVISOR), _ROLE_GATE, (False, True)),
    "viewer": (Caller(UserRole.VIEWER), _ROLE_GATE, (False, True)),
    # Authorship is not a role: an author since demoted is refused by the role gate.
    "author demoted to supervisor": (Caller(UserRole.SUPERVISOR, is_author=True), _ROLE_GATE, (True, True)),
    "foreign manager": (Caller(UserRole.MANAGER, company_id=COMPANY_B), _FOREIGN, (False, False)),
    "foreign admin": (Caller(UserRole.ADMIN, company_id=COMPANY_B), _FOREIGN, (False, False)),
    # A Platform Admin of company A switched OUT to B sees A's templates as any foreigner does.
    "platform admin switched out": (
        Caller(UserRole.PLATFORM_ADMIN, active_company_id=COMPANY_B),
        _FOREIGN,
        (False, False),
    ),
}


def _committed_template_actions() -> List[Tuple[str, int]]:
    with fresh_session() as verify:
        return [
            (row.action, row.resource_id)
            for row in verify.query(AuditLog).filter(AuditLog.resource_type == "report_template").order_by(AuditLog.id)
        ]


def _check(resp, outcome, label: str) -> None:
    if outcome == 200:
        assert resp.status_code == status.HTTP_200_OK, f"{label}: {resp.status_code} {resp.text}"
        return
    code, detail = outcome
    assert (resp.status_code, resp.json()["detail"]) == (code, detail), f"{label}: {resp.text}"


@pytest.mark.parametrize("name", sorted(ACCESS_MATRIX))
def test_template_access_matrix(client: TestClient, db_session: Session, name: str):
    caller_spec, outcomes, listed = ACCESS_MATRIX[name]
    _ensure_company(db_session, COMPANY_B)
    caller = make_user(
        db_session, role=caller_spec.role, company_id=caller_spec.company_id, is_superuser=caller_spec.is_superuser
    )
    author = caller if caller_spec.is_author else make_user(db_session)
    _wo(db_session, customer="Access Matrix", quantity=1)
    private = _template(db_session, author, name="Matrix Private")
    shared = _template(db_session, author, shared=True, name="Matrix Shared")
    headers = headers_for(caller, company_id=caller_spec.active_company_id)

    listing = client.get(TEMPLATES, headers=headers)
    assert listing.status_code == status.HTTP_200_OK, listing.text
    names = {t["name"] for t in listing.json()}
    assert ("Matrix Private" in names, "Matrix Shared" in names) == listed

    for template, read_outcome in zip((private, shared), outcomes["read"]):
        label = f"{name} / {template.name}"
        _check(client.get(f"{TEMPLATES}/{template.id}", headers=headers), read_outcome, f"{label} GET")
        _check(client.post(f"{TEMPLATES}/{template.id}/run", headers=headers), read_outcome, f"{label} run")
        _check(client.get(f"{EXPORT}?template_id={template.id}", headers=headers), read_outcome, f"{label} export")

    exports = [row.resource_id for row in committed_export_rows("custom_report")]
    assert exports == [t.id for t, o in zip((private, shared), outcomes["read"]) if o == 200]

    for template, write_outcome in zip((private, shared), outcomes["write"]):
        label = f"{name} / {template.name}"
        _check(
            client.patch(f"{TEMPLATES}/{template.id}", headers=headers, json={"description": "touched"}),
            write_outcome,
            f"{label} PATCH",
        )
        _check(client.delete(f"{TEMPLATES}/{template.id}", headers=headers), write_outcome, f"{label} DELETE")

    # A refused write changed nothing and audited nothing; an allowed one did both.
    db_session.expire_all()
    for template, write_outcome in zip((private, shared), outcomes["write"]):
        still_there = db_session.query(ReportTemplate).filter(ReportTemplate.id == template.id).count() == 1
        assert still_there is (write_outcome != 200), f"{name} / {template.name} row"
    audited = _committed_template_actions()
    expected_audit = [
        (action, t.id)
        for t, o in zip((private, shared), outcomes["write"])
        if o == 200
        for action in ("UPDATE", "DELETE")
    ]
    assert audited == expected_audit


def test_a_foreign_template_is_byte_identical_to_a_missing_one(client: TestClient, db_session: Session):
    """No status, header or body difference tells an existing foreign id from a free one."""
    owner = make_user(db_session)
    foreign = make_user(db_session, company_id=COMPANY_B)
    template = _template(db_session, owner, shared=True)
    missing = template.id + 1000
    headers = headers_for(foreign)
    for method, path in (
        ("GET", f"{TEMPLATES}/{{id}}"),
        ("POST", f"{TEMPLATES}/{{id}}/run"),
        ("PATCH", f"{TEMPLATES}/{{id}}"),
        ("DELETE", f"{TEMPLATES}/{{id}}"),
        ("GET", f"{EXPORT}?template_id={{id}}"),
    ):
        kwargs = {"json": {"name": "x"}} if method == "PATCH" else {}
        real = client.request(method, path.format(id=template.id), headers=headers, **kwargs)
        free = client.request(method, path.format(id=missing), headers=headers, **kwargs)
        assert (real.status_code, real.content) == (free.status_code, free.content) == (404, free.content), path
        assert real.headers.get("content-type") == free.headers.get("content-type")


def test_running_a_report_or_a_template_writes_no_audit_row(client: TestClient, db_session: Session):
    """A run is a read: neither run verb leaves anything in the audit log."""
    manager = make_user(db_session)
    _wo(db_session, customer="Read Only", quantity=1)
    template = _template(db_session, manager)
    with fresh_session() as verify:
        before = verify.query(AuditLog).count()
    body = {"data_source": "work_orders", "columns": [{"field": "work_order_number"}]}
    assert client.post(REPORT, headers=headers_for(manager), json=body).status_code == 200
    assert client.post(f"{TEMPLATES}/{template.id}/run", headers=headers_for(manager)).status_code == 200
    assert client.get(f"{TEMPLATES}/{template.id}", headers=headers_for(manager)).status_code == 200
    assert client.get(TEMPLATES, headers=headers_for(manager)).status_code == 200
    with fresh_session() as verify:
        assert verify.query(AuditLog).count() == before


def _mint(client: TestClient, admin_headers: dict, user: User, **scope) -> Tuple[int, dict]:
    resp = client.post(
        "/api/v1/api-tokens/", json={"user_id": user.id, "label": f"reports {_next()}", **scope}, headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], {"Authorization": f"Bearer {body['token']}", "X-Requested-With": "XMLHttpRequest"}


def test_api_tokens_on_the_report_routes(client: TestClient, db_session: Session, admin_headers: dict, test_user: User):
    """The documented credential posture (RBAC_PERMISSIONS.md -> Custom reports): no report
    route refuses an API token, so a Manager-bound FULL token builds, saves and exports --
    its template write attributed to the token; a READ-ONLY token reads the catalog and the
    templates but cannot run (both run verbs are POST) or download the CSV, and its refused
    download leaves no EXPORT row."""
    _wo(db_session, customer="Token Customer", quantity=1)
    run_body = {"data_source": "work_orders", "columns": [{"field": "work_order_number"}]}

    full_id, full = _mint(client, admin_headers, test_user)
    assert client.post(REPORT, headers=full, json=run_body).status_code == 200
    created = client.post(TEMPLATES, headers=full, json={"name": "Token template", **run_body})
    assert created.status_code == 200, created.text
    template_id = created.json()["id"]
    assert client.get(f"{EXPORT}?template_id={template_id}", headers=full).status_code == 200
    with fresh_session() as verify:
        create_row = (
            verify.query(AuditLog)
            .filter(AuditLog.resource_type == "report_template", AuditLog.action == "CREATE")
            .one()
        )
        assert create_row.user_id == test_user.id
        assert create_row.extra_data["credential"]["api_token_id"] == full_id
    assert [row.user_id for row in committed_export_rows("custom_report")] == [test_user.id]

    _ro_id, read_only = _mint(client, admin_headers, test_user, scope="read_only", ai_external=False)
    for path in ("/api/v1/analytics/data-sources", TEMPLATES, f"{TEMPLATES}/{template_id}"):
        assert client.get(path, headers=read_only).status_code == 200, path
    assert client.post(REPORT, headers=read_only, json=run_body).status_code == 403
    assert client.post(f"{TEMPLATES}/{template_id}/run", headers=read_only).status_code == 403
    refused = client.get(f"{EXPORT}?template_id={template_id}", headers=read_only)
    assert refused.status_code == 403 and b"Token Customer" not in refused.content
    assert refused.headers["content-type"].startswith("application/json")
    assert len(committed_export_rows("custom_report")) == 1, "a refused download recorded an export"


# ===========================================================================
# The client's copy of the server's caps and operator tables
# ===========================================================================

REPORT_DEFINITION_TS = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "src"
    / "components"
    / "analytics"
    / "reportBuilder"
    / "reportDefinition.ts"
)


def _ts_number(source: str, name: str) -> int:
    match = re.search(rf"export const {name} = ([0-9_]+);", source)
    assert match, f"{name} not found in reportDefinition.ts"
    return int(match.group(1).replace("_", ""))


def _ts_table(source: str, name: str) -> Dict[str, List[str]]:
    match = re.search(rf"const {name}[^=]*= \{{(.*?)\n\}};", source, re.S)
    assert match, f"{name} not found in reportDefinition.ts"
    table = {}
    for key, values in re.findall(r"(\w+): \[([^\]]*)\]", match.group(1)):
        table[key] = re.findall(r"'(\w+)'", values)
    return table


def test_client_caps_and_operator_tables_match_the_server():
    """The builder offers only what the server accepts (its docstring's promise). It holds its
    own copies of the caps and tables; this keeps them equal to the server's."""
    source = REPORT_DEFINITION_TS.read_text(encoding="utf-8")
    assert {
        "DEFAULT_LIMIT": _ts_number(source, "DEFAULT_LIMIT"),
        "MAX_LIMIT": _ts_number(source, "MAX_LIMIT"),
        "MAX_COLUMNS": _ts_number(source, "MAX_COLUMNS"),
        "MAX_FILTERS": _ts_number(source, "MAX_FILTERS"),
        "MAX_GROUP_BY": _ts_number(source, "MAX_GROUP_BY"),
        "MAX_SORT": _ts_number(source, "MAX_SORT"),
        "MAX_ALIAS_LENGTH": _ts_number(source, "MAX_ALIAS_LENGTH"),
        "MAX_TEXT_VALUE_LENGTH": _ts_number(source, "MAX_TEXT_VALUE_LENGTH"),
        "MAX_IN_VALUES": _ts_number(source, "MAX_IN_VALUES"),
    } == {
        "DEFAULT_LIMIT": REPORT_DEFAULT_LIMIT,
        "MAX_LIMIT": REPORT_MAX_LIMIT,
        "MAX_COLUMNS": REPORT_MAX_COLUMNS,
        "MAX_FILTERS": REPORT_MAX_FILTERS,
        "MAX_GROUP_BY": REPORT_MAX_GROUP_BY,
        "MAX_SORT": REPORT_MAX_SORT,
        "MAX_ALIAS_LENGTH": report_builder.MAX_ALIAS_LENGTH,
        "MAX_TEXT_VALUE_LENGTH": report_builder.MAX_TEXT_VALUE_LENGTH,
        "MAX_IN_VALUES": report_builder.MAX_IN_VALUES,
    }

    operators = _ts_table(source, "OPERATORS_BY_TYPE")
    assert operators == {kind: list(ops) for kind, ops in OPERATORS_BY_TYPE.items()}

    aggregates = _ts_table(source, "AGGREGATES_BY_TYPE")
    server_aggregates = {
        kind: sorted(agg.value for agg, kinds in AGGREGATE_TYPES.items() if kind in kinds) for kind in OPERATORS_BY_TYPE
    }
    assert {kind: sorted(aggs) for kind, aggs in aggregates.items()} == server_aggregates
