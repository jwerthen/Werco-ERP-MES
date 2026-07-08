# Always-On AI (Werco ERP-MES)

This document describes the **always-on / self-improving** AI layer: domain
sensors that mint Action Inbox recommendations without a human prompt, and
automatic outcome capture that closes the learning loop.

For RFQ quoting operations see [AI_QUOTING_AGENT_RUNBOOK.md](./AI_QUOTING_AGENT_RUNBOOK.md).
For the interactive copilot see the Copilot section in [API.md](./API.md).

## Design posture

| Principle | Meaning |
|---|---|
| Suggest-first | Sensors create `AIRecommendation` rows only. Accept does **not** mutate controlled ERP records (v1). |
| Deterministic first | Phase-1 sensors use SQL/rules on live MES data — **no LLM calls**, so they work even when `allow_ai_egress` is off. |
| Tenant-scoped | Every query and write is filtered by `company_id`. |
| Best-effort signals | Outcome capture never fails a WO complete or quote update. |
| Auditable | Recommendations carry evidence, confidence, impact, and deep links. |

Governance tiers live in `AIGovernanceService` (`observe → draft → recommend → execute_controlled`).  
`execute_controlled` remains **disabled**.

## What runs without prompting

### Nightly job (05:30 UTC via ARQ)

`aggregate_ai_learning_job` → `AILearningService.aggregate_learning_signals()`:

1. Expire stale recommendations / wake snoozed ones  
2. Workflow friction (repeated AI edits/rejects)  
3. Correction patterns (repeated field fixes)  
4. Stale open blockers (>24h) — in the job wrapper  
5. **Domain sensors (Phase 1)**  
   - `at_risk_delivery` — open WOs due within 3 days or already late  
   - `inventory_risk` — on-hand ≤ reorder point / safety stock  
   - `quality_trend` — part scrap rate ≥ 5% over ≥ 3 completed jobs in 30 days  

Manual trigger (admin/manager/supervisor):

```http
POST /api/v1/ai/aggregate
```

Response includes `sensor_recommendations_created` in addition to the existing counts.

### Automatic outcome capture (Phase 0)

| Event | Outcomes recorded |
|---|---|
| Work order COMPLETE (`emit_work_order_completed_event`) | `on_time_delivery`, `scrap_rate`, optional `cost_variance` |
| Quote status → accepted / rejected / converted / expired | `quote_result` (`win` 1.0 / 0.0) |

These write `ai_outcomes` rows used later to score recommendations and calibrate learning. They do **not** change shop-floor or quality state.

## Surfaces

- **Action Inbox** (`/action-inbox`) — primary inbox for pending recommendations (AI + setup + notifications).  
- **API** — `GET /api/v1/ai/recommendations?target_entity_type=work_order&target_entity_id=…` for contextual strips (Phase 3).  
- Deep links in `suggested_action.href` point at work orders, inventory, or quality pages.

## Recommendation shape (sensors)

```json
{
  "recommendation_type": "at_risk_delivery",
  "source_module": "scheduling",
  "priority": "high",
  "confidence_score": 0.85,
  "suggested_action": {
    "type": "review_work_order_schedule",
    "href": "/work-orders/123",
    "autonomy": "suggest_only",
    "dedupe_key": "at_risk_delivery:wo:123"
  },
  "evidence": [{ "type": "due_date", "days_until_due": -2 }],
  "impact": { "expected": "Protect on-time delivery...", "magnitude": 1.5 }
}
```

Dedupe: open (pending/snoozed) recommendations for the same type + target are not recreated.

## Code map

| Concern | Path |
|---|---|
| Outcome capture | `backend/app/services/ai_outcome_capture_service.py` |
| Domain sensors | `backend/app/services/ai_sensors/` |
| Aggregation | `backend/app/services/ai_learning_service.py` → `aggregate_learning_signals` |
| Cron | `backend/app/jobs/ai_learning_jobs.py`, `backend/app/worker.py` |
| WO completion hook | `backend/app/services/completion_signal_service.py` |
| Quote hook | `backend/app/api/endpoints/quotes.py` |
| Inbox UI | `frontend/src/pages/ActionInbox.tsx` |

## Phase 2 — Apply-with-approval

`POST /api/v1/ai/recommendations/{id}/accept` body:

```json
{ "reason": "optional", "apply": true }
```

Or convenience: `POST /api/v1/ai/recommendations/{id}/apply`.

Response:

```json
{
  "recommendation": { "...status": "accepted" },
  "applied": true,
  "apply_result": { "action_type": "adjust_work_order_priority", "new_priority": 1 },
  "apply_error": null
}
```

**Allowlisted actions** (`AIActionApplier` / governance snapshot `apply_allowlist`):

