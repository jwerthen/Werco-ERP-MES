"""Soft-deleted-part policy coverage for POST /po-upload/create-from-upload.

Locks in the user-decided policy (option c -- reject with 400) for part
numbers held by soft-deleted parts in the PO-upload create path:

- A create_parts entry whose part_number is held by a SOFT-DELETED part is
  rejected with 400 "Part number '<pn>' belongs to a deleted part - restore it
  or use a different part number"; the deleted row is untouched, no
  PurchaseOrder is created, and no PO_CREATE_FROM_UPLOAD audit row commits.
- A line item referencing a deleted part's number WITHOUT a create_parts entry
  is also rejected with 400 -- via the pre-existing resolution guard ("Part
  '<pn>' not found and not in create list"): the deleted part is invisible to
  resolution and can never be linked. (The deleted-part-specific message only
  fires on the create_parts path, which is the only path that would otherwise
  INSERT a colliding row.)
- A line item carrying a deleted part's actual id is rejected by the in-tenant
  part_id probe, which now also filters is_deleted: 400 "Part id <id> not
  found", indistinguishable from a nonexistent id.
- Controls: a LIVE holder of the number is reused (no new part), and a fresh
  number creates a part -- pre-existing semantics, unchanged.
- _find_existing_part_number_by_description never suggests a deleted part's
  number (invariant 3: soft-deleted rows are excluded from queries).
- The TOCTOU IntegrityError backstops (per-part flush + terminal commit) map a
  constraint violation to 400 "Part number already exists" and roll the whole
  transaction back -- including the PO_CREATE_FROM_UPLOAD audit row, so a
  failed create never commits an orphan audit row. Simulated by monkeypatching
  the request session's flush/commit one-shot (a real race isn't reproducible
  on the per-worker SQLite); an identical retry then succeeds.

Fixtures are direct inserts in company 1 (tenancy is covered separately in
test_po_upload_tenant_isolation.py); the soft delete mimics the real
parts.py delete flow: part.soft_delete(user_id) + is_active=False +
status="obsolete".
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.endpoints.po_upload import _find_existing_part_number_by_description
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

COMPANY_ID = 1  # seeded by the db_session fixture

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER) -> User:
    n = _next()
    user = User(
        email=f"user{n}@deleted-parts.test",
        employee_id=f"EMP-DP-{n:05d}",
        first_name=role.value.title(),
        last_name="DeletedParts",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=COMPANY_ID,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session, *, part_number: str, description: str = None, part_type: str = "purchased") -> Part:
    part = Part(
        part_number=part_number,
        name=f"{part_number} name",
        description=description or f"{part_number} description",
        part_type=part_type,
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_ID,
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


def make_vendor(db: Session) -> Vendor:
    n = _next()
    vendor = Vendor(
        name=f"Deleted Parts Vendor {n:05d}",
        code=f"VDP-{n:05d}",
        is_active=True,
        company_id=COMPANY_ID,
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


def _line(part_number: str, part_id: int = 0) -> dict:
    return {
        "part_id": part_id,
        "part_number": part_number,
        "description": "Deleted-part policy test line",
        "quantity_ordered": 1,
        "unit_price": 1.0,
    }


def _assert_nothing_committed(db: Session, po_number: str) -> None:
    """Rollback-before-query: discard any uncommitted request state, then
    assert the failed create left no PurchaseOrder and no committed
    PO_CREATE_FROM_UPLOAD audit row."""
    db.rollback()
    assert db.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).count() == 0
    assert db.query(AuditLog).filter(AuditLog.action == "PO_CREATE_FROM_UPLOAD").count() == 0


# ---------------------------------------------------------------------------
# 1. create_parts entry colliding with a soft-deleted part's number -> 400
# ---------------------------------------------------------------------------


def test_create_parts_deleted_number_is_400_and_commits_nothing(client: TestClient, db_session: Session):
    """A create_parts part_number held by a soft-deleted part is rejected with
    the exact deleted-part 400; the part stays deleted and neither a
    PurchaseOrder nor a PO_CREATE_FROM_UPLOAD audit row is committed."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"DEL-PN-{_next():05d}"
    part = make_part(db_session, part_number=pn)
    soft_delete_part(db_session, part, manager.id)
    po_number = f"PO-DEL-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn)],
        create_parts=[{"part_number": pn, "description": "Recreate attempt"}],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    expected = f"Part number '{pn}' belongs to a deleted part - restore it or use a different part number"
    assert resp.json()["detail"] == expected

    _assert_nothing_committed(db_session, po_number)
    # The deleted row is untouched and no duplicate part appeared.
    rows = db_session.query(Part).filter(Part.part_number == pn).all()
    assert len(rows) == 1
    assert rows[0].is_deleted is True
    assert rows[0].is_active is False
    assert rows[0].deleted_at is not None


