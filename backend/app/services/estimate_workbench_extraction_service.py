"""Phase 4 — PDF / RFQ assist for the Estimate Workbench.

Pipeline:
  RFQ package files → (optional) 3× LLM structured draws + deterministic parse
  → majority vote → draft assemblies / buyouts for the workbench UI.

When AI egress is off or Anthropic is unconfigured, falls back to the existing
deterministic ``parse_rfq_package_files`` path and marks lines Review / Majority
from source confidence scores (never invents Confirmed without 3 agreeing passes).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models.rfq_quote import RfqPackage, RfqPackageFile
from app.services.estimate_extraction_vote import (
    MAJORITY,
    REVIEW,
    align_and_vote_buyout_passes,
    align_and_vote_fab_passes,
    line_vote_to_buyout_draft,
    line_vote_to_fab_draft,
)
from app.services.llm_client import (
    LLMEgressDisabledError,
    LLMNotConfiguredError,
    run_llm_task,
)
from app.services.llm_model_router import LLMTaskContext
from app.services.pdf_service import extract_text_from_pdf
from app.services.prompts.estimate_drawing import (
    ESTIMATE_DRAWING_EXTRACTION_PROMPT,
    ESTIMATE_DRAWING_EXTRACTION_SCHEMA,
    PASS_PHRASINGS,
)
from app.services.rfq_parsing_service import parse_rfq_package_files
from app.services.storage_service import ref_as_local_path

logger = logging.getLogger(__name__)

_MAX_NATIVE_PDF_BYTES = 20 * 1024 * 1024
_PASS_COUNT = 3
# Mild temperature variation across passes (Anthropic accepts 0–1)
_PASS_TEMPERATURES = (0.0, 0.35, 0.7)


class ExtractionError(Exception):
    """User-facing extraction failure."""


def _strip_json_fence(text: str) -> str:
    response_text = (text or "").strip()
    if response_text.startswith("```json"):
        response_text = response_text[7:]
    if response_text.startswith("```"):
        response_text = response_text[3:]
    if response_text.endswith("```"):
        response_text = response_text[:-3]
    return response_text.strip()


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _part_to_fab_dict(part: Dict[str, Any]) -> Dict[str, Any]:
    """Map a normalized RFQ part spec into a fab extraction dict."""
    width = None
    length = None
    bbox = part.get("bbox") or {}
    if isinstance(bbox, dict):
        width = _safe_float(bbox.get("width"))
        length = _safe_float(bbox.get("length"))
    # Some parsers stash dims on the part directly
    width = width or _safe_float(part.get("width")) or _safe_float(part.get("width_in"))
    length = length or _safe_float(part.get("length")) or _safe_float(part.get("length_in"))

    return {
        "detail_name": part.get("part_name") or part.get("part_id") or "Detail",
        "part_number": part.get("part_id") or part.get("part_number"),
        "material": part.get("material"),
        "qty": int(part.get("qty") or part.get("quantity_per_assembly") or 1),
        "thickness_in": _safe_float(part.get("thickness_in")),
        "width_in": width,
        "length_in": length,
        "cut_length_in": _safe_float(part.get("cut_length")),
        "pierce_count": int(part.get("hole_count") or part.get("pierce_count") or 0),
        "bend_count": int(part.get("bend_count") or 0),
        "weld_length_in": _safe_float(part.get("weld_length")),
        "drawing_number": part.get("drawing_number"),
        "revision": part.get("revision"),
        "source_file": (
            (part.get("sources") or {}).get("drawing_pdf", [None])[0]
            if isinstance((part.get("sources") or {}).get("drawing_pdf"), list)
            else None
        ),
    }


def _hardware_to_buyout_dict(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "part_number": item.get("part_number") or item.get("part_id"),
        "description": item.get("description") or item.get("part_name") or item.get("notes") or "Hardware",
        "qty": float(item.get("qty") or item.get("quantity") or 1),
        "unit_cost": _safe_float(item.get("unit_cost") or item.get("unit_price")),
        "category": item.get("category") or item.get("line_type") or "hardware",
        "vendor": item.get("vendor"),
        "price_source": item.get("price_source"),
        "source_file": item.get("source") or item.get("file_name"),
    }


def deterministic_pass_from_parsed(parsed: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build one extraction pass from the existing RFQ parser output."""
    fab: List[Dict[str, Any]] = []
    for part in parsed.get("parts") or []:
        line_type = str(part.get("line_type") or "manufactured").lower()
        item_type = str(part.get("item_type") or "make").lower()
        if line_type in {"hardware", "consumable", "reference", "buy"} or item_type == "buy":
            continue
        fab.append(_part_to_fab_dict(part))

    buyouts: List[Dict[str, Any]] = []
    for hw in parsed.get("hardware_items") or []:
        buyouts.append(_hardware_to_buyout_dict(hw))
    # Also pick purchased rows from parts list
    for part in parsed.get("parts") or []:
        line_type = str(part.get("line_type") or "").lower()
        item_type = str(part.get("item_type") or "").lower()
        if line_type in {"hardware", "consumable"} or item_type == "buy":
            buyouts.append(_hardware_to_buyout_dict(part))

    return fab, buyouts


