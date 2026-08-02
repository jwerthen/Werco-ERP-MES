"""Vendor-quality acceptance metric: NOT_REQUIRED (dock-to-stock) counts as accepted.

Since PR #127 the receiving default is dock-to-stock, so a no-inspection receipt
lands ``inspection_status = NOT_REQUIRED`` rather than PASSED. The vendor-quality
metric in ``AnalyticsService.get_quality_metrics`` (the ``by_vendor``
``VendorQuality`` rollup) must therefore count NOT_REQUIRED as *accepted* —
otherwise a vendor received entirely dock-to-stock reads as ~0% acceptance despite
zero rejections. This PR added NOT_REQUIRED to the "accepted" ``case`` predicate;
"rejected" stays FAILED-only.

The acceptance-rate PREDICATE is locked here directly via an equivalent
aggregation with an explicit vendor→PO→line→receipt join
(``_vendor_quality_rollup``), copying the service's ``case`` expressions and
acceptance-rate formula verbatim — the service now uses the same explicit join
chain, and the end-to-end service call is pinned below too.
"""

from datetime import date, datetime

import pytest
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.purchasing import (
    InspectionStatus,
    POReceipt,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptStatus,
    Vendor,
)
from app.models.user import UserRole
from app.services.analytics_service import AnalyticsService
from tests.api.test_receiving_compliance import _next, make_po_line, make_user

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

WINDOW_START = date(2026, 6, 1)
WINDOW_END = date(2026, 6, 30)
IN_WINDOW = datetime(2026, 6, 15, 12, 0, 0)


def _add_receipt(db: Session, *, line: PurchaseOrderLine, received_by: int, inspection_status: InspectionStatus):
    """A committed POReceipt on ``line`` with a controlled inspection_status, in-window."""
    n = _next()
    accepted = 5 if inspection_status in (InspectionStatus.PASSED, InspectionStatus.NOT_REQUIRED) else 0
    receipt = POReceipt(
        receipt_number=f"RCV-VQ-{n:05d}",
        po_line_id=line.id,
        quantity_received=5,
        quantity_accepted=accepted,
        quantity_rejected=5 if inspection_status == InspectionStatus.FAILED else 0,
        lot_number=f"LOT-VQ-{n:05d}",
        status=ReceiptStatus.ACCEPTED,
        inspection_status=inspection_status,
        received_by=received_by,
        received_at=IN_WINDOW,
        company_id=1,
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


def _vendor_quality_rollup(db: Session, company_id: int, start: date, end: date) -> dict:
    """Replicate the service's vendor_stats aggregation.

    The explicit vendor→PO→line→receipt join, the soft-delete filters, the three
    aggregate columns (receipts / accepted / rejected ``case`` expressions) and the
    ``accepted / receipts * 100`` acceptance-rate formula are copied verbatim from
    ``AnalyticsService.get_quality_metrics`` so the predicate stays locked
    independently of the service plumbing.
    Returns ``{vendor_id: {receipts, accepted, rejected, acceptance_rate}}``.
    """
    rows = (
        db.query(
            Vendor.id.label("vendor_id"),
            func.count(POReceipt.id).label("receipts"),
            func.sum(
                case(
                    (POReceipt.inspection_status.in_([InspectionStatus.PASSED, InspectionStatus.NOT_REQUIRED]), 1),
                    else_=0,
                )
            ).label("accepted"),
            func.sum(case((POReceipt.inspection_status == InspectionStatus.FAILED, 1), else_=0)).label("rejected"),
        )
        .select_from(Vendor)
        .join(PurchaseOrder, PurchaseOrder.vendor_id == Vendor.id)
        .join(PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
        .join(POReceipt, POReceipt.po_line_id == PurchaseOrderLine.id)
        .filter(
            Vendor.company_id == company_id,
            POReceipt.company_id == company_id,
            Vendor.is_deleted == False,  # noqa: E712
            PurchaseOrder.is_deleted == False,  # noqa: E712
            POReceipt.is_deleted == False,  # noqa: E712
            POReceipt.received_at >= datetime.combine(start, datetime.min.time()),
            POReceipt.received_at <= datetime.combine(end, datetime.max.time()),
        )
        .group_by(Vendor.id)
        .all()
    )
    out = {}
    for r in rows:
        accepted = r.accepted or 0
        rejected = r.rejected or 0
        rate = round((accepted / r.receipts * 100) if r.receipts > 0 else 0, 1)
        out[r.vendor_id] = {
            "receipts": r.receipts,
            "accepted": accepted,
            "rejected": rejected,
            "acceptance_rate": rate,
        }
    return out


def test_all_dock_to_stock_vendor_reports_100pct_accepted(db_session: Session):
    """A vendor received entirely dock-to-stock (all NOT_REQUIRED) is 100% accepted,
    NOT 0% — the exact regression this PR fixes."""
    receiver = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=100)
    vendor_id = line.purchase_order.vendor_id

    for _ in range(3):
        _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.NOT_REQUIRED)

    rollup = _vendor_quality_rollup(db_session, 1, WINDOW_START, WINDOW_END)
    assert vendor_id in rollup
    row = rollup[vendor_id]
    assert row["receipts"] == 3
    assert row["accepted"] == 3  # NOT_REQUIRED counts as accepted
    assert row["rejected"] == 0
    assert row["acceptance_rate"] == 100.0  # NOT 0.0


