"""/exports/* date filters select company-LOCAL calendar days, and datetime cells carry an offset.

Before this change the two exports that filter a timestamp column
(``/exports/work-orders/export`` on ``WorkOrder.created_at`` and
``/exports/inventory/transactions/export`` on ``InventoryTransaction.created_at``)
compared the requested ``date`` against UTC midnight, so a Central "January 15" ran
from 6 PM CST on the 14th to 5:59 PM CST on the 15th and second-shift rows landed in
the next day's file. The window is now local midnight to (exclusive) next local
midnight in ``Company.timezone`` (default America/Chicago), and every datetime cell
is ISO-8601 in that zone WITH its offset (``2026-01-15T17:30:00-06:00``).

Each boundary case seeds four rows -- one microsecond before the local day, exactly
at local midnight, one microsecond before the next local midnight, and exactly at it
-- and expects only the middle two, the ``test_consumables.py`` 23/25-hour pattern.
"""

import csv
import io
from datetime import date, datetime, timedelta
from typing import List

import openpyxl
import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quote import Quote
from app.models.user import User
from app.models.work_order import WorkOrder
from tests.api.export_audit_helpers import committed_export_rows

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WO_URL = "/api/v1/exports/work-orders/export"
TXN_URL = "/api/v1/exports/inventory/transactions/export"

BOUNDARY_DAYS = [
    # (local day, local midnight in UTC, next local midnight in UTC)
    pytest.param("2026-01-15", datetime(2026, 1, 15, 6), datetime(2026, 1, 16, 6), id="cst"),
    pytest.param("2026-07-15", datetime(2026, 7, 15, 5), datetime(2026, 7, 16, 5), id="cdt"),
    pytest.param("2026-03-08", datetime(2026, 3, 8, 6), datetime(2026, 3, 9, 5), id="spring-forward-23h"),
    pytest.param("2026-11-01", datetime(2026, 11, 1, 5), datetime(2026, 11, 2, 6), id="fall-back-25h"),
]


