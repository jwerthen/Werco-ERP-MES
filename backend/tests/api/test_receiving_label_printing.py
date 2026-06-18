"""Endpoint / DB-integration coverage for receiving thermal-label printing.

Companion to the service-level unit tests (``tests/services/test_label_service.py``,
``test_proxybox_client.py``, ``test_print_profile_key.py``). These exercise the
HTTP surface end-to-end against the shared SQLite ``db_session`` with the ProxyBox
NETWORK layer mocked (``app.services.print_service.ProxyBoxClient`` patched to an
``AsyncMock``) -- NO real outbound call is ever made.

What this locks in (the AS9100D / CMMC-relevant invariants of the new endpoints):

POST /api/v1/receiving/receipt/{receipt_id}/print-label
  * Happy path: ProxyBox is constructed with the decrypted key + configured target
    and ``print_and_wait`` is awaited; a ``Document(RECEIVING_LABEL)`` is created,
    ``po_receipts.label_document_id`` is set, and a ``label_print`` audit row is
    written on the tamper-evident chain.
  * 409 when ``allow_print_egress`` is OFF, AND NO ProxyBox call happens.
  * 404 on a cross-tenant receipt (tenant isolation).
  * 403 for a disallowed role; 401 with no auth.
  * 502 when the bridge fails -- but the label Document is still persisted (reprint).

GET / PUT /api/v1/receiving/print-profile (admin-only)
  * Admin can upsert; the API key is stored encrypted and the response exposes only
    ``api_key_last4`` (never the raw key).
  * A non-admin is rejected (403).
  * Flipping ``allow_print_egress`` is audited as a STATUS_CHANGE.

Per the xdist/SQLite convention every fixture row uses a globally-unique natural key
(a module-level counter); assertions key off ids/those keys, never row counts.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.part import Part
from app.models.print_profile import CompanyPrintProfile
from app.models.purchasing import (
    POReceipt,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptStatus,
    Vendor,
)
from app.models.user import User, UserRole
from app.services.proxybox_client import ProxyBoxError

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; not used for login

# Where PrintService constructs the live client. Patching here means no socket opens.
PROXYBOX_CLIENT_PATH = "app.services.print_service.ProxyBoxClient"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"lbl-co-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole, company_id: int) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"lbl{n}@co{company_id}.test",
        employee_id=f"LBL-{n:05d}",
        first_name=role.value.title(),
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


def make_receipt(db: Session, *, company_id: int, with_location: bool = False) -> POReceipt:
    """Create Vendor + PO + PO line + Part + an ACCEPTED POReceipt, all stamped company_id."""
    _ensure_company(db, company_id)
    n = _next()

    vendor = Vendor(code=f"V{n:05d}", name=f"Vendor {n}", is_active=True, is_approved=True, company_id=company_id)
    db.add(vendor)

    part = Part(
        part_number=f"P-{n:05d}",
        name=f"Part {n}",
        description="Precision part",
        revision="C",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()

    po = PurchaseOrder(
        po_number=f"PO-{n:05d}",
        vendor_id=vendor.id,
        status=POStatus.PARTIAL,
        order_date=date.today(),
        company_id=company_id,
    )
    db.add(po)
    db.flush()

    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=10,
        quantity_received=5,
        unit_price=5.0,
        is_closed=False,
        company_id=company_id,
    )
    db.add(line)
    db.flush()

    receiver = make_user(db, role=UserRole.ADMIN, company_id=company_id)
    receipt = POReceipt(
        receipt_number=f"RCV-LBL-{n:05d}",
        po_line_id=line.id,
        quantity_received=5,
        lot_number=f"LOT-{n:05d}",
        heat_number=f"HT-{n:05d}",
        status=ReceiptStatus.ACCEPTED,
        requires_inspection=False,
        received_by=receiver.id,
        received_at=datetime.utcnow(),
        company_id=company_id,
    )
    receipt.company_id = company_id
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


def make_profile(
    db: Session,
    *,
    company_id: int,
    allow_egress: bool = True,
    auto_print: bool = False,
    is_active: bool = True,
    api_key: str = "PBX_LIVE_SECRET_8642",
) -> CompanyPrintProfile:
    """Create a complete, egress-enabled (by default) print profile for a company."""
    _ensure_company(db, company_id)
    profile = CompanyPrintProfile(
        proxybox_base_url="https://pbx-test.pbxz.cloud/api/v1",
        proxybox_target="usb_sn_TESTPRINTER",
        default_paper_size="4x6",
        default_copies=1,
        auto_print_on_receipt=auto_print,
        allow_print_egress=allow_egress,
        is_active=is_active,
    )
    profile.company_id = company_id
    if api_key:
        profile.set_api_key(api_key)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _mock_proxybox():
    """Return (constructor_mock, instance_mock) where print_and_wait is an awaitable success."""
    instance = MagicMock()
    instance.print_and_wait = AsyncMock(
        return_value={"job_id": "job-1", "status": "done", "terminal": True, "succeeded": True, "raw": {}}
    )
    constructor = MagicMock(return_value=instance)
    return constructor, instance


# ===========================================================================
# POST /receipt/{receipt_id}/print-label -- happy path
# ===========================================================================


def test_print_label_happy_path_calls_proxybox_and_persists_document(client: TestClient, db_session: Session):
    """Manual reprint: ProxyBox is called correctly, a Document is created + linked, audited."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, allow_egress=True, api_key="PBX_LIVE_SECRET_8642")

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        resp = client.post(
            f"/api/v1/receiving/receipt/{receipt.id}/print-label",
            headers=headers_for(admin),
            json={"copies": 2},
        )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["receipt_id"] == receipt.id
    assert body["receipt_number"] == receipt.receipt_number
    assert body["printed"] is True
    assert body["label_document_id"] is not None

    # ProxyBox was constructed with the DECRYPTED key + the configured base/target.
    constructor.assert_called_once()
    kwargs = constructor.call_args.kwargs
    assert kwargs["api_key"] == "PBX_LIVE_SECRET_8642"
    assert kwargs["base_url"] == "https://pbx-test.pbxz.cloud/api/v1"
    assert kwargs["target"] == "usb_sn_TESTPRINTER"

    # The print was actually awaited, with our copies override and the 4x6 paper size.
    instance.print_and_wait.assert_awaited_once()
    pw_args, pw_kwargs = instance.print_and_wait.call_args
    assert pw_args[0][:4] == b"%PDF"  # real rendered PDF bytes were submitted
    assert pw_kwargs["copies"] == 2
    assert pw_kwargs["paper_size"] == "4x6"

    # A RECEIVING_LABEL Document exists for this tenant and is linked onto the receipt.
    doc = db_session.query(Document).filter(Document.id == body["label_document_id"]).one()
    assert doc.company_id == 1
    assert doc.document_type == DocumentType.RECEIVING_LABEL
    db_session.refresh(receipt)
    assert receipt.label_document_id == doc.id

    # A label_print audit row was written on the tamper-evident chain (no secret).
    label_audits = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "label_print", AuditLog.resource_id == receipt.id)
        .all()
    )
    assert len(label_audits) == 1
    assert label_audits[0].action == "CREATE"
    import json as _json

    blob = _json.dumps({"n": label_audits[0].new_values, "d": label_audits[0].description})
    assert "PBX_LIVE_SECRET_8642" not in blob


