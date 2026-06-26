# AI Eval Harness

Golden-fixture evals for the LLM extraction pipelines (PO/quote and BOM), plus a
synthetic-fixture eval for the laser-nest native-PDF extraction path. Excluded
from the default pytest run via the `evals` marker.

## Running

```bash
# Offline (default): scores stored golden outputs — no API key, no network
pytest -m evals tests/evals

# Live: re-runs each case against the real Anthropic API and scores the result
RUN_LIVE_EVALS=1 ANTHROPIC_API_KEY=sk-ant-... pytest -m evals tests/evals
```

## Layout

- `golden/*.json` — one file per case:
  - `input_text` / `is_ocr` — what the pipeline receives
  - `stored_output` — a previously captured model output (offline mode scores this)
  - `expected` — ground truth the scorer compares against
  - `thresholds` — minimum scores the case must reach
- `scoring.py` — deterministic scorers (field accuracy, line-item recall/precision)
- `test_extraction_evals.py` — offline + live-gated PO/BOM tests
- `nest_fixtures.py` + `test_laser_nest_evals.py` — the laser-nest native-PDF
  eval. Instead of golden JSON it SYNTHESIZES nest-report PDFs with reportlab
  (a digital text-layer sheet and an image-only "scanned" variant) with known
  field values, then scores `cnc_number`/`material`/`thickness`/`sheet_size`.
  Offline it builds the fixtures and exercises the scorer; live
  (`RUN_LIVE_EVALS=1` + `ANTHROPIC_API_KEY`) runs the real native-PDF extraction.
  This measures the layout-aware native-PDF path vs. the text-flatten baseline —
  the fixtures put the material grade on a separate machine line on purpose.

## Adding a case

1. Drop a new `golden/<task>_<nnn>.json` with the fields above. Capture
   `stored_output` from a real run (the `_extraction_metadata` key is stripped
   before scoring). **Golden fixtures must be synthetic or fully sanitized** —
   never commit real customer/vendor names, part numbers, prices, or any other
   content from production documents.
2. Set `thresholds` to what the stored output actually achieves — evals catch
   regressions, they are not aspirational targets.
3. Bump thresholds deliberately when a prompt-version change improves scores
   (and note it in `app/services/prompts/CHANGELOG.md`).
