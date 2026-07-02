"""Roster-enriched ``GET /shop-floor/work-center-queue/{id}`` (crew kiosk read).

The queue read now serves the crew kiosk: each queued operation carries a
``roster`` of the OPEN labor TimeEntries on it (one chip per clocked-in
operator), the tally fields (``quantity_complete`` / ``quantity_scrapped`` /
``quantity_ordered``), a top-level ``server_time`` for timer-skew correction,
and the ``station`` identity block for station callers (null for users).

Headline invariants:
1. Multi-operator honesty — two operators on the SAME operation are two roster
   rows (the crew model: one TimeEntry per person per window).
2. Roster hygiene — closed entries, non-labor (BREAK/DOWNTIME) entries, and
   cross-tenant entries never render as crew.
3. Station fencing — a station may only read its OWN work center (403
   otherwise); tenant scope comes from the DB row.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry, TimeEntryType
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    ensure_company,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def _open_entry(
    db: Session,
    *,
    user,
    work_order,
    operation,
    work_center,
    company_id: int = COMPANY_A,
    entry_type: TimeEntryType = TimeEntryType.RUN,
    clock_in: datetime = None,
    clock_out: datetime = None,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=work_order.id,
        operation_id=operation.id,
        work_center_id=work_center.id,
        entry_type=entry_type,
        clock_in=clock_in or datetime.utcnow(),
        clock_out=clock_out,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def test_roster_two_operators_same_operation(client: TestClient, db_session: Session):
    """Two operators clocked into the SAME operation are two roster rows with
    the fields the kiosk chip needs (id/name/badge/type/clock_in)."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    wo, op = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)
    alice = make_user(db_session, company_id=COMPANY_A, first_name="Alice", last_name="Torres")
    bob = make_user(db_session, company_id=COMPANY_A, first_name="Bob", last_name="Miller")
    e_alice = _open_entry(db_session, user=alice, work_order=wo, operation=op, work_center=wc)
    e_bob = _open_entry(
        db_session, user=bob, work_order=wo, operation=op, work_center=wc, entry_type=TimeEntryType.SETUP
    )

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    items = [i for i in body["queue"] if i["operation_id"] == op.id]
    assert len(items) == 1
    roster = items[0]["roster"]
    assert len(roster) == 2

    by_user = {r["user_id"]: r for r in roster}
    assert by_user[alice.id]["time_entry_id"] == e_alice.id
    assert by_user[alice.id]["operator_name"] == "Alice T."
    assert by_user[alice.id]["employee_id"] == alice.employee_id
    assert by_user[alice.id]["entry_type"] == "run"
    assert by_user[alice.id]["clock_in"].endswith("Z")
    assert by_user[bob.id]["time_entry_id"] == e_bob.id
    assert by_user[bob.id]["entry_type"] == "setup"


def test_queue_tally_fields_and_server_time(client: TestClient, db_session: Session):
    """The tally block is server-derived: quantity_complete / quantity_scrapped /
    quantity_ordered per item, plus a UTC-Z server_time at the top level."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    wo, op = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc, quantity_ordered=50)
    op.quantity_complete = 37
    op.quantity_scrapped = 2
    db_session.commit()

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    item = next(i for i in body["queue"] if i["operation_id"] == op.id)
    assert item["quantity_complete"] == 37
    assert item["quantity_scrapped"] == 2
    assert item["quantity_ordered"] == 50
    assert item["roster"] == []

    assert body["server_time"].endswith("Z")
    # Sanity: the timestamp parses and is (about) now.
    server_time = datetime.fromisoformat(body["server_time"].replace("Z", "+00:00"))
    assert abs((server_time.replace(tzinfo=None) - datetime.utcnow()).total_seconds()) < 60


def test_station_block_for_station_null_for_user(client: TestClient, db_session: Session):
    """Station callers get their identity block; user callers get station=null."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc, label="Weld Kiosk 9")
    manager = make_user(db_session, company_id=COMPANY_A)

    as_station = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert as_station.status_code == status.HTTP_200_OK, as_station.text
    assert as_station.json()["station"] == {"id": station.id, "label": "Weld Kiosk 9"}

    as_user = client.get(queue_url(wc.id), headers=user_headers(manager))
    assert as_user.status_code == status.HTTP_200_OK, as_user.text
    assert as_user.json()["station"] is None