def test_print_label_uses_profile_default_copies_when_not_overridden(client: TestClient, db_session: Session):
    """No copies in the body -> the profile's default_copies is used."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_receipt(db_session, company_id=1)
    profile = make_profile(db_session, company_id=1, allow_egress=True)
    profile.default_copies = 3
    db_session.commit()

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        resp = client.post(
            f"/api/v1/receiving/receipt/{receipt.id}/print-label",
            headers=headers_for(admin),
            json={},
        )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    instance.print_and_wait.assert_awaited_once()
    assert instance.print_and_wait.call_args.kwargs["copies"] == 3


# ===========================================================================
# POST /print-label -- egress kill switch (409, NO outbound call)
# ===========================================================================


def test_print_label_egress_off_is_409_and_makes_no_proxybox_call(client: TestClient, db_session: Session):
    """SAFETY: with allow_print_egress OFF -> 409 and ProxyBox is NEVER constructed."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, allow_egress=False)

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        resp = client.post(
            f"/api/v1/receiving/receipt/{receipt.id}/print-label",
            headers=headers_for(admin),
            json={},
        )

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    # The egress gate fired BEFORE any client construction or print call.
    constructor.assert_not_called()
    instance.print_and_wait.assert_not_awaited()
    # No Document was generated for the receipt either.
    db_session.refresh(receipt)
    assert receipt.label_document_id is None


