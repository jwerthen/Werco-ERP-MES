"""Read-only CAD evidence for draft production routings; never a quoting engine."""

from pathlib import Path

from app.fabrication_quote.worker import analyze_in_worker


def inspect_routing_geometry(file_path: str, file_name: str) -> dict:
    analysis = analyze_in_worker(Path(file_path).read_bytes(), file_name)
    geometry = analysis.get("geometry", {})
    result = {
        "file_name": file_name,
        "source_type": analysis.get("kind"),
        "part_hint": Path(file_name).stem,
        "flat_area": None,
        "cut_length": None,
        "hole_count": None,
        "bend_count": None,
        "bbox": None,
        "low_confidence": True,
        "warning": "CAD measurements are unreviewed candidates; confirm routing operations with engineering.",
        "issues": analysis.get("issues", []),
    }
    # Routing's existing prompt contract uses inches. Missing units/measurements
    # remain unknown. STEP surfaces are never turned into a guessed flat blank.
    if analysis.get("kind") == "dxf" and geometry.get("units") == "mm":
        area = geometry.get("net_candidate_area_mm2")
        length = geometry.get("measured_length_mm")
        result["flat_area"] = area / 645.16 if area is not None else None
        result["cut_length"] = length / 25.4 if length is not None else None
        bounds = geometry.get("bounds_mm")
        if bounds and len(bounds) == 4:
            result["bbox"] = dict(zip(("min_x", "min_y", "max_x", "max_y"), (v / 25.4 for v in bounds)))
    return result
