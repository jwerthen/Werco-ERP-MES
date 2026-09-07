# Team views and remaining saved workspaces

Managers and administrators can explicitly save a current table view for colleagues in the same company who retain access to that module. Other permitted users can apply team views and create their own private views. Team views are identified in the selector; manager controls are absent for readers. Updates and deletion require the last observed version, preserving another manager's change on conflict.

Team views use a separate `team_workspace_records` table and the `/api/v1/user-workspaces/team/{namespace}` endpoints. The server accepts only view configuration (`table`, `layout`, and string-valued `filters`), rejects draft kinds/extra payload fields, rechecks module permissions for every operation, scopes all reads/writes by the authenticated company, and denies writes in a platform read-only company context. No private draft is migrated or shared. Private table-layout preferences remain private. The server caps team views at 25 per module.

Migration `097_team_workspaces` follows `096_working_calendars`. It enables RLS and revokes table and sequence access from PUBLIC and the Supabase `anon`/`authenticated` roles. ERP authentication remains the existing FastAPI custom-JWT model; the migration adds no guessed `auth.uid()` policy. It also adds the Work Orders browse index. Supabase's current RLS documentation and changelog were checked; this repository continues to use its established Alembic migration workflow.

## Screen coverage

| Screen | Saved configuration |
| --- | --- |
| Work Orders | Search, status/customer/COTS/date scope, grouping, server sort, columns, density |
| Purchasing, Quality | Existing private table workspaces now support explicit team views through the shared control |
| Inventory | Independent summary/detail layouts, sort/columns/density, text/group/low-stock filters |
| Shipping ready queue | Text filter, columns, sort and density; original due-date default retained |
| Shipping shipment history | Text/status/date filters; existing tracking expansion and bespoke table remain |
| Parts (`PartsNew`, the routed catalog) | Search, type, status, BOM-component visibility and table/grid mode |

Shipment history and Parts expose saved filter/view settings without offering unsupported column controls. Existing Parts device filters remain available and are explicitly labeled as device storage. “Import to my account” copies a chosen device filter into a private account view using the device filter's own values. It does not automatically claim browser-wide legacy filters for a signed-in user or share them with a team. New device saves are labeled “Save on device.” Applying an account view clears selected part IDs, and pending Parts refreshes preserve the saved-view controls/selection; obsolete request results are discarded.

## Acceptance evidence

- [Team API tests](../../backend/tests/api/test_team_workspaces.py), [private workspace regressions](../../backend/tests/api/test_user_workspaces.py), and [migration tests](../../backend/tests/test_migration_097_team_workspaces.py) cover reader/manager rights, revocation, tenant isolation, read-only company context, CAS conflicts, rejected draft/scope injection, added namespaces, SQLite upgrade/downgrade/up preserving existing drafts/WOs, and PostgreSQL offline table/sequence security SQL.
- [Shared hook tests](../../frontend/src/hooks/useTableWorkspace.test.ts), [controls tests](../../frontend/src/components/ui/TableWorkspaceControls.test.tsx), and [Parts import test](../../frontend/src/pages/PartsNew.saveFilterDialog.test.tsx) verify team/private key separation, non-manager controls, durable failed edits, explicit legacy import, and ignored late writes after an account switch.
- Synthetic Chromium checks apply saved Inventory and Shipping filters, import/apply the actual Parts catalog's legacy filter, and apply the manager's team view as a second non-manager user. All four screens measure 390px document width at 390px viewport. [Inventory](screenshots/inventory-mobile-workspace.png), [Parts](screenshots/parts-mobile-workspace.png), [Shipping](screenshots/shipping-mobile-workspace.png), [team reader](screenshots/work-orders-mobile-team-reader.png).
- Browser helper/log: `/tmp/werco-workspaces-acceptance.mjs` and `/tmp/werco-workspaces-acceptance.log`. The helper waits for saved-view loads and React navigation completion before asserting restored filters; initial immediate-read harness timing was corrected without changing product assertions.

## Focused validation

Backend: 36 tests pass across browse/team/private/migration suites. Frontend: 22 focused suites / 186 tests pass, including the real data-router rapid-query regression. Source and test TypeScript, scoped zero-warning ESLint, backend Black/isort/flake8, and `git diff --check` pass. Logs: `/tmp/werco-operations-backend-focused.log`, `/tmp/werco-operations-focused-frontend.log`, `/tmp/werco-operations-typecheck.log`, `/tmp/werco-operations-eslint.log`, and `/tmp/werco-operations-flake8-final.log`. The complete [workspace-recovery Playwright spec](../../frontend/e2e/workspace-recovery.spec.ts) also passes all 4 journeys without retries on the fresh synthetic 8006/5179 environment (14.7 seconds; `/tmp/werco-operations-workspace-recovery.log`). Its saved-view selector now explicitly expects the new `· Private` scope label; the initial full-suite failure was an outdated exact option label, with the view correctly present. The real PostgreSQL migration preflight and full integration gates are owned by the root task; offline SQL checks here are not represented as a PostgreSQL runtime test.
