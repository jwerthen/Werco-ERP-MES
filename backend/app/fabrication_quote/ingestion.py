"""Source-preserving, bounded CAD/document observations for estimator review.

No material, weld, bend rule, price, or manufacturing route is inferred as approved.
Optional CAD/OCR dependencies fail visibly; STEP geometry never falls back to regex.
Native parsing should be called in an isolated worker with an external hard timeout.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import tempfile
from collections import Counter, defaultdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

MAX_BYTES = 25 * 1024 * 1024
MAX_ENTITIES = 20000
MAX_PAGES = 200
MAX_TEXT = 100000
MAX_OCCURRENCES = 5000
MAX_VERTICES = 100000
TOLERANCE_MM = 0.01


def _version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _issue(result, code, message, source=None, severity="warning"):
    result["issues"].append(
        {
            "code": code,
            "message": message,
            "severity": severity,
            "source": source or {"file": result["file_name"]},
        }
    )


def analyze_file(content: bytes, filename: str, units_override: str | None = None) -> dict:
    """Analyze bytes into JSON-safe observations, geometry and unresolved requirements."""
    if not isinstance(content, bytes):
        raise ValueError("content must be bytes")
    name = Path(str(filename).replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    kind = {
        ".dxf": "dxf",
        ".pdf": "pdf",
        ".step": "step",
        ".stp": "step",
        ".csv": "csv",
    }.get(suffix, "unsupported")
    result = {
        "file_name": name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "kind": kind,
        "status": "needs_review",
        "parser": None,
        "units": None,
        "observations": [],
        "geometry": None,
        "issues": [],
        "pages": [],
        "automatic_costing_ready": False,
    }
    if len(content) > MAX_BYTES or not content:
        result["status"] = "error"
        _issue(
            result,
            "file_size",
            f"File must contain 1..{MAX_BYTES} bytes",
            severity="error",
        )
        return result
    if units_override not in (None, "mm", "in", "cm", "m"):
        raise ValueError("units_override must be mm, in, cm, m or null")
    try:
        if kind == "dxf":
            _dxf(content, result, units_override)
        elif kind == "pdf":
            _pdf(content, result)
        elif kind == "step":
            _step(content, result, units_override)
        elif kind == "csv":
            _csv(content, result)
        else:
            result["status"] = "unsupported"
            _issue(
                result,
                "unsupported_file",
                "Supported file extensions are DXF, STEP/STP, PDF and CSV",
            )
    except ImportError as exc:
        result["status"] = "parser_unavailable"
        _issue(
            result,
            "parser_unavailable",
            f"Optional parser dependency unavailable: {exc.name}",
            severity="error",
        )
    except Exception as exc:
        result["status"] = "partial" if result["geometry"] or result["pages"] else "error"
        _issue(
            result,
            "parse_failed",
            f"{type(exc).__name__}: {str(exc)[:300]}",
            severity="error",
        )
    return result


def _csv(content, result):
    result["parser"] = {"name": "python-csv", "version": "stdlib"}
    text = content.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("CSV contains NUL bytes; supply UTF-8 CSV")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    reader = csv.reader(io.StringIO(text, newline=""), dialect, strict=True)
    result["table"] = {
        "rows": rows,
        "delimiter": dialect.delimiter,
        "header_row_candidate": None,
        "review_status": "unreviewed",
    }
    for number, cells in enumerate(reader, 1):
        if number > 10000:
            result["status"] = "partial"
            _issue(
                result,
                "csv_row_limit",
                "CSV exceeds 10,000 rows; remaining rows were not interpreted",
                severity="error",
            )
            break
        if len(cells) > 200 or any(len(cell) > 10000 for cell in cells):
            raise ValueError(f"CSV row {number} exceeds column/cell size limits")
        record = {
            "row_number": number,
            "ending_physical_line": reader.line_num,
            "cells": cells,
            "source": {"file": result["file_name"], "row": number},
            "review_status": "unreviewed",
        }
        rows.append(record)
        if number == 1:
            result["table"]["header_row_candidate"] = cells
    result["row_count_extracted"] = len(rows)
    _issue(
        result,
        "table_mapping_required",
        "Raw cells preserve leading zeros and price units. Confirm headers, exact part identities, quantity/pack/MOQ/currency/validity and make-buy meaning before importing BOM or offers",
    )


def _dxf(content, result, override):
    import ezdxf

    from .nesting import _inside, _polygon, _segments_intersect

    result["parser"] = {"name": "ezdxf", "version": ezdxf.__version__}
    with tempfile.NamedTemporaryFile(suffix=".dxf") as file:
        file.write(content)
        file.flush()
        document = ezdxf.readfile(file.name)
    model = document.modelspace()
    source_units = {1: "in", 4: "mm", 5: "cm", 6: "m"}.get(document.units)
    selected_units = override or source_units
    factor = {"in": 25.4, "mm": 1.0, "cm": 10.0, "m": 1000.0}.get(selected_units)
    result["units"] = {
        "source": source_units,
        "source_code": document.units,
        "override": override,
        "normalized": "mm" if factor else None,
        "scale_to_mm": factor,
    }
    if override and source_units and override != source_units:
        _issue(
            result,
            "unit_override_conflict",
            "Explicit unit override differs from DXF INSUNITS; confirm the intended physical dimensions",
        )
    inventory = Counter(e.dxftype() for e in model)
    result["entity_inventory"] = dict(inventory)
    if not factor:
        _issue(
            result,
            "units_required",
            "DXF units are missing or unsupported; provide an explicit unit override before physical measurement",
            severity="error",
        )
        return
    edges: list[dict[str, Any]] = []
    primitive_bounds: list[list[float]] = []
    count = 0
    vertex_count = 0
    unsupported = 0

    def emit(entity, sources, depth=0):
        nonlocal count, unsupported, vertex_count
        count += 1
        if count > MAX_ENTITIES or depth > 32:
            raise ValueError("DXF entity expansion/depth limit exceeded")
        kind = entity.dxftype()
        source = {
            "file": result["file_name"],
            "entity_handles": sources,
            "layer": entity.dxf.get("layer", "0"),
            "entity_type": kind,
        }
        if kind in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
            text = entity.plain_text() if hasattr(entity, "plain_text") else entity.dxf.get("text", "")
            result["observations"].append(
                {
                    "kind": "text",
                    "text": text[:MAX_TEXT],
                    "source": source,
                    "review_status": "unreviewed",
                }
            )
            return
        if kind == "INSERT":
            block = entity.block()
            if block is None or block.block.dxf.get("flags", 0) & 12:
                unsupported += 1
                _issue(
                    result,
                    "external_block",
                    "Missing or external block is not resolved",
                    source,
                )
                return
            if entity.has_extension_dict:
                _issue(
                    result,
                    "block_extensions",
                    "Block extensions may include clipping; clipping is not interpreted",
                    source,
                )
                unsupported += 1
            inserts = entity.multi_insert() if entity.mcount > 1 else [entity]
            for insert_index, insert in enumerate(inserts):
                for attribute in insert.attribs:
                    emit(
                        attribute,
                        sources + [f"attribute:{attribute.dxf.handle}"],
                        depth + 1,
                    )

                def skipped(item, reason):
                    nonlocal unsupported
                    unsupported += 1
                    _issue(result, "block_entity_skipped", str(reason), source)

                for index, child in enumerate(insert.virtual_entities(skipped_entity_callback=skipped)):
                    emit(
                        child,
                        sources + [f"insert:{insert_index}/entity:{index}"],
                        depth + 1,
                    )
            return
        if kind in ("LWPOLYLINE", "POLYLINE"):
            if kind == "POLYLINE" and not entity.is_2d_polyline:
                unsupported += 1
                _issue(
                    result,
                    "nonplanar_polyline",
                    "Only planar 2D polylines are supported",
                    source,
                )
                return
            for index, child in enumerate(entity.virtual_entities()):
                emit(child, sources + [f"segment:{index}"], depth + 1)
            return
        if kind not in ("LINE", "ARC", "CIRCLE"):
            unsupported += 1
            _issue(
                result,
                "unsupported_entity",
                f"{kind} is inventoried but not measured; classify or convert it explicitly",
                source,
            )
            return
        extrusion = entity.dxf.get("extrusion", (0, 0, 1))
        if any(abs(extrusion[i] - (1 if i == 2 else 0)) > 1e-9 for i in range(3)):
            unsupported += 1
            _issue(
                result,
                "non_xy_geometry",
                "Non-default entity plane is not accepted as a reviewed flat",
                source,
            )
            return
        primitive: dict[str, Any]
        if kind == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            if abs(start.z) > 1e-7 or abs(end.z) > 1e-7:
                unsupported += 1
                _issue(
                    result,
                    "non_xy_geometry",
                    "Geometry must lie in the XY plane for flat extraction",
                    source,
                )
                return
            points = [
                [start.x * factor, start.y * factor],
                [end.x * factor, end.y * factor],
            ]
            length = math.dist(*points)
            signed_area = (points[0][0] * points[1][1] - points[1][0] * points[0][1]) / 2
            primitive = {"kind": "line", "start_mm": points[0], "end_mm": points[1]}
            extrema = points
        else:
            center = entity.dxf.center
            radius = entity.dxf.radius * factor
            if abs(center.z) > 1e-7 or radius <= 0:
                unsupported += 1
                _issue(
                    result,
                    "invalid_arc",
                    "Arc must have positive radius and lie in XY",
                    source,
                )
                return
            cx, cy = center.x * factor, center.y * factor
            start = math.radians(entity.dxf.start_angle) if kind == "ARC" else 0.0
            sweep = (
                math.radians((entity.dxf.end_angle - entity.dxf.start_angle) % 360) if kind == "ARC" else 2 * math.pi
            )
            if sweep <= 0:
                raise ValueError("Zero-sweep arc requires review")
            step = 2 * math.acos(max(-1, min(1, 1 - TOLERANCE_MM / radius)))
            samples = max(2, math.ceil(sweep / max(step, 1e-9)))
            if samples > 4096:
                raise ValueError("Arc approximation vertex limit exceeded")
            points = [
                [
                    cx + radius * math.cos(start + sweep * i / samples),
                    cy + radius * math.sin(start + sweep * i / samples),
                ]
                for i in range(samples + 1)
            ]
            if kind == "CIRCLE":
                points[-1] = points[0]
            length = radius * sweep
            end = start + sweep
            signed_area = (
                radius * cx * (math.sin(end) - math.sin(start))
                + radius * cy * (math.cos(start) - math.cos(end))
                + radius * radius * sweep
            ) / 2
            primitive = {
                "kind": kind.lower(),
                "center_mm": [cx, cy],
                "radius_mm": radius,
                "start_radians": start,
                "sweep_radians": sweep,
            }
            extrema = points[:1] + points[-1:]
            for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
                if (angle - start) % (2 * math.pi) <= sweep + 1e-12:
                    extrema.append([cx + radius * math.cos(angle), cy + radius * math.sin(angle)])
        if not all(math.isfinite(v) and abs(v) <= 1e7 for p in points for v in p) or length <= 1e-9:
            raise ValueError("Nonfinite, out-of-range or degenerate geometry")
        vertex_count += len(points)
        if vertex_count > MAX_VERTICES:
            raise ValueError("DXF total approximation vertex limit exceeded")
        primitive_bounds.extend(extrema)
        edges.append(
            {
                "points": points,
                "length": length,
                "area": signed_area,
                "source": source,
                "primitive": primitive,
            }
        )

    for entity in model:
        emit(entity, [str(entity.dxf.handle)])
    # Closed connected loops; branching/open networks are retained as unresolved geometry.
    nodes = defaultdict(list)

    def key(point):
        return tuple(round(v, 6) for v in point)

    for index, edge in enumerate(edges):
        nodes[key(edge["points"][0])].append(index)
        nodes[key(edge["points"][-1])].append(index)
    visited, contours = set(), []
    for initial, edge in enumerate(edges):
        if initial in visited:
            continue
        indices, points, refs, area, length = [], [], [], 0.0, 0.0
        start_key, current = key(edge["points"][0]), key(edge["points"][0])
        index = initial
        closed = False
        while index not in visited:
            visited.add(index)
            item = edges[index]
            forward = key(item["points"][0]) == current
            path = item["points"] if forward else list(reversed(item["points"]))
            points.extend(path if not points else path[1:])
            area += item["area"] if forward else -item["area"]
            length += item["length"]
            refs.append(item["source"])
            indices.append(index)
            current = key(path[-1])
            if current == start_key:
                closed = True
                break
            if len(nodes[current]) != 2:
                break
            candidates = [i for i in nodes[current] if i not in visited]
            if not candidates:
                break
            index = candidates[0]
        contour = {
            "id": f"contour-{len(contours) + 1}",
            "closed": closed,
            "vertices_mm": points[:-1] if closed else points,
            "length_mm": length,
            "area_mm2": abs(area) if closed else None,
            "sources": refs,
            "primitive_indices": indices,
            "is_hole": None,
        }
        if closed:
            try:
                if any(
                    len(nodes[key(edges[i]["points"][0])]) != 2 or len(nodes[key(edges[i]["points"][-1])]) != 2
                    for i in indices
                ):
                    raise ValueError("Ambiguous coincident or branching contour endpoints")
                _polygon(contour["vertices_mm"], contour["id"])
            except ValueError as exc:
                contour["valid"] = False
                _issue(result, "invalid_contour", str(exc), refs[0])
            else:
                contour["valid"] = True
        else:
            contour["valid"] = False
            _issue(
                result,
                "open_contour",
                "Open/branching geometry cannot define a blank without review",
                refs[0],
            )
        contours.append(contour)
    valid = [c for c in contours if c["valid"]]
    topology_checked = True
    checks = 0
    for i, contour in enumerate(valid):
        first = contour["vertices_mm"]
        for other in valid[:i]:
            second = other["vertices_mm"]
            # All loop boundaries must be disjoint; nested containment is allowed.
            for a, b in zip(first, first[1:] + first[:1]):
                for c, d in zip(second, second[1:] + second[:1]):
                    checks += 1
                    if checks > 200000:
                        topology_checked = False
                        break
                    if _segments_intersect(a, b, c, d):
                        contour["valid"] = other["valid"] = False
                if checks > 200000:
                    break
            if checks > 200000:
                break
        if checks > 200000:
            break
    if not topology_checked:
        _issue(
            result,
            "topology_check_limit",
            "Cross-contour validation budget exceeded; no combined area is reported",
        )
    if any(not c["valid"] for c in valid):
        _issue(
            result,
            "intersecting_contours",
            "Contours overlap or touch; combined area and blank identity require correction",
        )
    valid = [c for c in contours if c["valid"]]
    for contour in valid:
        p = contour["vertices_mm"][0]
        depth = sum(_inside(p, other["vertices_mm"], boundary=False) for other in valid if other is not contour)
        contour["nesting_depth"] = depth
        contour["is_hole"] = bool(depth % 2)
    if not contours:
        _issue(
            result,
            "no_flat_geometry",
            "No supported closed flat geometry was found",
            severity="error",
        )
    _issue(
        result,
        "layer_roles_unreviewed",
        "Cut/bend/marking/annotation layer roles and blank identity require estimator review; measured contours are candidates",
    )
    result["geometry"] = {
        "units": "mm",
        "contours": contours,
        "primitives": [dict(e["primitive"], source=e["source"], length_mm=e["length"]) for e in edges],
        "measured_length_mm": sum(e["length"] for e in edges),
        "bounds_mm": (
            [min(p[i] for p in primitive_bounds) for i in (0, 1)]
            + [max(p[i] for p in primitive_bounds) for i in (0, 1)]
            if primitive_bounds
            else None
        ),
        "closed_contour_count": len(valid),
        "net_candidate_area_mm2": (
            sum((-1 if c["is_hole"] else 1) * c["area_mm2"] for c in valid)
            if len(valid) == len(contours) and valid and not unsupported and topology_checked
            else None
        ),
        "curve_approximation_tolerance_mm": TOLERANCE_MM,
        "measurement_basis": "Analytic supported line/arc lengths and signed loop areas; vertices approximate curves and must not be used as exact curved stock envelopes",
        "unsupported_entity_count": unsupported,
    }
    if unsupported or len(valid) != len(contours) or not topology_checked:
        result["status"] = "partial"


def _pdf(content, result):
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pdfplumber:
        result["parser"] = {"name": "pdfplumber", "version": _version("pdfplumber")}
        with pdfplumber.open(io.BytesIO(content)) as document:
            result["page_count"] = len(document.pages)
            for number, page in enumerate(document.pages, 1):
                record = {
                    "page_number": number,
                    "status": "unprocessed",
                    "text": "",
                    "words": [],
                    "tables": [],
                    "source": {"file": result["file_name"], "page": number},
                }
                result["pages"].append(record)
                if number > MAX_PAGES:
                    continue
                try:
                    text = page.extract_text() or ""
                    record.update(
                        text=text[:MAX_TEXT],
                        width_points=float(page.width),
                        height_points=float(page.height),
                        status="native_text" if text.strip() else "ocr_required",
                    )
                    words = page.extract_words()[:10000]
                    record["words"] = [
                        {
                            "text": w["text"],
                            "bbox_points": [float(w[k]) for k in ("x0", "top", "x1", "bottom")],
                            "source": dict(
                                record["source"],
                                coordinate_system="top-left PDF points",
                            ),
                        }
                        for w in words
                    ]
                    record["tables"] = [
                        {
                            "rows": table,
                            "review_status": "unreviewed",
                            "source": record["source"],
                        }
                        for table in page.extract_tables()[:100]
                    ]
                    if len(text) > MAX_TEXT:
                        record["status"] = "partial"
                        _issue(
                            result,
                            "page_text_limit",
                            "Native page text exceeded extraction limit",
                            record["source"],
                        )
                except Exception as exc:
                    record.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    else:
        from pypdf import PdfReader

        result["parser"] = {"name": "pypdf", "version": _version("pypdf")}
        document = PdfReader(io.BytesIO(content))
        if document.is_encrypted and not document.decrypt(""):
            raise ValueError("Encrypted PDF requires a decrypted review copy")
        result["page_count"] = len(document.pages)
        for number, page in enumerate(document.pages, 1):
            record = {
                "page_number": number,
                "status": "unprocessed",
                "text": "",
                "words": [],
                "tables": [],
                "source": {"file": result["file_name"], "page": number},
            }
            result["pages"].append(record)
            if number > MAX_PAGES:
                continue
            try:
                text = page.extract_text() or ""
                record.update(
                    text=text[:MAX_TEXT],
                    status=("partial" if len(text) > MAX_TEXT else "native_text" if text.strip() else "ocr_required"),
                    width_points=float(page.mediabox.width),
                    height_points=float(page.mediabox.height),
                )
            except Exception as exc:
                record.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    for page in result["pages"]:
        if page["status"] != "native_text":
            result["status"] = "partial"
            _issue(
                result,
                "page_requires_review",
                f"Page extraction status: {page['status']}",
                page["source"],
            )
        if page["text"]:
            result["observations"].append(
                {
                    "kind": "page_text",
                    "text": page["text"],
                    "source": page["source"],
                    "review_status": "unreviewed",
                }
            )
    _issue(
        result,
        "drawing_semantics_unreviewed",
        "Native text/table extraction does not approve BOM quantities, material, dimensions, weld symbols or leader associations; all critical requirements need review",
    )


def _prismatic_flat(shape, definition_id):
    """Qualify an already-flat, thin extrusion from matching B-Rep cap faces.

    This does not unfold bends. Chamfers, blind pockets, stepped sections, splines
    and non-planar/non-axial-cylinder side walls are explicitly unsupported.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
    from OCP.GeomAbs import (
        GeomAbs_Circle,
        GeomAbs_Cylinder,
        GeomAbs_Line,
        GeomAbs_Plane,
    )
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    def unsupported(reason):
        return {"status": "unsupported", "reason": reason}

    faces, planar = [], []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        surface = BRepAdaptor_Surface(face)
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        item = (face, surface, props.Mass(), len(faces))
        faces.append(item)
        if surface.GetType() == GeomAbs_Plane:
            planar.append(item)
        explorer.Next()
    if len(planar) < 2 or len(faces) > 1000:
        return unsupported("No bounded planar cap-face pair")
    planar.sort(key=lambda f: -f[2])
    first = planar[0]
    plane = first[1].Plane()
    normal = plane.Axis().Direction()
    cap_area = first[2]
    pair, thickness = None, None
    for candidate in planar[1:]:
        other_plane = candidate[1].Plane()
        if abs(abs(normal.Dot(other_plane.Axis().Direction())) - 1) > 1e-8:
            continue
        delta = gp_Vec(plane.Location(), other_plane.Location())
        distance = delta.Dot(gp_Vec(normal))
        if abs(distance) < 1e-6 or abs(candidate[2] - cap_area) > max(1e-5, cap_area * 1e-7):
            continue
        transform = gp_Trsf()
        transform.SetTranslation(gp_Vec(normal).Multiplied(distance))
        moved = BRepBuilderAPI_Transform(first[0], transform, True).Shape()
        common = BRepAlgoAPI_Common(moved, candidate[0])
        if not common.IsDone():
            continue
        common_props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(common.Shape(), common_props)
        if abs(common_props.Mass() - cap_area) <= max(1e-5, cap_area * 1e-7):
            pair, thickness = candidate, abs(distance)
            break
    if pair is None:
        return unsupported("Opposite planar boundaries do not coincide after translation")
    for face, surface, _area, index in faces:
        if index in (first[3], pair[3]):
            continue
        kind = surface.GetType()
        if kind == GeomAbs_Plane and abs(surface.Plane().Axis().Direction().Dot(normal)) < 1e-8:
            continue
        if kind == GeomAbs_Cylinder and abs(abs(surface.Cylinder().Axis().Direction().Dot(normal)) - 1) < 1e-8:
            continue
        return unsupported("Side walls are not normal planar walls or coaxial through-hole/outer cylinders")
    volume = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, volume)
    if abs(volume.Mass() - cap_area * thickness) > max(1e-4, cap_area * thickness * 1e-7):
        return unsupported("Solid volume differs from matched cap area times thickness")
    origin = plane.Location()
    x_axis, y_axis = plane.XAxis().Direction(), plane.YAxis().Direction()

    def project(point):
        vector = gp_Vec(origin, point)
        return [vector.Dot(gp_Vec(x_axis)), vector.Dot(gp_Vec(y_axis))]

    outer_wire = BRepTools.OuterWire_s(first[0])
    wires = TopExp_Explorer(first[0], TopAbs_WIRE)
    contours, extrema = [], []
    while wires.More():
        wire = TopoDS.Wire_s(wires.Current())
        edge_explorer = BRepTools_WireExplorer(wire, first[0])
        points, primitives, length = [], [], 0.0
        while edge_explorer.More():
            edge = edge_explorer.Current()
            curve = BRepAdaptor_Curve(edge)
            start, end = curve.FirstParameter(), curve.LastParameter()
            if curve.GetType() == GeomAbs_Line:
                samples = [project(curve.Value(start)), project(curve.Value(end))]
                segment_length = math.dist(*samples)
                primitive = {
                    "kind": "line",
                    "start_mm": samples[0],
                    "end_mm": samples[1],
                }
                extrema.extend(samples)
            elif curve.GetType() == GeomAbs_Circle:
                circle = curve.Circle()
                radius, sweep = circle.Radius(), end - start
                step = 2 * math.acos(max(-1, min(1, 1 - TOLERANCE_MM / radius)))
                count = max(2, math.ceil(sweep / max(step, 1e-9)))
                if count > 4096:
                    return unsupported("Curve approximation budget exceeded")
                samples = [project(curve.Value(start + sweep * i / count)) for i in range(count + 1)]
                center = project(circle.Location())
                a = circle.XAxis().Direction()
                b = circle.YAxis().Direction()
                for axis in (x_axis, y_axis):
                    angle = math.atan2(b.Dot(axis), a.Dot(axis))
                    for candidate in (angle, angle + math.pi):
                        parameter = start + (candidate - start) % (2 * math.pi)
                        if parameter <= end + 1e-10:
                            extrema.append(project(curve.Value(parameter)))
                extrema.extend([samples[0], samples[-1]])
                segment_length = radius * sweep
                primitive = {
                    "kind": "arc",
                    "center_mm": center,
                    "radius_mm": radius,
                    "sweep_radians": sweep,
                    "start_mm": samples[0],
                    "end_mm": samples[-1],
                }
            else:
                return unsupported("Only analytic line/circular cap boundaries are currently supported")
            if edge.Orientation() == TopAbs_REVERSED:
                samples.reverse()
            if points and math.dist(points[-1], samples[0]) > 1e-5:
                return unsupported("Cap wire edge order has a gap")
            points.extend(samples if not points else samples[1:])
            if len(points) > 10000:
                return unsupported("Cap contour vertex budget exceeded")
            primitive["length_mm"] = segment_length
            primitives.append(primitive)
            length += segment_length
            edge_explorer.Next()
        if not points or math.dist(points[0], points[-1]) > 1e-5:
            return unsupported("Cap wire is not closed")
        contours.append(
            {
                "is_hole": not wire.IsSame(outer_wire),
                "vertices_mm": points[:-1],
                "primitives": primitives,
                "length_mm": length,
            }
        )
        wires.Next()
    outer = [c for c in contours if not c["is_hole"]]
    if len(outer) != 1:
        return unsupported("A single connected outer boundary is required")
    bounds = [min(p[i] for p in extrema) for i in (0, 1)] + [max(p[i] for p in extrema) for i in (0, 1)]
    short_span = min(bounds[2] - bounds[0], bounds[3] - bounds[1])
    if thickness > short_span * 0.2:
        return unsupported("Solid falls outside the supported thin-extrusion scope (thickness <= 20% of short span)")
    return {
        "status": "candidate",
        "basis": "matched translated planar cap faces, qualified side surfaces and volume/area/thickness agreement",
        "requires_estimator_review": True,
        "units": "mm",
        "thickness_mm": thickness,
        "area_mm2": cap_area,
        "cut_length_mm": sum(c["length_mm"] for c in contours),
        "outline_mm": outer[0]["vertices_mm"],
        "holes_mm": [c["vertices_mm"] for c in contours if c["is_hole"]],
        "contours": contours,
        "bounds_mm": bounds,
        "curve_approximation_tolerance_mm": TOLERANCE_MM,
        "source": {
            "definition_id": definition_id,
            "cap_face_indices": [first[3], pair[3]],
        },
        "local_plane": {
            "origin_mm": [origin.X(), origin.Y(), origin.Z()],
            "x_axis": [x_axis.X(), x_axis.Y(), x_axis.Z()],
            "y_axis": [y_axis.X(), y_axis.Y(), y_axis.Z()],
        },
        "limitations": "Already-flat prismatic solid only; no bend unfolding. Polygon vertices approximate curves: use analytic bounds for conservative nesting or retain the curve tolerance.",
    }


