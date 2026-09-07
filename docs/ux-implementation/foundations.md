# Foundation UX implementation evidence

Implemented in the isolated `codex/ux-audit-improvements` checkout from current main (`0a383b8c63926dd02e2a37c5153b43a3b095631b`). This report covers the assigned shared experience, authentication, navigation, search, notifications and overview work. No deployment, production record edits, or outbound messages were performed.

## Implemented packages

| Package | Result | Verification |
|---|---|---|
| UX12 — trustworthy overview state | Dashboard renders the main response without waiting for slow widgets. Secondary widgets preserve prior verified values and expose loading/unavailable/stale status with retry. Cache results distinguish a validated 304 from a failed-request fallback. A failed refresh retains shop content. Setup cannot claim readiness when health is unavailable. | Deferred-widget, failed-refresh, stale-cache and failed-setup regressions; existing activity/presence tests. |
| UX13 — overlays and accessibility | SelectField uses the owning modal's portal, outside its scroll panel and inside its focus boundary. Escape closes only the popup first; selection restores trigger focus. Trigger/list/options expose their relationships. ComboBox uses the same portal. Modal derives an accessible name from a heading or explicit label; confirmation/input dialogs connect their titles. Session warning uses Modal. Tabs have roving keyboard focus and selection semantics. Hidden navigation and Copilot are inert; Copilot is also hidden from rendering while closed and restores focus. | Modal/SelectField/ComboBox suites, overlay test, Tabs keyboard case, Copilot hidden/open/focus regression, existing navigation tests. |
| UX14 — recovery, return destination and timeout | Forgot password opens honest administrator-assisted recovery instructions. Password visibility has an accessible name/state. Route guards and API expiry preserve local record/query/fragment destinations; login rejects unsafe return paths and keeps kiosk defaults. AuthProvider no longer restarts its effect when the warning appears. Ordinary activity during the warning does not extend the session; explicit extension does. Kiosk idle logout returns to its badge state. | Real AuthProvider fake timers: warning at 14 minutes, logout at 15, explicit extension; safe-return rejection cases; login/default landing and token-clearing tests. Fresh Chromium acceptance confirmed the selected quote URL after the onboarding correction; New Work Order return has real AuthProvider/router integration coverage. |
| UX15 — durable feedback | Warning/error toasts remain until dismissed; success/info retain automatic dismissal. The stack scrolls within the viewport. Announcement semantics and defensive message normalization remain. | Durable-error timeout test plus existing warning/error/422 and dismissal cases. |
| UX18 — dirty SPA navigation | A data router uses one shared blocker aggregating dirty forms. Links, breadcrumbs and browser Back/Forward can display a named Leave/Stay dialog. Stay preserves edits. `markSaved()` allows successful save navigation; production owner adopted it in New Work Order, Part Edit, Part creation and BOM import. | Link/Back/Stay/discard/save integration tests. Real WorkOrderNew under App's wildcard/descendant Routes topology stays protected after failed validation. Fresh-tab browser checks confirmed Stay and Leave. |
| UX19 — permitted discovery and scoped help | Shared route-access rules drive route guards, sidebar children, mobile destinations and search actions/results. Parent navigation highlights detail routes. Mobile Help & Tours is reachable with bounded content. Tour completions, adaptive visit/dismissal history and inbox dismissals use user/workspace scopes. Adaptive work-order help scrolls/focuses the actual Blockers panel. | Existing role/navigation/help tests; inaccessible command filtering; defensive preference reads. |
| UX22 — search recovery | Delayed recents never erase typed input. Query changes, clear and close invalidate old responses. Failure exposes retry with the query retained. Keyboard navigation reaches permitted fallback quick actions and announces selection. | Delayed-recents, clear-before-response, failure/retry and quick-action keyboard cases. |
| UX23 — notification consistency | Bell/inbox share unread count, write deduplication and invalidation revision. Reads update both surfaces. Superseded count/list requests cannot restore stale state; session changes invalidate earlier work. Mark all read uses global scope rather than visible-page unread rows. | Shared simultaneous-read regression; existing bell/inbox suites; list sequence guards. |
| UX24 — readable identifiers and priorities | Shared `--fd-link` makes legacy navy identifiers readable on dark surfaces while retaining background brand colors. [utils/priority.ts](../../frontend/src/utils/priority.ts) defines P1–P10 labels/tones with lower numbers more urgent; production owner adopted it across planning/work-order surfaces. | Type checks and existing suites; common helper prevents separate label/color rules. |

## Overview scope and action hierarchy

Dashboard overdue/due-today counts now exclude soft-deleted and terminal jobs and use the shop's Central calendar day. Active work orders explicitly means in progress. Links carry `scope=overdue`, `scope=due_today`, or `status=in_progress`, plus `cots=1` so the destination does not hide categories that the summary counted. The production owner implemented the matching list scopes and drill-down regression.

Calibration uses `filter=due_soon`, Quality uses `tab=ncr&filter=open`, and Inventory uses `tab=inventory&filter=low_stock`, coordinated with the operations owner. Completed Today is labelled as operations and opens recent completion activity instead of implying that it counts work orders.

