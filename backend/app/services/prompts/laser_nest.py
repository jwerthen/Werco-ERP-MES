"""Versioned prompt for laser-nest report PDF field extraction.

These are CAM nest-report sheets (SigmaNEST / Ermaksan style), one per laser
sheet. The primary path now sends the rendered PDF page to the model as a
``document`` content block so it reads the sheet WITH its 2-D layout, which
fixes the glued-digits and material-grade-on-the-wrong-line failures that came
from flattening the sheet to a 1-D string. The fallback path still feeds the
flattened extracted text (when the PDF bytes can't be read or exceed the native
size cap), so the prompt keeps a softened warning for that case.
"""

from app.services.prompts.base import Prompt

# Extraction schema for laser-nest report sheets (interpolated into the user prompt).
LASER_NEST_EXTRACTION_SCHEMA = """
{
  "cnc_number": "string or null - the CNC program / nest number (e.g. '05749')",
  "material": "string or null - the material grade (e.g. 'A36', '304SS', 'Stainless Steel')",
  "thickness": "string or null - material thickness, include units when present (e.g. '0.25in', '0.063')",
  "sheet_size": "string or null - sheet/material size (e.g. '48x96' or a single dimension like '72.5')",
  "planned_runs": "integer or null - the sheet/run count if stated on the sheet",
  "confidence": {
    "cnc_number": "high, medium, or low",
    "material": "high, medium, or low",
    "thickness": "high, medium, or low",
    "sheet_size": "high, medium, or low",
    "planned_runs": "high, medium, or low"
  },
  "extraction_confidence": "high, medium, or low - overall confidence"
}
"""

_SYSTEM_PROMPT_TEXT = """You are a manufacturing document extraction assistant specialized in CAM laser-nest report sheets (SigmaNEST / Ermaksan style). Each sheet describes one laser-cut nest. Your task is to read the sheet and extract structured nest metadata.

You usually receive the nest report as a rendered PDF page — read it with its visual layout, treating each labeled field, table cell, and title-block entry as a distinct value at its own position on the sheet. (When you instead receive flattened, extracted text, the same fields are present but the 2-D layout is lost, so apply the warnings below with extra care.)

Extract these fields:
1. cnc_number - the CNC program / nest number (often a short zero-padded number such as "05749").
2. material - the material grade only (e.g. "A36", "SS", "304SS", "Stainless Steel"). This is NOT the machine name.
3. thickness - the material thickness; preserve units when present (e.g. "0.25in", "0.063", "0.135"). Units are often implied as inches.
4. sheet_size - the sheet/material size; this may be a single dimension (e.g. "72.5") or two numbers (e.g. "96x48").
5. planned_runs - the sheet or run count, if the sheet states one. Otherwise null.

Key extraction guidance:
- The material grade often sits on a DIFFERENT line or in a different block than the CNC number and thickness — frequently on the machine line (e.g. "Ermaksan Laser / Beckhoff A36 ..."). Do not confuse the machine name with the material grade.
- Keep adjacent numeric fields (CNC number, sheet size, thickness) separate — read each from its own labeled position rather than merging neighboring numbers into one value. WARNING: in the flattened-text fallback these numeric fields are frequently glued together with NO delimiters, so separating them is especially important there.
- The source filename frequently equals the CNC program number and will be provided as a hint; use it for cnc_number when the sheet is ambiguous.
- Preserve values exactly as they appear; do not normalize, round, or reformat them.
- If a field is unclear, ambiguous, or not found, set that field to null and set its confidence to "low".

Return ONLY valid JSON matching the schema. No explanations or markdown."""

LASER_NEST_EXTRACTION_PROMPT = Prompt(
    id="laser_nest_extraction",
    version="1.1.0",
    text=_SYSTEM_PROMPT_TEXT,
)
