export const meta = {
  name: 'workorder-tracking-audit',
  description: 'Audit work-order completion tracking across the platform + optimization, with adversarial verification',
  phases: [
    { title: 'Map', detail: 'Map the WO completion propagation surface (backend + frontend)' },
    { title: 'Audit', detail: '15 dimension finders over tracking/compliance/optimization' },
    { title: 'Verify', detail: 'Adversarially refute each finding against the code' },
    { title: 'Synthesize', detail: 'Dedupe, prioritize, route to owning subagents; completeness critic' },
  ],
}

// ---------- Shared context the agents need ----------
const DOMAIN = `
SYSTEM: Werco ERP-MES — FastAPI(Python 3.11)/SQLAlchemy2 backend + React19/TS frontend.
Compliance-critical (AS9100D/ISO9001/CMMC L2). INVARIANTS (treat violations as bugs):
  - Tenant isolation: tenant tables carry company_id (TenantMixin); EVERY query scoped to active company via app.db.tenant_filter helpers + get_current_company_id.
  - Audit logging: every create/update/delete/status-change recorded via AuditService (log_create/update/delete/status_change) from get_audit_service. audit_log is a tamper-evident hash chain.
  - Soft delete: SoftDeleteMixin models filter is_deleted == False; never hard DELETE.
  - Optimistic locking: OptimisticLockMixin 'version' column respected on concurrent updates.
  - Traceability: lot/serial genealogy, part/BOM revisions; preserve historical records.

WORK-ORDER COMPLETION MODEL (already mapped — ground truth):
  Models: WorkOrder(status: draft/released/in_progress/on_hold/complete/closed/cancelled; quantity_ordered/complete/scrapped; current_operation_id; actual_start/end; actual_hours/cost; lot_number; serial_numbers).
    WorkOrderOperation(sequence; status: pending/ready/in_progress/complete/on_hold; quantity_complete/scrapped; component_part_id/quantity; requires_inspection/inspection_complete; actual times; started_by/completed_by).
    TimeEntry(clock_in/out; quantity_produced/scrapped) = durable shop-floor completion evidence.
    OperationalEvent = append-only AI/realtime signal store.
  Completion status (WorkOrderStatus.COMPLETE) is SET IN THREE PLACES that must agree:
    1. backend/app/services/work_order_state_service.py  (centralized: reconcile_work_orders_from_completion_evidence, _sync_work_order_status_from_operations, sync_work_order_quantity_complete, release_next_ready_operation, work_order_operation_progress).
    2. backend/app/api/endpoints/shop_floor.py  (~L260-460 complete-op; ~L1240-1560 scan/complete; reconcile on GET list ~L577,989).
    3. backend/app/api/endpoints/work_orders.py  (~L1640 complete WO; ~L1858 complete op; reconcile on GET ~L448,766,1100).
  Observed downstream calls on completion: SchedulingService.update_availability_rates(...) and OperationalEventService(db).emit(...).
  Apparently NOT called on completion (verify whether this is a real tracking gap): inventory receipt/backflush, job_costing rollup, traceability lot/serial genealogy, MRP demand close, OEE, notification_service, webhook_service.
  NOTE: reconcile_work_orders_from_completion_evidence() MUTATES rows and is invoked from GET/list read handlers.
`

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['dimension', 'area_summary', 'findings'],
  properties: {
    dimension: { type: 'string' },
    area_summary: { type: 'string', description: 'What the code actually does in this area (2-5 sentences)' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'title', 'category', 'severity', 'files', 'description', 'evidence', 'proposed_fix', 'confidence'],
        properties: {
          id: { type: 'string', description: 'short id unique within this dimension, e.g. INV-1' },
          title: { type: 'string' },
          category: { type: 'string', enum: ['tracking-gap', 'correctness-bug', 'compliance', 'optimization'] },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          files: { type: 'array', items: { type: 'string' }, description: 'file:line references' },
          description: { type: 'string' },
          evidence: { type: 'string', description: 'concrete code observation that proves the issue' },
          proposed_fix: { type: 'string' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
      },
    },
  },
}

