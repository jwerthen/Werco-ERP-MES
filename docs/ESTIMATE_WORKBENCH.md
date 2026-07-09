# Estimate Workbench — Integration Contract

Excel-replacement estimating for Werco sheet-metal / weldment / machined quotes,
built **inside** Werco-ERP-MES (not a standalone app).

## Goals

- Thickness-banded machine-physics lookups (round-down, never round-up)
- Four cost buckets per fab line: material / laser / brake / weld (partial scope OK)
- Always surface **$ and hours** for labor-driven buckets
- Confidence / verification gate before finalize
- Convert finalized estimate → existing `Quote` → `WorkOrder`
- Multi-user via ERP JWT / RBAC / `company_id` / optimistic locking / `AuditService`

## Entity mapping

| Workbench concept | ERP home |
|-------------------|----------|
| RFQ / job package | `RfqPackage` |
| Internal estimate | `QuoteEstimate` (+ new child tables) |
| Assembly | `QuoteAssembly` (new) |
| Fab line item | `QuoteFabLineItem` (new) |
| Buyout line item | `QuoteBuyoutLineItem` (new) |
| Machined part | `QuoteMachinedLineItem` (new) |
| Customer-facing bid | `Quote` + `QuoteLine` |
| Shop rates | `QuoteSettings` + `LaborRate` + `QuoteMachine` |
| Material $/lb | `QuoteMaterial` |
| Cut / bend / gauge / weld tables | `CutBendTable` + `CutBendRow` (new) |
| Table edit history | `SettingsAuditLog` (existing) |
| Price / rate freeze on finalize | `PriceSnapshot` + `QuoteEstimate.internal_breakdown` |

## Lifecycle

```
Draft workbench  →  Needs Review (any REVIEW confidence)
                 →  Ready to Send (no reds)
                 →  Finalize → Quote (rate snapshot frozen)
                 →  Accept → Convert → WorkOrder
                 →  Job complete → estimate_calibration learner → Shop Data tune
```

## Calc engine contract

Pure functions in `backend/app/services/fab_calc_engine.py`.

- **No** SQLAlchemy / FastAPI / React imports
- All rates and table rows passed in as arguments
- Banded lookup: largest `thickness <= input`; below first band → fallback
- Past-capacity cells (`None`) → typed error, never silent $0
- Blank optional geometry with op in scope → $0 for that bucket (partial quote)
- Sell price: `cost / (1 - target_margin)` (margin on sell, not markup on cost)

## API surface (Phase 0+)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/estimate-workbench/recalc` | Stateless recalc from payload + **company** rates/tables |
| `POST` | `/api/v1/estimate-workbench/` | Create blank workbench on an RFQ package |
| `GET`  | `/api/v1/estimate-workbench/{estimate_id}` | Load workbench tree |
| `PUT`  | `/api/v1/estimate-workbench/{estimate_id}` | Save tree with optimistic `version` |
| `GET`  | `/api/v1/estimate-workbench/{estimate_id}/verification` | Bid Verification dashboard |
| `POST` | `/api/v1/estimate-workbench/{estimate_id}/finalize` | Gate on REVIEW; create Quote + freeze rate snapshot |
| `POST` | `/api/v1/estimate-workbench/{estimate_id}/extract-from-rfq` | Triple-pass PDF/BOM extract → staging draft (optional apply) |
| `GET`  | `/api/v1/estimate-workbench/shop-data` | List 5 Cut/Bend tables (auto-seed) |
| `PATCH`| `/api/v1/estimate-workbench/shop-data/{kind}/rows/{id}` | Edit cell + required note → `SettingsAuditLog` |
| `POST` | `/api/v1/estimate-workbench/shop-data/{kind}/rows` | Add thickness band (auto-sort) |
| `GET`  | `/api/v1/estimate-workbench/shop-data/history` | Cut/Bend change history |
| `GET`/`POST` | `/api/v1/estimate-workbench/job-actuals` | Quoted vs actual hours + tune hints |
| `GET`  | `/api/v1/estimate-workbench/{estimate_id}/export/audit.xlsx` | Internal audit Excel |
| `GET`  | `/api/v1/estimate-workbench/{estimate_id}/export/audit.json` | Internal audit JSON |
| `GET`  | `/api/v1/estimate-workbench/{estimate_id}/export/customer.pdf` | Customer PDF (no rates; Review gate) |

## Non-negotiables

1. Never hardcode rates/lookup values in formulas — always from DB / passed args
2. Approximate-match round-down banded lookups
3. Never silently invent a number — Placeholder / Review + note
4. Partial scope allowed (ops toggles or blank geometry → $0)
5. Always show $ and hours for labor buckets
6. Tenant-scoped; state changes through `AuditService`
7. Finalize freezes rate/table snapshot so later Shop Data edits don't rewrite sent bids

## Phases

| Phase | Deliverable |
|-------|-------------|
| **0** | Contract + `fab_calc_engine` + models/migration + seed + `/recalc` stub | ✅ |
| **1** | Persist workbench tree; wire rates/tables from DB into recalc | ✅ |
| **2** | Spreadsheet UI in React SPA | ✅ |
| **3** | Confidence / verification dashboard + finalize gate | ✅ |
| **4** | Triple-pass PDF extraction on existing RFQ parser | ✅ |
| **5** | Shop Data tuning UI + quoted-vs-actual | ✅ |
| **6** | Customer PDF + internal audit export | ✅ |

## Phase 4 notes

- Pure majority vote: `estimate_extraction_vote.py` (3/3 Confirmed, 2/3 Majority, else Review; anomalies force Review).
- Orchestration: `estimate_workbench_extraction_service.py` — 3 LLM passes with varied phrasing when PDFs + AI egress allow; otherwise deterministic `parse_rfq_package_files` (never invents Confirmed).
- Artifacts stored on `QuoteEstimate.source_attribution.estimate_workbench_extraction`.
- UI: **Extract from RFQ** → staging table → Replace / Merge, then Save.

## Phase 5 notes

- UI: `/shop-data` — editable Cut/Bend grids; every edit requires a note → `SettingsAuditLog` (`entity_type=cut_bend_row`).
- Rows auto-sort by thickness / gauge / fillet so banded lookup stays correct.
- `estimate_job_actuals` stores post-job laser/brake/weld hours; ≥15% variance surfaces a “tune this table” hint.
- Finalized bids keep their rate snapshot — Shop Data edits do not rewrite sent quotes.

## Phase 6 notes

- Customer PDF reuses `build_customer_quote_pdf` (sell lines only; no rates/hours/confidence).
- Blocked while Review items remain (same gate as finalize), unless already linked to a Quote.
- Internal audit: multi-sheet XLSX + JSON with fab/buyout/machined, hours, confidence, notes, rate snapshot, verification.
- Workbench header: **Audit XLSX** / **Audit JSON** / **Customer PDF**.

## Relationship to existing quote calculator

`quote_calculator.py` currently embeds a hardcoded `LASER_CUTTING_SPEEDS` dict.
Phase 1+ should route both the legacy calculator and the workbench through
`fab_calc_engine` + `CutBendTable` so there is **one** source of shop physics.
