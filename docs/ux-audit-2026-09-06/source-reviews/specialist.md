# Werco current specialist workflow UX review

> Persistent source review. Source citations are pinned to [audited commit `0a383b8c63926dd02e2a37c5153b43a3b095631b`](https://github.com/jwerthen/Werco-ERP-MES/tree/0a383b8c63926dd02e2a37c5153b43a3b095631b). The document is stored in the working checkout, but its evidence refers only to this pinned current revision, originally extracted at `/tmp/werco-ux-audit-current`. Bare continuation line numbers refer to the named source in their paragraph. This is source-review evidence, not a claim of visual coverage.

Read-only source review against **origin/main 0a383b8** at `/tmp/werco-ux-audit-current`. Paths/lines below refer to that tree. No visual observation or production mutation is claimed. Scope: Maintenance, OEE, Downtime Tracking, SPC, Engineering Changes, Tools, Operator Certifications.

## SPC-UX-01 — P1: Maintenance suppresses failed loads into reassuring empty states

Evidence: [frontend/src/pages/Maintenance.tsx:122](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Maintenance.tsx#L122)–125 catches each API rejection and replaces it with null/[] before Promise.all resolves. The outer error handler at 130–131 therefore does not see ordinary request failures. Schedules then shows 'No schedules configured' and Create CTA at 528–540.

Impact: A failed schedule/maintenance-WO request looks like no maintenance has been configured or no work is pending, even when overdue maintenance exists. A partial failure is handled as successful emptiness.

Recommendation: Keep independent result/error states per request, retain successfully loaded sections, and show Retry for any failed section. Distinguish no work from unknown.

Acceptance: Schedule/WO/dashboard failure separately produces a visible failed-state indicator; data failure cannot produce 'No schedules configured'; retry replaces only that section; loaded other sections remain usable.

## SPC-UX-02 — P2: Internal IDs are user-facing across specialist tasks

Evidence:
- Maintenance schedule and WO ask for Work Center ID at [frontend/src/pages/Maintenance.tsx:595](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Maintenance.tsx#L595)–597 and 661–663.
- Tool checkout asks for raw Work Order ID at [frontend/src/pages/ToolManagement.tsx:558](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/ToolManagement.tsx#L558)–560; operator is arbitrary 'name or ID' text at 553–555.
- Certification/training ask for User ID at [frontend/src/pages/OperatorCertifications.tsx:780](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/OperatorCertifications.tsx#L780)–782 and 872–874, and training Work Center ID at 905–907.
- ECO form requires comma-separated Affected Part IDs at [frontend/src/pages/EngineeringChanges.tsx:804](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/EngineeringChanges.tsx#L804)–815.

Impact: Users must leave their job to discover database keys, increasing selection mistakes and broken links. The shared FormField styling improves presentation but doesn't make this data entry meaningful.

Recommendation: Shared searchable pickers for employee, work center, WO and part, showing business identifiers/name/status; context can prefill selection. ECO uses multi-select affected-part chips with revision and description.

Acceptance: Complete each workflow without knowing a database ID; entity is previewed by human-readable identity before save; inactive/ineligible choices explain their state; searching works by code/name.

## SPC-UX-03 — P2: ECO silently discards or truncates invalid affected-part entries

Evidence: [frontend/src/pages/EngineeringChanges.tsx:516](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/EngineeringChanges.tsx#L516) parses each comma-separated token with parseInt and drops NaN; line 517 only includes the field when parsed IDs remain. [EngineeringChanges.tsx:519](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/EngineeringChanges.tsx#L519) proceeds to create and line 521 announces success.

Impact: Entering 'ABC123' can silently yield an ECO with no affected parts; '12ABC' parses as ID 12. The user sees success even though their intended part scope was not preserved.

Recommendation: Use the entity multi-select above; until then validate every token strictly and list invalid entries without submitting. Show the final affected-part list on confirmation/detail.

Acceptance: Mixed valid/invalid tokens never silently lose entries; an invalid token is named and editable; the saved ECO's scope matches the displayed selected parts exactly.

## SPC-UX-04 — P2: Tool actions refresh a different dataset than the selected tab

Evidence: [frontend/src/pages/ToolManagement.tsx:111](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/ToolManagement.tsx#L111)–126 loads tab-specific checked-out/replacement/inspection tools. After create/checkout/checkin, handlers call generic loadData at 155, 170 and 185, which always fetches all active tools at 98 and replaces the same tools state at 101. activeTab does not change, so the tab-specific effect does not rerun. Filtering at 128–143 filters only optional status/type/search, not the active tab.

Impact: Checking a tool in from Checked Out can replace that queue with all tools while the tab label still says Checked Out. Replacement/inspection queues can likewise show unrelated records after an action.

Recommendation: Refresh dashboard separately and refetch the currently selected tab's dataset, or use a query key containing activeTab so mutation invalidation preserves scope.

Acceptance: Check in from Checked Out removes only the returned tool from that queue; other tabs remain semantically correct after writes; retries/refreshes show data matching the selected tab.

## SPC-UX-05 — P2: OEE can retain stale data under newly selected filters without an error

Evidence: [frontend/src/pages/OEE.tsx:206](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/OEE.tsx#L206)–242 uses allSettled, but failures of trend/record calls only log (234/241) and leave previous trends/records state intact. Changing date/work-center invokes loadData through dependencies at 249–253; title is derived from current range (333 onward) and chart renders existing trends at 579–587. Dashboard failure sets loadError, but comment at 204–205 documents render gate is combined with !dashboard, so cached data remains visible.

Impact: After loading one period/work center successfully, a failed filter fetch can show the previous period's values under a new scope/title. That risks an incorrect operational interpretation.

Recommendation: Separate loading/error/data provenance per dashboard section, with a displayed 'Showing previous …; refresh failed' state or hide mismatched stale series. Cancel/ignore superseded requests to avoid races during rapid filter changes.

Acceptance: Force trends/records to fail after changing filters; no old series is presented as matching the new filter. Scope, last update and Retry are visible; a later stale response cannot overwrite a more recent selection.

## SPC-UX-06 — P2: Specialist actions repeat the missing save-progress pattern

Evidence: Maintenance create/start/complete at [frontend/src/pages/Maintenance.tsx:150](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Maintenance.tsx#L150)–195, with buttons disabled only for missing fields at 645/702; tools create/checkout/checkin at [frontend/src/pages/ToolManagement.tsx:145](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/ToolManagement.tsx#L145)–185; downtime create/resolve at [frontend/src/pages/DowntimeTracking.tsx:226](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/DowntimeTracking.tsx#L226)–261; OEE record create at [frontend/src/pages/OEE.tsx:255](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/OEE.tsx#L255) onwards. These handlers lack per-action pending state despite async writes.

Impact: During a slow request operators see no difference between saving and idle; repeated input causes confusing retries/duplicate logical requests.

Recommendation: Reuse existing LoadingButton/pending state, retain values on failure and announce success with entity context. This is one shared initiative, not separate bespoke fixes for every module.

Acceptance: Delayed writes clearly show progress and accept one logical submission; failed save preserves entered values; successful action refreshes the current scoped view.

## Useful existing specialist patterns to extend

- Engineering Changes already has detail view and named lifecycle actions with busy state; do not describe it as a static table. The missing piece identified here is affected-part selection/validation.
- SPC has substantial recent improvements: explicit dashboard/detail/parts error states, per-sample inline numeric validation ([frontend/src/pages/SPC.tsx:468](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/SPC.tsx#L468)–489), unit-aware subgroup capture, loading state, and success identifying recorded subgroup (495–536). No additional P1 UX finding established in this bounded source review; concurrent subgroup allocation is mentioned in code but needs a separate data-integrity review rather than claiming an observed UI defect.
- Operator certification/training creation already has pending state and success/error toasts at 272–351. Its entity selection is the substantiated UX gap here, not missing save feedback.
- Downtime now has work-center dropdowns and real load error state; use its selector pattern as a starting point for maintenance instead of requesting raw Work Center ID.