const VERIFIED_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['dimension', 'verified_findings'],
  properties: {
    dimension: { type: 'string' },
    verified_findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'title', 'category', 'severity', 'files', 'description', 'proposed_fix', 'verdict', 'verifier_reasoning', 'owner'],
        properties: {
          id: { type: 'string' },
          title: { type: 'string' },
          category: { type: 'string', enum: ['tracking-gap', 'correctness-bug', 'compliance', 'optimization'] },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'], description: 'adjusted severity after verification' },
          files: { type: 'array', items: { type: 'string' } },
          description: { type: 'string' },
          proposed_fix: { type: 'string' },
          verdict: { type: 'string', enum: ['confirmed', 'rejected', 'needs-info'] },
          verifier_reasoning: { type: 'string', description: 'why confirmed/rejected after re-reading the code' },
          owner: { type: 'string', enum: ['backend-engineer', 'frontend-engineer', 'database-migration-specialist', 'ai-integration-specialist', 'test-engineer', 'documentation-engineer', 'compliance-auditor'] },
        },
      },
    },
  },
}

// ---------- Phase 1: Map ----------
phase('Map')
const MAP_TASKS = [
  {
    label: 'map:core-state',
    prompt: `${DOMAIN}\nMAP the CORE completion state machine. Read fully: backend/app/services/work_order_state_service.py and the completion/release/close/list code paths in backend/app/api/endpoints/work_orders.py. Document: how an operation completion rolls up to WorkOrder status/quantity_complete/actual_start/end/current_operation_id; how the next operation is released; how reconcile_work_orders_from_completion_evidence works and from which handlers it runs; component-operation vs parent-assembly handling; the "regenerated operation slot" logic. Note anything that looks duplicated vs shop_floor.py. Return a precise markdown map with file:line anchors.`,
  },
  {
    label: 'map:shopfloor-scanner',
    prompt: `${DOMAIN}\nMAP the SHOP-FLOOR + SCANNER completion paths. Read fully: backend/app/api/endpoints/shop_floor.py and backend/app/api/endpoints/scanner.py and backend/app/models/time_entry.py. Document: clock-in/clock-out flow, how TimeEntry quantity_produced/scrapped becomes operation.quantity_complete and operation/WO status, where WorkOrderStatus.COMPLETE is set directly, idempotency of repeated completes, any transaction/locking, and which downstream services are invoked on completion. Return a precise markdown map with file:line anchors.`,
  },
  {
    label: 'map:downstream-material-cost-trace-mrp',
    prompt: `${DOMAIN}\nMAP DOWNSTREAM PROPAGATION on WO/operation completion for: INVENTORY (finished-goods receipt + component backflush/consumption + lot creation), JOB COSTING (actual hours/cost rollup), TRACEABILITY (lot/serial genealogy, as-built), MRP (demand satisfaction / supply close), SCHEDULING (capacity release). Read: backend/app/models/inventory.py, backend/app/api/endpoints/inventory.py, backend/app/api/endpoints/receiving.py, backend/app/models/job_costing.py, backend/app/api/endpoints/job_costing.py, backend/app/models/job.py, backend/app/api/endpoints/traceability.py, backend/app/services/mrp_service.py, backend/app/services/mrp_auto_service.py, backend/app/models/mrp.py, backend/app/services/scheduling_service.py. grep for work_order references in each. For EACH downstream system, state clearly: is it updated when a WO/op completes? If yes, where (file:line). If no, that is a candidate tracking gap. Return precise markdown.`,
  },
  {
    label: 'map:quality-blockers-oee-analytics',
    prompt: `${DOMAIN}\nMAP completion's interaction with QUALITY/INSPECTION gates (requires_inspection / inspection_complete enforcement, FAI/in-process/final, non-conformance), WORK-ORDER BLOCKERS (do blockers prevent completion / auto-resolve on completion), and ANALYTICS/OEE/DOWNTIME/on-time metrics. Read: backend/app/api/endpoints/quality.py, backend/app/models/quality.py, backend/app/services/work_order_blocker_service.py, backend/app/api/endpoints/work_order_blockers.py, backend/app/services/analytics_service.py, backend/app/models/oee.py, backend/app/api/endpoints/oee.py, backend/app/models/downtime.py. State whether inspection actually gates op/WO completion, and whether completion feeds OEE/analytics correctly. Return precise markdown with anchors.`,
  },
  {
    label: 'map:events-notify-webhook-frontend',
    prompt: `${DOMAIN}\nMAP cross-cutting signal + UI. (a) On completion, what operational events / notifications / webhooks / realtime(websocket) messages / AI-context updates are emitted? Read: backend/app/services/operational_event_service.py, backend/app/services/notification_service.py, backend/app/services/webhook_service.py, backend/app/core/realtime.py, backend/app/core/websocket.py, backend/app/jobs/notification_jobs.py. (b) FRONTEND data flow & post-completion freshness: read frontend/src/pages/WorkOrders.tsx, frontend/src/pages/WorkOrderDetail.tsx, frontend/src/pages/Routing.tsx, frontend/src/components/parts/PartRoutingTab.tsx, and the api client under frontend/src/services. Document how the UI reflects completion (refetch/invalidation/polling/ETag) and where it could show stale state. Return precise markdown with anchors.`,
  },
]
const maps = await parallel(
  MAP_TASKS.map((t) => () => agent(t.prompt, { label: t.label, phase: 'Map', agentType: 'general-purpose' })),
)
const MAP = '## PROPAGATION MAP (from scout agents)\n\n' + maps.filter(Boolean).join('\n\n---\n\n')
log(`Map complete: ${maps.filter(Boolean).length}/${MAP_TASKS.length} sections.`)