def test_station_403_on_foreign_work_center(client: TestClient, db_session: Session):
    """A station can only read ITS OWN work center's queue — any other WC id
    (same company or not) is 403."""
    wc_own = make_work_center(db_session, company_id=COMPANY_A)
    wc_sibling = make_work_center(db_session, company_id=COMPANY_A)
    wc_foreign = make_work_center(db_session, company_id=COMPANY_B)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc_own)
    token = kiosk_token_for(station)

    sibling = client.get(queue_url(wc_sibling.id), headers=bearer(token))
    assert sibling.status_code == status.HTTP_403_FORBIDDEN, sibling.text

    foreign = client.get(queue_url(wc_foreign.id), headers=bearer(token))
    assert foreign.status_code == status.HTTP_403_FORBIDDEN, foreign.text


def test_user_caller_may_read_any_queue_in_company(client: TestClient, db_session: Session):
    """The user path is unchanged: any authenticated user reads any of their
    company's queues (no station WC binding applies)."""
    wc1 = make_work_center(db_session, company_id=COMPANY_A)
    wc2 = make_work_center(db_session, company_id=COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A)

    for wc in (wc1, wc2):
        resp = client.get(queue_url(wc.id), headers=user_headers(user))
        assert resp.status_code == status.HTTP_200_OK, resp.text


def test_closed_entries_excluded_from_roster(client: TestClient, db_session: Session):
    """A clocked-out entry is history, not crew — it never renders as a chip."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    wo, op = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)
    alice = make_user(db_session, company_id=COMPANY_A)
    bob = make_user(db_session, company_id=COMPANY_A)
    _open_entry(db_session, user=alice, work_order=wo, operation=op, work_center=wc)
    _open_entry(
        db_session,
        user=bob,
        work_order=wo,
        operation=op,
        work_center=wc,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
    )

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    roster = next(i for i in resp.json()["queue"] if i["operation_id"] == op.id)["roster"]
    assert [r["user_id"] for r in roster] == [alice.id]


def test_non_labor_entries_excluded_from_roster(client: TestClient, db_session: Session):
    """Open BREAK/DOWNTIME entries are clocked time but not crew labor — no chip."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    wo, op = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)
    alice = make_user(db_session, company_id=COMPANY_A)
    bob = make_user(db_session, company_id=COMPANY_A)
    _open_entry(db_session, user=alice, work_order=wo, operation=op, work_center=wc)
    _open_entry(db_session, user=bob, work_order=wo, operation=op, work_center=wc, entry_type=TimeEntryType.BREAK)

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    roster = next(i for i in resp.json()["queue"] if i["operation_id"] == op.id)["roster"]
    assert [r["user_id"] for r in roster] == [alice.id]


def test_cross_tenant_entries_excluded_from_roster(client: TestClient, db_session: Session):
    """Tenant isolation on the roster query itself: an open entry tagged to
    ANOTHER company (even one anomalously pointing at this operation) never
    surfaces in this company's roster."""
    ensure_company(db_session, COMPANY_B)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    wo, op = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)
    alice = make_user(db_session, company_id=COMPANY_A)
    intruder = make_user(db_session, company_id=COMPANY_B)
    _open_entry(db_session, user=alice, work_order=wo, operation=op, work_center=wc)
    # Anomalous row: company-B entry pointing at company-A's operation. The
    # roster's TimeEntry.company_id filter must drop it regardless.
    _open_entry(db_session, user=intruder, work_order=wo, operation=op, work_center=wc, company_id=COMPANY_B)

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    roster = next(i for i in resp.json()["queue"] if i["operation_id"] == op.id)["roster"]
    assert [r["user_id"] for r in roster] == [alice.id]


def test_soft_deleted_work_order_excluded_from_queue(client: TestClient, db_session: Session):
    """A soft-deleted WO's operations must not queue on the crew kiosk even
    while its status is non-terminal (WO soft-delete does not change status)."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    wo_live, op_live = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)
    wo_deleted, op_deleted = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)
    wo_deleted.is_deleted = True
    db_session.commit()

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    queued_ids = [i["operation_id"] for i in resp.json()["queue"]]
    assert op_live.id in queued_ids
    assert op_deleted.id not in queued_ids