# ---------------------------------------------------------------------------
# 2. line item referencing the deleted number, no create_parts entry -> 400
# ---------------------------------------------------------------------------


def test_line_item_only_deleted_number_is_400_and_commits_nothing(client: TestClient, db_session: Session):
    """Without a create_parts entry the deleted part is invisible to line-item
    resolution: the request is rejected 400 by the pre-existing "not found and
    not in create list" guard (the deleted-part-specific message fires only on
    the create_parts path). Either way the deleted part is never linked and
    nothing commits."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"DEL-LINE-{_next():05d}"
    part = make_part(db_session, part_number=pn)
    soft_delete_part(db_session, part, manager.id)
    po_number = f"PO-DEL-{_next():05d}"

    payload = _create_payload(po_number, vendor.id, line_items=[_line(pn)])
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == f"Part '{pn}' not found and not in create list"

    _assert_nothing_committed(db_session, po_number)
    # No PO line ever pointed at the deleted part.
    assert db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.part_id == part.id).count() == 0
    refreshed = db_session.query(Part).filter(Part.id == part.id).one()
    assert refreshed.is_deleted is True


def test_line_item_with_deleted_part_id_is_400_and_commits_nothing(client: TestClient, db_session: Session):
    """A line item carrying the deleted part's actual id is rejected by the
    in-tenant part_id probe (which filters is_deleted) with the same 400 as a
    nonexistent id -- a deleted part cannot be linked onto a PO line by id."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"DEL-ID-{_next():05d}"
    part = make_part(db_session, part_number=pn)
    soft_delete_part(db_session, part, manager.id)
    po_number = f"PO-DEL-{_next():05d}"

    payload = _create_payload(po_number, vendor.id, line_items=[_line(pn, part_id=part.id)])
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == f"Part id {part.id} not found"

    _assert_nothing_committed(db_session, po_number)
    assert db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.part_id == part.id).count() == 0
    refreshed = db_session.query(Part).filter(Part.id == part.id).one()
    assert refreshed.is_deleted is True


# ---------------------------------------------------------------------------
# 3. Controls: live holder is reused; fresh number creates a part
# ---------------------------------------------------------------------------


def test_live_part_number_is_reused_not_recreated(client: TestClient, db_session: Session):
    """Control: a LIVE part holding the number keeps the pre-existing reuse
    semantics -- no new part, parts_created == 0, line links the existing id."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"LIVE-PN-{_next():05d}"
    live_part = make_part(db_session, part_number=pn)
    po_number = f"PO-LIVE-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn)],
        create_parts=[{"part_number": pn, "description": "Should be reused"}],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["parts_created"] == 0

    assert db_session.query(Part).filter(Part.part_number == pn).count() == 1
    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).one()
    lines = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).all()
    assert [line.part_id for line in lines] == [live_part.id]


def test_fresh_part_number_creates_part_and_po(client: TestClient, db_session: Session):
    """Control: a number with no holder at all still creates the part and the
    PO (parts_created == 1)."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"FRESH-PN-{_next():05d}"
    po_number = f"PO-FRESH-{_next():05d}"

    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn)],
        create_parts=[{"part_number": pn, "description": "Brand new part"}],
    )
    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["parts_created"] == 1

    new_part = db_session.query(Part).filter(Part.part_number == pn).one()
    assert new_part.is_deleted is False
    assert new_part.is_active is True
    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).one()
    lines = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).all()
    assert [line.part_id for line in lines] == [new_part.id]


# ---------------------------------------------------------------------------
# 4. _find_existing_part_number_by_description skips soft-deleted parts
# ---------------------------------------------------------------------------