// ---------- Phase 2+3: Audit dimensions -> adversarial verify (pipeline) ----------
const DIMENSIONS = [
  { key: 'rollup', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'Correctness of operation->work-order rollup in work_order_state_service.py. Edge cases: component operations completing before parent assembly; regenerated operation slots; partial quantities; scrap; out-of-sequence completion; quantity_complete clamping; actual_start/end and current_operation_id correctness; status transitions skipping IN_PROGRESS; what happens when there are zero operations.' },
  { key: 'shopfloor-idempotency', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'Shop-floor/scanner completion correctness: double-submit / repeated complete idempotency, concurrent operators on the same operation (race conditions), missing DB row locking, optimistic-lock (version) handling, TimeEntry->operation reconciliation correctness, partial vs full completion.' },
  { key: 'duplication', owner: 'backend-engineer', agentType: 'code-reviewer',
    lens: 'Divergence/duplication between the THREE completion sites (work_order_state_service.py vs shop_floor.py vs work_orders.py). Identify logic that is copied and could drift, behaviors that differ between office-complete and shop-floor-complete for the same conceptual action, and propose consolidation onto the shared service.' },
  { key: 'inventory', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'INVENTORY tracking gap: when a WO/op completes, is finished-goods quantity received into inventory? Are component/material quantities consumed (backflush)? Are inventory transactions audited & tenant-scoped? Is a lot created? If none of this happens on completion, quantify the impact and propose where to wire it.' },
  { key: 'costing', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'JOB COSTING / actuals: are WorkOrder.actual_hours and actual_cost (and operation actual_setup/run hours) rolled up from TimeEntry on completion? Is job_costing kept in sync? Are estimate-vs-actual variances captured? Identify gaps and where to compute.' },
  { key: 'traceability', owner: 'backend-engineer', agentType: 'compliance-auditor',
    lens: 'TRACEABILITY (AS9100D): on completion, is lot/serial genealogy recorded (as-built, parent/child lot linkage, which components/lots consumed)? Is serial_numbers populated/validated for serialized parts? Gaps here are compliance defects. Cite traceability.py behavior.' },
  { key: 'mrp-scheduling', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'MRP & SCHEDULING: does completing a WO satisfy/close MRP demand or supply and stop it from being re-planned? Does scheduling release the reserved capacity and recompute? Verify update_availability_rates is the right/sufficient call and runs on every completion path (office + shop floor). Identify gaps & inconsistencies between paths.' },
  { key: 'quality-gate', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'QUALITY GATE enforcement: does requires_inspection / inspection_complete actually BLOCK operation or WO completion? Can a WO reach COMPLETE with an open required inspection or open non-conformance? This is a compliance-relevant correctness gap if not enforced.' },
  { key: 'blockers', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'WORK-ORDER BLOCKERS: do active blockers prevent starting/completing operations or the WO? Are blockers auto-resolved or surfaced when the WO completes/closes? Tenant-scope & audit of blocker state changes. Read work_order_blocker_service.py.' },
  { key: 'analytics-oee', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'ANALYTICS/OEE/on-time: does completion feed OEE (availability/performance/quality), throughput, on-time-delivery, and downtime metrics correctly? Are completed/scrapped quantities and actual_end vs due_date used consistently? Identify metric correctness gaps.' },
  { key: 'events-notify', owner: 'backend-engineer', agentType: 'general-purpose',
    lens: 'EVENTS/NOTIFICATIONS/WEBHOOKS/REALTIME on completion: is an OperationalEvent emitted on EVERY completion path (op complete, WO complete, WO close)? Are stakeholder notifications and outbound webhooks fired on completion? Are websocket/realtime consumers updated? Identify completion paths that emit nothing.' },
  { key: 'tenant-isolation', owner: 'compliance-auditor', agentType: 'compliance-auditor',
    lens: 'TENANT ISOLATION across ALL work-order completion queries (work_orders.py, shop_floor.py, scanner.py, routing.py, state service, blocker service). Every WorkOrder/WorkOrderOperation/TimeEntry query must be scoped to the active company_id. Flag any query that could read/write another tenant rows, especially operation/time-entry lookups by id without company filter.' },
  { key: 'audit-softdelete-lock', owner: 'compliance-auditor', agentType: 'compliance-auditor',
    lens: 'AUDIT LOGGING + SOFT DELETE + OPTIMISTIC LOCK on completion: is every status-change / quantity change / completion recorded via AuditService? Do reconcile-driven status mutations (incl. those triggered from GET handlers) get audited? Are soft-deleted WOs/operations excluded from completion queries? Is the version column respected? Flag silent state changes that bypass the audit trail.' },
  { key: 'backend-perf', owner: 'backend-engineer', agentType: 'code-reviewer',
    lens: 'BACKEND PERFORMANCE/OPTIMIZATION: N+1 queries over operations/time_entries (missing selectinload/joinedload), reconcile_work_orders_from_completion_evidence performing WRITES inside GET/list handlers (write amplification, lock contention, side effects on reads), repeated reconciliation per request, missing indexes on hot filters (status, due_date, company_id, work_order_id), unbounded queries / pagination, per-row SchedulingService calls in loops. Quantify and propose concrete fixes.' },
  { key: 'frontend-perf', owner: 'frontend-engineer', agentType: 'general-purpose',
    lens: 'FRONTEND correctness/optimization for WO tracking: after completing an operation/WO does the UI refetch or invalidate cached data (ETag) so it is not stale? Unnecessary re-renders / refetch loops / polling intervals, missing optimistic update or missing invalidation, list vs detail consistency, large list rendering. Read WorkOrders.tsx, WorkOrderDetail.tsx, Routing.tsx, PartRoutingTab.tsx and the api client.' },
]

