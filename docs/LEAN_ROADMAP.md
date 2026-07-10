# Lean Improvement Roadmap — Glossary Digest → 5 Phases

**Date:** 2026-07-09 · **Status:** APPROVED — Phase 1 (Flow & Quality Metrics pack) selected to build first; both CI-culture modules (idea board + recurring audits) confirmed in scope as later phases (jwerthen, 2026-07-09). **Phase 1 implemented 2026-07-10** (issue #88): items 1a–1f all shipped, incl. the wallboard KPI strip and crew-queue scrap codes; migration `063_scrap_reason_codes_oee`; metric/endpoint details in `API.md`, `WALLBOARD.md`, `KIOSK.md`, `RBAC_PERMISSIONS.md`, `DOCKER_PRODUCTION.md`.
**Source:** arda.cards lean-manufacturing glossary — 222 terms across 10 categories; all 166 live detail pages digested 2026-07-09 (the 37 index-only famous terms — Kanban, 5S, Kaizen, VSM, SMED, Poka-Yoke, Heijunka, Andon, Takt… — covered from standard lean canon). Every implementation anchor below was verified against the codebase at the time of writing.

This is the standing roadmap for making Werco ERP-MES a lean-principles-native system: which lean mechanics we adopt, in what order, what we reuse to build them, and — just as deliberately — what we are not building. It is a scope/sequencing document in the same spirit as [PROCESS_SHEETS_SCOPE.md](PROCESS_SHEETS_SCOPE.md); each phase goes through the normal per-PR gates (code-reviewer, compliance-auditor for data/auth surface, test-engineer, documentation-engineer) when built.

## The framing insight

The glossary's recurring criticism of ERPs is specific and worth taking personally:

- **Stale standards** — "ERP cycle times are usually whatever was entered at quote time"; standards never update from measured actuals.
- **Transactions, not flow** — "ERP timestamps capture transactions, not flow"; ERP-derived flow times undercount queues 30–50%.
- **Dashboards nobody sees** — "a KPI dashboard that lives only in a browser tab is invisible to the people doing the work"; "TPS lives on the shop floor, not in an ERP."

Werco's existing architecture is already the counter-position: **kiosk-first live capture** (data that isn't stale), the **TV wallboard** (a screen that lives on the floor), and **learners that update standards from actuals** (the cycle-time learner). This roadmap leans into closing the recorded-vs-real gap — surfacing flow/quality metrics from data already captured, then adding the missing real-time floor mechanics (andon, pull signals, WIP caps), then CI-culture support — rather than adding planning abstractions the glossary itself argues against.

## Glossary digest — 11 themes

Condensed to the mechanics worth keeping:

