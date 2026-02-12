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


def test_rfq_package_generate_estimate_flow(client, auth_headers, tmp_path):
    bom_bytes = _make_bom_bytes()
    dxf_bytes = _make_dxf_bytes(tmp_path)

    create_response = client.post(
        "/api/v1/rfq-packages/",
        headers=auth_headers,
        data={"customer_name": "Acme Aerospace", "rfq_reference": "RFQ-DEMO-100"},
        files=[
            ("files", ("demo_bom.xlsx", bom_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
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

    export_response = client.get(
        f"/api/v1/rfq-packages/{package_id}/internal-estimate-export",
        headers=auth_headers,
    )
    assert export_response.status_code == 200
    assert export_response.headers["content-type"].startswith("application/json")


def test_rfq_generate_estimate_requires_geometry(client, auth_headers):
    bom_bytes = _make_bom_bytes()
    create_response = client.post(
        "/api/v1/rfq-packages/",
        headers=auth_headers,
        data={"customer_name": "No Geometry Test", "rfq_reference": "RFQ-NOGEO-1"},
        files=[
            ("files", ("demo_bom.xlsx", bom_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
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
