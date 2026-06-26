"""
LLM Service for Purchase Order Data Extraction
Uses Claude API to extract structured data from PDF text.

All API plumbing (client, model routing, usage telemetry, cost estimation)
lives in ``app.services.llm_client.run_llm_task``. Prompt text is versioned in
``app.services.prompts``.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.llm_client import (
    LLMEgressDisabledError,
    LLMNotConfiguredError,
    is_anthropic_api_error,
    run_llm_task,
)
from app.services.llm_model_router import LLMTaskContext
from app.services.prompts import (
    BOM_EXTRACTION_PROMPT,
    BOM_EXTRACTION_SCHEMA,
    PO_EXTRACTION_PROMPT,
    PO_EXTRACTION_SCHEMA,
)

logger = logging.getLogger(__name__)

# Hard cap on Anthropic API calls. Without this the SDK can hang an
# entire request thread if the upstream API is slow or unreachable.
LLM_API_TIMEOUT_SECONDS = 60.0

# Backwards-compatible alias — canonical text now lives in app.services.prompts.
EXTRACTION_SCHEMA = PO_EXTRACTION_SCHEMA


def _not_configured_message(exc: LLMNotConfiguredError) -> str:
    return "LLM library not available" if exc.reason == "library" else "API key not configured"


# User-facing degrade message when the company's AI egress kill switch is off.
_EGRESS_DISABLED_MESSAGE = "AI extraction is disabled for your company (allow_ai_egress is off)"


def extract_bom_data_with_llm(pdf_text: str, is_ocr: bool = False, company_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Use Claude API to extract structured BOM/part data from text.
    """
    ocr_note = (
        "\n\nNote: This text was extracted via OCR and may contain errors. Be extra careful with quantities and part numbers."
        if is_ocr
        else ""
    )

    user_prompt = f"""Extract BOM or drawing data from the following text. Return JSON matching this schema exactly:

{BOM_EXTRACTION_SCHEMA}

Important:
- Detect whether this is a BOM/assembly list or a single-part drawing
- If it's a BOM, populate assembly info and all line items
- Classify line_type as hardware or consumable when appropriate (bolts, nuts, washers, adhesives, paint, oil, etc.)
- If a field is not found, set to null
- Preserve part numbers as-is
{ocr_note}

Document Text:
---
{pdf_text}
---

Return ONLY the JSON object, no other text."""

    try:
        llm_result = run_llm_task(
            LLMTaskContext(task="bom_extraction", input_chars=len(pdf_text), is_ocr=is_ocr),
            messages=[{"role": "user", "content": user_prompt}],
            system=BOM_EXTRACTION_PROMPT.text,
            max_tokens=4096,
            company_id=company_id,
            feature="bom_import",
            prompt_version=BOM_EXTRACTION_PROMPT.version,
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
        result["_extraction_metadata"] = {
            "extracted_at": datetime.utcnow().isoformat(),
            "source_was_ocr": is_ocr,
            "model": llm_result.model,
            "model_tier": llm_result.tier,
            "model_selection_reason": llm_result.model_selection_reason,
            "prompt_version": llm_result.prompt_version,
        }

        logger.info(
            "LLM BOM extraction successful: %s items using %s",
            len(result.get("items", [])),
            llm_result.model,
        )
        return result
    except LLMNotConfiguredError as e:
        logger.error(str(e))
        return _create_empty_bom_result(_not_configured_message(e))
    except LLMEgressDisabledError as e:
        logger.warning(str(e))
        return _create_empty_bom_result(_EGRESS_DISABLED_MESSAGE)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response as JSON: {e}")
        return _create_empty_bom_result(f"Invalid JSON response: {str(e)}")
    except Exception as e:
        if is_anthropic_api_error(e):
            logger.error(f"Anthropic API error: {e}")
            return _create_empty_bom_result(f"API error: {str(e)}")
        logger.error(f"LLM extraction failed: {e}")
        return _create_empty_bom_result(f"Extraction failed: {str(e)}")


def _create_empty_bom_result(error_message: str) -> Dict[str, Any]:
    """Create an empty BOM result with error message."""
    return {
        "document_type": None,
        "assembly": {
            "part_number": None,
            "name": None,
            "revision": None,
            "description": None,
            "drawing_number": None,
            "part_type": None,
        },
        "items": [],
        "extraction_confidence": "low",
        "_error": error_message,
        "_extraction_metadata": {"extracted_at": datetime.utcnow().isoformat(), "source_was_ocr": False, "model": None},
    }


def extract_po_data_with_llm(
    pdf_text: str,
    is_ocr: bool = False,
    document_type: str = "po",
    company_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Use Claude API to extract structured PO/quote data from text.
    """
    # Prepare the extraction prompt
    ocr_note = (
        "\n\nNote: This text was extracted via OCR and may contain errors. Be extra careful with numbers and part numbers."
        if is_ocr
        else ""
    )

    user_prompt = f"""Extract purchasing document data from the following text. Return JSON matching this schema exactly:

{EXTRACTION_SCHEMA}

Important:
- Extract ALL line items found in the document
- Part numbers must be exact as shown
- Verify quantities and prices make logical sense
- Flag any uncertain extractions with low confidence
- If the document is a quote, set document_type to "quote" and populate quote_number
- If the document is a PO, set document_type to "po" and populate po_number
{ocr_note}

Purchase Order Text:
---
{pdf_text}
---

Return ONLY the JSON object, no other text."""

    try:
        llm_result = run_llm_task(
            LLMTaskContext(
                task="po_extraction",
                input_chars=len(pdf_text),
                is_ocr=is_ocr,
                document_type=document_type,
            ),
            messages=[{"role": "user", "content": user_prompt}],
            system=PO_EXTRACTION_PROMPT.text,
            max_tokens=4096,
            company_id=company_id,
            feature="po_upload",
            prompt_version=PO_EXTRACTION_PROMPT.version,
            timeout=LLM_API_TIMEOUT_SECONDS,
        )

        response_text = llm_result.text.strip()

        # Clean up response if needed
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        result = json.loads(response_text.strip())

        # Add metadata
        result["_extraction_metadata"] = {
            "extracted_at": datetime.utcnow().isoformat(),
            "source_was_ocr": is_ocr,
            "model": llm_result.model,
            "model_tier": llm_result.tier,
            "model_selection_reason": llm_result.model_selection_reason,
            "prompt_version": llm_result.prompt_version,
        }

        logger.info(
            "LLM extraction successful: %s line items using %s",
            len(result.get("line_items", [])),
            llm_result.model,
        )
        return result

    except LLMNotConfiguredError as e:
        logger.error(str(e))
        return _create_empty_result(_not_configured_message(e))
    except LLMEgressDisabledError as e:
        logger.warning(str(e))
        return _create_empty_result(_EGRESS_DISABLED_MESSAGE)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response as JSON: {e}")
        return _create_empty_result(f"Invalid JSON response: {str(e)}")
    except Exception as e:
        if is_anthropic_api_error(e):
            logger.error(f"Anthropic API error: {e}")
            return _create_empty_result(f"API error: {str(e)}")
        logger.error(f"LLM extraction failed: {e}")
        return _create_empty_result(f"Extraction failed: {str(e)}")


def _create_empty_result(error_message: str) -> Dict[str, Any]:
    """Create an empty result with error message."""
    return {
        "document_type": None,
        "po_number": None,
        "quote_number": None,
        "vendor": {"name": None, "address": None},
        "order_date": None,
        "expected_delivery_date": None,
        "required_date": None,
        "payment_terms": None,
        "shipping_method": None,
        "ship_to": None,
        "line_items": [],
        "subtotal": None,
        "tax": None,
        "shipping_cost": None,
        "total_amount": None,
        "notes": None,
        "extraction_confidence": "low",
        "_error": error_message,
        "_extraction_metadata": {"extracted_at": datetime.utcnow().isoformat(), "source_was_ocr": False, "model": None},
    }


def validate_extracted_data(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Validate extracted PO data and return list of issues.
    """
    issues = []
    document_type = (data.get("document_type") or "po").lower()

    # Check PO number
    if document_type == "quote":
        if not data.get("po_number") and not data.get("quote_number"):
            issues.append({"field": "quote_number", "severity": "error", "message": "Quote number is required"})
    else:
        if not data.get("po_number"):
            issues.append({"field": "po_number", "severity": "error", "message": "PO number is required"})

    # Check vendor
    if not data.get("vendor", {}).get("name"):
        issues.append({"field": "vendor.name", "severity": "error", "message": "Vendor name is required"})

    # Check line items
    line_items = data.get("line_items", [])
    if not line_items:
        issues.append({"field": "line_items", "severity": "error", "message": "No line items found"})
    else:
        for i, item in enumerate(line_items):
            if not item.get("part_number") and not item.get("description"):
                issues.append(
                    {
                        "field": f"line_items[{i}].part_number",
                        "severity": "error",
                        "message": f"Line {i+1}: Part number or description required",
                    }
                )
            if not item.get("qty_ordered") or item.get("qty_ordered", 0) <= 0:
                issues.append(
                    {
                        "field": f"line_items[{i}].qty_ordered",
                        "severity": "error",
                        "message": f"Line {i+1}: Quantity must be > 0",
                    }
                )
            if item.get("confidence") == "low":
                issues.append(
                    {
                        "field": f"line_items[{i}]",
                        "severity": "warning",
                        "message": f"Line {i+1}: Low confidence - please verify",
                    }
                )

    # Check total
    if not data.get("total_amount") and line_items:
        issues.append({"field": "total_amount", "severity": "warning", "message": "Total amount not found"})

    # Overall confidence
    if data.get("extraction_confidence") == "low":
        issues.append(
            {
                "field": "extraction_confidence",
                "severity": "warning",
                "message": "Overall extraction confidence is low - please review all fields",
            }
        )

    return issues
