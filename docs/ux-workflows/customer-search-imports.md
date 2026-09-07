# Customer preservation, recoverable imports, and dependable search

Implemented in `codex/production-workflow-reliability`, September 7, 2026. This note describes the final domain behavior; [API reference](../API.md) and [RBAC](../RBAC_PERMISSIONS.md) cover the integrated contracts.

## Customer edit preservation

Customer list/detail/update responses include all billing and shipping address fields, countries, notes, and special requirements. Editing initializes from that complete record. The browser sends only fields changed from the opened form, so a phone-only change preserves account requirements and concurrent changes to untouched fields. Explicit blank email clears the optional email. Shipping line 2 and country are also accepted by the legacy customer CSV validator/creator.

`PUT /customers/{id}` locks the company-scoped, non-deleted customer and records supplied-field old/new values through `AuditService` before the same commit. Existing authenticated-user authorization is retained. Same-field concurrent editing still uses last-save-wins; this is not a new optimistic-locking contract.

A real-browser regression exposed a linked customer detail reopening over its edit form while the router cleared `?id`. The detail effect now yields while the edit form is open. The mobile browser test actually presses Update and checks that its body contains only the changed phone number, then reloads and confirms shipping/requirements/notes survived.

## Durable import review and recovery

The Import Center uses saved reviews for eight existing direct formats: employees, parts, materials, customers, vendors, work centers, open work orders, and open purchase orders. BOM, routing, and inventory imports continue through their specialized workflows; they have not been silently replaced by a generic ledger.

Each normalized upload has a company-scoped source hash and stable request key. Reuploading the same source opens its existing receipt. Each source row retains its original row number and UUID, and all lines of a purchase order retain one immutable group identity. Created rows and their record links are immutable in the correction workflow.

| API under `/api/v1/import/batches` | Contract |
| --- | --- |
| `GET` | Latest 25 permitted batches; offset and `has_more`. |
| `POST /prepare` | Multipart entity, file, stable request key, optional transient default password. Validate and save a receipt; no business records yet. |
| `GET /{id}` | Read-only receipt, status counts, distinct created-record count, 200-row pages using `row_offset`. |
| `POST /{id}/commit` | Explicit reviewed `expected_version`; up to 25 record groups per request, each durably committed. Optional transient employee credential file/default password. |
| `GET /{id}/failed-rows.csv` | Only failed/invalid input rows; stable `_import_row_id` and escaped spreadsheet cells. A committed `EXPORT` audit records batch/entity/count only. |
| `POST /{id}/corrections` | Validate changes to failed rows from this exact batch/version. Created rows, duplicate/foreign row IDs, and incomplete PO groups are refused. |

All new multipart file reads are bounded at 10 MB + one byte before parsing; oversize input returns 413. The existing parser also enforces its row/file constraints. Password/default-password/hash columns are removed before persistence, history, source hashing, and export. Employee credentials must be supplied again on resume when required. The UI clears transient credentials on completion, token/company changes, and unmount.

The service calls the existing domain validators/creators inside a savepoint-joined session. Their internal commits cannot commit outside the parent unit. The business record, its domain audit/outbox changes, and durable row receipt commit together. Pending notification events are transferred only after the unit succeeds and dispatched only after the durable parent commit. A simulated lost commit response leaves a truthful receipt; a simulated receipt failure rolls back business data and pending events.

A stale reviewed version returns 409. The UI stops on an uncertain commit outcome and requires reading the receipt before further writes. The bounded automatic continuation only commits already-reviewed remaining groups, using the returned version. Synchronous token-change invalidation stops queued groups before CompanyContext finishes switching; the central API service pins each import mutation to its initiating token and never replays a 401 using another company's session.

One bad purchase-order line marks the entire group correctable. Export includes every failed group line, even when the underlying validator only identifies one line, so correction cannot strand valid-looking siblings. Corrections retain group/row identities and do not recreate successful records.

**Authorization:** employees require admin; customers/vendors/work centers/purchase orders require admin or manager; parts/materials/work orders also allow supervisor. Existing platform/superuser and read-only-company restrictions remain in the shared dependency layer. History, receipt, credentials, and failed export use the same company/entity authorization. The existing Import Center route permission remains in effect as well.

The legacy open-WO/open-PO cutover formats create open production jobs/issued purchase orders. The explicit review label states those semantics. This is not the MRP draft-creation flow and does not send purchase orders or email. The `Import Batches` tag is excluded from generic MCP tool generation so those reviewed cutover writes are not exposed as generic assistant mutations.

