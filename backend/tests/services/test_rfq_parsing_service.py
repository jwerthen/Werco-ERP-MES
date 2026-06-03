from pathlib import Path
from types import SimpleNamespace

import ezdxf
from openpyxl import Workbook

from app.services import rfq_parsing_service as rfq_parser
from app.services.rfq_parsing_service import build_normalized_part_specs, parse_bom_xlsx, parse_dxf_geometry


def test_parse_bom_xlsx_extracts_parts_and_hardware(tmp_path: Path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "BOM"
    sheet.append(
        [
            "Part Number",
            "Description",
            "Qty",
            "Material",
            "Thickness",
            "Finish",
            "Type",
            "Flat Length",
            "Flat Width",
        ]
    )
    sheet.append(["P-100", "Main Bracket", 4, "A36 Steel", "0.125", "Powder Coat", "Part", 12, 8])
    sheet.append(["HW-200", "PEM Nut 1/4-20", 8, "", "", "", "Hardware"])

    file_path = tmp_path / "sample_bom.xlsx"
    workbook.save(file_path)

    result = parse_bom_xlsx(str(file_path), "sample_bom.xlsx")
    assert len(result["parts"]) == 1
    assert len(result["hardware"]) == 1
    assert result["parts"][0]["part_number"] == "P-100"
    assert result["parts"][0]["qty"] == 4
    assert result["parts"][0]["material"] == "A36 Steel"
    assert result["parts"][0]["flat_area"] == 96
    assert result["parts"][0]["cut_length"] == 40
    assert "sample_bom.xlsx!BOM:row2" in result["parts"][0]["source"]


def test_parse_dxf_geometry_extracts_area_perimeter_and_features(tmp_path: Path):
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 2), (0, 2)], close=True)
    msp.add_circle((1, 1), 0.25)
    msp.add_line((0.5, 0.5), (3.5, 0.5), dxfattribs={"layer": "BEND"})

    file_path = tmp_path / "part_flat.dxf"
    doc.saveas(file_path)

    result = parse_dxf_geometry(str(file_path), "part_flat.dxf")
    assert result["flat_area"] is not None
    assert abs(result["flat_area"] - 8.0) < 0.25
    assert result["cut_length"] is not None
    assert result["cut_length"] > 12.0  # perimeter + hole cut
    assert result["hole_count"] == 1
    assert result["bend_count"] >= 1


def test_parse_pdf_drawing_extracts_drawing_details(monkeypatch):
    monkeypatch.setattr(
        rfq_parser,
        "extract_text_from_pdf",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=(
                "PART NO P-900 DWG NO D-900 REV B MATERIAL 5052 ALUMINUM "
                "THICKNESS .125 in POWDER COAT 4X Ø.250 HOLES 3 BEND LINES "
                "+/- .005 WELD"
            )
        ),
    )

    result = rfq_parser.parse_pdf_drawing("/tmp/P-900.pdf", "P-900.pdf")

    assert result["part_hint"] == "P-900"
    assert result["drawing_number"] == "D-900"
    assert result["revision"] == "B"
    assert result["material"] == "Aluminum"
    assert result["thickness_in"] == 0.125
    assert result["finish"].lower() == "powder coat"
    assert result["hole_count"] == 4
    assert result["bend_count"] == 3
    assert result["weld_required"] is True
    assert result["tolerances_flag"] is True


