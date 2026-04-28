"""
Routing Generation Service
Analyzes uploaded drawings (PDF, DXF, STEP) and proposes draft manufacturing routings
by extracting operations from drawing callouts and mapping them to work centers.
"""

import os
import json
import logging
import re
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

# Fallback work center types. Runtime generation receives the tenant-configured
# list from work_center_type_service plus in-use work center types.
DEFAULT_WORK_CENTER_TYPES = [
    "fabrication",
    "laser",
    "press_brake",
    "cnc_machining",
    "welding",
    "paint",
    "powder_coating",
    "assembly",
    "inspection",
    "shipping",
]

WORK_CENTER_ALIAS_GROUPS = {
    "cnc_machining": ["cnc", "machining", "machine", "milling", "turning", "lathe"],
    "press_brake": ["press", "brake", "bend", "bending", "form", "forming"],
    "powder_coating": ["powder", "powder_coat", "powdercoat", "coating"],
    "welding": ["weld"],
    "inspection": ["inspect", "qc", "quality", "quality_control", "final_inspection"],
    "shipping": ["ship", "pack", "packaging"],
    "assembly": ["assemble", "final_assembly"],
    "laser": ["cut", "cutting", "laser_cut", "laser_cutting"],
    "waterjet": ["water_jet", "waterjet_cut", "water_jet_cut"],
    "plasma": ["plasma_cut", "plasma_cutting"],
    "punch_press": ["punch", "punching", "turret", "turret_punch"],
    "saw": ["sawing", "saw_cut", "cutoff", "cut_off"],
    "deburr": ["deburring", "grind", "grinding"],
    "drilling": ["drill"],
    "tapping": ["tap", "thread", "threading"],
    "hardware": ["pem", "insert", "inserts", "fastener", "fasteners"],
    "kitting": ["kit", "pick", "picking"],
    "paint": ["painting"],
}


def normalize_work_center_type(value: str) -> str:
    """Normalize a user/model-provided work center type into the app slug shape."""
    if not value:
        return ""
    val = value.strip().lower()
    val = re.sub(r"[^a-z0-9\s_-]", "", val)
    val = re.sub(r"[\s-]+", "_", val)
    return val.strip("_")


def dedupe_work_center_types(types: Optional[List[str]]) -> List[str]:
    """Normalize/de-dupe work center type slugs while preserving configured order."""
    seen = set()
    result = []
    for wc_type in types or []:
        normalized = normalize_work_center_type(str(wc_type))
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def build_routing_extraction_schema(work_center_types: List[str]) -> str:
    types_str = ", ".join(work_center_types)
    return """
{
  "part_info": {
    "material": "string or null - e.g. Aluminum 6061, Carbon Steel A36, 304 Stainless",
    "thickness": "string or null - e.g. 0.125in, 16ga, 3mm",
    "finish": "string or null - e.g. powder coat, paint, anodize, passivate, zinc plate",
    "tolerances_noted": "boolean - true if tight tolerances or GD&T callouts are present",
    "weld_required": "boolean - true if any welding is called out",
    "assembly_required": "boolean - true if this is an assembly with multiple components"
  },
  "operations": [
    {
      "sequence": "integer - 10, 20, 30, etc.",
      "operation_name": "string - e.g. Laser Cut, Press Brake Form, CNC Machine, Weld, Powder Coat, Assemble, Final Inspection",
      "work_center_type": "string - one of: WORK_CENTER_TYPES",
      "description": "string - brief description of what this operation does for this part",
      "is_inspection_point": "boolean - true if this is an inspection/QC step",
      "is_outside_operation": "boolean - true if this would typically be sent to an outside vendor (e.g. plating, heat treat, anodize)",
      "tooling_requirements": "string or null - any tooling, fixtures, or dies noted",
      "work_instructions": "string or null - key instructions from the drawing for this operation",
      "confidence": "high, medium, or low"
    }
  ],
  "extraction_confidence": "high, medium, or low - overall confidence in the proposed routing"
}
""".replace("WORK_CENTER_TYPES", types_str)

