"""Laser-nest report PDF field extraction (and multi-page segmentation).

AI-always extraction of nest metadata (CNC number, material, thickness, sheet
size, optional planned runs) from CAM laser-nest report PDFs (SigmaNEST /
Ermaksan style).

Primary path: the PDF bytes are sent to Claude as a base64 ``document`` content
block so the model reads the sheet WITH its 2-D layout (layout-aware vision).
This fixes the glued-digits and material-grade-on-the-wrong-line failures that
came from flattening a nest report into a 1-D string. The flattened-text path
(``pdf_service.extract_text_from_document`` -> ``run_llm_task``) remains as a
fallback when the bytes can't be read or the PDF exceeds the native size cap.
Both paths run against the versioned ``laser_nest_extraction`` prompt.

Extraction is TWO-PASS: after the primary read parses, an independent
verification read (``laser_nest_verification`` prompt) re-derives every field
and the two reads are merged per field — agreement is "high" confidence, a
one-sided null is "medium", a conflict takes the verifier's value at "low" so
the wizard flags it for the planner. A pass-2 failure of any kind keeps the
pass-1 result untouched (``passes = 1``); verification can only add confidence
signal, never degrade a good first read.

``segment_nest_pdf`` is pass 0 for bare multi-page uploads: it asks the model
which pages form which nest and which to skip, degrading to one-nest-per-page
on any failure.

Contract: these functions NEVER raise. A bad, odd, or unconfigured PDF degrades
gracefully to a filename-only result so a batch of 50 nests can never hard-fail
on one file. The filename is also a reliable fallback for ``cnc_number`` — it
equals the CNC program number on every observed sample.
"""

import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.time_utils import to_utc_iso
from app.services.llm_client import (
    LLMEgressDisabledError,
    LLMNotConfiguredError,
    is_anthropic_api_error,
    run_llm_task,
)
from app.services.llm_model_router import LLMTaskContext
from app.services.llm_service import LLM_API_TIMEOUT_SECONDS
from app.services.pdf_service import extract_text_from_document
from app.services.prompts import (
    LASER_NEST_EXTRACTION_PROMPT,
    LASER_NEST_EXTRACTION_SCHEMA,
    LASER_NEST_SEGMENTATION_PROMPT,
    LASER_NEST_VERIFICATION_PROMPT,
)
from app.services.storage_service import is_s3_ref, read_ref_bytes

logger = logging.getLogger(__name__)

# Anthropic caps native PDF requests at ~32 MB / 100 pages. base64 inflates the
# payload ~33%, so cap on the RAW byte size well under that: 20 MB raw -> ~27 MB
# encoded. Anything larger falls back to the flattened-text path instead of
# being sent natively.
_MAX_NATIVE_PDF_BYTES = 20 * 1024 * 1024


def _not_configured_message(exc: LLMNotConfiguredError) -> str:
    return "LLM library not available" if exc.reason == "library" else "API key not configured"


def _read_pdf_bytes(pdf_path: str) -> bytes:
    """Read raw PDF bytes for both local paths and s3:// refs.

    Mirrors how ``extract_text_from_document`` materializes remote refs: a local
    path is read directly, an ``s3://`` ref goes through the storage backend.
    """
    if is_s3_ref(pdf_path):
        return read_ref_bytes(pdf_path)
    with open(pdf_path, "rb") as fh:
        return fh.read()


def _pdf_size_exceeds_native_cap(pdf_path: str) -> bool:
    """True when the PDF is over the native-document cap, WITHOUT reading it.

    The whole point is to decide before the bytes are pulled into RAM: a local
    path (the upload temp files and split segments) answers via ``stat``. An
    ``s3://`` ref can't be stat'ed here -- and a missing/unstatable path must
    surface through the READ path's error handling, not here -- so both
    conservatively answer False and the caller's post-read ``len`` check
    remains the backstop.
    """
    if is_s3_ref(pdf_path):
        return False
    try:
        return Path(pdf_path).stat().st_size > _MAX_NATIVE_PDF_BYTES
    except OSError:
        return False


