"""Every supplier-scorecard endpoint is scoped to the caller's company, and its write stamps it.

``api/endpoints/supplier_scorecards.py`` had 15 of 16 handlers reaching outside the
caller's tenant, in three distinct shapes.

**A. Three cross-tenant WRITES.** ``update_scorecard``, ``update_audit`` and
``update_approved_supplier`` resolved their row by bare id -- ``update_scorecard`` even
took a ``company_id`` dependency and never used it. Guessing an integer was enough to
rewrite another company's AS9100D supplier evaluation, fail their audit, or set their
approved supplier to ``removed``. §2 asserts the VICTIM'S STORED ROW after each refusal,
not the caller's status code: a handler that 404s and writes anyway passes a status-only
test.

**B. ``auto_calculate_scorecard`` was 500ing in production, for two reasons.** The
``SupplierScorecard`` insert omitted ``TenantMixin``'s NOT NULL ``company_id`` -- but the
handler never even reached it, because ``calculate_overall()`` runs on the in-memory
object before the flush, where the four weight COLUMN defaults are still ``None``
(``float * None``). ``create_scorecard`` never hit that because ``ScorecardCreate``
carries the same values as SCHEMA defaults. §1 pins both: a 200, the stored
``company_id``, and the stored weights.

**C. Every read was platform-wide, and the three creates trusted ``vendor_id``.** The
dashboard, ranking, both lists, due-soon and the two detail reads took no company
argument. The creates stamped ``company_id`` correctly but resolved the vendor with no
predicate -- and each serializer renders ``vendor.name`` / ``vendor.code`` straight back,
so a create doubled as a read of the foreign supplier. Same defect shape #191 closed on
operation work centers.

Two things shaped these tests
-----------------------------
* **Aggregates need a MIS-TENANTED row, not just a foreign one.** ``auto_calculate``
  walks PO -> line -> receipt -> NCR by id. Scoping only the first hop would look correct
  in a "B cannot see A's vendor" test while a foreign PO hanging off B's own vendor still
  moved B's quality and delivery scores. §4 constructs exactly that (``company_id`` is
  part of no FK, on SQLite or on Postgres).

* **A refusal must be indistinguishable from an absent id** -- 404, never 403 -- or the
  status code is itself an existence oracle over another tenant's supplier list.

Three more sections were added after review (§7-§9)
---------------------------------------------------
* **§7 Egress.** Scoping the CREATES closes ingress and does nothing about rows written
  BEFORE the guards. Such a row is owned by the caller and passes every ``company_id``
  filter, while the ``vendor`` relationship (and the ``joinedload`` on it) carries no
  predicate -- so it still rendered the FOREIGN supplier's name and code. The serializers
  now null both, and the audit identifier falls back to ``vendor #{id}``.

* **§8 The OTHER ``calculate_overall(None)`` crash.** Fixing it in ``auto_calculate`` left
  ``update_scorecard`` 500ing on an explicitly-sent ``null``, which
  ``model_dump(exclude_unset=True)`` keeps. Fixing one of two call sites of a
  crash-on-None reads as fixed and is not.

* **§9 Audit rows.** Invariant 2: the router wrote none at all, so who downgraded a
  supplier to ``Disqualified`` or flipped an ASL entry to ``removed`` was unrecoverable.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.part import Part
from app.models.purchasing import POReceipt, POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quality import NCRSource, NonConformanceReport
from app.models.supplier_scorecard import ApprovedSupplierList, SupplierAudit, SupplierScorecard
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1  # the seeded company -- plays the victim throughout
COMPANY_B = 2  # the caller that must never reach A's data

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

# Sentinels distinctive enough that a substring search over a whole response body is a
# real leak check. Who a competitor buys from, and how that supplier is scoring, is the
# disclosure that matters here.
FOREIGN_VENDOR_NAME = "ACME-SECRET Aerospace Alloys Inc"
FOREIGN_VENDOR_CODE = "VND-ACME-SECRET"
FOREIGN_NOTES = "ACME-SECRET titanium contract renegotiation"

SCORECARDS = "/api/v1/supplier-scorecards/supplier-scorecards"
AUDITS = "/api/v1/supplier-scorecards/supplier-audits"
ASL = "/api/v1/supplier-scorecards/approved-suppliers"

PERIOD = {"period_start": "2026-01-01", "period_end": "2026-03-31"}

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, company_id: int, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"ssc-iso-{n}@co{company_id}.test",
        employee_id=f"SSCISO-{n:05d}",
        first_name="Tenant",
        last_name="Isolation",
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


def make_vendor(db: Session, *, company_id: int, name: str = None, code: str = None) -> Vendor:
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(
        code=code or f"VND-ISO-{n}",
        name=name or f"Isolation vendor {n}",
        is_active=True,
        is_approved=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def foreign_vendor(db: Session) -> Vendor:
    """Company A's supplier -- the one Company B must never read, write or name."""
    return make_vendor(db, company_id=COMPANY_A, name=FOREIGN_VENDOR_NAME, code=FOREIGN_VENDOR_CODE)


