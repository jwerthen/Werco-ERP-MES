"""Tenant-isolation coverage for the PO-upload endpoints + matching service.

Locks in the fix on branch fix/po-upload-tenant-scope that scoped
``po_upload.py`` and ``matching_service.py`` to the active company
(CLAUDE.md invariant 1). Headline invariants:

- GET /api/v1/po-upload/search-parts and /search-vendors return ONLY the
  active company's rows, even when the same part_number / vendor name exists
  in another company (per-company unique constraints allow that).
- POST /api/v1/po-upload/create-from-upload rejects a vendor_id belonging to
  another company with the existing 400 "Vendor not found" (and creates no
  PurchaseOrder for either company).
- create-from-upload treats a part_number that exists only in another company
  as NEW for this company: it creates a fresh Part here instead of linking the
  foreign part id onto the PO line.
- matching_service's match_vendor / match_part / match_po_line_items /
  check_po_number_exists take a REQUIRED company_id and never match, suggest,
  or report rows from another company.

Fixture rows are direct inserts spanning company 1 (A) and company 2 (B);
tokens are minted directly via create_access_token with the company_id claim.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.user import User, UserRole
from app.services.matching_service import check_po_number_exists, match_part, match_po_line_items, match_vendor

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

COMPANY_A = 1  # seeded by the db_session fixture
COMPANY_B = 2

# Module-level counter so every fixture row gets a globally unique natural key,
# even across companies and across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=True,
        )
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole, company_id: int) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"user{n}@co{company_id}.test",
        employee_id=f"EMP-{n:05d}",
        first_name=role.value.title(),
        last_name=f"C{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    """Auth headers for ``user``, active company = the user's home company."""
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session, *, company_id: int, part_number: str, description: str = None) -> Part:
    _ensure_company(db, company_id)
    part = Part(
        part_number=part_number,
        name=f"{part_number} name",
        description=description or f"{part_number} description",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_vendor(db: Session, *, company_id: int, name: str) -> Vendor:
    _ensure_company(db, company_id)
    vendor = Vendor(
        name=name,
        code=f"VIS-{_next():05d}",
        is_active=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def make_po(db: Session, *, company_id: int, vendor_id: int, po_number: str) -> PurchaseOrder:
    po = PurchaseOrder(
        po_number=po_number,
        vendor_id=vendor_id,
        status=POStatus.DRAFT,
        company_id=company_id,
    )
    db.add(po)
    db.commit()
    db.refresh(po)
    return po


def _create_payload(po_number: str, vendor_id: int, *, line_items=None, create_parts=None) -> dict:
    """Minimal POCreateFromUpload payload. pdf_path stays "" so the
    move_pdf_to_po storage step is skipped."""
    return {
        "po_number": po_number,
        "vendor_id": vendor_id,
        "create_vendor": False,
        "line_items": line_items or [],
        "create_parts": create_parts or [],
        "pdf_path": "",
    }


# ---------------------------------------------------------------------------
# 1. GET /po-upload/search-parts is scoped to the active company
# ---------------------------------------------------------------------------


def test_search_parts_returns_only_own_company_row(client: TestClient, db_session: Session):
    """A part_number existing in BOTH companies yields only company A's row."""
    pn = f"ISO-PN-{_next():05d}"
    part_a = make_part(db_session, company_id=COMPANY_A, part_number=pn)
    part_b = make_part(db_session, company_id=COMPANY_B, part_number=pn)
    assert part_a.id != part_b.id
    user_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)

    resp = client.get("/api/v1/po-upload/search-parts", params={"q": pn}, headers=headers_for(user_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = resp.json()
    assert [r["id"] for r in rows] == [part_a.id]
    assert part_b.id not in {r["id"] for r in rows}
    assert rows[0]["part_number"] == pn


def test_search_parts_foreign_only_part_yields_empty(client: TestClient, db_session: Session):
    """A part_number existing only in company B is invisible to company A."""
    pn = f"ONLYB-PN-{_next():05d}"
    make_part(db_session, company_id=COMPANY_B, part_number=pn)
    user_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)

    resp = client.get("/api/v1/po-upload/search-parts", params={"q": pn}, headers=headers_for(user_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json() == []


# ---------------------------------------------------------------------------
# 2. GET /po-upload/search-vendors is scoped to the active company
# ---------------------------------------------------------------------------


def test_search_vendors_returns_only_own_company_row(client: TestClient, db_session: Session):
    """A vendor name existing in BOTH companies yields only company A's row."""
    name = f"Iso Vendor {_next():05d}"
    vendor_a = make_vendor(db_session, company_id=COMPANY_A, name=name)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B, name=name)
    assert vendor_a.id != vendor_b.id
    user_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)

    resp = client.get("/api/v1/po-upload/search-vendors", params={"q": name}, headers=headers_for(user_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = resp.json()
    assert [r["id"] for r in rows] == [vendor_a.id]
    assert vendor_b.id not in {r["id"] for r in rows}
    assert rows[0]["name"] == name


def test_search_vendors_foreign_only_vendor_yields_empty(client: TestClient, db_session: Session):
    """A vendor existing only in company B is invisible to company A."""
    name = f"OnlyB Vendor {_next():05d}"
    make_vendor(db_session, company_id=COMPANY_B, name=name)
    user_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)

    resp = client.get("/api/v1/po-upload/search-vendors", params={"q": name}, headers=headers_for(user_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json() == []


# ---------------------------------------------------------------------------
# 3. POST /po-upload/create-from-upload rejects a foreign vendor_id
# ---------------------------------------------------------------------------


def test_create_from_upload_foreign_vendor_id_is_400(client: TestClient, db_session: Session):
    """Company B's vendor_id is 'Vendor not found' for company A; no PO row is
    created for either company."""
    vendor_b = make_vendor(db_session, company_id=COMPANY_B, name=f"Foreign Vendor {_next():05d}")
    # The id is real -- only tenancy makes it invisible to company A.
    assert db_session.query(Vendor).filter(Vendor.id == vendor_b.id).first() is not None
    manager_a = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)
    po_number = f"PO-XT-{_next():05d}"

    resp = client.post(
        "/api/v1/po-upload/create-from-upload",
        headers=headers_for(manager_a),
        json=_create_payload(po_number, vendor_b.id),
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Vendor not found"
    # Unscoped check: no PurchaseOrder row exists with this number in ANY company.
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).count() == 0


# ---------------------------------------------------------------------------
# 4. create-from-upload: a part_number existing only in company B is NEW for A
# ---------------------------------------------------------------------------


def test_create_from_upload_foreign_part_number_creates_new_part(client: TestClient, db_session: Session):
    """A part_number that exists only in company B must not be linked onto
    company A's PO line; a fresh company-A Part is created instead."""
    pn = f"XTPART-{_next():05d}"
    part_b = make_part(db_session, company_id=COMPANY_B, part_number=pn)
    vendor_a = make_vendor(db_session, company_id=COMPANY_A, name=f"Own Vendor {_next():05d}")
    manager_a = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)
    po_number = f"PO-XT-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor_a.id,
        line_items=[
            {
                "part_id": 0,  # falsy -> resolved from the create_parts map
                "part_number": pn,
                "description": "Cross-tenant part-number collision",
                "quantity_ordered": 5,
                "unit_price": 2.5,
            }
        ],
        create_parts=[{"part_number": pn, "description": "Cross-tenant part-number collision"}],
    )

    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager_a), json=payload)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["success"] is True
    # Pre-fix, the unscoped part_number probe found B's part -> parts_created == 0.
    assert body["parts_created"] == 1

    parts = db_session.query(Part).filter(Part.part_number == pn).all()
    assert {p.company_id for p in parts} == {COMPANY_A, COMPANY_B}
    part_a = next(p for p in parts if p.company_id == COMPANY_A)
    assert part_a.id != part_b.id

    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).one()
    assert po.company_id == COMPANY_A
    lines = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).all()
    assert [line.part_id for line in lines] == [part_a.id]
    assert lines[0].part_id != part_b.id

    # The state change is audit-logged against company A (invariant 2).
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "PO_CREATE_FROM_UPLOAD", AuditLog.resource_id == po.id)
        .one()
    )
    assert audit.company_id == COMPANY_A