def _strip_json_fences(text: str) -> str:
    """Strip an optional ```json / ``` fence pair from a model response."""
    stripped = text.strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:]
    if stripped.startswith("```"):
        stripped = stripped[3:]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


# The five merge-policy fields. Confidence in the merged result is derived
# purely from the agreement pattern between the two passes (not from either
# pass's self-reported confidence): agreement is strong evidence, a one-sided
# read is weak evidence, a conflict is a flag for the planner.
_MERGE_FIELDS = ("cnc_number", "material", "thickness", "sheet_size", "planned_runs")
_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def _is_blank(value: Any) -> bool:
    """None and whitespace-only strings both count as "not pinned" in the merge."""
    return value is None or (isinstance(value, str) and not value.strip())


def _comparable(field: str, value: Any) -> Any:
    """Normalize a field value for the agreement compare only (never for output).

    Strings compare case-insensitively with surrounding whitespace stripped;
    ``planned_runs`` compares numerically when both sides coerce to an int
    (so ``3`` agrees with ``"3"``).
    """
    if field == "planned_runs" and not isinstance(value, bool):
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return str(value).strip().lower()


def _merge_extraction_passes(primary: Dict[str, Any], verifier: Dict[str, Any]) -> None:
    """Fold the verification read into ``primary`` in place, per the merge policy.

    Per field: agree -> keep pass 1's value, "high"; exactly one side null ->
    the non-null value, "medium"; both non-null but different -> the VERIFIER's
    value, "low" (the wizard flags it for manual review); both null -> null,
    "low". ``confidence`` becomes the merged per-field dict and
    ``extraction_confidence`` the minimum across fields.
    """
    merged_confidence: Dict[str, str] = {}
    for field in _MERGE_FIELDS:
        p_value = primary.get(field)
        v_value = verifier.get(field)
        p_null = _is_blank(p_value)
        v_null = _is_blank(v_value)
        if p_null and v_null:
            primary[field] = None
            merged_confidence[field] = "low"
        elif p_null:
            primary[field] = v_value
            merged_confidence[field] = "medium"
        elif v_null:
            primary[field] = p_value
            merged_confidence[field] = "medium"
        elif _comparable(field, p_value) == _comparable(field, v_value):
            primary[field] = p_value
            merged_confidence[field] = "high"
        else:
            primary[field] = v_value
            merged_confidence[field] = "low"
    primary["confidence"] = merged_confidence
    primary["extraction_confidence"] = min(merged_confidence.values(), key=lambda c: _CONFIDENCE_RANK[c])


def _verification_failure_reason(exc: BaseException) -> str:
    """Short, user-safe reason for a skipped verification pass."""
    if isinstance(exc, LLMNotConfiguredError):
        return _not_configured_message(exc)
    if isinstance(exc, LLMEgressDisabledError):
        return "AI egress is disabled for this company (allow_ai_egress is off)"
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON response: {exc}"
    if is_anthropic_api_error(exc):
        return f"API error: {exc}"
    return str(exc)


