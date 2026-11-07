# Always-On AI (Werco ERP-MES)

This document describes the **always-on / self-improving** AI layer: domain
sensors that mint Action Inbox recommendations without a human prompt, and
automatic outcome capture that closes the learning loop.

For RFQ quoting operations see [AI_QUOTING_AGENT_RUNBOOK.md](./AI_QUOTING_AGENT_RUNBOOK.md).
For the interactive assistant see [Hank](HANK.md) and [API → Hank](API.md#hank-ai-shop-teammate).
Hank's role-aware briefing is deterministic and on demand. Its reviewed task
workflows use a separate `hank_tasks` store: chat may prepare an audited proposal,
but only an explicit employee command executes it. These actions do not go through
the Action Inbox **Accept & apply** door described below. Proposal/receipt audit
evidence is required even though learning and sensor signals remain best effort.
Hank also offers explicitly saved personal work-order follow-ups. These use deterministic
five-minute or manual checks and finish once blockers clear or a new matching PDF is
attached, with a receipt and owner-only in-app notification. They do not execute ambient
recommendations, approve evidence, or use an LLM. Background checks require the separate
[Hank-only cron rollout](DEPLOYMENT.md#hank-follow-up-worker).
Hank's explicit personal preferences affect briefing presentation, chat handoff style and
private follow-up alerts. They are tenant/user scoped with audited save/reset, not learned
company policy. Muting alerts preserves completed receipts and required evidence; it does
not disable ambient sensors or change who may apply their recommendations.

Smart PDF intake is another explicit, request-driven workflow: a saved upload
queues bounded extraction through `hank_document_intake` prompt 1.0.0, the shared
model router, company AI-egress gate and usage telemetry. Employees review source
pages and a filing plan before a separate command creates the document/receipt
linkage. The worker never approves or files extracted evidence automatically.
Readiness, knowledge/genealogy, purchasing impact and shipping packets use
deterministic source reads; receiving, production and shipment preparation use
reviewed canonical commands. Explicit participant handoffs and approved routine
runs retain their own audit/version controls and do not activate ambient
automation. The combined work queue reports their actual saved states.

## Design posture

| Principle | Meaning |
|---|---|
| Suggest-first | Sensors create `AIRecommendation` rows only. **Nothing AI-driven executes on its own** (owner ruling 3, 2026-11-05): an allowlisted action runs only when a named person clicks **Accept & apply** (Phase 2). Plain Accept records a decision and changes nothing else. |
| Deterministic first | Phase-1 sensors use SQL/rules on live MES data — **no LLM calls**, so they work even when `allow_ai_egress` is off. |
| Tenant-scoped | Every query and write is filtered by `company_id`. |
| Best-effort signals | Outcome capture never fails a WO complete or quote update. |
| Auditable | Recommendations carry evidence, confidence, impact, and a target record. Applied actions are audit-logged as the person who clicked. |

Governance tiers live in `AIGovernanceService` (`observe → draft → recommend → apply_on_accept`).
There is **no auto tier**. `apply_on_accept` means a person's Accept & apply click, and it is
enabled only while the company's AI egress is on (`GET /api/v1/ai/governance` reports
`mode: "apply_on_accept"` and `ai_egress_enabled`). With egress off, nothing is applied.

## What runs without prompting

### Nightly job (05:30 UTC via ARQ)

`aggregate_ai_learning_job` → `AILearningService.aggregate_learning_signals()`:

1. Expire stale recommendations / wake snoozed ones  
2. Retire cards whose target closed — a blocker that is resolved, dismissed or gone, or a work
   order that is terminal or deleted — to `stale` with `status_reason` `Target closed: …`  
3. Workflow friction (repeated AI edits/rejects)  
4. Correction patterns (repeated field fixes)  
5. Stale open blockers (>24h) — in the job wrapper. **At most one escalation card per
   blocker**: a pending, snoozed, accepted or dismissed one suppresses another. Skipped for a
   work order that is terminal or deleted, and when escalating would change nothing (already
   acknowledged at high/critical with the job at P2 or better). The critical
   `work_order_blocker_escalated` event fires only when a card is minted.  
6. **Domain sensors (Phase 1)**  
   - `at_risk_delivery` — open WOs due within 3 days or already late  
   - `inventory_risk` — on-hand ≤ reorder point / safety stock  
   - `quality_trend` — part scrap rate ≥ 5% over ≥ 3 completed jobs in 30 days  

The job **never applies a recommendation**, makes no LLM call and mutates no ERP record. Every
card it mints waits for a person. (Until 2026-11-05 it also auto-executed allowlisted actions;
see [Phase 5](#phase-5--claude-auto-execute-retired-2026-11-05).)

Manual trigger (admin/manager/supervisor):

```http
POST /api/v1/ai/aggregate
```

Response includes `sensor_recommendations_created`, `learner_recommendations_created` and
`closed_target_recommendations_staled` in addition to the existing counts. It has no
`auto_executed` / `auto_execute_failed` keys.

### Automatic outcome capture (Phase 0)

| Event | Outcomes recorded |
|---|---|
| Work order COMPLETE (`emit_work_order_completed_event`) | `on_time_delivery`, `scrap_rate`, optional `cost_variance` |
| Quote status → accepted / rejected / converted / expired | `quote_result` (`win` 1.0 / 0.0) |

These write `ai_outcomes` rows used later to score recommendations and calibrate learning. They do **not** change shop-floor or quality state.

## Surfaces

- **Action Inbox** (`/action-inbox`) — primary inbox for pending recommendations (AI + setup + notifications).  
- **API** — `GET /api/v1/ai/recommendations?target_entity_type=work_order&target_entity_id=…` for contextual strips (Phase 3).  
- **Open record** — every listed row carries a server-computed `target_href`
  (`/work-orders/{id}` for a work-order or blocker target, `/parts/{id}` for a part, `null`
  otherwise). The UI links to it, never to the stored `suggested_action.href`, which is
  caller-supplied on `POST /ai/recommendations` and can point at a page that ignores its query.

## Recommendation shape (sensors)

The at-risk-delivery sensor, for a job two days late at P5:

```json
{
  "recommendation_type": "at_risk_delivery",
  "source_module": "scheduling",
  "priority": "high",
  "confidence_score": 0.79,
  "target_entity_type": "work_order",
  "target_entity_id": 123,
  "suggested_action": {
    "type": "adjust_work_order_priority",
    "work_order_id": 123,
    "work_order_number": "WO-20261103-002",
    "priority": 1,
    "href": "/work-orders/123",
    "autonomy": "apply_on_accept",
    "dedupe_key": "at_risk_delivery:wo:123"
  },
  "evidence": [{ "type": "due_date", "due_date": "2026-11-05", "days_until_due": -2,
                 "status": "in_progress", "priority": 5,
                 "quantity_ordered": 10.0, "quantity_complete": 4.0 }],
  "impact": { "expected": "Protect on-time delivery and reduce expedite cost.", "magnitude": 1.5 }
}
```

- `autonomy` is `apply_on_accept` only when applying would actually **raise** the priority (the
  target is P1 when late, P2 when at risk). A job already at or above the target gets a
  `suggest_only` card: informational, with no Accept & apply.
- The inventory sensor's `create_draft_po` action names **no vendor**. Accept & apply resolves
  the part's current primary supplier at click time; the supplier at mint time is kept in
  evidence as `primary_supplier_id_at_mint`.
- Only `suggest_only` and `apply_on_accept` can be written: the model refuses any other
  `autonomy` on write, and `POST /ai/recommendations` answers 422. Rows written before
  2026-11-05 with `auto_execute` / `execute_controlled` are **not rewritten** (that would rewrite
  the evidence of what the old agent did); every decision point reads them as `apply_on_accept`.
- `suggested_action.work_order_number` and similar labels are display hints only. What runs is
  resolved from the ids against live records (Phase 2).

Dedupe: open (pending/snoozed) recommendations for the same type + target are not recreated.
After an accept or dismiss, a sensor can mint the same condition again the next night while it
persists (for example, low stock after a draft PO was applied: a draft PO does not change on-hand).
The card's open-PO warning discloses the earlier PO.

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
| Apply door, plan resolver, policy | `backend/app/api/endpoints/ai_learning.py`, `backend/app/services/ai_action_applier.py` |

## Phase 2 — Accept & apply (a person's click)

**Owner ruling 3 (2026-11-05): nothing AI-driven executes without a named person clicking
Accept.** Two verbs, deliberately separate:

| Verb | Body | What it does |
|---|---|---|
| `POST /api/v1/ai/recommendations/{id}/accept` | `{ "reason"? }` | **Records** the decision. Never runs the action. `"apply": true` is refused with **422**. |
| `POST /api/v1/ai/recommendations/{id}/apply` | `{ "reason"?, "expected_fingerprint" }` | **Accept & apply** — the only executor of an AI recommendation, and not an MCP tool. |

Nothing else applies a recommendation: not the nightly job, not `POST /ai/aggregate`, not an
agent. `tests/test_ai_apply_single_door.py` holds that structurally (the applier is constructed
only by the `/apply` route, and it has no role bypass).

**What the person sees is what runs.** For each pending **action card** (an allowlisted type
with `apply_on_accept` autonomy), `GET /ai/recommendations` attaches `apply_action`:

```json
{
  "action_type": "create_draft_po",
  "summary": "Create a DRAFT purchase order to Acme Steel (VND-014) for 50 x 1018-CR-0250 at $12.40 each. Nothing is sent to the vendor.",
  "warnings": ["1018-CR-0250 already has open PO(s): PO-20261104-002"],
  "fingerprint": "3f9a0c1d2e4b5a6c",
  "can_apply": true,
  "can_mark_handled": true,
  "blocked_reason": null,
  "blocked_kind": null
}
```

One read-only resolver (`resolve_plan`) produces both this summary and what the applier runs.
It resolves every id in the stored action against live, tenant-scoped records and never trusts
a stored label. The client sends the `fingerprint` back as `expected_fingerprint`; if the
resolved plan changed in between, `/apply` answers **409** ("This action changed since you
loaded it. It would now: …") and nothing runs. `blocked_kind` is `credential`, `read_only`,
`egress`, `role` or `target` (the plan itself is refused), or `null` when the caller can apply.

**Gates on `/apply`, in order** (exact messages in [API.md → AI Recommendations](API.md#ai-recommendations-action-inbox)):

1. The card is pending — else **409**.
2. It has an action to apply (allowlisted type, `apply_on_accept`) — else **409**.
3. A person signed in to the app in their own session — an API token or a station/badge
   token gets **403**.
4. Not a read-only company context — else **403**.
5. Company AI egress is on — else **403**. Read literally: with egress off, nothing is applied,
   even by a person.
6. The person's role can perform the action (table below) — else **403**.
7. The target still makes sense — else **409** with the plan's reason.
8. The fingerprint still matches — else **409**.

The route claims the card (a compare-and-set from `pending`) **before** any ERP write, so a
losing double-click never reaches a handler, then runs the action **as the clicking person** in
the same transaction. Any refusal or error rolls everything back, the claim included: the card
stays pending. A concurrent work-order edit surfaces as the app-wide **409**.

Response:

```json
{
  "recommendation": { "...": "...", "status": "accepted", "accepted_by": 7 },
  "applied": true,
  "apply_result": {
    "action_type": "adjust_work_order_priority",
    "summary": "Raise WO-20261103-002 priority from P5 to P1",
    "work_order_id": 123, "work_order_number": "WO-20261103-002",
    "old_priority": 5, "new_priority": 1
  },
  "apply_error": null
}
```

`apply_error` is always `null` (kept for compatibility). There is no "accepted, but the apply
failed" outcome any more: a failed apply is a 403/409 and the card stays open.

**Allowlisted actions** (`AIActionApplier` / governance snapshot `apply_allowlist`):

| Action | Effect | Min role |
|---|---|---|
| `adjust_work_order_priority` | Raises the WO to the suggested priority. Never demotes: refused when the WO is already at that priority or higher (P1 is highest), terminal, or deleted | Supervisor+ |
| `escalate_blocker` | Acknowledges an OPEN blocker; raises severity to high only if it is below high (a critical blocker stays critical); expedites the WO to P2 only if the WO is live, open and at P3 or lower. Refused when none of those would change anything, or when the blocker is closed. **Never resolves the blocker or lifts its hold** | Supervisor+ |
| `acknowledge_blocker` | Acknowledge only. Refused when closed or already acknowledged | Supervisor+ |
| `create_draft_ncr` | Opens an OPEN in-process NCR (no disposition) with the locked NCR number. A named part or WO must be this company's and not deleted. Emits `ncr_created`, so the `ncr.created` notification fires as for a hand-raised NCR | Quality, Supervisor, Manager, Admin |
| `create_draft_po` | Creates a DRAFT PO through `create_purchase_order_command` (locked PO number, full audit snapshot, `purchase_order_created`) to the part's **current** primary supplier, which must exist and be live and active — there is no fallback vendor. Quantity is the suggested quantity, else the part's reorder quantity (refused when neither is positive). Warns when an open PO already has a line for the part. Nothing is sent to the vendor | Supervisor+ |

**Provenance.** Every audit row an applied action writes is written as the person who clicked
and carries `extra_data` `{source: "ai_recommendation", recommendation_id, recommendation_type,
source_module, action_type, approved_by, plan_fingerprint, prompt_version, model_version}`. An
escalation that raises severity writes its own `work_order_blocker` update row, because the
blocker service audits status changes only. The draft-NCR audit stays best-effort, as for the
other secondary NCR creators (CLAUDE.md invariant 2).

**Action cards are closed by someone who could act.** Record-only Accept on an action card is
limited to a person signed in whose role could apply it (403 for API tokens, agents and lower
roles). Dismiss, snooze and feedback stay open to everyone; suggest-only cards stay acceptable
by anyone. In the UI the button reads:

- **Accept & apply** when `can_apply`, with "Accept & apply will: {summary}" and any warnings;
- **Mark handled** when `can_mark_handled` but the apply is blocked by egress or the target,
  with "Won't apply: {reason}". It records the decision; nothing is changed;
- no accept button when neither (for example, an Operator on a Supervisor's card). The reason is
  shown, and Dismiss, Snooze and Feedback stay;
- plain **Accept** on a suggest-only card.

Every card also shows an **Open record** link (`target_href`), prominent when the apply is
blocked, so a card never dead-ends.

**Card lifecycle.**

- Applying a card stales its open siblings (same target and action type): `status_reason`
  `Superseded: recommendation #{id} was applied by {name}`.
- Closing a blocker (`PUT` to resolved/dismissed, or `/resolve`) stales its open cards in the same
  transaction (`Blocker {status}; nothing left to escalate`). No rejection is recorded.
- The nightly sweep retires cards whose target closed (see above).
- Every status writer — accept, apply, dismiss, snooze, the nightly wake and the staling paths —
  is one conditional `UPDATE` (compare-and-set), so a stale write can never overwrite or
  resurrect a decided card.

**UI.** In the Action Inbox, Accept & apply is **non-optimistic**: the card keeps a spinner until
the server answers; a refusal keeps the card, shows the server's message verbatim, and a 409 also
re-reads the list. Record-only Accept / Mark handled, Dismiss and Snooze stay optimistic. After a
successful apply the list is re-read so superseded siblings drop out. The contextual strip on
Work Order and Part detail uses the same door and reloads the record it sits on.

**Broadcasts.** After commit, `/apply` sends company-scoped realtime nudges (a work-order and
dashboard update for a priority change, the blocker's work-order/dashboard/shop-floor update, a
dashboard update for a PO or NCR). They are silent while `REALTIME_BROADCAST_ENABLED=false`.

Code: `backend/app/services/ai_action_applier.py`, `backend/app/api/endpoints/ai_learning.py`

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

## Phase 5 — Claude auto-execute (retired 2026-11-05)

**Retired by owner ruling 3.** From its release until 2026-11-05 the nightly job (and
`POST /ai/aggregate`) ended with an always-on agent that picked pending allowlisted cards —
with Claude (prompt `auto_execute_decision`, router task `auto_execute`), or by a confidence
threshold when AI egress was off or the call failed — and applied them as a borrowed account
(the company's first active Admin, Manager or Supervisor, else any active user) with role checks
bypassed. The service, the prompt and the router task are deleted, and Phase 2's `/apply` is now
the only executor.

The agent's environment variables are **ignored**: `AI_AUTO_EXECUTE_ENABLED`,
`AI_AUTO_EXECUTE_MIN_CONFIDENCE`, `AI_AUTO_EXECUTE_FALLBACK_MIN_CONFIDENCE`,
`AI_AUTO_EXECUTE_MAX_BATCH`, `AI_AUTO_EXECUTE_LLM_TIMEOUT` and `ANTHROPIC_AUTO_EXECUTE_MODEL`.
The nightly job logs one WARNING when any of them is still set; delete them (see
[DEPLOYMENT_RUNBOOK.md → Wave 2 rollout](DEPLOYMENT_RUNBOOK.md#wave-2-rollout)). The prompt id
`auto_execute_decision` stays reserved in `backend/app/services/prompts/CHANGELOG.md`.

**Historical rows are not rewritten.** A card the agent applied is `accepted` with
`status_reason` `Auto-executed by Claude always-on agent`, an `ai_interaction_events` row with
`ai_feature = auto_execute`, and `accepted_by` set to the borrowed account; the work-order,
blocker, NCR and PO audit rows it wrote are attributed to that account too. **None of those
records a person's decision.** Any "who accepted" or "who approved" report must exclude them.

## Roadmap status

| Phase | Status | Focus |
|---|---|---|
| 0 Instrumentation | **Shipped** | Auto outcomes on WO complete + quote terminal status |
| 1 Domain sensors | **Shipped** | Late WO, inventory risk, scrap trend → Action Inbox |
| 2 Accept & apply | **Shipped** (rewritten 2026-11-05) | A person's click runs a typed, allowlisted action; plan shown and fingerprint-checked |
| 3 Ambient AI | **Shipped** | Contextual strips, morning brief |
| 4 Continuous learners | **Shipped** (+ existing routing learning) | Cycle time / quote calibration / preferences |
| 5 Auto-execute | **Retired 2026-11-05** (owner ruling 3) | Was: Claude agent executing allowlisted actions without a person |

## Compliance notes

- Sensors do not call Anthropic; no CUI egress. The nightly job makes no LLM call.  
- Nothing AI-driven changes a controlled record without a named person's Accept & apply, which
  runs as that person, behind their role gate and company AI egress, and is audit-logged with
  the recommendation it came from. Plain Accept changes nothing.  
- Learning payloads are redacted via `redact_learning_payload`.  
- Multi-tenant isolation is enforced on every sensor query and on every id the plan resolver reads.

## Verification

```bash
cd backend
pytest tests/services/test_ai_sensors.py tests/api/test_ai_learning.py \
  tests/api/test_ai_recommendation_apply.py tests/api/test_ai_apply_tenancy_and_claim.py \
  tests/services/test_ai_no_autonomous_execution.py tests/test_ai_apply_single_door.py --no-cov -q
```

Plant smoke: seed a late WO and a low-stock part → `POST /api/v1/ai/aggregate` (or wait for 05:30 cron) → open Action Inbox → as a Supervisor with AI egress on, **Accept & apply** the priority card and confirm the work order's audit row names you.
