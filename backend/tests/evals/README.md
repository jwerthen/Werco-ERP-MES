# AI Eval Harness

Golden-fixture evals for the LLM extraction pipelines (PO/quote and BOM).
Excluded from the default pytest run via the `evals` marker.

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
- `test_extraction_evals.py` — offline + live-gated tests

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