def _run_verification_pass(
    result: Dict[str, Any],
    *,
    file_name: str,
    company_id: Optional[int],
    pdf_b64: Optional[str],
    text: Optional[str],
    is_ocr: bool,
    filename_is_cnc_hint: bool = True,
) -> tuple[int, Optional[str], Dict[str, Any]]:
    """Run pass 2 (independent re-read) and merge it into ``result`` in place.

    Returns ``(passes, warning, verification_metadata)``. NEVER raises and never
    degrades a good pass-1 result: any failure — API error, bad JSON, egress
    toggled off mid-flight — leaves ``result`` exactly as pass 1 produced it and
    returns ``passes=1`` with a warning noting verification was skipped.

    The verifier sees the SAME input pass 1 saw (the native ``document`` block
    when ``pdf_b64`` is given, otherwise the same flattened ``text``) plus pass
    1's field values, labeled comparison-only so it re-derives rather than
    rubber-stamps. Telemetry: same routing task as extraction, but
    ``feature="laser_nest_verification"`` and the verification prompt version so
    the two calls are distinguishable per tenant.
    """
    first_pass_fields = {field: result.get(field) for field in _MERGE_FIELDS}
    # A synthetic bare-PDF segment name ('nest-p001.pdf') would only anchor the
    # verifier toward a fabricated CNC number, so it is withheld in that mode.
    filename_note = (
        f"The source filename is '{file_name}'."
        if filename_is_cnc_hint
        else "The source filename carries no CNC-number information."
    )
    try:
        instruction_core = f"""Independently re-read the nest report and return JSON matching this schema exactly:

{LASER_NEST_EXTRACTION_SCHEMA}

A first reader extracted the following JSON from the same sheet. It is provided for comparison ONLY — re-derive every field yourself from the sheet and report your own values; return null (confidence "low") for any field you cannot pin yourself:

{json.dumps(first_pass_fields)}

{filename_note}

Return ONLY the JSON object, no other text."""

        if pdf_b64 is not None:
            messages: list = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": instruction_core},
                    ],
                }
            ]
            ctx = LLMTaskContext(task="laser_nest_extraction", input_chars=0, has_pdf_document=True)
        else:
            user_prompt = f"""{instruction_core}

Document Text:
---
{text or ''}
---"""
            messages = [{"role": "user", "content": user_prompt}]
            ctx = LLMTaskContext(task="laser_nest_extraction", input_chars=len(text or ""), is_ocr=is_ocr)

        llm_result = run_llm_task(
            ctx,
            messages=messages,
            system=LASER_NEST_VERIFICATION_PROMPT.text,
            max_tokens=1024,
            company_id=company_id,
            feature="laser_nest_verification",
            prompt_version=LASER_NEST_VERIFICATION_PROMPT.version,
            timeout=LLM_API_TIMEOUT_SECONDS,
        )
        verifier = json.loads(_strip_json_fences(llm_result.text))
        if not isinstance(verifier, dict):
            raise ValueError("verification response was not a JSON object")

        _merge_extraction_passes(result, verifier)
        return (
            2,
            None,
            {
                "verification_model": llm_result.model,
                "verification_prompt_version": llm_result.prompt_version,
            },
        )
    except Exception as exc:  # noqa: BLE001 - pass-2 failure must never sink a good pass-1 result
        reason = _verification_failure_reason(exc)
        logger.warning("Laser-nest verification pass skipped for %s: %s", file_name, reason)
        return 1, f"Verification pass skipped: {reason}", {}


