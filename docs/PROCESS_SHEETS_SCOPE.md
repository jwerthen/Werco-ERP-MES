# Process Sheets — Scope & Implementation Plan

**Date:** 2026-07-04 · **Status:** **FEATURE COMPLETE** — all four PRs merged: [#81](https://github.com/jwerthen/Werco-ERP-MES/pull/81), [#82](https://github.com/jwerthen/Werco-ERP-MES/pull/82), [#83](https://github.com/jwerthen/Werco-ERP-MES/pull/83), [#84](https://github.com/jwerthen/Werco-ERP-MES/pull/84) (2026-07-04 → 2026-07-07). Open follow-ups live in the deferred ledger below
**Feature:** Typed, revision-controlled operation steps ("Process Sheets") authored in engineering, snapshotted onto work orders, and executed on the shop-floor kiosk with per-step data capture (measurements with tolerance enforcement, checkboxes, lists, photo/file evidence).

## Context

Identified as the #1 adoption candidate in [CARBON_GAP_ANALYSIS.md](CARBON_GAP_ANALYSIS.md): Werco today stores work instructions as free-text (`setup_instructions` / `run_instructions` on routing/WO operations) plus PDF documents — nothing is captured at execution. For AS9100D this is the gap between "the traveler said to check it" and *recorded objective evidence per step, per unit, with out-of-tolerance blocked at entry*.

**Clean-room note:** the capability was identified by studying Carbon ERP (AGPL). This design is Werco's own — grounded in Werco's existing models, conventions, and quality modules. No Carbon code, schema names, or UI is to be copied. Deliberate divergences are listed in [How this stays ours](#how-this-stays-ours).

## Product decisions (settled)

| Decision | Choice |
|---|---|
| Name | **Process Sheets** (`process_sheets` / `process_sheet_steps` / `operation_step_records`) — classic aerospace planning-sheet vocabulary; no collision with the existing `work_instruction` document type |
| Structure | **Reusable library** — standalone revision-controlled entities, attached by reference to routing operations, snapshotted onto WO operations at WO creation |
| Out-of-tolerance | **Blocks recording as passed.** The only paths forward are hold + NCR, or a corrected re-measurement. Matches the existing convention that server-gated actions stay non-optimistic |
| Per-unit capture | v1 keys records by `serial_number` string validated against `WorkOrder.serial_numbers` (no new serial-unit table yet — see Deferred) |
| Operator qualification | **Warn-and-record**, not block — matches the existing `evaluate_operator_qualification()` posture (`services/operator_qualification_service.py`) |
| Lifecycle | draft → released → obsolete with revision strings (`A`, `B`, …) — identical vocabulary to `Routing` (`models/routing.py`), so engineering learns nothing new |

## Data model (migration `058_process_sheets`)

All tables: `TenantMixin` (non-null `company_id` + index), created/updated audit columns per house pattern. Register every model in `app/models/__init__.py` + `__all__` (Alembic autogenerate requirement).

### `process_sheets` — the library entity
`SoftDeleteMixin`, `OptimisticLockMixin`. Mirrors `Routing`'s lifecycle exactly:
- `sheet_number` (unique per company, auto `PS-000123`), `title`, `description`
- `revision` String(20) default `'A'`; `status` draft/released/obsolete; `effective_date`, `obsolete_date`; `is_active`
- Revisions are separate rows sharing `sheet_number` (same pattern as routing revisions — no separate revision table)

### `process_sheet_steps` — typed step definitions
- `process_sheet_id` FK, `sequence` (10/20/30 like operations), `label`, `instruction_text`
- `step_type` str-enum co-located with model: `MEASUREMENT | CHECKBOX | LIST | VALUE | PHOTO | FILE | INSTRUCTION` (INSTRUCTION = display-only, no record required)
- `is_required` Boolean (gates operation completion)
- `config` JSON — per type: measurement `{nominal, lsl, usl, unit, decimals}`; list `{options: []}`; photo/file `{hint}`
- `requires_gauge` Boolean — measurement steps only (see Integrations)
- `spc_characteristic_id` nullable FK → `spc_characteristics` — "feeds SPC" wiring
- No PERSON step type: recorder identity comes free from badge-scoped crew-station attribution

### `wo_operation_steps` — immutable snapshot on the traveler
Copied from the released sheet at **WO creation** inside `create_routing_operations_for_work_order()` ([work_orders.py:1454](../backend/app/api/endpoints/work_orders.py)) — the same moment routing operations are copied, preserving the existing invariant that routing changes never mutate open WOs. Columns = step definition columns + `work_order_operation_id` FK + `source_sheet_id`/`source_sheet_revision` (traceability back to the released sheet).

### `operation_step_records` — append-only captured evidence
- `wo_operation_step_id` FK, `work_order_operation_id` FK (denormalized for cheap gating queries)
- `serial_number` nullable String(100) — required when the WO carries serials; validated against the WO's `serial_numbers` JSON array
- `value_text` / `value_numeric` / `value_bool` (one populated per step type), `is_conforming` Boolean (server-computed for measurements)
- `recorded_by` user FK, `recorded_at` (UTC), `source` (KIOSK/DESKTOP — same adoption-telemetry channel enum as `TimeEntry.source`)
- `equipment_id` nullable FK → `equipment` (gauge used), `qualification_snapshot` JSON (warn-and-record cert/skill result at capture time)
- `attachment_document_id` nullable FK → `documents` (photo/file evidence via existing `StorageBackend`)
- `superseded_by_id` nullable self-FK + `supersede_reason` — **corrections are new records**, never updates/deletes (append-only; no soft-delete needed; satisfies the traceability invariant)

### Two small column additions (same migration)
- `spc_measurements.operation_id` nullable FK — step-level SPC traceability (recon confirmed `SPCMeasurement` already carries `work_order_id`/`lot_number`/`serial_number`/`measured_by`)
- `work_order_blockers.ncr_id` nullable FK — lets a QUALITY_HOLD blocker reference the NCR it was raised with (recon: the link is "cultural" today)

Migration follows the `057_kiosk_stations` precedent: idempotent (`_has_table()`/`_has_index()` guards), real `downgrade()` in reverse order.

## Backend API

### Engineering CRUD — new router `api/endpoints/process_sheets.py`, mounted `/api/v1/process-sheets`
Thin router → new `services/process_sheet_service.py`. All queries via `tenant_query()`; all writes audited via `get_audit_service` (`log_create` / `log_update` / `log_status_change` before commit, per the work_orders.py pattern).
- `GET /` list (status/search filters) · `GET /{id}` with steps · `POST /` · `PATCH /{id}` (draft-only; 409 on released) · step CRUD (draft-only)
- `POST /{id}/release` · `POST /{id}/obsolete` · `POST /{id}/new-revision` (copies steps, bumps revision, new row starts draft)
- Roles: author/edit `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])`; release/obsolete `require_role([ADMIN, MANAGER, QUALITY])` — quality owns released inspection documents
- Attach: `routing_operations.process_sheet_id` nullable FK, editable through the existing routing operation endpoints (released sheets only)

### Shop-floor execution — extend `api/endpoints/shop_floor.py` (kiosk tokens are fenced to `/api/v1/shop-floor` prefixes in [deps.py](../backend/app/api/deps.py); placing these here means **zero fence changes**)
- `GET /shop-floor/operations/{id}/steps` — snapshot steps + records (+ per-serial completeness map); joins the existing work-center queue payload so the kiosk shows a steps chip without an extra round-trip
- `POST /shop-floor/operations/{id}/steps/{step_id}/records` — the capture endpoint. Server-side, in order:
  1. WO not terminal, operation IN_PROGRESS (mirrors existing complete-endpoint predicates, same 409/400 detail shapes)
  2. serial required/valid when WO is serialized
  3. type-shaped value validation; measurements: compute `is_conforming` from snapshot lsl/usl — **out-of-tolerance → 409 with `{detail, code: "OUT_OF_TOLERANCE", measured, lsl, usl}`**, no record row
  4. `requires_gauge`: `equipment_id` mandatory; gauge must be `status == ACTIVE and next_calibration_date >= today` (recon: caller-implemented check) — else 409 `GAUGE_OUT_OF_CAL`
  5. qualification snapshot (warn-and-record), audit `log_create(resource_type='operation_step_record', ...)`, optional SPC insert when `spc_characteristic_id` set
- `POST .../records/{record_id}/supersede` — correction path (reason required, audited)
- `POST /shop-floor/operations/{id}/quality-hold` — OOT escape hatch: creates NCR (`source=IN_PROCESS`, pre-filled `specification`/`actual_value`/`required_value` from the step config + attempted value, `work_order_id`, lot/serial) + QUALITY_HOLD `WorkOrderBlocker` with the new `ncr_id` FK, flips operation ON_HOLD. Reuses existing NCR creation service.

### Completion gating — one edit in the existing complete endpoint ([shop_floor.py:2543](../backend/app/api/endpoints/shop_floor.py))
After the existing predecessor check, inside the existing `SELECT...FOR UPDATE` block: every `is_required` snapshot step needs a non-superseded conforming record — per serial when serialized. Failure → 409 `{code: "STEPS_INCOMPLETE", missing: [{step, serials}]}`. Non-optimistic by design.

## Frontend

### Engineering — new page `pages/ProcessSheets.tsx` (route `/process-sheets`)
- Nav: Engineering section in `Layout.tsx` `navSections` (sibling of Routing); title + breadcrumb in `utils/routeMeta.ts`
- List: shared `<DataTable>` (client sort/paginate/CSV like WorkOrders); `<StatusBadge>` via the canonical `statusColors` map (draft=amber, released=green, obsolete=slate — same as routing)
- Editor: detail panel + step editor `<Modal>` (per-type config fields via `<FormField>` render-prop wiring, RHF+Zod schema in `validation/`), `useUnsavedChanges(isDirty)`, `<LoadingButton>` on release/new-revision, `useToast()` + `<ErrorState>`/`<EmptyState>` throughout. Instrument-panel chrome: `bg-fd-panel`, hairline borders, sharp corners — match Routing.tsx, not the mockup's host styling
- `Routing.tsx` operation modal gains a "Process sheet" `<SelectField>` (released sheets only) with a link-out to the sheet

### Kiosk — new `steps` view state in both kiosks
- `OperatorKiosk.tsx`: extend the `KioskView` union (`queue|confirm|production|complete|hold`) with `steps`; entry chip on the job card ("Steps 2/6") once `GET .../steps` data is in the queue payload
- `CrewStationKiosk.tsx`: same via the `CrewView` union; records attribute to the badge-minted operator token identity (crew attribution for free); reuse the `generationRef` stale-poll guard for the steps list
- Step list mirrors the approved mockup (typed rows, live tolerance feedback, record trail, per-serial selector for serialized WOs, Central-time display via `formatCentralDateTime`)
- Offline: steps render read-only from last poll; record buttons respect the existing `mutationsBlocked` hard-disable — no queued/optimistic writes
- Photo capture: `<input type="file" accept="image/*" capture="environment">` (no component exists today — new small `KioskPhotoInput`, validation logic borrowed from `POUpload.tsx`), upload through the existing documents upload → link `attachment_document_id`
- API calls through `kioskStationClient` operator-token headers (existing pattern); desktop WO detail page gets a read-only "Process steps" records panel

## Integrations (built-in, not bolted on — these are the Werco differentiators)

All six shipped (PR 4 closed the set; "Shipped as" names the as-built mechanism in `services/process_sheet_service.py` unless pathed otherwise):

| Integration | Mechanism | Exists today | Shipped as |
|---|---|---|---|
| SPC | step `spc_characteristic_id` → auto `SPCMeasurement` row (`operation_id`, serial, measured_by) on every **conforming** record — a supersede inserts a NEW point (SPC sees the time series); refused OOT values never land; a since-deleted characteristic degrades to an audit note, never fails the record | `models/spc.py`, `POST /spc/measurements` | ✅ `_feed_spc_measurement` |
| Gauge calibration | `requires_gauge` steps validate `Equipment` calibration currency at capture (scan/type `equipment_code`; checked BEFORE tolerance; fail-closed — no due date fails; 409 `GAUGE_OUT_OF_CAL`); gauge identity stored on the record and echoed as `gauge` | `models/calibration.py` | ✅ `_validate_gauge` |
| NCR + hold | OOT → one-tap NCR (`IN_PROCESS`, pre-filled spec/actual/required) + QUALITY_HOLD blocker with `ncr_id` FK, op ON_HOLD, open time entries closed | `models/quality.py`, `work_order_blockers` | ✅ `create_quality_hold` (`POST /shop-floor/operations/{id}/steps/{step_id}/quality-hold`) |
| Operator quals | `qualification_snapshot` on every record (warn-and-record; unqualified exceptions echoed onto the audit row) | `operator_qualification_service.py` | ✅ `build_qualification_snapshot` |
| FAI | measurement records pre-fill `FAICharacteristic.actual_value`/`measuring_device` for AS9102 (label-match heuristic; never overwrites, never sets `is_conforming`; ambiguous/contradicting specs reported `unmatched`) | `FirstArticleInspection` models | ✅ `prefill_fai_from_step_records` (`POST /quality/fai/{fai_id}/prefill-from-steps`) |
| Audit | every create/status-change/supersede through `AuditService` → hash-chained log | `services/audit_service.py` | ✅ shipped since PR 1/3 |

## Compliance checklist (for compliance-auditor review)

- Tenant isolation: every query `tenant_query()`/`tenant_filter()`; snapshot copies carry `company_id`
- Audit: sheet lifecycle + every record + every supersede logged; no direct `audit_log` writes
- Records append-only (supersede, never mutate) → AS9100D evidence integrity; sheets soft-delete only
- RBAC per above; kiosk-scoped tokens reach only the `/shop-floor` read+record endpoints via the existing fence
- No new egress paths (fully on-platform; photo storage via existing StorageBackend)
- UTC in, `Z` out (`UTCModel` response schemas), Central display on all timestamps

## Testing (test-engineer gate)

- **pytest:** service + endpoint tests — lifecycle (draft-edit-only, release, revision copy), snapshot-at-WO-creation, record validation matrix per step type, OOT 409, gauge-out-of-cal 409, serial validation, supersede chain, completion gating incl. per-serial + concurrent-completer (FOR UPDATE) cases, kiosk-token fence access, tenant isolation, audit rows emitted
- **Jest/RTL:** ProcessSheets page (list/editor/validation), Routing attach control, kiosk steps view in both kiosks following `OperatorKiosk.test.tsx` patterns (mock `kioskStationClient`, offline disable, OOT flow, toast assertions)
- **Playwright:** one E2E smoke — author sheet → attach → create WO → record steps on kiosk → complete op

## Documentation (documentation-engineer gate)

`docs/KIOSK.md` (steps flow, offline behavior), `docs/API.md`, `docs/RBAC_PERMISSIONS.md` (release roles), CLAUDE.md docs index pointer, this file's status line.

## Phases / PR breakdown

| # | PR | Contents | Size |
|---|---|---|---|
| 1 | Schema + library backend | migration 058, models, process-sheets router/service, routing attach, audit, pytest | M |
| 2 | Engineering UI | ProcessSheets page, step editor, Routing attach, nav/routeMeta, Jest | M |
| 3 | Snapshot + capture + gating | WO-creation snapshot, shop-floor steps/record/supersede endpoints, completion gate, kiosk steps UI (both kiosks), photo capture, Jest + pytest | L |
| 4 | Integrations | SPC insert, gauge validation, OOT→NCR+hold, qualification snapshot, FAI pre-fill, Playwright smoke | M |

Each phase lands independently shippable behind the natural gate (sheets are inert until attached; attached sheets are inert until snapshot code ships). Standard gates per CLAUDE.md: code-reviewer + compliance-auditor (data/auth surface) on every phase; test-engineer + documentation-engineer before done.

### Completion-path postures (settled 2026-07-06, per PR 3 compliance audit)

The required-steps gate covers **every** path that flips an operation COMPLETE, with one deliberate override:

| Path | Posture |
|---|---|
| Shop-floor `POST /shop-floor/operations/{id}/complete` | Gated — 409 `STEPS_INCOMPLETE` |
| Office `POST /work-orders/operations/{id}/complete` | Gated — identical 409 (parity tested both directions) |
| Kiosk clock-out quantity rollup reaching target | Gated — the TimeEntry **always closes normally** (labor truth; G6-A never-trap precedent); the operation stays IN_PROGRESS at target and the response carries a `steps_incomplete` warning block |
| Read-time evidence reconcile (`reconcile_work_orders_from_completion_evidence`) | Gated — quantities reconcile, COMPLETE flip withheld while required steps are missing |
| WO-level `POST /work-orders/{id}/complete` (ADMIN/MANAGER/SUPERVISOR/QUALITY) | **Deliberate audited evidence-override** (jwerthen 2026-07-06): force-complete proceeds; the audit row records `steps_bypassed` count + details and the response carries a bypass summary. This is the sanctioned close-out for legacy/paper-evidenced/MRB-decided jobs |
| Excel migration import | Paper-evidenced cutover: imported COMPLETE operations carry no step records **by design** — evidence for migration-era WOs lives in the paper system of record |

Evidence attachments: `attachment_document_id` must reference a `QUALITY_RECORD` Document belonging to the operation's WO (exactly what the in-fence step-attachment endpoint produces) — anything else is 400. Kiosk tokens upload via `/shop-floor/.../attachment`; `/documents/upload` remains fenced off.

### Open decisions carried forward (surfaced in PR 1 review)

- **PR 2 (product/UX) — SETTLED 2026-07-06:** the release dialog detects a still-released prior revision and shows a **pre-checked "Obsolete Rev X" option** — one click releases B and obsoletes A (sequenced calls, non-optimistic); unchecking allows a deliberate transition period with both released. Backend unchanged (jwerthen decision).
- **PR 3 (snapshot semantics) — SETTLED 2026-07-06 (jwerthen):** at WO creation the snapshot **resolves the attached sheet's family (`sheet_number`) to its currently-RELEASED revision** — so releasing Rev B with obsolete-prior flows to future WOs without re-attaching routings; `wo_operation_steps.source_sheet_id/revision` records exactly what was snapshotted. If the family has **no released revision** (all obsolete, or soft-deleted), **WO creation is blocked with a 409** naming the operation and sheet (fix: release a revision or detach). Never snapshot obsolete content; never silently skip.

## How this stays ours

- **Vocabulary:** Process Sheets / step records — aerospace planning-sheet language, not Carbon's "procedures"
- **Lifecycle reuse:** draft/released/obsolete + revision strings copied from Werco's own `Routing`, not Carbon's item-row revision model
- **Snapshot point:** WO creation (Werco's existing traveler invariant), enforced in the same function that copies operations today
- **Differentiators Carbon doesn't have:** gauge-calibration enforcement at capture, operator-qualification snapshots, hash-chained audit on every record, crew-station multi-operator attribution, SPC feed into an existing SPC module, OOT→NCR with pre-filled spec/actual values
- **Deliberate omissions:** no PERSON step type (badge attribution makes it redundant); no DB triggers for status propagation (Werco keeps state transitions in services); no configurable per-type workflows in v1
- **UI:** Werco instrument-panel design system and shared primitives throughout — nothing visually derived from Carbon

## Deferred (recorded, not forgotten)

- First-class `wo_serial_units` table (would let TimeEntry/NCR/FAI also key per unit — larger cross-cutting refactor; v1's `serial_number` string columns migrate cleanly into it later)
- Realtime step-progress pushes to wallboard/dashboard (`broadcast_to_company` pattern exists; add once adoption proves demand)
- Reusable sheet *sections* / includes, conditional steps, and per-type approval workflows
- AI-assisted sheet drafting from drawing PDFs (natural `run_llm_task` extension; pairs with the AI-ballooning gap-analysis candidate)
- ~~Authoring guard: measurement `decimals` must be fine enough to resolve the lsl/usl band~~ **Closed in PR 4** — `_validate_step_definition` rejects a `decimals` too coarse to resolve the band (requires 10^−decimals ≤ usl−lsl)
- Upload hardening: magic-byte sniffing + streaming size checks on evidence uploads (current posture matches `/documents/upload` — client-declared MIME, buffered read)
- ~~`serial_numbers` not settable at WO creation~~ **Closed in PR 4** — `WorkOrderCreate.serial_numbers` (validated: unique, non-empty, ≤100 chars, count == `quantity_ordered`) stored to the existing JSON-in-Text column, plus a one-per-line serials field on WorkOrderNew with mirrored client-side validation; serialized capture is now reachable end-to-end from the office UI
- PR 3 code-review follow-ups (verdict "ready", none blocking) — **closed in PR 4:** compute `resolve_absolute_operation_quantity` once per completion (both twins — TOCTOU closed, duplicate evidence query gone); extract the 4x-duplicated document-number generator into one helper (`services/document_numbering.py`, advisory-locked, shared rollover quirk); point `coc_service._parse_serial_numbers` at the shared serial parser (`process_sheet_service.parse_serial_numbers`). **Still open:** cache the uploaded `document_id` in KioskStepsPanel so a failed record-create retry doesn't mint duplicate evidence Documents; re-intersect the OOT refusal strip's serials with live `missing_serials`
- PR 3 re-audit notes deferred to PR 4: (a) **closed** — `_copy_slot_completion_evidence` now skips step-gated target ops (regenerated op rows sharing a progress key); (b) **closed** — office `complete_operation` 404s on a soft-deleted/missing parent WO like the shop-floor twin; (c) **closed** — step-record `source` now follows TimeEntry's client-hint trust model (the single-operator kiosk sends `source="kiosk"` like clock-in; badge-minted kiosk tokens stay server-authoritative and always record `kiosk`); (d) **stays as designed** — evidence provenance remains "QUALITY_RECORD on this WO", not "minted by the step endpoint" (tightening would need a step-linkage column on Document)
- `docs/API.md` reference entries for the PR 3/4 shop-floor and quality endpoints (steps view / record / supersede / attachment, quality-hold, FAI pre-fill) — tracked as a separate docs follow-up
