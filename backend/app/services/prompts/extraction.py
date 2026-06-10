"""Versioned prompts for PO/quote and BOM document extraction.

Moved verbatim from ``app/services/llm_service.py`` (v1.0.0) — the text is
byte-identical to what shipped there so extraction behavior is unchanged.
"""

from app.services.prompts.base import Prompt

# Extraction schema for PO/quote documents (interpolated into the user prompt).
PO_EXTRACTION_SCHEMA = """
{
  "document_type": "string - 'po' or 'quote'",
  "po_number": "string - the purchase order number",
  "quote_number": "string - the quote number (if document is a quote)",
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

BOM_EXTRACTION_SCHEMA = """
{
  "document_type": "string - 'bom' or 'part'",
  "assembly": {
    "part_number": "string or null",
    "name": "string or null",
    "revision": "string or null",
    "description": "string or null",
    "drawing_number": "string or null",
    "part_type": "string - manufactured, assembly, purchased, raw_material, hardware, consumable"
  },
  "items": [
    {
      "line_number": "integer",
      "part_number": "string or null",
      "description": "string",
      "quantity": "number",
      "unit_of_measure": "string - EA, LB, FT, IN, etc.",
      "item_type": "string - make, buy, phantom",
      "line_type": "string - component, hardware, consumable, reference",
      "reference_designator": "string or null",
      "find_number": "string or null",
      "notes": "string or null",
      "confidence": "high, medium, or low"
    }
  ],
  "extraction_confidence": "high, medium, or low - overall confidence"
}
"""

_EXTRACTION_SYSTEM_PROMPT_TEXT = """You are a purchasing document extraction assistant specialized in manufacturing and fabrication industry documents. Your task is to extract structured data from purchase orders and vendor quotes.

Key guidelines:
1. Extract all fields according to the schema provided
2. For part numbers, preserve exact formatting (dashes, spaces, etc.)
3. For dates, convert to YYYY-MM-DD format
4. For monetary values, extract as numbers without currency symbols
5. If a field is unclear or ambiguous, set confidence to "low"
6. If a field is not found, set to null
7. Pay attention to quantity, unit price, and line totals - verify they make sense
8. Look for common PO/quote formats: header info, line items table, totals section
9. Set document_type to "quote" when the document is a vendor quote, otherwise "po"

Return ONLY valid JSON matching the schema. No explanations or markdown."""

# PO and BOM extraction share the same system prompt text today but are
# versioned independently so either can evolve on its own.
PO_EXTRACTION_PROMPT = Prompt(
    id="po_extraction",
    version="1.0.0",
    text=_EXTRACTION_SYSTEM_PROMPT_TEXT,
)

BOM_EXTRACTION_PROMPT = Prompt(
    id="bom_extraction",
    version="1.0.0",
    text=_EXTRACTION_SYSTEM_PROMPT_TEXT,
)
