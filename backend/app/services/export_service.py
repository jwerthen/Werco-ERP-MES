"""Data export service for CSV and Excel generation."""

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.time_utils import CENTRAL_TIME_ZONE, to_local_iso
from app.services.export_safety import sanitize_csv_row, write_cell


def format_value(value: Any, zone: ZoneInfo = CENTRAL_TIME_ZONE) -> Any:
    """Format a value for export.

    A ``datetime`` becomes ISO-8601 in ``zone``'s wall time WITH its UTC offset, e.g.
    ``2026-01-15T17:30:00-06:00`` (a naive value is read as UTC, which is how the schema
    stores it). Callers pass the company's zone, so a row reads on the same local day
    the export's ``start_date`` / ``end_date`` filter selected it by. A spreadsheet
    datetime cannot carry an offset, so XLSX cells stay text. The check runs before the
    ``date`` branch because ``datetime`` is a ``date`` subclass. A ``date`` stays
    ``YYYY-MM-DD``.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return to_local_iso(value, zone)
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, 'value'):
        return value.value
    return value


def generate_csv(
    data: List[Dict[str, Any]], columns: Optional[List[str]] = None, *, zone: ZoneInfo = CENTRAL_TIME_ZONE
) -> str:
    """Generate CSV string from list of dictionaries (datetimes in ``zone``, with offset)."""
    if not data:
        return ""

    if columns:
        headers = columns
    else:
        headers = list(data[0].keys())

    output = io.StringIO()
    writer = csv.writer(output)
    # Headers are sanitized too: `columns` is caller-supplied (a request query
    # param on every /export endpoint), so the header row carries tenant input.
    writer.writerow(sanitize_csv_row(headers))

    for row in data:
        writer.writerow(sanitize_csv_row(format_value(row.get(col), zone) for col in headers))

    return output.getvalue()


def generate_excel(
    data: List[Dict[str, Any]],
    columns: Optional[List[str]] = None,
    sheet_name: str = "Export",
    *,
    zone: ZoneInfo = CENTRAL_TIME_ZONE,
) -> bytes:
    """Generate Excel file from list of dictionaries (datetimes in ``zone``, with offset)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name

    if not data:
        wb_output = io.BytesIO()
        wb.save(wb_output)
        return wb_output.getvalue()

    if columns:
        headers = columns
    else:
        headers = list(data[0].keys())

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin')
    )

    # write_cell (not ws.cell) everywhere below: it pins string cells to
    # data_type "s" so tenant text can never be written as an <f> formula
    # element. Non-destructive -- values are byte-identical, numerics stay
    # numeric. See app/services/export_safety.py.
    for col_idx, header in enumerate(headers, 1):
        cell = write_cell(ws, 1, col_idx, header.replace("_", " ").title())
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    for row_idx, row_data in enumerate(data, 2):
        for col_idx, col in enumerate(headers, 1):
            value = format_value(row_data.get(col), zone)
            cell = write_cell(ws, row_idx, col_idx, value)
            cell.border = thin_border
            if isinstance(value, (int, float)):
                cell.alignment = Alignment(horizontal="right")

    for col_idx, header in enumerate(headers, 1):
        max_length = len(header)
        for row_idx in range(2, len(data) + 2):
            cell_value = ws.cell(row=row_idx, column=col_idx).value
            if cell_value:
                max_length = max(max_length, len(str(cell_value)))
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[get_column_letter(col_idx)].width = adjusted_width

    ws.freeze_panes = "A2"

    wb_output = io.BytesIO()
    wb.save(wb_output)
    return wb_output.getvalue()
