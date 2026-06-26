# Prompt Registry Changelog

Bump a prompt's semver `version` and add an entry here whenever its text or
request layout changes. The version string is recorded on `AIUsageEvent`
(every API call) and on `AIInteractionEvent`/`AIRecommendation` learning rows.

## 2026-06-24

- `laser_nest_extraction` 1.0.0 → 1.1.0 — request layout changed: the primary
  path now sends the nest report as a base64 PDF `document` content block
  (layout-aware vision) instead of a flattened-text user message, so the model
  reads the rendered sheet with its 2-D layout. The system prompt was reworded
  to describe reading the rendered sheet (each labeled field / table cell /
  title-block entry as a distinct value at its own position), keeping a softened
  glued-digits/OCR warning for the flattened-text fallback (PDFs that can't be
  read natively or exceed the 20 MB native cap). Rationale: fixes the
  glued-digits and material-grade-on-the-wrong-line extraction errors that came
  from flattening a 2-D nest sheet into a 1-D string. Native-PDF calls carry
  `input_chars~=0` and set the new `has_pdf_document` flag on `LLMTaskContext`,
  which lifts model selection off the FAST (Haiku) tier onto DEFAULT (Sonnet);
  `_extraction_metadata` now records `input_mode` (`native_pdf` | `text`).

## 2026-06-23

- `laser_nest_extraction` 1.0.0 — new prompt for extracting fields (CNC number,
  material grade, thickness, sheet size, optional planned runs) from CAM
  laser-nest report PDFs (SigmaNEST / Ermaksan style). Sent as the `system`
  argument to `run_llm_task` (the deterministic cacheable prefix); the variable
  document text + filename hint travel in the user message. The prompt warns
  that the extracted text glues the numeric fields together without delimiters
  and that the material grade sits on a different visual line than the CNC
  number/thickness. The task is unrouted in `llm_model_router`, so short clean
  nest text resolves to the FAST (Haiku) tier — appropriate for this cheap
  extraction workload. Confirm caching engages via `cache_read_tokens` on
  `laser_nest_extraction` rows in `ai_usage_events` once Haiku's minimum
  cacheable prefix (4096 tokens) is met; below that the system prefix is a
  harmless no-op and the call is uncached.

## 2026-06-10

- `copilot_chat` 1.0.0 — new system prompt for Werco Copilot v1 (read-only
  tool-use chat over tenant ERP data). Sent as a `system` block with
  `cache_control: ephemeral`; the deterministic tool schemas render before it,
  so tools + system are cached together and re-read on every iteration of the
  tool-use loop. Confirm the cache engages via `cache_read_tokens` on
  `copilot_chat` rows in `ai_usage_events` (Sonnet's minimum cacheable prefix
  is 2048 tokens; below that the breakpoint is a harmless no-op).
- `nl_search_intent` 1.0.0 — new fast-tier intent parser for `/search/nl`.
  Emits the same filter structure as the rule parser; the rule parser always
  runs first and the LLM is skipped when rules already score high confidence.

## 2026-06-09

- `po_extraction` 1.0.0 — system prompt + schema moved verbatim from
  `app/services/llm_service.py` (no text change; baseline version).
- `bom_extraction` 1.0.0 — shares the extraction system prompt text with
  `po_extraction`; versioned independently (baseline).
- `routing_generation` 1.1.0 — text moved verbatim from
  `app/services/routing_generation_service.py`. Request layout changed for
  prompt caching: system prompt, schema/allowed work-center types, and the
  learned-examples context now travel as cacheable `system` blocks
  (`cache_control: ephemeral`) instead of being inlined in the user prompt.
  Model-visible content is equivalent. Note: Anthropic only engages the cache
  above a minimum prefix length (1024 tokens on Sonnet/Opus); confirm it is
  actually engaging by checking `cache_creation_tokens`/`cache_read_tokens` on
  `routing_generation` rows in `ai_usage_events` — below the minimum the
  breakpoints are harmless no-ops.
- `qms_clause_extraction` 1.0.0 — version registration only; prompt text
  remains inline in `app/api/endpoints/qms_standards.py` (baseline).
