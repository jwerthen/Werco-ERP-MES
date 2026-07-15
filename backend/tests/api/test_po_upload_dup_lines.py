"""Duplicate-part-number and case/whitespace-normalization coverage for
POST /po-upload/create-from-upload.

Locks in the hardening on branch worktree-po-upload-dup-part-line-delete:
part numbers are matched case-insensitively on the stripped number, the same
number appearing on multiple create_parts entries / line items creates the
part ONCE (stored with the first occurrence's stripped canonical form), and
line items with part_id=0 fall back to an existing LIVE part with a matching
number when the number is absent from create_parts. Pinned behavior:

- Two line items sharing one create_parts part_number -> 200, exactly ONE Part
  row, parts_created == 1, BOTH PurchaseOrderLine rows point at the same
  part_id.
- Case-variant ("DUP-X-n" vs "dup-x-n") and whitespace-variant duplicates
  across create_parts + line_items -> 200, ONE part, stored with the FIRST
  occurrence's stripped canonical number.
- A live part already holding the number is reused even when the create_parts
  entry is a case variant: parts_created == 0, lines link the existing id.
- NEW fallback: a part_id=0 line item whose part_number is a case/whitespace
  variant of an existing LIVE part and which is NOT in create_parts resolves
  to that part (no new part created).
- The fallback must NOT resurrect soft-deleted parts (PR #112 policy): a line
  item referencing a deleted part's number, absent from create_parts, is 400
  with the generic "not found and not in create list" message.
- The deleted-holder probe on the create_parts path is case-insensitive: a
  create_parts entry that is a case variant of a soft-deleted part's number is
  400 "belongs to a deleted part".
- Empty line_items -> 400 "At least one line item is required".
- Tenant isolation of the new case-insensitive lookups: a same-numbered part
  in ANOTHER company is never reused -- the create_parts path creates a fresh
  part for the caller's company, and the fallback path treats the foreign-only
  number as not found.

Fixtures are direct inserts (company 1 primary; company 2 only for the
isolation tests, mirroring test_po_upload_tenant_isolation.py); the soft
delete mimics the real parts.py delete flow per test_po_upload_deleted_parts.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

COMPANY_A = 1  # seeded by the db_session fixture
COMPANY_B = 2

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
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


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"user{n}@dup-lines.test",
        employee_id=f"EMP-DL-{n:05d}",
        first_name=role.value.title(),
        last_name="DupLines",
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
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session, *, part_number: str, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    part = Part(
        part_number=part_number,
        name=f"{part_number} name",
        description=f"{part_number} description",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def soft_delete_part(db: Session, part: Part, user_id: int) -> None:
    """Mimic the real DELETE /parts/{id} flow (parts.py): soft_delete() plus
    is_active=False and status='obsolete'."""
    part.soft_delete(user_id)
    part.is_active = False
    part.status = "obsolete"
    db.commit()
    db.refresh(part)
    assert part.is_deleted is True and part.deleted_at is not None


def make_vendor(db: Session, *, company_id: int = COMPANY_A) -> Vendor:
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(
        name=f"Dup Lines Vendor {n:05d}",
        code=f"VDL-{n:05d}",
        is_active=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


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


def _line(part_number: str, part_id: int = 0, quantity: float = 1) -> dict:
    return {
        "part_id": part_id,
        "part_number": part_number,
        "description": "Dup-lines test line",
        "quantity_ordered": quantity,
        "unit_price": 1.0,
    }


def _parts_matching(db: Session, part_number: str) -> list:
    """All Part rows (any company, incl. deleted) matching case-insensitively
    on the stripped number."""
    return db.query(Part).filter(func.lower(Part.part_number) == part_number.strip().lower()).all()


def _po_lines(db: Session, po_number: str) -> tuple:
    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).one()
    lines = (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.purchase_order_id == po.id)
        .order_by(PurchaseOrderLine.line_number)
        .all()
    )
    return po, lines


def _assert_nothing_committed(db: Session, po_number: str) -> None:
    """Rollback-before-query: discard any uncommitted request state, then
    assert the failed create left no PurchaseOrder and no committed
    PO_CREATE_FROM_UPLOAD audit row."""
    db.rollback()
    assert db.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).count() == 0
    assert db.query(AuditLog).filter(AuditLog.action == "PO_CREATE_FROM_UPLOAD").count() == 0


# ---------------------------------------------------------------------------
# 1. Two line items sharing one create_parts part_number -> one part, two lines
# ---------------------------------------------------------------------------


def test_two_lines_sharing_one_created_part_number_create_one_part(client: TestClient, db_session: Session):
    """Two line items with the same part_number and a single create_parts entry
    yield 200 with exactly ONE Part created; both PO lines point at it."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"DUP-SAME-{_next():05d}"
    po_number = f"PO-DUP-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn, quantity=1), _line(pn, quantity=2)],
        create_parts=[{"part_number": pn, "description": "Shared part"}],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["parts_created"] == 1
    assert body["lines_created"] == 2

    parts = _parts_matching(db_session, pn)
    assert len(parts) == 1
    _, lines = _po_lines(db_session, po_number)
    assert [line.part_id for line in lines] == [parts[0].id, parts[0].id]