ROUTING_SYSTEM_PROMPT = """You are a manufacturing process engineer assistant specialized in sheet metal fabrication, CNC machining, welding, and general manufacturing. Your task is to analyze engineering drawing content and propose a manufacturing routing (sequence of operations).

Key guidelines:
1. Analyze the drawing text and any geometry data provided to determine the manufacturing operations needed
2. Sequence operations in logical manufacturing order (cut -> form -> weld -> finish -> inspect -> ship)
3. Map each operation to exactly one of the allowed work_center_type values
4. Include inspection operations where quality checks are needed (after critical operations, before shipping)
5. Mark outside operations appropriately (anodizing, plating, heat treating are typically outside)
6. For sheet metal parts: typical flow is cutting -> forming -> welding (if needed) -> finish -> inspect
7. For machined parts: typical flow is machining -> deburr -> finish -> inspect
8. For assemblies: include Assembly and Final Inspection steps
9. Always end with a Final Inspection operation and Shipping
10. If the drawing mentions specific processes, include them; if not, infer from geometry and material
11. Set confidence based on how clearly the drawing calls out each operation

Return ONLY valid JSON matching the schema. No explanations or markdown."""


# Default time estimates by work center type (in hours)
DEFAULT_TIME_ESTIMATES = {
    "laser": {"setup_hours": 0.20, "run_hours_per_unit": 0.08},
    "waterjet": {"setup_hours": 0.25, "run_hours_per_unit": 0.10},
    "plasma": {"setup_hours": 0.20, "run_hours_per_unit": 0.08},
    "punch_press": {"setup_hours": 0.20, "run_hours_per_unit": 0.07},
    "saw": {"setup_hours": 0.10, "run_hours_per_unit": 0.05},
    "press_brake": {"setup_hours": 0.15, "run_hours_per_unit": 0.05},
    "cnc_machining": {"setup_hours": 0.50, "run_hours_per_unit": 0.25},
    "deburr": {"setup_hours": 0.05, "run_hours_per_unit": 0.08},
    "drilling": {"setup_hours": 0.15, "run_hours_per_unit": 0.08},
    "tapping": {"setup_hours": 0.15, "run_hours_per_unit": 0.06},
    "welding": {"setup_hours": 0.25, "run_hours_per_unit": 0.20},
    "assembly": {"setup_hours": 0.10, "run_hours_per_unit": 0.17},
    "final_assembly": {"setup_hours": 0.10, "run_hours_per_unit": 0.17},
    "kitting": {"setup_hours": 0.05, "run_hours_per_unit": 0.05},
    "hardware": {"setup_hours": 0.05, "run_hours_per_unit": 0.06},
    "fabrication": {"setup_hours": 0.20, "run_hours_per_unit": 0.10},
    "paint": {"setup_hours": 0.10, "run_hours_per_unit": 0.08},
    "powder_coating": {"setup_hours": 0.10, "run_hours_per_unit": 0.08},
    "inspection": {"setup_hours": 0.0, "run_hours_per_unit": 0.08},
    "shipping": {"setup_hours": 0.0, "run_hours_per_unit": 0.05},
}

# Cutting speed in inches per minute (used when DXF cut_length is available)
CUT_SPEED_IPM = 200.0
# Seconds per bend (used when DXF bend_count is available)
SECONDS_PER_BEND = 30.0
# Seconds per hole for drilling/punching
SECONDS_PER_HOLE = 15.0


