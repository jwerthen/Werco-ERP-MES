import io
import json
import math
import tempfile

import ezdxf
import pytest

from app.fabrication_quote.ingestion import analyze_file


def dxf_bytes(document):
    stream = io.StringIO()
    document.write(stream)
    return stream.getvalue().encode("utf-8")


@pytest.mark.parametrize("unit,scale", [(4, 1), (1, 25.4)])
def test_dxf_exact_rectangle_hole_and_physical_units(unit, scale):
    doc = ezdxf.new()
    doc.units = unit
    model = doc.modelspace()
    model.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    model.add_circle((5, 5), 1)
    result = analyze_file(dxf_bytes(doc), "blank.dxf")
    geometry = result["geometry"]
    assert geometry["measured_length_mm"] == pytest.approx((40 + 2 * math.pi) * scale)
    assert geometry["net_candidate_area_mm2"] == pytest.approx((100 - math.pi) * scale * scale)
    assert sorted(c["is_hole"] for c in geometry["contours"]) == [False, True]
    assert geometry["bounds_mm"] == pytest.approx([0, 0, 10 * scale, 10 * scale])
    assert not result["automatic_costing_ready"]
    json.dumps(result, allow_nan=False)


def test_dxf_blocks_explicit_transforms_repeated_instances():
    doc = ezdxf.new()
    doc.units = 4
    block = doc.blocks.new("BRACKET")
    block.add_lwpolyline([(0, 0), (10, 0), (10, 5), (0, 5)], close=True)
    doc.modelspace().add_blockref("BRACKET", (0, 0))
    doc.modelspace().add_blockref("BRACKET", (20, 0))
    result = analyze_file(dxf_bytes(doc), "assembly.dxf")
    assert result["geometry"]["closed_contour_count"] == 2
    assert result["geometry"]["measured_length_mm"] == pytest.approx(60)
    assert result["geometry"]["bounds_mm"] == pytest.approx([0, 0, 30, 5])
    assert result["geometry"]["contours"][0]["sources"] != result["geometry"]["contours"][1]["sources"]


def test_dxf_bulge_is_analytic_arc():
    doc = ezdxf.new()
    doc.units = 4
    doc.modelspace().add_lwpolyline([(0, 0, 1), (10, 0, 0)], format="xyb", close=True)
    result = analyze_file(dxf_bytes(doc), "semicircle.dxf")
    assert result["geometry"]["measured_length_mm"] == pytest.approx(10 + 5 * math.pi)
    assert result["geometry"]["net_candidate_area_mm2"] == pytest.approx(math.pi * 25 / 2)


def test_dxf_missing_units_and_partial_geometry_are_visible():
    doc = ezdxf.new()
    doc.units = 0
    doc.modelspace().add_line((0, 0), (10, 0))
    result = analyze_file(dxf_bytes(doc), "unknown.dxf")
    assert result["geometry"] is None
    assert "units_required" in {i["code"] for i in result["issues"]}
    result = analyze_file(dxf_bytes(doc), "unknown.dxf", units_override="mm")
    assert result["status"] == "partial"
    assert result["geometry"]["contours"][0]["closed"] is False


def test_pdf_preserves_all_pages_including_empty_page():
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    stream = io.BytesIO()
    canvas = reportlab.Canvas(stream)
    canvas.drawString(50, 700, "PART P-100 QTY 4")
    canvas.showPage()
    canvas.showPage()
    canvas.drawString(50, 700, "MATERIAL A36")
    canvas.save()
    result = analyze_file(stream.getvalue(), "drawing.pdf")
    assert result["page_count"] == 3
    assert [p["page_number"] for p in result["pages"]] == [1, 2, 3]
    assert result["pages"][1]["status"] == "ocr_required"
    assert "MATERIAL A36" in result["pages"][2]["text"]
    assert result["automatic_costing_ready"] is False


def test_invalid_file_does_not_invent_geometry():
    result = analyze_file(b"not a CAD file CARTESIAN_POINT('',(1.,2.,3.));", "fake.step")
    assert result["status"] in ("parser_unavailable", "error")
    assert result["geometry"] is None


def test_csv_raw_cells_preserve_identifiers_price_basis_and_embedded_lines():
    content = b'MPN,Qty,Price,Unit,Description\r\n000123,60,12.50,per 100,"Line 1\nLine 2"\r\n'
    result = analyze_file(content, "offer.csv")
    assert result["table"]["rows"][1]["cells"] == [
        "000123",
        "60",
        "12.50",
        "per 100",
        "Line 1\nLine 2",
    ]
    assert result["table"]["rows"][1]["source"]["row"] == 2
    assert result["table"]["rows"][1]["ending_physical_line"] == 3
    assert result["automatic_costing_ready"] is False


def test_dxf_intersecting_contours_do_not_report_combined_area():
    doc = ezdxf.new()
    doc.units = 4
    model = doc.modelspace()
    model.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    model.add_lwpolyline([(5, 5), (15, 5), (15, 15), (5, 15)], close=True)
    result = analyze_file(dxf_bytes(doc), "overlap.dxf")
    assert result["geometry"]["net_candidate_area_mm2"] is None
    assert result["status"] == "partial"