Migration `099_recoverable_import_batches` follows `098_runtime_metrics`; migration 100 follows it. Both new tables enable PostgreSQL RLS with no public policies and revoke all table/serial-sequence privileges from PUBLIC and existing anon/authenticated roles. The custom authenticated server retains its own database access. Table/index guards permit repeat upgrades without deleting stored data; downgrades are guarded and explicitly remove these new receipt tables. Operational rollback should preserve a backup of receipt history rather than casually downgrading after use.

## Search across the full matching set

`GET /search/` keeps its existing entity types and adds offset pagination, exact totals, and `has_more`. The SQL union ranks all permitted matching rows before applying the final limit: current exact matches, exact retired-part aliases, identifier prefixes, then substring matches. SQL wildcard characters are treated literally. Each page uses rank, case-insensitive title, type, and ID for stable ties.

The search service includes current part/customer/WO/vendor fields and retired part-number aliases. Company predicates apply to every union branch and correlated alias lookup. Soft-deleted parts, WOs, customers, vendors, POs, BOMs, and routings are excluded; joined engineering parents are also scoped and non-deleted. Employee search retains its admin/manager restriction. Explicit type filters constrain both hits and returned counts, including Copilot's existing exclusion of people.

The command menu offers See all record matches, opening `/search?q=...`. The result page supports type selection and 25-result pages with truthful ranges; past-end bookmarked pages offer Previous/First page. Both browser surfaces limit requested types to accessible destination routes. Old query/company responses cannot replace the latest result page. Existing NL/command-menu behavior remains intact.

Customer, vendor, PO, quote, BOM, routing, employee, WO, and Part links use existing detail/deep-link contracts. Materials are Part records and use the actual `/parts/{id}` detail. Import work-center receipts show their created record ID because that page does not implement a record-selection query; they do not pretend an unsupported `?id` opens a detail.

This is substring/exact search, not fuzzy matching or a relevance-trained engine. Totals and page rows are separate queries; concurrent record edits can change the result set between requests. No production-scale latency improvement or exhaustive authorization redesign is claimed.

## Verification

- Focused initial backend set: **55 passed** across import service/API/migration, customer preservation/audit, search ranking/soft-delete, and Copilot compatibility. Subsequent targeted runs covered all later changes: **12 import service tests passed** including full failed-PO correction and outbox rollback; **12 late search/API tests passed** including BOM/routing tombstones, empty type-list consistency, and metadata-only export audit. These overlapping counts must not be summed.
- Final focused frontend set: **29 tests passed in 6 suites**. Coverage includes partial update payloads and dialog layering, search pagination/stale queries, command-menu races, import stable prepare keys/reconciliation, scoped old responses, immediate queued-chunk cancellation on token replacement, and real ApiService delayed-401 mutation behavior.
- Frontend typecheck and zero-warning ESLint for owned source/test files passed. Owned backend mypy, Black/isort, and Flake8 passed. The repository excludes E2E files from its ESLint configuration; the E2E file is checked by Playwright/TypeScript compilation.
- Real Chromium on isolated local API8007/SQLite and frontend5180: **2 browser tests passed**. Customer full-field edit/reload at 390 px; customer import partial commit/reload/failed-row CSV correction/review/commit; completed receipt reload without duplicate import; type-filtered search followed to the actual customer record. No live systems or mail providers were used.
- Migration tests exercise upgrade twice, downgrade twice, and re-upgrade on isolated SQLite, plus PostgreSQL offline SQL for RLS/grants. Root's combined migration verifier/CI owns real PostgreSQL upgrade/privilege checks. No local PostgreSQL contention test or all-device/accessibility conformance claim is made here.

Reproduce the behavioral checks with `pytest tests/services/test_import_batches.py tests/api/test_import_batches.py tests/test_migration_099.py tests/api/test_customers.py tests/api/test_customers_audit_persistence.py tests/api/test_search_pagination.py tests/api/test_search_soft_delete.py tests/api/test_copilot.py --no-cov -q` from `backend/`; the focused no-coverage run does not replace the full-suite coverage gate. Run `playwright test e2e/customer-search-imports.spec.ts --workers=1 --retries=0` against an isolated seeded test environment with the repository E2E environment variables.

Screenshots, visually inspected after the flow passed: [customer form on mobile](screenshots/customer-preserved-mobile.png), [corrected import receipt on mobile](screenshots/import-correction-mobile.png), [completed receipt](screenshots/import-complete-desktop.png), and [filtered search](screenshots/search-customer-results-desktop.png).
