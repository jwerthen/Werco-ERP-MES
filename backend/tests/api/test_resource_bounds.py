"""Resource-bound regressions for list, pagination and export parameters.

Before this sweep roughly fifty route parameters were declared as a bare
``limit: int = 100`` / ``skip: int = 0`` / ``days: int = 30`` with no
``Query(...)`` validation at all, and three list endpoints plus six of the seven
exporters had no cap whatsoever. Two distinct failure modes:

- **No lower bound.** ``?limit=-1`` reached ``.limit(-1)``, which PostgreSQL
  rejects outright (500) while SQLite silently reads as *unbounded* -- the exact
  before/after that ``test_low_stock_rejects_non_positive_limit`` in
  ``test_inventory_hardening.py`` locks in for ``/inventory/low-stock``.
  ``?skip=-1`` is the same defect on OFFSET, which PostgreSQL also rejects. One
  site (``/bom/uom-mismatches``) feeds a Python slice rather than SQL, so a
  negative value there returned a silently *wrong short page* instead of
  erroring.
- **No upper bound.** ``?limit=99999999`` on any of them was a full-table read
  from a single authenticated request.

The ceilings are deliberately **generous** rather than tight page sizes: every
default is unchanged, so no caller in the field sees any difference, and only a
pathological request reaches the cap. For the three previously-uncapped list
endpoints the default IS the ceiling, so a zero-argument caller still receives
its whole set.

The exporters take the opposite posture: they **refuse** with 400 rather than
truncate, because a StreamingResponse of a spreadsheet has no channel to signal
"this file is partial" and these are documents a manager reconciles from.
``export_inventory_transactions`` previously truncated silently at a magic
``10000``; that is now the same named constant and the same refusal.

Also covered here, found while capping the customer endpoints:

- ``GET /customers/names`` returned **soft-deleted** customers into every
  quote / RFQ / order picker (soft-delete does not imply ``is_active=False``).
  The dropdown-facing half of that fix is asserted in
  ``test_soft_delete_read_sweep.py``; the bound is asserted here.
- ``GET /customers/?search=`` matched ``name`` only, while the Customers page's
  client-side filter matches name/code/contact_name/city. Widened server-side
  *ahead* of the frontend dropping its own filter, so searching by account code,
  buyer name or city cannot silently start returning zero rows.
- ``GET /purchasing/purchase-orders`` eagerly loaded every PO line
  (``selectinload``) purely to compute ``line_count=len(po.lines)`` for a
  response schema that never exposes the lines. That is now a correlated
  ``COUNT``; ``test_po_list_line_count_matches_actual_lines`` asserts the number
  did not move.
"""

