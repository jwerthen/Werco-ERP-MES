"""Laser-nest report PDF field extraction.

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

Contract: this function NEVER raises. A bad, odd, or unconfigured PDF degrades
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

from app.services.llm_client import (
    LLMEgressDisabledError,
    LLMNotConfiguredError,
    is_anthropic_api_error,
    run_llm_task,
)
from app.services.llm_model_router import LLMTaskContext
from app.services.llm_service import LLM_API_TIMEOUT_SECONDS
from app.services.pdf_service import extract_text_from_document
from app.services.prompts import LASER_NEST_EXTRACTION_PROMPT, LASER_NEST_EXTRACTION_SCHEMA
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


def extract_nest_fields_from_pdf(pdf_path: str, file_name: str, company_id: Optional[int] = None) -> Dict[str, Any]:
    """Extract laser-nest fields from a PDF. Never raises.

    Args:
        pdf_path: Local path (or storage ref) to the nest report PDF.
        file_name: Original filename; used as the CNC-number fallback and as a
            hint to the model (the filename stem usually equals the CNC number).
        company_id: Active company for tenant-scoped AI usage telemetry.

    Returns:
        A dict with stable keys callers depend on: ``cnc_number``, ``material``,
        ``thickness``, ``sheet_size``, ``planned_runs``, ``confidence``,
        ``source`` ("ai" | "filename"), ``warning`` (None on success), and
        ``_extraction_metadata``.
    """
    # Defaults BEFORE the try so the degrade paths below can always reference
    # them, even if reading the bytes / text extraction raises (see in-try calls
    # below). is_ocr stays False on the native path (no OCR happens there);
    # input_mode records which path produced the row.
    is_ocr = False
    input_mode = "native_pdf"

    try:
        # Reading the bytes is inside the try so a read error (missing file,
        # s3 hiccup) degrades to a filename-only result rather than propagating
        # -- the never-raises contract covers the whole pipeline. A read failure
        # is fatal to BOTH paths (the text path needs the file too), so it falls
        # straight through to the degrade handlers.
        pdf_bytes = _read_pdf_bytes(pdf_path)

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
- The source filename is '{file_name}', which is usually the CNC program number — use it for cnc_number if the sheet is ambiguous.
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
            # to text and use the glued-digits/OCR-aware user prompt.
            input_mode = "text"
            logger.info(
                "Laser-nest PDF %s is %d bytes (> %d cap); using text fallback",
                file_name,
                len(pdf_bytes),
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
- The source filename is '{file_name}', which is usually the CNC program number — use it for cnc_number if the document text is ambiguous.
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

        response_text = llm_result.text.strip()

        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        result = json.loads(response_text.strip())

        # Fallback: if the model couldn't pin the CNC number, use the filename
        # stem (equals the CNC program number on every observed sample).
        cnc_number = result.get("cnc_number")
        if not cnc_number:
            result["cnc_number"] = Path(file_name).stem
            result["source"] = "filename"
        else:
            result["source"] = "ai"

        result["warning"] = None
        result["_extraction_metadata"] = {
            "extracted_at": datetime.utcnow().isoformat(),
            "source_was_ocr": is_ocr,
            "input_mode": input_mode,
            "model": llm_result.model,
            "model_tier": llm_result.tier,
            "model_selection_reason": llm_result.model_selection_reason,
            "prompt_version": llm_result.prompt_version,
        }

        logger.info(
            "Laser-nest extraction successful: cnc=%s source=%s mode=%s using %s",
            result.get("cnc_number"),
            result["source"],
            input_mode,
            llm_result.model,
        )
        return result

    except LLMNotConfiguredError as e:
        logger.error(str(e))
        return _create_empty_nest_result(file_name, _not_configured_message(e), is_ocr, input_mode)
    except LLMEgressDisabledError as e:
        logger.warning(str(e))
        return _create_empty_nest_result(
            file_name,
            "AI extraction is disabled for this company (allow_ai_egress is off)",
            is_ocr,
            input_mode,
        )
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse laser-nest LLM response as JSON: {e}")
        return _create_empty_nest_result(file_name, f"Invalid JSON response: {str(e)}", is_ocr, input_mode)
    except Exception as e:
        if is_anthropic_api_error(e):
            logger.error(f"Anthropic API error: {e}")
            return _create_empty_nest_result(file_name, f"API error: {str(e)}", is_ocr, input_mode)
        logger.error(f"Laser-nest extraction failed: {e}")
        return _create_empty_nest_result(file_name, f"Extraction failed: {str(e)}", is_ocr, input_mode)


def _create_empty_nest_result(
    file_name: str, error_message: str, is_ocr: bool = False, input_mode: str = "native_pdf"
) -> Dict[str, Any]:
    """A well-formed, filename-only result for the degrade-gracefully path."""
    return {
        "cnc_number": Path(file_name).stem,
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
        "source": "filename",
        "warning": error_message,
        "_extraction_metadata": {
            "extracted_at": datetime.utcnow().isoformat(),
            "source_was_ocr": is_ocr,
            "input_mode": input_mode,
            "model": None,
        },
    }