def _step(content, result, override):
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    result["parser"] = {
        "name": "OCP/STEPCAFControl/XDE",
        "version": _version("cadquery-ocp"),
    }
    if override:
        _issue(
            result,
            "step_units_override_rejected",
            "STEP unit declarations are handled by OCCT; DXF-style unit override is not applied",
            severity="error",
        )
    reader = STEPCAFControl_Reader()
    # OCCT's STEP unit setting is process-global: production calls belong in an isolated worker.
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    document = TDocStd_Document(TCollection_ExtendedString("quote-step"))
    with tempfile.NamedTemporaryFile(suffix=".step") as file:
        file.write(content)
        file.flush()
        if reader.ReadFile(file.name) != IFSelect_RetDone or not reader.Transfer(document):
            raise ValueError("OCCT could not read/transfer the STEP document")
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    definitions, occurrences = {}, []
    meshes = []
    mesh_vertices = mesh_triangles = 0

    def display_mesh(shape, definition_id):
        nonlocal mesh_vertices, mesh_triangles
        mesh = {
            "id": definition_id,
            "positions": [],
            "indices": [],
            "face_ranges": [],
            "tolerance_mm": 0.1,
            "angular_tolerance_radians": 0.3,
            "status": "complete",
            "purpose": "display_only",
        }
        if mesh_vertices >= 30000 or mesh_triangles >= 40000:
            mesh["status"] = "budget_exceeded"
            return mesh
        mesher = BRepMesh_IncrementalMesh(shape, 0.1, False, 0.3, False)
        if not mesher.IsDone():
            mesh["status"] = "unavailable"
            return mesh
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        face_index = 0
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            location = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, location)
            if triangulation is None:
                mesh["status"] = "partial"
                explorer.Next()
                face_index += 1
                continue
            nodes, triangles = triangulation.NbNodes(), triangulation.NbTriangles()
            if mesh_vertices + nodes > 30000 or mesh_triangles + triangles > 40000:
                mesh["status"] = "partial"
                break
            offset = len(mesh["positions"]) // 3
            for index in range(1, nodes + 1):
                point = triangulation.Node(index).Transformed(location.Transformation())
                mesh["positions"].extend([point.X(), point.Y(), point.Z()])
            start = len(mesh["indices"]) // 3
            for index in range(1, triangles + 1):
                a, b, c = triangulation.Triangle(index).Get()
                if face.Orientation() == TopAbs_REVERSED:
                    b, c = c, b
                mesh["indices"].extend([offset + a - 1, offset + b - 1, offset + c - 1])
            mesh["face_ranges"].append(
                {
                    "face_index": face_index,
                    "triangle_start": start,
                    "triangle_count": triangles,
                }
            )
            mesh_vertices += nodes
            mesh_triangles += triangles
            explorer.Next()
            face_index += 1
        if not mesh["indices"]:
            mesh["status"] = "unavailable"
        return mesh

    def entry(label):
        value = TCollection_AsciiString()
        TDF_Tool.Entry_s(label, value)
        return value.ToCString()

    def label_name(label):
        name = TDataStd_Name()
        return name.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(), name) else None

    def walk(label, parent_location, path, active):
        if len(occurrences) >= MAX_OCCURRENCES or len(path) > 64:
            raise ValueError("STEP occurrence/depth limit exceeded")
        reference = TDF_Label()
        definition = reference if tool.IsReference_s(label) and tool.GetReferredShape_s(label, reference) else label
        definition_id = entry(definition)
        if definition_id in active:
            raise ValueError("Cyclic STEP assembly reference")
        location = parent_location.Multiplied(tool.GetLocation_s(label))
        occurrence_id = "/".join(path + [entry(label)])
        transform = location.Transformation()
        matrix = [[transform.Value(row, column) for column in range(1, 5)] for row in range(1, 4)] + [[0, 0, 0, 1]]
        assembly = tool.IsAssembly_s(definition)
        occurrences.append(
            {
                "id": occurrence_id,
                "definition_id": definition_id,
                "parent_id": "/".join(path) or None,
                "name": label_name(label) or label_name(definition),
                "is_assembly": assembly,
                "transform_mm": matrix,
                "source": {"file": result["file_name"], "xde_label": entry(label)},
            }
        )
        if definition_id not in definitions:
            shape = tool.GetShape_s(definition)
            if shape.IsNull():
                raise ValueError("STEP definition has a null shape")
            # A free/simple definition may itself carry a placement. Its occurrence
            # already records that location, so tessellate/measure in local coordinates.
            shape = shape.Located(TopLoc_Location())
            box = Bnd_Box()
            BRepBndLib.AddOptimal_s(shape, box, False, False)
            count = {}
            for what, title in (
                (TopAbs_SOLID, "solid_count"),
                (TopAbs_FACE, "face_count"),
            ):
                explorer = TopExp_Explorer(shape, what)
                total = 0
                while explorer.More():
                    total += 1
                    explorer.Next()
                count[title] = total
            area, volume = GProp_GProps(), GProp_GProps()
            BRepGProp.SurfaceProperties_s(shape, area)
            if count["solid_count"]:
                BRepGProp.VolumeProperties_s(shape, volume)
            definitions[definition_id] = {
                "id": definition_id,
                "name": label_name(definition),
                "is_assembly": assembly,
                "bounds_mm": list(box.Get()) if not box.IsVoid() else None,
                "surface_area_mm2": area.Mass(),
                "volume_mm3": volume.Mass() if count["solid_count"] else None,
                "valid_brep": BRepCheck_Analyzer(shape).IsValid(),
                **count,
                "source": {"file": result["file_name"], "xde_label": definition_id},
            }
            if not assembly:
                if count["solid_count"] == 1 and definitions[definition_id]["valid_brep"]:
                    try:
                        definitions[definition_id]["flat_pattern"] = _prismatic_flat(shape, definition_id)
                    except Exception as exc:
                        definitions[definition_id]["flat_pattern"] = {
                            "status": "unsupported",
                            "reason": f"Prismatic qualification failed: {type(exc).__name__}",
                        }
                else:
                    definitions[definition_id]["flat_pattern"] = {
                        "status": "unsupported",
                        "reason": "Exactly one valid solid is required for prismatic qualification",
                    }
                try:
                    mesh = display_mesh(shape, definition_id)
                    meshes.append(mesh)
                    if mesh["status"] != "complete":
                        _issue(
                            result,
                            "display_mesh_incomplete",
                            f"Display tessellation is {mesh['status']}; canonical B-Rep measurements retained",
                            {"file": result["file_name"], "xde_label": definition_id},
                        )
                except Exception as exc:
                    _issue(
                        result,
                        "display_mesh_failed",
                        f"Display tessellation failed: {type(exc).__name__}; canonical B-Rep measurements retained",
                        {"file": result["file_name"], "xde_label": definition_id},
                    )
        if assembly:
            children = TDF_LabelSequence()
            tool.GetComponents_s(definition, children, False)
            for index in range(1, children.Length() + 1):
                walk(
                    children.Value(index),
                    location,
                    path + [entry(label)],
                    active | {definition_id},
                )

    for index in range(1, roots.Length() + 1):
        walk(roots.Value(index), TopLoc_Location(), [], set())
    result["units"] = {
        "source": "STEP declared units interpreted by OCCT",
        "normalized": "mm",
    }
    leaf_definitions = [d for d in definitions.values() if not d["is_assembly"]]
    flat_count = sum(d.get("flat_pattern", {}).get("status") == "candidate" for d in leaf_definitions)
    all_flat = bool(leaf_definitions) and flat_count == len(leaf_definitions)
    result["geometry"] = {
        "units": "mm",
        "definitions": list(definitions.values()),
        "occurrences": occurrences,
        "meshes": meshes,
        "flat_pattern_definition_count": flat_count,
        "leaf_occurrence_count": sum(not o["is_assembly"] for o in occurrences),
        "assembly_occurrence_count": sum(o["is_assembly"] for o in occurrences),
        "measurement_basis": "OCCT B-Rep mass properties and optimal bounds; solid definitions cached, every XDE occurrence retained",
        "unfold": {
            "status": "not_required" if all_flat else "unsupported",
            "reason": (
                "All leaf definitions are already-flat prismatic candidates; estimator review remains required"
                if all_flat
                else "Bent/unsupported solids cannot be unfolded: no qualified FreeCAD SheetMetal worker configured"
            ),
        },
    }
    if not occurrences or any(not definition["valid_brep"] for definition in definitions.values()):
        result["status"] = "partial"
        _issue(
            result,
            "invalid_step_shapes",
            "Missing occurrences or invalid B-Rep shapes require review",
            severity="error",
        )
    _issue(
        result,
        "manufacturing_interpretation_required",
        "STEP geometry does not approve material, thickness, welds, make/buy boundaries or bend allowances",
    )
    if not all_flat:
        _issue(
            result,
            "unfold_unavailable",
            "Bend unfolding is not configured; no pseudo-flat or bounding rectangle is substituted for unsupported solids",
        )