def test_description_lookup_skips_deleted_part(db_session: Session):
    """The description matcher never suggests a deleted part's number: it
    returns None when the only match is soft-deleted, and the LIVE part's
    number when a live row with the same description also exists."""
    user = make_user(db_session)
    n = _next()
    # A fixed point of normalize_description (uppercase, single spaces, no
    # punctuation) so the stored description equals the normalized query.
    description = f"STEEL SHEET 4X8 16GA LOT {n}"

    deleted = make_part(db_session, part_number=f"RM-DEL-{n:05d}", description=description, part_type="raw_material")
    soft_delete_part(db_session, deleted, user.id)

    assert _find_existing_part_number_by_description(db_session, description, "raw_material", COMPANY_ID) is None

    live = make_part(db_session, part_number=f"RM-LIVE-{n:05d}", description=description, part_type="raw_material")
    found = _find_existing_part_number_by_description(db_session, description, "raw_material", COMPANY_ID)
    assert found == live.part_number
    assert found != deleted.part_number


# ---------------------------------------------------------------------------
# 5. IntegrityError backstops (simulated TOCTOU race)
# ---------------------------------------------------------------------------


def _integrity_error() -> IntegrityError:
    return IntegrityError(
        "(sqlite3.IntegrityError) UNIQUE constraint failed: parts.company_id, parts.part_number",
        None,
        Exception("simulated concurrent insert"),
    )


def test_flush_backstop_maps_race_to_400_and_commits_nothing(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """The per-part flush backstop: an IntegrityError at the Part-insert flush
    (a concurrent create slipping past the probes) becomes 400 "Part number
    already exists" with nothing committed, and the rollback leaves the session
    healthy -- an identical retry succeeds.

    Seam: the request runs on this very db_session (get_db override), whose
    autoflush is off, so the FIRST Session.flush() call of the request is the
    Part insert. The patch raises on that call only and delegates to the real
    flush afterwards."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"RACE-PN-{_next():05d}"
    po_number = f"PO-RACE-{_next():05d}"
    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn)],
        create_parts=[{"part_number": pn, "description": "Simulated racing create"}],
    )

    real_flush = db_session.flush
    calls = {"n": 0}

    def exploding_first_flush(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _integrity_error()
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", exploding_first_flush)

    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Part number already exists"
    assert calls["n"] == 1  # the backstop aborted at the Part-insert flush

    _assert_nothing_committed(db_session, po_number)
    assert db_session.query(Part).filter(Part.part_number == pn).count() == 0

    # Retry with the identical payload: the one-shot patch now delegates to the
    # real flush, and the failed attempt left nothing behind to collide with.
    resp_retry = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)
    assert resp_retry.status_code == status.HTTP_200_OK, resp_retry.text
    assert resp_retry.json()["parts_created"] == 1
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).count() == 1


def test_commit_backstop_rolls_back_audit_row_and_po(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """The terminal-commit backstop: an IntegrityError at the final commit
    becomes 400 "Part number already exists" and rolls back the WHOLE
    transaction -- the flushed Part, PO, lines, and crucially the already
    -written PO_CREATE_FROM_UPLOAD audit row (no orphan audit row for a failed
    create). An identical retry then succeeds and commits exactly one audit row.

    Seam: create_po_from_upload issues exactly one Session.commit() (AuditService
    .log flushes but never commits), so raising on the first commit call of the
    request hits the terminal commit precisely."""
    manager = make_user(db_session)
    vendor = make_vendor(db_session)
    pn = f"CRACE-PN-{_next():05d}"
    po_number = f"PO-CRACE-{_next():05d}"
    payload = _create_payload(
        po_number,
        vendor.id,
        line_items=[_line(pn)],
        create_parts=[{"part_number": pn, "description": "Simulated racing create"}],
    )

    real_commit = db_session.commit
    calls = {"n": 0}

    def exploding_first_commit(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _integrity_error()
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(db_session, "commit", exploding_first_commit)

    resp = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Part number already exists"
    assert calls["n"] == 1  # the backstop fired at the terminal commit

    # The CMMC claim: the failed create committed NO audit row (and no PO), even
    # though log_audit had already written one into the doomed transaction.
    _assert_nothing_committed(db_session, po_number)
    assert db_session.query(Part).filter(Part.part_number == pn).count() == 0

    # Identical retry succeeds cleanly and commits exactly one audit row.
    resp_retry = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)
    assert resp_retry.status_code == status.HTTP_200_OK, resp_retry.text
    assert resp_retry.json()["parts_created"] == 1
    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).one()
    audit_rows = db_session.query(AuditLog).filter(AuditLog.action == "PO_CREATE_FROM_UPLOAD").all()
    assert [row.resource_id for row in audit_rows] == [po.id]