const verified = await pipeline(
  DIMENSIONS,
  (d) =>
    agent(
      `${DOMAIN}\n\n${MAP}\n\nYou are auditing ONE dimension of work-order completion tracking.\nDIMENSION: ${d.key}\nFOCUS/LENS: ${d.lens}\n\nRead the actual source files (do not rely only on the map). Report the most important, GENUINE findings (typically 2-7; do not pad with speculation). For each: precise file:line evidence, concrete impact, and a concrete proposed fix. Prefer high-signal correctness/tracking gaps and real optimizations over style nits. If the area is actually healthy, say so with few/zero findings.`,
      { label: `find:${d.key}`, phase: 'Audit', schema: FINDINGS_SCHEMA, agentType: d.agentType },
    ),
  (found, d) => {
    if (!found || !found.findings || found.findings.length === 0) {
      return { dimension: d.key, verified_findings: [] }
    }
    return agent(
      `${DOMAIN}\n\nYou are a SKEPTICAL senior reviewer. Adversarially verify each finding below by RE-READING the cited code yourself. A finding is "confirmed" ONLY if the code genuinely exhibits the problem AND the impact is real in this system's context; otherwise "rejected" (default to rejected when you cannot prove it from the code) or "needs-info". Adjust severity to reflect true impact. Assign the most appropriate owning subagent. Beware false positives: e.g. behavior that is actually handled in the shared state service, queries that ARE tenant-scoped via a helper, or "gaps" that are intentionally handled elsewhere (shipping, separate receipt flow, etc.).\n\nDIMENSION: ${d.key}\nFINDER AREA SUMMARY: ${found.area_summary}\nFINDINGS TO VERIFY (JSON):\n${JSON.stringify(found.findings, null, 2)}`,
      { label: `verify:${d.key}`, phase: 'Verify', schema: VERIFIED_SCHEMA, agentType: 'general-purpose' },
    )
  },
)