def test_build_normalized_part_specs_cross_references_pdf_and_dxf():
    pdf_specs = [
        {
            "file_name": "P-500 Rev B.pdf",
            "source_type": "pdf",
            "part_hint": "P-500",
            "drawing_number": "D-500",
            "revision": "B",
            "material": "Aluminum",
            "thickness": "0.125",
            "thickness_in": 0.125,
            "flat_area": None,
            "cut_length": None,
            "hole_count": 6,
            "bend_count": 4,
            "finish": "Powder Coat",
            "weld_required": True,
            "assembly_required": False,
            "tolerances_flag": True,
            "confidence": {"material": 0.8, "thickness": 0.8, "finish": 0.75, "geometry": 0.0},
            "sources": {
                "material": ["P-500 Rev B.pdf:text"],
                "thickness": ["P-500 Rev B.pdf:text"],
                "finish": ["P-500 Rev B.pdf:text"],
                "drawing_detail": ["P-500 Rev B.pdf:text:drawing_number=D-500;revision=B;holes=6;bends=4"],
            },
        }
    ]
    dxf_specs = [
        {
            "file_name": "P-500_flat.dxf",
            "source_type": "dxf",
            "part_hint": "P-500_flat",
            "flat_area": 18.0,
            "cut_length": 36.0,
            "hole_count": 8,
            "bend_count": 4,
            "confidence": {"geometry": 0.9},
            "sources": {"geometry": ["P-500_flat.dxf:modelspace"]},
        }
    ]

    result = build_normalized_part_specs([], pdf_specs, dxf_specs, [])

    assert len(result["parts"]) == 1
    part = result["parts"][0]
    assert part["part_id"] == "P-500"
    assert part["material"] == "Aluminum"
    assert part["thickness_in"] == 0.125
    assert part["flat_area"] == 18.0
    assert part["cut_length"] == 36.0
    assert part["hole_count"] == 8
    assert part["bend_count"] == 4
    assert part["drawing_number"] == "D-500"
    assert part["revision"] == "B"
    assert "drawing_pdf" in part["sources"]
    assert "flat_pattern_dxf" in part["sources"]
    assert any(item["field"] == "cross_reference" for item in result["assumptions"])


def test_parse_pdf_drawing_extracts_assembly_bom(monkeypatch):
    text = """
PREP
RETAINER, CAPACITOR
TITLE
REVISION HISTORY
A CO-001 2026-01-01 JW
NOTES:
1. MATERIAL: PART 1: ITEM 1, .090 STOCK.
PARTS 2 AND 3: ITEM 1, .125 STOCK.
PART 4: COPPER, ELECTROLYTIC TOUGH PITCH TEMPER
SOFT-ANNEALED .0647 STOCK, IN ACCORDANCE WITH QQ-A-250/8.
2. FINISH: PARTS 1, 2 AND 3: ITEM 2.
PART 4: TIN PLATE, .00015 MIN.
9. CALCULATED WEIGHT: (0.73 LBS).
QTY ITEM
NO
PART OR IDENTIFYING NUMBER NOMENCLATURE OR DESCRIPTION DOCUMENT NO CAGEC NOTES REF DESIGNATOR
PARTS LIST
6 7 MS20426AD6-8 RIVET (CSK. .187 DIA X .500) 5
4 6 MS20426AD4-6 RIVET (CSK. .125 DIA X .375) 5
REF 2 580-0036-002 PROCESS, CHEMICAL FILM - YELLOW 2
AR 1 820-5052-010 ALUMINUM, 5052-H32 1
PART 1 PART 4
QUANTITY: 2
PART 2
PART 3
"""
    monkeypatch.setattr(rfq_parser, "extract_text_from_pdf", lambda *_args, **_kwargs: SimpleNamespace(text=text))

    pdf = rfq_parser.parse_pdf_drawing("/tmp/818-3928-638_RevA.pdf", "818-3928-638_RevA.pdf")
    result = build_normalized_part_specs([], [pdf], [], [])

    assert pdf["document_kind"] == "assembly"
    assert pdf["assembly"]["part_number"] == "818-3928-638"
    assert pdf["assembly"]["part_name"] == "RETAINER, CAPACITOR"
    assert len(pdf["assembly"]["bom_items"]) == 4
    parts = result["parts"]
    assert any(part["line_type"] == "assembly" and part["part_id"] == "818-3928-638" for part in parts)
    manufactured = [part for part in parts if part["line_type"] == "manufactured"]
    assert len(manufactured) == 4
    part4 = next(part for part in manufactured if part["part_id"].endswith("PART-4"))
    assert part4["material"] == "Copper"
    assert part4["thickness_in"] == 0.0647
    assert part4["quantity_per_assembly"] == 2.0
    assert len([part for part in parts if part["line_type"] == "hardware"]) == 2