def extract_routing_data_with_llm(
    drawing_text: str,
    geometry: Optional[Dict[str, Any]] = None,
    work_center_types: Optional[List[str]] = None,
    is_ocr: bool = False,
) -> Dict[str, Any]:
    """
    Send drawing text and optional geometry data to Claude to propose routing operations.
    Returns structured JSON with proposed operations mapped to work center types.
    """
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic package not installed")
        return _create_empty_routing_result("LLM library not available")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return _create_empty_routing_result("API key not configured")

    types_list = dedupe_work_center_types(work_center_types) or DEFAULT_WORK_CENTER_TYPES
    types_str = ", ".join(types_list)
    schema = build_routing_extraction_schema(types_list)

    geometry_context = ""
    if geometry:
        parts = []
        if geometry.get("cut_length"):
            parts.append(f"Cut length: {geometry['cut_length']:.1f} inches")
        if geometry.get("hole_count"):
            parts.append(f"Holes: {geometry['hole_count']}")
        if geometry.get("bend_count"):
            parts.append(f"Bends: {geometry['bend_count']}")
        if geometry.get("flat_area"):
            parts.append(f"Flat area: {geometry['flat_area']:.1f} sq inches")
        bbox = geometry.get("bbox", {})
        if bbox and bbox.get("min_x") is not None:
            width = (bbox.get("max_x", 0) or 0) - (bbox.get("min_x", 0) or 0)
            height = (bbox.get("max_y", 0) or 0) - (bbox.get("min_y", 0) or 0)
            parts.append(f"Bounding box: {width:.1f} x {height:.1f} inches")
        if parts:
            geometry_context = f"\n\nGeometry data extracted from the drawing file:\n- " + "\n- ".join(parts)

    ocr_note = (
        "\n\nNote: This text was extracted via OCR and may contain errors."
        if is_ocr
        else ""
    )

    user_prompt = f"""Analyze the following engineering drawing content and propose a manufacturing routing (sequence of operations).

Return JSON matching this schema exactly:
{schema}

Allowed work_center_type values: {types_str}

Important:
- Propose operations in logical manufacturing sequence
- Each operation must use one of the allowed work_center_type values listed above
- Include inspection and shipping steps
- If the drawing shows a sheet metal part with cut profiles, start with the best available cutting work center type
- If bends are present, include the best available forming/press brake work center type
- If welding symbols or weld callouts are present, include the best available welding work center type
- If a finish is specified (powder coat, paint, anodize, etc.), include the appropriate finish operation
- Mark anodizing, plating, and heat treating as outside operations
{geometry_context}
{ocr_note}

Drawing Content:
---
{drawing_text[:8000]}
---

Return ONLY the JSON object, no other text."""

    try:
        client = anthropic.Anthropic(api_key=api_key)

        model = os.getenv("ANTHROPIC_ROUTING_MODEL", "claude-sonnet-4-20250514")
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_prompt}],
            system=ROUTING_SYSTEM_PROMPT,
        )

        response_text = message.content[0].text.strip()

        # Strip markdown fences if present
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        result = json.loads(response_text.strip())
        result["_extraction_metadata"] = {
            "extracted_at": datetime.utcnow().isoformat(),
            "source_was_ocr": is_ocr,
            "model": model,
        }

        logger.info(
            f"LLM routing extraction successful: {len(result.get('operations', []))} operations proposed"
        )
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM routing response as JSON: {e}")
        return _create_empty_routing_result(f"Invalid JSON response: {str(e)}")
    except Exception as e:
        logger.error(f"LLM routing extraction failed: {e}")
        return _create_empty_routing_result(f"Extraction failed: {str(e)}")


def map_operations_to_work_centers(
    proposed_operations: List[Dict[str, Any]],
    work_centers_by_type: Dict[str, List[Dict[str, Any]]],
) -> tuple[List[Dict[str, Any]], List[str]]:
    """
    Map each proposed operation's work_center_type to an actual work center.
    Returns (operations_with_ids, warnings).
    """
    warnings: List[str] = []
    result = []

    # Build a normalized lookup: any accepted alias -> canonical type/work centers.
    normalized: Dict[str, Dict[str, Any]] = {}

    def add_lookup(alias: str, wc_type: str, wcs: List[Dict[str, Any]], replace: bool = False) -> None:
        key = normalize_work_center_type(alias)
        if not key:
            return
        if replace or key not in normalized:
            normalized[key] = {
                "type": normalize_work_center_type(wc_type),
                "work_centers": wcs,
            }

    for wc_type, wcs in work_centers_by_type.items():
        normalized_type = normalize_work_center_type(wc_type)
        if not normalized_type:
            continue
        add_lookup(normalized_type, normalized_type, wcs, replace=True)
        add_lookup(normalized_type.replace("_", ""), normalized_type, wcs)

        aliases = list(WORK_CENTER_ALIAS_GROUPS.get(normalized_type, []))
        for canonical_type, canonical_aliases in WORK_CENTER_ALIAS_GROUPS.items():
            if any(token in normalized_type for token in canonical_aliases + [canonical_type]):
                aliases.extend(canonical_aliases)
        for alias in aliases:
            add_lookup(alias, normalized_type, wcs)

    for op in proposed_operations:
        op_type = normalize_work_center_type(op.get("work_center_type") or "")
        match = normalized.get(op_type)

        if not match:
            searchable_text = " ".join(
                str(op.get(field) or "")
                for field in ("operation_name", "description", "tooling_requirements", "work_instructions")
            )
            searchable_slug = normalize_work_center_type(searchable_text)
            for alias, candidate in normalized.items():
                if alias and alias in searchable_slug:
                    match = candidate
                    break

        matched_wcs = match["work_centers"] if match else None
        if matched_wcs and len(matched_wcs) > 0:
            wc = matched_wcs[0]  # Pick the first active work center of this type
            op["work_center_type"] = match["type"]
            op["work_center_id"] = wc["id"]
            op["work_center_name"] = wc["name"]
        else:
            op["work_center_id"] = None
            op["work_center_name"] = None
            warnings.append(
                f"Operation '{op.get('operation_name', '?')}': No active work center found for type '{op_type or 'unknown'}'. Please select one manually."
            )

        result.append(op)

    return result, warnings


