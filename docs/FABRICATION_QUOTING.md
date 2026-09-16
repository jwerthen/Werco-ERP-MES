# Replacement fabrication quoting

This subsystem implements a new estimator workspace, assembly/cost domain and immutable quote history. It is independent of the old Workbench calculation paths. The ERP supplies authentication, company/customer identity, and an explicit approved-package handoff.

**Entry point:** `/fabrication-quotes` under Sales & Quoting. This is the only estimator creation workspace. Old estimator UI URLs redirect here; the old pricing/authoring APIs and standalone quote-nesting worker have been retired. Existing issued quote records and customer-document workflows are retained.

## Workflow

1. Create an estimate and select its customer and currency. Define parts, root demand and parent/child BOM quantities. Purchased subassemblies stop child cost rollup.
2. Attach DXF, STEP/STP, PDF or CSV sources. Review the original and extracted evidence. Record which requirements are represented or why a source is excluded.
3. Enter material allocations, process routes and purchased hardware. Blank prices and times remain unknown. Shop rates are never prepopulated as Werco measurements.
4. Select manual, laser, brake or weld time recipes. Separate setup, attended labor, machine occupancy, consumables and outside services. Record supporting evidence and review the complete route.
5. Calculate a nest against actual stock, quantities, material/thickness, spacing and orientation constraints. Save its evidence and explicitly apply its stock cost once. A changed assembly quantity invalidates a referenced layout.
6. Calculate and save. Resolve blocking issues, review the cost and selling margin, and approve a frozen revision. Further editing requires a new draft revision.
7. Create an ERP draft quote or export the approved manufacturing package. Handoff retries return the existing result. Sending to customers and creating released production BOMs/work orders are separate actions.
8. Record operation actuals against an approved revision. Distinguish complete observations from partial measurements; unknown time is not zero.

## Cost contract

The Pydantic schema is in `backend/app/fabrication_quote/schemas.py`; the pure calculation function is `evaluate_plan`. Decimal strings are the wire format. Distances in process recipes use millimeters; time uses seconds; rates name their hourly basis. A quote uses one explicit currency.

The engine rolls repeated BOM demand before calculating costs. Batch setup uses a named batch size; per-piece and per-batch runs are distinct. Equal hardware manufacturer/MPN demand is pooled before stock, price breaks, packs, MOQ and order multiples are applied. Existing inventory carries an issued value. Purchased excess inventory and procurement cash are separated from consumed hardware cost.

CSV hardware import provides explicit header mapping, optional repeated terms, a row preview, and separate BOM-only or BOM-with-offer modes. It records file hash and source record for each proposed item. Duplicate identities require demand/supply reconciliation; multi-row price tiers use the hardware editor. Imports clear affected completion/source reviews and leave offers unapproved. CSV uploads and mapping do not constitute live supplier-price retrieval.

An estimate includes an engine version, input hash, calculation date, source evidence, review assumptions, detailed cost lines and totals. A price refresh does not modify a prior approved revision. Internal cost precision is six decimal places at output; the UI formats money for reading. ERP handoff currently requires USD because legacy ERP quote records do not store currency.

## Implemented file processing

| Input | Implemented | Review/coverage boundary |
|---|---|---|
| DXF | ezdxf entity/transform processing, physical-unit normalization, curve lengths, loops and hole observations | Cut/mark/bend layer meaning and executed pierces need confirmation. Unsupported/invalid/overlapping geometry is reported. |
| STEP | OCCT/XDE definitions and occurrences, placed transforms, solid measurements, bounded display meshes | Geometry does not determine material, weld callouts, make/buy intent or a production route. |
| Already-flat STEP sheet | Restricted constant-thickness planar-body qualification, cap/boundary comparison, candidate developed contours and through-holes | Candidate geometry requires estimator review. Bent, stepped and unsupported surfaces do not receive invented flats. |
| PDF | All-page native extraction, text/vector/table evidence and page coverage; original PDF browser preview | Image-only pages and ambiguous engineering symbols remain visible. General OCR and automatic weld-symbol interpretation have not been qualified. |
| CSV | Bounded raw table cells, mapped hardware demand and optional supplier offers, row preview and exact source evidence | Identifiers, per-part quantities, price units, shared supply and offer applicability need explicit reconciliation before use. |

