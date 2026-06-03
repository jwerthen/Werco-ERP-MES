from io import BytesIO
from pathlib import Path

import ezdxf
from openpyxl import Workbook


def _make_bom_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "BOM"
    sheet.append(["Part Number", "Description", "Qty", "Material", "Thickness", "Finish", "Type"])
    sheet.append(["P-500", "Demo Bracket", 3, "A36 Steel", "0.125", "Powder Coat", "Part"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _make_dxf_bytes(tmp_path: Path) -> bytes:
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (6, 0), (6, 3), (0, 3)], close=True)
    msp.add_circle((1.5, 1.5), 0.2)
    msp.add_line((0.5, 0.5), (5.5, 0.5), dxfattribs={"layer": "BEND"})
    file_path = tmp_path / "demo_flat.dxf"
    doc.saveas(file_path)
    return file_path.read_bytes()


def _make_pdf_bytes() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    pdf.drawString(72, 720, "PART NO P-500")
    pdf.drawString(72, 700, "DWG NO D-500 REV B")
    pdf.drawString(72, 680, "MATERIAL A36 STEEL THICKNESS .125 in POWDER COAT")
    pdf.drawString(72, 660, "1X .400 HOLES 1 BEND LINES +/- .005 WELD")
    pdf.drawString(72, 640, "This drawing is machine-readable for RFQ quoting cross reference.")
    pdf.save()
    output.seek(0)
    return output.getvalue()


def _make_assembly_pdf_bytes() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    y = 740
    for line in [
        "PREP",
        "RETAINER, CAPACITOR",
        "TITLE",
        "REVISION HISTORY",
        "A CO-001 2026-01-01 JW",
        "NOTES:",
        "1. MATERIAL: PART 1: ITEM 1, .090 STOCK.",
        "PARTS 2 AND 3: ITEM 1, .125 STOCK.",
        "PART 4: COPPER, ELECTROLYTIC TOUGH PITCH TEMPER SOFT-ANNEALED .0647 STOCK.",
        "2. FINISH: PARTS 1, 2 AND 3: ITEM 2.",
        "PART 4: TIN PLATE, .00015 MIN.",
        "9. CALCULATED WEIGHT: (0.73 LBS).",
        "QTY ITEM NO PART OR IDENTIFYING NUMBER NOMENCLATURE OR DESCRIPTION DOCUMENT NO CAGEC NOTES REF DESIGNATOR",
        "PARTS LIST",
        "6 7 MS20426AD6-8 RIVET (CSK. .187 DIA X .500) 5",
        "4 6 MS20426AD4-6 RIVET (CSK. .125 DIA X .375) 5",
        "REF 2 580-0036-002 PROCESS, CHEMICAL FILM - YELLOW 2",
        "AR 1 820-5052-010 ALUMINUM, 5052-H32 1",
        "PART 1 PART 4",
        "QUANTITY: 2",
        "PART 2",
        "PART 3",
    ]:
        pdf.drawString(72, y, line)
        y -= 18
    pdf.save()
    output.seek(0)
    return output.getvalue()


