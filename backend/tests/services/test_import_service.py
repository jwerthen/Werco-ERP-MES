"""Unit tests for the shared Excel/CSV import kit (A0.2).

Covers the parsing edge cases the migration depends on: header normalization,
blank-row tolerance, ``#`` guidance-row skipping, defensive cell coercion
(Excel floats/dates/bools, numbers-as-text), size/row caps, and the invariant
that every server-generated template round-trips through the parser.
"""

import io
from datetime import date, datetime

import pytest
from openpyxl import Workbook, load_workbook

from app.services.import_service import (
    IMPORT_TEMPLATES,
    ImportFileError,
    build_import_template_workbook,
    coerce_cell,
    list_import_templates,
    normalize_import_header,
    parse_date_field,
    parse_import_file,
)


def _xlsx_bytes(rows, sheet_rows_extra=None):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    if sheet_rows_extra:
        extra = workbook.create_sheet("Second")
        for row in sheet_rows_extra:
            extra.append(row)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


@pytest.mark.unit
class TestHeaderNormalization:
    def test_strip_lower_snake_case(self):
        assert normalize_import_header(" Part Number ") == "part_number"
        assert normalize_import_header("PART-NUMBER") == "part_number"
        assert normalize_import_header("Lead Time (Days)") == "lead_time_days"
        assert normalize_import_header("customer/po") == "customer_po"

    def test_non_string_headers(self):
        assert normalize_import_header(42) == "42"
        assert normalize_import_header(None) == ""


@pytest.mark.unit
class TestCellCoercion:
    def test_integral_float_becomes_int_string(self):
        assert coerce_cell(5.0) == "5"
        assert coerce_cell(10.0) == "10"

    def test_non_integral_float_keeps_decimals(self):
        assert coerce_cell(2.5) == "2.5"

    def test_bool_and_none(self):
        assert coerce_cell(True) == "true"
        assert coerce_cell(False) == "false"
        assert coerce_cell(None) == ""

    def test_dates(self):
        assert coerce_cell(datetime(2026, 7, 15)) == "2026-07-15"  # date-only Excel cell
        assert coerce_cell(datetime(2026, 7, 15, 7, 30)) == "2026-07-15 07:30:00"
        assert coerce_cell(date(2026, 7, 15)) == "2026-07-15"

    def test_text_is_stripped(self):
        assert coerce_cell("  42 ") == "42"


@pytest.mark.unit
class TestParseDateField:
    def test_accepted_formats(self):
        assert parse_date_field("2026-07-15", "due_date") == date(2026, 7, 15)
        assert parse_date_field("07/15/2026", "due_date") == date(2026, 7, 15)
        assert parse_date_field("2026-07-15 07:30:00", "due_date") == date(2026, 7, 15)
        assert parse_date_field("", "due_date") is None

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="due_date"):
            parse_date_field("July fifteen", "due_date")


@pytest.mark.unit
class TestParseImportFileXlsx:
    def test_happy_path_with_blank_and_guidance_rows(self):
        content = _xlsx_bytes(
            [
                [None, None, None],  # leading blank row tolerated
                ["Part Number", "Name", "Lead Time Days"],
                ["# REQUIRED. Unique part number.", "# REQUIRED.", "# Optional."],
                ["P-100", "Bracket", 10.0],
                [None, None, None],  # blank row mid-file tolerated
                ["P-200", "Plate", " 12 "],
            ]
        )
        table = parse_import_file("upload.xlsx", content, required_columns={"part_number", "name"})
        assert table.headers == ["part_number", "name", "lead_time_days"]
        rows = list(table.iter_rows())
        assert len(rows) == 2
        first_row_number, first = rows[0]
        assert first == {"part_number": "P-100", "name": "Bracket", "lead_time_days": "10"}
        assert first_row_number == 4  # real file row number for error reporting
        _, second = rows[1]
        assert second["lead_time_days"] == "12"

    def test_only_first_sheet_is_read(self):
        content = _xlsx_bytes(
            [["part_number", "name"], ["P-1", "One"]],
            sheet_rows_extra=[["part_number", "name"], ["P-EXAMPLE", "Should not import"]],
        )
        table = parse_import_file("upload.xlsx", content)
        assert [row["part_number"] for _, row in table.iter_rows()] == ["P-1"]

    def test_trailing_unnamed_columns_ignored(self):
        content = _xlsx_bytes([["part_number", "name", None], ["P-1", "One", "stray"]])
        table = parse_import_file("upload.xlsx", content)
        _, row = next(table.iter_rows())
        assert row == {"part_number": "P-1", "name": "One"}

    def test_typed_cells_coerced(self):
        content = _xlsx_bytes(
            [
                ["part_number", "quantity", "due_date", "is_critical"],
                ["P-1", 25.0, datetime(2026, 7, 15), True],
            ]
        )
        _, row = next(parse_import_file("u.xlsx", content).iter_rows())
        assert row == {"part_number": "P-1", "quantity": "25", "due_date": "2026-07-15", "is_critical": "true"}

    def test_corrupt_xlsx_rejected(self):
        with pytest.raises(ImportFileError, match="xlsx"):
            parse_import_file("upload.xlsx", b"this is not a zip archive")