| # | Theme | Mechanics that matter for Werco |
|---|---|---|
| 1 | **Flow measurement** | Lead time, throughput time (Little's Law: WIP ÷ throughput), dock-to-dock, Process Cycle Efficiency (VA time ÷ lead time; typical shops 0.4–5%), schedule adherence (planned-vs-actual per window), build-to-schedule (mix% × volume% × sequence%), OTD (delivery-date based, partials penalized, per-customer) |
| 2 | **Waste taxonomy** | 8 wastes (DOWNTIME); Hidden Factory — unlogged rework/recovery is 10–30% of capacity ("ERP measures scheduled work only"); mura (batch release vs daily release), muri (overburden → defect spikes); VA/NVA/necessary-NVA step classification |
| 3 | **Pull & replenishment** | ROP = daily demand × lead time + safety; kanban sizing = demand-during-LT ÷ card qty + cushion; two-bin; supermarket; **Runner-Repeater-Stranger** — the glossary's own answer for job shops (runners→kanban, repeaters→leveled rotation, strangers→MRP/reserved slots); sequenced pull + FIFO lanes with queue caps for custom work; WIP limits ("a cap that lives on a screen gets ignored") |
| 4 | **Leveling & capacity** | Takt = available time ÷ demand (recalc monthly/quarterly, runners only); pitch; EPEI; heijunka mix patterns; run the **constraint at 75–85%** with slack elsewhere; Yamazumi balancing |
| 5 | **Quality at source** | Jidoka stop authority; andon cord + **response tiers: lead 60–90 s → supervisor 5 min → manager 15–30 min, response time measured**; FPY vs recorded defect rate (86% real vs 2% recorded example); DPU/DPMO/PPM; RTY = Π(op FPYs) (0.95¹⁰ ≈ 60%); Pareto weighted by cost; control charts (±3σ, don't chase noise); 8D; FMEA; countermeasure ≠ solution (requires standard-work update + recurrence monitoring) |
| 6 | **Changeover (SMED)** | Changeover = **last good part → first good part**; split internal/external setup, externalize, simplify; 75→18-min gains enable small batches; per-transition trend; sequence-dependent setup matrix |
| 7 | **TPM & reliability** | OEE = A×P×Q ("manual boards first; software helps later, once the discipline is in place"); six big losses mapped to A/P/Q; MTBF/MTTR; autonomous maintenance = operator 10-min daily checklists with findings log; planned-vs-reactive ratio target ~⅔ planned |
| 8 | **Standard work & people** | Standardized work = takt + sequence + standard WIP; time study (10–30 cycles, element level); TWI Job Instruction; **skills matrix** + cross-training (2 backups per critical station); standards must update from measured actuals |
| 9 | **Visual management** | Production control board (hourly plan/actual/comment); kamishibai rotating audit cards; visual controls legible without interpretation; 5–7 floor-visible KPIs; 5S/6S with red-tag register (30–90-day review) |
| 10 | **CI culture** | Kaizen (point/flow) + events; **idea board with closure loop (New→In Test→Adopted→Declined, response measured in days; the failure mode is "rewarding posts over adoptions")**; A3/PDCA; Toyota Kata; daily huddles (3 questions, 10–15 min) + tiered meetings; leader standard work; yokoten (spread wins, track adoption); hoshin/X-matrix (3–5 objectives max) |
| 11 | **Editorial stance** | Physical/visual first, "no software needed" recurs; the ERP criticisms quoted above. Implication: Werco's job is closing the recorded-vs-real gap, not adding planning abstractions |

## Current state

### Strong anchors to build on

- **OEE + downtime:** full `OEERecord` (A/P/Q + six big losses), `OEETarget`, `DowntimeEvent` with a **reason-code table** ([models/downtime.py](../backend/app/models/downtime.py)), dashboards (`endpoints/oee.py`, `endpoints/downtime.py`, `pages/OEE.tsx`, `DowntimeTracking.tsx`)
- **Quality records:** NCR/CAR/FAI-AS9102 (`models/quality.py`), SPC with Western Electric rules + Cp/Cpk (`models/spc.py`), process-sheet MEASUREMENT steps with gauge enforcement + `is_conforming`, OOT→QUALITY_HOLD blocker→NCR (a jidoka hook that already exists)
- **Standard work:** Process Sheets — revision-controlled typed steps, immutable WO snapshot, append-only records ([PROCESS_SHEETS_SCOPE.md](PROCESS_SHEETS_SCOPE.md))
- **Scheduling:** finite-capacity scheduler + drag-drop board + capacity heatmap (`services/scheduling_service.py`, `pages/Scheduling.tsx`)
- **Kiosk time capture:** `TimeEntry` (setup/run/rework/inspection types, qty produced/scrapped) with `source` adoption telemetry (A0.1) + `OperationalEvent` ([KIOSK.md](KIOSK.md))
- **AI recommendation/learning spine:** `AIRecommendation`/`AIOutcome`, sensors, learners (cycle_time, estimate_calibration), ActionInbox ([AI_ALWAYS_ON.md](AI_ALWAYS_ON.md))
- **Inventory:** cycle counts with variance, transactions with reason codes, lot/serial traceability
- **Maintenance:** PM schedules/checklists/`MaintenanceWorkOrder`; calibration (`Equipment` + `CalibrationRecord`); tool life tracking
- **Routing:** setup/run/move/queue hours, outside-op flags, inspection points; `WorkOrderBlocker` (MACHINE_DOWN / QUALITY_HOLD / MATERIAL_MISSING, severity)

### The 10 gaps

1. **No pull/kanban at all** — replenishment is push-MRP + static reorder point; no cards/loops/supermarket/two-bin/e-kanban
2. **No takt time, no measured lead time** — actuals captured but no demand-rate calc, no dock-to-dock/order-to-ship metric, no promised-vs-actual OTD KPI (late = wallboard ticker only)
3. **No heijunka/level loading** — scheduler is greedy priority+due-date, front-loads
4. **No true andon** — blockers are form submissions; no one-tap operator call, no escalation timers/SLA, no live station status
5. **Adoption/CI telemetry captured but never surfaced** — no digital-completion %, clock-in coverage, or backfill-rate dashboard
6. **No shift/working-time calendar** — flat `capacity_hours_per_day`, hardcoded Mon–Fri
7. **No bottleneck/constraint designation** — heatmap shows overload; nothing marks or protects the constraint
8. **Scrap has NO reason codes** (free text) while downtime does; no AQL/sampling engine
9. **SMED = data distinction only** — setup hours exist but no internal/external split, no changeover trend, no sequence-dependent setup matrix
10. **WIP flow thin** — no WIP aging, no WIP limits/CONWIP, no VA/NVA classification; move/queue hours are static standards, not measured

Also absent: 5S module, suggestion/kaizen queue for humans, A3/PDCA object, VSM artifact, min/max policy, RCCP, asset master separate from WorkCenter, MTBF/MTTR.

## The roadmap — five phases

Ordered by verified feasibility, dependency, and build-on-strengths. Standing rules for every schema addition: `TenantMixin` non-null `company_id` + index, `ENABLE ROW LEVEL SECURITY` in the migration, `AuditService` on state changes, soft-delete where applicable. Changes touching data access route through compliance-auditor; test-engineer + documentation-engineer close every phase (repo definition of done).

### Phase 1 — Flow & Quality Metrics pack (BUILD FIRST — user-selected)

Zero new floor behavior; surfaces data already captured. Lean terms served: OTD, lead time, Little's Law, PCE, schedule adherence, FPY/RTY, scrap Pareto, Hidden Factory, OEE, MTBF/MTTR, DIOH.

- **1a. Scrap reason codes** — new `scrap_reason_codes` table mirroring `DowntimeReasonCode` ([models/downtime.py:66-78](../backend/app/models/downtime.py)) **but with `UniqueConstraint("company_id", "code")`** — the downtime template's `unique=True` on `code` (line 72) is a globally-unique-across-tenants bug; do not copy it. Nullable `scrap_reason_code_id` FK on `TimeEntry`/`WorkOrderOperation`/`WorkOrder` beside the existing free text (free text becomes narrative detail — AS9100D). Wire the 3 write paths: shop-floor clock-out (`ClockOut` schema), `/operations/{id}/production`, work_orders `/complete`. CRUD endpoints mirror downtime's (list/create/update, deactivate-not-delete); kiosk + desktop pickers. Extends the already-approved scrap-reason enforcement work.
- **1b. Ship-based OTD/OTIF** — new analytics calc joining `Shipment.ship_date` ([models/shipping.py:60](../backend/app/models/shipping.py); partials = multiple shipments with `quantity_shipped`) against the promise with precedence **`must_ship_by || due_date`** ([models/work_order.py:64](../backend/app/models/work_order.py)); OTIF = full qty shipped by promise; per-customer breakdown. Replaces/parallels the completion-based `_calculate_otd_kpi` ([analytics_service.py:505](../backend/app/services/analytics_service.py)). Prereq: a promise-field hygiene pass (report of WOs missing both fields) before publishing the KPI.
- **1c. Measured lead time & WIP aging** — emit an `operation_ready` `OperationalEvent` in `release_first_ready_operation` / `release_next_ready_operation` ([work_order_state_service.py:173/189](../backend/app/services/work_order_state_service.py)) — zero-migration; a `ready_at` column is the fallback. Queue time = op N `actual_end` → op N+1 `actual_start`; WO lead time = `released_at` → last op `actual_end` → ship; throughput time via Little's Law; PCE = run-hours ÷ elapsed. New flow report endpoint + WIP-aging view (reuse the wallboard elapsed-time helpers).
- **1d. FPY/RTY** — add `quantity_reworked` to `WorkOrderOperation`, incremented where REWORK TimeEntries book quantity (today rework qty is conflated into `quantity_complete`). FPY per op = (complete − reworked − scrapped) ÷ (complete + scrapped); RTY = Π(op FPYs) per routing. Scrap/defect Pareto by reason code (needs 1a), with a weight-by-cost option.
- **1e. Auto-OEE** — extract the ~220-line calculation already inside `POST /oee/calculate/{work_center_id}` ([endpoints/oee.py:439](../backend/app/api/endpoints/oee.py)) into a new `services/oee_service.py` (thin-router invariant); add `OEERecord.calculation_source` (auto|manual) + unique `(company_id, work_center_id, record_date, shift)`; nightly ARQ cron per tenant (worker.py cron pattern), manual records win. The staffed-time availability convention stands (no shift-calendar dependency). MTBF/MTTR per work center from `DowntimeEvent` + maintenance records.
- **1f. Adoption + hidden-factory dashboard** — `GET /analytics/adoption`: digital-completion %, clock-in coverage, backfill rate from `OperationalEvent` + `TimeEntry.source` (captured since A0.1, never surfaced). Hidden factory: rework hours % (REWORK entries), planned-vs-reactive maintenance ratio.
- **Frontend:** extend Analytics/Reports with a Flow panel (MiniStat/CockpitPanel/DataTable primitives, `statusColors`); add a compact KPI strip to the Wallboard — the glossary's point about floor-visible KPIs, not browser-tab dashboards.
- **Risk controls (from feasibility verification):** segment every metric by provenance — **exclude `backfill`/`import` sources from baselines** (completion paths backfill `actual_*` timestamps, e.g. the clock-out one-shot completion stamps `actual_start` retroactively — [shop_floor.py:1057-1073](../backend/app/api/endpoints/shop_floor.py)); document the no-backdating limitation of `ship_date`.

### Phase 2 — Andon + Daily Management (visual management / jidoka / tiered meetings)

- **2a. Andon** — extend `WorkOrderBlocker` (no new model): `escalation_tier`, `last_escalated_at`, `responded_at`, `raised_via`. Kiosk one-tap flow = badge-scan → 5-min operator token → the existing hold/blocker endpoint (preserves the kiosk-token two-capability invariant; widening kiosk scope would need a security review — not recommended). 5-min ARQ escalation cron: OPEN blockers past a per-severity ack SLA → bump tier + `NotificationLog` + `safe_broadcast` + `OperationalEvent`. Wallboard: station andon states (color + tier + age), acknowledge tracking. Metrics: time-to-acknowledge/respond/resolve (glossary tier benchmark: lead 60–90 s → supervisor 5 min → manager 15–30 min; make SLAs configurable per severity).
- **2b. Huddle board** — a sibling wallboard view: new payload builder in [wallboard_service.py](../backend/app/services/wallboard_service.py) (alongside `build_wallboard_payload`), same `get_display_or_user` display-token dependency ([deps.py:182](../backend/app/api/deps.py)). Content: yesterday (OTD, scrap Pareto, schedule adherence, andon response times), today (plan by WC), issues (open blockers/andons with age). This is the tier-1/tier-2 meeting screen.
- **2c. Schedule adherence** — planned (`scheduled_start/end`) vs actual per WC/day; feeds the huddle board.

### Phase 3 — Pull & Flow Control (kanban / RRS / WIP limits / FIFO)

- **3a. Runner-Repeater-Stranger + inventory KPIs** — classification service from `InventoryTransaction` ISSUE/SHIP + WO completions (frequency + coefficient of variation); weekly ARQ job persists a new `Part.demand_class`. DIOH, turns, excess-inventory (days-of-cover) reports per part.
- **3b. E-kanban for runners/purchased consumables** — `KanbanLoop` (part, vendor, bin_qty, num_cards, location) + `KanbanCard` (state: full/triggered/on_order/received); sizing calculator (demand-during-lead-time + safety ÷ card qty); scanner `KB:` prefix dispatched through the existing `scan_resolve_service` → trigger → draft PO via the `MRPAutoService` PO/vendor/price helpers (`_create_po_from_action` / `_get_preferred_vendor` / `_get_part_cost`, [mrp_auto_service.py:99/232/291](../backend/app/services/mrp_auto_service.py)) or a WO for make-parts; card PDF via the existing thermal-print pipeline (`build_kanban_card_pdf` alongside `build_receiving_label_pdf` in [label_service.py](../backend/app/services/label_service.py); respects `allow_print_egress` — see [THERMAL_LABEL_PRINTING.md](THERMAL_LABEL_PRINTING.md)). MRP remains the path for strangers — per the glossary's own RRS guidance.
- **3c. WIP caps + FIFO + leveled release** — per-WC queue cap (WIP limit); release gating warns/blocks when the first op's queue is over cap; FIFO-within-priority dispatch discipline; release-leveling suggestion (spread the week's releases; cap daily load at the constraint) surfaced as an `AIRecommendation`, not a silent auto-action.

### Phase 4 — Capacity honesty & Changeover (shift calendar / constraint / SMED)

- **4a. ShiftCalendar** — company default + per-WC override: weekday→hours + `ShiftCalendarException` (holiday/overtime). Single seam: replace the flat `capacity_hours_per_day or 8.0` + hardcoded weekend skip ([scheduling_service.py:155, 319](../backend/app/services/scheduling_service.py)) with `calendar.capacity_hours(wc, date)`; later feeds calendar-based OEE availability and workday-true lead times.
- **4b. Constraint designation** — `is_constraint` on WorkCenter + a 75–85% utilization target band on dashboards/heatmap; scheduler protects the constraint (buffer before it; light drum-buffer-rope, not full TOC).
- **4c. SMED analytics + setup checklists** — changeover trend per WC and per part-transition (setup TimeEntries + CHANGEOVER downtime; definition: **last good part → first good part**, linked to first-pass inspection); internal/external setup step tagging (reuse the process-sheet step machinery or a routing setup checklist) so the kiosk shows external steps during the prior run; a from→to setup matrix feeding `optimize_setup` (today it only groups identical parts).

### Phase 5 — CI Culture (idea board / audits / A3) — user-confirmed scope

- **5a. Idea Board** — `ImprovementIdea` model; kiosk one-tap capture (text/photo); states New→In Test→Adopted→Declined with owner + response SLA; closure metrics (the glossary's failure mode is "rewarding posts over adoptions"); yokoten flag ("applies to other WCs?"); ActionInbox integration.
- **5b. Recurring audits (5S/kamishibai)** — `AuditSchedule` generalizing the MaintenanceSchedule checklist pattern: zones, rotating auditor assignment, per-S 1–5 scoring, photo evidence, trend per zone; red-tag register (item, date, reason, holding area, 30–90-day review queue).
- **5c. A3 view on CAR** — one-page A3 rendering of existing `CorrectiveActionRequest` fields + countermeasure recurrence monitoring (reuse the `AIOutcome` baseline/target measurement pattern).

## Deliberately NOT building

Job-shop fit and the glossary's own physical-first stance rule these out:

| Not building | Rationale |
|---|---|
| VSM drawing tool | A VSM-lite *report* over measured routing actuals may come later (post-Phase 1 data); a drawing canvas is a planning abstraction the floor won't touch |
| Yamazumi / line balancing | Line-balancing math is for repetitive lines, not a job-shop mix |
| Hoshin / X-matrix module | Strategy deployment stays a leadership artifact; 3–5 objectives don't need software |
| 3P | Production-preparation process is a workshop method, not an ERP feature |
| Takt displays for the general mix | Takt is meaningless across a job-shop's whole mix; revisit for runners only, after Phase 3 classification data exists |
| 5S execution | 5S itself stays physical — the ERP only schedules and scores the audits (Phase 5b) |

## Metric definitions to standardize

These definitions are the contract for every implementation above — dashboards, reports, and learners must agree on them.

| Metric | Definition / formula |
|---|---|
| **OTD** | Shipped (`Shipment.ship_date`) on or before the promise; promise precedence = **`must_ship_by \|\| due_date`**; partials penalized; per-customer breakdown |
| **OTIF** | Full ordered quantity shipped by the promise date (OTD ∧ in-full) |
| **FPY (per op)** | (complete − reworked − scrapped) ÷ (complete + scrapped) |
| **RTY (per routing)** | Π(op FPYs) — e.g. 0.95¹⁰ ≈ 60% |
| **Lead time (WO)** | `released_at` → last op `actual_end` → ship |
| **Queue time** | op N `actual_end` → op N+1 `actual_start` (with `operation_ready` events as the true ready marker) |
| **Throughput time** | Little's Law: WIP ÷ throughput |
| **PCE** | Value-added (run) hours ÷ total elapsed lead time (typical shops 0.4–5% — don't be alarmed by small numbers) |
| **OEE** | Availability × Performance × Quality; staffed-time availability convention until a shift calendar exists (Phase 4a) |
| **MTBF** | Run time ÷ number of failures |
| **MTTR** | Total repair time ÷ number of failures — clock starts at failure and **includes parts wait** |
| **Availability (reliability)** | MTBF ÷ (MTBF + MTTR) |
| **Changeover time** | **Last good part of run A → first good part of run B** (not "setup labor booked") |
| **Takt** | Available time ÷ demand — runners only, recalculated monthly/quarterly (Phase 3+ only) |
| **Schedule adherence** | Planned (`scheduled_start/end`) vs actual, per WC per day |
| **Provenance rule** | Every metric segments by capture source; **`backfill` and `import` `source` values are excluded from metric baselines** — force-complete/one-shot paths backfill `actual_*` timestamps and would poison flow numbers |

## Verification approach (when phases are built)

- **Phase 1:** pytest service tests with fixture WOs of known timestamps/quantities → assert exact OTD/FPY/RTY/lead-time/queue-time values; provenance-segmentation tests (backfilled entries excluded); migration idempotency + downgrade + the RLS CI gate; auto-OEE cron test comparing service output to the existing endpoint calc on seeded data; Jest for new panels; kiosk scrap-code picker via preview + a Playwright happy path.
- **Later phases:** same gates; andon adds a websocket-broadcast integration test + an escalation-cron time-travel test; kanban adds scanner-resolve and PO-draft tests with egress switches off.
- **Live check:** seeded dev stack (`python -m scripts.seed_data`), preview the Analytics Flow panel and the Wallboard KPI strip.
