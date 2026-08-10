# Prompt Registry Changelog

Bump a prompt's semver `version` and add an entry here whenever its text or
request layout changes. The version string is recorded on `AIUsageEvent`
(every API call) and on `AIInteractionEvent`/`AIRecommendation` learning rows.

## 2026-08-10

- `sheet_stock_disambiguation` 1.0.0 — new prompt for the AI leg of laser-nest
  sheet-stock matching. It runs ONLY on the residue: the `ambiguous` rows
  `services/sheet_stock_matcher.py` refused to resolve, and only those carrying a
  shortlist. One call per preview covers every unresolved spec (rows are grouped
  by candidate part ids + refusal sentence, so a 42-nest package with three
  unresolved specs is three groups in one request), sent as the `system` argument
  with the groups in the user message. No `cache_control`: a single call carrying
  a per-request shortlist would write a cache block that is never read, at 1.25x.
  No `tools` either — `llm_client._first_text` returns "" when a response leads
  with a `tool_use` block, which would surface as an obscure JSONDecodeError
  instead of a clean parse.
  The prompt's framing is the point: the server has already gated thickness to
  0.002" and already dropped contradicting grades, so the model is told to take
  thickness off the table and answer only grade and size. It is told that `null`
  is a correct answer, that on-hand is context and never a tiebreaker, and that a
  part number must be copied character-for-character from that group's own
  shortlist. Response is strict JSON `{"picks": [{key, part_number, reason}]}`,
  capped at `max_tokens` 512 with `max_retries=0` and a 20s timeout because a
  planner is watching a spinner.
  None of this can pre-fill anything. `resolve_ambiguous_sheet_matches`
  re-resolves the returned part number by exact string match against the
  shortlist that group was given (the hallucination fence and the cross-tenant
  fence in one), drops any pick whose reason is blank, and on success only
  reorders the shortlist and stamps `basis='ai_disambiguated'`.
  `auto_fill_part_id` and `status` are never touched — that stays the
  deterministic gate's alone, because the tie depletes real inventory into an
  as-built record that never auto-reverses.
  Task `sheet_stock_disambiguation` is routed EXPLICITLY to the DEFAULT (Sonnet)
  tier in `llm_model_router`. Left unlisted it would fall through to complexity
  scoring, where a short prompt scores 0 and lands the hardest judgment in the
  feature on the cheapest model.

## 2026-08-05

- `laser_nest_extraction` 1.1.0 → 1.2.0 and `laser_nest_verification` 1.0.0 →
  1.1.0 — `planned_runs` guidance only; the other four fields are untouched and
  the request layout is unchanged. Both prompts previously described the field
  in one line ("the sheet or run count, if the sheet states one"), and it was
  the field the extractor missed by a wide margin: a 42-nest package came back
  with the count defaulted on essentially every row. The schema line and both
  system prompts now (a) define it as how many times the WHOLE nest is cut,
  (b) list the label vocabulary CAM post-processors actually print ("Sheets",
  "No. of Sheets", "Sheets Required", "Sheet Qty", "Nest Qty", "Qty", "Repeat",
  "Repetitions", "Runs", "Cycles", …) and say to match on meaning rather than
  exact wording, (c) name the specific confusion to avoid — the parts table's
  per-sheet and total PART quantities, which are not sheet repeats — and forbid
  deriving the count by dividing one by the other, and (d) require null +
  confidence "low" when the sheet states no count, explicitly rather than
  defaulting to 1. That last point matters downstream: `planned_runs` is a
  non-optional int on the wire (floored at 1 by `_coerce_planned_runs`), so
  `field_confidence["planned_runs"]` is the ONLY thing separating "reads 1" from
  "not found", and the import wizard now counts and labels the not-found rows.
  The verification prompt carries the same guidance on purpose — the merge
  policy hands a conflict to the VERIFIER's value, so a verifier that mistook a
  part quantity for a run count would overwrite a correct first read.

## 2026-07-20

- `laser_nest_segmentation` 1.0.0 — new prompt (pass 0 of the multi-page bare-PDF
  laser-nest upload). The whole multi-page PDF travels as a base64 `document`
  content block; the system prompt instructs page grouping ONLY (which pages form
  which nest, which to skip as cover/summary pages), with the safety rule that an
  uncertain continuation page becomes its own nest. Response is strict JSON
  `{nests: [{pages, cnc_number_hint}], skipped_pages, confidence}`; any failure
  (egress off, unconfigured, bad JSON, failed validation) degrades to one nest
  per page with `confidence "low"` — segmentation can never sink an upload.
  Task `laser_nest_segmentation` is unrouted; `has_pdf_document` lifts it to the
  DEFAULT (Sonnet) tier. Single-page PDFs skip the call entirely.
- `laser_nest_verification` 1.0.0 — new prompt (pass 2 of laser-nest extraction).
  An independent second read of the SAME nest PDF (same `document` block; the
  flattened text on the text-fallback path) plus pass 1's extracted JSON,
  explicitly instructed NOT to rubber-stamp pass 1 and to return null/"low" for
  anything it cannot itself pin. Per-field merge in code: agree → "high"; one
  null → non-null value, "medium"; conflict → verifier's value, "low"; both null
  → null, "low". Telemetry records the call under feature
  `laser_nest_verification` (context task stays `laser_nest_extraction`); a
  pass-2 failure keeps the pass-1 result untouched (`passes = 1`, warning noting
  verification was skipped).

## 2026-07-08

- `auto_execute_decision` 1.0.0 — new prompt for the always-on agent that selects
  which allowlisted Action Inbox recommendations to auto-execute. Sent as the
  `system` argument to `run_llm_task` (task `auto_execute`, Fast/Haiku by default).
  Variable recommendation batch travels in the user message; response is JSON
  `{execute, skip}`. Same Anthropic client as all other LLM features.

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
