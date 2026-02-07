"""RFQ package ingestion and parsing for sheet-metal AI estimating."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.models.rfq_quote import RfqPackageFile
from app.services.pdf_service import extract_text_from_pdf


GAUGE_TO_INCHES: Dict[str, float] = {
    "24ga": 0.0239,
    "22ga": 0.0299,
    "20ga": 0.0359,
    "18ga": 0.0478,
    "16ga": 0.0598,
    "14ga": 0.0747,
    "12ga": 0.1046,
    "11ga": 0.1196,
    "10ga": 0.1345,
    "7ga": 0.1793,
}


MATERIAL_PATTERNS = [
    (re.compile(r"\b(304|316)\s*stainless|\bstainless\b", re.IGNORECASE), "Stainless Steel"),
    (re.compile(r"\b(5052|6061|aluminum|aluminium)\b", re.IGNORECASE), "Aluminum"),
    (re.compile(r"\b(a36|1018|mild steel|carbon steel|crs|hrs|steel)\b", re.IGNORECASE), "Carbon Steel"),
]


FINISH_PATTERNS = [
    re.compile(r"powder\s*coat(?:ing)?", re.IGNORECASE),
    re.compile(r"\bpaint(?:ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\banodiz(?:e|ed|ing)\b", re.IGNORECASE),
    re.compile(r"\bpassivat(?:e|ed|ion)\b", re.IGNORECASE),
    re.compile(r"\bzinc\s*plate|plating\b", re.IGNORECASE),
]


HARDWARE_HINTS = ("pem", "nut", "bolt", "screw", "standoff", "washer", "rivet", "insert", "stud")


def _clean_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        return float(text)
    except Exception:
        return None


def _detect_material(text: str) -> Optional[str]:
    for pattern, material in MATERIAL_PATTERNS:
        if pattern.search(text):
            return material
    return None


def _detect_finish(text: str) -> Optional[str]:
    for pattern in FINISH_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _parse_thickness(text: str) -> Tuple[Optional[str], Optional[float], float]:
    if not text:
        return None, None, 0.0

    gauge_match = re.search(r"\b(\d{1,2})\s*(ga|gauge)\b", text, re.IGNORECASE)
    if gauge_match:
        gauge_key = f"{gauge_match.group(1)}ga"
        thickness = GAUGE_TO_INCHES.get(gauge_key)
        return gauge_key, thickness, 0.85 if thickness else 0.55

    inch_match = re.search(r"(\d*\.?\d+)\s*(?:in|\"|\binch(?:es)?)\b", text, re.IGNORECASE)
    if inch_match:
        raw = inch_match.group(1)
        value = _safe_float(raw)
        return raw, value, 0.80 if value else 0.4

    mm_match = re.search(r"(\d*\.?\d+)\s*mm\b", text, re.IGNORECASE)
    if mm_match:
        raw_mm = mm_match.group(1)
        mm_value = _safe_float(raw_mm)
        if mm_value is None:
            return None, None, 0.0
        return f"{raw_mm}mm", mm_value / 25.4, 0.70

    return None, None, 0.0


def _extract_pdf_part_hint(text: str) -> Optional[str]:
    patterns = [
        re.compile(r"(?:part\s*(?:no|number)|p\/n|dwg(?:\s*no)?)[\s:#-]*([A-Z0-9._-]{3,})", re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def parse_bom_xlsx(file_path: str, file_name: str) -> Dict[str, Any]:
    from openpyxl import load_workbook

    workbook = load_workbook(file_path, data_only=True)
    part_rows: List[Dict[str, Any]] = []
    hardware_rows: List[Dict[str, Any]] = []

    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue

        header_row_index = None
        header_map: Dict[str, int] = {}
        for idx, row in enumerate(rows[:30]):
            normalized = [str(cell).strip().lower() if cell is not None else "" for cell in row]
            has_qty = any(token in ("qty", "quantity") for token in normalized)
            has_part = any("part" in token or token in ("pn", "p/n") for token in normalized)
            if has_qty and has_part:
                header_row_index = idx
                for col_idx, token in enumerate(normalized):
                    if token:
                        header_map[token] = col_idx
                break

        if header_row_index is None:
            continue

        def _get_value(data_row: Tuple[Any, ...], *candidates: str) -> Any:
            for candidate in candidates:
                for key, col_idx in header_map.items():
                    if candidate in key and col_idx < len(data_row):
                        return data_row[col_idx]
            return None

        for row_idx, row in enumerate(rows[header_row_index + 1 :], start=header_row_index + 2):
            part_number = _get_value(row, "part number", "part", "pn", "p/n")
            description = _get_value(row, "description", "desc", "name")
            qty = _safe_float(_get_value(row, "qty", "quantity")) or 0
            if not part_number and not description:
                continue
            if qty <= 0:
                qty = 1

            material = _get_value(row, "material")
            thickness = _get_value(row, "thickness", "gauge")
            finish = _get_value(row, "finish", "coating")
            item_type = str(_get_value(row, "type", "item type") or "").lower()

            source_ref = f"{file_name}!{sheet.title}:row{row_idx}"
            row_data = {
                "part_number": str(part_number).strip() if part_number else None,
                "part_name": str(description).strip() if description else str(part_number).strip(),
                "qty": qty,
                "material": str(material).strip() if material else None,
                "thickness": str(thickness).strip() if thickness else None,
                "finish": str(finish).strip() if finish else None,
                "notes": str(description).strip() if description else "",
                "source": source_ref,
            }

            combined = " ".join(
                [str(part_number or ""), str(description or ""), item_type]
            ).lower()
            is_hardware = "hardware" in item_type or any(keyword in combined for keyword in HARDWARE_HINTS)
            if is_hardware:
                hardware_rows.append(row_data)
            else:
                part_rows.append(row_data)

    return {"parts": part_rows, "hardware": hardware_rows}


def parse_pdf_drawing(file_path: str, file_name: str) -> Dict[str, Any]:
    result = extract_text_from_pdf(file_path)
    text = (result.text or "").strip()
    material = _detect_material(text)
    thickness_raw, thickness_in, thickness_conf = _parse_thickness(text)
    finish = _detect_finish(text)
    tolerances_flag = bool(re.search(r"(?:\+\/-|±|tolerance)", text, re.IGNORECASE))
    weld_required = bool(re.search(r"(?:\bweld\b|fillet|gmaw|mig|tig)", text, re.IGNORECASE))
    assembly_required = bool(re.search(r"(?:assembly|assy)", text, re.IGNORECASE))
    part_hint = _extract_pdf_part_hint(text) or Path(file_name).stem

    sources: Dict[str, List[str]] = {}
    if material:
        sources["material"] = [f"{file_name}:text"]
    if thickness_raw:
        sources["thickness"] = [f"{file_name}:text"]
    if finish:
        sources["finish"] = [f"{file_name}:text"]

    return {
        "part_hint": part_hint,
        "material": material,
        "thickness": thickness_raw,
        "thickness_in": thickness_in,
        "finish": finish,
        "weld_required": weld_required,
        "assembly_required": assembly_required,
        "tolerances_flag": tolerances_flag,
        "confidence": {
            "material": 0.80 if material else 0.0,
            "thickness": thickness_conf,
            "finish": 0.75 if finish else 0.0,
        },
        "sources": sources,
        "text_length": len(text),
    }


def _poly_area(points: List[Tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def parse_dxf_geometry(file_path: str, file_name: str) -> Dict[str, Any]:
    import ezdxf

    doc = ezdxf.readfile(file_path)
    msp = doc.modelspace()
    entities = list(msp)

    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")
    cut_length = 0.0
    bend_count = 0
    hole_count = 0
    closed_areas: List[float] = []

    def _update_bounds(x: float, y: float) -> None:
        nonlocal min_x, min_y, max_x, max_y
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)

    for entity in entities:
        entity_type = entity.dxftype()
        layer = (entity.dxf.layer or "").lower()
        is_bend = any(token in layer for token in ("bend", "fold", "brake"))

        if entity_type == "LINE":
            start = entity.dxf.start
            end = entity.dxf.end
            _update_bounds(start.x, start.y)
            _update_bounds(end.x, end.y)
            length = math.sqrt((end.x - start.x) ** 2 + (end.y - start.y) ** 2)
            if is_bend:
                bend_count += 1
            else:
                cut_length += length
        elif entity_type == "CIRCLE":
            center = entity.dxf.center
            radius = entity.dxf.radius
            _update_bounds(center.x - radius, center.y - radius)
            _update_bounds(center.x + radius, center.y + radius)
            cut_length += 2 * math.pi * radius
            area = math.pi * radius * radius
            closed_areas.append(area)
            if radius * 2 <= 2.0:
                hole_count += 1
        elif entity_type == "ARC":
            center = entity.dxf.center
            radius = entity.dxf.radius
            start_angle = entity.dxf.start_angle
            end_angle = entity.dxf.end_angle
            _update_bounds(center.x - radius, center.y - radius)
            _update_bounds(center.x + radius, center.y + radius)
            span = end_angle - start_angle
            if span < 0:
                span += 360
            cut_length += 2 * math.pi * radius * (abs(span) / 360.0)
        elif entity_type in ("LWPOLYLINE", "POLYLINE"):
            points: List[Tuple[float, float]] = []
            if entity_type == "LWPOLYLINE":
                points = [(point[0], point[1]) for point in entity.get_points()]
                is_closed = bool(entity.closed)
            else:
                points = [(vertex.dxf.location.x, vertex.dxf.location.y) for vertex in entity.vertices]
                is_closed = bool(entity.is_closed)
            if not points:
                continue
            for x, y in points:
                _update_bounds(x, y)
            length = 0.0
            for idx in range(len(points) - 1):
                length += math.dist(points[idx], points[idx + 1])
            if is_closed and len(points) > 2:
                length += math.dist(points[-1], points[0])
                closed_areas.append(_poly_area(points))
            if is_bend:
                bend_count += 1
            else:
                cut_length += length

    flat_area = None
    if closed_areas:
        flat_area = max(closed_areas)
    elif min_x != float("inf"):
        flat_area = max(max_x - min_x, 0.0) * max(max_y - min_y, 0.0)

    confidence = 0.90 if closed_areas else 0.65 if flat_area else 0.0
    return {
        "part_hint": Path(file_name).stem,
        "flat_area": flat_area,
        "cut_length": cut_length if cut_length > 0 else None,
        "hole_count": hole_count,
        "bend_count": bend_count,
        "bbox": {
            "min_x": None if min_x == float("inf") else min_x,
            "max_x": None if max_x == float("-inf") else max_x,
            "min_y": None if min_y == float("inf") else min_y,
            "max_y": None if max_y == float("-inf") else max_y,
        },
        "confidence": {"geometry": confidence},
        "sources": {"geometry": [f"{file_name}:modelspace"]},
    }


def parse_step_fallback(file_path: str, file_name: str) -> Dict[str, Any]:
    """
    STEP fallback when full geometry parser is unavailable.
    Attempts rough bounding box extraction from CARTESIAN_POINT coordinates.
    """
    content = Path(file_path).read_text(errors="ignore")
    points = re.findall(
        r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-+0-9.Ee]+)\s*,\s*([-+0-9.Ee]+)\s*,\s*([-+0-9.Ee]+)\s*\)\s*\)",
        content,
    )
    if not points:
        return {
            "part_hint": Path(file_name).stem,
            "flat_area": None,
            "cut_length": None,
            "hole_count": None,
            "bend_count": None,
            "bbox": None,
            "confidence": {"geometry": 0.0},
            "low_confidence": True,
            "sources": {"geometry": [f"{file_name}:fallback-none"]},
            "warning": "STEP parsing unavailable. Provide flat pattern DXF for high-confidence estimate.",
        }

    coords = [(float(x), float(y), float(z)) for x, y, z in points]
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    zs = [p[2] for p in coords]
    length = max(xs) - min(xs)
    width = max(ys) - min(ys)
    thickness_guess = max(min(max(zs) - min(zs), max(length, width)), 0.0)
    flat_area = max(length * width, 0.0)

    return {
        "part_hint": Path(file_name).stem,
        "flat_area": flat_area if flat_area > 0 else None,
        "cut_length": 2.0 * (length + width) if length > 0 and width > 0 else None,
        "hole_count": None,
        "bend_count": None,
        "bbox": {"length": length, "width": width, "thickness_guess": thickness_guess},
        "confidence": {"geometry": 0.35},
        "low_confidence": True,
        "sources": {"geometry": [f"{file_name}:cartesian_points"]},
        "warning": "STEP parsed with bounding-box fallback. Confirm thickness and flat pattern DXF before release.",
    }


def build_normalized_part_specs(
    bom_parts: List[Dict[str, Any]],
    pdf_specs: List[Dict[str, Any]],
    dxf_specs: List[Dict[str, Any]],
    step_specs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    parts: Dict[str, Dict[str, Any]] = {}
    assumptions: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    global_sources: Dict[str, List[str]] = {}

    def ensure_part(key: str, default_name: str) -> Dict[str, Any]:
        if key not in parts:
            parts[key] = {
                "part_id": key,
                "part_name": default_name,
                "qty": 1,
                "material": None,
                "thickness": None,
                "thickness_in": None,
                "flat_area": None,
                "cut_length": None,
                "hole_count": None,
                "bend_count": None,
                "finish": None,
                "weld_required": False,
                "assembly_required": False,
                "tolerances_flag": False,
                "notes": "",
                "confidence": {"material": 0.0, "thickness": 0.0, "geometry": 0.0, "finish": 0.0},
                "sources": {},
            }
        return parts[key]

    for row in bom_parts:
        key_seed = row.get("part_number") or row.get("part_name") or f"row-{len(parts)+1}"
        key = _clean_key(key_seed) or f"part-{len(parts)+1}"
        part = ensure_part(key, row.get("part_name") or key_seed)
        part["part_id"] = row.get("part_number") or key
        part["part_name"] = row.get("part_name") or part["part_name"]
        part["qty"] = row.get("qty") or part["qty"]
        part["notes"] = row.get("notes") or part["notes"]
        if row.get("material"):
            part["material"] = row["material"]
            part["confidence"]["material"] = max(part["confidence"]["material"], 0.9)
            part["sources"].setdefault("material", []).append(row["source"])
        if row.get("thickness"):
            raw, inches, conf = _parse_thickness(str(row["thickness"]))
            part["thickness"] = raw or str(row["thickness"])
            part["thickness_in"] = inches
            part["confidence"]["thickness"] = max(part["confidence"]["thickness"], conf)
            part["sources"].setdefault("thickness", []).append(row["source"])
        if row.get("finish"):
            part["finish"] = row["finish"]
            part["confidence"]["finish"] = max(part["confidence"]["finish"], 0.85)
            part["sources"].setdefault("finish", []).append(row["source"])

    def attach_by_hint(payload: Dict[str, Any], field: str) -> Dict[str, Any]:
        hint = _clean_key(payload.get("part_hint") or "")
        if hint and hint in parts:
            return parts[hint]
        if hint:
            for part_key, part_value in parts.items():
                if hint in part_key or part_key in hint:
                    return part_value
        key = hint or f"part-{len(parts)+1}"
        return ensure_part(key, payload.get("part_hint") or key)

    for payload in pdf_specs:
        part = attach_by_hint(payload, "pdf")
        if payload.get("material") and not part.get("material"):
            part["material"] = payload["material"]
            part["confidence"]["material"] = max(part["confidence"]["material"], payload["confidence"]["material"])
            part["sources"].setdefault("material", []).extend(payload["sources"].get("material", []))
        if payload.get("thickness") and not part.get("thickness"):
            part["thickness"] = payload["thickness"]
            part["thickness_in"] = payload.get("thickness_in")
            part["confidence"]["thickness"] = max(part["confidence"]["thickness"], payload["confidence"]["thickness"])
            part["sources"].setdefault("thickness", []).extend(payload["sources"].get("thickness", []))
        if payload.get("finish") and not part.get("finish"):
            part["finish"] = payload["finish"]
            part["confidence"]["finish"] = max(part["confidence"]["finish"], payload["confidence"]["finish"])
            part["sources"].setdefault("finish", []).extend(payload["sources"].get("finish", []))
        part["weld_required"] = part["weld_required"] or payload.get("weld_required", False)
        part["assembly_required"] = part["assembly_required"] or payload.get("assembly_required", False)
        part["tolerances_flag"] = part["tolerances_flag"] or payload.get("tolerances_flag", False)

    for payload in dxf_specs + step_specs:
        part = attach_by_hint(payload, "geometry")
        if payload.get("flat_area") is not None:
            part["flat_area"] = payload["flat_area"]
        if payload.get("cut_length") is not None:
            part["cut_length"] = payload["cut_length"]
        if payload.get("hole_count") is not None:
            part["hole_count"] = payload["hole_count"]
        if payload.get("bend_count") is not None:
            part["bend_count"] = payload["bend_count"]
        part["confidence"]["geometry"] = max(part["confidence"]["geometry"], payload.get("confidence", {}).get("geometry", 0.0))
        part["sources"].setdefault("geometry", []).extend(payload.get("sources", {}).get("geometry", []))
        warning = payload.get("warning")
        if warning:
            assumptions.append(
                {
                    "part_id": part["part_id"],
                    "field": "geometry",
                    "assumption": warning,
                    "confidence": payload.get("confidence", {}).get("geometry", 0.0),
                }
            )

    # Controlled inference: if all resolved materials are identical, apply to unresolved parts.
    known_materials = {part["material"] for part in parts.values() if part.get("material")}
    if len(known_materials) == 1:
        inferred_material = next(iter(known_materials))
        for part in parts.values():
            if part.get("material"):
                continue
            part["material"] = inferred_material
            part["confidence"]["material"] = 0.6
            part["sources"].setdefault("material", []).append("inferred:single-material-across-package")
            assumptions.append(
                {
                    "part_id": part["part_id"],
                    "field": "material",
                    "assumption": f"Inferred {inferred_material} from package-wide consistency.",
                    "confidence": 0.6,
                }
            )

    for part in parts.values():
        required = ("material", "thickness", "flat_area", "cut_length")
        for field in required:
            missing_condition = part.get(field) in (None, "", 0)
            if missing_condition:
                missing.append(
                    {
                        "part_id": part["part_id"],
                        "field": field,
                        "message": f"Missing required {field} for reliable sheet-metal quote.",
                    }
                )
        for field_key, refs in part["sources"].items():
            global_sources.setdefault(field_key, []).extend(refs)

    part_list = sorted(parts.values(), key=lambda item: str(item.get("part_id")))
    return {
        "parts": part_list,
        "assumptions": assumptions,
        "missing_specs": missing,
        "source_attribution": global_sources,
    }


def parse_rfq_package_files(files: List[RfqPackageFile]) -> Dict[str, Any]:
    bom_parts: List[Dict[str, Any]] = []
    hardware_items: List[Dict[str, Any]] = []
    pdf_specs: List[Dict[str, Any]] = []
    dxf_specs: List[Dict[str, Any]] = []
    step_specs: List[Dict[str, Any]] = []
    warnings: List[str] = []
    file_results: Dict[int, Dict[str, Any]] = {}

    for file_record in files:
        ext = (file_record.file_ext or "").lower()
        name = file_record.file_name
        try:
            if ext in (".xlsx", ".xls"):
                bom = parse_bom_xlsx(file_record.file_path, name)
                bom_parts.extend(bom["parts"])
                hardware_items.extend(bom["hardware"])
                file_results[file_record.id] = {
                    "parse_status": "parsed",
                    "summary": {
                        "parts_found": len(bom["parts"]),
                        "hardware_found": len(bom["hardware"]),
                    },
                }
            elif ext == ".pdf":
                pdf = parse_pdf_drawing(file_record.file_path, name)
                pdf_specs.append(pdf)
                file_results[file_record.id] = {
                    "parse_status": "parsed",
                    "summary": {"text_length": pdf["text_length"], "part_hint": pdf["part_hint"]},
                }
            elif ext == ".dxf":
                dxf = parse_dxf_geometry(file_record.file_path, name)
                dxf_specs.append(dxf)
                file_results[file_record.id] = {
                    "parse_status": "parsed",
                    "summary": {"flat_area": dxf["flat_area"], "cut_length": dxf["cut_length"]},
                }
            elif ext in (".step", ".stp"):
                step = parse_step_fallback(file_record.file_path, name)
                step_specs.append(step)
                file_results[file_record.id] = {
                    "parse_status": "parsed_with_fallback",
                    "summary": {"flat_area": step.get("flat_area"), "warning": step.get("warning")},
                }
            else:
                file_results[file_record.id] = {
                    "parse_status": "skipped",
                    "summary": {"reason": f"Unsupported extension {ext}"},
                }
        except Exception as exc:
            warnings.append(f"{name}: parse failed ({exc})")
            file_results[file_record.id] = {
                "parse_status": "error",
                "parse_error": str(exc),
            }

    normalized = build_normalized_part_specs(bom_parts, pdf_specs, dxf_specs, step_specs)
    return {
        "parts": normalized["parts"],
        "hardware_items": hardware_items,
        "assumptions": normalized["assumptions"],
        "missing_specs": normalized["missing_specs"],
        "source_attribution": normalized["source_attribution"],
        "warnings": warnings,
        "file_results": file_results,
    }