def make_scorecard(db: Session, *, company_id: int, vendor_id: int, **overrides) -> SupplierScorecard:
    fields = {
        "company_id": company_id,
        "vendor_id": vendor_id,
        "period_type": "quarterly",
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 3, 31),
        "quality_score": 91.0,
        "quality_weight": 0.40,
        "delivery_score": 88.0,
        "delivery_weight": 0.30,
        "responsiveness_score": 80.0,
        "responsiveness_weight": 0.15,
        "price_score": 80.0,
        "price_weight": 0.15,
        "overall_score": 88.0,
        "rating": "Good",
    }
    fields.update(overrides)
    scorecard = SupplierScorecard(**fields)
    db.add(scorecard)
    db.commit()
    db.refresh(scorecard)
    return scorecard


def make_audit(db: Session, *, company_id: int, vendor_id: int, **overrides) -> SupplierAudit:
    fields = {
        "company_id": company_id,
        "vendor_id": vendor_id,
        "audit_type": "Annual",
        "audit_date": date(2026, 1, 15),
        "next_audit_date": date.today() + timedelta(days=5),
        "result": "passed",
        "score": 95.0,
    }
    fields.update(overrides)
    audit = SupplierAudit(**fields)
    db.add(audit)
    db.commit()
    db.refresh(audit)
    return audit