def estimate_operation_times(
    operation: Dict[str, Any],
    geometry: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """
    Generate deterministic time estimates for an operation based on type and geometry.
    Returns dict with setup_hours and run_hours_per_unit.
    """
    wc_type = (operation.get("work_center_type") or "").lower()
    defaults = DEFAULT_TIME_ESTIMATES.get(wc_type, {"setup_hours": 0.15, "run_hours_per_unit": 0.10})

    setup_hours = defaults["setup_hours"]
    run_hours_per_unit = defaults["run_hours_per_unit"]

    if geometry:
        cut_length = geometry.get("cut_length")
        hole_count = geometry.get("hole_count") or 0
        bend_count = geometry.get("bend_count") or 0

        if wc_type in {"laser", "waterjet", "plasma"} and cut_length:
            # Run time based on actual cut length
            run_hours_per_unit = (cut_length / CUT_SPEED_IPM) / 60.0
            # Add time for holes (pierce time)
            run_hours_per_unit += (hole_count * SECONDS_PER_HOLE) / 3600.0

        elif wc_type == "punch_press" and hole_count > 0:
            run_hours_per_unit = (hole_count * SECONDS_PER_HOLE) / 3600.0

        elif wc_type == "press_brake" and bend_count > 0:
            # Run time based on bend count
            run_hours_per_unit = (bend_count * SECONDS_PER_BEND) / 3600.0

    return {
        "setup_hours": round(setup_hours, 4),
        "run_hours_per_unit": round(run_hours_per_unit, 4),
    }


def generate_draft_routing(
    drawing_text: str,
    geometry: Optional[Dict[str, Any]],
    work_centers_by_type: Dict[str, List[Dict[str, Any]]],
    is_ocr: bool = False,
    work_center_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Orchestrator: extract operations via LLM, map to work centers, estimate times.
    Returns the full routing proposal.
    """
    available_types = (
        dedupe_work_center_types(work_center_types)
        or dedupe_work_center_types(list(work_centers_by_type.keys()))
        or DEFAULT_WORK_CENTER_TYPES
    )

    # Step 1: LLM extraction
    llm_result = extract_routing_data_with_llm(
        drawing_text=drawing_text,
        geometry=geometry,
        work_center_types=available_types if available_types else None,
        is_ocr=is_ocr,
    )

    if llm_result.get("_error"):
        return llm_result

    proposed_ops = llm_result.get("operations", [])
    part_info = llm_result.get("part_info", {})

    # Step 2: Map to work centers
    mapped_ops, wc_warnings = map_operations_to_work_centers(
        proposed_ops, work_centers_by_type
    )

    # Step 3: Estimate times for each operation
    for op in mapped_ops:
        times = estimate_operation_times(op, geometry)
        op["setup_hours"] = times["setup_hours"]
        op["run_hours_per_unit"] = times["run_hours_per_unit"]

    return {
        "part_info": part_info,
        "operations": mapped_ops,
        "extraction_confidence": llm_result.get("extraction_confidence", "medium"),
        "warnings": wc_warnings,
        "_extraction_metadata": llm_result.get("_extraction_metadata", {}),
    }


def _create_empty_routing_result(error_message: str) -> Dict[str, Any]:
    """Create an empty routing result with error message."""
    return {
        "part_info": {
            "material": None,
            "thickness": None,
            "finish": None,
            "tolerances_noted": False,
            "weld_required": False,
            "assembly_required": False,
        },
        "operations": [],
        "extraction_confidence": "low",
        "_error": error_message,
        "_extraction_metadata": {
            "extracted_at": datetime.utcnow().isoformat(),
            "source_was_ocr": False,
            "model": None,
        },
    }
