"""Authorization + audit coverage for bulk data exports.

Bulk export is its own access category, distinct from a domain read
(``docs/RBAC_PERMISSIONS.md`` -> Bulk data export). The reads behind these
datasets stay deliberately read-broad; what is gated here is handing the WHOLE
dataset over as a file in one request -- the parts master with
``standard_cost``, the full inventory valuation, every PO line with
``unit_price`` and vendor, every quote with its customer contacts.

``docs/RBAC_PERMISSIONS.md`` requires that a least-privilege change ship *with
authorization tests*. This file is that requirement. Every test here is written
to FAIL against the pre-change code (``Depends(get_current_user)``, no audit
row); the handful that would also have passed before are the ones that guard the
new code's own failure modes, and each says so in its docstring.

Three properties are asserted for every ``/api/v1/exports/*`` route:

1. ``require_role([ADMIN, MANAGER])`` -- matching the analytics custom-report
   export, the system's other general-purpose exporter. SUPERVISOR is included in
   the refusal cases on purpose: it is the role that can *read* every one of
   these datasets in the UI, so it is the case that proves the gate is about the
   bulk file rather than about the underlying read.
2. An ``EXPORT`` audit row per successful export, written through
   ``AuditService`` (invariant 2), recording the REQUEST -- dataset, row count,
   format, disclosed columns, applied filters -- and never the payload. A refusal
   must leave no row: a 403 disclosed nothing.
3. The row is **committed**, not merely flushed. This is asserted through
   ``committed_export_rows`` (``tests/api/export_audit_helpers.py`` -- a second
   engine, hence a second connection), never through the ``db_session`` the app
   itself is using; see that module's docstring for why reading back through the
   shared session cannot tell the two apart.

The last section is the over-gating regression guard: the single-record document
routes an operator needs on the floor (the traveler, the shop drawing) are
deliberately NOT bulk exports and must keep working unauthenticated-by-role.

Fixture conventions follow ``test_exports_tenant_isolation.py``.
"""

from datetime import date, datetime

import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from tests.api.export_audit_helpers import committed_export_rows

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

# (route, resource_type the audit row must carry)
EXPORT_ROUTES = [
    ("/api/v1/exports/work-orders/export", "work_order"),
    ("/api/v1/exports/parts/export", "part"),
    ("/api/v1/exports/inventory/export", "inventory_item"),
    ("/api/v1/exports/purchase-orders/export", "purchase_order"),
    ("/api/v1/exports/purchase-orders/lines/export", "purchase_order_line"),
    ("/api/v1/exports/quotes/export", "quote"),
    ("/api/v1/exports/inventory/transactions/export", "inventory_transaction"),
]

# Every role that must be refused. SUPERVISOR can read all of these datasets in
# the UI; that is precisely why it belongs here.
REFUSED_ROLES = [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER]
ALLOWED_ROLES = [UserRole.ADMIN, UserRole.MANAGER]

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


