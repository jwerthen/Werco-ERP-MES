"""Behavior locks for the Batch-11C Certificate of Conformance generation (G6-B).

A CoC is an APPEND-ONLY, per-Shipment frozen-snapshot compliance artifact. The service
mints the DB row (capturing an immutable ``content_snapshot``) and renders the PDF
DETERMINISTICALLY from that snapshot -- there is no filesystem blob.

Covered:
- (a) ``generate_coc_for_shipment`` creates a row with the frozen snapshot, writes an
  ``audit.log_create``, sets ``shipment.cert_of_conformance=True``.
- (b) idempotent -- a 2nd call (and a concurrent-style duplicate) returns the SAME row,
  no duplicate, no 2nd audit row.
- (c) ``coc_required_for_shipment`` true when the shipment flag set OR a matching
  company-scoped Customer has ``requires_coc`` (default True).
- (d) auto-trigger: ``POST /shipping/{id}/ship`` creates a CoC when required; a
  CoC-generation failure must NOT fail the ship.
- (e) endpoints: ``POST /coc`` RBAC (ADMIN/MANAGER/QUALITY ok; OPERATOR forbidden),
  cross-tenant 404, ``GET /coc`` 404 when none, ``GET /coc/pdf`` returns
  ``application/pdf`` bytes starting with ``%PDF``.
- (f) ``render_coc_pdf`` is deterministic from ``content_snapshot``.
"""

