"""Vendor-quality acceptance metric: NOT_REQUIRED (dock-to-stock) counts as accepted.

Since PR #127 the receiving default is dock-to-stock, so a no-inspection receipt
lands ``inspection_status = NOT_REQUIRED`` rather than PASSED. The vendor-quality
metric in ``AnalyticsService.get_quality_metrics`` (the ``by_vendor``
``VendorQuality`` rollup) must therefore count NOT_REQUIRED as *accepted* —
otherwise a vendor received entirely dock-to-stock reads as ~0% acceptance despite
zero rejections. This PR added NOT_REQUIRED to the "accepted" ``case`` predicate;
"rejected" stays FAILED-only.

IMPORTANT — pre-existing defect surfaced while writing these tests:
``AnalyticsService.get_quality_metrics`` currently raises ``sqlalchemy.exc.
ArgumentError`` at query-BUILD time on EVERY call (data-independent), because its
vendor_stats join clause ``POReceipt.po_line.has(purchase_order=Vendor.
purchase_orders)`` compares a relationship to a relationship attribute. That join
is UNCHANGED by this PR (present since the Analytics module was added) and has no
test coverage, so ``GET /api/v1/analytics/quality-metrics`` 500s regardless of the
NOT_REQUIRED work. Because the service method is unreachable, the acceptance-rate
PREDICATE this PR actually changed is locked here directly via an equivalent
aggregation with a WORKING vendor→PO→line→receipt join
(``_vendor_quality_rollup``), copying the service's ``case`` expressions and
acceptance-rate formula verbatim. The end-to-end service call is additionally
pinned as a strict xfail so it flips to a failure (prompting marker removal) the
moment the broken join is fixed.
"""

from datetime import date, datetime

import pytest
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.purchasing import (
    InspectionStatus,
    POReceipt,
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
    """Replicate the service's vendor_stats aggregation with a WORKING join.

    The three aggregate columns (receipts / accepted / rejected ``case``
    expressions) and the ``accepted / receipts * 100`` acceptance-rate formula are
    copied verbatim from ``AnalyticsService.get_quality_metrics``; only the broken
    ``POReceipt.po_line.has(...)`` join is swapped for an explicit
    vendor→PO→line→receipt join so the (unchanged) predicate can be exercised.
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


@pytest.mark.xfail(
    reason=(
        "PRE-EXISTING broken join in AnalyticsService.get_quality_metrics vendor_stats: "
        "POReceipt.po_line.has(purchase_order=Vendor.purchase_orders) raises sqlalchemy ArgumentError "
        "at query-build time on EVERY call (data-independent, unchanged by this PR, no coverage) -> "
        "GET /api/v1/analytics/quality-metrics 500s. This pins the NOT_REQUIRED end-to-end behavior; "
        "when the join is fixed it xpasses and strict mode fails -> remove this marker. See test report."
    ),
    strict=True,
)
def test_get_quality_metrics_counts_not_required_as_accepted_end_to_end(db_session: Session):
    """End-to-end via the real service (currently unreachable — see xfail reason)."""
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
