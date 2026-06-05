"""
Routing Generation Service
Analyzes uploaded drawings (PDF, DXF, STEP) and proposes draft manufacturing routings
by extracting operations from drawing callouts and mapping them to work centers.
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.llm_model_router import LLMTaskContext, select_anthropic_model

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
    part_context: Optional[str] = None,
    is_assembly: bool = False,
    learned_examples_context: Optional[str] = None,
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
            geometry_context = "\n\nGeometry data extracted from the drawing file:\n- " + "\n- ".join(parts)

    ocr_note = "\n\nNote: This text was extracted via OCR and may contain errors." if is_ocr else ""
    context_note = f"\n\nERP part context:\n{part_context}" if part_context else ""
    learned_examples_note = (
        f"\n\nSimilar approved routings from this company:\n{learned_examples_context}"
        if learned_examples_context
        else ""
    )
    assembly_note = (
        "\n\nThe selected ERP part is an assembly. Include assembly/build operations if an allowed assembly-type work center exists."
        if is_assembly
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
{context_note}
{learned_examples_note}
{geometry_context}
{ocr_note}
{assembly_note}

Drawing Content:
---
{drawing_text[:8000]}
---

Return ONLY the JSON object, no other text."""

    try:
        client = anthropic.Anthropic(api_key=api_key)

        model_decision = select_anthropic_model(
            LLMTaskContext(
                task="routing_generation",
                input_chars=len(drawing_text),
                is_ocr=is_ocr,
                geometry=geometry,
                learned_examples=bool(learned_examples_context),
                is_assembly=is_assembly,
            )
        )
        message = client.messages.create(
            model=model_decision.model,
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
            "model": model_decision.model,
            "model_tier": model_decision.tier.value,
            "model_selection_reason": model_decision.reason,
        }

        logger.info(
            "LLM routing extraction successful: %s operations proposed using %s",
            len(result.get("operations", [])),
            model_decision.model,
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
    learned_aliases: Optional[List[Dict[str, Any]]] = None,
    preferred_work_center_ids: Optional[Dict[str, List[int]]] = None,
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

    for learned_alias in learned_aliases or []:
        alias = learned_alias.get("alias")
        wc_type = normalize_work_center_type(learned_alias.get("work_center_type") or "")
        wcs = work_centers_by_type.get(wc_type)
        if alias and wcs:
            add_lookup(alias, wc_type, wcs, replace=True)

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
            preferred_ids = (preferred_work_center_ids or {}).get(match["type"], [])
            wc = _choose_best_work_center(op, matched_wcs, preferred_ids=preferred_ids)
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


def _choose_best_work_center(
    operation: Dict[str, Any],
    work_centers: List[Dict[str, Any]],
    preferred_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Prefer the active work center whose name/code best matches the operation text."""
    searchable_text = normalize_work_center_type(
        " ".join(
            str(operation.get(field) or "")
            for field in ("operation_name", "description", "tooling_requirements", "work_instructions")
        )
    )
    if not searchable_text:
        return work_centers[0]

    def score(work_center: Dict[str, Any]) -> int:
        wc_text = normalize_work_center_type(f"{work_center.get('code', '')} {work_center.get('name', '')}")
        preference_score = 0
        if preferred_ids and work_center.get("id") in preferred_ids:
            preference_score = 100 - preferred_ids.index(work_center["id"])
        if not wc_text:
            return preference_score
        wc_tokens = [token for token in wc_text.split("_") if len(token) > 1]
        return preference_score + sum(1 for token in wc_tokens if token in searchable_text)

    return max(enumerate(work_centers), key=lambda item: (score(item[1]), -item[0]))[1]


def _type_aliases(wc_type: str) -> set[str]:
    normalized = normalize_work_center_type(wc_type)
    aliases = {normalized, normalized.replace("_", "")}
    aliases.update(normalize_work_center_type(alias) for alias in WORK_CENTER_ALIAS_GROUPS.get(normalized, []))
    for canonical_type, canonical_aliases in WORK_CENTER_ALIAS_GROUPS.items():
        canonical = normalize_work_center_type(canonical_type)
        normalized_aliases = {normalize_work_center_type(alias) for alias in canonical_aliases}
        if normalized == canonical or normalized in normalized_aliases:
            aliases.add(canonical)
            aliases.update(normalized_aliases)
    return {alias for alias in aliases if alias}


def _types_match(desired_type: str, available_type: str) -> bool:
    desired_aliases = _type_aliases(desired_type)
    available_aliases = _type_aliases(available_type)
    if desired_aliases & available_aliases:
        return True
    desired = normalize_work_center_type(desired_type)
    available = normalize_work_center_type(available_type)
    return bool(desired and available and (desired in available or available in desired))


def _find_available_work_center_type(candidates: List[str], available_types: List[str]) -> Optional[str]:
    normalized_available = dedupe_work_center_types(available_types)
    for candidate in candidates:
        for available_type in normalized_available:
            if _types_match(candidate, available_type):
                return available_type
    return None


def _drawing_mentions(text: str, tokens: List[str]) -> bool:
    lower_text = (text or "").lower()
    normalized_text = normalize_work_center_type(text or "")
    for token in tokens:
        lower_token = token.lower()
        normalized_token = normalize_work_center_type(token)
        if lower_token and lower_token in lower_text:
            return True
        if normalized_token and normalized_token in normalized_text:
            return True
    return False


def _infer_part_info_from_drawing(
    drawing_text: str,
    geometry: Optional[Dict[str, Any]] = None,
    is_assembly: bool = False,
) -> Dict[str, Any]:
    text = drawing_text or ""
    lower_text = text.lower()

    material = None
    material_patterns = [
        (r"\b(?:al|alum(?:inum)?)\s*[- ]?(5052|6061|7075|3003)?\b", "Aluminum"),
        (r"\b(?:ss|stainless)\s*[- ]?(304|316|17-4)?\b", "Stainless Steel"),
        (r"\b(?:carbon\s+steel|mild\s+steel|a36|crs|hrs)\b", "Carbon Steel"),
        (r"\b(?:brass|copper|delrin|acetal)\b", None),
    ]
    for pattern, label in material_patterns:
        match = re.search(pattern, lower_text)
        if match:
            material = label or match.group(0).strip().title()
            if label and match.groups() and match.group(1):
                material = f"{label} {match.group(1).upper()}"
            break

    thickness = None
    thickness_match = re.search(
        r"\b(\d+(?:\.\d+)?\s*(?:ga|gauge|mm|in|inch|inches)|\.\d{2,4}\s*(?:in|inch|inches)?)\b",
        lower_text,
    )
    if thickness_match:
        thickness = thickness_match.group(1).strip()

    finish = None
    finish_tokens = [
        ("powder coat", "Powder Coat"),
        ("powdercoat", "Powder Coat"),
        ("paint", "Paint"),
        ("anodize", "Anodize"),
        ("anodized", "Anodize"),
        ("plating", "Plate"),
        ("plate", "Plate"),
        ("passivate", "Passivate"),
        ("zinc", "Zinc Plate"),
    ]
    for token, label in finish_tokens:
        if token in lower_text:
            finish = label
            break

    return {
        "material": material,
        "thickness": thickness,
        "finish": finish,
        "tolerances_noted": _drawing_mentions(
            text,
            ["tolerance", "true position", "flatness", "profile", "gd&t", "gdt", "critical", "inspect", "±"],
        ),
        "weld_required": _drawing_mentions(text, ["weld", "welding", "fillet", "spot weld", "mig", "tig"]),
        "assembly_required": is_assembly
        or _drawing_mentions(text, ["assembly", "assemble", "assy", "weldment", "bom", "install", "pem", "hardware"]),
    }


def _operation_family_present(
    operations: List[Dict[str, Any]], family_types: List[str], family_words: List[str]
) -> bool:
    for operation in operations:
        op_type = normalize_work_center_type(operation.get("work_center_type") or "")
        if any(_types_match(family_type, op_type) for family_type in family_types):
            return True
        searchable = " ".join(
            str(operation.get(field) or "") for field in ("operation_name", "description", "tooling_requirements")
        )
        if _drawing_mentions(searchable, family_words):
            return True
    return False


def _renumber_operations(operations: List[Dict[str, Any]]) -> None:
    operations.sort(key=lambda operation: int(operation.get("sequence") or 0))
    for index, operation in enumerate(operations, start=1):
        operation["sequence"] = index * 10


def _add_operation_if_available(
    operations: List[Dict[str, Any]],
    available_types: List[str],
    candidates: List[str],
    operation_name: str,
    description: str,
    work_instructions: str,
    *,
    family_key: str,
    is_inspection_point: bool = False,
    is_outside_operation: bool = False,
    confidence: str = "medium",
) -> bool:
    if any(operation.get("_family_key") == family_key for operation in operations):
        return False

    wc_type = _find_available_work_center_type(candidates, available_types)
    if not wc_type:
        return False

    operations.append(
        {
            "sequence": (len(operations) + 1) * 10,
            "operation_name": operation_name,
            "work_center_type": wc_type,
            "description": description,
            "is_inspection_point": is_inspection_point,
            "is_outside_operation": is_outside_operation,
            "tooling_requirements": None,
            "work_instructions": work_instructions,
            "confidence": confidence,
            "_family_key": family_key,
        }
    )
    return True


def infer_operations_from_drawing(
    drawing_text: str,
    geometry: Optional[Dict[str, Any]],
    part_info: Optional[Dict[str, Any]],
    available_types: List[str],
    is_assembly: bool = False,
) -> List[Dict[str, Any]]:
    """Create a deterministic routing draft from geometry/text using only available work center types."""
    text = drawing_text or ""
    info = {**_infer_part_info_from_drawing(text, geometry, is_assembly=is_assembly), **(part_info or {})}
    operations: List[Dict[str, Any]] = []

    cut_required = bool(geometry and geometry.get("cut_length")) or _drawing_mentions(
        text, ["laser", "waterjet", "plasma", "cut", "flat pattern", "profile", "blank"]
    )
    holes_required = bool(geometry and geometry.get("hole_count")) or _drawing_mentions(
        text, ["hole", "drill", "pierce"]
    )
    bends_required = bool(geometry and geometry.get("bend_count")) or _drawing_mentions(
        text, ["bend", "forming", "form", "press brake", "brake"]
    )
    hardware_required = _drawing_mentions(text, ["pem", "insert", "stud", "standoff", "nutsert", "rivet", "hardware"])
    tapping_required = _drawing_mentions(text, ["tap", "tapped", "thread", "threaded"])
    deburr_required = _drawing_mentions(text, ["deburr", "break edges", "break all edges", "remove burr"])
    machining_required = _drawing_mentions(text, ["machine", "mill", "turn", "lathe", "counterbore", "countersink"])
    finish = str(info.get("finish") or "")

    if cut_required:
        _add_operation_if_available(
            operations,
            available_types,
            ["laser", "waterjet", "plasma", "punch_press", "saw", "fabrication"],
            "Cut Profile",
            "Cut the blank/profile from the drawing geometry.",
            "Verify material, thickness, drawing revision, and cut profile before releasing the first piece.",
            family_key="cutting",
        )

    if holes_required and not cut_required and not machining_required:
        _add_operation_if_available(
            operations,
            available_types,
            ["drilling", "punch_press", "cnc_machining", "fabrication"],
            "Drill/Punch Holes",
            "Create hole pattern called out by the drawing.",
            "Confirm hole sizes and locations against the drawing before running the lot.",
            family_key="holes",
        )

    if machining_required:
        _add_operation_if_available(
            operations,
            available_types,
            ["cnc_machining", "machining", "drilling", "tapping", "fabrication"],
            "Machine Features",
            "Machine drawing features that require controlled material removal.",
            "Machine all noted features to drawing dimensions and verify critical dimensions on the first piece.",
            family_key="machining",
        )

    if tapping_required:
        _add_operation_if_available(
            operations,
            available_types,
            ["tapping", "cnc_machining", "drilling", "fabrication"],
            "Tap Threads",
            "Tap/thread holes called out on the drawing.",
            "Verify thread size, depth, and go/no-go gauge requirements before moving to the next operation.",
            family_key="tapping",
        )

    if deburr_required or cut_required or machining_required:
        _add_operation_if_available(
            operations,
            available_types,
            ["deburr", "fabrication"],
            "Deburr",
            "Remove burrs and break sharp edges as required.",
            "Deburr cut and machined edges without changing controlled dimensions or cosmetic finish requirements.",
            family_key="deburr",
            confidence="medium" if deburr_required else "low",
        )

    if bends_required:
        _add_operation_if_available(
            operations,
            available_types,
            ["press_brake", "bend", "forming", "fabrication"],
            "Form Bends",
            "Form bends/features called out by the drawing.",
            "Confirm bend direction, angle, and first-piece dimensions before completing the run.",
            family_key="forming",
        )

    if hardware_required:
        _add_operation_if_available(
            operations,
            available_types,
            ["hardware", "assembly", "fabrication"],
            "Install Hardware",
            "Install inserts, studs, fasteners, or other hardware called out on the drawing.",
            "Verify hardware type, orientation, flushness, and quantity against the drawing/BOM.",
            family_key="hardware",
        )

    if info.get("weld_required"):
        _add_operation_if_available(
            operations,
            available_types,
            ["welding", "weld", "fabrication"],
            "Weld Assembly",
            "Weld components/features per drawing symbols and notes.",
            "Verify fit-up, weld size, location, and distortion control requirements before final inspection.",
            family_key="welding",
        )

    if finish:
        lower_finish = finish.lower()
        finish_candidates = ["powder_coating", "paint", "finishing", "outside_processing", "fabrication"]
        is_outside = any(token in lower_finish for token in ["anod", "plate", "plating", "passivat", "heat treat"])
        operation_name = finish if is_outside else f"{finish} Finish"
        _add_operation_if_available(
            operations,
            available_types,
            finish_candidates,
            operation_name,
            f"Apply specified finish: {finish}.",
            "Process finish per drawing/customer requirements. Protect critical surfaces and verify color/specification.",
            family_key="finishing",
            is_outside_operation=is_outside,
        )

    if info.get("assembly_required"):
        _add_operation_if_available(
            operations,
            available_types,
            ["assembly", "final_assembly", "fabrication"],
            "Assemble",
            "Build the assembly using the drawing, BOM, and required components.",
            "Verify component revisions, quantities, orientation, and fit before sending to final inspection.",
            family_key="assembly",
        )

    _add_operation_if_available(
        operations,
        available_types,
        ["inspection", "quality", "quality_control", "final_inspection"],
        "Final Inspection",
        "Final quality verification against the drawing and routing.",
        "Inspect dimensions, finish, hardware, welds, and assembly requirements before release.",
        family_key="inspection",
        is_inspection_point=True,
        confidence="high" if info.get("tolerances_noted") else "medium",
    )

    _add_operation_if_available(
        operations,
        available_types,
        ["shipping", "packaging", "pack"],
        "Pack/Ship",
        "Package the completed part or assembly for shipment.",
        "Package to protect finish/features and attach required traveler, labels, and documentation.",
        family_key="shipping",
        confidence="medium",
    )

    _renumber_operations(operations)
    for operation in operations:
        operation.pop("_family_key", None)
    return operations


def ensure_current_work_center_completion_steps(
    operations: List[Dict[str, Any]],
    drawing_text: str,
    geometry: Optional[Dict[str, Any]],
    part_info: Dict[str, Any],
    available_types: List[str],
    is_assembly: bool = False,
) -> List[Dict[str, Any]]:
    """Append missing assembly/inspection/shipping steps only when the company has matching types."""
    additions: List[Dict[str, Any]] = []
    info = {**_infer_part_info_from_drawing(drawing_text, geometry, is_assembly=is_assembly), **(part_info or {})}

    if info.get("assembly_required") and not _operation_family_present(
        operations, ["assembly", "final_assembly"], ["assembly", "assemble", "build"]
    ):
        _add_operation_if_available(
            additions,
            available_types,
            ["assembly", "final_assembly", "fabrication"],
            "Assemble",
            "Build the assembly using the drawing, BOM, and required components.",
            "Verify component revisions, quantities, orientation, and fit before sending to final inspection.",
            family_key="assembly",
            confidence="medium",
        )

    if not _operation_family_present(
        operations + additions,
        ["inspection", "quality", "quality_control", "final_inspection"],
        ["inspection", "inspect", "quality", "qc"],
    ):
        _add_operation_if_available(
            additions,
            available_types,
            ["inspection", "quality", "quality_control", "final_inspection"],
            "Final Inspection",
            "Final quality verification against the drawing and routing.",
            "Inspect dimensions, finish, hardware, welds, and assembly requirements before release.",
            family_key="inspection",
            is_inspection_point=True,
            confidence="medium",
        )

    if not _operation_family_present(
        operations + additions,
        ["shipping", "packaging", "pack"],
        ["shipping", "ship", "pack", "packaging"],
    ):
        _add_operation_if_available(
            additions,
            available_types,
            ["shipping", "packaging", "pack"],
            "Pack/Ship",
            "Package the completed part or assembly for shipment.",
            "Package to protect finish/features and attach required traveler, labels, and documentation.",
            family_key="shipping",
            confidence="medium",
        )

    if additions:
        operations = operations + additions
        _renumber_operations(operations)

    for operation in operations:
        operation.pop("_family_key", None)
    return operations


def _operations_from_learned_patterns(learned_patterns: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Convert the strongest learned pattern into proposed operations."""
    if not learned_patterns:
        return []

    pattern = learned_patterns[0]
    operations = []
    for index, operation in enumerate(pattern.get("operations") or []):
        operation_name = operation.get("operation_name") or operation.get("name")
        work_center_type = operation.get("work_center_type")
        if not operation_name or not work_center_type:
            continue
        operations.append(
            {
                "sequence": operation.get("sequence") or (index + 1) * 10,
                "operation_name": operation_name,
                "work_center_type": work_center_type,
                "description": operation.get("description"),
                "is_inspection_point": bool(operation.get("is_inspection_point")),
                "is_outside_operation": bool(operation.get("is_outside_operation")),
                "tooling_requirements": operation.get("tooling_requirements"),
                "work_instructions": operation.get("work_instructions"),
                "confidence": "medium",
            }
        )
    _renumber_operations(operations)
    return operations


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
    part_context: Optional[str] = None,
    is_assembly: bool = False,
    learned_aliases: Optional[List[Dict[str, Any]]] = None,
    learned_patterns: Optional[List[Dict[str, Any]]] = None,
    preferred_work_center_ids: Optional[Dict[str, List[int]]] = None,
    learned_examples_context: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orchestrator: extract operations via LLM, map to work centers, estimate times.
    Returns the full routing proposal.
    """
    active_work_center_types = dedupe_work_center_types(list(work_centers_by_type.keys()))
    available_types = (
        dedupe_work_center_types(work_center_types) or active_work_center_types or DEFAULT_WORK_CENTER_TYPES
    )
    automatic_types = active_work_center_types or available_types
    drawing_context_text = "\n".join(part for part in [part_context or "", drawing_text or ""] if part)

    # Step 1: LLM extraction
    llm_result = extract_routing_data_with_llm(
        drawing_text=drawing_text,
        geometry=geometry,
        work_center_types=available_types if available_types else None,
        is_ocr=is_ocr,
        part_context=part_context,
        is_assembly=is_assembly,
        learned_examples_context=learned_examples_context,
    )

    warnings: List[str] = []
    if llm_result.get("_error"):
        fallback_part_info = _infer_part_info_from_drawing(
            drawing_context_text,
            geometry,
            is_assembly=is_assembly,
        )
        fallback_ops = _operations_from_learned_patterns(learned_patterns)
        if fallback_ops:
            warnings.append("AI extraction was unavailable, so a similar approved routing pattern was used.")
        if not fallback_ops:
            fallback_ops = infer_operations_from_drawing(
                drawing_context_text,
                geometry,
                fallback_part_info,
                automatic_types,
                is_assembly=is_assembly,
            )
            if fallback_ops:
                warnings.append(
                    "AI extraction was unavailable, so a rules-based routing was generated from the drawing and current work centers."
                )
        if not fallback_ops:
            return llm_result
        llm_result = {
            "part_info": fallback_part_info,
            "operations": fallback_ops,
            "extraction_confidence": "low",
            "_extraction_metadata": llm_result.get("_extraction_metadata", {}),
        }

    proposed_ops = llm_result.get("operations", [])
    part_info = {
        **_infer_part_info_from_drawing(drawing_context_text, geometry, is_assembly=is_assembly),
        **llm_result.get("part_info", {}),
    }
    if is_assembly:
        part_info["assembly_required"] = True
    if not proposed_ops:
        proposed_ops = _operations_from_learned_patterns(learned_patterns)
        if proposed_ops:
            warnings.append("No operations were extracted, so a similar approved routing pattern was used.")
    if not proposed_ops:
        proposed_ops = infer_operations_from_drawing(
            drawing_context_text,
            geometry,
            part_info,
            automatic_types,
            is_assembly=is_assembly,
        )
        if proposed_ops:
            warnings.append(
                "No operations were extracted, so a rules-based routing was generated from current work centers."
            )

    proposed_ops = ensure_current_work_center_completion_steps(
        proposed_ops,
        drawing_context_text,
        geometry,
        part_info,
        automatic_types,
        is_assembly=is_assembly,
    )

    # Step 2: Map to work centers
    mapped_ops, wc_warnings = map_operations_to_work_centers(
        proposed_ops,
        work_centers_by_type,
        learned_aliases=learned_aliases,
        preferred_work_center_ids=preferred_work_center_ids,
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
        "warnings": warnings + wc_warnings,
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
