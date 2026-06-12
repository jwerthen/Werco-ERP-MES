import io
import time

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.api.endpoints.bom import _extract_excel_table
from app.models.bom import BOM, BOMItem
from app.models.part import Part
from app.services.import_service import MAX_CONSECUTIVE_BLANK_ROWS, MAX_IMPORT_COLUMNS, XLSX_MEDIA_TYPE, ImportFileError
from app.services.pdf_service import extract_text_from_excel


def _workbook_bytes(*sheets) -> bytes:
    """Build an in-memory xlsx; each positional arg is one sheet's list of rows."""
    workbook = Workbook()
    for sheet_index, rows in enumerate(sheets):
        sheet = workbook.active if sheet_index == 0 else workbook.create_sheet(f"Sheet{sheet_index + 1}")
        for row in rows:
            sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def _bloated_workbook_bytes() -> bytes:
    """Header + two data rows, plus the production failure mode: a single stray
    whitespace cell in the very last cell of the grid (XFD1048576) bloating the
    declared used range to 16,384 x 1,048,576."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Part Number", "Description", "Qty"])
    sheet.append(["P-1", "Bracket", 2])
    sheet.append(["P-2", "Spacer", 4])
    sheet.cell(row=1_048_576, column=16_384, value=" ")
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


@pytest.mark.api
@pytest.mark.requires_db
class TestBOMImport:
    def test_commit_bom_import_creates_assembly_and_items(
        self,
        client: TestClient,
        auth_headers: dict,
        db_session: Session,
    ):
        response = client.post(
            "/api/v1/bom/import/commit",
            headers=auth_headers,
            json={
                "document_type": "bom",
                "assembly": {
                    "part_number": "ASSY-100",
                    "name": "Imported Assembly",
                    "revision": "A",
                    "part_type": "manufactured",
                },
                "items": [
                    {
                        "line_number": 10,
                        "part_number": "COMP-100",
                        "description": "Machined bracket",
                        "quantity": 2,
                        "item_type": "make",
                        "line_type": "component",
                    },
                    {
                        "line_number": 20,
                        "part_number": "BUY-100",
                        "description": "Purchased spacer",
                        "quantity": 4,
                        "item_type": "buy",
                        "line_type": "component",
                    },
                ],
                "create_missing_parts": True,
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["bom_id"] is not None
        assert data["created_bom_items"] == 2

        assembly = db_session.query(Part).filter(Part.part_number == "ASSY-100").one()
        assert assembly.part_type == "assembly"

        make_component = db_session.query(Part).filter(Part.part_number == "COMP-100").one()
        buy_component = db_session.query(Part).filter(Part.part_number == "BUY-100").one()
        assert make_component.part_type == "manufactured"
        assert buy_component.part_type == "purchased"

        bom = db_session.query(BOM).filter(BOM.id == data["bom_id"]).one()
        items = db_session.query(BOMItem).filter(BOMItem.bom_id == bom.id).all()
        assert bom.part_id == assembly.id
        assert {item.component_part_id for item in items} == {make_component.id, buy_component.id}

        bom_response = client.get(f"/api/v1/bom/by-part/{assembly.id}", headers=auth_headers)
        assert bom_response.status_code == status.HTTP_200_OK

        bom_data = bom_response.json()
        assert bom_data["id"] == bom.id
        assert bom_data["part_id"] == assembly.id
        assert {item["component_part_id"] for item in bom_data["items"]} == {
            make_component.id,
            buy_component.id,
        }


@pytest.mark.api
@pytest.mark.requires_db
class TestBOMImportPreviewBoundedScan:
    """The Excel preview path must never iterate a workbook's full *declared*
    grid: one stray whitespace cell at XFD1048576 used to make a 5 KB upload
    scan ~17B cells (~5 minutes of CPU on the event loop in prod)."""

    def test_bloated_used_range_previews_fast_and_succeeds(
        self,
        client: TestClient,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # _extract_excel_table has no kwarg injection, so tighten the
        # scanned-row backstop it reads from module globals to make the
        # cutoff check deterministic: with the per-sheet blank-run cutoff
        # working, the scan stops after ~1k blank rows; if the cutoff ever
        # regresses to unbounded blank scanning, the backstop raises
        # ImportFileError (HTTP 400 here) regardless of runner speed.
        monkeypatch.setattr("app.api.endpoints.bom.MAX_SCANNED_ROWS", 60_000)

        started = time.monotonic()
        response = client.post(
            "/api/v1/bom/import/preview",
            headers=auth_headers,
            files={"file": ("bloated.xlsx", io.BytesIO(_bloated_workbook_bytes()), XLSX_MEDIA_TYPE)},
        )
        elapsed = time.monotonic() - started

        assert response.status_code == status.HTTP_200_OK, response.text
        data = response.json()
        assert data["source_format"] == "excel"
        assert [item["part_number"] for item in data["items"]] == ["P-1", "P-2"]
        assert [item["quantity"] for item in data["items"]] == [2.0, 4.0]
        # Wall clock is only a loose backstop — coverage-traced CI runners are
        # ~60x slower than local; a full-grid regression takes many minutes.
        assert elapsed < 90, f"bloated-dimension preview took {elapsed:.1f}s — grid scan regression"


@pytest.mark.unit
class TestExtractExcelTable:
    def test_multi_sheet_collection(self, tmp_path):
        """Header comes from the first non-empty row anywhere; later non-empty
        rows across ALL sheets are data rows (original semantics preserved)."""
        path = tmp_path / "multi.xlsx"
        path.write_bytes(
            _workbook_bytes(
                [["Part Number", "Description", "Qty"], ["P-1", "Bracket", "2"]],
                [["P-2", "Spacer", "4"], ["P-3", "Shim", "1"]],
            )
        )

        columns, rows = _extract_excel_table(str(path), ".xlsx")

        assert columns == ["Part Number", "Description", "Qty"]
        assert rows == [["P-1", "Bracket", "2"], ["P-2", "Spacer", "4"], ["P-3", "Shim", "1"]]

    def test_wider_than_cap_ignores_extra_columns(self, tmp_path):
        header = ["Part Number"] + [f"extra_{i}" for i in range(MAX_IMPORT_COLUMNS + 10)]
        data = ["P-1"] + ["x"] * (MAX_IMPORT_COLUMNS + 10)
        path = tmp_path / "wide.xlsx"
        path.write_bytes(_workbook_bytes([header, data]))

        columns, rows = _extract_excel_table(str(path), ".xlsx")

        assert len(columns) == MAX_IMPORT_COLUMNS
        assert columns[0] == "Part Number"
        assert len(rows) == 1
        assert len(rows[0]) == MAX_IMPORT_COLUMNS
        assert rows[0][0] == "P-1"

    def test_blank_run_cutoff_is_per_sheet(self, tmp_path):
        """A gap longer than MAX_CONSECUTIVE_BLANK_ROWS ends only THAT sheet's
        scan (treated as used-range bloat); later sheets still contribute rows.
        There is deliberately no loud-refusal look-ahead here — the preview
        shows users exactly which rows parsed before anything is committed."""
        workbook = Workbook()
        first = workbook.active
        first.append(["Part Number", "Qty"])
        first.append(["P-1", "1"])
        first.cell(row=MAX_CONSECUTIVE_BLANK_ROWS + 100, column=1, value="P-DROPPED")
        second = workbook.create_sheet("Second")
        second.append(["P-2", "3"])
        out = io.BytesIO()
        workbook.save(out)
        path = tmp_path / "gap.xlsx"
        path.write_bytes(out.getvalue())

        columns, rows = _extract_excel_table(str(path), ".xlsx")

        assert columns == ["Part Number", "Qty"]
        assert ["P-1", "1"] in rows
        assert ["P-2", "3"] in rows
        assert not any("P-DROPPED" in row for row in rows)

    def test_corrupt_file_raises_import_file_error(self, tmp_path):
        path = tmp_path / "corrupt.xlsx"
        path.write_bytes(b"this is not a spreadsheet")

        with pytest.raises(ImportFileError, match="Could not read the Excel file"):
            _extract_excel_table(str(path), ".xlsx")

    def test_header_padded_to_widest_data_row(self, tmp_path):
        """A data column with no header cell must still surface in the mapping
        UI (rendered as "Col N"), so the header is padded to the widest row."""
        path = tmp_path / "unheadered.xlsx"
        path.write_bytes(
            _workbook_bytes(
                [["Part Number", "Qty"], ["P-1", "2", "vendor-note"]],
            )
        )

        columns, rows = _extract_excel_table(str(path), ".xlsx")

        assert columns == ["Part Number", "Qty", ""]
        assert rows == [["P-1", "2", "vendor-note"]]


@pytest.mark.unit
class TestExtractTextFromExcelBounded:
    def test_bloated_used_range_extracts_quickly(self, tmp_path):
        path = tmp_path / "bloated.xlsx"
        path.write_bytes(_bloated_workbook_bytes())

        started = time.monotonic()
        result = extract_text_from_excel(str(path))
        elapsed = time.monotonic() - started

        assert "P-1" in result.text
        assert "P-2" in result.text
        assert result.confidence == "medium"
        # Loose backstop only (see TestBOMImportPreviewBoundedScan); a
        # full-grid regression takes many minutes, not seconds.
        assert elapsed < 90, f"bloated-dimension text extraction took {elapsed:.1f}s — grid scan regression"

    def test_scan_cap_returns_partial_text_instead_of_failing(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        """Hitting the workbook-wide scanned-row cap must degrade gracefully:
        stop scanning and return what was extracted so far at medium
        confidence — never raise (text extraction feeds best-effort flows)."""
        monkeypatch.setattr("app.services.pdf_service.MAX_SCANNED_ROWS", 3)
        path = tmp_path / "rows.xlsx"
        path.write_bytes(_workbook_bytes([["h1"], ["r1"], ["r2"], ["r3"], ["r4"]]))

        result = extract_text_from_excel(str(path))

        assert result.confidence == "medium"
        assert "r2" in result.text  # rows inside the cap survive
        assert "r3" not in result.text  # rows past the cap are dropped, not an error
        assert "r4" not in result.text
