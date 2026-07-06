# Carbon ERP → Werco Gap Analysis

**Date:** 2026-07-03
**Subject:** [Carbon ERP](https://github.com/crbnos/carbon) (open-source manufacturing ERP/MES/QMS — React Router + Supabase + Inngest, AGPL) studied at commit `f0d83ce` (2026-07-03), compared feature-by-feature against Werco ERP-MES.
**Method:** 105-agent analysis — 10 domain deep-dives over Carbon's actual code (routes, migrations, services, its `.ai/rules/*.md` module docs), each claimed gap then **adversarially verified against Werco's codebase** before inclusion. 112 gaps confirmed (71 high-value), 6 claims rejected in verification (see [Rejected claims](#rejected-claims--where-werco-is-ahead)).

> **License note:** Carbon is AGPL-3.0. This document is for studying *patterns and feature design*. Do not copy Carbon source code into Werco.

---

## Executive summary

Carbon is strongest exactly where Werco is thinnest: **structured shop-floor data capture, statistical receiving inspection, queue-aware scheduling, and supplier-facing workflows**. Werco is verifiably ahead on compliance infrastructure (hash-chained audit log, CUI egress kill switches), AI (Carbon has almost none), gauge calibration, training/skill matrix, FAI, and the kiosk/wallboard/visitor station family.

### Top five adoption candidates

1. **Typed operation steps + procedures with MES capture** — reusable versioned work instructions built from typed steps (Measurement with min/max, Checkbox, List, File…), recorded per-execution/per-serial on the floor. Werco has free-text instructions + PDFs; nothing is captured. The single biggest AS9100D objective-evidence gap.
2. **AQL receiving inspection** — ANSI Z1.4 / ISO 2859-1 sampling tables, per-item sampling plans, plan snapshot frozen per lot (+ receipt-line lot/serial linking to make it work).
3. **Queue-aware scheduling** — deadline types (ASAP/Hard/Soft/None), priority-queue load calculation, load-balancing work-center selection, drag-and-drop dispatch boards.
4. **FEFO/pick-method policies + genealogy graph UI** — both largely ride on data Werco already has.
5. **PO revision/status history + outbound supplier RFQ → digital quote flow.**

### Quick wins (each roughly under a week)

| Item | Why | Werco today |
|---|---|---|
| PO revision + status history | Audit-trail gap: PO updates overwrite in place | `PurchaseOrder` has no `revision`/history table |
| FEFO/FIFO pick method policy | Shelf-life exposure (sealants, primers) | Backflush consumes lowest `InventoryItem.id` implicitly |
| Scrap reasons as master data | Extends the in-flight scrap-reason enforcement work | Hardcoded frontend string constants |
| Deadline-type enum on WOs | Priority that distinguishes hard vs. soft dates | Single 1–10 priority int |
| "Used-in" reverse-BOM tree | Change-impact analysis for ECOs | Part detail shows children only |
| Fix dormant webhook retries | `WebhookDelivery.max_attempts` / `next_retry_at` columns exist but **no code populates them** — single-attempt POST today | Latent bug found during verification |
| Account manager field on Customer | Ownership/routing | Not modeled |

---

## Theme 1 — Structured shop-floor data capture (highest value)

**Procedures / typed operation steps.** Carbon's `procedure` table is version-controlled (A/B/C, Draft/Active/Archived) work instructions composed of `procedureStep` rows with a type enum — *Value, Measurement (min/max enforced at entry), Checkbox, List, Timestamp, Person, File* — attached to operations (`jobOperationStep`, mirrored on methods and quotes). In their MES, operators see steps inline and record into `jobOperationStepRecord` (indexed, so serialized runs get per-unit records). Werco stores only free-text `setup_instructions`/`run_instructions` on `RoutingOperation`/`WorkOrderOperation` plus PDF documents viewed on the kiosk; out-of-tolerance can't be blocked at entry and nothing is recorded.

Riding along with it (all verified gaps):

- **Typed production quantities** — Carbon's `productionQuantity` table (type: Production/Scrap/Rework, `scrapReasonId` FK, notes, audit) with triggers rolling up `quantityComplete/Reworked/Scrapped` per operation. Supports rework-in-place vs. scrap-and-replace as distinct flows.
- **Scrap reason master data** — a `scrapReason` table per company instead of Werco's hardcoded frontend constants.
- **Floor-initiated NCRs** — reporting a quality issue on an operation auto-creates the NCR *and a linked inspection step* the crew executes before restart. Werco's kiosk HOLD (`work_order_blockers`) and `quality_issues` are unconnected; supervisors file NCRs after the fact.
- **Operation dependency DAG** — Carbon's `jobOperationDependency` table with DB-enforced cycle prevention and trigger-driven Waiting→Ready propagation models "assembly waits for 3 parallel subassemblies." Werco's `has_incomplete_predecessors()` supports linear sequences only.
- **Picking lists (missing entirely in Werco)** — assigned pick lists with per-line lot/serial selection, greedy lot recommendation, progress %, due dates, and a pick-schedule view. Werco is backflush-on-complete only; kitting is invisible, unassignable work.
- **MES offline resilience** — Carbon hydrates item/people stores from IndexedDB with realtime sync on top; Werco's kiosk polls every 10–15s and loses queue visibility when the network drops.

## Theme 2 — Quality: statistical inspection & configurable workflows

- **AQL sampling for receiving** (`apps/erp/app/modules/quality/samplingStandards.ts`): full ANSI/ASQ Z1.4 + ISO 2859-1 tables; per-item sampling plans with inspection level (I–III, S1–S4) and severity (Normal/Tightened/Reduced); sample size and accept/reject numbers computed per lot; the plan snapshot is frozen onto the received lot. Werco's `POReceipt.inspection_method` enum lists "sampling" with zero backing implementation.
- **Receipt-line tracked entities** — serials/batches created per receipt line with PO/supplier/cert metadata, which is what makes lot-based sampling and inbound inspection possible. Werco creates lots implicitly at backflush, not explicitly at receiving.
- **Inspection plans as first-class entities** — `inspectionDocument` + `inspectionFeature` (balloon number, nominal, ±tol, unit, type) with `inspectionBalloonAnchor` regions drawn on the drawing PDF. Werco's FAI exists but characteristics are free-form; `inspection_plan` is just a document type.
- **AI drawing ballooning** — Carbon's one real AI feature: GPT-4V extracts nominal/tol_plus/tol_minus/unit/type from cropped drawing regions into a strict schema (`inspectionBalloonAnalyze.server.ts`). Werco already has the stronger LLM plumbing (`run_llm_task`, model router, `allow_ai_egress` kill switch) — this is a natural extension for AS9102 FAI prep.
- **Configurable issue types & workflows** — custom `nonConformanceType` + `nonConformanceWorkflow` rows define required action types and approval requirements (MRB) per issue category. Typed action tasks (Containment / Corrective / Preventive / Verification / Communication) with sort order, due dates, and **supplier assignment** (`supplierId` on the action task → SCARs). Multi-item NCRs: one NCR spans multiple parts/lots via `nonConformanceItem`, down to specific tracked serials, with split/assign. Werco's NCR/CAR pipeline is solid but single-part, fixed-flow, with free-text `containment_action`/`corrective_action`/`preventive_action` fields.
- **11-value disposition enum** incl. Conditional Acceptance, Deviation Accepted, Quarantine, Repair — Werco has 6.
- **Risk register** (`riskRegister`: severity 1–5 × likelihood 1–5, Risk/Opportunity, tied to items/jobs/customers/suppliers/work centers) — analysts rated it lower-value, but note AS9100D §6.1 auditors ask for exactly this artifact.
- **Quality document library** — dedicated QualityDocument entities with Draft → Pending Approval → Active → Archived workflow and `ApprovalRequest` sign-off tracking (Werco's generic Document model covers part of this).

## Theme 3 — Scheduling: from load *chart* to load *queue*

Verified asymmetries (Carbon's engine lives in `packages/database/supabase/functions/lib/scheduling/`):

- **Queue-aware load** — Werco sums *all* non-complete operations per work center; Carbon computes "what's actually ahead of me at my priority" (`resource-manager.ts`), which is what makes promise dates honest.
- **Load-balancing work-center selector** — given a process, pick the least-loaded capable machine (3 CNC mills doing the same op). Werco's `_choose_best_work_center()` (routing_generation_service.py) matches by text name with no load input, and ops must be pre-assigned to a single WC.
- **Deadline types** — ASAP > Hard > Soft > No Deadline enum with conditional due-date validation, feeding priority; plus fractional priority insertion (drop a job between priorities 1 and 2 as 1.5 — no renumbering).
- **Dual boards** — drag-and-drop Kanban by work center (drag = reassign WC + reprioritize) and a jobs-by-due-date calendar board (drag = change due date and **re-run the scheduling engine**). Werco has a single Gantt-style table.
- Parity/ahead (rejected claims): Werco already matches Carbon on backward/forward calendar scheduling and **beats it on setup-time optimization** (Carbon has none).

## Theme 4 — Inventory & material flow

- **Pick-method policies** — per-item/location FEFO, FIFO, Oldest, Newest driving lot recommendations. *(Small effort; Werco has none.)*
- **Shelf-life modes** — NotManaged / Fixed Duration (from a trigger process) / Calculated (min of BOM component expiries) / Set on Receipt; validated to apply only to serial/batch-tracked items. Werco has an optional expiration date field with no policy or automation.
- **Genealogy graph** — Carbon models traceability as a directed graph (`trackedEntity` nodes, `trackedActivity` + input/output edges) with up/down traversal RPCs and a tree UI. **Werco's backend already returns `consumed_components` ancestry (`LotTraceResponse`), but `Traceability.tsx` renders only a flat timeline and never shows it** — the cheapest meaningful win in this document.
- **Kanban replenishment** — physical QR cards per item/location; scan triggers a PO (Buy) or job (Make), with auto-release options and printed card labels.
- **Storage rules** — rule-driven putaway (segregation, zone restrictions, bin-type matching). Werco receiving defaults to a hardcoded `RECV-01`.
- Stock/warehouse transfers with barcode scan flows (Werco: transfer transaction type exists; no scan workflow).

## Theme 5 — Purchasing & supplier collaboration

Werco handles *inbound* RFQs (customer → Werco, AI-parsed) but has no *outbound* flow:

- **Purchasing RFQ multi-supplier broadcast** — draft→ready→requested→closed, `purchasingRfqSupplier` join tracks who was asked.
- **Supplier share-link quote submission** — unauthenticated external form per supplier (`share+/supplier-quote.$id.tsx`) with access timestamps and audit; responses compared and finalized. (Apply the same CUI fail-closed gating as other egress paths.)
- **Supplier part master** — unit price + lead time + quantity price breaks per supplier+part. Werco's `SupplierPartMapping` has neither price nor lead time, forcing quote-time lookups.
- **PO `revisionId` + `purchaseOrderStatusHistory`** — Werco PO updates overwrite prior state (small effort, real AS9100D audit gap).
- **Supplier approval workflow** — Active/Inactive/Pending/Rejected with decision tracking, vs. Werco's `is_approved` boolean.
- **Multi-location suppliers and customers** with per-location contacts (Werco: single flattened address on both `Vendor` and `Customer`).
- Supplier KPI trends (PO/invoice/quote counts and amounts, period-over-period) on top of Werco's static scorecard snapshots.

## Theme 6 — Sales & pricing

- **Price lists with quantity breaks** — `customerItemPriceOverride(Break)`: per-customer or per-customer-type, validity dates, compose-with-rules flag.
- **Rules-based pricing engine** — `pricingRule` (discount/markup, % or fixed, scoped to items/posting groups/customer types/date ranges/qty bands, priority-ordered) with a `priceTrace` audit of which rule produced the price. Werco: manual `unit_price` + `markup_pct`.
- **Immutable quote revisions** — integer-sequenced revision rows; old revisions retained. **Werco hard-deletes quote lines on update (`cascade="all, delete-orphan"`) — Rev A→B line history is unrecoverable.**
- **Quote cost explorer** — tree UI expanding quote line → make/buy method → sub-materials with cost rollup ("which material drives cost?"). Werco shows line totals only.
- **Customer part-number mappings** (customer's PN ↔ internal PN), customer type/status master data, and **customer portal digital quote acceptance** via share link (high value, but gate behind CUI/egress policy — verifiers downgraded it to medium for that reason).

## Theme 7 — Engineering & item master

- **Material taxonomy** — material as substance + grade + finish + form/shape + dimensions (cascading pickers). Werco's material is a free-text string on Part and RFQ — no way to aggregate "all 6061-T6 bar" purchases.
- **BOM line effectivity** — `effectiveFrom/effectiveTo` per BOM line (build-date-dependent variants inside a revision). Werco has header-level `effective_date` only.
- **Item supersession** — successor part links with modes (Consume First / Prefer New / Stock Only / No Stock) and effectivity windows; Werco has a binary active/obsolete flag.
- **Product configurator** — typed configuration parameters + rules computing properties, driving parametric BOM/routing (large effort; relevant if configured product families emerge).
- **3D CAD** — STEP/IGES upload with Autodesk viewer + Onshape OAuth. Werco's DXF parser covers 2D laser nesting only.
- Replenishment classification (Buy/Make/Buy+Make) + four reorder policies feeding MRP (small effort on top of Werco's existing reorder-point fields).

## Theme 8 — Platform & integration surface

- **Scoped API keys** — per-module permission matrix stored as JSONB scopes, expiration, RLS-gated creation. Werco has no third-party API access at all.
- **OAuth 2.0 / MCP server** — RFC 8414 discovery endpoints; external tools (including Claude instances) can call Carbon as an MCP tool server. Strategically aligned with Werco's AI-forward track.
- **Webhook delivery with retries** — Inngest-backed, 3 retries, idempotency key per (table, record). *Werco's outbound webhooks make one attempt; the retry columns in `WebhookDelivery` are dead schema.*
- **Saved table views** — per-user/company-shared persisted column order/pinning/visibility/filters/sorts. Would slot naturally into the shared `<DataTable>` primitive.
- **Multi-channel notifications** — 27 event types → topic buckets → in-app + email + Slack with per-user destination prefs and digest mode. Werco is email-only.
- **Threshold-based approval rules** — per-document-type approval rules with amount thresholds (POs above $X need approval).

## Theme 9 — Financials (strategic decision, not a backlog item)

Werco has **zero invoicing/AR/AP/GL**; Carbon has all of it (sales/purchase invoices with 3-way match, payments with settlement and write-offs, credit/debit memos, AR/AP aging with GL tie-outs, manual journal entries with dimensions, fixed assets with MACRS). Building a GL is almost certainly wrong for Werco. The instructive pattern is Carbon's **"invoicing without accounting"** split (`BACKWARD_COMPATIBILITY.md`, added in the very latest commit): generate sales invoices from shipments *without* a GL, and sync to Xero/QuickBooks. If invoicing enters the roadmap, adopt that shape.

---

## Rejected claims — where Werco is ahead

Six analyst claims died in adversarial verification; two showed Werco ahead of Carbon:

| Claim | Verdict |
|---|---|
| "Carbon has setup-time optimization; Werco lacks it" | **Inverted — Werco has it, Carbon doesn't** |
| "Werco lacks backward/forward calendar scheduling" | False — parity |
| "Werco lacks KPI estimates-vs-actuals tracking" | False — Werco has it |
| "Werco lacks multi-level BOM scheduling" | False — different implementations, both work |
| Quote configurator w/ CAD extraction; production-event photo evidence | Overstated on the Carbon side |

Verified Werco advantages Carbon lacks: **hash-chained tamper-evident audit log** (Carbon's per-company audit tables have no integrity chain), **CUI egress kill-switch architecture / CMMC posture**, the **AI RFQ-parsing & quoting agent** (Carbon's only AI is the GPT-4V ballooning endpoint; its chat/agent scaffolding is dead code), **gauge calibration**, **training records/skill matrix**, **FAI module**, **customer complaints**, **supplier scorecards**, and the **kiosk crew-station / wallboard / visitor-station** family. Both systems independently chose ProxyBox for print bridging.

---

## Appendix A — Confirmed findings (107 gaps)

Of the 112 confirmed findings, the 107 where Werco is *missing* or *partial* are tabled below (the other 5 were verified "present" — approach-comparison notes, not gaps). Legend: **Werco** = Werco's state. **Value** = verified value to Werco (verifier-adjusted where a verification pass ran; analyst rating in parentheses when unverified). **Effort** = rough, against Werco's stack (— = not rated).

| # | Area | Feature (Carbon) | Werco | Value | Effort |
|---|---|---|---|---|---|
| 1 | MES / Shop Floor | Picking lists with lot/serial tracking | missing | high | large |
| 2 | MES / Shop Floor | Procedure/work instruction display per operation with inline step tracking | partial | high | large |
| 3 | MES / Shop Floor | Local-first data hydration (IndexedDB + realtime sync) for offline operation | missing | high | medium |
| 4 | MES / Shop Floor | Flexible step recording (Value, Measurement, Checkbox, List types) with min/max validation | missing | high | medium |
| 5 | MES / Shop Floor | Picking list status (draft, in-progress, completed) with line-item completion tracking | missing | high | large |
| 6 | MES / Shop Floor | Non-conformance quality issues auto-created from operation quality actions with rinse workflow | partial | high | large |
| 7 | MES / Shop Floor | Tracked entities (serial/batch genealogy) with consumed-input traceability | partial | medium | large |
| 8 | MES / Shop Floor | Attribute-based serial tracking with operation history per unit | missing | medium | medium |
| 9 | MES / Shop Floor | Suggestion/feedback modal on kiosk with timestamped operator notes + photos | missing | (low) | small |
| 10 | Production & Jobs | Procedures as Reusable Work Instructions with Typed Steps and Data Capture | missing | high | — |
| 11 | Production & Jobs | Maintenance Procedures Linked to Maintenance Dispatch with Execution Tracking | partial | high | — |
| 12 | Production & Jobs | Operation-Level Step Attributes with Real-Time Data Collection During Execution | missing | high | — |
| 13 | Production & Jobs | Job Operation Dependencies (Predecessor/Successor Graph) with Status Propagation | missing | high | — |
| 14 | Production & Jobs | Scrap Reason as Master Data with Categorization | partial | high | — |
| 15 | Production & Jobs | Job Status with Deadline Type and Due Date Scheduling | partial | high | — |
| 16 | Production & Jobs | Production Quantity Tracking with Type (Production/Scrap/Rework) | partial | high | — |
| 17 | Production & Jobs | Calendar-Based Schedule View with Kanban Drag-Drop for Date/Priority Assignment | partial | (low) | — |
| 18 | Production & Jobs | Maintenance Failure Mode with OEE Impact Classification | missing | (low) | — |
| 19 | Production & Jobs | Bulk Job Creation with Quantity Distribution and Due Date Spreading | missing | (low) | — |
| 20 | Quality | Inbound Inspection Sampling with ANSI Z1.4 / ISO 2859-1 Integration | missing | high | medium |
| 21 | Quality | Configurable Issue Types and Issue Workflows | partial | high | large |
| 22 | Quality | AI-Powered CAD Balloon Region Vision Analysis (GPT-4V) | missing | high | medium |
| 23 | Quality | Multi-Level Issue Task Hierarchy and Required Action System | partial | high | medium |
| 24 | Quality | Issue Multi-Item Association and Tracked Entity Linking | partial | high | large |
| 25 | Quality | Quality Document Library with Approval Workflow and Version Control | partial | high | medium |
| 26 | Quality | Inspection Plans as First-Class Entities with Question/Characteristic Sets | missing | high | large |
| 27 | Quality | Inline Issue Task and Reviewer Management with Auto-Start Workflow | partial | high | medium |
| 28 | Quality | Supplier-Linked Corrective Actions and External Process Mapping | partial | high | small |
| 29 | Quality | Risk Register (FMEA-style Risk/Opportunity Tracking) | missing | (low) | small |
| 30 | Quality | Gauge Calibration Management with Certificate Tracking | partial | (low) | small |
| 31 | Scheduling | Drag-and-drop Kanban board for operations scheduling | missing | high | medium |
| 32 | Scheduling | Deadline type priority (ASAP > Hard > Soft > No Deadline) with fractional priority insertion | partial | high | small |
| 33 | Scheduling | Work center load by priority (queue-aware scheduling) | missing | high | small |
| 34 | Scheduling | Gantt timeline view (dates.tsx) alongside Kanban (operations.tsx) | partial | high | medium |
| 35 | Scheduling | Work center selector with load balancing across parallel resources | missing | high | medium |
| 36 | Scheduling | Reshedule operation endpoint with backward/forward mode and direction toggle | partial | (low) | small |
| 37 | Scheduling | Multi-factor unit conversion for operation times (Hours/Piece, Pieces/Hour, Minutes/100Pieces, etc.) | partial | (low) | small |
| 38 | Scheduling | Conflict detection with conflict reason and blocking job info | partial | (low) | small |
| 39 | Scheduling | Filter + search on schedule board (work centers, tags, sales orders, assignees) | missing | (low) | small |
| 40 | Inventory & Logistics | Picking List Scheduling & Workload Distribution | missing | high | small |
| 41 | Inventory & Logistics | Lot/Serial Genealogy Graph Visualization | partial | high | large |
| 42 | Inventory & Logistics | Inbound Inspection with Barcode-Driven Sample Selection | partial | high | large |
| 43 | Inventory & Logistics | Kanban & Replenishment System Designation | missing | high | medium |
| 44 | Inventory & Logistics | Storage Rules & Smart Bin Assignment | missing | high | medium |
| 45 | Inventory & Logistics | Pick Method Sort Order (FEFO, FIFO, Oldest, Newest) | missing | high | small |
| 46 | Inventory & Logistics | Receipt Line Tracking (Serials/Batches during Inbound) | partial | high | medium |
| 47 | Inventory & Logistics | Stock Transfers with Barcode Scan Workflow | partial | medium | medium |
| 48 | Inventory & Logistics | Document-Driven Shipment Confirmation (BOL/Packing Slip Integration) | partial | medium | small |
| 49 | Inventory & Logistics | Traceability Search and Expansion UI (Advanced Filtering & Graph Queries) | partial | (medium) | medium |
| 50 | Inventory & Logistics | Warehouse Transfers (Multi-Location Inventory Moves) | missing | (low) | medium |
| 51 | Purchasing & Suppliers | Supplier Locations and Contacts (multi-location supplier model) | missing | high | medium |
| 52 | Purchasing & Suppliers | Supplier Quotes with External Sharing & Digital Submission | missing | high | large |
| 53 | Purchasing & Suppliers | Purchasing RFQ with Multi-Supplier Broadcast | partial | high | large |
| 54 | Purchasing & Suppliers | Supplier Performance KPIs with Trend Analytics | partial | high | medium |
| 55 | Purchasing & Suppliers | Purchase Order Revision Tracking with Status History | missing | high | small |
| 56 | Purchasing & Suppliers | Supplier-side Digital Quote Form Submission (via Share Link) | missing | high | large |
| 57 | Purchasing & Suppliers | Supplier Part Master with Lead Times and Price Breaks | partial | high | medium |
| 58 | Purchasing & Suppliers | Supplier Approval Workflow with Status Gating | partial | high | medium |
| 59 | Purchasing & Suppliers | Planned Orders with Reorder Policies | partial | medium | medium |
| 60 | Purchasing & Suppliers | Purchase Order Drop-ship (Direct-to-Customer) with Customer Location Override | missing | medium | medium |
| 61 | Purchasing & Suppliers | Purchase Order Email Notifications & Supplier Contact Selection | missing | (medium) | small |
| 62 | Purchasing & Suppliers | Purchase Order Line Types (Part, Material, Tool, Service, G/L Account, Fixed Asset, Consumable, Fixture, Comment) | missing | (low) | small |
| 63 | Sales & Quoting | Flexible customer-type and customer-status master data | missing | high | medium |
| 64 | Sales & Quoting | Quantity-break pricing overrides (price lists) | missing | high | large |
| 65 | Sales & Quoting | Rules-based dynamic pricing (discount & markup engine) | missing | high | large |
| 66 | Sales & Quoting | Quote revisions with immutable audit trail | partial | high | medium |
| 67 | Sales & Quoting | Quote explorer & bill-of-material ancestry for costing transparency | partial | high | medium |
| 68 | Sales & Quoting | Customer locations and shipping/billing address variants | partial | high | medium |
| 69 | Sales & Quoting | Customer portal with digital quote acceptance & rejection | missing | medium | large |
| 70 | Sales & Quoting | Account manager assignment & routing on customer | missing | medium | small |
| 71 | Sales & Quoting | Quote-to-order conversion with partial line acceptance | partial | (low) | small |
| 72 | Sales & Quoting | No-quote reasons tracking | missing | (low) | small |
| 73 | Items & Engineering | Material taxonomy with substance/grade/finish/dimension hierarchy | missing | high | large |
| 74 | Items & Engineering | Configurable items with parametric BoM/routing (product configurator) | missing | high | large |
| 75 | Items & Engineering | Shelf-life management with trigger-based expiration (NotManaged, Fixed Duration, Calculated, Set on Receipt) | partial | high | medium |
| 76 | Items & Engineering | Item supersession/discontinuation tracking (part lifecycle) | missing | high | medium |
| 77 | Items & Engineering | BoM line effectivity (date-range or serial-range applicability) | partial | high | medium |
| 78 | Items & Engineering | 3D CAD model upload & Onshape OAuth integration | missing | high | medium |
| 79 | Items & Engineering | Item reordering policies & replenishment systems (Buy, Make, Buy+Make) | partial | high | small |
| 80 | Items & Engineering | BoM 'Used In' tree & reverse traceability | missing | high | small |
| 81 | Items & Engineering | Customer part mappings (customer sees part as different PN) | partial | high | medium |
| 82 | Items & Engineering | BoM & Routing copy/clone with selective element inheritance | missing | medium | medium |
| 83 | Items & Engineering | Item tracking types with Batch/Serial enforcement (inventory model) | partial | (low) | small |
| 84 | Items & Engineering | Item costing methods (Standard, Average, FIFO, LIFO) | partial | (low) | small |
| 85 | Platform | OAuth 2.0 public API for third-party integrations (MCP discovery + authorization server) | missing | high | medium |
| 86 | Platform | Scoped API keys with permission matrix and expiration | missing | high | large |
| 87 | Platform | Saved views (smart views) with column pinning, sorting, filtering, and visibility state | missing | high | medium |
| 88 | Platform | Event-driven notifications with multi-channel fan-out (in-app + email + Slack) and digest mode | partial | high | large |
| 89 | Platform | Webhook event subscriptions with delivery logging and retry orchestration | partial | high | medium |
| 90 | Platform | Settings UI with approval rules, multi-company company management, and feature plan gating | partial | high | medium |
| 91 | Platform | Account/user attribute system with custom profile fields and preference schema | partial | medium | medium |
| 92 | Platform | Bulk user provisioning and deprovisioning with role assignment matrix | partial | medium | medium |
| 93 | Platform | Audit log with event-specific enrichment and contextual link generation for notifications | partial | medium | medium |
| 94 | Platform | Plan-gated feature toggles (require subscription tier for webhooks, API keys, integrations, advanced reports) | missing | (low) | large |
| 95 | Accounting & Invoicing | General Ledger (Chart of Accounts, Posting Groups, Account Hierarchy) | missing | high | — |
| 96 | Accounting & Invoicing | Sales Invoicing (AR, Invoice Header/Lines, Line Types, Status Workflow, Shipment Link) | missing | high | — |
| 97 | Accounting & Invoicing | Purchase Invoicing (AP, Invoice Header/Lines, Line Types, PO Link, Receiving GR/IR) | missing | high | — |
| 98 | Accounting & Invoicing | Payments & Collections (AR Receipts, AP Disbursements, Payment Applications, Settlement, Dust Forgiveness) | missing | high | — |
| 99 | Accounting & Invoicing | Credit Memos & Debit Memos (AR/AP adjustments, Memo Status, Partial Application) | missing | high | — |
| 100 | Accounting & Invoicing | Journal Entry Manual Entry (GL posting, Dimensions, Multi-line, Draft/Posted/Reversed Status) | missing | high | — |
| 101 | Accounting & Invoicing | AR/AP Aging Reports & Tie-Outs (Aging buckets, Open by party, GL reconciliation) | missing | high | — |
| 102 | Accounting & Invoicing | Dimensions & Cost Allocation (Cost Centers, Customer, Item, Supplier, Location, Custom Dimensions) | partial | high | — |
| 103 | Accounting & Invoicing | Fixed Assets Lifecycle (Classes, Acquisition, Depreciation Methods, Disposal, Book/Tax Tracking, MACRS) | missing | medium | — |
| 104 | Accounting & Invoicing | GL Posting Groups (Inventory, AR, AP, Revenue, COGS, Variance Account Mapping) | missing | medium | — |
| 105 | Accounting & Invoicing | Intercompany Transactions & Consolidation (IC matching, elimination entity, consolidated balance sheet) | missing | (low) | — |
| 106 | Accounting & Invoicing | Payment Terms & Early-Pay Discounts (Net days, discount %, calculation method) | partial | (low) | — |
| 107 | Accounting & Invoicing | Currency & Exchange Rates (Multi-currency, Historical Rates, Translation, Realized/Unrealized Gains) | missing | (low) | — |

---

## Appendix B — Method

Multi-agent workflow (105 agents, ~3,700 tool calls): (1) parallel feature inventories of Werco (61 routers, 52 models, pages, docs) and Carbon (apps/erp, apps/mes, 20 packages, migrations, `.ai/rules` docs); (2) ten domain deep-dive agents reading Carbon's code and grepping Werco for equivalents; (3) every missing/partial finding rated medium+ was independently re-verified by an adversarial agent instructed to *refute* it against both codebases; (4) a completeness critic swept Carbon's route groups/packages for uncovered areas (none found). Carbon's in-repo `.ai/rules/*.md` module docs are dense and accurate — re-clone the repo and start there when revisiting any feature in this document.