# ---------------------------------------------------------------------------
# 2. Case-variant and whitespace-variant duplicates collapse to one part
# ---------------------------------------------------------------------------


def test_case_variant_duplicate_creates_one_part_with_first_canonical_number(client: TestClient, db_session: Session):
    """ "DUP-X-n" and "dup-x-n" in create_parts + line_items collapse to ONE
    part stored under the FIRST occurrence's number; both lines share its id."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn_first = f"DUP-X-{_next():05d}"  # uppercase, first occurrence
    pn_variant = pn_first.lower()
    po_number = f"PO-DUP-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn_first), _line(pn_variant)],
        create_parts=[
            {"part_number": pn_first, "description": "First occurrence"},
            {"part_number": pn_variant, "description": "Case-variant duplicate"},
        ],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["parts_created"] == 1
    assert body["lines_created"] == 2

    parts = _parts_matching(db_session, pn_first)
    assert len(parts) == 1
    assert parts[0].part_number == pn_first  # first occurrence wins, verbatim
    _, lines = _po_lines(db_session, po_number)
    assert [line.part_id for line in lines] == [parts[0].id, parts[0].id]


def test_whitespace_variant_duplicate_creates_one_part_with_stripped_number(client: TestClient, db_session: Session):
    """A padded first occurrence ("  PN ") and its exact form collapse to ONE
    part stored under the STRIPPED canonical number; both lines share its id."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"DUP-W-{_next():05d}"
    pn_padded = f"  {pn} "  # first occurrence carries the whitespace
    po_number = f"PO-DUP-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn_padded), _line(pn)],
        create_parts=[
            {"part_number": pn_padded, "description": "Padded first occurrence"},
            {"part_number": pn, "description": "Whitespace-variant duplicate"},
        ],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["parts_created"] == 1
    assert body["lines_created"] == 2

    parts = _parts_matching(db_session, pn)
    assert len(parts) == 1
    assert parts[0].part_number == pn  # stored stripped, no padding
    _, lines = _po_lines(db_session, po_number)
    assert [line.part_id for line in lines] == [parts[0].id, parts[0].id]


# ---------------------------------------------------------------------------
# 3. A live part is reused when the create_parts entry is a case variant
# ---------------------------------------------------------------------------


def test_existing_live_part_reused_for_case_variant_create_entry(client: TestClient, db_session: Session):
    """A live part holding the number is reused even when create_parts carries
    a case variant: parts_created == 0 and the line links the existing id."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"REUSE-CV-{_next():05d}"
    live_part = make_part(db_session, part_number=pn)
    po_number = f"PO-REUSE-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn.lower())],
        create_parts=[{"part_number": pn.lower(), "description": "Should reuse the live part"}],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["parts_created"] == 0

    parts = _parts_matching(db_session, pn)
    assert len(parts) == 1  # no case-variant duplicate row appeared
    assert parts[0].id == live_part.id
    assert parts[0].part_number == pn  # canonical stored form untouched
    _, lines = _po_lines(db_session, po_number)
    assert [line.part_id for line in lines] == [live_part.id]


# ---------------------------------------------------------------------------
# 4. NEW fallback: part_id=0 line, number not in create_parts, live part exists
# ---------------------------------------------------------------------------


def test_line_item_fallback_resolves_live_part_case_and_whitespace_variants(client: TestClient, db_session: Session):
    """A part_id=0 line item whose part_number is a case variant or a
    whitespace variant of an existing LIVE part -- with NO create_parts entry --
    resolves to that part; no new part is created."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"FALLBACK-{_next():05d}"
    live_part = make_part(db_session, part_number=pn)
    po_number = f"PO-FB-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn.lower()), _line(f" {pn}  ")],
        create_parts=[],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["parts_created"] == 0
    assert body["lines_created"] == 2

    parts = _parts_matching(db_session, pn)
    assert len(parts) == 1
    assert parts[0].id == live_part.id
    _, lines = _po_lines(db_session, po_number)
    assert [line.part_id for line in lines] == [live_part.id, live_part.id]


# ---------------------------------------------------------------------------
# 5. Fallback never resurrects a soft-deleted part (PR #112 policy pin)
# ---------------------------------------------------------------------------