def test_true_step_repeated_assembly_occurrences():
    pytest.importorskip("OCP")
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    document = TDocStd_Document(TCollection_ExtendedString("test"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    definition = tool.AddShape(BRepPrimAPI_MakeBox(10, 20, 3).Shape(), False)
    compound = TopoDS_Compound()
    BRep_Builder().MakeCompound(compound)
    assembly = tool.AddShape(compound, True)
    tool.AddComponent(assembly, definition, TopLoc_Location())
    transform = gp_Trsf()
    transform.SetTranslation(gp_Vec(50, 0, 0))
    tool.AddComponent(assembly, definition, TopLoc_Location(transform))
    tool.UpdateAssemblies()
    writer = STEPCAFControl_Writer()
    assert writer.Transfer(document, STEPControl_AsIs)
    with tempfile.NamedTemporaryFile(suffix=".step") as file:
        writer.Write(file.name)
        file.seek(0)
        result = analyze_file(file.read(), "repeated.step")
    assert result["geometry"], result["issues"]
    assert result["geometry"]["leaf_occurrence_count"] == 2
    leaves = [o for o in result["geometry"]["occurrences"] if not o["is_assembly"]]
    assert leaves[0]["definition_id"] == leaves[1]["definition_id"]
    assert sorted(o["transform_mm"][0][3] for o in leaves) == pytest.approx([0, 50])
    definition = next(d for d in result["geometry"]["definitions"] if not d["is_assembly"])
    assert definition["volume_mm3"] == pytest.approx(600)
    assert definition["surface_area_mm2"] == pytest.approx(580)
    assert result["geometry"]["unfold"]["status"] == "unsupported"
    mesh = result["geometry"]["meshes"][0]
    assert mesh["status"] == "complete", result["issues"]
    assert len(mesh["indices"]) == 36
    assert max(mesh["indices"]) < len(mesh["positions"]) // 3
    assert len(mesh["face_ranges"]) == 6
    json.dumps(result, allow_nan=False)


def _step_bytes(shape, unit="MM"):
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    previous = Interface_Static.CVal_s("write.step.unit")
    Interface_Static.SetCVal_s("write.step.unit", unit)
    try:
        writer.Transfer(shape, STEPControl_AsIs)
        with tempfile.NamedTemporaryFile(suffix=".step") as file:
            writer.Write(file.name)
            file.seek(0)
            return file.read()
    finally:
        Interface_Static.SetCVal_s("write.step.unit", previous)


@pytest.mark.parametrize("unit", ["MM", "INCH"])
def test_step_prismatic_plate_with_through_hole_has_true_boundary(unit):
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    plate = BRepPrimAPI_MakeBox(40, 30, 2).Shape()
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(10, 10, 0), gp_Dir(0, 0, 1)), 3, 2).Shape()
    shape = BRepAlgoAPI_Cut(plate, hole).Shape()
    result = analyze_file(_step_bytes(shape, unit), "plate.step")
    assert result["geometry"], result["issues"]
    definition = next(d for d in result["geometry"]["definitions"] if not d["is_assembly"])
    flat = definition["flat_pattern"]
    assert flat["status"] == "candidate", flat
    assert flat["thickness_mm"] == pytest.approx(2)
    assert flat["area_mm2"] == pytest.approx(1200 - 9 * math.pi)
    assert flat["cut_length_mm"] == pytest.approx(140 + 6 * math.pi)
    assert len(flat["outline_mm"]) == 4
    assert len(flat["holes_mm"]) == 1
    assert flat["requires_estimator_review"]
    assert definition["volume_mm3"] == pytest.approx((1200 - 9 * math.pi) * 2)


def test_step_stepped_and_bent_sections_do_not_become_flat_blanks():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    base = BRepPrimAPI_MakeBox(40, 30, 2).Shape()
    for addition in [
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 2), 10, 10, 2).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 2), 2, 30, 20).Shape(),
    ]:
        shape = BRepAlgoAPI_Fuse(base, addition).Shape()
        result = analyze_file(_step_bytes(shape), "unsupported.step")
        assert result["geometry"], result["issues"]
        definition = next(d for d in result["geometry"]["definitions"] if not d["is_assembly"])
        assert definition["flat_pattern"]["status"] == "unsupported"


def test_step_top_level_location_is_applied_exactly_once_to_mesh():
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location

    transform = gp_Trsf()
    transform.SetTranslation(gp_Vec(100, 200, 300))
    shape = BRepPrimAPI_MakeBox(40, 30, 2).Shape().Moved(TopLoc_Location(transform))
    result = analyze_file(_step_bytes(shape), "located.step")
    assert result["geometry"], result["issues"]
    leaf = next(o for o in result["geometry"]["occurrences"] if not o["is_assembly"])
    mesh = next(m for m in result["geometry"]["meshes"] if m["id"] == leaf["definition_id"])
    matrix = leaf["transform_mm"]
    vertices = [mesh["positions"][i : i + 3] for i in range(0, len(mesh["positions"]), 3)]
    world = [
        [sum(matrix[row][col] * point[col] for col in range(3)) + matrix[row][3] for row in range(3)]
        for point in vertices
    ]
    assert [min(p[i] for p in world) for i in range(3)] == pytest.approx([100, 200, 300])
    assert [max(p[i] for p in world) for i in range(3)] == pytest.approx([140, 230, 302])