def test_rfq_package_generate_estimate_flow(client, auth_headers, tmp_path):
    bom_bytes = _make_bom_bytes()
    pdf_bytes = _make_pdf_bytes()
    dxf_bytes = _make_dxf_bytes(tmp_path)

    create_response = client.post(
        "/api/v1/rfq-packages/",
        headers=auth_headers,
        data={"customer_name": "Acme Aerospace", "rfq_reference": "RFQ-DEMO-100"},
        files=[
            (
                "files",
                ("demo_bom.xlsx", bom_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ),
            ("files", ("P-500 drawing.pdf", pdf_bytes, "application/pdf")),
            ("files", ("P-500.dxf", dxf_bytes, "application/dxf")),
        ],
    )
    assert create_response.status_code == 200, create_response.text
    package_data = create_response.json()
    assert package_data["rfq_number"].startswith("RFQ-")
    package_id = package_data["id"]

    estimate_response = client.post(
        f"/api/v1/rfq-packages/{package_id}/generate-estimate",
        headers=auth_headers,
        json={"target_margin_pct": 20, "valid_days": 30},
    )
    assert estimate_response.status_code == 200, estimate_response.text
    estimate_data = estimate_response.json()
    assert estimate_data["quote_id"] > 0
    assert estimate_data["quote_number"].startswith("QTE-")
    assert len(estimate_data["line_summaries"]) >= 1
    assert estimate_data["totals"]["grand_total"] > 0
    line_sources = estimate_data["line_summaries"][0]["sources"]
    assert "drawing_pdf" in line_sources
    assert "flat_pattern_dxf" in line_sources
    assert any(item["field"] == "cross_reference" for item in estimate_data["assumptions"])

    package_response = client.get(f"/api/v1/rfq-packages/{package_id}", headers=auth_headers)
    assert package_response.status_code == 200, package_response.text
    parsed_package = package_response.json()
    pdf_summary = next(file["summary"] for file in parsed_package["files"] if file["extension"] == ".pdf")
    assert pdf_summary["drawing_number"] == "D-500"
    assert pdf_summary["revision"] == "B"

    export_response = client.get(
        f"/api/v1/rfq-packages/{package_id}/internal-estimate-export",
        headers=auth_headers,
    )
    assert export_response.status_code == 200
    assert export_response.headers["content-type"].startswith("application/json")


def test_rfq_package_quotes_assembly_bom_from_pdf_and_child_dxf(client, auth_headers, tmp_path):
    pdf_bytes = _make_assembly_pdf_bytes()
    dxf_bytes = _make_dxf_bytes(tmp_path)

    create_response = client.post(
        "/api/v1/rfq-packages/",
        headers=auth_headers,
        data={"customer_name": "Rockwell Collins", "rfq_reference": "ASM-818"},
        files=[
            ("files", ("818-3928-638_RevA.pdf", pdf_bytes, "application/pdf")),
            ("files", ("818-3928-638-PART-2.dxf", dxf_bytes, "application/dxf")),
        ],
    )
    assert create_response.status_code == 200, create_response.text
    package_id = create_response.json()["id"]

    estimate_response = client.post(
        f"/api/v1/rfq-packages/{package_id}/generate-estimate",
        headers=auth_headers,
        json={"target_margin_pct": 20, "valid_days": 30},
    )
    assert estimate_response.status_code == 200, estimate_response.text
    estimate_data = estimate_response.json()
    line_types = {line["line_type"] for line in estimate_data["line_summaries"]}
    assert {"assembly", "manufactured", "hardware"}.issubset(line_types)
    assembly_line = next(line for line in estimate_data["line_summaries"] if line["line_type"] == "assembly")
    assert assembly_line["part_number"] == "818-3928-638"
    child_line = next(line for line in estimate_data["line_summaries"] if line["part_number"] == "818-3928-638-PART-2")
    assert child_line["parent_part_number"] == "818-3928-638"
    assert child_line["quantity_per_assembly"] == 1.0
    assert child_line["flat_area"] is not None
    assert any(item["field"] == "cross_reference" for item in estimate_data["assumptions"])


def test_rfq_generate_estimate_requires_geometry(client, auth_headers):
    bom_bytes = _make_bom_bytes()
    create_response = client.post(
        "/api/v1/rfq-packages/",
        headers=auth_headers,
        data={"customer_name": "No Geometry Test", "rfq_reference": "RFQ-NOGEO-1"},
        files=[
            (
                "files",
                ("demo_bom.xlsx", bom_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ),
        ],
    )
    assert create_response.status_code == 200, create_response.text
    package_id = create_response.json()["id"]

    estimate_response = client.post(
        f"/api/v1/rfq-packages/{package_id}/generate-estimate",
        headers=auth_headers,
        json={"target_margin_pct": 20, "valid_days": 30},
    )
    assert estimate_response.status_code == 400
    assert "geometry" in estimate_response.json()["detail"].lower()
