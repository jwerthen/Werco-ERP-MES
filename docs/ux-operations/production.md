# Production retry reliability and working calendars

Implemented in the `codex/production-workflow-reliability` worktree. All validation below uses isolated synthetic records; no production business records were changed.

## 1. Safe kiosk retries

The existing additive production endpoint accepts an optional `request_id`. It serializes keyed requests within the company, fingerprints the original operation/operator/body, and commits a durable receipt in the same transaction as good, scrap, rework, time-entry, NCR and audit updates. Replaying that exact request returns its original committed response with `replayed: true`; a changed body or badge cannot reuse it. Historical receipts remain recoverable after the operator checks out or the job completes. Keyless clients retain their existing response contract.

Both kiosks now assign stable request IDs to keyed reports and coalesced one-tap batches. An uncertain batch stays immutable; later taps remain a separate buffer with their own eventual request. Retry after a lost response or an expired badge keeps the original identity. The mounted page prevents duplicate pending submissions. Session storage retains original operator/job IDs, quantities and request IDs across reloads, without retaining badge credentials. Explicit original-operator recovery can check a report even after the operation leaves the current queue. The UI refreshes current counts after confirmation rather than adding replayed quantities to an already updated tally.

The server receipt is durable. Browser recovery is scoped to the tab's session storage; clearing browser storage or closing the tab ends that local recovery context. Unkeyed historical clients are not retroactively idempotent. These are report retries, not an offline background outbox or a replacement for the existing over-count correction workflow.

Main files:

- `backend/app/api/endpoints/shop_floor.py`
- `backend/app/services/production_receipt_service.py`
- `backend/app/models/production_receipt.py`
- `frontend/src/components/kiosk/useOneTapPieces.ts`
- `frontend/src/components/kiosk/useProductionReportRequest.ts`
- `frontend/src/components/kiosk/ProductionRecoveryNotice.tsx`
- `frontend/src/pages/OperatorKiosk.tsx` and `CrewStationKiosk.tsx`

## 6. Working calendars

Each work center can configure seven weekday capacities and dated exceptions. Daily hours mean combined available shift hours, with zero indicating a nonworking day; holiday, shutdown, overtime and shortened-shift exceptions include an explanatory reason. The editor is available from each center row in Scheduling. It retains input after failed saves, guards pending submissions, and rejects concurrent edits with an explicit reload action.

`GET/PUT /scheduling/work-centers/{id}/calendar` is tenant scoped. Writes require an admin, manager or supervisor, lock the work center, compare `expected_version`, increment the version, and record an audit update. Weekly hours contain exactly seven values in Monday–Sunday order (0–24); exceptions have unique dates, hours (0–24) and nonblank reasons.

Configured calendars drive reviewed bulk scheduling and shifting, committed work-order projections, individual operation date validation, capacity summaries, daily heatmaps, date previews and the older finite scheduling service. Projections spread estimated hours over working capacity and skip closures. Downstream operations remain after their predecessors, including routes that mix configured and unconfigured centers. Earliest search refuses openings outside its requested horizon. Changing the calendar invalidates an older signed review token, so apply cannot silently use new rules. Existing fixed-date work stays visible as overload when a later shutdown removes its capacity.

Centers without saved calendar rows keep legacy projection and load conventions. Saving the first version explicitly enables calendar rules. The finite scheduler retains its minimum one-hour reservation for zero estimates (including capacity reload) while preserving legitimate fractional estimates; ordinary date-preview zero-estimate conventions remain unchanged. This is date-level planning, not an intraday shift clock or personnel staffing calendar.

Main files:

- `backend/app/services/working_calendar_service.py`
- `backend/app/services/scheduling_projection.py`
- `backend/app/services/scheduling_impact_service.py`
- `backend/app/services/scheduling_service.py`
- `backend/app/api/endpoints/scheduling.py`
- `frontend/src/components/scheduling/WorkingCalendarEditor.tsx`
- `frontend/src/pages/Scheduling.tsx`

## Migrations and verification