def extract_nest_fields_from_pdf(
    pdf_path: str,
    file_name: str,
    company_id: Optional[int] = None,
    *,
    cnc_hint: Optional[str] = None,
    filename_is_cnc_hint: bool = True,
) -> Dict[str, Any]:
    """Extract laser-nest fields from a PDF. Never raises.

    Args:
        pdf_path: Local path (or storage ref) to the nest report PDF.
        file_name: Original filename. When ``filename_is_cnc_hint`` is True
            (per-file packages, where the filename genuinely is the CNC program
            number) it is used as the CNC-number fallback and as a hint to the
            model. The bare-PDF path passes False -- its per-segment files carry
            SYNTHETIC generated names (``nest-p001.pdf``) that must never be
            presented to the model as a CNC number nor stamped into the result,
            or degraded rows would fabricate CNC numbers from the split names.
        company_id: Active company for tenant-scoped AI usage telemetry.
        cnc_hint: Optional CNC-number suggestion from the segmentation pass
            (bare-PDF path only); offered to the model as an unverified hint,
            never used as a fallback value.
        filename_is_cnc_hint: See ``file_name`` above.

    Returns:
        A dict with stable keys callers depend on: ``cnc_number``, ``material``,
        ``thickness``, ``sheet_size``, ``planned_runs``, ``confidence``,
        ``source`` ("ai" | "filename" | "none"), ``warning`` (None on success),
        and ``_extraction_metadata``.
    """
    # Defaults BEFORE the try so the degrade paths below can always reference
    # them, even if reading the bytes / text extraction raises (see in-try calls
    # below). is_ocr stays False on the native path (no OCR happens there);
    # input_mode records which path produced the row.
    is_ocr = False
    input_mode = "native_pdf"

    if filename_is_cnc_hint:
        cnc_hint_line = (
            f"- The source filename is '{file_name}', which is usually the CNC program number "
            "— use it for cnc_number if the sheet is ambiguous."
        )
    elif cnc_hint:
        cnc_hint_line = (
            f"- A page-segmentation pass suggested this sheet's CNC program number may be '{cnc_hint}' "
            "— treat it as an unverified hint and prefer what the sheet itself shows."
        )
    else:
        cnc_hint_line = (
            "- The source filename carries no CNC-number information; take cnc_number only from the sheet itself."
        )

    try:
        # Size gate BEFORE the bytes are loaded (stat for local files) so an
        # oversized upload is never pulled into RAM just to route it to the
        # text fallback. Everything stays inside the try so a read/stat error
        # (missing file, s3 hiccup) degrades to a filename-only result rather
        # than propagating -- the never-raises contract covers the pipeline.
        use_native = not _pdf_size_exceeds_native_cap(pdf_path)
        if use_native:
            pdf_bytes = _read_pdf_bytes(pdf_path)
            # Backstop for s3 refs, which can't be stat'ed without reading.
            use_native = len(pdf_bytes) <= _MAX_NATIVE_PDF_BYTES

        if use_native:
            # Primary path: hand the rendered PDF to the model so it reads the
            # sheet with its 2-D layout. input_chars~=0 -- the has_pdf_document
            # flag is what lifts model selection off the FAST tier.
            input_mode = "native_pdf"
            pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()

            instruction = f"""Extract laser-nest report data from the attached nest report PDF. Return JSON matching this schema exactly:

{LASER_NEST_EXTRACTION_SCHEMA}

Important:
- Keep adjacent numeric fields (CNC number, sheet size, thickness) separate — read each from its own labeled position on the sheet.
- The material grade may appear in a different block than the CNC number and thickness (often the machine line); do not confuse the machine name with the material.
{cnc_hint_line}
- Preserve values exactly as shown; do not normalize or reformat them.
- If a field is not found, set it to null and set its confidence to "low".

Return ONLY the JSON object, no other text."""

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": instruction},
                    ],
                }
            ]
            ctx = LLMTaskContext(task="laser_nest_extraction", input_chars=0, has_pdf_document=True)
        else:
            # Fallback path: the PDF is too large to send natively, so flatten it
            # to text and use the glued-digits/OCR-aware user prompt. (pdf_bytes
            # was never loaded on this path -- the size gate answered via stat.)
            input_mode = "text"
            logger.info(
                "Laser-nest PDF %s exceeds the %d-byte native cap; using text fallback",
                file_name,
                _MAX_NATIVE_PDF_BYTES,
            )
            extraction = extract_text_from_document(pdf_path)
            text = extraction.text or ""
            is_ocr = extraction.is_ocr

            ocr_note = (
                "\n\nNote: This text was extracted via OCR and may contain errors. "
                "Be extra careful with the numeric fields."
                if is_ocr
                else ""
            )

            user_prompt = f"""Extract laser-nest report data from the following text. Return JSON matching this schema exactly:

{LASER_NEST_EXTRACTION_SCHEMA}

Important:
- The numeric fields (CNC number, sheet size, thickness) may be glued together with no delimiters — separate them carefully.
- The material grade may appear on a different line than the CNC number and thickness (often the machine line); do not confuse the machine name with the material.
{cnc_hint_line}
- Preserve values exactly as shown; do not normalize or reformat them.
- If a field is not found, set it to null and set its confidence to "low".
{ocr_note}

Document Text:
---
{text}
---

Return ONLY the JSON object, no other text."""

            messages = [{"role": "user", "content": user_prompt}]
            ctx = LLMTaskContext(task="laser_nest_extraction", input_chars=len(text), is_ocr=is_ocr)

        llm_result = run_llm_task(
            ctx,
            messages=messages,
            system=LASER_NEST_EXTRACTION_PROMPT.text,
            max_tokens=1024,
            company_id=company_id,
            feature="laser_nest_extraction",
            prompt_version=LASER_NEST_EXTRACTION_PROMPT.version,
            timeout=LLM_API_TIMEOUT_SECONDS,
        )

        result = json.loads(_strip_json_fences(llm_result.text))

        # Pass 2: independent verification read, merged per the field policy.
        # Runs only after a successful pass-1 parse (the isinstance guard lets a
        # non-object pass-1 payload fall into the existing catch-all degrade
        # below) and can never fail this function -- a pass-2 fault keeps the
        # pass-1 result untouched with a skip warning.
        passes = 1
        verification_warning: Optional[str] = None
        verification_meta: Dict[str, Any] = {}
        if isinstance(result, dict):
            passes, verification_warning, verification_meta = _run_verification_pass(
                result,
                file_name=file_name,
                company_id=company_id,
                pdf_b64=pdf_b64 if use_native else None,
                text=None if use_native else text,
                is_ocr=is_ocr,
                filename_is_cnc_hint=filename_is_cnc_hint,
            )

        # Fallback: if neither pass could pin the CNC number, use the filename
        # stem (equals the CNC program number on every observed sample) -- but
        # ONLY when the filename genuinely carries that meaning. A synthetic
        # bare-PDF segment name stays null so the wizard amber-flags the field
        # for the planner instead of importing a fabricated 'nest-p001'.
        cnc_number = result.get("cnc_number")
        if not cnc_number:
            if filename_is_cnc_hint:
                result["cnc_number"] = Path(file_name).stem
                result["source"] = "filename"
            else:
                result["cnc_number"] = None
                result["source"] = "ai"
                if isinstance(result.get("confidence"), dict):
                    result["confidence"]["cnc_number"] = "low"
        else:
            result["source"] = "ai"

        # None when verification ran (or wasn't applicable); the skip note when
        # pass 2 failed -- the pass-1 field values themselves stay untouched.
        result["warning"] = verification_warning
        result["passes"] = passes
        result["_extraction_metadata"] = {
            "extracted_at": to_utc_iso(datetime.utcnow()),
            "source_was_ocr": is_ocr,
            "input_mode": input_mode,
            "model": llm_result.model,
            "model_tier": llm_result.tier,
            "model_selection_reason": llm_result.model_selection_reason,
            "prompt_version": llm_result.prompt_version,
            **verification_meta,
        }

        logger.info(
            "Laser-nest extraction successful: cnc=%s source=%s mode=%s passes=%d using %s",
            result.get("cnc_number"),
            result["source"],
            input_mode,
            passes,
            llm_result.model,
        )
        return result

    except LLMNotConfiguredError as e:
        logger.error(str(e))
        return _create_empty_nest_result(
            file_name, _not_configured_message(e), is_ocr, input_mode, filename_is_cnc_hint
        )
    except LLMEgressDisabledError as e:
        logger.warning(str(e))
        return _create_empty_nest_result(
            file_name,
            "AI extraction is disabled for this company (allow_ai_egress is off)",
            is_ocr,
            input_mode,
            filename_is_cnc_hint,
        )
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse laser-nest LLM response as JSON: {e}")
        return _create_empty_nest_result(
            file_name, f"Invalid JSON response: {str(e)}", is_ocr, input_mode, filename_is_cnc_hint
        )
    except Exception as e:
        if is_anthropic_api_error(e):
            logger.error(f"Anthropic API error: {e}")
            return _create_empty_nest_result(
                file_name, f"API error: {str(e)}", is_ocr, input_mode, filename_is_cnc_hint
            )
        logger.error(f"Laser-nest extraction failed: {e}")
        return _create_empty_nest_result(
            file_name, f"Extraction failed: {str(e)}", is_ocr, input_mode, filename_is_cnc_hint
        )