def test_mixed_vendor_counts_failed_as_rejected_and_rate_between(db_session: Session):
    """PASSED + NOT_REQUIRED are accepted; FAILED is rejected; rate is strictly between.

    Confirms the change did not break the other legs: a real PASSED still counts as
    accepted, a FAILED still counts as rejected, and PENDING (unresolved) counts in
    the receipts denominator but is neither accepted nor rejected.
    """
    receiver = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=100)
    vendor_id = line.purchase_order.vendor_id

    _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.NOT_REQUIRED)
    _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.PASSED)
    _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.NOT_REQUIRED)
    _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.FAILED)
    _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.PENDING)

    rollup = _vendor_quality_rollup(db_session, 1, WINDOW_START, WINDOW_END)
    row = rollup[vendor_id]
    assert row["receipts"] == 5
    assert row["accepted"] == 3  # 2 NOT_REQUIRED + 1 PASSED
    assert row["rejected"] == 1  # FAILED only (PENDING is neither)
    assert row["acceptance_rate"] == 60.0  # 3/5, strictly between 0 and 100


def test_not_required_is_never_counted_as_rejected(db_session: Session):
    """Guard the opposite error: NOT_REQUIRED must not leak into the FAILED (rejected) leg."""
    receiver = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=100)
    vendor_id = line.purchase_order.vendor_id

    _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.NOT_REQUIRED)

    row = _vendor_quality_rollup(db_session, 1, WINDOW_START, WINDOW_END)[vendor_id]
    assert row["rejected"] == 0
    assert row["accepted"] == 1


def test_get_quality_metrics_counts_not_required_as_accepted_end_to_end(db_session: Session):
    """End-to-end via the real service."""
    receiver = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=100)
    vendor_id = line.purchase_order.vendor_id
    for _ in range(3):
        _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.NOT_REQUIRED)

    metrics = AnalyticsService(db_session, 1).get_quality_metrics(WINDOW_START, WINDOW_END)
    row = next(v for v in metrics.by_vendor if v.vendor_id == vendor_id)
    assert row.receipts_count == 3
    assert row.accepted_count == 3
    assert row.acceptance_rate == 100.0


def test_voided_receipt_is_excluded_from_vendor_quality(db_session: Session):
    """A voided receipt must not count (invariant 3): the receiving void path
    soft-deletes the row, and a voided FAILED receipt — the wrong-entry case the
    void verb exists for — would otherwise permanently ding the vendor's
    acceptance rate."""
    receiver = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=100)
    vendor_id = line.purchase_order.vendor_id

    _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.NOT_REQUIRED)
    voided = _add_receipt(db_session, line=line, received_by=receiver.id, inspection_status=InspectionStatus.FAILED)
    voided.soft_delete(receiver.id)
    db_session.commit()

    metrics = AnalyticsService(db_session, 1).get_quality_metrics(WINDOW_START, WINDOW_END)
    row = next(v for v in metrics.by_vendor if v.vendor_id == vendor_id)
    assert row.receipts_count == 1, "the voided receipt leaves the denominator"
    assert row.accepted_count == 1
    assert row.rejected_count == 0, "the voided FAILED receipt no longer counts as rejected"
    assert row.acceptance_rate == 100.0