@pytest.mark.unit
class TestParseImportFileCsv:
    def test_utf8_sig_and_blank_rows(self):
        content = ("﻿" + "Part Number,Name\n\nP-100,Bracket\n# guidance,skipped\n").encode("utf-8")
        table = parse_import_file("upload.csv", content)
        rows = list(table.iter_rows())
        assert len(rows) == 1
        assert rows[0][1] == {"part_number": "P-100", "name": "Bracket"}

    def test_non_utf8_rejected(self):
        with pytest.raises(ImportFileError, match="UTF-8"):
            parse_import_file("upload.csv", "pärt".encode("latin-1"))


@pytest.mark.unit
class TestParseImportFileGuards:
    def test_unsupported_extension(self):
        with pytest.raises(ImportFileError, match="CSV or Excel"):
            parse_import_file("upload.xls", b"x")
        with pytest.raises(ImportFileError, match="CSV or Excel"):
            parse_import_file(None, b"x")

    def test_empty_file(self):
        with pytest.raises(ImportFileError, match="empty"):
            parse_import_file("upload.csv", b"")

    def test_size_cap(self):
        with pytest.raises(ImportFileError, match="too large"):
            parse_import_file("upload.csv", b"a,b\n1,2\n", max_bytes=4)

    def test_row_cap(self):
        content = b"a,b\n" + b"1,2\n" * 5
        with pytest.raises(ImportFileError, match="Too many rows"):
            parse_import_file("upload.csv", content, max_rows=3)

    def test_missing_required_columns(self):
        with pytest.raises(ImportFileError, match="part_number"):
            parse_import_file("upload.csv", b"name\nBracket\n", required_columns={"part_number", "name"})

    def test_header_only_blank_file(self):
        with pytest.raises(ImportFileError, match="header"):
            parse_import_file("upload.csv", b"\n\n\n")


@pytest.mark.unit
class TestImportTemplates:
    def test_listing_covers_all_entities(self):
        listed = {item["entity"] for item in list_import_templates()}
        assert listed == set(IMPORT_TEMPLATES)
        assert "work-orders" in listed and "purchase-orders" in listed

    @pytest.mark.parametrize("entity", sorted(IMPORT_TEMPLATES))
    def test_every_template_round_trips_through_the_parser(self, entity):
        """The template's own header + guidance rows must parse to zero data rows."""
        content = build_import_template_workbook(entity)
        table = parse_import_file(f"{entity}.xlsx", content)
        assert table.headers == [column.name for column in IMPORT_TEMPLATES[entity].columns]
        assert len(list(table.iter_rows())) == 0  # guidance row skipped, examples on separate sheet

    def test_template_structure(self):
        workbook = load_workbook(io.BytesIO(build_import_template_workbook("work-orders")))
        assert workbook.sheetnames == ["Import", "Examples"]
        sheet = workbook["Import"]
        assert sheet.cell(row=1, column=1).value == "wo_number"
        assert str(sheet.cell(row=2, column=1).value).startswith("#")
        examples = workbook["Examples"]
        assert examples.cell(row=1, column=2).value == "part_number"
        assert examples.cell(row=2, column=2).value  # at least one example row

    def test_unknown_entity_raises(self):
        with pytest.raises(KeyError):
            build_import_template_workbook("not-an-entity")