def _create_empty_nest_result(
    file_name: str,
    error_message: str,
    is_ocr: bool = False,
    input_mode: str = "native_pdf",
    filename_is_cnc_hint: bool = True,
) -> Dict[str, Any]:
    """A well-formed result for the degrade-gracefully path.

    Filename-only when the filename genuinely is the CNC program number;
    all-null (``source="none"``) for synthetic bare-PDF segment names, so a
    degraded preview can never fabricate 'nest-p001' CNC numbers.
    """
    return {
        "cnc_number": Path(file_name).stem if filename_is_cnc_hint else None,
        "material": None,
        "thickness": None,
        "sheet_size": None,
        "planned_runs": None,
        "confidence": {
            "cnc_number": "low",
            "material": "low",
            "thickness": "low",
            "sheet_size": "low",
            "planned_runs": "low",
        },
        "extraction_confidence": "low",
        "source": "filename" if filename_is_cnc_hint else "none",
        "warning": error_message,
        "passes": 1,
        "_extraction_metadata": {
            "extracted_at": to_utc_iso(datetime.utcnow()),
            "source_was_ocr": is_ocr,
            "input_mode": input_mode,
            "model": None,
        },
    }


def _default_segmentation(page_count: int, warning: Optional[str]) -> Dict[str, Any]:
    """The degrade default: one nest per page, nothing skipped, low confidence.

    Chosen because it is the SAFE wrong answer: a continuation page that should
    have been merged shows up as a bogus extra row the planner deletes in the
    wizard; the reverse error (two nests silently merged) would lose a nest.
    """
    return {
        "nests": [{"pages": [page], "cnc_number_hint": None} for page in range(1, page_count + 1)],
        "skipped_pages": [],
        "confidence": "low",
        "warning": warning,
    }