Dashboard gives its first operational exception a review action. Action Inbox gives its first high-priority item a review action. Inbox counts include featured recommendations and stay stable when filters move those recommendations between hero and queue. All non-dismissed views apply the same dismissal rule. URL filters survive navigation and support AI deep links. The AI count discloses its bounded population of up to 25 pending recommendations. Unavailable sources are partial/unverified; stale AI recommendations cannot be actioned until refreshed.

The shell reports the actual update-connection state instead of static LIVE/SYNC OK. This describes the WebSocket transport; dashboard validation timestamps and section errors describe data freshness.

## Shared interface notes

- `useUnsavedChanges` preserves synchronous `confirmDiscard()` and adds `markSaved()`. Call `markSaved()` after a successful save and immediately before navigation. The app-level provider owns SPA confirmation.
- `Modal` keeps existing props and adds optional `ariaLabel`; explicit `ariaLabelledBy` remains supported. `useModalPortal()` supplies the popup container for SelectField and ComboBox.
- `SelectField` trigger now has the correct `combobox` role. Caller tests querying its former button role were updated without changing its accessible label.
- `Tab.panelId` is optional. When supplied, the tab button ID is `${panelId}-tab` and `aria-controls` is `panelId`. The caller renders `role="tabpanel" id={panelId} aria-labelledby={panelId + '-tab'}`.
- [priority.ts](../../frontend/src/utils/priority.ts) exports `PRIORITY_OPTIONS`, `PRIORITY_HELP_TEXT`, `getPriorityLabel`, `getPriorityTone`, and `getPriorityClasses`. P1 Critical, P2 Urgent, P3 High, P4 Elevated, P5 Normal, P6–8 Low, P9–10 Lowest.

## Validation record

- The foundation tests passed in the final combined frontend run. Targeted suites cover real AuthProvider timing, nested overlays, stale search/notification requests, dirty navigation, independent overview failures and first-use login routing. Final combined totals and checks are maintained in [validation.md](validation.md); overlapping targeted runs are not added together.
- The backend dashboard suite passed all seven tests, including Central midnight, deleted jobs and terminal status scope, against isolated fixtures.
- Owned source/tests passed ESLint with zero warnings and both production/test TypeScript checks. Owned TypeScript/TSX files were formatted. Changes to the shared `api.ts` remained local to authentication/cache code.
- Chromium checks exercised the named Leave/Stay dialog, hidden Copilot state and the fresh-session selected-quote return. Specific captures and remaining browser checks belong in the coordinator's browser acceptance record.

## Boundaries

Administrator-assisted recovery is implemented because no verified self-service recovery endpoint exists in the app; the UI does not claim to send a reset email. SPA Leave/Stay uses an application dialog. Existing synchronous `confirmDiscard()` callers still use native confirmation for their own Cancel/Close actions, and refresh/close uses native before-unload confirmation.

Notification coordination covers a browser session; cross-session freshness retains polling and the existing transport infrastructure. Recommendation counts disclose the endpoint limit rather than inventing totals. Priority meanings retain the established ordering.

Browser checks strengthen the exercised paths, without claiming full assistive-technology conformance, every role/device, exhaustive workflow coverage, or measured performance gains. Staging validation should include screen readers with nested dialogs, kiosk session expiry, and notifications across multiple signed-in sessions.

## Final rendered contrast corrections

Root browser QA caught the separate `text-werco-600/700` legacy color family on Time Clock work-order identifiers. Dark-surface identifiers and links in ShopFloor, ShopFloorSimple, WorkOrders, WorkOrderNew, and Dashboard now use the shared `text-fd-link` token (`#93c5fd`) with light hover color; controls with genuinely light brand fills retain dark text. The warning ConfirmDialog now uses `text-slate-950` on its amber fill in normal and hover states. The related button regression was updated. Chromium at 390px measured the Time Clock identifier as `rgb(147, 197, 253)`, matching the shared token. The warning button's computed colors were converted to sRGB in Chromium and measured at 9.44:1; the exact pair is recorded in browser-acceptance.md. No app-wide WCAG conformance is asserted.

## First-use onboarding preserves sign-in destinations

Final clean-browser acceptance exposed a separate onboarding redirect: sign-in correctly returned to the selected quote, then the shell auto-started Getting Started and its first step navigated to Dashboard. Automatic onboarding now runs only on its own Dashboard start page. Other destinations keep their path, query and form context and do not consume the first-use flag; the Help menu still starts a tour explicitly. The automatic-attempt key is scoped to workspace and user.

A real AuthProvider/Login/Layout/TourProvider/TourHighlight/data-router integration test includes delayed permission loading and fresh preference storage. Both `/quotes?id=1` and `/work-orders/new` deterministically failed to `/` before the fix and pass afterwards. The related eight-suite run passed 57 tests; both TypeScript checks and zero-warning ESLint passed. Fresh Chromium acceptance then confirmed that the selected quote URL survives sign-in and onboarding effects. The fix changes onboarding navigation; the existing single login handoff remains.
