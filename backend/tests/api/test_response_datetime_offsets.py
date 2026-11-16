"""Endpoint-level proof that the converted reads serialize datetimes as UTC with a 'Z'.

``tests/test_response_datetime_offset_guard.py`` proves structurally that every
response model carries the UTC encoder; this file proves it on the wire for the
plugin-facing reads that used to emit naive strings (``2026-01-15T23:30:00``):
downtime events, OEE records, quotes, and the hand-built receiving / traceability
dicts, which have no response model and so run each value through ``to_utc_iso``.
"""

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimePlannedType
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.oee import OEERecord
from app.models.part import Part
from app.models.purchasing import POReceipt
from app.models.quote import Quote
from app.models.user import UserRole
from app.models.work_center import WorkCenter
from tests.api.test_receiving_compliance import headers_for, make_pending_receipt, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

NAIVE = datetime(2026, 1, 15, 23, 30)
WIRE = "2026-01-15T23:30:00Z"


@pytest.fixture
def work_center(db_session: Session) -> WorkCenter:
    row = WorkCenter(code="TZ-WIRE", name="Wire WC", work_center_type="welding", is_active=True, company_id=1)
    db_session.add(row)
    db_session.commit()
    return row


def test_downtime_event_datetimes_end_in_z(client, auth_headers, test_user, work_center, db_session):
    db_session.add(
        DowntimeEvent(
            work_center_id=work_center.id,
            start_time=NAIVE,
            end_time=datetime(2026, 1, 16, 0, 0),
            duration_minutes=30,
            category=DowntimeCategory.MECHANICAL,
            planned_type=DowntimePlannedType.UNPLANNED,
            reported_by=test_user.id,
            company_id=1,
            created_at=NAIVE,
            updated_at=NAIVE,
        )
    )
    db_session.commit()

    [event] = client.get("/api/v1/downtime/", headers=auth_headers).json()

    assert event["start_time"] == WIRE
    assert event["end_time"] == "2026-01-16T00:00:00Z"
    assert event["created_at"] == WIRE
    assert event["updated_at"] == WIRE


def test_oee_record_datetimes_end_in_z_and_record_date_stays_a_date(client, auth_headers, work_center, db_session):
    db_session.add(
        OEERecord(
            work_center_id=work_center.id,
            record_date=date(2026, 1, 15),
            company_id=1,
            created_at=NAIVE,
            updated_at=NAIVE,
        )
    )
    db_session.commit()

    [record] = client.get("/api/v1/oee/records", headers=auth_headers).json()

    assert record["created_at"] == WIRE
    assert record["updated_at"] == WIRE
    assert record["record_date"] == "2026-01-15"


def test_quote_datetimes_end_in_z(client, auth_headers, db_session):
    db_session.add(
        Quote(
            quote_number="Q-WIRE-1",
            customer_name="Wire Co",
            quote_date=date(2026, 1, 15),
            company_id=1,
            created_at=NAIVE,
            updated_at=NAIVE,
        )
    )
    db_session.commit()

    [quote] = client.get("/api/v1/quotes/", headers=auth_headers).json()

    assert quote["created_at"] == WIRE
    assert quote["updated_at"] == WIRE


def test_receiving_dict_datetimes_end_in_z(client, db_session):
    receipt = make_pending_receipt(db_session, company_id=1)
    receipt.received_at = NAIVE
    receipt.inspected_at = datetime(2026, 1, 16, 1, 15)
    receipt.po_line.purchase_order.supplier_acknowledged_at = NAIVE
    db_session.commit()
    headers = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))

    detail = client.get(f"/api/v1/receiving/receipt/{receipt.id}", headers=headers)
    po_view = client.get(f"/api/v1/receiving/po/{receipt.po_line.purchase_order_id}", headers=headers)

    assert detail.status_code == 200, detail.text
    assert detail.json()["received_at"] == WIRE
    assert detail.json()["inspected_at"] == "2026-01-16T01:15:00Z"
    assert po_view.status_code == 200, po_view.text
    assert po_view.json()["supplier_acknowledged_at"] == WIRE
    received = [r["received_at"] for line in po_view.json()["lines"] for r in line.get("receipts", [])]
    assert received == [WIRE]


def test_receiving_history_datetimes_end_in_z(client, db_session):
    receipt = make_pending_receipt(db_session, company_id=1)
    receipt.received_at = datetime.utcnow().replace(microsecond=0)
    db_session.commit()
    headers = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))

    rows = client.get("/api/v1/receiving/history", headers=headers).json()

    assert rows and all(row["received_at"].endswith("Z") for row in rows)
    assert all(row["inspected_at"] is None for row in rows)


def test_traceability_serial_datetimes_end_in_z_and_sort_chronologically(client, auth_headers, test_user, db_session):
    part = Part(
        part_number="TZ-SERIAL-PART",
        name="Serial part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(part)
    db_session.flush()
    db_session.add(
        InventoryItem(
            part_id=part.id,
            location="A-1",
            warehouse="MAIN",
            quantity_on_hand=1,
            serial_number="SN-TZ-1",
            received_date=NAIVE,
            company_id=1,
        )
    )
    for created_at in (datetime(2026, 1, 16, 2, 0, 0, 500000), NAIVE):
        db_session.add(
            InventoryTransaction(
                company_id=1,
                part_id=part.id,
                transaction_type=TransactionType.RECEIVE,
                quantity=1,
                serial_number="SN-TZ-1",
                created_by=test_user.id,
                created_at=created_at,
            )
        )
    db_session.commit()

    body = client.get("/api/v1/traceability/serial/SN-TZ-1", headers=auth_headers).json()

    assert body["received_date"] == WIRE
    assert [event["timestamp"] for event in body["history"]] == [WIRE, "2026-01-16T02:00:00.500000Z"]


def test_po_receipt_model_is_the_one_seeded(db_session):
    """Vacuity check for the receiving fixture: the helper really creates a POReceipt."""
    assert isinstance(make_pending_receipt(db_session, company_id=1), POReceipt)