- `095_kiosk_production_receipts` follows `094_document_deliveries`.
- `096_working_calendars` follows `095_kiosk_production_receipts`.
- The shared branch continues through `097_team_workspaces` to the single head `098_runtime_metrics`.
- Both new tables enforce company-scoped uniqueness, enable PostgreSQL RLS, and revoke table/sequence access from PUBLIC and available anon/authenticated roles. The existing server API remains the access boundary.
- Migration roundtrips run against isolated SQLite. PostgreSQL DDL is compiled and checked for uniqueness, RLS and revocation. No live PostgreSQL lock-contention test is claimed.
- **73 focused backend tests pass**, covering new calendars/receipts plus existing scheduling, scheduling-impact, tenant isolation and kiosk NCR regressions. Independent review reproduced and then verified fixes for mixed-center predecessor delays, horizon bounds, zero-hour finite reservations and fractional estimates.
- **241 focused frontend tests across 24 suites pass under `CI=true TZ=UTC`**, including the kiosks, one-tap binding/buffering, reload recovery, original-badge checks for departed jobs, Scheduling, and the calendar editor's pending/conflict/read-retry states.
- Targeted backend Black/isort, Flake8 and mypy checks pass. Frontend source types and targeted zero-warning ESLint pass. Final aggregate validation and local browser evidence are recorded in the round's README.

Focused logs are under `/tmp/werco-production-reviewed-*`; these temporary logs are local validation evidence and are not deployment artifacts.

## Browser acceptance, September 7

Real Chromium flows against the isolated API8005/frontend5178 passed with synthetic `QA-CALENDAR-PLAN` and `QA-RETRY-REPORT` records:

- Saved Friday4h, closed weekends, and a September14 shutdown in the editor; a one-day shift reviewed September11–15 and **Apply reviewed plan** committed those dates. Reopening the editor retained the saved values. The original saved job dates were untouched until apply.
- Intercepted an actual keyed3-piece production POST only after the API committed, then aborted its browser response. Reload retained the exact request/operator/body. Original badge recovery returned `replayed:true`; committed total12 remained12.
- Repeated the lost-response scenario for one one-tap piece, then buffered another tap. Reload retained2 pending with1 immutable. Checking the original receipt kept total13; checking the remaining buffer sent a distinct request ID and increased the total exactly once to14.
- The calendar modal measured358px inside a390px viewport; Scheduling and kiosk document widths measured390px. A narrow Scheduling header wrap fixed the overflow found during this check. Both flows recorded zero browser page errors.
- Browser testing also caught overlapping crew-board and recovery-panel scanner capture; the board capture is disabled while recovery owns the scanner, with a focused regression asserting no board shortcut/API call.

Screenshots:

- [Calendar desktop](screenshots/working-calendar-desktop.png)
- [Calendar mobile](screenshots/working-calendar-mobile.png)
- [Calendar mobile overrides](screenshots/working-calendar-mobile-overrides.png)
- [Reviewed dates](screenshots/working-calendar-impact-preview.png)
- [Keyed report after lost response](screenshots/kiosk-report-unknown-desktop.png)
- [Keyed recovery after reload, mobile](screenshots/kiosk-report-recovery-mobile.png)
- [Confirmed keyed report](screenshots/kiosk-report-confirmed.png)
- [One-tap recovery after reload, mobile](screenshots/kiosk-onetap-recovery-mobile.png)
- [Original one-tap receipt confirmed, buffer retained](screenshots/kiosk-onetap-original-confirmed-buffer-held.png)
- [Buffer confirmed separately](screenshots/kiosk-onetap-buffer-confirmed.png)

The temporary reproducible browser scripts are `/tmp/werco-production-calendar-browser.cjs`, `/tmp/werco-production-kiosk-browser.cjs`, and `/tmp/werco-production-onetap-browser.cjs`. They intentionally change only the named synthetic local records. The calendar dialog also passed a scoped serious/critical axe scan at390px (0 violations).

Final screenshot review exposed a midnight-date display mismatch: the queue and date editor treated date-level API values as UTC timestamps and showed the previous Central date. Scheduling now normalizes only its schedule date fields on load. The regression failed before the fix and passes under UTC, America/Chicago and Asia/Tokyo. A real Chromium session in America/Chicago confirmed the applied September11 date in the queue and editor, and a subsequent read-only shift preview showed September15–16 after the weekend/shutdown. The final preview screenshot shows that later read-only check.
