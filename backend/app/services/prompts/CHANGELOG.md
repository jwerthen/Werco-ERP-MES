# Prompt Registry Changelog

## 2026-11-09 — prompt-drift guard and lock (#9-5a)

- `prompt_lock.json` (new, next to this file) holds an append-only history of
  `{version, sha256, variants}` per prompt id, plus a history for the router's
  default models. `tests/test_prompt_drift_guard.py` renders every prompt's real
  request builder once per branch that adds text the model sees (OCR notes, routing
  branches, laser hint lines, the copilot forced-final call and message wrappers)
  and fails when the request changes without a version bump, a line here and an
  APPENDED lock entry. CI compares the lock with the base branch, so an existing
  entry can never be edited. No prompt changed version: the first snapshot is the
  baseline for every current version.
- `models` 1.0.0 — baseline of the router's default model ids:
  `llm_model_router.DEFAULT_ANTHROPIC_MODELS` (fast `claude-haiku-4-5-20251001`,
  default `claude-sonnet-4-6`, reasoning `claude-opus-4-8`) and
  `DEFAULT_COPILOT_MODEL` (`claude-opus-5-5`). Changing a default now needs a
  `models` line here (`- models 1.0.0 → 1.1.0 — ...`) and an appended lock entry.
- Backfilled below, because they shipped without entries: `copilot_chat`
  1.5.0 → 1.6.0 → 1.7.0 and `hank_document_intake` 1.0.0 → 1.1.0 → 1.2.0 (#288,
  #289, 2026-09-23), and the `estimate_drawing_extraction` retirement (2026-09-16).

## 2026-11-09 — qms_clause_extraction text moved out of the router (#9-0)

- `qms_clause_extraction` 1.0.0 — **no version change**, because neither the text
  nor the request layout changed. The user message moved verbatim from the inline
  f-string in `app/api/endpoints/qms_standards.py` to `QMS_CLAUSE_EXTRACTION_TEMPLATE`
  + `QMS_CLAUSE_SCHEMA` in `prompts/qms.py`; the model call moved to
  `app/services/qms_extraction_service.py`. The rendered prompt and the request
  (one user message, no `system`, `max_tokens` 16000, routing on the full input
  length) are byte-identical, pinned by `tests/services/test_qms_extraction_service.py`
  and `tests/api/test_qms_upload_request_parity.py`. Supersedes the 2026-06-09
  "remains inline" note. Edit the template from now on; `text=None` stays because
  it is a per-request user message, not a cacheable system prompt.

## 2026-11-05 — auto_execute_decision retired (owner ruling 3)

- `auto_execute_decision` **RETIRED**. 1.0.0 was the last version. Owner ruling 3
  (2026-11-05): nothing AI-driven executes without a named person clicking Accept,
  so the always-on agent that picked recommendations to auto-execute is removed
  with its service, its `auto_execute` model-router task and the
  `ANTHROPIC_AUTO_EXECUTE_MODEL` override. The id stays **reserved** and must never
  be reused: historical `ai_usage_events` (feature `ai_auto_execute`) and
  `ai_interaction_events` (`ai_feature = auto_execute`) rows reference it.

## 2026-09-23 — Hank document evidence and Office intake (backfilled 2026-11-09, #9-5a)

These four versions shipped without entries; they are recorded here after the fact.

- `copilot_chat` 1.5.0 → 1.6.0 (#288) — the panel's Upload PDFs action (Admin,
  Manager, Quality) now saves and analyzes PDFs, and analyzed evidence can be
  attached to chat. New guidance plus two tool schemas in the cached prefix:
  `hank_document_evidence` (read saved fields and lines with page citations, a
  small page at a time) and `hank_receiving_document` (match delivery evidence to
  open PO lines without posting anything). A `receive_delivery` proposal must carry
  `source_intake_file_id` and `source_intake_version`, and ordered or backordered
  quantities are never delivered quantities. Attached PDF summaries, extracted
  fields and page excerpts join the untrusted-context list. Attached files reach the
  model as an `<attached_pdf_evidence>` manifest block in the uncached user turn.
- `hank_document_intake` 1.0.0 → 1.1.0 (#288) — for delivery lists and packing
  slips, quantity means the quantity shipped or delivered on this document, not
  ordered, previously received or backordered (null plus an explanation when the
  columns can't be told apart). Keep the printed unit of measure, keep each lot or
  heat line separate with its own evidence, and never add line quantities.
- `copilot_chat` 1.6.0 → 1.7.0 (#289) — Upload documents accepts PDF, DOCX, XLSX and
  XLS, and evidence cites paragraphs or sheet/cell locations. New guidance plus the
  `hank_purchase_order_document` tool schema (match an uploaded supplier PO's vendor
  and every line to current records); `draft_purchase_order` may create an
  explicitly reviewed, source-backed PO ready for Receiving only when the employee
  asks. Still no supplier email, receipt or inventory change. The manifest block
  was renamed `<attached_document_evidence>`.
- `hank_document_intake` 1.1.0 → 1.2.0 (#289) — classifies Word and Excel documents
  as well as PDFs: cite Office section numbers and exact paragraph/table-row/
  sheet-row locators, treat formula results marked UNTRUSTED as unconfirmed, extract
  every relevant line up to 50 with `has_more_lines`, and read purchase-order fields
  as printed (ordered quantity, per-unit price, requested delivery date, supplier
  versus customer). Request layout: Office documents travel as numbered text
  sections in the user message instead of a PDF `document` block.

## 2026-09-22 — Hank operational workflows

- `copilot_chat` **1.5.0**: six reviewed action schemas, live operational/evidence reports, own active-job and receiving context, saved-work status/extraction tools, and truthful handoff/routine/voice guidance. Cached tool schemas remain deterministic.
- `hank_document_intake` **1.0.0**: bounded PDF classification and structured fields with source pages, uncertainty and record-matching hints; source content never supplies instructions or manufacturing approval.

Bump a prompt's semver `version` and add an entry here whenever its text or
request layout changes, then APPEND the new `{version, sha256, variants}` entry to
`prompt_lock.json` (the drift guard's failure message prints it). Never edit an
existing lock entry; CI checks the lock against the base branch. The version
string is recorded on `AIUsageEvent` (every API call) and on
`AIInteractionEvent`/`AIRecommendation` learning rows.

## 2026-09-22

- `copilot_chat` 1.3.0 → 1.4.0 — apply explicitly saved, typed personal
  preferences for briefing detail, work-area focus and handoff format from
  the uncached context suffix. Current employee requests take precedence.
  Preferences cannot change authority or production policy, and checklist
  formatting cannot imply completion. Explain the Preferences editor and
  alert muting while retaining task receipts. Stable prompt/tool ids unchanged.

- `copilot_chat` 1.2.0 → 1.3.0 — explain the explicit New follow-up form,
  periodic/manual checks and snooze/stop controls. Chat cannot start a watch.
  Distinguish a cleared blocker list from production readiness and a new PDF
  attachment from content verification or approval. No new chat tool or model route.

- `copilot_chat` 1.1.0 → 1.2.0 — add permission-scoped shift briefing and
  explicit-request-only task preparation. Hank can save a preview for a draft
  repeat job, draft PO, or existing PDF attachment; the employee executes it
  through the typed Tasks workflow. Require resolved identifiers and supplied
  quantities/prices; distinguish saved previews from business completion.
  Extend the deterministic tool prefix without changing routing/telemetry ids.

- `copilot_chat` 1.0.0 → 1.1.0 — rename the assistant to Hank, inspired by the
  shop's yellow Labrador, with practical shift briefings, handoffs, and next-step
  guidance. Add metadata-only document lookup guidance and distinguish the
  panel's user-submitted Upload PDF release workflow from read-only chat tools.
  Explicitly prohibit invented completion receipts, PDF-content claims,
  background monitoring promises, and instructions embedded in retrieved data.
  Prompt id, model routing, telemetry keys, and cached-prefix layout stay stable;
  append the deterministic `search_documents` tool schema to the cached prefix.

## 2026-09-16 — estimate_drawing_extraction retired (backfilled 2026-11-09, #9-5a)

- `estimate_drawing_extraction` **RETIRED**. 1.0.0 was the last version. The legacy
  Estimate Workbench's drawing-PDF extraction (fab and buyout lines over three
  varied passes) was removed with the workbench when legacy quoting was replaced by
  reviewed fabrication estimates (3e17456e). The id stays **reserved** and must
  never be reused: historical `ai_usage_events` rows carry task and feature
  `estimate_drawing_extraction`.

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


## 2026-11-02: Drawing Engineering UI handoff

- copilot_chat 1.7.1 routes drawing-to-engineering requests to the signed-in local-PDF review workspace. No engineering write tools or model changes.
- hank_document_intake 1.2.1 clarifies the separation between filing metadata and reviewed engineering entry.