Exact B-Rep measurements remain server-side. Three.js meshes are display derivatives with definition/occurrence mappings and documented tessellation tolerance, not production dimensions.

The baseline nest is a deterministic **conservative envelope placement** with stock containment, spacing, rotation, material and quantity checks. It can overestimate material and does not prove minimum sheet count. It produces no NC code, bend collision simulation, common-line cutting or part-in-hole manufacturing strategy.

## Local demo

The demo runner creates a new temporary SQLite database with synthetic users, a customer and a complete example assembly. It changes its working directory before loading ERP settings and overrides its database connection. It never loads the repository `.env` or seeds production records.

```sh
cd backend
.venv/bin/python scripts/run_fabrication_quote_demo.py --port 8766
```

In another terminal:

```sh
cd frontend
REACT_APP_API_URL=http://127.0.0.1:8766/api/v1 npm run dev -- --host 127.0.0.1 --port 5179 --strictPort
```

Open `http://127.0.0.1:5179/fabrication-quotes?id=1`. The runner prints its **demo-only** login and temporary data directory. All example rates and prices are synthetic. Each runner invocation creates a fresh database; retain its output directory if the examples matter.

Demo sign-in: `demo@example.com` / `Local-quote-demo-2026!`. Synthetic STEP, DXF and PDF examples with independent expected geometry are available in `/Users/jonwerthen/Desktop/New Quote Tool/examples/`.

### Native CAD/document worker

The API Docker images install the hash-locked native worker at `/opt/werco-quote-worker` and set `WERCO_QUOTE_WORKER_PYTHON` automatically. Native CAD libraries are isolated from the main API dependencies. For a local or non-Docker deployment, install the same exact dependency set:

```sh
cd backend
python3.11 -m venv .venv-quoting
.venv-quoting/bin/python -m pip install --require-hashes -r requirements-quoting.lock
export WERCO_QUOTE_WORKER_PYTHON="$PWD/.venv-quoting/bin/python"
```

The local demo detects this environment automatically. Non-Docker API deployments must set `WERCO_QUOTE_WORKER_PYTHON` explicitly. The worker starts with a restricted environment containing no ERP database or supplier credentials. Each subprocess has a 60-second wall timeout, a 45-second CPU budget where supported, and bounded result size. Failed analysis preserves the original with a visible failure; it does not produce a zero-cost estimate.

Worker inputs are capped at 25 MiB per file, 100 files/100 MiB per quote; quote JSON writes at 2 MiB. Geometry/page/occurrence budgets are also enforced. Limits and unsupported cases appear in extraction evidence.

## Persistence, access and migrations

- Migration **106** creates fabrication quotes, immutable revisions, source files and actual observations; it performs no legacy-data migration.
- Migration **107** adds the versioned process-profile library. Applying a template requires new job review; a saved template is not a manufacturing approval.
- All records are company scoped. Effective `purchasing:view` governs reads; writes additionally require `purchasing:create`. Read-only company contexts cannot mutate records.
- Each write uses an expected revision and atomic audit logging. PostgreSQL and SQLite database controls prevent modification/deletion of frozen evidence. PostgreSQL direct client roles are denied access.
- Source bytes and analysis are stored in immutable rows for the pilot. Revision manifests reference their hashes rather than copying mesh/document data on every save. An object-storage adapter can replace binary storage without changing source identity.
- Approved handoff creates a single ERP draft line for the complete quoted package, with all internal cost categories mapped and the approved source revision retained. It does not guess internal part IDs or create released engineering records.

