# AI Quoting Agent (Sheet Metal) Runbook

## Scope
This feature supports sheet metal quoting for:
- Laser cutting
- Bending
- Welding/assembly (rule-based)
- Deburr
- Powder coat / paint style finishing
- Basic hardware insertion

## Supported Input Files
- `.pdf` drawing packages
- `.xlsx` / `.xls` BOM files
- `.dxf` flat patterns
- `.step` / `.stp` CAD files (fallback parsing)

## User Workflow
1. Open `Sales & Shipping -> AI RFQ Quote`.
2. Upload RFQ files as one package.
3. Click `Generate AI Estimate`.
4. Review:
- Part summaries
- Total cost breakdown
- Lead time range
- Assumptions
- Missing / Needs Review list
5. Click `Approve & Create Quote`.
6. Export:
- `Export Internal Estimate` for internal review record
- `Generate Customer Quote PDF` for customer-facing quote

## API Endpoints
- `POST /api/v1/rfq-packages/`
- `POST /api/v1/rfq-packages/{id}/generate-estimate`
- `GET /api/v1/quotes/{id}` (includes AI estimate block when available)
- `POST /api/v1/quotes/{id}/generate-pdf`

Additional operational endpoints:
- `POST /api/v1/rfq-packages/{id}/approve-create-quote`
- `GET /api/v1/rfq-packages/{id}/internal-estimate-export`

## Pricing Providers and Configuration
Pricing is pluggable through `MaterialPriceService` in `backend/app/services/rfq_pricing_service.py`.

Default providers:
- `InternalPriceList` (ERP/quote config tables)
- `WebLookup` placeholder (disabled unless controlled implementation exists)

Caching:
- Snapshot cache in `price_snapshots`
- Cache freshness from quote setting `rfq_price_cache_hours` (default `12`)
- If lookup fails: uses last-known snapshot and flags fallback

## Cost Model Configuration
Primary settings are read from `quote_settings`. If missing, defaults are applied.

Key settings:
- `rfq_scrap_factor`
- `rfq_laser_rate_per_hour`
- `rfq_brake_rate_per_hour`
- `rfq_welding_rate_per_hour`
- `rfq_assembly_rate_per_hour`
- `rfq_shop_overhead_pct`
- `rfq_sec_per_bend`
- `rfq_bend_setup_minutes`
- `rfq_laser_setup_minutes`
- `rfq_weld_minutes_per_part`
- `rfq_assembly_minutes_per_part`
- `rfq_finish_rate_per_sqft`
- `rfq_base_queue_days`
- `rfq_daily_capacity_hours`
- `rfq_outside_service_buffer_days`
- `rfq_consumables_factor_pct`

Margin:
- Request-level `target_margin_pct` in estimate generation payload
- Falls back to `default_markup_pct` if not provided

## Confidence, Assumptions, and Missing Specs
The parser never silently invents required fields.

Rules:
- If strong evidence exists, a value may be inferred and logged to `assumptions` with confidence.
- Missing critical fields are listed in `missing_specs`.
- STEP fallback geometry is explicitly marked low confidence.

Confidence fields:
- `material`
- `thickness`
- `geometry`
- `finish`

Overall confidence is reduced by missing-spec penalties.

## STEP Fallback Behavior
If STEP cannot be fully parsed:
- Attempts bounding-box geometry extraction from point data.
- Marks geometry as low confidence.
- Adds explicit warning to assumptions.
- Recommended operator action: provide flat pattern DXF for release-quality quote.

## Customer PDF Rules
Customer PDF includes:
- Customer and RFQ reference
- Part summary table (part/qty/material/thickness/finish)
- Total quote and lead time
- Assumptions and exclusions

Customer PDF excludes:
- Operation-level time line items

## Operational Notes
- Parsing is resilient per file. One bad file should not abort all parsing.
- Excel BOM scanning is bounded with the Import Center's shared caps (`backend/app/services/import_service.py`): at most 256 columns are read per row, a run of more than 1,000 consecutive blank rows ends that sheet's scan (used-range bloat; later sheets still parse), and a workbook is refused — recorded as that file's `error` state with an actionable message — past 100,000 scanned rows or 10,000 collected rows.
- File parse state is tracked per RFQ package file (`pending`, `parsed`, `parsed_with_fallback`, `error`).
- Logs include parsing failures and fallback conditions.

## Platform LLM Operations (shared across AI features)

These notes cover the shared LLM plumbing used by **every** Anthropic call in the platform —
PO/quote document extraction (`po-upload`), BOM import, AI routing generation, QMS clause
extraction, laser-nest report PDF extraction (`POST /laser-nests/extract` + the PDF
laser-nest-package preview/import), Werco Copilot chat (`POST /copilot/chat`), and the
natural-language search intent parse (`POST /search/nl`). (The RFQ package parsing documented above
is deterministic and makes no LLM calls.)

