"""Vendor soft-delete sweep: every ``Vendor`` query site is accounted for, either way.

Companion to ``test_soft_delete_read_sweep.py`` (which is organised per record type and
covers NCR / receipt / PO / customer). Vendors get their own file because the sweep has a
shape the others don't: **the answer is not "filter everywhere"**. A blanket filter would
break restore, break the re-delete guard, break three vendor-code duplicate probes, and
blank the supplier out of purchase orders, receipts, labels and lot traceability. So every
site lands in exactly one of two states, and BOTH states are pinned here:

1. It filters ``is_deleted`` -- §1-§8 below. Each has a negative (the deleted vendor is
   not resolvable / not offered / not counted) **and a positive control** (a live vendor
   still is). Without the positive, a query accidentally broken to return nothing passes
   as "correctly excluded", which is the classic failure mode of a sweep like this one.
2. It deliberately does NOT -- §9 (restore, the re-delete guard, the code probes) and §10
   (record fidelity: a PO / receipt / label / lot trace still names the supplier it was
   actually placed with). These are the tests that fail when someone "completes the
   sweep", and they are the most valuable ones in the file.

The line between the two is **record vs. selection/permission**, not read vs. write. Two
WRITES are deliberately still open to a removed vendor and pinned as such: a vendor-attached
document (§6 -- a cert or MTR for material already received routinely arrives after the
relationship ends, and ``Document.vendor_id`` is its only supplier linkage), and the
supplier scorecard / audit verbs (§5 -- removing a supplier is usually the OUTCOME of the
finding those record). What IS gated there is the Approved Supplier List, on BOTH its create
and its update: the ASL states who the shop is permitted to buy from *now*.

§11 proves the added predicates neither dropped nor widened company scoping (invariant 1).

Two things about the fixtures are load-bearing
----------------------------------------------
* ``_soft_delete_vendor`` mimics the real ``DELETE /purchasing/vendors/{id}``: it sets
  ``is_deleted`` **and** ``is_active=False``. That second flag is the incidental mask that
  made six of these sites *latent* rather than leaking.

* ``_reanimate`` builds the divergent state ``is_deleted=True`` + ``is_active=True`` at the
  DB level, which is what the ``is_active`` mask cannot survive. It is written directly
  rather than through ``PUT /purchasing/vendors/{id}`` **because that door is now shut**
  (§1.1 pins the shut door); before the fix the PUT reached it, and legacy rows in a live
  database can still be in it. Every ``is_active``-masked site is tested through this
  state, since testing them on a plain delete would pass with or without the fix.
"""

from io import BytesIO

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints import documents as documents_module
from app.models.ai_learning import AIRecommendation
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.supplier_part import SupplierPartMapping
from app.models.user import UserRole
from app.services.ai_action_applier import AIActionApplier, AIActionApplyError
from app.services.matching_service import match_vendor
from app.services.mrp_auto_service import MRPAutoService
from app.services.print_service import PrintService
from tests.api.test_receiving_compliance import _ensure_company, _next, headers_for, make_po_line, make_user
from tests.api.test_vendor_delete_restore import make_vendor

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

VENDORS = "/api/v1/purchasing/vendors"
PO_BASE = "/api/v1/purchasing/purchase-orders"
SCORECARDS = "/api/v1/supplier-scorecards/supplier-scorecards"
AUDITS = "/api/v1/supplier-scorecards/supplier-audits"
ASL = "/api/v1/supplier-scorecards/approved-suppliers"

PERIOD = {"period_start": "2026-01-01", "period_end": "2026-03-31"}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _soft_delete_vendor(db: Session, vendor: Vendor, user_id: int) -> None:
    """Exactly what ``DELETE /purchasing/vendors/{id}`` does: soft delete + deactivate.

    ``is_active=False`` is deliberate and must stay -- it is the incidental mask that made
    the search / typeahead / matching / MRP sites latent rather than live leaks, and the
    planned ``is_active_before_delete`` change depends on this behaviour being unchanged.
    """
    vendor.soft_delete(user_id)
    vendor.is_active = False
    db.commit()
    db.refresh(vendor)
    assert vendor.is_deleted is True and vendor.is_active is False


def _reanimate(db: Session, vendor: Vendor) -> None:
    """Put a soft-deleted vendor back to ``is_active=True`` WITHOUT clearing ``is_deleted``.

    The state ``is_deleted=True`` + ``is_active=True`` is what every ``is_active``-masked
    read could not survive. Written at the DB level on purpose: the API route into it
    (``PUT /purchasing/vendors/{id}``, whose blind setattr loop accepts ``is_active``) is
    closed by ``_live_vendor_or_404`` and §1.1 pins that it stays closed. Legacy rows in a
    real database can still hold this state, which is why the reads carry their own
    predicate rather than trusting the flag.
    """
    vendor.is_active = True
    db.commit()
    db.refresh(vendor)
    assert vendor.is_deleted is True and vendor.is_active is True


def _make_part(db: Session, *, company_id: int = 1, **overrides) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    fields = dict(
        part_number=f"VSD-P-{n:05d}",
        name=f"Vendor sweep part {n}",
        description="vendor soft-delete sweep fixture",
        part_type="purchased",
        unit_of_measure="each",
        standard_cost=7.5,
        is_active=True,
        company_id=company_id,
    )
    fields.update(overrides)
    part = Part(**fields)
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _closed_po_for(db: Session, *, company_id: int = 1):
    """A PO line whose parent PO is CLOSED, so its vendor may legally be deleted.

    ``delete_vendor`` refuses while any non-closed PO references the vendor, so this is
    the ONLY shape in which a real deleted vendor can sit behind purchase-order history --
    which is exactly the shape §10's record-fidelity guards need.
    """
    line = make_po_line(db, company_id=company_id, quantity_ordered=10)
    line.purchase_order.status = POStatus.CLOSED
    db.commit()
    db.refresh(line)
    return line


def _csv(text: str):
    return {"file": ("import.csv", BytesIO(text.encode("utf-8")), "text/csv")}


# ===========================================================================
# §1. By-id resolves -- purchasing.py::_live_vendor_or_404
# ===========================================================================


