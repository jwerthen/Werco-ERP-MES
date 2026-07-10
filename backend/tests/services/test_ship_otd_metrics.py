"""Metric-exactness locks for the ship-based OTD/OTIF legs (Lean Phase 1, issue #88).

``AnalyticsService`` gains two SHIP-anchored delivery measures against the
promise (precedence: must_ship_by || due_date):
  * OTD (fulfillment-anchored): of WOs whose FULL ordered quantity finished
    shipping in the window (partials roll up cumulatively; the full-ship date is
    the shipment that crossed the ordered qty), the share on/before promise.
  * OTIF (promise-anchored): of WOs PROMISED in the window, the share shipped
    IN FULL by the promise -- an open WO past its promise is a live miss.
Only real shipments count (ship_date set, not soft-deleted, not CANCELLED);
CANCELLED WOs are excluded; empty denominators are None, never a fake 100.

Plus the /reports/ship-otd detail report (rows, per-customer rollup, promise
hygiene) and the endpoint contract.
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.shipping import ShipmentStatus
from app.models.work_order import WorkOrderStatus
from app.services.analytics_service import AnalyticsService
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    headers_for,
    make_part,
    make_shipment,
    make_user,
    make_wo,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

WINDOW_START = date(2026, 6, 1)
WINDOW_END = date(2026, 6, 30)


def _seed_mixed_delivery_set(db: Session):
    """The canonical 4-WO set used by the exactness tests.

    S1 on time (partials roll up), S2 late (must_ship_by beats due_date),
    S3 unmeasurable (no promise), S4 partial-only (never fully shipped).
    """
    part = make_part(db)
    s1 = make_wo(db, part, quantity_ordered=10, due_date=date(2026, 6, 10), customer_name="Acme")
    make_shipment(db, s1, ship_date=date(2026, 6, 5), quantity_shipped=4)
    make_shipment(db, s1, ship_date=date(2026, 6, 9), quantity_shipped=6)

    s2 = make_wo(
        db, part, quantity_ordered=10, must_ship_by=date(2026, 6, 5), due_date=date(2026, 6, 30), customer_name="Acme"
    )
    make_shipment(db, s2, ship_date=date(2026, 6, 3), quantity_shipped=5)
    make_shipment(db, s2, ship_date=date(2026, 6, 8), quantity_shipped=5)

    s3 = make_wo(db, part, quantity_ordered=10, customer_name="NoPromise Inc")
    make_shipment(db, s3, ship_date=date(2026, 6, 4), quantity_shipped=10)

    s4 = make_wo(db, part, quantity_ordered=10, due_date=date(2026, 6, 20), customer_name="Partial LLC")
    make_shipment(db, s4, ship_date=date(2026, 6, 15), quantity_shipped=4)
    return s1, s2, s3, s4


def test_ship_otd_partial_rollup_promise_precedence_and_exact_pct(db_session: Session):
    _seed_mixed_delivery_set(db_session)

    svc = AnalyticsService(db_session, COMPANY_A)
    # Measured: S1 (full Jun 9 <= Jun 10, on time) + S2 (full Jun 8 > must_ship_by
    # Jun 5 -> LATE even though due_date Jun 30 would have said on time).
    # S3 has no promise, S4 never fully shipped -> both unmeasurable.
    assert svc.get_ship_otd_value(WINDOW_START, WINDOW_END) == pytest.approx(50.0)


def test_otif_counts_unshipped_promises_as_misses(db_session: Session):
    _seed_mixed_delivery_set(db_session)

    svc = AnalyticsService(db_session, COMPANY_A)
    # Promised in window: S1 (in full by Jun 10: 10/10), S2 (by Jun 5: 5/10 miss),
    # S4 (by Jun 20: 4/10 miss). S3 has no promise. -> 1/3.
    assert svc._get_otif_value(WINDOW_START, WINDOW_END) == pytest.approx(33.33333, rel=1e-4)


def test_cancelled_wos_and_non_counted_shipments_are_excluded(db_session: Session):
    part = make_part(db_session)
    # CANCELLED WO promised in window: not a delivery miss, not a denominator entry.
    make_wo(db_session, part, status_=WorkOrderStatus.CANCELLED, quantity_ordered=5, due_date=date(2026, 6, 12))
    # Promised WO whose only shipments are cancelled / soft-deleted / dateless:
    # nothing counted -> OTD has no candidate; OTIF sees an in-window promise, miss.
    wo = make_wo(db_session, part, quantity_ordered=5, due_date=date(2026, 6, 15))
    make_shipment(db_session, wo, ship_date=date(2026, 6, 10), quantity_shipped=5, status=ShipmentStatus.CANCELLED)
    make_shipment(db_session, wo, ship_date=date(2026, 6, 10), quantity_shipped=5, is_deleted=True)
    make_shipment(db_session, wo, ship_date=None, quantity_shipped=5)

    svc = AnalyticsService(db_session, COMPANY_A)
    assert svc.get_ship_otd_value(WINDOW_START, WINDOW_END) is None  # no counted shipment at all
    assert svc._get_otif_value(WINDOW_START, WINDOW_END) == pytest.approx(0.0)  # the live WO missed; cancelled ignored

    report = svc.get_ship_otd_report(WINDOW_START, WINDOW_END)
    assert [row.work_order_id for row in report.rows] == [wo.id]
    assert report.rows[0].quantity_shipped == pytest.approx(0.0)
    assert report.rows[0].on_time is False  # promise passed, nothing really shipped


def test_cancelled_but_fully_shipped_wo_excluded_from_ship_otd(db_session: Session):
    """A WO cancelled despite having shipped in full must NOT count in the
    fulfillment-anchored ship-OTD leg -- same population rule as OTIF (pins the
    CANCELLED exclusion added to _ship_otd_candidates after review)."""
    part = make_part(db_session)
    cancelled = make_wo(
        db_session, part, status_=WorkOrderStatus.CANCELLED, quantity_ordered=5, due_date=date(2026, 6, 15)
    )
    make_shipment(db_session, cancelled, ship_date=date(2026, 6, 10), quantity_shipped=5)

    svc = AnalyticsService(db_session, COMPANY_A)
    # It was the only shipped WO: with it excluded the denominator is empty ->
    # n/a, never a 100% "hit" minted by a cancelled order shipped on time.
    assert svc.get_ship_otd_value(WINDOW_START, WINDOW_END) is None
    # And it surfaces nowhere in the detail report either.
    report = svc.get_ship_otd_report(WINDOW_START, WINDOW_END)
    assert report.rows == []
    assert report.otd_ship_pct is None


def test_ship_otd_none_on_empty_denominator_and_tenant_scoped(db_session: Session):
    # Company B has deliveries; company A must still read n/a, not B's numbers.
    part_b = make_part(db_session, company_id=COMPANY_B)
    wo_b = make_wo(db_session, part_b, company_id=COMPANY_B, quantity_ordered=5, due_date=date(2026, 6, 10))
    make_shipment(db_session, wo_b, company_id=COMPANY_B, ship_date=date(2026, 6, 5), quantity_shipped=5)

    svc = AnalyticsService(db_session, COMPANY_A)
    assert svc.get_ship_otd_value(WINDOW_START, WINDOW_END) is None
    assert svc._get_otif_value(WINDOW_START, WINDOW_END) is None

    svc_b = AnalyticsService(db_session, COMPANY_B)
    assert svc_b.get_ship_otd_value(WINDOW_START, WINDOW_END) == pytest.approx(100.0)


def test_ship_otd_report_rows_rollup_and_open_miss_days_late(db_session: Session):
    s1, s2, s3, s4 = _seed_mixed_delivery_set(db_session)
    # An open WO whose promise passed without full shipment: a LIVE miss whose
    # days_late grows daily. Promise is in the fixed window AND in the past.
    days_since_promise = (date.today() - date(2026, 6, 25)).days

    svc = AnalyticsService(db_session, COMPANY_A)
    report = svc.get_ship_otd_report(WINDOW_START, WINDOW_END)

    assert report.otd_ship_pct == pytest.approx(50.0)
    assert report.otif_pct == pytest.approx(33.3)

    rows = {row.work_order_id: row for row in report.rows}
    assert rows[s1.id].on_time is True
    assert rows[s1.id].fully_shipped is True
    assert rows[s1.id].full_ship_date == date(2026, 6, 9)
    assert rows[s1.id].days_late == -1  # full ship Jun 9 vs promise Jun 10
    assert rows[s1.id].promise_source == "due_date"

    assert rows[s2.id].promise_source == "must_ship_by"
    assert rows[s2.id].promise_date == date(2026, 6, 5)
    assert rows[s2.id].on_time is False
    assert rows[s2.id].days_late == 3  # full Jun 8 vs promise Jun 5

    # No promise: shipped row present but undeterminable (hygiene's job).
    assert rows[s3.id].on_time is None
    assert rows[s3.id].promise_date is None

    # Partial only, promise passed -> live miss counted from today.
    assert rows[s4.id].on_time is False
    assert rows[s4.id].fully_shipped is False
    assert rows[s4.id].days_late == (date.today() - date(2026, 6, 20)).days

    # Per-customer rollup covers only determinable rows: Acme = S1 + S2.
    acme = next(c for c in report.by_customer if c.customer_name == "Acme")
    assert (acme.work_orders, acme.on_time, acme.late) == (2, 1, 1)
    assert acme.otd_pct == pytest.approx(50.0)
    assert acme.avg_days_late == pytest.approx(3.0)
    # S3's customer has no determinable rows -> no rollup entry.
    assert all(c.customer_name != "NoPromise Inc" for c in report.by_customer)

    # Promise hygiene: S3 shipped in the window with NEITHER promise field.
    hygiene = {row.work_order_id: row for row in report.promise_hygiene}
    assert s3.id in hygiene
    assert hygiene[s3.id].quantity_shipped == pytest.approx(10.0)
    assert hygiene[s3.id].last_ship_date == date(2026, 6, 4)
    assert days_since_promise >= 0  # sanity: fixture window genuinely in the past


def test_promise_hygiene_lists_open_promiseless_wos(db_session: Session):
    part = make_part(db_session)
    open_wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)  # no promise, open
    # COMPLETE + promiseless but NOT shipped in window: not actionable, not listed.
    make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)

    report = AnalyticsService(db_session, COMPANY_A).get_ship_otd_report(WINDOW_START, WINDOW_END)
    assert [row.work_order_id for row in report.promise_hygiene] == [open_wo.id]


def test_kpi_dashboard_carries_ship_otd_and_otif_legs(db_session: Session):
    """Empty window: both new KPI legs serialize as honest n/a (None), FLAT trend."""
    dashboard = AnalyticsService(db_session, COMPANY_A).get_kpi_dashboard(WINDOW_START, WINDOW_END)
    assert dashboard.on_time_delivery_ship is not None
    assert dashboard.otif is not None
    assert dashboard.on_time_delivery_ship.value is None
    assert dashboard.otif.value is None


def test_ship_otd_report_endpoint_contract(client: TestClient, db_session: Session):
    """GET /reports/ship-otd: any authenticated user, live values in the payload."""
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, quantity_ordered=5, due_date=date.today() - timedelta(days=1))
    make_shipment(db_session, wo, ship_date=date.today() - timedelta(days=3), quantity_shipped=5)

    resp = client.get("/api/v1/reports/ship-otd", params={"period": "30d"}, headers=headers_for(user))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["otd_ship_pct"] == 100.0
    assert payload["otif_pct"] == 100.0
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["work_order_number"] == wo.work_order_number
    assert payload["rows"][0]["on_time"] is True
    assert {"by_customer", "promise_hygiene", "period_start", "period_end", "generated_at"} <= set(payload.keys())