from datetime import date

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.inventory import InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.user import UserRole
from tests.api.test_inventory_hardening import (
    COMPANY_A,
    _ensure_company,
    _next,
    headers_for,
    make_user,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Representative sample. Fifty-odd parameters were swept; exhaustively testing
# each would be fifty near-identical tests with no extra signal, so one or two
# are pinned per defect class and per distinct ceiling tier.
# ---------------------------------------------------------------------------

# (path, ceiling) -- one per ceiling tier plus the three newly-capped list
# endpoints. 5000 is the standard list tier; /bom/ is 10000 because
# PartsNew.tsx requests limit=5000 today; /mrp/runs is the small-analytic tier;
# /audit/ is a pre-existing le=500 that was missing its ge=.
BOUNDED_LIMIT_ENDPOINTS = [
    ("/api/v1/work-orders/", 5000),  # class A: bare `limit: int = 100`
    ("/api/v1/quality/ncr", 5000),  # class A
    ("/api/v1/documents/", 5000),  # class A
    ("/api/v1/bom/", 10000),  # class A, raised tier
    ("/api/v1/mrp/runs", 500),  # class A, small-analytic tier
    ("/api/v1/audit/", 500),  # class C: had le=, was missing ge=
    ("/api/v1/inventory/", 10000),  # newly capped list endpoint
    ("/api/v1/inventory/summary", 10000),  # capped WITH /inventory/, never apart
    ("/api/v1/customers/", 5000),  # newly capped list endpoint
    ("/api/v1/customers/names", 5000),  # newly capped dropdown feed
    ("/api/v1/purchasing/purchase-orders", 5000),  # newly capped list endpoint
]

# (path, param name) -- OFFSET has the same negative-value defect as LIMIT.
BOUNDED_OFFSET_ENDPOINTS = [
    ("/api/v1/work-orders/", "skip"),
    ("/api/v1/documents/", "skip"),
    ("/api/v1/quality/ncr", "skip"),
    ("/api/v1/audit/", "offset"),
    ("/api/v1/inventory/", "offset"),
    ("/api/v1/customers/", "offset"),
    ("/api/v1/purchasing/purchase-orders", "offset"),
]

# `days` widens a WHERE window; a zero/negative value inverts it and a huge one
# is an unbounded scan. All bounded to Query(30, ge=1, le=365), matching the
# in-repo reference at ai_usage.py.
BOUNDED_DAYS_ENDPOINTS = [
    "/api/v1/audit/summary",
    "/api/v1/calibration/equipment/due-soon",
    "/api/v1/receiving/stats",
    "/api/v1/certifications/certifications/expiring",
]


@pytest.mark.parametrize("path,ceiling", BOUNDED_LIMIT_ENDPOINTS)
def test_limit_rejects_non_positive_and_over_ceiling(client: TestClient, db_session: Session, path: str, ceiling: int):
    """``?limit=0`` / ``?limit=-1`` / ``?limit=<ceiling+1>`` must all be 422.

    A negative LIMIT is a PostgreSQL error and an unbounded read on SQLite; an
    unbounded upper end is a full-table read. Both ends of the range are pinned,
    and the ceiling itself must still be accepted so the bound is a ceiling and
    not an off-by-one.
    """
    headers = headers_for(make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN))

    for bad in (0, -1, ceiling + 1):
        resp = client.get(path, headers=headers, params={"limit": bad})
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, f"{path}?limit={bad} -> {resp.text}"

    for good in (1, ceiling):
        resp = client.get(path, headers=headers, params={"limit": good})
        assert resp.status_code == status.HTTP_200_OK, f"{path}?limit={good} -> {resp.text}"


@pytest.mark.parametrize("path,param", BOUNDED_OFFSET_ENDPOINTS)
def test_offset_rejects_negative(client: TestClient, db_session: Session, path: str, param: str):
    """A negative OFFSET is a PostgreSQL error, exactly like a negative LIMIT."""
    headers = headers_for(make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN))

    resp = client.get(path, headers=headers, params={param: -1})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, f"{path}?{param}=-1 -> {resp.text}"

    ok = client.get(path, headers=headers, params={param: 0})
    assert ok.status_code == status.HTTP_200_OK, ok.text


@pytest.mark.parametrize("path", BOUNDED_DAYS_ENDPOINTS)
def test_days_window_is_bounded(client: TestClient, db_session: Session, path: str):
    """``?days=`` must be a positive number of days, capped at a year."""
    headers = headers_for(make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN))

    for bad in (0, -1, 366):
        resp = client.get(path, headers=headers, params={"days": bad})
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, f"{path}?days={bad} -> {resp.text}"

    ok = client.get(path, headers=headers, params={"days": 365})
    assert ok.status_code == status.HTTP_200_OK, ok.text


def test_global_search_limit_is_bounded(client: TestClient, db_session: Session):
    """``/search/`` already carried ``le=50`` but admitted ``?limit=-1``."""
    headers = headers_for(make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN))

    for bad in (0, -1, 51):
        resp = client.get("/api/v1/search/", headers=headers, params={"q": "x", "limit": bad})
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text

    ok = client.get("/api/v1/search/", headers=headers, params={"q": "x", "limit": 50})
    assert ok.status_code == status.HTTP_200_OK, ok.text