# ---------------------------------------------------------------------------
# 5. matching_service is scoped by its required company_id
# ---------------------------------------------------------------------------


def test_check_po_number_exists_scoped_to_company(db_session: Session):
    """A po_number existing only in company B reads False for company A."""
    vendor_b = make_vendor(db_session, company_id=COMPANY_B, name=f"PO Vendor {_next():05d}")
    po_number = f"PO-ISO-{_next():05d}"
    make_po(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id, po_number=po_number)

    assert check_po_number_exists(po_number, db_session, company_id=COMPANY_A) is False
    assert check_po_number_exists(po_number, db_session, company_id=COMPANY_B) is True


def test_match_vendor_never_matches_or_suggests_foreign_vendor(db_session: Session):
    """match_vendor for company A ignores a company-B vendor entirely -- no
    exact match, no fuzzy match, and no appearance in suggestions -- even when
    A has its own (unrelated) vendors forming the candidate pool."""
    n = _next()
    vendor_b = make_vendor(db_session, company_id=COMPANY_B, name=f"Acme Precision Mach {n}")
    make_vendor(db_session, company_id=COMPANY_A, name=f"Zeta Fasteners Supply {_next()}")

    result_a = match_vendor(vendor_b.name, db_session, company_id=COMPANY_A)
    assert result_a.matched is False
    assert vendor_b.id not in {s["id"] for s in result_a.suggestions}

    # Positive control: the same lookup scoped to company B matches exactly.
    result_b = match_vendor(vendor_b.name, db_session, company_id=COMPANY_B)
    assert result_b.matched is True
    assert result_b.match_id == vendor_b.id


def test_match_part_never_matches_or_suggests_foreign_part(db_session: Session):
    """match_part for company A ignores a company-B part -- no match and no
    appearance in suggestions -- even with an A-side candidate pool."""
    pn_b = f"XTB-{_next():05d}"
    part_b = make_part(db_session, company_id=COMPANY_B, part_number=pn_b)
    make_part(db_session, company_id=COMPANY_A, part_number=f"AAA-{_next():05d}")

    result_a = match_part(pn_b, db_session, company_id=COMPANY_A)
    assert result_a.matched is False
    assert part_b.id not in {s["id"] for s in result_a.suggestions}

    # Positive control: scoped to company B it is an exact match.
    result_b = match_part(pn_b, db_session, company_id=COMPANY_B)
    assert result_b.matched is True
    assert result_b.match_id == part_b.id


def test_match_po_line_items_does_not_link_foreign_part(db_session: Session):
    """A line item whose part_number exists only in company B resolves to no
    match for company A (matched_part_id None), and to B's part for company B."""
    pn_b = f"XTLINE-{_next():05d}"
    part_b = make_part(db_session, company_id=COMPANY_B, part_number=pn_b)
    line_items = [{"part_number": pn_b, "qty_ordered": 3}]

    result_a = match_po_line_items(line_items, db_session, company_id=COMPANY_A)
    assert len(result_a) == 1
    assert result_a[0]["matched_part_id"] is None
    assert result_a[0]["part_match"]["matched"] is False

    result_b = match_po_line_items(line_items, db_session, company_id=COMPANY_B)
    assert result_b[0]["matched_part_id"] == part_b.id
