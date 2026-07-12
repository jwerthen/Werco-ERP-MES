"""Committed-audit-row coverage for the four purchase-order endpoints.

Locks in the fix/po-audit-logging change to ``app.api.endpoints.purchasing``,
which brought four previously-unaudited state-changing PO endpoints (CLAUDE.md
invariant 2 violations) under ``AuditService``:

- POST /purchasing/purchase-orders (``create_purchase_order``) → exactly one
  ``log_create("purchase_order", ...)`` row with a new_values snapshot of the PO
  and ``extra_data={"vendor_code", "line_count"}``. Document create writes NO
  per-line audit rows.
- PUT /purchasing/purchase-orders/{po_id} (``update_purchase_order``) → one
  ``log_update("purchase_order", ...)`` row whose column-only old-values
  snapshot is taken BEFORE mutation, so ``extra_data["changes"]`` carries the
  real field diffs (a status change surfaces there) and the required-but-
  vestigial ``POUpdate.version`` (PurchaseOrder has no version column) never
  enters the diff. A no-change PUT writes NO row (``log_update`` self-skips
  when the diff is empty).
- POST /purchasing/purchase-orders/{po_id}/send (``send_purchase_order``) → one
  ``log_status_change("purchase_order", ...)`` row (draft|approved → sent) with
  ``extra_data={"order_date"}``; non-sendable statuses are guarded by 400
  "Can only send draft or approved POs" and write nothing.
- POST /purchasing/purchase-orders/{po_id}/lines (``add_po_line``) → TWO rows:
  a ``log_create("purchase_order_line", ...)`` for the new line (identifier
  ``"{po_number}-L{line_number}"``) AND a ``log_update("purchase_order", ...)``
  whose changes diff captures the subtotal/total roll (``extra_data`` carries
  cause="po_line_added" + line_id/line_number); non-draft POs are guarded by
  400 "Can only add lines to draft POs" and write nothing.

Every audit call sits after ``db.flush()`` and BEFORE the handler's terminal
``db.commit()``, so the audit row commits ATOMICALLY with the domain rows.

Audit assertions use the committed-only pattern from
tests/api/test_vendor_create_audit.py / test_vendor_code_update.py: the
``client`` fixture shares ONE open session with the endpoint, so a
flushed-but-uncommitted audit row would still be visible to a naive query.
``_committed_audit_rows`` rolls back BEFORE querying — only a row committed
atomically with the domain change survives. We never insert AuditLog rows
directly (tamper-evident hash chain); the endpoints produce them. Fixture rows
(Vendor / Part / PurchaseOrder / PurchaseOrderLine) are inserted DIRECTLY via
the session so they produce no audit rows of their own.

The default seeded company is id=1 (tests/conftest.py).
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

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

COMPANY_A = 1
PO_RESOURCE = "purchase_order"
LINE_RESOURCE = "purchase_order_line"

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


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"po-audit-{n}@co{company_id}.test",
        employee_id=f"POAUDIT-{n:05d}",
        first_name="Po",
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


def _headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_vendor(db: Session, *, company_id: int = COMPANY_A) -> Vendor:
    """Insert a vendor row DIRECTLY (no endpoint) — produces no audit rows."""
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(
        code=f"VND-PA-{n:05d}",
        name=f"PO Audit Vendor {n}",
        contact_name="Pat Lee",
        email=f"po-audit-vendor{n}@supplier.test",
        is_active=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    """Insert a part row DIRECTLY (no endpoint) — produces no audit rows."""
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"PN-PA-{n:05d}",
        name=f"PO Audit Part {n}",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _make_po(
    db: Session,
    vendor: Vendor,
    *,
    company_id: int = COMPANY_A,
    po_status: POStatus = POStatus.DRAFT,
    subtotal: float = 0.0,
    tax: float = 0.0,
    shipping: float = 0.0,
) -> PurchaseOrder:
    """Insert a PO row DIRECTLY (no endpoint) — produces no audit rows.

    The ``PO-AUD-`` prefix cannot collide with the endpoint's generated
    ``PO-YYYYMMDD-XXX`` numbers (per-company unique constraint).
    """
    _ensure_company(db, company_id)
    n = _next()
    po = PurchaseOrder(
        po_number=f"PO-AUD-{n:05d}",
        vendor_id=vendor.id,
        status=po_status,
        subtotal=subtotal,
        tax=tax,
        shipping=shipping,
        total=subtotal + tax + shipping,
        company_id=company_id,
    )
    db.add(po)
    db.commit()
    db.refresh(po)
    return po


def _make_po_line(
    db: Session,
    po: PurchaseOrder,
    part: Part,
    *,
    line_number: int = 1,
    quantity: float = 10.0,
    unit_price: float = 10.0,
) -> PurchaseOrderLine:
    """Insert a PO line DIRECTLY (no endpoint) — produces no audit rows."""
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=line_number,
        part_id=part.id,
        quantity_ordered=quantity,
        unit_price=unit_price,
        line_total=quantity * unit_price,
        company_id=po.company_id,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def _reload_po(db: Session, po_id: int) -> PurchaseOrder:
    """Re-read the PO through the shared session, dropping identity-map state."""
    db.expire_all()
    return db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()


def _audit_rows(
    db: Session,
    *,
    resource_type: str,
    resource_id: int = None,
    resource_identifier: str = None,
    action: str = None,
):
    """Fetch AuditLog rows for a resource type, newest first.

    A rejected create never gets an id, so callers may filter by
    ``resource_identifier`` instead of ``resource_id``.
    """
    db.expire_all()
    q = db.query(AuditLog).filter(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        q = q.filter(AuditLog.resource_id == resource_id)
    if resource_identifier is not None:
        q = q.filter(AuditLog.resource_identifier == resource_identifier)
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, **filters):
    """Fetch AuditLog rows that were actually COMMITTED, not merely flushed.

    Rolling back BEFORE querying guards against the audit-after-commit bug class:
    a committed audit row survives the rollback, a flushed-but-uncommitted one is
    discarded. See the module docstring.
    """
    db.rollback()
    return _audit_rows(db, **filters)


def _committed_pos(db: Session, *, company_id: int = COMPANY_A):
    """Committed-only PurchaseOrder rows for a company (rollback first, then query)."""
    db.rollback()
    db.expire_all()
    return db.query(PurchaseOrder).filter(PurchaseOrder.company_id == company_id).all()


# ---------------------------------------------------------------------------
# POST /purchase-orders — one committed CREATE row for the document
# ---------------------------------------------------------------------------


def test_create_po_persists_committed_create_audit_row(client: TestClient, db_session: Session):
    """POST /purchase-orders emits exactly one COMMITTED CREATE AuditLog row for
    resource_type 'purchase_order' carrying the new PO's id, po_number, acting
    user, and company; the new_values snapshot holds po_number / status 'draft' /
    the computed subtotal+total; extra_data carries vendor_code + line_count.
    Document create writes NO purchase_order_line audit rows.

    FAILS against the previously-unaudited handler: no CREATE row at all.
    FAILS against audit-after-commit code: the row would land in a
    never-committed transaction and be discarded by the rollback in
    ``_committed_audit_rows``.
    """
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    part_a = _make_part(db_session)
    part_b = _make_part(db_session)

    resp = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=_headers_for(admin),
        json={
            "vendor_id": vendor.id,
            "lines": [
                {"part_id": part_a.id, "quantity_ordered": 4, "unit_price": 12.5},
                {"part_id": part_b.id, "quantity_ordered": 2, "unit_price": 10.0},
            ],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["id"] is not None
    assert body["po_number"].startswith("PO-")
    assert body["status"] == "draft"
    assert float(body["subtotal"]) == 70.0
    assert float(body["total"]) == 70.0

    rows = _committed_audit_rows(db_session, resource_type=PO_RESOURCE, resource_id=body["id"], action="CREATE")
    assert len(rows) == 1, "expected exactly one COMMITTED CREATE audit row for the new PO"
    row = rows[0]
    assert row.action == "CREATE"
    assert row.resource_type == PO_RESOURCE
    assert row.resource_id == body["id"]
    assert row.resource_identifier == body["po_number"]
    assert row.company_id == COMPANY_A
    assert row.user_id == admin.id

    # The new_values snapshot captures the created document's business fields.
    assert row.new_values["po_number"] == body["po_number"]
    assert row.new_values["status"] == "draft"
    assert row.new_values["subtotal"] == 70.0
    assert row.new_values["total"] == 70.0
    assert row.new_values["vendor_id"] == vendor.id

    assert row.extra_data == {"vendor_code": vendor.code, "line_count": 2}

    # Document create is ONE audit row: no per-line purchase_order_line rows.
    assert _committed_audit_rows(db_session, resource_type=LINE_RESOURCE) == []


def test_create_po_unknown_vendor_404_writes_nothing(client: TestClient, db_session: Session):
    """POSTing with a vendor_id that does not exist in the company is a 404
    "Vendor not found" BEFORE anything is written: no committed PurchaseOrder
    row and no committed purchase_order audit row of any kind exists afterwards
    (the po_number is server-generated, so we assert on the whole resource type;
    the per-test DB starts empty)."""
    admin = _make_user(db_session)
    part = _make_part(db_session)

    resp = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=_headers_for(admin),
        json={
            "vendor_id": 999999,
            "lines": [{"part_id": part.id, "quantity_ordered": 1, "unit_price": 5.0}],
        },
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Vendor not found"

    assert _committed_pos(db_session) == []
    assert _committed_audit_rows(db_session, resource_type=PO_RESOURCE) == []
    assert _committed_audit_rows(db_session, resource_type=LINE_RESOURCE) == []


# ---------------------------------------------------------------------------
# PUT /purchase-orders/{po_id} — one committed UPDATE row with a changes diff
# ---------------------------------------------------------------------------


def test_update_po_persists_committed_update_row_with_changes_diff(client: TestClient, db_session: Session):
    """PUT /purchase-orders/{id} (notes + status -> approved) emits exactly one
    COMMITTED UPDATE AuditLog row whose extra_data["changes"] diff carries the
    changed fields — including the status transition — with old/new snapshots
    reflecting it. The old-values snapshot is column-only and taken BEFORE
    mutation, so the required-but-vestigial POUpdate.version field (PurchaseOrder
    has no version column) never appears in the snapshots or the diff."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    po = _make_po(db_session, vendor)

    resp = client.put(
        f"/api/v1/purchasing/purchase-orders/{po.id}",
        headers=_headers_for(admin),
        json={"version": 0, "notes": "Expedite audit trail", "status": "approved"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == "approved"

    rows = _committed_audit_rows(db_session, resource_type=PO_RESOURCE, resource_id=po.id, action="UPDATE")
    assert len(rows) == 1, "expected exactly one COMMITTED UPDATE audit row for the PO change"
    row = rows[0]
    assert row.action == "UPDATE"
    assert row.resource_type == PO_RESOURCE
    assert row.resource_id == po.id
    assert row.resource_identifier == po.po_number
    assert row.company_id == COMPANY_A
    assert row.user_id == admin.id

    # Old/new snapshots reflect the transition...
    assert row.old_values["status"] == "draft"
    assert row.old_values["notes"] is None
    assert row.new_values["status"] == "approved"
    assert row.new_values["notes"] == "Expedite audit trail"

    # ...and the changes diff carries exactly those field transitions.
    changes = (row.extra_data or {}).get("changes") or {}
    assert changes["status"] == {"old": "draft", "new": "approved"}
    assert changes["notes"] == {"old": None, "new": "Expedite audit trail"}

    # The vestigial POUpdate.version never enters the audited data.
    assert "version" not in changes
    assert "version" not in row.old_values
    assert "version" not in row.new_values

    # The domain change itself committed atomically with the audit row.
    assert _reload_po(db_session, po.id).status == POStatus.APPROVED


def test_update_po_noop_writes_no_audit_row(client: TestClient, db_session: Session):
    """A PUT carrying only the required version field changes nothing: 200, and
    NO committed audit row of any kind exists for the PO — log_update self-skips
    when the changes diff is empty (no noise rows in the tamper-evident chain)."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    po = _make_po(db_session, vendor)

    resp = client.put(
        f"/api/v1/purchasing/purchase-orders/{po.id}",
        headers=_headers_for(admin),
        json={"version": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert _committed_audit_rows(db_session, resource_type=PO_RESOURCE, resource_id=po.id) == []


# ---------------------------------------------------------------------------
# POST /purchase-orders/{po_id}/send — one committed STATUS_CHANGE row
# ---------------------------------------------------------------------------


def test_send_po_persists_committed_status_change_row(client: TestClient, db_session: Session):
    """POST /purchase-orders/{id}/send on a draft PO returns 200 and emits
    exactly one COMMITTED STATUS_CHANGE AuditLog row: old_values
    {"status": "draft"}, new_values {"status": "sent"}, extra_data carrying the
    stamped order_date — and the PO row itself commits as sent atomically."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    po = _make_po(db_session, vendor)

    resp = client.post(
        f"/api/v1/purchasing/purchase-orders/{po.id}/send",
        headers=_headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json() == {"message": "PO sent", "po_number": po.po_number}

    rows = _committed_audit_rows(db_session, resource_type=PO_RESOURCE, resource_id=po.id, action="STATUS_CHANGE")
    assert len(rows) == 1, "expected exactly one COMMITTED STATUS_CHANGE audit row for the send"
    row = rows[0]
    assert row.action == "STATUS_CHANGE"
    assert row.resource_type == PO_RESOURCE
    assert row.resource_id == po.id
    assert row.resource_identifier == po.po_number
    assert row.company_id == COMPANY_A
    assert row.user_id == admin.id
    assert row.old_values == {"status": "draft"}
    assert row.new_values == {"status": "sent"}

    # The PO committed as sent, with the order_date the audit row echoes.
    sent_po = _reload_po(db_session, po.id)
    assert sent_po.status == POStatus.SENT
    assert sent_po.order_date is not None
    assert row.extra_data == {"order_date": sent_po.order_date.isoformat()}


def test_send_already_sent_po_400_writes_no_row(client: TestClient, db_session: Session):
    """Sending an already-sent PO is a 400 "Can only send draft or approved POs"
    and writes NOTHING: no committed audit row of any kind exists for that PO."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    po = _make_po(db_session, vendor, po_status=POStatus.SENT)

    resp = client.post(
        f"/api/v1/purchasing/purchase-orders/{po.id}/send",
        headers=_headers_for(admin),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Can only send draft or approved POs"

    assert _committed_audit_rows(db_session, resource_type=PO_RESOURCE, resource_id=po.id) == []
    # The guard also left the row untouched (still sent, never date-stamped).
    unchanged = _reload_po(db_session, po.id)
    assert unchanged.status == POStatus.SENT
    assert unchanged.order_date is None


# ---------------------------------------------------------------------------
# POST /purchase-orders/{po_id}/lines — committed line CREATE + PO UPDATE pair
# ---------------------------------------------------------------------------


def test_add_line_persists_line_create_and_po_update_rows(client: TestClient, db_session: Session):
    """POST /purchase-orders/{id}/lines on a draft PO with one existing line
    emits TWO committed AuditLog rows atomically with the domain change: a
    purchase_order_line CREATE (identifier "{po_number}-L2") and a
    purchase_order UPDATE whose changes diff captures the subtotal/total roll
    (extra_data cause="po_line_added" + line_id/line_number). Totals math:
    100.0 existing + 4 x 12.5 = 150.0 subtotal; +5 tax +10 shipping = 165.0."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    part_existing = _make_part(db_session)
    part_new = _make_part(db_session)
    po = _make_po(db_session, vendor, subtotal=100.0, tax=5.0, shipping=10.0)
    _make_po_line(db_session, po, part_existing, line_number=1, quantity=10.0, unit_price=10.0)

    resp = client.post(
        f"/api/v1/purchasing/purchase-orders/{po.id}/lines",
        headers=_headers_for(admin),
        json={"part_id": part_new.id, "quantity_ordered": 4, "unit_price": 12.5},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["line_number"] == 2
    assert float(body["line_total"]) == 50.0

    # Row 1: the line CREATE, identified as "{po_number}-L{line_number}".
    line_rows = _committed_audit_rows(db_session, resource_type=LINE_RESOURCE, resource_id=body["id"], action="CREATE")
    assert len(line_rows) == 1, "expected exactly one COMMITTED CREATE audit row for the new PO line"
    line_row = line_rows[0]
    assert line_row.action == "CREATE"
    assert line_row.resource_type == LINE_RESOURCE
    assert line_row.resource_id == body["id"]
    assert line_row.resource_identifier == f"{po.po_number}-L2"
    assert line_row.company_id == COMPANY_A
    assert line_row.user_id == admin.id
    assert line_row.extra_data == {"po_id": po.id, "po_number": po.po_number}
    assert line_row.new_values["purchase_order_id"] == po.id
    assert line_row.new_values["line_number"] == 2
    assert line_row.new_values["part_id"] == part_new.id
    assert float(line_row.new_values["quantity_ordered"]) == 4.0
    assert float(line_row.new_values["unit_price"]) == 12.5
    assert float(line_row.new_values["line_total"]) == 50.0

    # Row 2: the PO UPDATE whose diff captures the totals roll.
    po_rows = _committed_audit_rows(db_session, resource_type=PO_RESOURCE, resource_id=po.id, action="UPDATE")
    assert len(po_rows) == 1, "expected exactly one COMMITTED UPDATE audit row for the PO totals roll"
    po_row = po_rows[0]
    assert po_row.resource_identifier == po.po_number
    assert po_row.company_id == COMPANY_A
    assert po_row.user_id == admin.id
    extra = po_row.extra_data or {}
    assert extra.get("cause") == "po_line_added"
    assert extra.get("line_id") == body["id"]
    assert extra.get("line_number") == 2
    changes = extra.get("changes") or {}
    assert changes["subtotal"] == {"old": 100.0, "new": 150.0}
    assert changes["total"] == {"old": 115.0, "new": 165.0}
    assert "status" not in changes  # only the totals rolled

    # The pair was logged in order (line CREATE first) on the same chain.
    assert line_row.sequence_number < po_row.sequence_number

    # And the domain totals committed to match the audited roll.
    rolled = _reload_po(db_session, po.id)
    assert rolled.subtotal == 150.0
    assert rolled.total == 165.0


def test_add_line_to_sent_po_400_writes_no_rows(client: TestClient, db_session: Session):
    """Adding a line to a non-draft (sent) PO is a 400 "Can only add lines to
    draft POs" and writes NOTHING: no committed purchase_order_line CREATE row,
    no committed purchase_order UPDATE row, no new domain line, totals unchanged."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    part = _make_part(db_session)
    po = _make_po(db_session, vendor, po_status=POStatus.SENT, subtotal=100.0)

    resp = client.post(
        f"/api/v1/purchasing/purchase-orders/{po.id}/lines",
        headers=_headers_for(admin),
        json={"part_id": part.id, "quantity_ordered": 4, "unit_price": 12.5},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Can only add lines to draft POs"

    assert _committed_audit_rows(db_session, resource_type=LINE_RESOURCE) == []
    assert _committed_audit_rows(db_session, resource_type=PO_RESOURCE, resource_id=po.id) == []

    db_session.expire_all()
    lines = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).all()
    assert lines == []
    unchanged = _reload_po(db_session, po.id)
    assert unchanged.subtotal == 100.0
    assert unchanged.total == 100.0


# ---------------------------------------------------------------------------
# RBAC: an operator cannot create POs, and no row of any kind is written
# ---------------------------------------------------------------------------


def test_create_po_rejects_unprivileged_role_403_and_writes_no_row(client: TestClient, db_session: Session):
    """create_purchase_order is gated by require_role([ADMIN, MANAGER,
    SUPERVISOR]): an OPERATOR gets 403 "Insufficient permissions" before the
    handler runs — so neither a committed PurchaseOrder row nor a committed
    purchase_order audit row of any kind may exist afterwards."""
    operator = _make_user(db_session, role=UserRole.OPERATOR)
    vendor = _make_vendor(db_session)
    part = _make_part(db_session)

    resp = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=_headers_for(operator),
        json={
            "vendor_id": vendor.id,
            "lines": [{"part_id": part.id, "quantity_ordered": 1, "unit_price": 5.0}],
        },
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Insufficient permissions"

    assert _committed_pos(db_session) == []
    assert _committed_audit_rows(db_session, resource_type=PO_RESOURCE) == []
    assert _committed_audit_rows(db_session, resource_type=LINE_RESOURCE) == []