def make_asl(db: Session, *, company_id: int, vendor_id: int, **overrides) -> ApprovedSupplierList:
    fields = {
        "company_id": company_id,
        "vendor_id": vendor_id,
        "approval_status": "approved",
        "approved_date": date(2026, 1, 1),
        "next_review_date": date.today() + timedelta(days=5),
        "review_frequency_months": 12,
        "certifications_verified": True,
    }
    fields.update(overrides)
    entry = ApprovedSupplierList(**fields)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_part(db: Session, *, company_id: int) -> Part:
    n = _next()
    part = Part(
        part_number=f"SSC-ISO-P-{n}",
        name=f"Isolation part {n}",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_po_with_receipt(
    db: Session,
    *,
    row_company_id: int,
    vendor_id: int,
    received: float = 100.0,
    rejected: float = 0.0,
    received_by: int,
    late: bool = False,
) -> POReceipt:
    """A PO -> line -> receipt chain, all three rows stamped ``row_company_id``.

    Passing a ``row_company_id`` that disagrees with the vendor's company builds the
    mis-tenanted chain the aggregate has to survive.
    """
    n = _next()
    po = PurchaseOrder(
        company_id=row_company_id,
        po_number=f"PO-ISO-{n}",
        vendor_id=vendor_id,
        status=POStatus.RECEIVED,
        order_date=date(2026, 2, 1),
        required_date=date(2026, 2, 10),
    )
    db.add(po)
    db.commit()
    db.refresh(po)

    line = PurchaseOrderLine(
        company_id=row_company_id,
        purchase_order_id=po.id,
        line_number=1,
        part_id=make_part(db, company_id=row_company_id).id,
        quantity_ordered=received,
        unit_price=1.0,
        required_date=date(2026, 2, 10),
    )
    db.add(line)
    db.commit()
    db.refresh(line)

    receipt = POReceipt(
        company_id=row_company_id,
        receipt_number=f"RCP-ISO-{n}",
        po_line_id=line.id,
        quantity_received=received,
        quantity_accepted=received - rejected,
        quantity_rejected=rejected,
        lot_number=f"LOT-ISO-{n}",
        received_by=received_by,
        received_at=datetime(2026, 3, 1 if late else 2, 12, 0, 0) if late else datetime(2026, 2, 5, 12, 0, 0),
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


def make_ncr(db: Session, *, company_id: int, receipt_id: int, car_id: int = None) -> NonConformanceReport:
    n = _next()
    ncr = NonConformanceReport(
        company_id=company_id,
        ncr_number=f"NCR-ISO-{n}",
        receipt_id=receipt_id,
        source=NCRSource.INCOMING_INSPECTION,
        title=f"Isolation NCR {n}",
        description="fixture",
        detected_date=date(2026, 2, 15),
        car_id=car_id,
    )
    db.add(ncr)
    db.commit()
    db.refresh(ncr)
    return ncr


def assert_discloses_nothing(response) -> None:
    """No sentinel may appear ANYWHERE in the body -- detail, echo or field."""
    body = response.text
    for sentinel in (FOREIGN_VENDOR_NAME, FOREIGN_VENDOR_CODE, FOREIGN_NOTES):
        assert sentinel not in body, f"leaked {sentinel!r}: {body}"


def fresh(db: Session, model, pk: int):
    """The row as COMMITTED, not as the shared session last remembered it."""
    db.expire_all()
    return db.get(model, pk)


def snapshot(db: Session, model, pk: int) -> dict:
    """Every mapped column of a row, for a byte-identical before/after comparison.

    ``updated_at`` carries an ``onupdate``, so any write at all shows up here.
    """
    row = fresh(db, model, pk)
    assert row is not None
    return {c.key: getattr(row, c.key) for c in sa_inspect(model).mapper.column_attrs}


# ===========================================================================
# 1. POST /calculate/{vendor_id} — the production 500, both landmines
# ===========================================================================


def test_auto_calculate_succeeds_and_stamps_the_callers_company_id(client: TestClient, db_session: Session):
    """THE production-bug reproducer, and it has TWO causes.

    ``SupplierScorecard.company_id`` is NOT NULL and the insert omitted it -- but the
    handler never got that far: ``calculate_overall()`` multiplies four scores by four
    weights that are declared as COLUMN defaults, so on the not-yet-flushed object they
    are all ``None`` and the call raised ``float * NoneType`` about twenty lines earlier.
    Fixing only the ``company_id`` would have shipped an endpoint that still 500s, which
    is why the stored WEIGHTS are asserted here and not just the stored scope.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)

    response = client.post(f"{SCORECARDS}/calculate/{vendor_b.id}", headers=headers_for(user_b), json=dict(PERIOD))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert (body["quality_weight"], body["delivery_weight"]) == (0.40, 0.30)
    assert (body["responsiveness_weight"], body["price_weight"]) == (0.15, 0.15)
    assert body["overall_score"] > 0, "a None weight would have made this unreachable"

    stored = fresh(db_session, SupplierScorecard, body["id"])
    assert stored.company_id == COMPANY_B, "the write must stamp the CALLER's company"
    assert stored.evaluated_by == user_b.id
    assert stored.quality_weight == 0.40


def test_auto_calculate_refuses_another_companys_vendor(client: TestClient, db_session: Session):
    """Cross-tenant, this computed and PERSISTED a supplier evaluation from another
    company's purchase, receipt and non-conformance history."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)

    response = client.post(f"{SCORECARDS}/calculate/{stolen.id}", headers=headers_for(user_b), json=dict(PERIOD))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(SupplierScorecard).count() == 0


# ===========================================================================
# 2. The three cross-tenant WRITES
# ===========================================================================


def test_update_scorecard_refuses_another_companys_scorecard(client: TestClient, db_session: Session):
    """``update_scorecard`` DECLARED a ``company_id`` dependency and never used it, so
    any admin or manager could rewrite another company's supplier evaluation -- scores,
    rating, action items -- and the row would then carry their user id as ``evaluated_by``.
    On an AS9100D system that is a quality record being silently authored by an outsider.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    victim = make_scorecard(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    before = snapshot(db_session, SupplierScorecard, victim.id)

    response = client.put(
        f"{SCORECARDS}/{victim.id}",
        headers=headers_for(user_b),
        json={"quality_score": 1.0, "delivery_score": 1.0, "action_items": "disqualify"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    assert snapshot(db_session, SupplierScorecard, victim.id) == before


def test_update_audit_refuses_another_companys_audit(client: TestClient, db_session: Session):
    """Flipping another company's supplier audit to ``failed`` is a finding they never
    made, against a supplier they may not even share."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    victim = make_audit(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    before = snapshot(db_session, SupplierAudit, victim.id)

    response = client.put(
        f"{AUDITS}/{victim.id}",
        headers=headers_for(user_b),
        json={"result": "failed", "score": 0.0, "findings": "written by another tenant"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    assert snapshot(db_session, SupplierAudit, victim.id) == before


def test_update_approved_supplier_refuses_another_companys_entry(client: TestClient, db_session: Session):
    """The sharpest of the three: setting another company's ASL entry to ``removed``
    de-qualifies a live supplier on someone else's approved list."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    victim = make_asl(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    before = snapshot(db_session, ApprovedSupplierList, victim.id)

    response = client.put(
        f"{ASL}/{victim.id}",
        headers=headers_for(user_b),
        json={"approval_status": "removed", "certifications_verified": False},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    assert snapshot(db_session, ApprovedSupplierList, victim.id) == before


def test_the_three_updates_still_work_on_the_callers_own_rows(client: TestClient, db_session: Session):
    """Positive control for all three: scoping narrows WHICH rows are writable, it does
    not switch the endpoints off."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)
    scorecard = make_scorecard(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)
    audit = make_audit(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)
    entry = make_asl(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)
    headers = headers_for(user_b)

    sc_resp = client.put(f"{SCORECARDS}/{scorecard.id}", headers=headers, json={"quality_score": 50.0})
    assert sc_resp.status_code == status.HTTP_200_OK, sc_resp.text
    assert sc_resp.json()["quality_score"] == 50.0

    audit_resp = client.put(f"{AUDITS}/{audit.id}", headers=headers, json={"result": "conditional"})
    assert audit_resp.status_code == status.HTTP_200_OK, audit_resp.text
    assert audit_resp.json()["result"] == "conditional"

    asl_resp = client.put(f"{ASL}/{entry.id}", headers=headers, json={"approval_status": "probationary"})
    assert asl_resp.status_code == status.HTTP_200_OK, asl_resp.text
    assert asl_resp.json()["approval_status"] == "probationary"


# ===========================================================================
# 3. vendor_id ownership on the three creates
# ===========================================================================


def test_create_scorecard_refuses_a_foreign_vendor(client: TestClient, db_session: Session):
    """The create stamped ``company_id`` correctly but never checked WHOSE vendor it was
    pointing at, and ``scorecard_to_response`` renders ``vendor.name``/``vendor.code``
    straight back -- so the write doubled as a read of the foreign supplier."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)

    response = client.post(
        f"{SCORECARDS}/", headers=headers_for(user_b), json={"vendor_id": stolen.id, **PERIOD, "quality_score": 90}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(SupplierScorecard).count() == 0, "a refused create must leave no row behind"


def test_create_audit_refuses_a_foreign_vendor(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)

    response = client.post(
        f"{AUDITS}/",
        headers=headers_for(user_b),
        json={"vendor_id": stolen.id, "audit_type": "Annual", "audit_date": "2026-01-15"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(SupplierAudit).count() == 0


def test_create_approved_supplier_refuses_a_foreign_vendor(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)

    response = client.post(f"{ASL}/", headers=headers_for(user_b), json={"vendor_id": stolen.id})

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(ApprovedSupplierList).count() == 0


def test_one_tenant_cannot_squat_another_tenants_asl_slot(client: TestClient, db_session: Session):
    """The ASL create is worse than a leak: ``ApprovedSupplierList.vendor_id`` carries a
    GLOBAL unique constraint, so Company B creating an entry against Company A's vendor
    consumed the ONE slot that vendor will ever have. A then gets a 400 on its own
    approved-supplier list and cannot recover without a DBA.

    The duplicate check stays deliberately unscoped (it has to mirror the global
    constraint exactly, or a miss becomes a 500 instead of a 400) -- it is the vendor
    scoping above that makes it unreachable across tenants, and this test is what proves
    the combination holds.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_a = foreign_vendor(db_session)

    squat = client.post(f"{ASL}/", headers=headers_for(user_b), json={"vendor_id": vendor_a.id})
    assert squat.status_code == status.HTTP_404_NOT_FOUND, squat.text

    # A can still create its own entry for its own vendor.
    own = client.post(f"{ASL}/", headers=headers_for(user_a), json={"vendor_id": vendor_a.id})
    assert own.status_code == status.HTTP_200_OK, own.text
    assert fresh(db_session, ApprovedSupplierList, own.json()["id"]).company_id == COMPANY_A


def test_the_three_creates_still_work_for_the_callers_own_vendor(client: TestClient, db_session: Session):
    """Positive control: the validation narrows WHICH vendor ids are acceptable, it does
    not break the create paths."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)
    foreign_vendor(db_session)  # present and irrelevant
    headers = headers_for(user_b)

    sc = client.post(f"{SCORECARDS}/", headers=headers, json={"vendor_id": vendor_b.id, **PERIOD, "quality_score": 90})
    assert sc.status_code == status.HTTP_200_OK, sc.text
    assert fresh(db_session, SupplierScorecard, sc.json()["id"]).company_id == COMPANY_B

    audit = client.post(
        f"{AUDITS}/",
        headers=headers,
        json={"vendor_id": vendor_b.id, "audit_type": "Annual", "audit_date": "2026-01-15"},
    )
    assert audit.status_code == status.HTTP_200_OK, audit.text
    assert fresh(db_session, SupplierAudit, audit.json()["id"]).company_id == COMPANY_B

    entry = client.post(f"{ASL}/", headers=headers, json={"vendor_id": vendor_b.id})
    assert entry.status_code == status.HTTP_200_OK, entry.text
    assert fresh(db_session, ApprovedSupplierList, entry.json()["id"]).company_id == COMPANY_B


# ===========================================================================
# 4. auto_calculate's six read legs — the mis-tenanted-row traps
# ===========================================================================


def test_auto_calculate_ignores_another_companys_purchase_orders(client: TestClient, db_session: Session):
    """The PO leg. ``WHERE vendor_id = ?`` with no tenant predicate counted every
    company's purchase orders against that vendor id, so ``total_pos`` and ``total_lines``
    on B's own scorecard reported A's buying volume.

    Reachable through a mis-tenanted row -- a PO owned by A pointing at B's vendor --
    which is constructible because ``company_id`` is part of no FK.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)

    make_po_with_receipt(db_session, row_company_id=COMPANY_B, vendor_id=vendor_b.id, received_by=user_b.id)
    # A's chain, hanging off B's vendor.
    make_po_with_receipt(db_session, row_company_id=COMPANY_A, vendor_id=vendor_b.id, received_by=user_a.id)

    response = client.post(f"{SCORECARDS}/calculate/{vendor_b.id}", headers=headers_for(user_b), json=dict(PERIOD))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["total_pos"] == 1, "another company's purchase orders were counted"
    assert body["total_lines"] == 1


def test_auto_calculate_ignores_another_companys_receipts(client: TestClient, db_session: Session):
    """The receipt leg, which feeds the QUALITY score directly:
    ``(1 - rejected/received) * 100``. A foreign receipt with a large rejected quantity
    dragged this company's supplier quality score down -- or, in the other direction, a
    clean foreign receipt masked a real rejection rate.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)

    own_receipt = make_po_with_receipt(
        db_session, row_company_id=COMPANY_B, vendor_id=vendor_b.id, received=100.0, rejected=0.0, received_by=user_b.id
    )
    # A's receipt, on A's PO chain, pointing at B's vendor: 100% rejected.
    make_po_with_receipt(
        db_session,
        row_company_id=COMPANY_A,
        vendor_id=vendor_b.id,
        received=100.0,
        rejected=100.0,
        received_by=user_a.id,
    )
    assert own_receipt.quantity_rejected == 0.0

    response = client.post(f"{SCORECARDS}/calculate/{vendor_b.id}", headers=headers_for(user_b), json=dict(PERIOD))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["total_received_qty"] == 100.0, "another company's receipts were summed in"
    assert body["rejected_qty"] == 0.0
    assert body["quality_score"] == 100.0


def test_auto_calculate_ignores_another_companys_ncrs(client: TestClient, db_session: Session):
    """The NCR leg. Non-conformances are the supplier-quality evidence chain; counting a
    foreign tenant's NCRs against your vendor puts findings on a scorecard that nobody in
    your organisation ever raised."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)

    receipt = make_po_with_receipt(db_session, row_company_id=COMPANY_B, vendor_id=vendor_b.id, received_by=user_b.id)
    # A's NCR, against B's receipt.
    make_ncr(db_session, company_id=COMPANY_A, receipt_id=receipt.id)

    response = client.post(f"{SCORECARDS}/calculate/{vendor_b.id}", headers=headers_for(user_b), json=dict(PERIOD))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["ncr_count"] == 0, "another company's NCRs were counted"


def test_auto_calculate_still_counts_the_callers_own_data(client: TestClient, db_session: Session):
    """Positive control across all six legs: the predicates narrow WHOSE rows count, they
    do not empty the calculation out."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)

    receipt = make_po_with_receipt(
        db_session,
        row_company_id=COMPANY_B,
        vendor_id=vendor_b.id,
        received=200.0,
        rejected=20.0,
        received_by=user_b.id,
    )
    make_ncr(db_session, company_id=COMPANY_B, receipt_id=receipt.id)

    response = client.post(f"{SCORECARDS}/calculate/{vendor_b.id}", headers=headers_for(user_b), json=dict(PERIOD))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["total_pos"] == 1
    assert body["total_lines"] == 1
    assert body["total_received_qty"] == 200.0
    assert body["rejected_qty"] == 20.0
    assert body["quality_score"] == 90.0
    assert body["ncr_count"] == 1


# ===========================================================================
# 5. The reads and the aggregates
# ===========================================================================


def test_dashboard_counts_only_the_callers_own_suppliers(client: TestClient, db_session: Session):
    """``avg_score``, the below-threshold and probationary counts, audits-due,
    reviews-due, and the named top/worst performer all spanned every tenant -- so a
    competitor's worst supplier could be printed on your dashboard, by name."""
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    make_scorecard(db_session, company_id=COMPANY_A, vendor_id=stolen.id, overall_score=42.0, rating="Disqualified")
    make_audit(db_session, company_id=COMPANY_A, vendor_id=stolen.id)
    make_asl(db_session, company_id=COMPANY_A, vendor_id=stolen.id)

    vendor_b = make_vendor(db_session, company_id=COMPANY_B, name="B preferred supplier")
    make_scorecard(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id, overall_score=95.0, rating="Excellent")

    seen_by_b = client.get(f"{SCORECARDS}/dashboard", headers=headers_for(user_b))
    assert seen_by_b.status_code == status.HTTP_200_OK, seen_by_b.text
    body_b = seen_by_b.json()
    assert body_b["total_vendors_scored"] == 1
    assert body_b["avg_score"] == 95.0
    assert body_b["below_threshold"] == 0
    assert body_b["disqualified_count"] == 0
    assert body_b["audits_due_30_days"] == 0
    assert body_b["reviews_due_30_days"] == 0
    assert body_b["worst_performer"]["vendor_name"] == "B preferred supplier"
    assert_discloses_nothing(seen_by_b)

    # The positive control in the other direction: A still sees its OWN.
    seen_by_a = client.get(f"{SCORECARDS}/dashboard", headers=headers_for(user_a))
    body_a = seen_by_a.json()
    assert body_a["total_vendors_scored"] == 1
    assert body_a["avg_score"] == 42.0
    assert body_a["audits_due_30_days"] == 1
    assert body_a["reviews_due_30_days"] == 1
    assert body_a["worst_performer"]["vendor_name"] == FOREIGN_VENDOR_NAME
    assert "B preferred supplier" not in seen_by_a.text


def test_ranking_returns_only_the_callers_own_suppliers(client: TestClient, db_session: Session):
    """The ranking is the whole supplier base in one response, vendor names included."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    make_scorecard(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)
    own = make_scorecard(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)

    response = client.get(f"{SCORECARDS}/ranking", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert [row["id"] for row in response.json()] == [own.id]
    assert_discloses_nothing(response)


def test_vendor_history_refuses_another_companys_vendor(client: TestClient, db_session: Session):
    """Period-over-period performance for a named supplier -- and the vendor id is the
    only key, so guessing an integer was the whole attack."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    make_scorecard(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)

    response = client.get(f"{SCORECARDS}/vendor/{stolen.id}/history", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)


def test_vendor_history_returns_the_callers_own(client: TestClient, db_session: Session):
    """Positive control, and the second half of the fix: even for an OWNED vendor the
    scorecard read is scoped, so a mis-tenanted scorecard on your own vendor stays out."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)
    own = make_scorecard(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)
    make_scorecard(db_session, company_id=COMPANY_A, vendor_id=vendor_b.id, notes=FOREIGN_NOTES)

    response = client.get(f"{SCORECARDS}/vendor/{vendor_b.id}/history", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert [row["id"] for row in response.json()] == [own.id]
    assert_discloses_nothing(response)


def test_audits_due_soon_lists_only_the_callers_own(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    make_audit(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)
    own = make_audit(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)

    response = client.get(f"{AUDITS}/due-soon", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert [row["id"] for row in response.json()] == [own.id]
    assert_discloses_nothing(response)


def test_each_company_only_ever_lists_its_own(client: TestClient, db_session: Session):
    """End-to-end shape of the invariant across the three list endpoints.

    ``GET /supplier-scorecards/`` was already scoped before this change; it is swept in
    here so a future edit cannot quietly unscope it while the other two stay covered.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    sc_a = make_scorecard(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    audit_a = make_audit(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    asl_a = make_asl(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)

    vendor_b = make_vendor(db_session, company_id=COMPANY_B, name="B preferred supplier")
    sc_b = make_scorecard(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)
    audit_b = make_audit(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)
    asl_b = make_asl(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)

    for url, expected in ((f"{SCORECARDS}/", sc_b), (f"{AUDITS}/", audit_b), (f"{ASL}/", asl_b)):
        response = client.get(url, headers=headers_for(user_b))
        assert response.status_code == status.HTTP_200_OK, f"{url} -> {response.text}"
        assert [row["id"] for row in response.json()] == [expected.id], url
        assert_discloses_nothing(response)

    for url, expected in ((f"{SCORECARDS}/", sc_a), (f"{AUDITS}/", audit_a), (f"{ASL}/", asl_a)):
        response = client.get(url, headers=headers_for(user_a))
        assert [row["id"] for row in response.json()] == [expected.id], url
        assert "B preferred supplier" not in response.text


# ===========================================================================
# 6. Sweep + the refusal is not an oracle
# ===========================================================================


def test_every_id_keyed_endpoint_refuses_a_foreign_id(client: TestClient, db_session: Session):
    """No per-endpoint exception survives, and none of them leaks a supplier name."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    sc_a = make_scorecard(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    audit_a = make_audit(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    asl_a = make_asl(db_session, company_id=COMPANY_A, vendor_id=stolen.id, notes=FOREIGN_NOTES)
    headers = headers_for(user_b)

    gets = [
        f"{SCORECARDS}/{sc_a.id}",
        f"{SCORECARDS}/vendor/{stolen.id}/history",
        f"{ASL}/{asl_a.id}",
    ]
    for url in gets:
        response = client.get(url, headers=headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND, f"{url} -> {response.status_code}: {response.text}"
        assert_discloses_nothing(response)

    puts = [
        (f"{SCORECARDS}/{sc_a.id}", {"quality_score": 1.0}),
        (f"{AUDITS}/{audit_a.id}", {"result": "failed"}),
        (f"{ASL}/{asl_a.id}", {"approval_status": "removed"}),
    ]
    for url, payload in puts:
        response = client.put(url, headers=headers, json=payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND, f"{url} -> {response.status_code}: {response.text}"
        assert_discloses_nothing(response)

    posts = [
        (f"{SCORECARDS}/calculate/{stolen.id}", dict(PERIOD)),
        (f"{SCORECARDS}/", {"vendor_id": stolen.id, **PERIOD}),
        (f"{AUDITS}/", {"vendor_id": stolen.id, "audit_type": "Annual", "audit_date": "2026-01-15"}),
        (f"{ASL}/", {"vendor_id": stolen.id}),
    ]
    for url, payload in posts:
        response = client.post(url, headers=headers, json=payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND, f"{url} -> {response.status_code}: {response.text}"
        assert_discloses_nothing(response)

    # Nothing above may have landed on A's rows, and nothing new may exist.
    assert fresh(db_session, SupplierScorecard, sc_a.id).quality_score == 91.0
    assert fresh(db_session, SupplierAudit, audit_a.id).result == "passed"
    assert fresh(db_session, ApprovedSupplierList, asl_a.id).approval_status == "approved"
    assert db_session.query(SupplierScorecard).count() == 1
    assert db_session.query(SupplierAudit).count() == 1
    assert db_session.query(ApprovedSupplierList).count() == 1


def test_a_foreign_scorecard_refuses_exactly_like_an_absent_one(client: TestClient, db_session: Session):
    """If the two answers differ at all, the status code is itself an existence oracle
    over another tenant's supplier list."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_vendor(db_session)
    sc_a = make_scorecard(db_session, company_id=COMPANY_A, vendor_id=stolen.id)
    headers = headers_for(user_b)

    foreign = client.get(f"{SCORECARDS}/{sc_a.id}", headers=headers)
    absent = client.get(f"{SCORECARDS}/{sc_a.id + 900_000}", headers=headers)
    assert (foreign.status_code, foreign.json()) == (absent.status_code, absent.json())

    foreign_put = client.put(f"{SCORECARDS}/{sc_a.id}", headers=headers, json={"quality_score": 1.0})
    absent_put = client.put(f"{SCORECARDS}/{sc_a.id + 900_000}", headers=headers, json={"quality_score": 1.0})
    assert (foreign_put.status_code, foreign_put.json()) == (absent_put.status_code, absent_put.json())


# ===========================================================================
# 7. Egress — a LEGACY row pointing at another tenant's vendor
# ===========================================================================
#
# The creates now validate vendor_id, which stops NEW cross-tenant rows. It does nothing
# about rows written BEFORE that guard, and the ``vendor`` relationship (and the
# ``joinedload`` on it) carries no predicate of its own: such a row passes every
# company_id filter in the query -- it really is the caller's row -- and used to render
# the FOREIGN supplier's name and code straight back. Ingress closed, egress open.


def test_a_legacy_scorecard_never_names_another_companys_vendor(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_a = foreign_vendor(db_session)
    # The exact row the pre-fix create_scorecard could write: owned by B, pointed at A's
    # vendor. company_id is part of no FK, so the DB accepts it on SQLite and Postgres.
    legacy = make_scorecard(db_session, company_id=COMPANY_B, vendor_id=vendor_a.id)

    for url in (f"{SCORECARDS}/", f"{SCORECARDS}/{legacy.id}", f"{SCORECARDS}/ranking", f"{SCORECARDS}/dashboard"):
        response = client.get(url, headers=headers_for(user_b))
        assert response.status_code == status.HTTP_200_OK, response.text
        assert_discloses_nothing(response)

    detail = client.get(f"{SCORECARDS}/{legacy.id}", headers=headers_for(user_b)).json()
    assert detail["vendor_name"] is None, "a foreign relation must read as absent, not as a name"
    assert detail["vendor_code"] is None
    assert detail["vendor_id"] == vendor_a.id, "the stored id stays visible so the row can be corrected"


def test_a_legacy_audit_and_asl_never_name_another_companys_vendor(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_a = foreign_vendor(db_session)
    make_audit(db_session, company_id=COMPANY_B, vendor_id=vendor_a.id)
    legacy_asl = make_asl(db_session, company_id=COMPANY_B, vendor_id=vendor_a.id)

    for url in (f"{AUDITS}/", f"{AUDITS}/due-soon", f"{ASL}/", f"{ASL}/{legacy_asl.id}"):
        response = client.get(url, headers=headers_for(user_b))
        assert response.status_code == status.HTTP_200_OK, response.text
        assert_discloses_nothing(response)

    assert client.get(f"{ASL}/{legacy_asl.id}", headers=headers_for(user_b)).json()["vendor_name"] is None


# ===========================================================================
# 8. The OTHER calculate_overall(None) crash — update_scorecard
# ===========================================================================


def test_update_scorecard_refuses_an_explicit_null_instead_of_500ing(client: TestClient, db_session: Session):
    """``auto_calculate_scorecard``'s ``float * None`` crash had a second call site.

    Every numeric field on ``ScorecardUpdate`` is ``Optional[... ] = None``, and
    ``model_dump(exclude_unset=True)`` KEEPS an explicitly-sent ``null`` -- so
    ``{"quality_weight": null}`` reached the setattr loop and then ``calculate_overall``,
    which raised ``unsupported operand type(s) for *: 'float' and 'NoneType'``. Fixing one
    of two call sites of a crash-on-None reads as fixed and is not.

    Pydantic does not validate defaults, so an OMITTED field never reaches the validator.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)
    scorecard = make_scorecard(db_session, company_id=COMPANY_B, vendor_id=vendor_b.id)
    before = snapshot(db_session, SupplierScorecard, scorecard.id)

    for field in ("quality_weight", "delivery_score", "ncr_count"):
        refused = client.put(
            f"{SCORECARDS}/{scorecard.id}", headers=headers_for(user_b), json={field: None, "notes": "attempt"}
        )
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, (field, refused.status_code, refused.text)

    assert snapshot(db_session, SupplierScorecard, scorecard.id) == before, "a refusal must leave the row untouched"

    # Omitting the field is still a no-op, and a nullable TEXT field may still be nulled.
    accepted = client.put(
        f"{SCORECARDS}/{scorecard.id}", headers=headers_for(user_b), json={"notes": None, "quality_score": 55.0}
    )
    assert accepted.status_code == status.HTTP_200_OK, accepted.text
    assert accepted.json()["quality_weight"] == 0.40, "an omitted weight must keep its stored value"


# ===========================================================================
# 9. Audit rows — invariant 2, no write in this file recorded anything
# ===========================================================================


def _audit_rows(db: Session, resource_type: str) -> list:
    from app.models.audit_log import AuditLog

    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == resource_type).all()


def test_every_supplier_write_now_leaves_an_audit_row(client: TestClient, db_session: Session):
    """Invariant 2. Supplier approval status and scorecard ratings are AS9100D-auditable
    records -- who downgraded a supplier to ``Disqualified``, or flipped an ASL entry to
    ``removed``, was unrecoverable.

    Rows are asserted as COMMITTED, not merely flushed: ``AuditService.log()`` only
    flushes, so a call placed after ``db.commit()`` lands in a transaction that get_db
    teardown rolls back, and a plain query would still see it because the client fixture
    shares one open transaction with the endpoint."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    headers = headers_for(user_b)
    vendor_b = make_vendor(db_session, company_id=COMPANY_B)

    created = client.post(
        f"{SCORECARDS}/", headers=headers, json={"vendor_id": vendor_b.id, **PERIOD, "quality_score": 90.0}
    )
    assert created.status_code == status.HTTP_200_OK, created.text
    assert (
        client.put(f"{SCORECARDS}/{created.json()['id']}", headers=headers, json={"quality_score": 40.0}).status_code
        == 200
    )
    calculated = client.post(f"{SCORECARDS}/calculate/{vendor_b.id}", headers=headers, json=PERIOD)
    assert calculated.status_code == status.HTTP_200_OK, calculated.text

    audit_created = client.post(
        f"{AUDITS}/",
        headers=headers,
        json={"vendor_id": vendor_b.id, "audit_type": "Annual", "audit_date": "2026-02-01"},
    )
    assert audit_created.status_code == status.HTTP_200_OK, audit_created.text
    assert (
        client.put(f"{AUDITS}/{audit_created.json()['id']}", headers=headers, json={"result": "failed"}).status_code
        == 200
    )

    asl_created = client.post(f"{ASL}/", headers=headers, json={"vendor_id": vendor_b.id})
    assert asl_created.status_code == status.HTTP_200_OK, asl_created.text
    assert (
        client.put(
            f"{ASL}/{asl_created.json()['id']}", headers=headers, json={"approval_status": "removed"}
        ).status_code
        == 200
    )

    scorecard_rows = _audit_rows(db_session, "supplier_scorecard")
    assert sorted(r.action for r in scorecard_rows) == ["CREATE", "CREATE", "UPDATE"]
    assert all(r.user_id == user_b.id for r in scorecard_rows), "the audit row must name who"
    assert all(r.company_id == COMPANY_B for r in scorecard_rows)

    assert sorted(r.action for r in _audit_rows(db_session, "supplier_audit")) == ["CREATE", "UPDATE"]
    assert sorted(r.action for r in _audit_rows(db_session, "approved_supplier")) == ["CREATE", "UPDATE"]


def test_an_audit_row_for_a_legacy_entry_does_not_name_the_foreign_vendor(client: TestClient, db_session: Session):
    """The audit identifier is a vendor code, so auditing an update to a legacy
    cross-tenant row must not become the thing that discloses the foreign supplier."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    vendor_a = foreign_vendor(db_session)
    legacy = make_scorecard(db_session, company_id=COMPANY_B, vendor_id=vendor_a.id)

    updated = client.put(f"{SCORECARDS}/{legacy.id}", headers=headers_for(user_b), json={"quality_score": 12.0})

    assert updated.status_code == status.HTTP_200_OK, updated.text
    rows = _audit_rows(db_session, "supplier_scorecard")
    assert rows, "the update must still be recorded"
    assert all(FOREIGN_VENDOR_CODE not in (r.resource_identifier or "") for r in rows)
    assert all(FOREIGN_VENDOR_NAME not in (r.resource_identifier or "") for r in rows)