def make_user(db: Session, *, role: UserRole, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"exp-gate-{n}@co{company_id}.test",
        employee_id=f"EXPG-{n:05d}",
        first_name="Export",
        last_name="Gate",
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


def make_part(db: Session, *, company_id: int = COMPANY_A, standard_cost: float = 12.34) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"EXPG-PART-{n:05d}",
        name=f"Export gate fixture part {n}",
        part_type="purchased",
        unit_of_measure="each",
        standard_cost=standard_cost,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


# ===========================================================================
# 1. The gate -- parameterized over every route x every role
# ===========================================================================


@pytest.mark.parametrize("route,resource_type", EXPORT_ROUTES)
@pytest.mark.parametrize("role", REFUSED_ROLES)
def test_export_refuses_below_admin_manager(
    client: TestClient, db_session: Session, route: str, resource_type: str, role: UserRole
):
    """Every bulk export refuses 403 below the ADMIN/MANAGER tier.

    Pre-change these routes carried ``Depends(get_current_user)`` only, so every
    role here got 200 and a file. This is the core assertion of the change.
    """
    headers = headers_for(make_user(db_session, role=role))
    response = client.get(route, headers=headers, params={"format": "csv"})

    assert response.status_code == http_status.HTTP_403_FORBIDDEN, f"{route} as {role.value}: {response.text}"
    assert "attachment" not in response.headers.get("content-disposition", "")


@pytest.mark.parametrize("route,resource_type", EXPORT_ROUTES)
@pytest.mark.parametrize("role", ALLOWED_ROLES)
def test_export_allows_admin_and_manager_and_audits_the_disclosure(
    client: TestClient, db_session: Session, route: str, resource_type: str, role: UserRole
):
    """ADMIN and MANAGER still get the file -- and the file is now on record.

    The 200 half alone would pass against the old code (which let everyone
    through); it is here as the anti-over-gating complement. The audit half is
    what makes this test fail pre-change: the old handlers wrote no row at all.
    """
    headers = headers_for(make_user(db_session, role=role))
    response = client.get(route, headers=headers, params={"format": "csv"})

    assert response.status_code == http_status.HTTP_200_OK, f"{route} as {role.value}: {response.text}"
    assert "attachment" in response.headers["content-disposition"]
    assert len(committed_export_rows(resource_type)) == 1, f"{route} as {role.value} left no EXPORT audit row"


def test_export_gate_admits_a_platform_admin_and_still_records_the_disclosure(client: TestClient, db_session: Session):
    """``require_role`` also admits PLATFORM_ADMIN, exactly as every other gate does.

    Pinned so the tier is described accurately in ``docs/RBAC_PERMISSIONS.md``
    rather than inferred from the literal role list. The audit half is what makes
    this fail pre-change, and it is the half that matters here: a platform admin
    is by definition operating inside a tenant that is not their own, so their
    bulk export is the one most in need of a record. ``user_id`` is asserted so
    the row names them rather than the tenant.
    """
    user = make_user(db_session, role=UserRole.PLATFORM_ADMIN)
    response = client.get("/api/v1/exports/parts/export", headers=headers_for(user), params={"format": "csv"})

    assert response.status_code == http_status.HTTP_200_OK, response.text
    rows = committed_export_rows("part")
    assert len(rows) == 1, "a platform admin's export left no record"
    assert rows[0].user_id == user.id


@pytest.mark.parametrize("route,resource_type", EXPORT_ROUTES)
def test_export_refuses_an_anonymous_caller(client: TestClient, route: str, resource_type: str):
    """No token, no file.

    This one would also pass pre-change (``get_current_user`` rejected anonymous
    callers too). It is kept because the role gate REPLACED that dependency:
    without it, a ``require_role`` that somehow resolved a ``None`` user would
    not be caught by any other test in this file.
    """
    response = client.get(route, params={"format": "csv"})
    assert response.status_code in (http_status.HTTP_401_UNAUTHORIZED, http_status.HTTP_403_FORBIDDEN)


# ===========================================================================
# 2. The audit row
# ===========================================================================


@pytest.mark.parametrize("route,resource_type", EXPORT_ROUTES)
def test_every_export_commits_exactly_one_audit_row(
    client: TestClient, db_session: Session, route: str, resource_type: str
):
    """One export, one committed ``EXPORT`` row carrying the right dataset + actor.

    Read from ``fresh_session()``: a flush-without-commit would be discarded by
    ``get_db``'s teardown in production, so committing is the property, not
    "the ORM knows about a row".
    """
    user = make_user(db_session, role=UserRole.MANAGER)
    assert committed_export_rows(resource_type) == []

    response = client.get(route, headers=headers_for(user), params={"format": "csv"})
    assert response.status_code == http_status.HTTP_200_OK, response.text

    rows = committed_export_rows(resource_type)
    assert len(rows) == 1, f"{route} committed {len(rows)} EXPORT rows"
    row = rows[0]
    assert row.action == "EXPORT"
    assert row.resource_type == resource_type
    assert row.user_id == user.id
    assert row.company_id == COMPANY_A
    assert row.description and "record(s)" in row.description


@pytest.mark.parametrize("route,resource_type", EXPORT_ROUTES)
def test_refused_export_commits_no_audit_row(client: TestClient, db_session: Session, route: str, resource_type: str):
    """A 403 disclosed nothing, so it must not leave a disclosure record.

    Trivially true pre-change (nothing was ever logged). It is here to catch the
    new code's own failure mode: an audit call wired ahead of the role check, or
    a gate implemented in the body instead of as a dependency, would log a
    disclosure that never happened -- into rows that are immutable and
    undeletable.
    """
    headers = headers_for(make_user(db_session, role=UserRole.OPERATOR))
    response = client.get(route, headers=headers, params={"format": "csv"})

    assert response.status_code == http_status.HTTP_403_FORBIDDEN
    assert committed_export_rows() == []


def test_audit_row_is_committed_before_the_file_is_streamed(client: TestClient, db_session: Session):
    """The disclosure is on record even if the client never reads the body.

    ``client.get`` consumes the whole response, so the streaming order is forced
    explicitly here: the row must be visible on a separate connection while the
    ``StreamingResponse`` body is still being iterated.
    """
    make_part(db_session)
    user = make_user(db_session, role=UserRole.MANAGER)

    with client.stream(
        "GET", "/api/v1/exports/parts/export", headers=headers_for(user), params={"format": "csv"}
    ) as response:
        assert response.status_code == http_status.HTTP_200_OK
        assert len(committed_export_rows("part")) == 1
        response.read()


def test_audit_row_records_row_count_and_format(client: TestClient, db_session: Session):
    make_part(db_session)
    make_part(db_session)
    user = make_user(db_session, role=UserRole.ADMIN)

    response = client.get("/api/v1/exports/parts/export", headers=headers_for(user), params={"format": "xlsx"})
    assert response.status_code == http_status.HTTP_200_OK

    row = committed_export_rows("part")[0]
    assert "Exported 2 part record(s) to XLSX" == row.description
    assert row.new_values["format"] == "xlsx"


def test_audit_row_records_requested_filters_not_payload(client: TestClient, db_session: Session):
    """The row describes the query, never the rows it returned."""
    part = make_part(db_session, standard_cost=987.65)
    user = make_user(db_session, role=UserRole.MANAGER)

    response = client.get(
        "/api/v1/exports/purchase-orders/export",
        headers=headers_for(user),
        params={
            "format": "csv",
            "status": "sent",
            "start_date": date(2026, 1, 1).isoformat(),
            "end_date": date(2026, 12, 31).isoformat(),
        },
    )
    assert response.status_code == http_status.HTTP_200_OK

    row = committed_export_rows("purchase_order")[0]
    filters = row.new_values["filters"]
    assert filters["status"] == "sent"  # str-backed enum recorded as its value
    assert filters["start_date"] == "2026-01-01"
    assert filters["end_date"] == "2026-12-31"
    assert "vendor_id" not in filters  # unset filters are omitted, not logged as null

    # No payload: nothing identifying an exported record is in the row.
    serialized = f"{row.description} {row.new_values} {row.old_values} {row.extra_data}"
    assert part.part_number not in serialized
    assert "987.65" not in serialized


def test_audit_row_records_the_columns_that_were_disclosed(client: TestClient, db_session: Session):
    make_part(db_session)
    user = make_user(db_session, role=UserRole.MANAGER)

    response = client.get(
        "/api/v1/exports/parts/export",
        headers=headers_for(user),
        params={"format": "csv", "columns": ["part_number", "standard_cost"]},
    )
    assert response.status_code == http_status.HTTP_200_OK

    row = committed_export_rows("part")[0]
    assert row.new_values["columns"] == ["part_number", "standard_cost"]


def test_audit_row_drops_unrecognized_caller_columns(client: TestClient, db_session: Session):
    """``columns`` is caller-supplied and an audit row is immutable and undeletable.

    Unrecognized text must not be able to write itself into the hash chain, so
    only columns the endpoint actually recognizes are recorded.
    """
    make_part(db_session)
    user = make_user(db_session, role=UserRole.MANAGER)

    response = client.get(
        "/api/v1/exports/parts/export",
        headers=headers_for(user),
        params={"format": "csv", "columns": ["part_number", "x" * 500]},
    )
    assert response.status_code == http_status.HTTP_200_OK

    row = committed_export_rows("part")[0]
    assert row.new_values["columns"] == ["part_number"]
    assert "x" * 500 not in str(row.new_values)


# Every free-text export filter, with the bound it is declared at and the
# ``resource_type`` its audit row carries.
BOUNDED_FILTERS = [
    ("/api/v1/exports/inventory/export", "inventory_item", "warehouse", 50),
    ("/api/v1/exports/quotes/export", "quote", "customer", 255),
    ("/api/v1/exports/inventory/transactions/export", "inventory_transaction", "transaction_type", 40),
]


@pytest.mark.parametrize("route,resource_type,param,limit", BOUNDED_FILTERS)
def test_oversized_filter_value_is_refused_and_writes_no_audit_row(
    client: TestClient, db_session: Session, route: str, resource_type: str, param: str, limit: int
):
    """Filter values are caller-supplied, and once exports are audited they are permanent.

    ``columns`` was already fenced by allowlist. A filter *value* is free text by
    definition, so it is fenced by length instead -- and it has to be fenced
    somewhere, because an ``audit_log`` row is un-UPDATE-able and un-DELETE-able
    (the 008/060 triggers) and is covered by the integrity hash. Unbounded caller
    text would be unbounded, unremovable text in the chain, appendable once per
    export call with no remedy. Before the ``max_length`` declarations a
    50,000-character filter was recorded verbatim.
    """
    user = make_user(db_session, role=UserRole.MANAGER)

    response = client.get(route, headers=headers_for(user), params={"format": "csv", param: "x" * (limit + 1)})

    assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_CONTENT
    # A refusal disclosed nothing, so it records nothing -- and in particular the
    # rejected text does not reach the chain by way of the audit row.
    assert committed_export_rows(resource_type) == []


@pytest.mark.parametrize("route,resource_type,param,limit", BOUNDED_FILTERS)
def test_filter_value_at_the_bound_is_accepted_and_recorded_verbatim(
    client: TestClient, db_session: Session, route: str, resource_type: str, param: str, limit: int
):
    """The bound sits at the column width, so nothing that could match a row is clipped.

    Non-vacuous counterpart to the test above: this fails if a bound is ever
    tightened below the width of the data it filters on.
    """
    user = make_user(db_session, role=UserRole.MANAGER)
    value = "y" * limit

    response = client.get(route, headers=headers_for(user), params={"format": "csv", param: value})

    assert response.status_code == http_status.HTTP_200_OK
    row = committed_export_rows(resource_type)[0]
    assert row.new_values["filters"][param] == value


def test_audit_seam_caps_a_filter_value_that_reaches_it_unbounded():
    """Backstop for a future exporter that forgets ``max_length`` on its own filter.

    The endpoints bound their inputs at the edge, which is where a caller earns an
    honest 422. This asserts the seam does not *depend* on their doing so: text
    that arrives long is truncated and marked rather than written whole.
    """
    from app.services.export_audit import _MAX_FILTER_VALUE_CHARS, _json_safe

    capped = _json_safe("z" * (_MAX_FILTER_VALUE_CHARS + 5_000))

    assert len(capped) < _MAX_FILTER_VALUE_CHARS + 50
    assert capped.endswith("...[truncated]")
    # A value inside the bound is untouched -- the cap must not rewrite real filters.
    assert _json_safe("z" * _MAX_FILTER_VALUE_CHARS) == "z" * _MAX_FILTER_VALUE_CHARS


def test_default_export_records_the_default_column_set(client: TestClient, db_session: Session):
    """No ``columns`` param still records what left the building."""
    make_part(db_session)
    user = make_user(db_session, role=UserRole.MANAGER)

    response = client.get("/api/v1/exports/parts/export", headers=headers_for(user), params={"format": "csv"})
    assert response.status_code == http_status.HTTP_200_OK

    row = committed_export_rows("part")[0]
    assert "standard_cost" in row.new_values["columns"]
    assert "part_number" in row.new_values["columns"]


def test_each_repeated_export_is_its_own_audit_row(client: TestClient, db_session: Session):
    """Three downloads are three disclosures, not one deduplicated event."""
    make_part(db_session)
    user = make_user(db_session, role=UserRole.MANAGER)

    for _ in range(3):
        response = client.get("/api/v1/exports/parts/export", headers=headers_for(user), params={"format": "csv"})
        assert response.status_code == http_status.HTTP_200_OK

    assert len(committed_export_rows("part")) == 3


# ===========================================================================
# 3. Over-gating regression guard
#
# The decision drew the line at "a whole dataset as a file". Single-record
# document routes are on the other side of it, and two of them are load-bearing
# on the shop floor: an OPERATOR must still be able to pull the traveler for the
# job in front of them and open the controlled drawing at the point of use.
# Every test below fails if this change was applied to a route it should not
# have touched.
# ===========================================================================


READ_BROAD_LIST_ROUTES = [
    "/api/v1/parts/",
    "/api/v1/inventory/",
    "/api/v1/work-orders/",
    "/api/v1/quotes/",
    "/api/v1/purchasing/purchase-orders",
]


@pytest.mark.parametrize("route", READ_BROAD_LIST_ROUTES)
def test_the_domain_reads_behind_every_gated_export_stay_read_broad(
    client: TestClient, db_session: Session, route: str
):
    """The reads were NOT tiered -- only the bulk file was.

    ``docs/RBAC_PERMISSIONS.md`` directs that least-privilege on domain reads, if
    ever wanted, be done uniformly across modules rather than per-router. This
    change is allowed to sit alongside that directive precisely because it left
    the reads alone. If a later edit "tidies up" by pushing the export tier down
    onto the list endpoints these exports are built from, this is the test that
    catches it -- an OPERATOR loses the parts list, the stock list, the job list.
    """
    headers = headers_for(make_user(db_session, role=UserRole.OPERATOR))

    response = client.get(route, headers=headers)

    assert response.status_code == http_status.HTTP_200_OK, f"{route}: {response.status_code} {response.text}"


def make_work_order(db: Session, *, part: Part, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    work_order = WorkOrder(
        work_order_number=f"EXPG-WO-{n:05d}",
        part_id=part.id,
        quantity_ordered=10.0,
        status="released",
        priority=3,
        due_date=date.today(),
        customer_name="Fixture Customer",
        company_id=company_id,
        is_deleted=False,
    )
    db.add(work_order)
    db.commit()
    db.refresh(work_order)
    return work_order


def make_local_document(db: Session, tmp_path, *, part: Part, company_id: int = COMPANY_A) -> Document:
    """A released, part-linked DRAWING backed by a real file on disk.

    Both the shop-floor inline viewer and the generic download route stream the
    bytes at ``file_path``, so the file has to exist for a 200 to mean anything.
    """
    n = _next()
    pdf_path = tmp_path / f"expg-drawing-{n}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fixture\n")
    document = Document(
        document_number=f"EXPG-DOC-{n:05d}",
        title=f"Export gate fixture drawing {n}",
        document_type=DocumentType.DRAWING,
        status="released",
        part_id=part.id,
        file_name=pdf_path.name,
        file_path=str(pdf_path),
        mime_type="application/pdf",
        company_id=company_id,
        created_at=datetime.utcnow(),
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def test_operator_can_still_pull_a_work_order_traveler(client: TestClient, db_session: Session):
    """The traveler is one record through the UI -- a domain read, not an export."""
    part = make_part(db_session)
    work_order = make_work_order(db_session, part=part)
    headers = headers_for(make_user(db_session, role=UserRole.OPERATOR))

    response = client.get(f"/api/v1/print/work-orders/{work_order.id}/print-data", headers=headers)

    assert response.status_code == http_status.HTTP_200_OK, response.text
    assert response.json()["work_order_number"] == work_order.work_order_number


def test_operator_can_still_open_a_controlled_drawing_at_the_point_of_use(
    client: TestClient, db_session: Session, tmp_path
):
    """The kiosk document viewer is read-broad on purpose (``docs/KIOSK.md``)."""
    part = make_part(db_session)
    document = make_local_document(db_session, tmp_path, part=part)
    headers = headers_for(make_user(db_session, role=UserRole.OPERATOR))

    response = client.get(f"/api/v1/shop-floor/documents/{document.id}/inline", headers=headers)

    assert response.status_code == http_status.HTTP_200_OK, response.text
    assert response.headers["content-type"].startswith("application/pdf")


def test_operator_can_still_download_a_single_document(client: TestClient, db_session: Session, tmp_path):
    part = make_part(db_session)
    document = make_local_document(db_session, tmp_path, part=part)
    headers = headers_for(make_user(db_session, role=UserRole.OPERATOR))

    response = client.get(f"/api/v1/documents/{document.id}/download", headers=headers)

    assert response.status_code == http_status.HTTP_200_OK, response.text


def test_operator_can_still_download_a_static_import_template(client: TestClient, db_session: Session):
    """Zero tenant data in the workbook -- there is nothing here to gate."""
    headers = headers_for(make_user(db_session, role=UserRole.OPERATOR))

    response = client.get("/api/v1/import/templates/parts", headers=headers)

    assert response.status_code == http_status.HTTP_200_OK, response.text


def test_single_record_document_routes_are_not_role_gated(client: TestClient, db_session: Session):
    """An OPERATOR reaching a MISSING record must get 404, never 403.

    A role gate is a dependency, so it fires before the handler's lookup: a 403
    on a nonexistent id is the signature of one of these routes having been
    swept into the export tier. 404 proves no gate ran. Covers the byte-serving
    routes whose fixtures are heavier than the assertion warrants.
    """
    headers = headers_for(make_user(db_session, role=UserRole.OPERATOR))

    for route in (
        "/api/v1/laser-nests/999999/document",
        "/api/v1/shop-floor/documents/999999/inline",
        "/api/v1/documents/999999/download",
        "/api/v1/print/work-orders/999999/print-data",
        "/api/v1/print/quotes/999999/print-data",
        "/api/v1/print/purchase-orders/999999/print-data",
    ):
        response = client.get(route, headers=headers)
        assert response.status_code == http_status.HTTP_404_NOT_FOUND, f"{route}: {response.status_code}"


def test_estimate_workbench_export_keeps_its_own_supervisor_tier(client: TestClient, db_session: Session):
    """A single estimate's audit workbook is not a bulk export and was not re-tiered.

    ``/estimate-workbench/{id}/export/audit.xlsx`` is ``[ADMIN, MANAGER,
    SUPERVISOR]``. Had it been folded into the uniform ADMIN/MANAGER tier, the
    supervisor case below would be 403 instead of 404.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    route = "/api/v1/estimate-workbench/999999/export/audit.xlsx"

    assert client.get(route, headers=headers_for(supervisor)).status_code == http_status.HTTP_404_NOT_FOUND
    assert client.get(route, headers=headers_for(operator)).status_code == http_status.HTTP_403_FORBIDDEN


def test_visitor_log_export_keeps_its_own_pii_tier_and_audit(client: TestClient, db_session: Session):
    """The audit-shape precedent is untouched: still ADMIN/MANAGER, still audited.

    Visitor logs are a documented exception to read-broad because of visitor PII,
    so its tier is a PII decision rather than the bulk-export decision. This
    change copied its audit shape, not its rationale, and must not have loosened
    or re-derived either.
    """
    manager = make_user(db_session, role=UserRole.MANAGER)
    operator = make_user(db_session, role=UserRole.OPERATOR)

    assert (
        client.get("/api/v1/visitor-logs/export.csv", headers=headers_for(operator)).status_code
        == http_status.HTTP_403_FORBIDDEN
    )
    response = client.get("/api/v1/visitor-logs/export.csv", headers=headers_for(manager))
    assert response.status_code == http_status.HTTP_200_OK, response.text
    assert len(committed_export_rows("visitor_log")) == 1