def test_print_label_no_profile_is_409_and_makes_no_proxybox_call(client: TestClient, db_session: Session):
    """No print profile configured at all -> still gated (409), no outbound call."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_receipt(db_session, company_id=1)  # no profile created

    constructor, _ = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        resp = client.post(
            f"/api/v1/receiving/receipt/{receipt.id}/print-label",
            headers=headers_for(admin),
            json={},
        )

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    constructor.assert_not_called()


# ===========================================================================
# POST /print-label -- tenant isolation (404 cross-tenant)
# ===========================================================================


def test_print_label_cross_tenant_receipt_is_404(client: TestClient, db_session: Session):
    """A company-1 admin cannot print a company-2 receipt (404). Egress ON for BOTH."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    make_profile(db_session, company_id=1, allow_egress=True)
    other_receipt = make_receipt(db_session, company_id=2)
    make_profile(db_session, company_id=2, allow_egress=True)

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        resp = client.post(
            f"/api/v1/receiving/receipt/{other_receipt.id}/print-label",
            headers=headers_for(admin1),
            json={},
        )

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Receipt not found"
    # Tenant-1's own (enabled) profile must NOT have been used to print another tenant's row.
    instance.print_and_wait.assert_not_awaited()


# ===========================================================================
# POST /print-label -- RBAC
# ===========================================================================


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY, UserRole.VIEWER])
def test_print_label_forbidden_for_unauthorized_roles(client: TestClient, db_session: Session, role: UserRole):
    """OPERATOR / QUALITY / VIEWER may NOT print a label (403)."""
    user = make_user(db_session, role=role, company_id=1)
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, allow_egress=True)

    constructor, _ = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        resp = client.post(
            f"/api/v1/receiving/receipt/{receipt.id}/print-label",
            headers=headers_for(user),
            json={},
        )

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    constructor.assert_not_called()


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
def test_print_label_allowed_for_authorized_roles(client: TestClient, db_session: Session, role: UserRole):
    """ADMIN / MANAGER / SUPERVISOR may print (not 403)."""
    user = make_user(db_session, role=role, company_id=1)
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, allow_egress=True)

    constructor, _ = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        resp = client.post(
            f"/api/v1/receiving/receipt/{receipt.id}/print-label",
            headers=headers_for(user),
            json={},
        )

    assert resp.status_code != status.HTTP_403_FORBIDDEN, resp.text
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_print_label_requires_auth(client: TestClient, db_session: Session):
    """No bearer token -> 401."""
    receipt = make_receipt(db_session, company_id=1)
    resp = client.post(f"/api/v1/receiving/receipt/{receipt.id}/print-label", json={})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ===========================================================================
# POST /print-label -- printer failure maps to 502 but Document is retained
# ===========================================================================