def _confidence_from_parser_scores(part: Dict[str, Any]) -> str:
    """Map legacy 0–1 field confidences into workbench enum (no fake Confirmed)."""
    conf = part.get("confidence") or {}
    scores = [
        float(conf.get("material") or 0),
        float(conf.get("thickness") or 0),
        float(conf.get("geometry") or 0),
    ]
    avg = sum(scores) / max(len(scores), 1)
    sources = part.get("sources") or {}
    source_kinds = sum(1 for k in ("bom", "drawing_pdf", "flat_pattern_dxf") if sources.get(k))
    if source_kinds >= 2 and avg >= 0.7:
        return MAJORITY
    return REVIEW


def drafts_from_deterministic_only(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """When LLM is unavailable: one pass → all lines Review/Majority, never Confirmed."""
    fab_raw, buy_raw = deterministic_pass_from_parsed(parsed)
    # Vote against itself three times so Confirmed is only possible if we
    # deliberately triplicate — we instead set confidence from parser scores.
    fab_drafts: List[Dict[str, Any]] = []
    part_by_key = {
        "".join(ch for ch in str(p.get("part_id") or p.get("part_name") or "").lower() if ch.isalnum()): p
        for p in (parsed.get("parts") or [])
    }
    for item in fab_raw:
        # Single-pass vote → Review by agreement rules; override with parser score
        voted = align_and_vote_fab_passes([[item], [item], [{}]])[0]
        key = "".join(
            ch for ch in str(item.get("part_number") or item.get("detail_name") or "").lower() if ch.isalnum()
        )
        src = part_by_key.get(key) or {}
        draft = line_vote_to_fab_draft(voted)
        draft["confidence"] = _confidence_from_parser_scores(src) if src else REVIEW
        note_bits = [draft.get("verification_note") or "", "deterministic RFQ parse (AI unavailable)"]
        if parsed.get("warnings"):
            note_bits.append("see package parse warnings")
        draft["verification_note"] = "; ".join(b for b in note_bits if b)
        fab_drafts.append(draft)

    buy_drafts = []
    for item in buy_raw:
        voted = align_and_vote_buyout_passes([[item], [item], [{}]])[0]
        draft = line_vote_to_buyout_draft(voted)
        draft["confidence"] = REVIEW
        buy_drafts.append(draft)

    return {
        "mode": "deterministic",
        "fab_lines": fab_drafts,
        "buyout_lines": buy_drafts,
        "warnings": list(parsed.get("warnings") or []),
        "assumptions": list(parsed.get("assumptions") or []),
    }


def _collect_pdf_texts(files: Sequence[RfqPackageFile]) -> List[Dict[str, Any]]:
    """Materialize PDF text (and optional bytes) for LLM passes."""
    docs: List[Dict[str, Any]] = []
    for file_record in files:
        ext = (file_record.file_ext or "").lower()
        if ext != ".pdf":
            continue
        try:
            with ref_as_local_path(file_record.file_path) as local_file:
                path = Path(local_file)
                extraction = extract_text_from_pdf(str(path))
                text = (extraction.text or "").strip()
                pdf_bytes: Optional[bytes] = None
                size = path.stat().st_size if path.exists() else 0
                if size and size <= _MAX_NATIVE_PDF_BYTES:
                    pdf_bytes = path.read_bytes()
                docs.append(
                    {
                        "file_name": file_record.file_name,
                        "file_id": file_record.id,
                        "text": text,
                        "pdf_bytes": pdf_bytes,
                        "text_length": len(text),
                    }
                )
        except Exception as exc:  # noqa: BLE001 — collect and continue
            logger.warning("PDF text extract failed for %s: %s", file_record.file_name, exc)
    return docs


def _run_one_llm_pass(
    *,
    docs: List[Dict[str, Any]],
    pass_index: int,
    company_id: int,
) -> Dict[str, Any]:
    phrasing = PASS_PHRASINGS[pass_index % len(PASS_PHRASINGS)]
    # Prefer native PDF for the first document when small enough; else concatenate text
    content_blocks: List[Dict[str, Any]] = []
    input_chars = 0
    has_pdf = False

    instruction = f"""Extract fab and buyout line items for a sheet-metal / weldment estimate.
Return JSON matching this schema exactly:

{ESTIMATE_DRAWING_EXTRACTION_SCHEMA}

{phrasing}

If multiple drawings are provided, emit one fab_line per manufactured detail across all of them.
Never invent prices or geometry. Return ONLY the JSON object."""

    content_blocks.append({"type": "text", "text": instruction})

    for doc in docs[:6]:  # hard cap to control cost
        if doc.get("pdf_bytes") and not has_pdf:
            # One native PDF keeps the call on Sonnet; additional docs as text
            import base64

            content_blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(doc["pdf_bytes"]).decode("ascii"),
                    },
                }
            )
            content_blocks.append({"type": "text", "text": f"(Document filename: {doc['file_name']})"})
            has_pdf = True
        else:
            snippet = (doc.get("text") or "")[:40_000]
            input_chars += len(snippet)
            content_blocks.append(
                {
                    "type": "text",
                    "text": f"--- Drawing text: {doc['file_name']} ---\n{snippet}\n---",
                }
            )

    messages = [{"role": "user", "content": content_blocks}]
    ctx = LLMTaskContext(
        task="estimate_drawing_extraction",
        input_chars=input_chars,
        has_pdf_document=has_pdf,
        document_type="drawing",
        max_output_tokens=4096,
        metadata={"pass_index": pass_index},
    )

    # Anthropic Messages API: temperature via create kwargs — run_llm_task doesn't
    # expose it yet; we vary via phrasing. (Temperature support can be added later.)
    _ = _PASS_TEMPERATURES[pass_index % len(_PASS_TEMPERATURES)]

    llm_result = run_llm_task(
        ctx,
        messages=messages,
        system=ESTIMATE_DRAWING_EXTRACTION_PROMPT.text,
        max_tokens=4096,
        company_id=company_id,
        feature="estimate_drawing_extraction",
        prompt_version=ESTIMATE_DRAWING_EXTRACTION_PROMPT.version,
        timeout=90.0,
        max_retries=0,
    )
    raw = _strip_json_fence(llm_result.text)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"LLM pass {pass_index + 1} returned invalid JSON") from exc

    fab = []
    for row in payload.get("fab_lines") or []:
        if not isinstance(row, dict):
            continue
        fab.append(
            {
                "detail_name": row.get("detail_name"),
                "part_number": row.get("part_number"),
                "material": row.get("material"),
                "qty": row.get("qty") or 1,
                "thickness_in": _safe_float(row.get("thickness_in")),
                "width_in": _safe_float(row.get("width_in")),
                "length_in": _safe_float(row.get("length_in")),
                "cut_length_in": _safe_float(row.get("cut_length_in")),
                "pierce_count": int(row.get("pierce_count") or 0),
                "bend_count": int(row.get("bend_count") or 0),
                "weld_length_in": _safe_float(row.get("weld_length_in")),
                "source_quote": row.get("source_quote"),
            }
        )
    buyouts = []
    for row in payload.get("buyout_lines") or []:
        if not isinstance(row, dict):
            continue
        buyouts.append(
            {
                "part_number": row.get("part_number"),
                "description": row.get("description"),
                "qty": row.get("qty") or 1,
                "unit_cost": _safe_float(row.get("unit_cost")),
                "category": row.get("category"),
                "vendor": row.get("vendor"),
                "source_quote": row.get("source_quote"),
            }
        )
    return {
        "fab_lines": fab,
        "buyout_lines": buyouts,
        "notes": payload.get("notes"),
        "model": llm_result.model,
        "prompt_version": llm_result.prompt_version,
        "raw": payload,
    }