def test_bom_uom_mismatch_report_rejects_negative_slice_bounds(client: TestClient, db_session: Session):
    """Guard, not a fix: this endpoint was ALREADY bounded, and must stay that way.

    ``/bom/uom-mismatches`` is the one paginated endpoint whose ``skip``/``limit``
    feed a Python slice (``passing[skip : skip + limit]``) rather than SQL, so a
    negative value here never raises -- it silently returns a wrong short page,
    which is worse than an error. It shipped with ``ge=0``/``ge=1`` already in
    place; pinning it stops a future refactor from dropping the bound on the one
    site where the database would not catch the mistake for us.
    """
    headers = headers_for(make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN))

    for params in ({"limit": 0}, {"limit": -1}, {"skip": -1}):
        resp = client.get("/api/v1/bom/uom-mismatches", headers=headers, params=params)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, f"{params} -> {resp.text}"

    ok = client.get("/api/v1/bom/uom-mismatches", headers=headers, params={"skip": 0, "limit": 1})
    assert ok.status_code == status.HTTP_200_OK, ok.text


# ---------------------------------------------------------------------------
# Exporters -- refuse, never truncate
# ---------------------------------------------------------------------------


def _make_parts(db: Session, *, company_id: int, count: int) -> list:
    _ensure_company(db, company_id)
    parts = []
    for _ in range(count):
        n = _next()
        part = Part(
            part_number=f"BOUNDS-P-{n:05d}",
            name=f"Bounds part {n}",
            description="resource-bounds fixture part",
            part_type="purchased",
            unit_of_measure="each",
            is_active=True,
            company_id=company_id,
        )
        db.add(part)
        parts.append(part)
    db.commit()
    return parts


