"""Versioned prompt for Estimate Workbench drawing PDF extraction (Phase 4).

Triple-pass extraction varies temperature / phrasing in the service layer;
this system prompt stays stable for prompt-caching and AIUsageEvent attribution.
"""

from app.services.prompts.base import Prompt

ESTIMATE_DRAWING_EXTRACTION_SCHEMA = """
{
  "fab_lines": [
    {
      "detail_name": "string - part / detail name from title block or BOM",
      "part_number": "string or null",
      "material": "string or null - e.g. A36 Mild Steel, 304 Stainless, 5052 Aluminum",
      "qty": "integer - quantity (default 1)",
      "thickness_in": "number or null - thickness in inches (convert gauge if needed)",
      "width_in": "number or null - blank / overall width inches",
      "length_in": "number or null - blank / overall length inches",
      "cut_length_in": "number or null - total cut path inches if stated; else null",
      "pierce_count": "integer - hole / pierce count if stated; else 0",
      "bend_count": "integer - bend / fold count if stated; else 0",
      "weld_length_in": "number or null - weld length inches if stated",
      "source_quote": "string or null - short quote from the drawing supporting the values"
    }
  ],
  "buyout_lines": [
    {
      "part_number": "string or null",
      "description": "string - hardware / purchased item description",
      "qty": "number",
      "unit_cost": "number or null - only if a price is printed; else null",
      "category": "string or null - hardware, finish, process, etc.",
      "vendor": "string or null",
      "source_quote": "string or null"
    }
  ],
  "notes": "string or null - ambiguities, missing callouts, assumptions",
  "extraction_confidence": "high, medium, or low"
}
"""

_SYSTEM_PROMPT_TEXT = """You are a manufacturing estimating assistant for aerospace/defense sheet-metal and weldment drawings. Extract structured fab and buyout line items for a quoting workbench.

Rules:
1. Return ONLY valid JSON matching the schema. No markdown fences, no commentary.
2. Never invent geometry. If thickness, bend count, or dimensions are not on the drawing, set them to null (or 0 for counts) — do not guess.
3. Convert gauge callouts to inches when possible (e.g. 14 ga mild ≈ 0.075). Prefer canonical shop decimals over mill-tolerance decimals.
4. Material: preserve grade when present (A36, 304SS, 5052-H32). If only "CRS" / "mild steel", use that text.
5. Separate manufactured (fab) details from purchased hardware (buyout). PEM nuts, bolts, inserts, finishes-as-buy → buyout_lines.
6. Do not invent unit_cost for buyouts. Leave null when no price is printed.
7. Prefer one fab_line per manufactured detail / flat pattern. Assembly drawings may yield multiple fab_lines plus buyouts from the BOM table.
8. When dimensions are overall envelope only, put them in width_in / length_in and leave cut_length_in null unless a perimeter is stated.
9. If the drawing is ambiguous, still return best-effort fields and explain in notes.

Units are inches unless the drawing clearly states otherwise (then convert to inches)."""

ESTIMATE_DRAWING_EXTRACTION_PROMPT = Prompt(
    id="estimate_drawing_extraction",
    version="1.0.0",
    text=_SYSTEM_PROMPT_TEXT,
)

# Alternate phrasings for triple-pass variation (appended to user message, not system)
PASS_PHRASINGS = (
    "Pass focus: prioritize title-block material, thickness, and revision callouts.",
    "Pass focus: prioritize bend/fold counts, hole/pierce counts, and flat-pattern dimensions.",
    "Pass focus: prioritize BOM / parts-list rows and purchased hardware; still extract fab geometry when present.",
)