def test_update_vendor_on_soft_deleted_vendor_is_404_and_cannot_reanimate(client: TestClient, db_session: Session):
    """THE headline fix. ``PUT /vendors/{id}`` used to resolve on ``company_id`` alone and
    then run a blind setattr loop over a body that includes ``is_active`` -- so one PUT
    produced ``is_deleted=True`` + ``is_active=True``, unmasking every read that filtered
    ``is_active`` as a proxy for "removed", and doing it through an ordinary UPDATE audit
    row rather than the ``action="restore"`` row ``/restore`` writes.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)
    vendor_id = vendor.id

    assert client.delete(f"{VENDORS}/{vendor_id}", headers=headers).status_code == status.HTTP_200_OK

    resp = client.put(f"{VENDORS}/{vendor_id}", headers=headers, json={"version": 0, "is_active": True})
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Vendor not found"

    # The refusal happens BEFORE the first setattr: nothing on the row moved.
    db_session.expire_all()
    row = db_session.query(Vendor).filter(Vendor.id == vendor_id).one()
    assert row.is_deleted is True
    assert row.is_active is False, "the reanimated is_deleted+is_active state must be unreachable via PUT"


def test_update_vendor_on_live_vendor_still_succeeds(client: TestClient, db_session: Session):
    """Positive control for the test above: the helper did not break ordinary editing."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1)

    resp = client.put(
        f"{VENDORS}/{vendor.id}",
        headers=headers_for(admin),
        json={"version": 0, "name": "Renamed Live Vendor"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["name"] == "Renamed Live Vendor"


def test_get_vendor_detail_404s_deleted_and_serves_live(client: TestClient, db_session: Session):
    """``GET /vendors/{id}`` adopted the same helper (it previously carried an identical
    inline filter). Behaviour is unchanged by design -- this is a LOCK-IN, not a regression
    proof: it passes against the pre-fix code too, and exists so the refactor's "identical
    filter" claim is a test rather than a comment.

    The asymmetry it pins is what made the PUT defect concrete rather than theoretical: a
    deleted vendor 404'd on READ while PUT on the same id succeeded.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    live = make_vendor(db_session, company_id=1)
    doomed = make_vendor(db_session, company_id=1)
    _soft_delete_vendor(db_session, doomed, admin.id)

    assert client.get(f"{VENDORS}/{doomed.id}", headers=headers).status_code == status.HTTP_404_NOT_FOUND
    live_resp = client.get(f"{VENDORS}/{live.id}", headers=headers)
    assert live_resp.status_code == status.HTTP_200_OK, live_resp.text
    assert live_resp.json()["id"] == live.id


# ===========================================================================
# §2. PO creation's second door -- po_upload.py::create_po_from_upload
# ===========================================================================


def _upload_payload(po_number: str, vendor_id: int, part_id: int) -> dict:
    return {
        "po_number": po_number,
        "vendor_id": vendor_id,
        "create_vendor": False,
        "line_items": [
            {
                "part_id": part_id,
                "part_number": "unused",
                "description": "vendor sweep line",
                "quantity_ordered": 3,
                "unit_price": 2.0,
            }
        ],
        "create_parts": [],
        "pdf_path": "",
    }


def test_create_po_from_upload_refuses_soft_deleted_vendor(client: TestClient, db_session: Session):
    """The AI PDF-review flow's create button was the unguarded twin of
    ``POST /purchasing/purchase-orders``: ``vendor_id`` is a client-supplied body field, so
    a stale review tab or an id read off a supplier-quality page raised a LIVE, receivable
    purchase order against a removed supplier."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1)
    part = _make_part(db_session)
    _soft_delete_vendor(db_session, vendor, admin.id)
    po_number = f"PO-VSD-{_next():05d}"

    resp = client.post(
        "/api/v1/po-upload/create-from-upload",
        headers=headers_for(admin),
        json=_upload_payload(po_number, vendor.id, part.id),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Vendor not found"

    db_session.rollback()
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).count() == 0


def test_create_po_from_upload_succeeds_for_live_vendor(client: TestClient, db_session: Session):
    """Positive control."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1)
    part = _make_part(db_session)
    po_number = f"PO-VSD-{_next():05d}"

    resp = client.post(
        "/api/v1/po-upload/create-from-upload",
        headers=headers_for(admin),
        json=_upload_payload(po_number, vendor.id, part.id),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).count() == 1


def test_create_po_from_upload_still_accepts_a_deactivated_but_live_vendor(client: TestClient, db_session: Session):
    """DECISION ON RECORD (fix agent's disagreement 1): this door filters ``is_deleted``
    only, deliberately stopping short of ``POST /purchasing/purchase-orders``'s additional
    ``is_active == True``.

    Removed and switched-off are different things, and adding the second predicate would
    400 a deactivated-but-live vendor at the LAST step of a long AI review flow -- a new
    user-visible refusal unrelated to soft delete. If the owner later decides the two doors
    should agree on ``is_active`` too, THIS is the test that will fail and say so.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1, is_active=False)
    part = _make_part(db_session)
    po_number = f"PO-VSD-{_next():05d}"

    resp = client.post(
        "/api/v1/po-upload/create-from-upload",
        headers=headers_for(admin),
        json=_upload_payload(po_number, vendor.id, part.id),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # The guarded door still refuses the same vendor -- the asymmetry is the point.
    guarded = client.post(
        PO_BASE,
        headers=headers_for(admin),
        json={"vendor_id": vendor.id, "lines": [{"part_id": part.id, "quantity_ordered": 1, "unit_price": 1.0}]},
    )
    assert guarded.status_code == status.HTTP_404_NOT_FOUND, guarded.text


# ===========================================================================
# §3. Pickers and matchers -- typeahead, PO-extraction matching, global search
#
# All three were masked by is_active, so each negative is driven through the
# reanimated state; a plain delete would pass with or without the fix.
# ===========================================================================


def test_po_review_typeahead_excludes_soft_deleted_vendor(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    live = make_vendor(db_session, company_id=1)
    live.name = "Typeahead Sweep Live"
    doomed = make_vendor(db_session, company_id=1)
    doomed.name = "Typeahead Sweep Doomed"
    db_session.commit()
    _soft_delete_vendor(db_session, doomed, admin.id)
    _reanimate(db_session, doomed)

    resp = client.get(
        "/api/v1/po-upload/search-vendors",
        headers=headers_for(admin),
        params={"q": "Typeahead Sweep"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    ids = {row["id"] for row in resp.json()}
    assert doomed.id not in ids, "a removed supplier must never be offerable in the PO-review picker"
    assert live.id in ids, "control: a live vendor is still offered"


def test_vendor_matching_exact_and_fuzzy_legs_skip_soft_deleted_vendor(db_session: Session):
    """``match_vendor`` is the service entry point behind PO-PDF extraction. The exact leg
    returns confidence 100 and pre-fills the review screen; the fuzzy leg returns up to 5
    suggestions carrying id, name and code, so it is a disclosure path as well."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    doomed = make_vendor(db_session, company_id=1)
    doomed.name = "Matching Sweep Alloys"
    db_session.commit()
    _soft_delete_vendor(db_session, doomed, admin.id)
    _reanimate(db_session, doomed)

    exact = match_vendor("Matching Sweep Alloys", db_session, company_id=1)
    assert exact.matched is False
    assert doomed.id not in {s["id"] for s in exact.suggestions}

    fuzzy = match_vendor("Matching Sweep Alloy Co", db_session, company_id=1)
    assert fuzzy.match_id != doomed.id
    assert doomed.id not in {s["id"] for s in fuzzy.suggestions}

    # Positive control: an identically-named LIVE vendor matches exactly, which proves the
    # query still works rather than having been broken into returning nothing.
    live = make_vendor(db_session, company_id=1)
    live.name = "Matching Sweep Alloys"
    db_session.commit()
    live_match = match_vendor("Matching Sweep Alloys", db_session, company_id=1)
    assert live_match.matched is True
    assert live_match.match_id == live.id
    assert live_match.confidence == 100.0


def test_global_search_excludes_soft_deleted_vendor(client: TestClient, db_session: Session):
    """Global search is open to EVERY authenticated user (the vendor screens are
    ADMIN/MANAGER) and each hit carries the vendor id in its url -- the widest-audience
    vendor read in the app."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    live = make_vendor(db_session, company_id=1)
    live.name = "Searchable Sweep Live"
    doomed = make_vendor(db_session, company_id=1)
    doomed.name = "Searchable Sweep Doomed"
    db_session.commit()
    _soft_delete_vendor(db_session, doomed, admin.id)
    _reanimate(db_session, doomed)

    results = client.get("/api/v1/search/", headers=headers_for(admin), params={"q": "Searchable Sweep"}).json()[
        "results"
    ]
    vendor_ids = {r["id"] for r in results if r["type"] == "vendor"}
    assert doomed.id not in vendor_ids
    assert live.id in vendor_ids, "control: a live vendor is still findable"


# ===========================================================================
# §4. Automatically generated purchase orders -- MRP cron and AI apply
# ===========================================================================


def test_mrp_preferred_vendor_priority1_supplier_mapping_skips_deleted_vendor(db_session: Session):
    """Priority 1 picks the supplier for AUTO_DRAFT (the 6 AM cron) and AUTO_SUBMIT POs --
    the highest-consequence vendor selection in the app, since AUTO_SUBMIT sends with no
    human in the loop."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part = _make_part(db_session)
    doomed = make_vendor(db_session, company_id=1)
    db_session.add(
        SupplierPartMapping(
            company_id=1,
            part_id=part.id,
            vendor_id=doomed.id,
            supplier_part_number=f"SPN-{_next():05d}",
            is_active=True,
        )
    )
    db_session.commit()
    _soft_delete_vendor(db_session, doomed, admin.id)
    _reanimate(db_session, doomed)

    service = MRPAutoService(db_session, company_id=1)
    assert service._get_preferred_vendor(part.id) is None

    # Positive control: the same mapping shape resolves when the vendor is live.
    live_part = _make_part(db_session)
    live = make_vendor(db_session, company_id=1)
    db_session.add(
        SupplierPartMapping(
            company_id=1,
            part_id=live_part.id,
            vendor_id=live.id,
            supplier_part_number=f"SPN-{_next():05d}",
            is_active=True,
        )
    )
    db_session.commit()
    assert service._get_preferred_vendor(live_part.id).id == live.id


def test_mrp_preferred_vendor_priority2_po_history_skips_deleted_vendor(db_session: Session):
    """Priority 2 -- the most-used vendor from PO line history. Missed by the original
    sweep (its ``db.query(Vendor, func.count(...))`` two-entity form slipped the detector),
    and strictly the MOST likely leg to fire on a just-removed supplier: the vendor a shop
    bought from for years is the one with the most PO lines."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = _closed_po_for(db_session)
    doomed = line.purchase_order.vendor
    part_id = line.part_id
    _soft_delete_vendor(db_session, doomed, admin.id)
    _reanimate(db_session, doomed)

    service = MRPAutoService(db_session, company_id=1)
    assert service._get_preferred_vendor(part_id) is None

    # Positive control: identical history against a live vendor DOES resolve.
    live_line = _closed_po_for(db_session)
    assert service._get_preferred_vendor(live_line.part_id).id == live_line.purchase_order.vendor_id


def test_mrp_preferred_vendor_priority3_fallback_skips_deleted_vendor(db_session: Session):
    """Priority 3 fires for a part with no mapping and no purchase history -- a NEW part,
    i.e. the draft PO least likely to look wrong to a buyer skimming the queue."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part = _make_part(db_session)
    doomed = make_vendor(db_session, company_id=1)
    _soft_delete_vendor(db_session, doomed, admin.id)
    _reanimate(db_session, doomed)

    service = MRPAutoService(db_session, company_id=1)
    assert service._get_preferred_vendor(part.id) is None

    live = make_vendor(db_session, company_id=1)
    assert service._get_preferred_vendor(part.id).id == live.id


def _po_recommendation(db: Session, part_id: int, *, vendor_id: int = None) -> AIRecommendation:
    action = {"type": "create_draft_po", "part_id": part_id, "autonomy": "apply_on_accept"}
    if vendor_id is not None:
        action["vendor_id"] = vendor_id
    rec = AIRecommendation(
        company_id=1,
        source_module="inventory",
        recommendation_type="reorder",
        title="Reorder below point",
        summary="Below reorder point",
        target_entity_type="part",
        target_entity_id=part_id,
        suggested_action=action,
        confidence_score=0.9,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def test_ai_draft_po_fallback_vendor_skips_deleted_vendor(db_session: Session):
    """The fallback branch had NO mask of any kind: ``order_by(Vendor.id.asc()).first()``
    happily returned the tenant's oldest vendor, deleted or not -- for this shop, VND-001
    from the 100-supplier ASL load."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    doomed = make_vendor(db_session, company_id=1)  # lowest id in the tenant
    _soft_delete_vendor(db_session, doomed, admin.id)
    part = _make_part(db_session, part_type="raw_material", reorder_quantity=25)
    rec = _po_recommendation(db_session, part.id)

    applier = AIActionApplier(db_session, company_id=1, user=admin)
    with pytest.raises(AIActionApplyError, match="No vendor available"):
        applier.apply(rec)
    db_session.rollback()

    # Positive control: with a live vendor present the fallback resolves -- to the LIVE one,
    # even though the deleted vendor still sorts first by id.
    live = make_vendor(db_session, company_id=1)
    rec2 = _po_recommendation(db_session, part.id)
    result = AIActionApplier(db_session, company_id=1, user=admin).apply(rec2)
    db_session.commit()
    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.id == result["purchase_order_id"]).one()
    assert po.vendor_id == live.id


def test_ai_draft_po_explicit_vendor_skips_deleted_vendor(db_session: Session):
    """The explicit branch reads ``action["vendor_id"] or part.primary_supplier_id`` -- and
    that FK is stale by construction, since no vendor-delete path clears it. So this branch
    reaches a removed supplier without anyone typing its id."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    doomed = make_vendor(db_session, company_id=1)
    part = _make_part(db_session, part_type="raw_material", primary_supplier_id=doomed.id, reorder_quantity=10)
    _soft_delete_vendor(db_session, doomed, admin.id)
    rec = _po_recommendation(db_session, part.id)

    # The message matters as much as the refusal: ai_learning.py surfaces it verbatim as
    # ``apply_error`` on the ActionInbox card, the recommendation fails identically on every
    # retry, and "Vendor not found for this company" sends a buyer hunting a tenancy bug
    # instead of repointing the part's primary supplier.
    with pytest.raises(AIActionApplyError, match="has been removed"):
        AIActionApplier(db_session, company_id=1, user=admin).apply(rec)
    db_session.rollback()

    # An id that resolves to nothing keeps the original message -- the two cases must stay
    # distinguishable, which is why the lookup is raw with an explicit is_deleted test.
    _ensure_company(db_session, 2)
    foreign = make_vendor(db_session, company_id=2)
    gone_part = _make_part(db_session, part_type="raw_material", primary_supplier_id=foreign.id, reorder_quantity=10)
    gone_rec = _po_recommendation(db_session, gone_part.id)
    with pytest.raises(AIActionApplyError, match="Vendor not found for this company"):
        AIActionApplier(db_session, company_id=1, user=admin).apply(gone_rec)
    db_session.rollback()

    # Positive control: the same stale-FK shape against a live vendor still drafts a PO.
    live = make_vendor(db_session, company_id=1)
    live_part = _make_part(db_session, part_type="raw_material", primary_supplier_id=live.id, reorder_quantity=10)
    rec2 = _po_recommendation(db_session, live_part.id)
    result = AIActionApplier(db_session, company_id=1, user=admin).apply(rec2)
    db_session.commit()
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.id == result["purchase_order_id"]).one().vendor_id == (
        live.id
    )


# ===========================================================================
# §5. Supplier quality -- records stay writable, only the ASL approval is gated
# ===========================================================================


def _record_verbs(vendor_id: int):
    """The four RECORD verbs on ``_vendor_in_company``, as (method, url, body).

    Every one of these documents a supplier relationship that existed, so all four must
    keep working after the vendor is removed -- see ``_vendor_in_company``'s docstring.
    """
    return [
        ("post", f"{SCORECARDS}/", {"vendor_id": vendor_id, **PERIOD, "quality_score": 90.0}),
        ("post", f"{SCORECARDS}/calculate/{vendor_id}", dict(PERIOD)),
        ("post", f"{AUDITS}/", {"vendor_id": vendor_id, "audit_type": "Annual", "audit_date": "2026-01-15"}),
        ("get", f"{SCORECARDS}/vendor/{vendor_id}/history", None),
    ]


def test_supplier_quality_records_still_write_and_read_for_a_soft_deleted_vendor(
    client: TestClient, db_session: Session
):
    """DECISION ON RECORD, and the reason this router does NOT get a blanket gate.

    Removing a supplier is very often the OUTCOME of the audit finding or the closing
    scorecard, so gating these on vendor liveness makes the AS9100D 8.4 record that
    documents the removal the one record that can never be filed -- and with no vendor
    restore screen yet, no way out of it. ``calculate`` is the clearest case: every input it
    reads is bounded by an explicit period, so it writes a truthful account of a period
    during which the vendor WAS a supplier.

    The by-vendor history read is here for the mirror-image reason: 404-ing it while
    ``GET /supplier-scorecards/?vendor_id=`` and the ranking still return the very same rows
    would be a contradiction, not a protection (§10.5 pins that they do).

    Liveness is enforced on the ASL instead -- the one write here that is a PERMISSION.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)
    _soft_delete_vendor(db_session, vendor, admin.id)

    for method, url, body in _record_verbs(vendor.id):
        resp = client.request(method.upper(), url, headers=headers, json=body)
        assert resp.status_code == status.HTTP_200_OK, f"{method.upper()} {url}: {resp.text}"


def test_supplier_quality_records_still_work_for_a_live_vendor(client: TestClient, db_session: Session):
    """Positive control: the record verbs are not merely broken-open for everyone."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)

    for method, url, body in _record_verbs(vendor.id):
        resp = client.request(method.upper(), url, headers=headers, json=body)
        assert resp.status_code == status.HTTP_200_OK, f"{method.upper()} {url}: {resp.text}"


def test_supplier_quality_verbs_still_refuse_a_foreign_vendor(client: TestClient, db_session: Session):
    """The tenancy half of ``_vendor_in_company`` survived the soft-delete split (#191)."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    _ensure_company(db_session, 2)
    foreign = make_vendor(db_session, company_id=2)

    for method, url, body in _record_verbs(foreign.id) + [("post", f"{ASL}/", {"vendor_id": foreign.id})]:
        resp = client.request(method.upper(), url, headers=headers, json=body)
        assert resp.status_code == status.HTTP_404_NOT_FOUND, f"{method.upper()} {url}: {resp.text}"
        assert resp.json()["detail"] == "Vendor not found"


def test_asl_create_refuses_a_soft_deleted_vendor(client: TestClient, db_session: Session):
    """The Approved Supplier List is the AS9100D 8.4 statement of who the shop is PERMITTED
    to buy from right now -- not a history -- so a removed supplier must not be enterable on
    it by anyone still holding its id from a page that renders EXISTING rows (§10.5)."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    live = make_vendor(db_session, company_id=1)
    doomed = make_vendor(db_session, company_id=1)
    _soft_delete_vendor(db_session, doomed, admin.id)

    refused = client.post(f"{ASL}/", headers=headers, json={"vendor_id": doomed.id})
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    assert "has been removed" in refused.json()["detail"]

    accepted = client.post(f"{ASL}/", headers=headers, json={"vendor_id": live.id})
    assert accepted.status_code == status.HTTP_200_OK, accepted.text


def test_asl_update_cannot_re_approve_a_removed_supplier_but_notes_still_edit(client: TestClient, db_session: Session):
    """The second ASL door. ``PUT /approved-suppliers/{asl_id}`` resolves by ASL id and
    never re-reads ``vendor_id``, so closing only the create left a removed supplier
    re-approvable through the blind setattr loop -- and the ASL list, which deliberately
    keeps rendering deleted vendors (§10.5), hands out both ids.

    Refused per FIELD, not per row: an entry whose vendor is gone stays correctable, since
    there is no restore screen to unfreeze it with. The refusal must also leave the row
    untouched -- ``last_review_date`` is stamped on every accepted edit.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)

    created = client.post(f"{ASL}/", headers=headers, json={"vendor_id": vendor.id, "approval_status": "pending"})
    assert created.status_code == status.HTTP_200_OK, created.text
    asl_id = created.json()["id"]

    _soft_delete_vendor(db_session, vendor, admin.id)

    refused = client.put(f"{ASL}/{asl_id}", headers=headers, json={"approval_status": "approved"})
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    assert "cannot be re-approved" in refused.json()["detail"]
    assert client.get(f"{ASL}/{asl_id}", headers=headers).json()["approval_status"] == "pending"

    corrected = client.put(f"{ASL}/{asl_id}", headers=headers, json={"notes": "supplier removed 2026-08"})
    assert corrected.status_code == status.HTTP_200_OK, corrected.text
    assert corrected.json()["notes"] == "supplier removed 2026-08"


def test_asl_update_still_approves_a_live_vendor(client: TestClient, db_session: Session):
    """Positive control -- the per-field refusal must not gate ordinary approvals."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)

    created = client.post(f"{ASL}/", headers=headers, json={"vendor_id": vendor.id, "approval_status": "pending"})
    assert created.status_code == status.HTTP_200_OK, created.text
    approved = client.put(f"{ASL}/{created.json()['id']}", headers=headers, json={"approval_status": "approved"})
    assert approved.status_code == status.HTTP_200_OK, approved.text
    assert approved.json()["approval_status"] == "approved"


# ===========================================================================
# §6. Attachment and routing writes -- documents, scanner mappings
# ===========================================================================


def test_document_upload_still_accepts_a_soft_deleted_vendor(
    client: TestClient, db_session: Session, tmp_path, monkeypatch
):
    """DECISION ON RECORD -- do not "complete the sweep" here.

    A vendor-attached document is a quality RECORD about material that already arrived
    (certificate of conformance, material test report, corrected packing slip), and those
    routinely turn up AFTER the supplier relationship ends. ``Document.vendor_id`` is the
    only field linking such a record to the supplier that certified it, so refusing the
    attachment does not prevent the upload -- ``vendor_id`` is optional -- it only strips the
    supplier off an AS9100D 8.4 record. Same posture as the PO vendor block, the receipt,
    the lot trace and the thermal label (§10).

    It would also be asymmetric: ``GET /documents?vendor_id=`` carries no vendor predicate
    at all, so gating the write alone gives a path you can read but not write to -- exactly
    the shape this sweep exists to eliminate. The tenancy half is still enforced.
    """
    monkeypatch.setattr(documents_module, "UPLOAD_DIR", str(tmp_path))
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    live = make_vendor(db_session, company_id=1)
    doomed = make_vendor(db_session, company_id=1)
    _ensure_company(db_session, 2)
    foreign = make_vendor(db_session, company_id=2)
    _soft_delete_vendor(db_session, doomed, admin.id)

    def _upload(vendor_id: int):
        return client.post(
            "/api/v1/documents/upload",
            headers=headers,
            data={"title": "Supplier cert", "document_type": "specification", "vendor_id": str(vendor_id)},
            files={"file": ("cert.pdf", b"%PDF-1.4 vendor sweep\n", "application/pdf")},
        )

    filed = _upload(doomed.id)
    assert filed.status_code == status.HTTP_200_OK, filed.text
    assert filed.json()["vendor_id"] == doomed.id, "the supplier linkage is the point of the record"

    accepted = _upload(live.id)
    assert accepted.status_code == status.HTTP_200_OK, accepted.text
    assert accepted.json()["vendor_id"] == live.id

    refused = _upload(foreign.id)
    assert refused.status_code == status.HTTP_404_NOT_FOUND, refused.text
    assert refused.json()["detail"] == "Vendor not found"


def test_create_scanner_mapping_refuses_a_soft_deleted_vendor(client: TestClient, db_session: Session):
    """A supplier-part mapping is a live routing rule, not a record: bound to a removed
    vendor it keeps surfacing that supplier on every dock scan that matches it."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    part = _make_part(db_session)
    live = make_vendor(db_session, company_id=1)
    doomed = make_vendor(db_session, company_id=1)
    _soft_delete_vendor(db_session, doomed, admin.id)

    def _create(vendor_id: int):
        return client.post(
            "/api/v1/scanner/mappings",
            headers=headers,
            json={
                "supplier_part_number": f"SPN-{_next():05d}",
                "part_id": part.id,
                "vendor_id": vendor_id,
            },
        )

    refused = _create(doomed.id)
    assert refused.status_code == status.HTTP_404_NOT_FOUND, refused.text
    assert refused.json()["detail"] == "Vendor not found"

    accepted = _create(live.id)
    assert accepted.status_code == status.HTTP_200_OK, accepted.text
    assert accepted.json()["vendor_name"] == live.name


def test_scanner_reads_withhold_a_deleted_vendors_name_but_keep_resolving_the_part(
    client: TestClient, db_session: Session
):
    """DECISION ON RECORD (fix agent's disagreement 4). Closing the create verb stops NEW
    bindings; existing mappings survive a vendor delete because nothing deactivates them.
    The fix withholds the vendor NAME rather than dropping the mapping -- dropping it would
    make a barcode stop resolving at the receiving dock over a vendor bookkeeping change.
    ``vendor_name=None`` is the shape a vendor-less mapping has always rendered.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    part = _make_part(db_session)
    live = make_vendor(db_session, company_id=1)
    doomed = make_vendor(db_session, company_id=1)
    doomed_code = f"SPN-DEL-{_next():05d}"
    live_code = f"SPN-LIVE-{_next():05d}"
    for vendor, spn in ((doomed, doomed_code), (live, live_code)):
        created = client.post(
            "/api/v1/scanner/mappings",
            headers=headers,
            json={"supplier_part_number": spn, "part_id": part.id, "vendor_id": vendor.id},
        )
        assert created.status_code == status.HTTP_200_OK, created.text
    _soft_delete_vendor(db_session, doomed, admin.id)

    scanned = client.post("/api/v1/scanner/lookup", headers=headers, params={"code": doomed_code})
    assert scanned.status_code == status.HTTP_200_OK, scanned.text
    body = scanned.json()
    assert body["found"] is True and body["part_id"] == part.id, "the part half must still resolve"
    assert body["vendor_name"] is None
    assert doomed.name not in scanned.text

    live_scan = client.post("/api/v1/scanner/lookup", headers=headers, params={"code": live_code}).json()
    assert live_scan["vendor_name"] == live.name, "control: a live vendor is still named"

    listed = client.get("/api/v1/scanner/mappings", headers=headers).json()
    by_spn = {row["supplier_part_number"]: row for row in listed}
    assert by_spn[doomed_code]["vendor_name"] is None
    assert by_spn[live_code]["vendor_name"] == live.name


# ===========================================================================
# §7. Excel-migration open-PO loader -- migration_import_service
# ===========================================================================


def test_open_po_import_reports_a_soft_deleted_vendor_code_with_the_restore_remedy(
    client: TestClient, db_session: Session
):
    """A soft-deleted vendor used to satisfy this loader's "import vendors first" contract
    silently -- so the dry run reported the row as VALID and the commit attached an open,
    receivable PO to a removed supplier.

    The message is asserted, not just the refusal. A cutover operator hits a genuine dead
    end on a bare "not found": re-creating the vendor is refused because a deleted vendor
    still owns its code (§9.3), and the spreadsheet's ``vendor_code`` column is the join key
    so a different code is not an option. Until the restore screen ships, this string IS the
    remedy, so it must name the verb and carry the id."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    live = make_vendor(db_session, company_id=1)
    doomed = make_vendor(db_session, company_id=1)
    live_part = _make_part(db_session)
    doomed_part = _make_part(db_session)
    _soft_delete_vendor(db_session, doomed, admin.id)

    csv_text = (
        "po_number,vendor_code,part_number,quantity,unit_price\n"
        f"PO-VSD-LIVE-{_next()},{live.code},{live_part.part_number},5,1.00\n"
        f"PO-VSD-DEAD-{_next()},{doomed.code},{doomed_part.part_number},5,1.00\n"
    )

    dry = client.post("/api/v1/purchasing/purchase-orders/import?dry_run=true", headers=headers, files=_csv(csv_text))
    assert dry.status_code == status.HTTP_200_OK, dry.text
    body = dry.json()
    reasons = " | ".join(e["reason"] for e in body["errors"])
    assert f"vendor '{doomed.code}' was deleted" in reasons
    assert f"/purchasing/vendors/{doomed.id}/restore" in reasons
    assert body["created_count"] == 1, "control: the live vendor's row still validates"

    missing_csv = (
        "po_number,vendor_code,part_number,quantity,unit_price\n"
        f"PO-VSD-GONE-{_next()},NO-SUCH-VENDOR-CODE,{live_part.part_number},5,1.00\n"
    )
    missing = client.post(
        "/api/v1/purchasing/purchase-orders/import?dry_run=true", headers=headers, files=_csv(missing_csv)
    )
    assert missing.status_code == status.HTTP_200_OK, missing.text
    missing_reasons = " | ".join(e["reason"] for e in missing.json()["errors"])
    assert "not found (import vendors first)" in missing_reasons, "a code matching nothing keeps the old message"


# ===========================================================================
# §8. Counts -- onboarding readiness, AS9100D supplier-control evidence pack
#
# Both assert a DELTA across the delete rather than an absolute count, so the
# tests never depend on how many other rows a fixture happened to create.
# ===========================================================================


def test_setup_health_vendor_count_drops_when_a_vendor_is_removed(client: TestClient, db_session: Session):
    """A readiness gate that counts tombstones reports "you are ready" on a short vendor
    master -- the cutover checklist's whole value is that this number is trustworthy.

    Driven through the reanimated state, because a plain delete would pass with or without
    the fix: ``is_active`` was doing the work incidentally. With the tombstone reactivated
    the mask is gone and only a real ``is_deleted`` predicate keeps it out of the count.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    make_vendor(db_session, company_id=1)  # live control -- keeps the count non-zero after the delete
    doomed = make_vendor(db_session, company_id=1)

    before = client.get("/api/v1/setup/health", headers=headers).json()["counts"]["vendors"]
    assert before >= 2
    _soft_delete_vendor(db_session, doomed, admin.id)
    _reanimate(db_session, doomed)
    after = client.get("/api/v1/setup/health", headers=headers).json()["counts"]["vendors"]

    assert after == before - 1
    assert after >= 1, "control: the live vendor is still counted"


def test_supplier_evidence_pack_counts_exclude_a_removed_supplier(client: TestClient, db_session: Session):
    """``total_vendors`` had NO mask and was wrong TODAY, while the neighbouring ``approved``
    figure was masked by ``is_active`` -- so the pack handed to a registrar reported an
    approval gap that does not exist. Both counts now move together."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    standard = QMSStandard(company_id=1, name="AS9100D", version="Rev D", is_active=True)
    db_session.add(standard)
    db_session.flush()
    clause = QMSClause(
        company_id=1,
        standard_id=standard.id,
        clause_number="8.4",
        title="Control of externally provided processes, products and services",
        description="Evaluation of supplier performance and the approved supplier list.",
    )
    db_session.add(clause)
    db_session.commit()

    make_vendor(db_session, company_id=1)  # live control
    doomed = make_vendor(db_session, company_id=1)

    def _pack() -> dict:
        resp = client.get(f"/api/v1/qms-standards/clauses/{clause.id}/auto-evidence", headers=headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        suppliers = [e for e in resp.json()["discovered_evidence"] if e["title"].startswith("Supplier Management")]
        assert len(suppliers) == 1, resp.text
        return suppliers[0]

    before = _pack()
    _soft_delete_vendor(db_session, doomed, admin.id)
    # Reanimated so BOTH halves are actually exercised: ``total_vendors`` was wrong with a
    # plain delete already (no mask), but ``approved`` needs the tombstone reactivated
    # before its own is_deleted predicate is the only thing keeping it out.
    _reanimate(db_session, doomed)
    after = _pack()

    assert after["total_count"] == before["total_count"] - 1
    assert after["total_count"] >= 1, "control: the live vendor is still counted"
    # `approved` is not a response field; it is rendered into the description as
    # "N suppliers (M active)". Both halves must move, or the pack reports an approval gap
    # that does not exist -- a discrepancy a registrar will ask about.
    assert after["description"].startswith(f"{after['total_count']} suppliers ({after['total_count']} active)")


# ===========================================================================
# §9. DELIBERATELY RAW -- the sites that must keep seeing the tombstone.
#
# These are the tests that fail if someone "completes the sweep".
# ===========================================================================


def test_restore_still_finds_and_restores_a_soft_deleted_vendor(client: TestClient, db_session: Session):
    """THE most valuable test in this file. ``restore_vendor`` resolves with a RAW lookup --
    seeing the tombstone is the verb's entire job. Adding ``is_deleted == False`` to it (or
    routing it through ``_live_vendor_or_404``) turns restore into a permanent 404 and makes
    every soft delete irreversible, which is exactly what invariant 3 exists to prevent."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)
    vendor_id = vendor.id
    assert client.delete(f"{VENDORS}/{vendor_id}", headers=headers).status_code == status.HTTP_200_OK
    assert client.get(f"{VENDORS}/{vendor_id}", headers=headers).status_code == status.HTTP_404_NOT_FOUND

    restored = client.post(f"{VENDORS}/{vendor_id}/restore", headers=headers)
    assert restored.status_code == status.HTTP_200_OK, restored.text

    db_session.expire_all()
    row = db_session.query(Vendor).filter(Vendor.id == vendor_id).one()
    assert row.is_deleted is False and row.is_active is True
    # And the round trip is complete: the vendor is workable again on the filtered paths.
    assert client.get(f"{VENDORS}/{vendor_id}", headers=headers).status_code == status.HTTP_200_OK
    assert (
        client.put(f"{VENDORS}/{vendor_id}", headers=headers, json={"version": 0, "city": "Tulsa"})
    ).status_code == status.HTTP_200_OK


def test_double_delete_still_returns_the_already_deleted_400(client: TestClient, db_session: Session):
    """``delete_vendor``'s lookup is raw so the re-delete guard can answer 400 "already
    deleted". Filtering it would turn that guard into a bare 404 and let a second DELETE
    reset ``deleted_at``/``deleted_by`` and write a duplicate audit row."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)
    assert client.delete(f"{VENDORS}/{vendor.id}", headers=headers).status_code == status.HTTP_200_OK

    second = client.delete(f"{VENDORS}/{vendor.id}", headers=headers)
    assert second.status_code == status.HTTP_400_BAD_REQUEST, second.text
    assert "already deleted" in second.json()["detail"]


def test_create_vendor_duplicate_code_probe_still_sees_a_deleted_vendors_code(client: TestClient, db_session: Session):
    """DECISION ON RECORD. ``uq_vendors_company_code`` is a plain UniqueConstraint on
    (company_id, code) with NO partial predicate, so a deleted vendor still OWNS its code.
    The create probe therefore stays raw, for two independent reasons:

    1. A clean 400 instead of pushing the failure into the IntegrityError backstop below it
       (a rollback today, a 500 the day anyone removes that backstop).
    2. Restore safety -- if the code were reusable while the vendor is away, someone takes
       it and ``POST /vendors/{id}/restore`` then resurrects a row that violates the
       constraint. Restore has no collision check and cannot be given one after the fact.

    The second reason is the one a reader of only the first might try to relax with a
    partial index. All three are pinned: the 400, the restore that still works afterwards,
    and -- via the canary below -- that the 400 came from the PROBE and not the backstop.

    The canary is load-bearing and not decoration. The status code alone cannot tell the
    two apart: filter the probe and the insert still fails on ``uq_vendors_company_code``,
    and the ``except IntegrityError`` handler surfaces the very same 400. Verified by
    simulation -- with only the probe filtered, a 400-and-restore-only assertion passes.
    What differs is the ``db.rollback()`` on the backstop path, and the test session IS the
    request session (``conftest``'s ``override_get_db``), so an uncommitted row flushed here
    survives the probe path and is discarded by the backstop path.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)
    taken_code = vendor.code
    _soft_delete_vendor(db_session, vendor, admin.id)

    canary = Vendor(code=f"CANARY{_next():04d}", name="Rollback canary", company_id=1)
    db_session.add(canary)
    db_session.flush()  # uncommitted: only a rollback destroys it
    canary_id = canary.id

    resp = client.post(
        f"{VENDORS}",
        headers=headers,
        json={"code": taken_code, "name": "Squatting On A Tombstone"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Vendor code already exists"
    assert db_session.query(Vendor).filter(Vendor.id == canary_id).first() is not None, (
        "the 400 must come from the pre-insert probe, which refuses before touching the "
        "session -- a surviving canary proves no IntegrityError rollback happened"
    )

    # Reason 2 made concrete: because the code was never re-issued, restore still works.
    assert client.post(f"{VENDORS}/{vendor.id}/restore", headers=headers).status_code == status.HTTP_200_OK


def test_rename_duplicate_probe_still_sees_a_deleted_vendors_code(client: TestClient, db_session: Session):
    """The same constraint argument on ``PUT``. This handler holds BOTH shapes eight lines
    apart -- the row RESOLUTION gained the filter (§1.1), the duplicate PROBE must not.
    Fixing "both" is the trap."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    tombstone = make_vendor(db_session, company_id=1)
    taken_code = tombstone.code
    _soft_delete_vendor(db_session, tombstone, admin.id)
    renamer = make_vendor(db_session, company_id=1)

    resp = client.put(f"{VENDORS}/{renamer.id}", headers=headers, json={"version": 0, "code": taken_code})
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Vendor code already exists"

    db_session.expire_all()
    assert db_session.query(Vendor).filter(Vendor.id == renamer.id).one().code != taken_code


def test_vendor_csv_import_still_treats_a_deleted_vendors_code_as_taken(client: TestClient, db_session: Session):
    """The bulk form of the create probe: ``existing_codes`` is built with no ``is_deleted``
    predicate on purpose. An import row reusing a deleted vendor's code must fail as a
    duplicate, or it writes a row that cannot coexist with a restore of that vendor."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    tombstone = make_vendor(db_session, company_id=1)
    taken_code = tombstone.code
    _soft_delete_vendor(db_session, tombstone, admin.id)
    fresh_code = f"VFRESH{_next():04d}"

    resp = client.post(
        f"{VENDORS}/import-csv",
        headers=headers,
        files=_csv(f"code,name\n{taken_code},Reused Code Row\n{fresh_code},Fresh Code Row\n"),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert [e["code"] for e in body["errors"]] == [taken_code]
    assert body["errors"][0]["reason"] == "Vendor code already exists"
    assert body["imported_count"] == 1, "control: the fresh-code row still imports"


def test_generated_vendor_code_still_counts_deleted_vendors(client: TestClient, db_session: Session):
    """``_generate_vendor_code`` counts ``code LIKE '<prefix>%'`` WITHOUT excluding
    tombstones. Skipping them would mint a code that is already taken and turn a clean
    insert into an IntegrityError. Driven through the real entry point -- a CSV import row
    with no ``code`` column value, which is what makes the generator run."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    # ZZQ001 is what _generate_vendor_code mints first for a name starting "ZZQ".
    first = client.post(f"{VENDORS}", headers=headers, json={"code": "ZZQ001", "name": "ZZQ Alloys"})
    assert first.status_code == status.HTTP_200_OK, first.text
    tombstone = db_session.query(Vendor).filter(Vendor.id == first.json()["id"]).one()
    _soft_delete_vendor(db_session, tombstone, admin.id)

    resp = client.post(f"{VENDORS}/import-csv", headers=headers, files=_csv("name\nZZQ Fasteners\n"))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["errors"] == [], resp.text
    assert resp.json()["imported_count"] == 1

    created = db_session.query(Vendor).filter(Vendor.name == "ZZQ Fasteners", Vendor.company_id == 1).one()
    assert created.code != "ZZQ001", "the generator must not re-mint a tombstone's code"
    assert created.code == "ZZQ002"


def test_po_upload_generated_vendor_code_still_skips_a_deleted_vendors_code(client: TestClient, db_session: Session):
    """The site the auditor's own table omitted: the ``while`` loop in
    ``create_po_from_upload`` that suffixes a derived vendor code until it is free. Same
    tombstone-spanning constraint, same rule -- it must keep seeing deleted rows."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    part = _make_part(db_session)
    # "Kestrel Ltd" derives to V-KESTRELLT; park that code on a tombstone.
    parked = client.post(f"{VENDORS}", headers=headers, json={"code": "V-KESTRELLT", "name": "Kestrel Ltd"})
    assert parked.status_code == status.HTTP_200_OK, parked.text
    _soft_delete_vendor(db_session, db_session.query(Vendor).filter(Vendor.id == parked.json()["id"]).one(), admin.id)

    po_number = f"PO-VSD-{_next():05d}"
    resp = client.post(
        "/api/v1/po-upload/create-from-upload",
        headers=headers,
        json={
            "po_number": po_number,
            "vendor_id": 0,
            "create_vendor": True,
            "new_vendor_name": "Kestrel Ltd",
            "line_items": [
                {
                    "part_id": part.id,
                    "part_number": "unused",
                    "description": "line",
                    "quantity_ordered": 1,
                    "unit_price": 1.0,
                }
            ],
            "create_parts": [],
            "pdf_path": "",
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    codes = {
        v.code for v in db_session.query(Vendor).filter(Vendor.company_id == 1, Vendor.name == "Kestrel Ltd").all()
    }
    assert codes == {"V-KESTRELLT", "V-KESTRELLT-1"}, "the loop must step past the tombstone, not collide with it"


# ===========================================================================
# §10. DELIBERATELY RAW -- record fidelity.
#
# A purchase order, a receipt, a printed document and a lot trace name the
# supplier the transaction ACTUALLY involved. That cannot change because the
# supplier relationship later ended. ``delete_vendor`` refuses while any
# non-closed PO references the vendor, so every row below sits behind a CLOSED
# PO: history, not an open exposure.
# ===========================================================================


def _closed_po_with_deleted_vendor(client: TestClient, db_session: Session, admin):
    line = _closed_po_for(db_session)
    vendor = line.purchase_order.vendor
    vendor_name = vendor.name
    po_number = line.purchase_order.po_number
    assert client.delete(f"{VENDORS}/{vendor.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    db_session.expire_all()
    return line, vendor_name, po_number


def test_closed_po_cannot_be_revived_to_a_live_status_against_a_deleted_vendor(client: TestClient, db_session: Session):
    """The THIRD door into "a live PO against a removed supplier", and the only one that
    REVIVES rather than creates.

    ``delete_vendor``'s "no live POs" check is point-in-time; nothing re-evaluates it. And
    ``PUT /purchase-orders/{id}`` validates no transition at all -- ``POStatus(value)`` only
    checks enum membership -- so a CLOSED PO took ``{"status": "sent"}`` and landed straight
    back in ``GET /receiving/open-pos``, receivable, against a vendor the quality system says
    was removed. ``POST .../send`` refuses (not DRAFT/APPROVED) and ``POST .../restore``
    refuses (vendor deleted); this verb bypassed both.

    Moving the PO INTO a terminal status stays allowed: closing out a removed supplier's
    paperwork must never be blocked. Non-status edits are likewise untouched.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    line, vendor_name, po_number = _closed_po_with_deleted_vendor(client, db_session, admin)
    po_id = line.purchase_order_id

    revived = client.put(f"{PO_BASE}/{po_id}", headers=headers, json={"version": 0, "status": "sent"})
    assert revived.status_code == status.HTTP_400_BAD_REQUEST, revived.text
    detail = revived.json()["detail"]
    assert vendor_name in detail and "Restore the vendor first" in detail
    db_session.expire_all()
    assert db_session.get(PurchaseOrder, po_id).status == POStatus.CLOSED, "a refusal must not mutate the row"

    # Terminal statuses stay reachable, and so do ordinary field edits.
    cancelled = client.put(f"{PO_BASE}/{po_id}", headers=headers, json={"version": 0, "status": "cancelled"})
    assert cancelled.status_code == status.HTTP_200_OK, cancelled.text
    noted = client.put(f"{PO_BASE}/{po_id}", headers=headers, json={"version": 0, "notes": "supplier removed"})
    assert noted.status_code == status.HTTP_200_OK, noted.text


def test_status_change_on_a_po_with_a_live_vendor_is_untouched(client: TestClient, db_session: Session):
    """Positive control -- the revival guard must not gate ordinary status edits."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    line = _closed_po_for(db_session)

    resp = client.put(f"{PO_BASE}/{line.purchase_order_id}", headers=headers, json={"version": 0, "status": "sent"})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == "sent"


def test_scanner_withholds_a_cross_tenant_vendor_name(client: TestClient, db_session: Session):
    """``_live_vendor_name`` tests tenancy as well as soft delete, mirroring
    ``supplier_scorecards._same_tenant_vendor``.

    Both call sites scope the MAPPING by ``company_id`` and then traverse ``mapping.vendor``,
    which carries no predicate of its own -- transitive scoping holds only while every FK was
    stamped correctly at write time, and that is exactly the assumption that failed in #191.
    A mis-stamped row must render ``vendor_name: null`` rather than another tenant's supplier,
    and must still resolve its PART so the dock is not broken by it.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    part = _make_part(db_session)
    _ensure_company(db_session, 2)
    foreign = make_vendor(db_session, company_id=2)
    code = f"SPN-XT-{_next():05d}"
    db_session.add(
        SupplierPartMapping(
            company_id=1,
            supplier_part_number=code,
            part_id=part.id,
            vendor_id=foreign.id,
            is_active=True,
        )
    )
    db_session.commit()

    scanned = client.post("/api/v1/scanner/lookup", headers=headers, params={"code": code})
    assert scanned.status_code == status.HTTP_200_OK, scanned.text
    assert scanned.json()["found"] is True, "the part half must still resolve"
    assert scanned.json()["vendor_name"] is None
    assert foreign.name not in scanned.text

    listed = client.get("/api/v1/scanner/mappings", headers=headers)
    assert listed.status_code == status.HTTP_200_OK, listed.text
    row = next(r for r in listed.json() if r["supplier_part_number"] == code)
    assert row["vendor_name"] is None
    assert foreign.name not in listed.text


def test_purchase_order_list_and_detail_still_name_a_deleted_vendor(client: TestClient, db_session: Session):
    """``joinedload(PurchaseOrder.vendor)`` carries no ``is_deleted`` predicate on purpose:
    blanking the supplier out of a closed order erases the traceability the record exists
    for. The live-vendor gate belongs on the WRITE verbs, which §1-§2 cover."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    line, vendor_name, po_number = _closed_po_with_deleted_vendor(client, db_session, admin)

    # ``?status=closed`` because the default list excludes CLOSED/CANCELLED -- and CLOSED
    # is the only state in which a PO's vendor can legally have been deleted at all.
    listed = client.get(PO_BASE, headers=headers, params={"status": "closed"})
    assert listed.status_code == status.HTTP_200_OK, listed.text
    row = next(p for p in listed.json() if p["po_number"] == po_number)
    assert row["vendor_name"] == vendor_name

    detail = client.get(f"{PO_BASE}/{line.purchase_order_id}", headers=headers)
    assert detail.status_code == status.HTTP_200_OK, detail.text
    assert detail.json()["vendor"]["name"] == vendor_name


def test_purchase_order_exports_and_print_still_name_a_deleted_vendor(client: TestClient, db_session: Session):
    """An export of historical POs, and a reprint of one, must reproduce the document as
    issued -- vendor block included."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    line, vendor_name, po_number = _closed_po_with_deleted_vendor(client, db_session, admin)

    po_export = client.get("/api/v1/exports/purchase-orders/export", headers=headers, params={"format": "csv"})
    assert po_export.status_code == status.HTTP_200_OK, po_export.text
    assert vendor_name in po_export.text

    line_export = client.get("/api/v1/exports/purchase-orders/lines/export", headers=headers, params={"format": "csv"})
    assert line_export.status_code == status.HTTP_200_OK, line_export.text
    assert vendor_name in line_export.text

    printed = client.get(f"/api/v1/print/purchase-orders/{line.purchase_order_id}/print-data", headers=headers)
    assert printed.status_code == status.HTTP_200_OK, printed.text
    assert vendor_name in printed.text


def test_receiving_history_lot_trace_and_label_still_name_a_deleted_vendor(client: TestClient, db_session: Session):
    """The strongest of the raw cases. A receipt names the supplier the material actually
    came from -- lot traceability, invariant 5 -- and a label reprinted for material already
    on the shelf must say the same thing. Never add the filter to these loads.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    line = make_po_line(db_session, company_id=1, quantity_ordered=20)
    vendor = line.purchase_order.vendor
    vendor_name = vendor.name
    lot = f"LOT-VSD-{_next():05d}"

    received = client.post(
        "/api/v1/receiving/receive",
        headers=headers,
        json={"po_line_id": line.id, "quantity_received": 4, "lot_number": lot, "requires_inspection": False},
    )
    assert received.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), received.text
    receipt_id = received.json()["id"]

    # Close the PO so the vendor may legally be removed, then remove it.
    line.purchase_order.status = POStatus.CLOSED
    db_session.commit()
    assert client.delete(f"{VENDORS}/{vendor.id}", headers=headers).status_code == status.HTTP_200_OK
    db_session.expire_all()

    history = client.get("/api/v1/receiving/history", headers=headers)
    assert history.status_code == status.HTTP_200_OK, history.text
    assert vendor_name in history.text

    trace = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers)
    assert trace.status_code == status.HTTP_200_OK, trace.text
    assert trace.json()["supplier_name"] == vendor_name

    # The thermal label's Source block, via the service seam the comment sits on. Asserted
    # on the loaded object because the rendered PDF stream is compressed; the label body is
    # built straight off ``receipt.po_line.purchase_order.vendor``.
    loaded = PrintService(db_session)._get_receipt(1, receipt_id)
    assert loaded.po_line.purchase_order.vendor is not None
    assert loaded.po_line.purchase_order.vendor.name == vendor_name
    assert PrintService(db_session).build_label_for_receipt(1, receipt_id)[:4] == b"%PDF"


def test_supplier_quality_pages_still_list_a_deleted_vendor_by_name(client: TestClient, db_session: Session):
    """DECISION ON RECORD (fix agent's disagreement 3), and the accepted cost of it.

    A scorecard, a supplier audit and an ASL entry are AS9100D 8.4 records of a supplier
    relationship that existed. ``_same_tenant_vendor`` deliberately has no ``is_deleted``
    concept, so these pages still render the removed supplier's name, code and id -- rather
    than the rows silently vanishing (or 404-ing) with no UI able to explain or correct
    them. The reachable harm was closed at the WRITE verbs instead (§5).

    If the owner decides the ASL must stop listing removed suppliers, this test is the one
    that fails and points at the decision rather than at a bug.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    vendor = make_vendor(db_session, company_id=1)
    vendor_name = vendor.name

    created = client.post(f"{ASL}/", headers=headers, json={"vendor_id": vendor.id})
    assert created.status_code == status.HTTP_200_OK, created.text
    _soft_delete_vendor(db_session, vendor, admin.id)

    listed = client.get(f"{ASL}/", headers=headers)
    assert listed.status_code == status.HTTP_200_OK, listed.text
    row = next(r for r in listed.json() if r["id"] == created.json()["id"])
    assert row["vendor_name"] == vendor_name
    assert row["vendor_id"] == vendor.id


# ===========================================================================
# §11. Tenant isolation (invariant 1) -- the added predicates neither dropped
#      nor widened company scoping.
# ===========================================================================


def test_added_filters_did_not_widen_or_drop_company_scoping(client: TestClient, db_session: Session):
    """Company B must not reach company A's LIVE vendor through any path this change
    touched, and must still reach its own.

    This is the half a soft-delete sweep can quietly break in either direction: an
    ``is_deleted`` predicate appended to a query whose ``company_id`` filter was dropped in
    the edit looks correct in every §1-§8 test above (the deleted row is still excluded)
    while leaking the whole tenant.
    """
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    headers_b = headers_for(admin_b)
    vendor_a = make_vendor(db_session, company_id=1)
    vendor_a.name = "Tenant-A Only Alloys"
    db_session.commit()
    vendor_b = make_vendor(db_session, company_id=2)
    part_b = _make_part(db_session, company_id=2)

    # --- Not widened: every touched path refuses A's live vendor for caller B. -----------
    assert (
        client.put(f"{VENDORS}/{vendor_a.id}", headers=headers_b, json={"version": 0, "city": "Tulsa"}).status_code
        == status.HTTP_404_NOT_FOUND
    )
    assert client.get(f"{VENDORS}/{vendor_a.id}", headers=headers_b).status_code == status.HTTP_404_NOT_FOUND
    assert (
        client.post(f"{ASL}/", headers=headers_b, json={"vendor_id": vendor_a.id}).status_code
        == status.HTTP_404_NOT_FOUND
    )
    assert (
        client.post(
            "/api/v1/scanner/mappings",
            headers=headers_b,
            json={"supplier_part_number": f"SPN-{_next():05d}", "part_id": part_b.id, "vendor_id": vendor_a.id},
        ).status_code
        == status.HTTP_404_NOT_FOUND
    )

    b_search = client.get("/api/v1/search/", headers=headers_b, params={"q": "Tenant-A Only"}).json()["results"]
    assert not any(r["type"] == "vendor" and r["id"] == vendor_a.id for r in b_search)
    assert match_vendor("Tenant-A Only Alloys", db_session, company_id=2).match_id != vendor_a.id
    assert MRPAutoService(db_session, company_id=2)._get_preferred_vendor(part_b.id).id == vendor_b.id

    # --- Not dropped: B still reaches its OWN live vendor on the same paths. ------------
    assert (
        client.put(f"{VENDORS}/{vendor_b.id}", headers=headers_b, json={"version": 0, "city": "Tulsa"}).status_code
        == status.HTTP_200_OK
    )
    assert client.get(f"{VENDORS}/{vendor_b.id}", headers=headers_b).status_code == status.HTTP_200_OK
    assert client.post(f"{ASL}/", headers=headers_b, json={"vendor_id": vendor_b.id}).status_code == status.HTTP_200_OK
    assert (
        client.post(
            "/api/v1/scanner/mappings",
            headers=headers_b,
            json={"supplier_part_number": f"SPN-{_next():05d}", "part_id": part_b.id, "vendor_id": vendor_b.id},
        ).status_code
        == status.HTTP_200_OK
    )
    assert client.get("/api/v1/setup/health", headers=headers_b).json()["counts"]["vendors"] == 1

    # A's own vendor is untouched by every refusal above -- assert the VICTIM'S STORED ROW,
    # not just B's status codes: a handler that 404s and writes anyway passes a status-only
    # test. And A still reaches its own vendor, so nothing was scoped away from its owner.
    db_session.expire_all()
    assert db_session.query(Vendor).filter(Vendor.id == vendor_a.id).one().city is None
    headers_a = headers_for(admin_a)
    assert client.get(f"{VENDORS}/{vendor_a.id}", headers=headers_a).status_code == status.HTTP_200_OK
    a_search = client.get("/api/v1/search/", headers=headers_a, params={"q": "Tenant-A Only"}).json()["results"]
    assert any(r["type"] == "vendor" and r["id"] == vendor_a.id for r in a_search)


def test_a_deleted_vendor_in_another_company_is_still_a_404_not_a_400(client: TestClient, db_session: Session):
    """A cross-tenant refusal must stay indistinguishable from an absent id. The re-delete
    guard answers 400 "already deleted" from a RAW lookup -- so if that lookup ever lost its
    ``company_id`` predicate, company B could probe whether company A's vendor id exists AND
    whether it is deleted, straight off the status code."""
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    vendor_a = make_vendor(db_session, company_id=1)
    _soft_delete_vendor(db_session, vendor_a, admin_a.id)

    deleted = client.delete(f"{VENDORS}/{vendor_a.id}", headers=headers_for(admin_b))
    assert deleted.status_code == status.HTTP_404_NOT_FOUND, deleted.text
    restored = client.post(f"{VENDORS}/{vendor_a.id}/restore", headers=headers_for(admin_b))
    assert restored.status_code == status.HTTP_404_NOT_FOUND, restored.text

    db_session.expire_all()
    assert db_session.query(Vendor).filter(Vendor.id == vendor_a.id).one().is_deleted is True


def test_deleted_vendor_code_is_free_in_a_different_company(client: TestClient, db_session: Session):
    """The duplicate probes stay raw, but they are still tenant-scoped: the constraint is
    (company_id, code), so a tombstone in company A must not block company B from using the
    same code. Pins that "raw" means "no is_deleted predicate", never "no company_id one"."""
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    _ensure_company(db_session, 2)
    vendor_a = make_vendor(db_session, company_id=1)
    shared_code = vendor_a.code
    _soft_delete_vendor(db_session, vendor_a, admin_a.id)

    resp = client.post(
        f"{VENDORS}",
        headers=headers_for(admin_b),
        json={"code": shared_code, "name": "Company B Same Code"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["code"] == shared_code

    # And company A still cannot take it back while the tombstone holds it.
    conflict = client.post(
        f"{VENDORS}", headers=headers_for(admin_a), json={"code": shared_code, "name": "Company A Retry"}
    )
    assert conflict.status_code == status.HTTP_400_BAD_REQUEST, conflict.text
