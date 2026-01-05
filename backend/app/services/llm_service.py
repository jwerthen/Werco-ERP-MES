"""
LLM Service for Purchase Order Data Extraction
Uses Claude API to extract structured data from PDF text.
"""
import os
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

# Extraction schema for LLM
EXTRACTION_SCHEMA = """
{
  "po_number": "string - the purchase order number",
  "vendor": {
    "name": "string - vendor/supplier company name",
    "address": "string - full address if available"
  },
  "order_date": "YYYY-MM-DD format or null",
  "expected_delivery_date": "YYYY-MM-DD format or null",
  "required_date": "YYYY-MM-DD format or null",
  "payment_terms": "string or null",
  "shipping_method": "string or null",
  "ship_to": "string - shipping address or null",
  "line_items": [
    {
      "line_number": "integer",
      "part_number": "string - the part/item number",
      "description": "string - item description",
      "qty_ordered": "number",
      "unit_of_measure": "string - EA, LB, FT, etc.",
      "unit_price": "number",
      "line_total": "number",
      "confidence": "high, medium, or low"
    }
  ],
  "subtotal": "number or null",
  "tax": "number or null",
  "shipping_cost": "number or null",
  "total_amount": "number",
  "notes": "string - any special instructions or notes",
  "extraction_confidence": "high, medium, or low - overall confidence"
}
"""

SYSTEM_PROMPT = """You are a purchase order data extraction assistant specialized in manufacturing and fabrication industry documents. Your task is to extract structured data from purchase order text.

Key guidelines:
1. Extract all fields according to the schema provided
2. For part numbers, preserve exact formatting (dashes, spaces, etc.)
3. For dates, convert to YYYY-MM-DD format
4. For monetary values, extract as numbers without currency symbols
5. If a field is unclear or ambiguous, set confidence to "low"
6. If a field is not found, set to null
7. Pay attention to quantity, unit price, and line totals - verify they make sense
8. Look for common PO formats: header info, line items table, totals section

Return ONLY valid JSON matching the schema. No explanations or markdown."""


def extract_po_data_with_llm(pdf_text: str, is_ocr: bool = False) -> Dict[str, Any]:
    """
    Use Claude API to extract structured PO data from text.
    """
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic package not installed")
        return _create_empty_result("LLM library not available")
    
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return _create_empty_result("API key not configured")
    
    # Prepare the extraction prompt
    ocr_note = "\n\nNote: This text was extracted via OCR and may contain errors. Be extra careful with numbers and part numbers." if is_ocr else ""
    
    user_prompt = f"""Extract purchase order data from the following text. Return JSON matching this schema exactly:

{EXTRACTION_SCHEMA}

Important:
- Extract ALL line items found in the document
- Part numbers must be exact as shown
- Verify quantities and prices make logical sense
- Flag any uncertain extractions with low confidence
{ocr_note}

Purchase Order Text:
---
{pdf_text}
---

Return ONLY the JSON object, no other text."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
            system=SYSTEM_PROMPT
        )
        
        response_text = message.content[0].text.strip()
        
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
            "model": "claude-sonnet-4-20250514"
        }
        
        logger.info(f"LLM extraction successful: {len(result.get('line_items', []))} line items")
        return result
        
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response as JSON: {e}")
        return _create_empty_result(f"Invalid JSON response: {str(e)}")
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return _create_empty_result(f"API error: {str(e)}")
    except Exception as e:
        logger.error(f"LLM extraction failed: {e}")
        return _create_empty_result(f"Extraction failed: {str(e)}")


def _create_empty_result(error_message: str) -> Dict[str, Any]:
    """Create an empty result with error message."""
    return {
        "po_number": None,
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
        "_extraction_metadata": {
            "extracted_at": datetime.utcnow().isoformat(),
            "source_was_ocr": False,
            "model": None
        }
    }


def validate_extracted_data(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Validate extracted PO data and return list of issues.
    """
    issues = []
    
    # Check PO number
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
            if not item.get("part_number"):
                issues.append({"field": f"line_items[{i}].part_number", "severity": "error", "message": f"Line {i+1}: Part number required"})
            if not item.get("qty_ordered") or item.get("qty_ordered", 0) <= 0:
                issues.append({"field": f"line_items[{i}].qty_ordered", "severity": "error", "message": f"Line {i+1}: Quantity must be > 0"})
            if item.get("confidence") == "low":
                issues.append({"field": f"line_items[{i}]", "severity": "warning", "message": f"Line {i+1}: Low confidence - please verify"})
    
    # Check total
    if not data.get("total_amount") and line_items:
        issues.append({"field": "total_amount", "severity": "warning", "message": "Total amount not found"})
    
    # Overall confidence
    if data.get("extraction_confidence") == "low":
        issues.append({"field": "extraction_confidence", "severity": "warning", "message": "Overall extraction confidence is low - please review all fields"})
    
    return issues