The API deployment applies migrations through the repository's normal `alembic upgrade head` startup. No historical quote tables are dropped. Old quote authoring/pricing endpoints are unregistered; customer quote documents retain export, delivery, status, commercial metadata and work-order conversion, while manufacturing quantities and prices are frozen. New manufacturing changes require a reviewed fabrication estimate revision.

## Validation and next qualification gates

Focused automated coverage exercises independent cost examples, nested/repeated BOMs, batch setup, hardware purchasing conservation, unpriced inputs, parser fixtures, nesting feasibility, stale-layout rejection, effective permissions, tenant boundaries, immutable history, audit rollback and idempotent handoff/actuals. Migration tests use disposable SQLite and a real, disposable PostgreSQL 15 instance to verify both migrations, immutable evidence, tenant foreign keys, role restrictions, safe rollback and preservation of historical quote records. Frontend checks cover stale/unsaved approval gates and actual interaction paths.

The 2026-09-15 verification included 129 passing backend cost/API/parser/migration/body-size tests in the main environment (five OCP cases skipped there), 30 passing worker/upload-boundary tests, and a separate locked-worker run of all 28 ingestion/nesting cases with no skips. The worker run overlaps the main parser cases; these counts should not be added as independent cases. Full backend mypy passed across 467 source files. Browser checks exercised quantity changes, dirty approval blocking, save/approve/handoff, historical actuals, STEP occurrence selection, DXF contours and all three PDF pages. A 390-pixel viewport had no page overflow.

After retiring the legacy workspaces on 2026-09-16, the full frontend suite passed 365 suites / 3,846 tests with all existing coverage thresholds satisfied. Production and test TypeScript programs, full ESLint and the production build also passed. Existing CSS ordering/optimization and large-chunk warnings remain; the native 3D viewer loads separately. A real browser CSV upload → mapping → preview → append → save round trip preserved source provenance and correctly left total cost unresolved pending item/offer/source review. Targeted frontend ESLint, backend flake8 and medium/high Bandit checks passed.

Run the native geometry cases using main pytest and the worker's pinned libraries without adding test packages to its runtime:

```sh
cd backend
PYTHONPATH=.venv-quoting/lib/python3.11/site-packages:. .venv/bin/python -m pytest tests/services/test_fabrication_quote_ingestion.py tests/services/test_fabrication_quote_nesting.py --noconftest -o addopts='' -q
```

This scoped command does not assert the repository's whole-application coverage threshold. On 2026-09-16, the PostgreSQL migration verifier passed against a disposable PostgreSQL 15 database. The production CAD worker also passed Linux container smoke tests for DXF, STEP, PDF, CSV and nesting with networking disabled and a read-only filesystem. These checks are included in CI.

The full ERP backend regression run reached **87.20% coverage**, above the unchanged 78% requirement. Frontend coverage requirements also remain unchanged.

These tests establish software behavior on the covered cases. They do not establish a Werco actual-cost error percentage.

Shop qualification and operating setup:

1. Load reviewed shop rates, process families, tooling/allowance information and applicable supplier offers. No machine/controller connection is necessary.
2. Run representative Werco packages through the whole workflow and measure setup, run, attended labor, good/scrap quantity and actual cost.
3. Qualify additional automation separately: FreeCAD SheetMetal for bent-part unfolding, PackingSolver for improved irregular nesting, and Docling/PaddleOCR for scanned/complex drawings. The working manual/conservative paths remain explicit until those components pass the same review requirements.
4. Connect the selected suppliers using their authorized interfaces. Quote/price-list evidence and manually reviewed offers work without live account integrations; no live supplier prices are invented.
5. Review approved BOM/routing-to-production mappings before releasing work orders. The ERP handoff creates a customer quote package, not an automatically released engineering BOM or routing.

The research and equipment baseline are in `/Users/jonwerthen/Desktop/New Quote Tool/`. The equipment list is context; it is not a source of tested speeds, labor rates, tooling inventory or accuracy claims.