### Shared client and model routing
- Every Anthropic call flows through `backend/app/services/llm_client.py` (`run_llm_task`).
  `run_llm_task` is also the **single enforcement point for the per-company AI egress kill switch**
  (`Company.allow_ai_egress`) — see "Per-company AI egress kill switch" below; a call for a company
  with egress OFF never leaves the boundary.
  `run_llm_task` also carries the platform's tool-use support (`tools` / `tool_choice`,
  forwarded verbatim) — the copilot's multi-round tool loop calls it once per iteration, and
  each iteration records its own usage row.
- Model selection stays in the existing router (`app/services/llm_model_router.py`):
  `ANTHROPIC_MODEL_SELECTION` plus per-tier / per-task model override env vars.

| Task | Tier in `auto` mode | Per-task override env var |
|------|---------------------|----------------------------|
| `po_extraction` | Fast / Default / Reasoning by document complexity | `ANTHROPIC_PO_MODEL` |
| `bom_extraction` | Fast / Default / Reasoning by document complexity | `ANTHROPIC_BOM_MODEL` |
| `routing_generation` | Default; Reasoning for complex parts | `ANTHROPIC_ROUTING_MODEL` |
| `qms_clause_extraction` | Default; Reasoning for large documents | `ANTHROPIC_QMS_MODEL` |
| `copilot_chat` | Default (Sonnet); escalates to Reasoning for long multi-tool conversations | `ANTHROPIC_COPILOT_MODEL` |
| `nl_search` | Pinned Fast (Haiku) — cheap intent classification | `ANTHROPIC_NL_SEARCH_MODEL` |
| `laser_nest_extraction` | Default (Sonnet) — the native-PDF path sets `has_pdf_document`, lifting it off the Fast tier; the text fallback escalates for OCR'd/large sheets. The verification pass (`feature="laser_nest_verification"`) runs under this same task, so the override covers both passes | `ANTHROPIC_LASER_NEST_MODEL` |
| `laser_nest_segmentation` | Default (Sonnet) — unrouted; the whole-PDF `document` block sets `has_pdf_document` | — (none; tier-level overrides only) |
- **Laser-nest extraction now sends the PDF natively (vision).** As of prompt
  `laser_nest_extraction` 1.1.0, the primary path hands the rendered nest report to the model as a
  base64 `document` content block (layout-aware vision) instead of flattened extracted text — this
  fixes the glued-digits and material-grade-on-the-wrong-line errors that came from a 1-D text
  flatten. Because a native-PDF call sets `has_pdf_document` on `LLMTaskContext`, it routes to the
  **Default (Sonnet) tier** rather than Fast (Haiku), so **per-call token cost is higher than the
  old text-only path** (the rendered-page tokens are billed as input; tracked per call in
  `ai_usage_events`). PDFs over the **~20 MB** native cap (or whose bytes can't be read) fall back
  to the flattened-text path, which routes by complexity as before. `_extraction_metadata.input_mode`
  records which path produced each result (`native_pdf` | `text`).
- **Laser-nest extraction is two-pass, and bare multi-page PDFs get a segmentation pass
  (2026-07-20).** Everywhere nest PDFs are extracted (single-PDF `POST /laser-nests/extract` and
  the laser-nest-package preview/import), a successful extraction is followed by an **independent
  verification read** (prompt `laser_nest_verification` 1.0.0, `feature="laser_nest_verification"`,
  same `laser_nest_extraction` routing task) merged per field — agreement = "high", one-sided null
  = "medium", conflict = the verifier's value at "low"; a pass-2 failure keeps pass 1 with a
  warning (`passes` 1|2). Bare multi-page PDF uploads additionally run a page-grouping pass first
  (prompt `laser_nest_segmentation` 1.0.0, `feature="laser_nest_segmentation"`, the whole PDF as a
  `document` block; skipped for single-page PDFs), degrading to one-nest-per-page on any failure.
  Cost note: a verified nest sheet is **two** Sonnet-tier native-PDF calls instead of one, plus one
  segmentation call per multi-page upload — each visible per feature string in `ai_usage_events`;
  the bare-PDF import path re-splits by confirmed pages with **zero** AI calls.
- Routing generation sends its stable prefix (system prompt + schema/allowed work-center types +
  learned-examples context) as `cache_control: ephemeral` system blocks, so repeat generations
  hit the prompt cache and only the drawing content is reprocessed at full input price.
  Data-handling note (CMMC traceability): with caching enabled, that prefix — including the
  company's learned-routing examples — is retained server-side in Anthropic's prompt cache for
  the ephemeral TTL (~5 minutes) instead of being transient per-request. Same provider, same
  trust boundary and ToS; recorded here as a data-flow change. Caching only engages above
  Anthropic's minimum cacheable prefix length; verify via `cache_creation_tokens`/`cache_read_tokens`
  on `routing_generation` rows in `ai_usage_events`.
- **Werco Copilot also uses `cache_control: ephemeral`** on its stable prefix: the deterministic
  tool schemas (which render before `system`) plus the versioned `copilot_chat` system prompt are
  cached together by a single breakpoint on the system block, so every iteration of the per-turn
  tool-use loop re-reads the cached prefix instead of re-paying full input price. The `nl_search`
  intent parse likewise caches only its static system prompt.
  Data-handling note (CMMC, extends the note above): for the copilot, the **cached-prefix
  retention applies to the system prompt + tool schemas only** — static text containing no tenant
  data. **Conversation content is never cached.** Tool RESULTS — which do contain tenant
  operational data (work orders, blockers, schedules, inventory, customer orders) — flow to
  Anthropic **per-request** as ordinary uncached message content, the same per-request trust
  boundary as the other AI features. The copilot's `search_erp` tool excludes the employee
  directory (`user`-type search results) entirely, so employee names/emails are never part of
  that data flow. Verify the cache engages via `cache_read_tokens` on
  `copilot_chat` rows in `ai_usage_events`.

### Per-company AI egress kill switch (`allow_ai_egress`, CUI control)
- `Company.allow_ai_egress` (`companies.allow_ai_egress`, default **OFF**) is a per-company kill
  switch that gates **all** outbound AI document-extraction egress to Anthropic — the AI analogue of
  the carrier (`allow_carrier_egress`) and print (`allow_print_egress`) controls. It is enforced at
  a **single fail-closed point** in `run_llm_task`: before any Anthropic call, `_ai_egress_allowed`
  resolves the flag for the call's `company_id` and a disabled company raises
  `LLMEgressDisabledError`. Because it lives in the shared client, **every** AI feature listed above
  is covered by this one seam — PO/quote, BOM, routing generation, QMS clause, laser-nest PDF
  extraction, Werco Copilot, and NL search.
- **When egress is OFF:** no request leaves the boundary and **no `ai_usage_events` row is written**
  (the call never reaches telemetry). Callers catch `LLMEgressDisabledError` and **degrade
  gracefully** rather than 500 — e.g. laser-nest extraction falls back to **filename-only** parsing
  for per-file packages (bare-PDF segments instead leave `cnc_number` blank — their split names are
  synthetic; bare-PDF segmentation defaults to one-nest-per-page; the local page split keeps working);
  PO/BOM/QMS/routing endpoints return a "disabled for your company" message; the copilot and NL
  search degrade. The check **fails closed**: an unknown tenant or any DB error denies egress.
- **Flipping the flag:** only via `PUT /api/v1/companies/me/ai-egress` (gated **ADMIN-only**,
  matching the sibling `allow_carrier_egress` / `allow_print_egress` controls — a CUI-boundary
  decision reserved to Admins), which records the change on the tamper-evident `audit_log` as both a
  field update and an `ai_egress_enabled` / `ai_egress_disabled` status change. The toggle is exposed
  in the UI at **Admin Settings → AI Privacy** (`/admin/settings?tab=aiprivacy`); the control is
  interactive for ADMIN (turning egress **on** requires an explicit confirmation; turning **off** is
  immediate) and read-only for other roles. New companies default **OFF**; existing companies were
  grandfathered **ON**.

### AI usage telemetry (cost / latency ledger)
Each call writes one tenant-scoped row to `ai_usage_events`: task, exact model id, tier, feature,
prompt version, input/output/cache-write/cache-read token counts, estimated USD cost (from the
price table `MODEL_PRICING_USD_PER_MTOK` in `llm_client.py`; `NULL` for unpriced models), latency,
and success/error type.
- This is **operational telemetry, not audit data** — rows are not on the tamper-evident
  `audit_log` hash chain, and they are written fire-and-forget on a dedicated short-lived session
  (a telemetry failure logs a warning and never breaks the AI call).
- Read it via `GET /api/v1/ai-usage/summary?days=N` (Admin/Manager) or the
  `Admin Settings -> AI Usage & Cost` tab.
- When Anthropic pricing changes or a new model is pinned, edit `MODEL_PRICING_USD_PER_MTOK`.

### Versioned prompt registry
- Prompt text lives in `backend/app/services/prompts/` (PO/quote extraction, BOM extraction,
  routing generation, `laser_nest_extraction` 1.1.0 — laser-nest report PDF field extraction —
  with `laser_nest_verification` 1.0.0 (its independent second read) and
  `laser_nest_segmentation` 1.0.0 (multi-page bare-PDF page grouping),
  `copilot_chat` 1.0.0 — the Werco Copilot system prompt — and
  `nl_search_intent` 1.0.0 — the `/search/nl` fast-tier intent parser; QMS clause extraction is
  version-registered with its text still at the call site).
- Bump the prompt's semver `version` and add an entry to `CHANGELOG.md` in that package whenever
  prompt text or request layout changes — the version is recorded on every usage row and on
  AI-learning rows, so regressions can be attributed to a prompt revision.

### Eval harness
- `backend/tests/evals/` holds golden-fixture evals for the PO and BOM extraction pipelines,
  excluded from the default pytest run via the `evals` marker.
- Offline (default — no API key, no network): `pytest -m evals tests/evals`
- Live (opt-in, billable): `RUN_LIVE_EVALS=1 ANTHROPIC_API_KEY=... pytest -m evals tests/evals`
- See `backend/tests/evals/README.md` for the fixture layout and how to add cases.