def _validate_segmentation(parsed: Any, page_count: int) -> Optional[Dict[str, Any]]:
    """Strictly validate a segmentation response; None means "use the default".

    Enforces the prompt's own rules: at least one nest; every page 1..page_count
    appears exactly once across nests + skipped_pages; pages within a nest are
    consecutive ascending ints. An all-pages-skipped response also fails (zero
    preview rows would dead-end the wizard). Normalizes on the way out: nests
    sorted by first page (so split order == page order), hints coerced to
    str-or-None, confidence coerced into the high/medium/low vocabulary.
    """
    if not isinstance(parsed, dict):
        return None
    raw_nests = parsed.get("nests")
    raw_skipped = parsed.get("skipped_pages", [])
    if not isinstance(raw_nests, list) or not raw_nests or not isinstance(raw_skipped, list):
        return None

    def _is_page(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= page_count

    if not all(_is_page(page) for page in raw_skipped):
        return None

    nests: list[Dict[str, Any]] = []
    seen: set[int] = set(raw_skipped)
    if len(seen) != len(raw_skipped):
        return None
    for entry in raw_nests:
        if not isinstance(entry, dict):
            return None
        pages = entry.get("pages")
        if not isinstance(pages, list) or not pages or not all(_is_page(page) for page in pages):
            return None
        if any(later != earlier + 1 for earlier, later in zip(pages, pages[1:])):
            return None
        if seen.intersection(pages):
            return None
        seen.update(pages)
        hint = entry.get("cnc_number_hint")
        hint = hint.strip() if isinstance(hint, str) and hint.strip() else None
        nests.append({"pages": list(pages), "cnc_number_hint": hint})

    if seen != set(range(1, page_count + 1)):
        return None

    confidence = parsed.get("confidence")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    nests.sort(key=lambda nest: nest["pages"][0])
    return {"nests": nests, "skipped_pages": sorted(raw_skipped), "confidence": confidence}


def segment_nest_pdf(
    pdf_path: str, file_name: str, page_count: int, company_id: Optional[int] = None
) -> Dict[str, Any]:
    """Pass 0: decide which pages of a bare multi-page PDF form which nest.

    Never raises. Returns ``{"nests": [{"pages": [...], "cnc_number_hint":
    str|None}], "skipped_pages": [...], "confidence": "high|medium|low",
    "warning": str|None}`` with nests ordered by first page. ANY failure — LLM
    unconfigured, egress disabled, oversized PDF, API error, bad JSON, a
    response failing strict validation — degrades to one nest per page with
    ``confidence "low"`` and a warning, so segmentation can never sink an
    upload; the planner just gets more rows to prune.

    A single-page PDF short-circuits without an LLM call (one nest, page [1]).
    Callers enforce the ``LASER_PDF_PACKAGE_MAX`` page cap BEFORE calling, so
    this function never sees an over-cap document.
    """
    if page_count <= 1:
        return {
            "nests": [{"pages": [1], "cnc_number_hint": None}],
            "skipped_pages": [],
            "confidence": "high",
            "warning": None,
        }

    try:
        # Size gate BEFORE the bytes are loaded: an oversized upload must not be
        # pulled into RAM just to learn it is oversized (stat is enough for a
        # local temp file; s3 refs never reach segmentation).
        if _pdf_size_exceeds_native_cap(pdf_path):
            # Segmentation is layout work; there is no useful flattened-text
            # fallback for "which page is a title page". Degrade instead.
            return _default_segmentation(
                page_count, "PDF is too large for AI segmentation; defaulted to one nest per page"
            )
        pdf_bytes = _read_pdf_bytes(pdf_path)
        if len(pdf_bytes) > _MAX_NATIVE_PDF_BYTES:
            # Backstop for sources the stat probe can't size (s3 refs).
            return _default_segmentation(
                page_count, "PDF is too large for AI segmentation; defaulted to one nest per page"
            )
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()

        instruction = f"""Group the pages of the attached laser-nest report PDF into nests. The document has exactly {page_count} pages (1-based). The source filename is '{file_name}'.

Return ONLY the JSON object, no other text."""

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": instruction},
                ],
            }
        ]
        ctx = LLMTaskContext(task="laser_nest_segmentation", input_chars=0, has_pdf_document=True)
        llm_result = run_llm_task(
            ctx,
            messages=messages,
            system=LASER_NEST_SEGMENTATION_PROMPT.text,
            max_tokens=2048,
            company_id=company_id,
            feature="laser_nest_segmentation",
            prompt_version=LASER_NEST_SEGMENTATION_PROMPT.version,
            timeout=LLM_API_TIMEOUT_SECONDS,
        )

        parsed = json.loads(_strip_json_fences(llm_result.text))
        validated = _validate_segmentation(parsed, page_count)
        if validated is None:
            logger.warning("Laser-nest segmentation response failed validation for %s; using default", file_name)
            return _default_segmentation(
                page_count, "AI segmentation response failed validation; defaulted to one nest per page"
            )
        validated["warning"] = None
        logger.info(
            "Laser-nest segmentation for %s: %d pages -> %d nests, %d skipped (%s)",
            file_name,
            page_count,
            len(validated["nests"]),
            len(validated["skipped_pages"]),
            validated["confidence"],
        )
        return validated

    except LLMNotConfiguredError as e:
        logger.error(str(e))
        return _default_segmentation(page_count, _not_configured_message(e))
    except LLMEgressDisabledError as e:
        logger.warning(str(e))
        return _default_segmentation(
            page_count, "AI segmentation is disabled for this company (allow_ai_egress is off)"
        )
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse laser-nest segmentation response as JSON: {e}")
        return _default_segmentation(page_count, f"Invalid JSON response: {str(e)}")
    except Exception as e:
        if is_anthropic_api_error(e):
            logger.error(f"Anthropic API error during laser-nest segmentation: {e}")
            return _default_segmentation(page_count, f"API error: {str(e)}")
        logger.error(f"Laser-nest segmentation failed: {e}")
        return _default_segmentation(page_count, f"Segmentation failed: {str(e)}")