def test_print_label_proxybox_failure_is_502_but_document_retained(client: TestClient, db_session: Session):
    """A ProxyBoxError -> 502, yet the rendered label Document is still committed for reprint."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, allow_egress=True)

    instance = MagicMock()
    instance.print_and_wait = AsyncMock(side_effect=ProxyBoxError("printer offline"))
    constructor = MagicMock(return_value=instance)

    with patch(PROXYBOX_CLIENT_PATH, constructor):
        resp = client.post(
            f"/api/v1/receiving/receipt/{receipt.id}/print-label",
            headers=headers_for(admin),
            json={},
        )

    assert resp.status_code == status.HTTP_502_BAD_GATEWAY, resp.text
    instance.print_and_wait.assert_awaited_once()

    # Record retention: the Document + link were committed BEFORE the print POST.
    db_session.refresh(receipt)
    assert receipt.label_document_id is not None
    doc = db_session.query(Document).filter(Document.id == receipt.label_document_id).one()
    assert doc.company_id == 1
    assert doc.document_type == DocumentType.RECEIVING_LABEL


# ===========================================================================
# GET / PUT /print-profile -- admin-only, secret hygiene, egress audit
# ===========================================================================


def test_get_print_profile_404_until_created(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    resp = client.get("/api/v1/receiving/print-profile", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_upsert_print_profile_admin_creates_and_hides_raw_key(client: TestClient, db_session: Session):
    """Admin can create the profile; the raw key is encrypted and never returned (last4 only)."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = client.put(
        "/api/v1/receiving/print-profile",
        headers=headers_for(admin),
        json={
            "proxybox_base_url": "https://pbx-x.pbxz.cloud/api/v1",
            "proxybox_target": "usb_sn_ABC123",
            "api_key": "PBX_LIVE_WRITEONLY_4242",
            "default_copies": 2,
        },
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["proxybox_target"] == "usb_sn_ABC123"
    assert body["has_api_key"] is True
    assert body["api_key_last4"] == "4242"
    # The raw key must NOT appear anywhere in the response body.
    assert "api_key" not in body
    import json as _json

    assert "PBX_LIVE_WRITEONLY_4242" not in _json.dumps(body)
    # Created OFF by default (egress kill switch).
    assert body["allow_print_egress"] is False

    # Persisted encrypted, not plaintext.
    profile = db_session.query(CompanyPrintProfile).filter(CompanyPrintProfile.company_id == 1).one()
    assert profile.encrypted_api_key
    assert profile.encrypted_api_key != "PBX_LIVE_WRITEONLY_4242"
    assert profile.api_key_last4 == "4242"