// ---------- Phase 4: Synthesize ----------
phase('Synthesize')
const confirmed = verified
  .filter(Boolean)
  .flatMap((v) => (v.verified_findings || []).map((f) => ({ ...f, dimension: v.dimension })))
  .filter((f) => f.verdict === 'confirmed' || f.verdict === 'needs-info')

// dedupe by primary file + normalized title
const seen = new Set()
const deduped = []
for (const f of confirmed) {
  const primaryFile = (f.files && f.files[0] ? String(f.files[0]).split(':')[0] : 'unknown')
  const k = primaryFile + '::' + String(f.title || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().slice(0, 60)
  if (seen.has(k)) continue
  seen.add(k)
  deduped.push(f)
}
log(`Confirmed findings after dedupe: ${deduped.length} (from ${confirmed.length} pre-dedupe).`)

const SUMMARY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['executive_summary', 'themes', 'prioritized_actions'],
  properties: {
    executive_summary: { type: 'string' },
    themes: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['theme', 'finding_ids', 'why_it_matters'],
        properties: {
          theme: { type: 'string' },
          finding_ids: { type: 'array', items: { type: 'string' } },
          why_it_matters: { type: 'string' },
        },
      },
    },
    prioritized_actions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['rank', 'title', 'severity', 'category', 'owner', 'files', 'change', 'risk', 'finding_ids'],
        properties: {
          rank: { type: 'integer' },
          title: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          category: { type: 'string', enum: ['tracking-gap', 'correctness-bug', 'compliance', 'optimization'] },
          owner: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          change: { type: 'string', description: 'concrete change to make' },
          risk: { type: 'string', description: 'risk / blast radius / migration or data implications' },
          finding_ids: { type: 'array', items: { type: 'string' } },
        },
      },
    },
  },
}

const [synthesis, critique] = await parallel([
  () =>
    agent(
      `${DOMAIN}\n\nYou are the lead engineer. Below are adversarially-CONFIRMED findings about work-order completion tracking & optimization. Produce a prioritized remediation plan. Group into themes. Rank actions by (compliance/correctness > tracking gaps > optimization) and by severity. Each action must be concrete (files + change), note risk/blast-radius (esp. anything needing an Alembic migration or touching live multi-tenant data), and name the owning subagent. Be decisive about sequencing.\n\nCONFIRMED FINDINGS (JSON):\n${JSON.stringify(deduped, null, 2)}`,
      { label: 'synthesize:plan', phase: 'Synthesize', schema: SUMMARY_SCHEMA, agentType: 'general-purpose' },
    ),
  () =>
    agent(
      `${DOMAIN}\n\n${MAP}\n\nYou are a COMPLETENESS CRITIC. The audit covered these dimensions: ${DIMENSIONS.map((d) => d.key).join(', ')}. Confirmed findings so far (titles):\n${deduped.map((f) => '- [' + f.severity + '] ' + f.title).join('\n')}\n\nIdentify what the audit likely MISSED regarding "work orders correctly tracked across the entire platform when tasks complete": untouched completion paths, modules not examined (e.g. shipping consuming completed WOs, engineering-change effect on in-flight WOs, parent/child WO rollup for assemblies, laser_nest operations, exports/reports correctness, search indexing, document/CoC generation on completion, time-entry approval effect on costing), and any cross-module invariant not checked. Return a concise prioritized list of concrete gaps worth a follow-up pass, each with the file(s) to inspect and why.`,
      { label: 'synthesize:critic', phase: 'Synthesize', agentType: 'general-purpose' },
    ),
])

return {
  counts: {
    dimensions: DIMENSIONS.length,
    confirmed_predupe: confirmed.length,
    confirmed_deduped: deduped.length,
  },
  confirmed_findings: deduped,
  synthesis,
  completeness_critique: critique,
}