import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.customer import Customer
from app.models.part import Part
from app.models.shipping import CertificateOfConformance, Shipment, ShipmentStatus
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.coc_service import (
    coc_required_for_shipment,
    generate_coc_for_shipment,
    render_coc_pdf,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"b11c-g6b-{n}@co{company_id}.test",
        employee_id=f"B11CG6B-{n:05d}",
        first_name="B11C",
        last_name="G6B",
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


def make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"B11CG6B-P-{n}",
        name=f"Bracket {n}",
        description="batch11c G6B fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        revision="C",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"B11CG6B-WC-{n}",
        code=f"B11CG6B-WC-{n}",
        work_center_type="welding",
        description="batch11c G6B fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session,
    part: Part,
    *,
    customer_name: str = "Acme Aerospace",
    customer_po: str = "PO-9001",
    lot_number: str = "LOT-77",
    serial_numbers: str = None,
    status_: WorkOrderStatus = WorkOrderStatus.COMPLETE,
    company_id: int = COMPANY_A,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B11CG6B-WO-{n:05d}",
        customer_name=customer_name,
        customer_po=customer_po,
        part_id=part.id,
        quantity_ordered=10,
        quantity_complete=10,
        lot_number=lot_number,
        serial_numbers=serial_numbers,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_shipment(
    db: Session,
    wo: WorkOrder,
    *,
    quantity_shipped: float = 4,
    cert_of_conformance: bool = False,
    status_: ShipmentStatus = ShipmentStatus.PENDING,
    ship_date=None,
    company_id: int = COMPANY_A,
) -> Shipment:
    n = _next()
    shipment = Shipment(
        shipment_number=f"B11CG6B-SHP-{n:05d}",
        work_order_id=wo.id,
        status=status_,
        quantity_shipped=quantity_shipped,
        cert_of_conformance=cert_of_conformance,
        ship_date=ship_date,
        company_id=company_id,
    )
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    return shipment


def make_customer(db: Session, name: str, *, requires_coc: bool = True, company_id: int = COMPANY_A) -> Customer:
    _ensure_company(db, company_id)
    n = _next()
    customer = Customer(
        name=name,
        code=f"CUST-{n}",
        requires_coc=requires_coc,
        company_id=company_id,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _coc_rows(db: Session, shipment_id: int, company_id: int = COMPANY_A) -> list[CertificateOfConformance]:
    return (
        db.query(CertificateOfConformance)
        .filter(
            CertificateOfConformance.company_id == company_id,
            CertificateOfConformance.shipment_id == shipment_id,
        )
        .all()
    )


def _coc_create_audit(db: Session, coc_number: str, company_id: int = COMPANY_A) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.resource_type == "certificate_of_conformance",
            AuditLog.resource_identifier == coc_number,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# (a) generate_coc_for_shipment: row + frozen snapshot + audit + shipment flag
# ---------------------------------------------------------------------------


def test_generate_coc_creates_row_snapshot_audit_and_flag(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, serial_numbers=json.dumps(["SN-1", "SN-2"]))
    shipment = make_shipment(db_session, wo, quantity_shipped=5, ship_date=date.today())
    db_session.commit()

    audit = AuditService(db_session, user)
    coc = generate_coc_for_shipment(db_session, shipment=shipment, company_id=COMPANY_A, user_id=user.id, audit=audit)
    db_session.commit()

    assert coc.id is not None
    assert coc.coc_number == f"COC-{shipment.shipment_number}"
    assert coc.company_id == COMPANY_A
    assert coc.shipment_id == shipment.id
    assert coc.work_order_id == wo.id
    assert coc.part_id == part.id
    assert coc.quantity == 5

    # Frozen snapshot is self-contained.
    snap = json.loads(coc.content_snapshot)
    assert snap["coc_number"] == coc.coc_number
    assert snap["customer_name"] == wo.customer_name
    assert snap["customer_po"] == wo.customer_po
    assert snap["work_order_number"] == wo.work_order_number
    assert snap["part_number"] == part.part_number
    assert snap["revision"] == part.revision
    assert snap["lot_number"] == wo.lot_number
    assert snap["serial_numbers"] == ["SN-1", "SN-2"]
    assert snap["issued_by_name"] == user.full_name
    assert snap["conformance_statement"]

    # Audit row written on first issue.
    assert len(_coc_create_audit(db_session, coc.coc_number)) == 1

    # Shipment flag flipped.
    db_session.refresh(shipment)
    assert shipment.cert_of_conformance is True


# ---------------------------------------------------------------------------
# (b) idempotency: 2nd + concurrent-style call returns the SAME row, no 2nd audit
# ---------------------------------------------------------------------------


def test_generate_coc_is_idempotent_no_duplicate_no_second_audit(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    audit = AuditService(db_session, user)
    first = generate_coc_for_shipment(db_session, shipment=shipment, company_id=COMPANY_A, user_id=user.id, audit=audit)
    db_session.commit()

    # Second call returns the SAME row.
    second = generate_coc_for_shipment(
        db_session, shipment=shipment, company_id=COMPANY_A, user_id=user.id, audit=audit
    )
    db_session.commit()

    assert second.id == first.id
    assert len(_coc_rows(db_session, shipment.id)) == 1
    assert len(_coc_create_audit(db_session, first.coc_number)) == 1, "no second audit row on idempotent re-issue"


def test_generate_coc_concurrent_duplicate_resolves_to_winner(client: TestClient, db_session: Session):
    """Simulate the double-ship race: the existence pre-check is bypassed (returns None) so
    the second insert reaches the flush and trips a CoC unique constraint; the service rolls
    back ONLY the savepoint and re-queries the winner -- one row, no error, outer txn usable.
    Regression guard for the fix that moved ``db.add(coc)`` inside the ``begin_nested`` try
    (so the collision IntegrityError is caught, not raised from begin_nested's autoflush)."""
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    audit = AuditService(db_session, user)
    winner = generate_coc_for_shipment(
        db_session, shipment=shipment, company_id=COMPANY_A, user_id=user.id, audit=audit
    )
    db_session.commit()

    # Force the existence pre-check to miss on the FIRST call (the racing insert path),
    # then return the real existing row on the post-IntegrityError re-query.
    call = {"n": 0}
    real_existing = winner

    def fake_existing(db, shipment_id, company_id):
        call["n"] += 1
        if call["n"] == 1:
            return None  # pre-check miss -> proceed to insert -> IntegrityError
        return real_existing  # re-query after the savepoint rollback -> the winner

    with patch("app.services.coc_service._existing_coc", side_effect=fake_existing):
        resolved = generate_coc_for_shipment(
            db_session, shipment=shipment, company_id=COMPANY_A, user_id=user.id, audit=audit
        )
    db_session.commit()

    assert resolved.id == winner.id
    assert len(_coc_rows(db_session, shipment.id)) == 1


# ---------------------------------------------------------------------------
# (c) coc_required_for_shipment
# ---------------------------------------------------------------------------


def test_coc_required_when_shipment_flagged(db_session: Session):
    part = make_part(db_session)
    wo = make_wo(db_session, part, customer_name="No Match Co")
    shipment = make_shipment(db_session, wo, cert_of_conformance=True)
    db_session.commit()
    assert coc_required_for_shipment(db_session, work_order=wo, shipment=shipment, company_id=COMPANY_A) is True


def test_coc_required_when_customer_requires_it(db_session: Session):
    part = make_part(db_session)
    wo = make_wo(db_session, part, customer_name="Boeing Tier 1")
    make_customer(db_session, "Boeing Tier 1", requires_coc=True)
    shipment = make_shipment(db_session, wo, cert_of_conformance=False)
    db_session.commit()
    assert coc_required_for_shipment(db_session, work_order=wo, shipment=shipment, company_id=COMPANY_A) is True


def test_coc_not_required_when_neither_flag_nor_customer(db_session: Session):
    part = make_part(db_session)
    wo = make_wo(db_session, part, customer_name="Casual Customer")
    make_customer(db_session, "Casual Customer", requires_coc=False)
    shipment = make_shipment(db_session, wo, cert_of_conformance=False)
    db_session.commit()
    assert coc_required_for_shipment(db_session, work_order=wo, shipment=shipment, company_id=COMPANY_A) is False


def test_coc_required_customer_match_is_tenant_scoped(db_session: Session):
    """A requires_coc customer of the SAME name but in company B must not make a company-A
    shipment require a CoC."""
    part = make_part(db_session, company_id=COMPANY_A)
    wo = make_wo(db_session, part, customer_name="Cross Tenant Co", company_id=COMPANY_A)
    make_customer(db_session, "Cross Tenant Co", requires_coc=True, company_id=COMPANY_B)
    shipment = make_shipment(db_session, wo, cert_of_conformance=False, company_id=COMPANY_A)
    db_session.commit()
    assert coc_required_for_shipment(db_session, work_order=wo, shipment=shipment, company_id=COMPANY_A) is False


# ---------------------------------------------------------------------------
# (d) auto-trigger on ship + failure must not fail the ship
# ---------------------------------------------------------------------------


def test_ship_auto_generates_coc_when_required(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo, cert_of_conformance=True)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    rows = _coc_rows(db_session, shipment.id)
    assert len(rows) == 1
    assert rows[0].coc_number == f"COC-{shipment.shipment_number}"


def test_ship_does_not_generate_coc_when_not_required(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, customer_name="Plain Co")
    make_customer(db_session, "Plain Co", requires_coc=False)
    shipment = make_shipment(db_session, wo, cert_of_conformance=False)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert _coc_rows(db_session, shipment.id) == []


def test_ship_succeeds_even_if_coc_generation_fails(client: TestClient, db_session: Session):
    """A CoC-generation failure is best-effort: the ship still succeeds (200) and the WO
    still closes."""
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo, cert_of_conformance=True)
    db_session.commit()

    with patch(
        "app.api.endpoints.shipping.generate_coc_for_shipment",
        side_effect=RuntimeError("boom"),
    ):
        resp = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    # The ship + WO close still committed; no CoC row.
    assert db_session.get(Shipment, shipment.id).status == ShipmentStatus.SHIPPED
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.CLOSED
    assert _coc_rows(db_session, shipment.id) == []


# ---------------------------------------------------------------------------
# (e) endpoints: RBAC, tenant scope, GET 404, PDF
# ---------------------------------------------------------------------------


def test_post_coc_allows_quality_role(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(quality))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["coc_number"] == f"COC-{shipment.shipment_number}"


def test_post_coc_forbidden_for_operator(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _coc_rows(db_session, shipment.id) == []


def test_post_coc_cross_tenant_shipment_404(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    part_b = make_part(db_session, company_id=COMPANY_B)
    wo_b = make_wo(db_session, part_b, company_id=COMPANY_B)
    shipment_b = make_shipment(db_session, wo_b, company_id=COMPANY_B)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment_b.id}/coc", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    # Nothing minted under either tenant.
    assert _coc_rows(db_session, shipment_b.id, company_id=COMPANY_B) == []


def test_post_coc_is_idempotent_via_endpoint(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    first = client.post(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(manager))
    second = client.post(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(manager))
    assert first.status_code == status.HTTP_200_OK, first.text
    assert second.status_code == status.HTTP_200_OK, second.text
    assert first.json()["id"] == second.json()["id"]
    db_session.expire_all()
    assert len(_coc_rows(db_session, shipment.id)) == 1


def test_get_coc_404_when_none(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.get(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(user))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_get_coc_returns_metadata_after_issue(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    viewer = make_user(db_session, role=UserRole.OPERATOR)  # GET is open to any active user
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    client.post(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(admin))
    resp = client.get(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(viewer))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["coc_number"] == f"COC-{shipment.shipment_number}"


def test_get_coc_pdf_returns_pdf_bytes(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    wo = make_wo(db_session, part, serial_numbers=json.dumps(["SN-A", "SN-B"]))
    shipment = make_shipment(db_session, wo, ship_date=date.today())
    db_session.commit()

    client.post(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(admin))
    resp = client.get(f"/api/v1/shipping/{shipment.id}/coc/pdf", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")


def test_get_coc_pdf_404_when_none(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.get(f"/api/v1/shipping/{shipment.id}/coc/pdf", headers=headers_for(user))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


# ---------------------------------------------------------------------------
# (f) render_coc_pdf determinism from the frozen snapshot
# ---------------------------------------------------------------------------


def _captured_pdf_facts(coc) -> dict:
    """Render the CoC while intercepting the facts handed to the PDF builder.

    The raw PDF bytes are NOT byte-deterministic (reportlab seeds a random document /ID
    on every build), so determinism is asserted at the FACTS level -- the kwargs the
    snapshot resolves to -- which is the actual content_snapshot contract. We still let
    the real builder run so the produced bytes are exercised."""
    captured = {}
    real_builder = render_coc_pdf.__globals__["build_certificate_of_conformance_pdf"]

    def spy(**kwargs):
        captured.update(kwargs)
        return real_builder(**kwargs)

    with patch("app.services.coc_service.build_certificate_of_conformance_pdf", side_effect=spy):
        pdf = render_coc_pdf(coc)
    return {"facts": captured, "pdf": pdf}


def test_render_coc_pdf_deterministic_facts_from_snapshot(db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, serial_numbers=json.dumps(["SN-9"]))
    shipment = make_shipment(db_session, wo, ship_date=date.today())
    db_session.commit()

    audit = AuditService(db_session, user)
    coc = generate_coc_for_shipment(db_session, shipment=shipment, company_id=COMPANY_A, user_id=user.id, audit=audit)
    db_session.commit()

    first = _captured_pdf_facts(coc)
    second = _captured_pdf_facts(coc)
    # Valid PDF output each time.
    assert first["pdf"].startswith(b"%PDF")
    assert second["pdf"].startswith(b"%PDF")
    # Deterministic at the facts level: the same frozen snapshot resolves to the same
    # rendered facts every time, and they match the snapshot.
    assert first["facts"] == second["facts"]
    snap = json.loads(coc.content_snapshot)
    assert first["facts"]["coc_number"] == snap["coc_number"]
    assert first["facts"]["customer_name"] == snap["customer_name"]
    assert first["facts"]["part_number"] == snap["part_number"]
    assert first["facts"]["serial_numbers"] == snap["serial_numbers"]
    assert first["facts"]["issued_by_name"] == snap["issued_by_name"]


def test_render_coc_pdf_uses_snapshot_over_mutated_columns(db_session: Session):
    """The render reads the frozen snapshot, not the live (mutable) denormalized columns:
    mutating a column after issue must not change the rendered facts."""
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo, ship_date=date.today())
    db_session.commit()

    audit = AuditService(db_session, user)
    coc = generate_coc_for_shipment(db_session, shipment=shipment, company_id=COMPANY_A, user_id=user.id, audit=audit)
    db_session.commit()

    before = _captured_pdf_facts(coc)["facts"]
    # Tamper with the denormalized column (NOT the snapshot).
    coc.customer_name = "TAMPERED CUSTOMER"
    db_session.flush()
    after = _captured_pdf_facts(coc)["facts"]
    assert after["customer_name"] == before["customer_name"], "render must read content_snapshot, not the row column"
    assert after["customer_name"] != "TAMPERED CUSTOMER"