@pytest.fixture
def export_part(db_session: Session) -> Part:
    part = Part(
        part_number="TZ-EXPORT-PART",
        name="Time zone export part",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(part)
    db_session.commit()
    return part


def _seed_work_orders(db: Session, part: Part, stamps: List[datetime]) -> List[str]:
    numbers = []
    for index, created_at in enumerate(stamps):
        number = f"WO-TZ-{index}-{created_at:%Y%m%d%H%M%S%f}"
        db.add(
            WorkOrder(
                work_order_number=number,
                part_id=part.id,
                quantity_ordered=1,
                status="draft",
                company_id=1,
                created_at=created_at,
            )
        )
        numbers.append(number)
    db.commit()
    return numbers


def _seed_transactions(db: Session, part: Part, user: User, stamps: List[datetime]) -> List[str]:
    references = []
    for index, created_at in enumerate(stamps):
        reference = f"TZ-REF-{index}-{created_at:%Y%m%d%H%M%S%f}"
        db.add(
            InventoryTransaction(
                company_id=1,
                part_id=part.id,
                transaction_type=TransactionType.ADJUST,
                quantity=1,
                reference_type="manual",
                reference_number=reference,
                created_by=user.id,
                created_at=created_at,
            )
        )
        references.append(reference)
    db.commit()
    return references


def _csv_rows(response) -> List[dict]:
    assert response.status_code == 200, response.text
    if not response.text:
        return []
    return list(csv.DictReader(io.StringIO(response.text)))


def _four_stamps(start: datetime, end: datetime) -> List[datetime]:
    one = timedelta(microseconds=1)
    return [start - one, start, end - one, end]


@pytest.mark.parametrize("day,start,end", BOUNDARY_DAYS)
def test_work_order_export_day_is_the_central_calendar_day(
    client, admin_headers, db_session, export_part, day, start, end
):
    numbers = _seed_work_orders(db_session, export_part, _four_stamps(start, end))

    rows = _csv_rows(client.get(WO_URL, headers=admin_headers, params={"start_date": day, "end_date": day}))

    assert sorted(row["work_order_number"] for row in rows) == sorted(numbers[1:3])


@pytest.mark.parametrize("day,start,end", BOUNDARY_DAYS)
def test_inventory_transaction_export_day_is_the_central_calendar_day(
    client, admin_headers, admin_user, db_session, export_part, day, start, end
):
    references = _seed_transactions(db_session, export_part, admin_user, _four_stamps(start, end))

    rows = _csv_rows(client.get(TXN_URL, headers=admin_headers, params={"start_date": day, "end_date": day}))

    assert sorted(row["reference_number"] for row in rows) == sorted(references[1:3])


def test_second_shift_row_is_included_and_previous_evening_excluded(client, admin_headers, db_session, export_part):
    """The user-visible regression, in Central wall-clock terms.

    7 PM CST on Jan 15 (01:00Z Jan 16) belongs to Jan 15; it used to be in Jan 16's file.
    9 PM CST on Jan 14 (03:00Z Jan 15) belongs to Jan 14; it used to be in Jan 15's file.
    """
    second_shift, previous_evening = _seed_work_orders(
        db_session, export_part, [datetime(2026, 1, 16, 1, 0), datetime(2026, 1, 15, 3, 0)]
    )

    jan15 = _csv_rows(
        client.get(WO_URL, headers=admin_headers, params={"start_date": "2026-01-15", "end_date": "2026-01-15"})
    )
    jan16 = _csv_rows(
        client.get(WO_URL, headers=admin_headers, params={"start_date": "2026-01-16", "end_date": "2026-01-16"})
    )

    assert [row["work_order_number"] for row in jan15] == [second_shift]
    assert previous_evening not in {row["work_order_number"] for row in jan16}
    assert jan16 == []


def test_open_ended_ranges_use_the_same_local_bounds(client, admin_headers, db_session, export_part):
    before, at_start, last_instant, after = _seed_work_orders(
        db_session, export_part, _four_stamps(datetime(2026, 1, 15, 6), datetime(2026, 1, 16, 6))
    )

    from_only = _csv_rows(client.get(WO_URL, headers=admin_headers, params={"start_date": "2026-01-15"}))
    to_only = _csv_rows(client.get(WO_URL, headers=admin_headers, params={"end_date": "2026-01-15"}))

    assert sorted(row["work_order_number"] for row in from_only) == sorted([at_start, last_instant, after])
    assert sorted(row["work_order_number"] for row in to_only) == sorted([before, at_start, last_instant])


def test_company_timezone_is_honored(client, admin_headers, db_session, export_part):
    db_session.get(Company, 1).timezone = "America/New_York"
    db_session.commit()
    before, at_start, last_instant, after = _seed_work_orders(
        db_session, export_part, _four_stamps(datetime(2026, 1, 15, 5), datetime(2026, 1, 16, 5))
    )

    response = client.get(WO_URL, headers=admin_headers, params={"start_date": "2026-01-15", "end_date": "2026-01-15"})
    rows = _csv_rows(response)

    assert sorted(row["work_order_number"] for row in rows) == sorted([at_start, last_instant])
    # Cells are rendered in the same zone the filter used.
    by_number = {row["work_order_number"]: row for row in rows}
    assert by_number[at_start]["created_at"] == "2026-01-15T00:00:00-05:00"


@pytest.mark.parametrize("bad_zone", [None, "", "Not/AZone"])
def test_unusable_company_timezone_falls_back_to_central(client, admin_headers, db_session, export_part, bad_zone):
    db_session.get(Company, 1).timezone = bad_zone
    db_session.commit()
    numbers = _seed_work_orders(
        db_session, export_part, _four_stamps(datetime(2026, 1, 15, 6), datetime(2026, 1, 16, 6))
    )

    rows = _csv_rows(
        client.get(WO_URL, headers=admin_headers, params={"start_date": "2026-01-15", "end_date": "2026-01-15"})
    )

    assert sorted(row["work_order_number"] for row in rows) == sorted(numbers[1:3])


@pytest.mark.parametrize("url", [WO_URL, TXN_URL])
def test_end_date_max_is_422_not_500(client, admin_headers, url):
    response = client.get(url, headers=admin_headers, params={"end_date": date.max.isoformat()})

    assert response.status_code == 422
    assert response.json()["detail"] == "End date is outside the supported range"


@pytest.mark.parametrize(
    "url",
    [
        WO_URL,
        TXN_URL,
        "/api/v1/exports/purchase-orders/export",
        "/api/v1/exports/purchase-orders/lines/export",
        "/api/v1/exports/quotes/export",
    ],
)
def test_reversed_range_is_422(client, admin_headers, url):
    response = client.get(url, headers=admin_headers, params={"start_date": "2026-01-16", "end_date": "2026-01-15"})

    assert response.status_code == 422
    assert response.json()["detail"] == "Start date must be on or before end date"


def test_csv_datetime_cells_are_local_with_offset_and_dates_stay_bare(client, admin_headers, db_session, export_part):
    winter = WorkOrder(
        work_order_number="WO-TZ-WINTER",
        part_id=export_part.id,
        quantity_ordered=1,
        status="draft",
        company_id=1,
        created_at=datetime(2026, 1, 15, 23, 30),
        actual_start=datetime(2026, 1, 15, 14, 0, 7, 123456),
        due_date=date(2026, 1, 20),
    )
    summer = WorkOrder(
        work_order_number="WO-TZ-SUMMER",
        part_id=export_part.id,
        quantity_ordered=1,
        status="draft",
        company_id=1,
        created_at=datetime(2026, 7, 15, 23, 30),
    )
    db_session.add_all([winter, summer])
    db_session.commit()

    rows = {row["work_order_number"]: row for row in _csv_rows(client.get(WO_URL, headers=admin_headers))}

    assert rows["WO-TZ-WINTER"]["created_at"] == "2026-01-15T17:30:00-06:00"
    assert rows["WO-TZ-WINTER"]["actual_start"] == "2026-01-15T08:00:07-06:00"
    assert rows["WO-TZ-WINTER"]["due_date"] == "2026-01-20"
    assert rows["WO-TZ-WINTER"]["actual_end"] == ""
    assert rows["WO-TZ-SUMMER"]["created_at"] == "2026-07-15T18:30:00-05:00"


def test_xlsx_datetime_cells_are_local_with_offset_text(client, admin_headers, db_session, export_part):
    db_session.add(
        WorkOrder(
            work_order_number="WO-TZ-XLSX",
            part_id=export_part.id,
            quantity_ordered=1,
            status="draft",
            company_id=1,
            created_at=datetime(2026, 1, 15, 23, 30),
            due_date=date(2026, 1, 20),
        )
    )
    db_session.commit()

    response = client.get(
        WO_URL,
        headers=admin_headers,
        params={"format": "xlsx", "columns": ["work_order_number", "due_date", "created_at"]},
    )
    assert response.status_code == 200, response.text
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    header = [cell.value for cell in sheet[1]]
    values = [cell.value for cell in sheet[2]]

    assert header == ["Work Order Number", "Due Date", "Created At"]
    assert values == ["WO-TZ-XLSX", "2026-01-20", "2026-01-15T17:30:00-06:00"]
    # Text, not an Excel date: a spreadsheet datetime cannot carry the offset.
    assert sheet.cell(row=2, column=3).data_type == "s"


def test_every_export_renders_its_datetime_columns_with_an_offset(
    client, admin_headers, admin_user, db_session, export_part
):
    """One row per dataset that has a datetime column; each cell must end in the Central offset."""
    created = datetime(2026, 1, 15, 23, 30)
    expected = "2026-01-15T17:30:00-06:00"
    export_part.created_at = created
    vendor = Vendor(name="TZ Vendor", code="TZV", company_id=1, is_active=True, is_approved=True)
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        PurchaseOrder(
            po_number="PO-TZ-1",
            vendor_id=vendor.id,
            status="draft",
            order_date=date(2026, 1, 15),
            company_id=1,
            created_at=created,
        )
    )
    db_session.add(
        Quote(
            quote_number="Q-TZ-1",
            customer_name="TZ Customer",
            quote_date=date(2026, 1, 15),
            company_id=1,
            created_at=created,
        )
    )
    db_session.add(
        InventoryTransaction(
            company_id=1,
            part_id=export_part.id,
            transaction_type=TransactionType.ADJUST,
            quantity=1,
            reference_number="TZ-EVERY",
            created_by=admin_user.id,
            created_at=created,
        )
    )
    # ``InventoryItem.received_date`` is a DateTime column despite its name.
    db_session.add(
        InventoryItem(
            company_id=1,
            part_id=export_part.id,
            location="TZ-BIN",
            quantity_on_hand=1,
            is_active=True,
            received_date=created,
        )
    )
    db_session.add(
        WorkOrder(
            work_order_number="WO-TZ-EVERY",
            part_id=export_part.id,
            quantity_ordered=1,
            status="draft",
            company_id=1,
            created_at=created,
        )
    )
    db_session.commit()

    for url, column in DATETIME_COLUMN_BY_EXPORT.items():
        rows = _csv_rows(client.get(url, headers=admin_headers))
        assert rows, url
        assert {row[column] for row in rows} == {expected}, url