| Action | Effect | Min role |
|---|---|---|
| `adjust_work_order_priority` | Sets WO priority | Supervisor+ |
| `escalate_blocker` | Ack blocker, severity high, WO priority ≤2 | Supervisor+ |
| `acknowledge_blocker` | Ack only | Supervisor+ |
| `create_draft_ncr` | Opens OPEN NCR (no disposition) | Quality / Supervisor+ |
| `create_draft_po` | Creates DRAFT PO + line | Supervisor+ |

Sensors now set `autonomy: "apply_on_accept"` on those types. UI shows **Accept & apply**.
Accept without apply remains available for non-applyable types. Apply failures do **not**
roll back the accept status.

Code: `backend/app/services/ai_action_applier.py`

## Phase 3 — Ambient AI

- **Contextual strip** on Work Order Detail and Part Detail (`ContextualAIStrip`) loads
  `GET /ai/recommendations?target_entity_type=…&target_entity_id=…`.
- **Morning brief** sensor mints one `morning_brief` recommendation per company per day +
  in-app notifications for Admin/Manager/Supervisor. Dashboard shows `MorningBriefBanner`.
- Action Inbox still the full queue.

## Phase 4 — Continuous learners

Nightly aggregation also runs learners (draft proposals only, never silent master-data writes):

| Learner | Type | Signal |
|---|---|---|
| Cycle time | `standard_update` | Actual/standard hours by work center outside 0.75–1.35 |
| Estimate calibration | `estimate_calibration` | Actual vs estimated job cost variance ≥20% |
| Correction preference | `learned_preference` | Same field corrected to same final value ≥3× |

Preferences feed `AIContextService` (Copilot / NL context) via `learned_preferences`.

Code: `backend/app/services/ai_learners/`

## Phase 5 — Claude auto-execute (shipped)

After sensors/learners mint recommendations, the same nightly job calls
``auto_execute_pending_recommendations``:

1. Collect pending rows with allowlisted `suggested_action.type` and
   `autonomy` in `{auto_execute, apply_on_accept, execute_controlled}`.
2. Call **existing** `run_llm_task(LLMTaskContext(task="auto_execute"), …)` with
   versioned `AUTO_EXECUTE_PROMPT` (Anthropic Claude only — no new AI vendors).
3. Claude returns JSON `{ execute: [{id, reason}], skip: [...] }`.
4. Each selected id is applied via `AIActionApplier` (system admin actor) and
   marked accepted with telemetry `ai_feature=auto_execute`.

**Fallback:** if `allow_ai_egress` is off or Anthropic is not configured, high-confidence
candidates (`>= AI_AUTO_EXECUTE_FALLBACK_MIN_CONFIDENCE`, default 0.75) still auto-execute
deterministically so the plant improves without a human prompt.

| Env | Default | Meaning |
|---|---|---|
| `AI_AUTO_EXECUTE_ENABLED` | `true` | Master switch |
| `AI_AUTO_EXECUTE_MIN_CONFIDENCE` | `0.55` | Min conf to send to Claude |
| `AI_AUTO_EXECUTE_FALLBACK_MIN_CONFIDENCE` | `0.75` | Min conf when Claude unavailable |
| `AI_AUTO_EXECUTE_MAX_BATCH` | `25` | Cap per company per run |
| `ANTHROPIC_AUTO_EXECUTE_MODEL` | (router Haiku) | Optional model override |

Never auto-executes: `morning_brief`, `workflow_friction`, `correction_pattern`,
`learned_preference`, `standard_update`, `estimate_calibration` (stay human/review).

Code: `backend/app/services/ai_auto_execute_service.py`,
`backend/app/services/prompts/auto_execute.py`.

## Roadmap status

| Phase | Status | Focus |
|---|---|---|
| 0 Instrumentation | **Shipped** | Auto outcomes on WO complete + quote terminal status |
| 1 Domain sensors | **Shipped** | Late WO, inventory risk, scrap trend → Action Inbox |
| 2 Apply-with-approval | **Shipped** | Typed `AIActionApplier` for safe draft actions |
| 3 Ambient AI | **Shipped** | Contextual strips, morning brief |
| 4 Continuous learners | **Shipped** (+ existing routing learning) | Cycle time / quote calibration / preferences |
| 5 Auto-execute | **Shipped** | Claude agent executes allowlisted actions without prompt |

## Compliance notes

- Sensors do not call Anthropic; no CUI egress.  
- Accept remains suggest-only for controlled records.  
- Learning payloads are redacted via `redact_learning_payload`.  
- Multi-tenant isolation is enforced on every sensor query.

## Verification

```bash
cd backend
pytest tests/services/test_ai_sensors.py -q
pytest tests/api/test_ai_learning.py -q
```

Plant smoke: seed a late WO and a low-stock part → `POST /api/v1/ai/aggregate` (or wait for 05:30 cron) → open Action Inbox.
