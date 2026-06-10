# Prompt Registry Changelog

Bump a prompt's semver `version` and add an entry here whenever its text or
request layout changes. The version string is recorded on `AIUsageEvent`
(every API call) and on `AIInteractionEvent`/`AIRecommendation` learning rows.

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