# Every /exports/* route, and the default datetime column each one renders (None: the
# dataset has no datetime column -- PO lines carry only the ``required_date`` DATE).
DATETIME_COLUMN_BY_EXPORT = {
    WO_URL: "created_at",
    "/api/v1/exports/parts/export": "created_at",
    "/api/v1/exports/inventory/export": "received_date",
    "/api/v1/exports/purchase-orders/export": "created_at",
    "/api/v1/exports/quotes/export": "created_at",
    TXN_URL: "created_at",
}
# The FAI log of record (P2.5) is datetime-free too: its date columns (``log_date``,
# ``inspection_date``, ``completed_date``, ``customer_approval_date``) are calendar DATES and
# ``created_at`` is not an export column -- pinned by the schema check below.
EXPORTS_WITHOUT_A_DATETIME_COLUMN = {
    "/api/v1/exports/purchase-orders/lines/export",
    "/api/v1/exports/fai/export",
}


def test_every_export_route_is_checked_for_datetime_offsets():
    """A new /exports/* route must be added to the offset check above (or declared datetime-free)."""
    from app.main import app

    export_routes = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/exports/") and "GET" in getattr(route, "methods", set())
    }
    assert export_routes, "the walk found no export routes"
    assert export_routes == set(DATETIME_COLUMN_BY_EXPORT) | EXPORTS_WITHOUT_A_DATETIME_COLUMN


def test_the_fai_log_export_has_no_datetime_column():
    """Every column ``GET /exports/fai/export`` can write is a date, text, number or boolean on
    ``FAILogRow`` -- never a datetime, which would need the company-local offset above. A
    datetime column added to the export fails here until it is moved to DATETIME_COLUMN_BY_EXPORT."""
    import typing

    from app.api.endpoints.exports import FAI_LOG_EXTRA_COLUMNS, FAI_LOG_LEGACY_COLUMNS
    from app.schemas.quality import FAILogRow

    for column in FAI_LOG_LEGACY_COLUMNS + FAI_LOG_EXTRA_COLUMNS:
        annotation = FAILogRow.model_fields[column].annotation
        assert annotation is not datetime and datetime not in typing.get_args(annotation), column


def test_export_audit_still_records_the_requested_dates_not_utc_instants(client, admin_headers):
    response = client.get(WO_URL, headers=admin_headers, params={"start_date": "2026-01-15", "end_date": "2026-01-15"})
    assert response.status_code == 200, response.text

    filters = committed_export_rows("work_order")[-1].new_values["filters"]
    assert filters["start_date"] == "2026-01-15"
    assert filters["end_date"] == "2026-01-15"