def test_export_over_limit_refuses_with_an_actionable_message(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Over the cap the export 400s and tells the operator what to do about it.

    Monkeypatching the constant keeps the test from having to build 10k rows --
    the same idiom ``test_rfq_parsing_service.py`` uses against MAX_IMPORT_ROWS.
    The message must name the cap and state the concrete next action, matching
    the ``import_service`` "Too many rows ... Split the file" idiom with an
    export-appropriate tail (narrow the filters, since every exporter already
    accepts date/status/type filters).
    """
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    _make_parts(db_session, company_id=COMPANY_A, count=4)

    monkeypatch.setattr("app.api.endpoints.exports.MAX_EXPORT_ROWS", 3)
    resp = client.get("/api/v1/exports/parts/export", headers=headers_for(user))

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    detail = resp.json()["detail"]
    assert "3" in detail, detail
    assert "Narrow the date range" in detail, detail


def test_export_at_exactly_the_limit_still_succeeds(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """The cap is a ceiling, not an off-by-one: N rows export, N+1 refuse."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    _make_parts(db_session, company_id=COMPANY_A, count=4)

    monkeypatch.setattr("app.api.endpoints.exports.MAX_EXPORT_ROWS", 4)
    resp = client.get("/api/v1/exports/parts/export", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_export_cap_counts_entities_not_joined_rows(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """The cap must count POs, not the PO x line cartesian the eager load emits.

    ``export_purchase_orders`` (and ``export_quotes``) eager-load a COLLECTION
    -- ``joinedload(PurchaseOrder.lines)``. Applying ``.limit()`` on top of a
    collection joinedload is the classic way to truncate an entity mid-collection
    and miscount the result. SQLAlchemy's ``Query`` avoids it by wrapping the
    primary entity in a subquery and applying LIMIT there, so ``len(rows)`` is a
    count of POs and every PO still carries all of its lines.

    That behaviour is what makes ``fetch_export_rows`` correct here, and it is
    invisible in the code -- a future refactor to a hand-rolled join, or to a
    2.0-style ``select()`` without the same wrapping, would silently start
    refusing exports that are well under the cap. Hence this test: 3 POs of 3
    lines each is 9 joined rows, and a cap of 3 must still succeed.
    """
    _ensure_company(db_session, COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    for _ in range(3):
        n = _next()
        vendor = Vendor(code=f"JV{n:05d}", name=f"Join Vendor {n}", is_active=True, company_id=COMPANY_A)
        db_session.add(vendor)
        db_session.flush()
        po = PurchaseOrder(
            po_number=f"JPO-{n:05d}",
            vendor_id=vendor.id,
            status=POStatus.SENT,
            order_date=date.today(),
            company_id=COMPANY_A,
        )
        db_session.add(po)
        db_session.flush()
        for i, part in enumerate(_make_parts(db_session, company_id=COMPANY_A, count=3), start=1):
            db_session.add(
                PurchaseOrderLine(
                    purchase_order_id=po.id,
                    line_number=i,
                    part_id=part.id,
                    quantity_ordered=1.0,
                    quantity_received=0.0,
                    unit_price=1.0,
                    is_closed=False,
                    company_id=COMPANY_A,
                )
            )
    db_session.commit()

    monkeypatch.setattr("app.api.endpoints.exports.MAX_EXPORT_ROWS", 3)
    resp = client.get("/api/v1/exports/purchase-orders/export", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # A fourth PO puts it over the cap -- proving the cap is live, not inert.
    n = _next()
    vendor = Vendor(code=f"JV{n:05d}", name=f"Join Vendor {n}", is_active=True, company_id=COMPANY_A)
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        PurchaseOrder(
            po_number=f"JPO-{n:05d}",
            vendor_id=vendor.id,
            status=POStatus.SENT,
            order_date=date.today(),
            company_id=COMPANY_A,
        )
    )
    db_session.commit()

    over = client.get("/api/v1/exports/purchase-orders/export", headers=headers_for(user))
    assert over.status_code == status.HTTP_400_BAD_REQUEST, over.text


def test_inventory_transaction_export_refuses_instead_of_truncating(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """The one exporter that already had a cap used to truncate SILENTLY.

    A bare ``.limit(10000)`` handed back a short file indistinguishable from a
    complete one -- for a ledger a manager reconciles from, that is worse than
    an error. It now shares the named constant and the refusal.
    """
    from app.api.endpoints import exports

    # The magic number became a shared named constant, equal to MAX_IMPORT_ROWS.
    assert exports.MAX_EXPORT_ROWS == 10_000

    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    part = _make_parts(db_session, company_id=COMPANY_A, count=1)[0]
    for _ in range(2):
        db_session.add(
            InventoryTransaction(
                part_id=part.id,
                transaction_type=TransactionType.ADJUST,
                quantity=1.0,
                created_by=user.id,
                company_id=COMPANY_A,
            )
        )
    db_session.commit()

    monkeypatch.setattr("app.api.endpoints.exports.MAX_EXPORT_ROWS", 1)
    resp = client.get("/api/v1/exports/inventory/transactions/export", headers=headers_for(user))

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "Narrow the date range" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Customers -- the widened `search` predicate
# ---------------------------------------------------------------------------


def _make_customer(db: Session, *, company_id: int, name: str, code: str, contact_name: str, city: str) -> Customer:
    _ensure_company(db, company_id)
    customer = Customer(
        name=name,
        code=code,
        contact_name=contact_name,
        city=city,
        is_active=True,
        company_id=company_id,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def test_customer_search_matches_name_code_contact_and_city(client: TestClient, db_session: Session):
    """``?search=`` matched ``name`` only; it now matches all four fields.

    This is the ordering fix the frontend rewiring depends on. The Customers
    page filters name/code/contact_name/city client-side today; a later PR drops
    that filter and passes the term to the server instead. Had the server stayed
    name-only, that PR would have silently broken finding an account by its code,
    its buyer, or its city -- which is how a buyer actually searches -- with no
    scroll-to-find fallback, because the list is now capped.
    """
    n = _next()
    headers = headers_for(make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN))
    target = _make_customer(
        db_session,
        company_id=COMPANY_A,
        name=f"Acme Aerospace {n}",
        code=f"ZZCODE{n}",
        contact_name=f"Wilhelmina Buyer {n}",
        city=f"Kalamazoo{n}",
    )
    # A decoy that matches on none of the four terms below.
    _make_customer(
        db_session,
        company_id=COMPANY_A,
        name=f"Decoy Industries {n}",
        code=f"DEC{n}",
        contact_name=f"Nobody {n}",
        city=f"Elsewhere{n}",
    )

    for field, term in (
        ("name", f"Acme Aerospace {n}"),
        ("code", f"ZZCODE{n}"),
        ("contact_name", f"Wilhelmina Buyer {n}"),
        ("city", f"Kalamazoo{n}"),
    ):
        resp = client.get("/api/v1/customers/", headers=headers, params={"search": term})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        ids = [row["id"] for row in resp.json()]
        assert target.id in ids, f"search by {field} ({term!r}) did not find the customer"
        assert len(ids) == 1, f"search by {field} also matched the decoy: {resp.json()}"


def test_customer_search_is_case_insensitive_on_every_widened_field(client: TestClient, db_session: Session):
    """All four legs stay ``ilike``, not ``like`` -- a buyer types lowercase."""
    n = _next()
    headers = headers_for(make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN))
    target = _make_customer(
        db_session,
        company_id=COMPANY_A,
        name=f"Uppercase Co {n}",
        code=f"UPPER{n}",
        contact_name=f"Shouty Person {n}",
        city=f"Bigtown{n}",
    )

    for term in (f"upper{n}", f"shouty person {n}", f"bigtown{n}"):
        resp = client.get("/api/v1/customers/", headers=headers, params={"search": term})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert target.id in [row["id"] for row in resp.json()], f"lowercase {term!r} did not match"


# ---------------------------------------------------------------------------
# Purchase orders -- the correlated line_count must equal the old len(po.lines)
# ---------------------------------------------------------------------------


def test_po_list_line_count_matches_actual_lines(client: TestClient, db_session: Session):
    """Parity guard for dropping ``selectinload(PurchaseOrder.lines)``.

    ``line_count`` used to come from ``len(po.lines)`` after hydrating every line
    ORM object; it is now a correlated ``COUNT``. ``PurchaseOrderLine`` carries
    ``TenantMixin`` but NOT ``SoftDeleteMixin``, so there is no soft-deleted line
    for the two to disagree about -- this pins that.
    """
    n = _next()
    _ensure_company(db_session, COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    vendor = Vendor(code=f"BV{n:05d}", name=f"Bounds Vendor {n}", is_active=True, company_id=COMPANY_A)
    db_session.add(vendor)
    db_session.flush()

    po = PurchaseOrder(
        po_number=f"BPO-{n:05d}",
        vendor_id=vendor.id,
        status=POStatus.SENT,
        order_date=date.today(),
        company_id=COMPANY_A,
    )
    db_session.add(po)
    db_session.flush()

    parts = _make_parts(db_session, company_id=COMPANY_A, count=3)
    for i, part in enumerate(parts, start=1):
        db_session.add(
            PurchaseOrderLine(
                purchase_order_id=po.id,
                line_number=i,
                part_id=part.id,
                quantity_ordered=5.0,
                quantity_received=0.0,
                unit_price=2.5,
                is_closed=False,
                company_id=COMPANY_A,
            )
        )
    db_session.commit()

    resp = client.get("/api/v1/purchasing/purchase-orders", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    row = next(r for r in resp.json() if r["po_number"] == po.po_number)
    assert row["line_count"] == 3, row


def test_po_with_no_lines_reports_zero_not_null(client: TestClient, db_session: Session):
    """A correlated COUNT on a PO with no lines must be 0, not NULL.

    ``POListResponse.line_count`` is a non-optional ``int``; a NULL here would be
    a 500 on the response-model validation rather than a wrong number, so it is
    worth its own assertion.
    """
    n = _next()
    _ensure_company(db_session, COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    vendor = Vendor(code=f"EV{n:05d}", name=f"Empty Vendor {n}", is_active=True, company_id=COMPANY_A)
    db_session.add(vendor)
    db_session.flush()

    po = PurchaseOrder(
        po_number=f"EPO-{n:05d}",
        vendor_id=vendor.id,
        status=POStatus.SENT,
        order_date=date.today(),
        company_id=COMPANY_A,
    )
    db_session.add(po)
    db_session.commit()

    resp = client.get("/api/v1/purchasing/purchase-orders", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    row = next(r for r in resp.json() if r["po_number"] == po.po_number)
    assert row["line_count"] == 0, row