def test_vendor_attribution_uses_the_true_join_columns(db_session: Session):
    """Join-semantics guard through the REAL service. The other fixtures in this
    module create vendor/PO/line in lockstep, so their autoincrement ids can
    coincide (vendor.id == po.id == po_line.id) and a wrong join column — e.g.
    ``POReceipt.po_line_id == PurchaseOrder.id`` — would produce identical
    results. Here the three joined tables get explicit, pairwise-disjoint id
    ranges, two vendors receive on distinct POs (one PO carrying two lines, the
    other a FAILED receipt), and a receipt-less vendor exists, so only the true
    vendor→PO→line→receipt columns can attribute the rows this way."""
    receiver = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    n = _next()
    part = Part(
        part_number=f"P-JS-{n:05d}",
        name=f"Join part {n}",
        description="join-semantics fixture",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(part)
    db_session.flush()

    def _vendor(id_: int) -> Vendor:
        m = _next()
        v = Vendor(
            id=id_, code=f"V-JS-{m:05d}", name=f"Join vendor {m}", is_active=True, is_approved=True, company_id=1
        )
        db_session.add(v)
        return v

    def _po(id_: int, vendor_id: int) -> PurchaseOrder:
        m = _next()
        p = PurchaseOrder(
            id=id_,
            po_number=f"PO-JS-{m:05d}",
            vendor_id=vendor_id,
            status=POStatus.SENT,
            order_date=WINDOW_START,
            company_id=1,
        )
        db_session.add(p)
        return p

    def _line(id_: int, po_id: int, line_number: int) -> PurchaseOrderLine:
        li = PurchaseOrderLine(
            id=id_,
            purchase_order_id=po_id,
            line_number=line_number,
            part_id=part.id,
            quantity_ordered=10,
            quantity_received=0.0,
            unit_price=5.0,
            is_closed=False,
            company_id=1,
        )
        db_session.add(li)
        return li

    vendor_a, vendor_b, vendor_c = _vendor(910_001), _vendor(910_002), _vendor(910_003)
    db_session.flush()
    po_a, po_b = _po(920_001, vendor_a.id), _po(920_002, vendor_b.id)
    db_session.flush()
    line_a1, line_a2, line_b = _line(930_001, po_a.id, 1), _line(930_002, po_a.id, 2), _line(930_003, po_b.id, 1)
    db_session.commit()

    # The guarantee that makes a wrong join column unable to reproduce the
    # expected attribution: no id coincides across the three joined tables.
    all_ids = [vendor_a.id, vendor_b.id, vendor_c.id, po_a.id, po_b.id, line_a1.id, line_a2.id, line_b.id]
    assert len(set(all_ids)) == len(all_ids)

    _add_receipt(db_session, line=line_a1, received_by=receiver.id, inspection_status=InspectionStatus.NOT_REQUIRED)
    _add_receipt(db_session, line=line_a2, received_by=receiver.id, inspection_status=InspectionStatus.PASSED)
    _add_receipt(db_session, line=line_b, received_by=receiver.id, inspection_status=InspectionStatus.FAILED)

    metrics = AnalyticsService(db_session, 1).get_quality_metrics(WINDOW_START, WINDOW_END)
    by_vendor = {v.vendor_id: v for v in metrics.by_vendor}

    row_a = by_vendor[vendor_a.id]
    assert row_a.receipts_count == 2  # both lines of PO-A, nothing from PO-B
    assert row_a.accepted_count == 2
    assert row_a.rejected_count == 0
    assert row_a.acceptance_rate == 100.0

    row_b = by_vendor[vendor_b.id]
    assert row_b.receipts_count == 1
    assert row_b.accepted_count == 0
    assert row_b.rejected_count == 1  # the FAILED receipt lands on vendor B alone
    assert row_b.acceptance_rate == 0.0

    assert vendor_c.id not in by_vendor, "a vendor with no receipts must be absent (inner join)"


def test_quality_metrics_endpoint_returns_200(client, db_session: Session):
    """API smoke: GET /analytics/quality-metrics returns 200 for an authorized role
    (the endpoint 500'd unconditionally while the vendor_stats join was malformed)."""
    from tests.lean_phase1_helpers import headers_for

    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    resp = client.get("/api/v1/analytics/quality-metrics", headers=headers_for(quality))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "by_vendor" in body and "summary" in body
