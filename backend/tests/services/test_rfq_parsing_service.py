from pathlib import Path

import ezdxf
from openpyxl import Workbook

from app.services.rfq_parsing_service import parse_bom_xlsx, parse_dxf_geometry


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