def run_triple_pass_llm(
    docs: List[Dict[str, Any]],
    *,
    company_id: int,
) -> Tuple[List[Dict[str, Any]], List[List[Dict[str, Any]]], List[List[Dict[str, Any]]]]:
    """Run 3 LLM passes; return (pass_meta, fab_pass_lists, buyout_pass_lists)."""
    metas: List[Dict[str, Any]] = []
    fab_lists: List[List[Dict[str, Any]]] = []
    buy_lists: List[List[Dict[str, Any]]] = []
    for i in range(_PASS_COUNT):
        result = _run_one_llm_pass(docs=docs, pass_index=i, company_id=company_id)
        metas.append(
            {
                "pass_index": i,
                "model": result.get("model"),
                "prompt_version": result.get("prompt_version"),
                "notes": result.get("notes"),
                "fab_count": len(result["fab_lines"]),
                "buyout_count": len(result["buyout_lines"]),
                "raw": result.get("raw"),
            }
        )
        fab_lists.append(result["fab_lines"])
        buy_lists.append(result["buyout_lines"])
    return metas, fab_lists, buy_lists


def build_workbench_draft_from_votes(
    fab_pass_lists: Sequence[Sequence[Dict[str, Any]]],
    buy_pass_lists: Sequence[Sequence[Dict[str, Any]]],
    *,
    assembly_name: str = "Extracted assembly",
    mode: str = "triple_pass",
    warnings: Optional[List[str]] = None,
    extraction_artifact: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fab_votes = align_and_vote_fab_passes(fab_pass_lists)
    buy_votes = align_and_vote_buyout_passes(buy_pass_lists)
    fab_drafts = [line_vote_to_fab_draft(v) for v in fab_votes]
    buy_drafts = [line_vote_to_buyout_draft(v) for v in buy_votes]

    review_n = sum(1 for d in fab_drafts + buy_drafts if d.get("confidence") == REVIEW)
    majority_n = sum(1 for d in fab_drafts + buy_drafts if d.get("confidence") == MAJORITY)

    return {
        "mode": mode,
        "assemblies": [
            {
                "name": assembly_name,
                "sort_order": 0,
                "assembly_labor_hrs": 0.0,
                "electrical_labor_hrs": 0.0,
                "fab_lines": fab_drafts,
                "buyout_lines": buy_drafts,
            }
        ],
        "machined_parts": [],
        "summary": {
            "fab_count": len(fab_drafts),
            "buyout_count": len(buy_drafts),
            "review_count": review_n,
            "majority_count": majority_n,
            "confirmed_count": sum(1 for d in fab_drafts + buy_drafts if d.get("confidence") == "confirmed"),
        },
        "warnings": warnings or [],
        "extraction_artifact": extraction_artifact or {},
    }


def extract_workbench_draft_from_rfq(
    db: Session,
    *,
    rfq_package_id: int,
    company_id: int,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """Main entry: RFQ package → voted workbench draft (not yet persisted)."""
    pkg = (
        db.query(RfqPackage)
        .options(joinedload(RfqPackage.files))
        .filter(RfqPackage.id == rfq_package_id, RfqPackage.company_id == company_id)
        .first()
    )
    if not pkg:
        raise ExtractionError("RFQ package not found")

    files: List[RfqPackageFile] = list(pkg.files or [])
    if not files:
        raise ExtractionError("RFQ package has no files to extract from")

    parsed = parse_rfq_package_files(files)
    warnings = list(parsed.get("warnings") or [])
    assembly_name = pkg.rfq_number or f"RFQ-{pkg.id}"

    if not use_llm:
        return _deterministic_draft(parsed, assembly_name, warnings)

    # Prefer LLM triple-pass when PDFs exist; always merge deterministic as a
    # soft cross-check stored in the artifact (not a 4th vote) for audit.
    docs = _collect_pdf_texts(files)
    det_fab, det_buy = deterministic_pass_from_parsed(parsed)

    if not docs and not det_fab and not det_buy:
        raise ExtractionError("No PDF drawings or BOM parts found in this RFQ package")

    try:
        if docs:
            metas, fab_lists, buy_lists = run_triple_pass_llm(docs, company_id=company_id)
            # If LLM returned empty but deterministic has parts, fall back
            if not any(fab_lists) and det_fab:
                warnings.append("LLM returned no fab lines — using deterministic RFQ parse")
                return _deterministic_draft(parsed, assembly_name, warnings)
            artifact = {
                "passes": metas,
                "deterministic_fab": det_fab,
                "deterministic_buyout": det_buy,
                "prompt_id": ESTIMATE_DRAWING_EXTRACTION_PROMPT.id,
                "prompt_version": ESTIMATE_DRAWING_EXTRACTION_PROMPT.version,
            }
            return build_workbench_draft_from_votes(
                fab_lists,
                buy_lists,
                assembly_name=assembly_name,
                mode="triple_pass",
                warnings=warnings,
                extraction_artifact=artifact,
            )
        # No PDFs — deterministic only
        warnings.append("No PDF drawings in package — using BOM/DXF deterministic parse")
        return _deterministic_draft(parsed, assembly_name, warnings)
    except (LLMNotConfiguredError, LLMEgressDisabledError) as exc:
        warnings.append(f"AI extraction unavailable ({exc}); using deterministic parse")
        return _deterministic_draft(parsed, assembly_name, warnings)
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Triple-pass extraction failed")
        warnings.append(f"LLM extraction failed ({exc}); using deterministic parse")
        return _deterministic_draft(parsed, assembly_name, warnings)


def _fab_draft_to_pass_dict(draft: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: draft.get(k)
        for k in (
            "detail_name",
            "part_number",
            "material",
            "qty",
            "thickness_in",
            "width_in",
            "length_in",
            "cut_length_in",
            "pierce_count",
            "bend_count",
            "weld_length_in",
        )
    }


def _buy_draft_to_pass_dict(draft: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: draft.get(k)
        for k in (
            "part_number",
            "description",
            "qty",
            "unit_cost",
            "category",
            "vendor",
        )
    }


def _triplicate_as_passes(items: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Helper unused in happy path — kept for tests."""
    return [list(items), list(items), list(items)]


def _deterministic_draft(
    parsed: Dict[str, Any],
    assembly_name: str,
    warnings: List[str],
) -> Dict[str, Any]:
    det = drafts_from_deterministic_only(parsed)
    fab_items = [_fab_draft_to_pass_dict(f) for f in det["fab_lines"]]
    # Re-build drafts with parser confidence already set on det["fab_lines"]
    # Use those drafts directly rather than re-voting.
    return {
        "mode": "deterministic",
        "assemblies": [
            {
                "name": assembly_name,
                "sort_order": 0,
                "assembly_labor_hrs": 0.0,
                "electrical_labor_hrs": 0.0,
                "fab_lines": det["fab_lines"],
                "buyout_lines": det["buyout_lines"],
            }
        ],
        "machined_parts": [],
        "summary": {
            "fab_count": len(det["fab_lines"]),
            "buyout_count": len(det["buyout_lines"]),
            "review_count": sum(1 for d in det["fab_lines"] + det["buyout_lines"] if d.get("confidence") == REVIEW),
            "majority_count": sum(1 for d in det["fab_lines"] + det["buyout_lines"] if d.get("confidence") == MAJORITY),
            "confirmed_count": 0,
        },
        "warnings": warnings,
        "extraction_artifact": {
            "deterministic": True,
            "source_attribution": parsed.get("source_attribution"),
            "fab_pass_seed": fab_items,
        },
    }


def apply_extraction_to_estimate(
    db: Session,
    estimate,
    draft: Dict[str, Any],
    *,
    expected_version: int,
    company_id: int,
    user_id: Optional[int],
    audit=None,
    replace: bool = True,
) -> Any:
    """Persist voted draft onto an estimate via save_estimate_tree."""
    from app.services.estimate_workbench_service import save_estimate_tree

    assemblies = draft.get("assemblies") or []
    if not replace and (estimate.assemblies or []):
        # Merge: append fab/buyout into first live assembly
        existing = []
        for asm in estimate.assemblies:
            if getattr(asm, "is_deleted", False):
                continue
            existing.append(
                {
                    "name": asm.name,
                    "sort_order": asm.sort_order,
                    "assembly_labor_hrs": asm.assembly_labor_hrs,
                    "electrical_labor_hrs": asm.electrical_labor_hrs,
                    "notes": asm.notes,
                    "fab_lines": [
                        {
                            "detail_name": fl.detail_name,
                            "part_number": fl.part_number,
                            "material": fl.material,
                            "qty": fl.qty,
                            "thickness_in": fl.thickness_in,
                            "width_in": fl.width_in,
                            "length_in": fl.length_in,
                            "cut_length_in": fl.cut_length_in,
                            "pierce_count": fl.pierce_count,
                            "bend_count": fl.bend_count,
                            "weld_length_in": fl.weld_length_in,
                            "weld_minutes_ea": fl.weld_minutes_ea,
                            "include_material": fl.include_material,
                            "include_laser": fl.include_laser,
                            "include_brake": fl.include_brake,
                            "include_weld": fl.include_weld,
                            "confidence": fl.confidence,
                            "verification_note": fl.verification_note,
                        }
                        for fl in (asm.fab_line_items or [])
                        if not getattr(fl, "is_deleted", False)
                    ],
                    "buyout_lines": [
                        {
                            "description": bl.description,
                            "qty": bl.qty,
                            "unit_cost": bl.unit_cost,
                            "category": bl.category,
                            "vendor": bl.vendor,
                            "part_number": bl.part_number,
                            "price_source": bl.price_source,
                            "confidence": bl.confidence,
                            "verification_note": bl.verification_note,
                        }
                        for bl in (asm.buyout_line_items or [])
                        if not getattr(bl, "is_deleted", False)
                    ],
                }
            )
        if existing and assemblies:
            incoming = assemblies[0]
            existing[0]["fab_lines"].extend(incoming.get("fab_lines") or [])
            existing[0]["buyout_lines"].extend(incoming.get("buyout_lines") or [])
            assemblies = existing

    payload = {
        "assemblies": assemblies,
        "machined_parts": draft.get("machined_parts") or [],
    }
    saved = save_estimate_tree(
        db,
        estimate,
        payload,
        expected_version=expected_version,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
    )
    # Store extraction artifact on estimate for audit
    artifact = draft.get("extraction_artifact") or {}
    prev = dict(saved.source_attribution or {})
    prev["estimate_workbench_extraction"] = {
        "mode": draft.get("mode"),
        "summary": draft.get("summary"),
        "warnings": draft.get("warnings"),
        "artifact": artifact,
    }
    saved.source_attribution = prev
    db.add(saved)
    db.commit()
    from app.services.estimate_workbench_service import get_estimate_tree

    return get_estimate_tree(db, saved.id, company_id)