def test_line_item_fallback_excludes_deleted_part(client: TestClient, db_session: Session):
    """A part_id=0 line item referencing a soft-deleted part's number (as a
    case variant, exercising the new lookup), absent from create_parts, is 400
    with the generic not-found message -- the deleted part is never linked."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"FB-DEL-{_next():05d}"
    part = make_part(db_session, part_number=pn)
    soft_delete_part(db_session, part, manager.id)
    po_number = f"PO-FBDEL-{_next():05d}"

    payload = _create_payload(po_number, vendor.id, line_items=[_line(pn.lower())])
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == f"Part '{pn.lower()}' not found and not in create list"

    _assert_nothing_committed(db_session, po_number)
    assert db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.part_id == part.id).count() == 0
    refreshed = db_session.query(Part).filter(Part.id == part.id).one()
    assert refreshed.is_deleted is True


# ---------------------------------------------------------------------------
# 6. Deleted-holder probe on the create_parts path is case-insensitive
# ---------------------------------------------------------------------------


def test_deleted_holder_probe_is_case_insensitive(client: TestClient, db_session: Session):
    """A create_parts entry that is a case variant of a soft-deleted part's
    number is rejected with the deleted-part 400; nothing commits and no
    case-variant duplicate row appears."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"ABC-DEL-{_next():05d}"
    part = make_part(db_session, part_number=pn)
    soft_delete_part(db_session, part, manager.id)
    po_number = f"PO-CIDEL-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn.lower())],
        create_parts=[{"part_number": pn.lower(), "description": "Case-variant recreate attempt"}],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    expected = f"Part number '{pn.lower()}' belongs to a deleted part - restore it or use a different part number"
    assert resp.json()["detail"] == expected

    _assert_nothing_committed(db_session, po_number)
    parts = _parts_matching(db_session, pn)
    assert len(parts) == 1  # only the deleted holder; no new row
    assert parts[0].is_deleted is True


# ---------------------------------------------------------------------------
# 7. Empty line_items -> 400
# ---------------------------------------------------------------------------


def test_empty_line_items_is_400(client: TestClient, db_session: Session):
    """A payload with no line items is rejected up front with 400 and creates
    nothing."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    po_number = f"PO-EMPTY-{_next():05d}"

    payload = _create_payload(po_number, vendor.id, line_items=[])
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "At least one line item is required"
    _assert_nothing_committed(db_session, po_number)


# ---------------------------------------------------------------------------
# 8. Tenant isolation of the new case-insensitive lookups
# ---------------------------------------------------------------------------


def test_foreign_case_variant_part_is_not_reused(client: TestClient, db_session: Session):
    """A part whose number (as a case variant) exists only in company B is NOT
    reused for company A: a fresh company-A part is created and the line points
    at it, never at B's row."""
    pn = f"XT-DUP-{_next():05d}"
    part_b = make_part(db_session, part_number=pn, company_id=COMPANY_B)
    manager_a = make_user(db_session, company_id=COMPANY_A)
    vendor_a = make_vendor(db_session, company_id=COMPANY_A)
    po_number = f"PO-XTDUP-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor_a.id,
        line_items=[_line(pn.lower())],
        create_parts=[{"part_number": pn.lower(), "description": "Cross-tenant case-variant collision"}],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager_a), json=payload)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["success"] is True
    # Pre-fix risk: an unscoped case-insensitive probe would find B's part -> 0.
    assert body["parts_created"] == 1

    parts = _parts_matching(db_session, pn)
    assert {p.company_id for p in parts} == {COMPANY_A, COMPANY_B}
    part_a = next(p for p in parts if p.company_id == COMPANY_A)
    assert part_a.id != part_b.id
    assert part_a.part_number == pn.lower()  # stored as submitted (stripped)

    po, lines = _po_lines(db_session, po_number)
    assert po.company_id == COMPANY_A
    assert [line.part_id for line in lines] == [part_a.id]
    assert lines[0].part_id != part_b.id


def test_fallback_never_resolves_foreign_part(client: TestClient, db_session: Session):
    """The new part_id=0 fallback lookup is tenant-scoped: a number existing
    only in company B (probed as a case variant) reads as not found for company
    A -- 400, and no PO is created for either company."""
    pn = f"XT-FB-{_next():05d}"
    part_b = make_part(db_session, part_number=pn, company_id=COMPANY_B)
    manager_a = make_user(db_session, company_id=COMPANY_A)
    vendor_a = make_vendor(db_session, company_id=COMPANY_A)
    po_number = f"PO-XTFB-{_next():05d}"

    payload = _create_payload(po_number, vendor_a.id, line_items=[_line(pn.lower())])
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager_a), json=payload)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == f"Part '{pn.lower()}' not found and not in create list"

    _assert_nothing_committed(db_session, po_number)
    assert db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.part_id == part_b.id).count() == 0