def test_get_print_profile_after_upsert_never_exposes_raw_key(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    make_profile(db_session, company_id=1, allow_egress=False, api_key="PBX_GET_SECRET_7777")

    resp = client.get("/api/v1/receiving/print-profile", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["api_key_last4"] == "7777"
    assert body["has_api_key"] is True
    import json as _json

    assert "PBX_GET_SECRET_7777" not in _json.dumps(body)


@pytest.mark.parametrize("role", [UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY, UserRole.OPERATOR])
def test_upsert_print_profile_rejected_for_non_admin(client: TestClient, db_session: Session, role: UserRole):
    """Only ADMIN may configure the printer / enter the API key (403 otherwise)."""
    user = make_user(db_session, role=role, company_id=1)
    resp = client.put(
        "/api/v1/receiving/print-profile",
        headers=headers_for(user),
        json={"proxybox_target": "nope"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    # Nothing was written.
    assert db_session.query(CompanyPrintProfile).filter(CompanyPrintProfile.company_id == 1).first() is None


def test_get_print_profile_rejected_for_non_admin(client: TestClient, db_session: Session):
    user = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    make_profile(db_session, company_id=1)
    resp = client.get("/api/v1/receiving/print-profile", headers=headers_for(user))
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_upsert_print_profile_flipping_egress_is_audited(client: TestClient, db_session: Session):
    """Flipping allow_print_egress ON is recorded as a STATUS_CHANGE on the tamper-evident trail."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    # Start with a profile that has egress OFF.
    make_profile(db_session, company_id=1, allow_egress=False)

    resp = client.put(
        "/api/v1/receiving/print-profile",
        headers=headers_for(admin),
        json={"allow_print_egress": True},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["allow_print_egress"] is True

    status_changes = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "company_print_profile",
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )
    assert len(status_changes) == 1
    assert status_changes[0].old_values == {"status": "egress_disabled"}
    assert status_changes[0].new_values == {"status": "egress_enabled"}


def test_upsert_print_profile_no_egress_change_no_status_audit(client: TestClient, db_session: Session):
    """Updating an unrelated field without touching egress emits NO egress STATUS_CHANGE."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    make_profile(db_session, company_id=1, allow_egress=True)

    resp = client.put(
        "/api/v1/receiving/print-profile",
        headers=headers_for(admin),
        json={"default_copies": 4},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    status_changes = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "company_print_profile",
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )
    assert status_changes == []


# ===========================================================================
# receive_material best-effort auto-print enqueue (after commit, Redis-failure-safe)
# ===========================================================================

# enqueue is imported INTO the receiving endpoint module; patch it there.
ENQUEUE_PATH = "app.api.endpoints.receiving.enqueue_job_best_effort"


def _make_open_po_line(db: Session, *, company_id: int = 1) -> PurchaseOrderLine:
    n = _next()
    vendor = Vendor(code=f"V{n:05d}", name=f"Vendor {n}", is_active=True, is_approved=True, company_id=company_id)
    db.add(vendor)
    part = Part(
        part_number=f"P-{n:05d}",
        name=f"Part {n}",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    po = PurchaseOrder(
        po_number=f"PO-{n:05d}",
        vendor_id=vendor.id,
        status=POStatus.SENT,
        order_date=date.today(),
        company_id=company_id,
    )
    db.add(po)
    db.flush()
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=10,
        quantity_received=0.0,
        unit_price=5.0,
        is_closed=False,
        company_id=company_id,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def test_receive_enqueues_autoprint_job_after_commit(client: TestClient, db_session: Session):
    """A successful receive enqueues the auto-print job with company_id/receipt_id/user_id."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = _make_open_po_line(db_session, company_id=1)

    with patch(ENQUEUE_PATH) as enqueue:
        resp = client.post(
            "/api/v1/receiving/receive",
            headers=headers_for(admin),
            json={
                "po_line_id": line.id,
                "quantity_received": 5,
                "lot_number": f"LOT-ENQ-{_next():05d}",
                "requires_inspection": False,
            },
        )

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    receipt_id = resp.json()["id"]

    enqueue.assert_called_once()
    args, kwargs = enqueue.call_args
    assert args[0] == "print_receiving_label_job"
    assert kwargs["company_id"] == 1
    assert kwargs["receipt_id"] == receipt_id
    assert kwargs["user_id"] == admin.id

    # The receipt is committed and visible -- the enqueue happens AFTER the commit.
    assert db_session.query(POReceipt).filter(POReceipt.id == receipt_id).first() is not None


def test_receive_succeeds_even_when_redis_is_down(client: TestClient, db_session: Session):
    """A Redis outage must NOT fail an already-committed receipt.

    Exercise the REAL ``enqueue_job_best_effort`` (not a mock of it) with the inner
    Redis pool creation failing -- the function is contractually required to swallow
    the error so the committed receipt still returns success. Patching the underlying
    ``create_pool`` rather than ``enqueue_job_best_effort`` keeps the swallow path
    under test instead of bypassing it.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = _make_open_po_line(db_session, company_id=1)

    async def _boom(*_a, **_k):
        raise RuntimeError("redis down")

    with patch("app.core.queue.create_pool", side_effect=_boom):
        resp = client.post(
            "/api/v1/receiving/receive",
            headers=headers_for(admin),
            json={
                "po_line_id": line.id,
                "quantity_received": 5,
                "lot_number": f"LOT-ENQFAIL-{_next():05d}",
                "requires_inspection": False,
            },
        )

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    receipt_id = resp.json()["id"]
    assert db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one().company_id == 1
