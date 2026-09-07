# Werco ERP/MES UX implementation backlog

Prepared 2026-09-06 from the five current source reviews of **origin/main `0a383b8`**, inspected at `/tmp/werco-ux-audit-current`, plus the root audit’s live observations. The older workspace checkout was 317 commits behind and is not the evidence baseline. This document defines proposed work; no product changes or production write tests were performed.

There are **25 consolidated work packages** below. P1 packages come first because they concern incorrect task scope, blocked progression, inaccessible records, or misleading operational state. Packages combine duplicate findings without treating the shared pattern as proof that every implementation is broken.

**Owners:** FE = frontend, BE = backend/integration, QA = outcome verification. The listed lead owns delivery; supporting owners are explicit. **Size:** S = a constrained interaction or contract change; M = several related components/endpoints; L = a lifecycle or cross-module change. Sizes express relative effort, not delivery dates; confirm them after inspecting current branch state. Accessibility and responsive checks apply to every changed interaction.

**Evidence:** “Source” means the current code path/contract was traced, not that the failure was reproduced in production. “Live” is limited to the cited read-only observations. The session timer behavior remains a verification item, and PO delivery remains an integration question. No receipt, shipment, quote conversion, MRP processing, recovery email, or other business write outcome was tested live.

## P1 packages

### UX-01 — Make scheduling selection match the visible job scope

**Lead FE · Support QA · Size S · Evidence Source · Findings PROD-01**

Select jobs from the displayed filtered collection, and disclose any retained selection outside the current filter. Apply the same scope to the table checkbox and “Select visible.”

Acceptance checks:

- With 12 jobs and 3 matching a work-center/search filter, both selection controls select exactly 3; bulk mutation payloads contain exactly those 3 IDs.
- Changing filters cannot silently expand the mutation scope. Any retained hidden selection is counted and described before applying an action.
- Keyboard selection and row-selection state agree with the visible count.

### UX-02 — Persist the exact work-order operation preview the user reviewed

**Lead FE · Support BE, QA · Size M · Evidence Source · Findings PROD-02, PROD-06**

Track operation-list overrides independently of individual row edits. Distinguish “use released routing” from an intentionally supplied operation list. Bind readiness/routing previews to the current part and quantity, with separate checking, missing, and failed states.

Acceptance checks:

- Delete one auto-populated operation, make no other changes, create the WO: the deleted operation stays absent. Removing all either validates clearly or honors an explicitly permitted empty list.
- Select part A then B with reversed response order: only B’s readiness/operations can be reviewed or submitted.
- A routing/readiness 500 never claims that no released routing exists. Submission waits for the current check or an explicitly supported override.

### UX-03 — Convert quote lines into the intended work orders and quantities

**Lead BE · Support FE, QA · Size L · Evidence Source · Findings QUOTE-02**

Define and display a conversion plan mapping each eligible quote line to its part, quantity, target WO, and deferred/service scope. Persist line-level conversion references and explicit ordering; derive overall conversion status from the handled scope.

Acceptance checks:

- A custom/setup first line of quantity 1 followed by a linked part line of quantity 20 produces quantity 20 for that part.
- Multiple linked lines either produce the selected WOs or remain visibly pending. No line disappears merely because another converted.
- Reordering quote lines cannot change the conversion meaning; generated WOs link to their source lines and appear in the success/review view.
- Verify only with staging/local fixtures; no customer quote or production WO conversion is part of this audit.

### UX-04 — Keep remaining quantities actionable after partial shipments

**Lead BE · Support FE, QA · Size L · Evidence Source · Findings OPS-02**

Define completed, reserved, shipped, and remaining quantities and the difference between manufacturing completion and fulfillment closure. Keep remaining units in the shipping workflow and offer “Create remaining shipment.”

Acceptance checks:

- Creating/dispatching 2 of 10 completed units leaves 8 visible and available for a subsequent shipment.
- Cancellation releases the correct reservation; pending shipment quantities are counted once; cumulative over-shipping stays blocked.
- WO and fulfillment status clearly describe partial dispatch rather than silently implying all units shipped.

### UX-05 — Repair warehouse navigation and preserve record query/history context

**Lead FE · Support QA · Size M · Evidence Live for warehouse defect; Source otherwise · Findings OPS-01, PROD-10, REPORT-02**

Use independent parent/child route state for Warehouse and Receiving. Establish consistent URL contracts for BOM/Routing selection and Reports tab/period, preserving unrelated parameters. Add catalog part/revision/status search where the existing record selector only scrolls.

Acceptance checks:

- Warehouse → Receiving → Inspection Queue and History stay within Receiving; refresh, copied URL, Back and Forward restore the same nested view.
- BOM/Routing part-and-revision search finds the intended record without scanning; selecting it updates a stable URL that restores correctly.
- Reports tab/period round-trip through refresh/share/Back/Forward, with invalid values falling back safely and other query parameters preserved.

Live evidence: the root audit selected Inspection Queue and observed `/warehouse?tab=queue` displaying Inventory. This read-only navigation failure is confirmed; related writes remain untested.

### UX-06 — Make “Process,” “Send,” and “Done” describe the actual side effect

**Lead BE · Support FE, QA · Size L · Evidence Source; PO delivery requires integration verification · Findings PROD-03, QUOTE-04, OPS-11**

Give MRP recommendations an explicit create-document or reviewed-only outcome. Align quote/PO issuance wording with actual delivery: manual marking is distinct from queued/sent/failed transport. Resolve the PO operational-event consumer before claiming that sending is absent or implemented.

Acceptance checks:

- MRP processing either creates/links the correct supply draft or clearly states that no supply document was created and identifies remaining work. “Done” cannot conceal manual follow-up.
- Quote “Mark as sent” copy reflects the traced status-only endpoint unless actual dispatch is implemented.
- Trace the PO event through any worker/provider; label the UI based on verified behavior. Actual sending includes recipient/document review and observable delivery outcomes.
- Integration tests use a fake provider or sandbox and send no messages to real customers/vendors.

### UX-07 — Complete the calculator → quote review → approval workflow

**Lead FE · Support BE, QA · Size L · Evidence Source · Findings QUOTE-01, QUOTE-03, QUOTE-05, QUOTE-07**

Persist an immutable calculation input/result snapshot and open a populated quote review from “Create Quote.” Mark results stale after input edits. Provide full saved-quote detail and supported draft corrections. Open the specific quote returned by RFQ approval and make Pending’s next permitted action explicit.

Acceptance checks:

- Calculate → Create Quote carries quantity, price, lead time and breakdown without retyping; cancelling review preserves the calculation.
- Changing quantity, geometry, material or rush marks results stale; displayed quantity and prices always belong to one snapshot. Stale results cannot be promoted without recalculation/review.
- RFQ approval opens the returned quote ID with an actionable next lifecycle step; a saved quote exposes lines, notes, terms and supported corrections.
- Successful create opens a stable quote URL; sending/conversion behavior aligns with UX-03 and UX-06.

### UX-08 — Route serial traces correctly and make investigations navigable

**Lead FE · Support BE, QA · Size M · Evidence Source · Findings OPS-03, OPS-12**

Dispatch typed lot/serial search results to the correct endpoint. Separate search/detail loading, add explicit no-match recovery, link related receipts/POs/WOs/shipments/NCRs, and retain query/selection in the URL.

Acceptance checks:

- Lot-only, serial-only and mixed search results load the matching endpoint and record type; serials never display as lots.
- A one-result search remains loading until detail arrives; zero matches explicitly identify the query and offer recovery.
- Traversing related records and returning preserves search context; copied trace URLs restore the selected entity.

### UX-09 — Add NCR/CAR investigation and resolution detail

**Lead FE · Support BE, QA · Size L · Evidence Source · Findings OPS-05**

Give every NCR/CAR a linkable detail view with full problem description, owner, due dates, evidence, disposition/corrective action and history. Expose authorized progress/closure actions with validation. Preserve existing FAI detail rather than treating it as missing.

Acceptance checks:

- Each NCR/CAR opens from list, keyboard and stable URL; complete descriptions and evidence are readable.
- Authorized users can investigate, update and close using the supported lifecycle; missing required closure evidence is explained locally.
- Returning preserves list scope; Void remains separate from ordinary resolution.

### UX-10 — Make record history and query pagination complete

**Lead BE · Support FE, QA · Size M · Evidence Source · Findings OPS-04, QUOTE-06, REPORT-01**

Use server-side filtering/pagination with truthful totals, or a demonstrably complete fetch strategy, for Quality and Quotes. Expose converted/expired quote history. Make every Employee Time entry reachable and clarify visible versus full totals.

Acceptance checks:

- The 101st matching NCR/CAR/FAI and quote are reachable; filters search the full documented scope rather than only the first fetched page.
- Converted/expired quotes can be browsed without knowing a record ID; empty copy distinguishes no open work from no history.
- For 11+ employee entries, all records are reachable; visible/full totals and counts reconcile without silent truncation.
- Do not apply the first-100 defect to Parts/Documents, whose services already autopaginate.

### UX-11 — Correct BOM multi-level rendering and support component edits

**Lead FE · Support BE, QA · Size M · Evidence Live for multi-level display defect; Source for editing gap · Findings PROD-04, PROD-09**

Type and consume the BOM explode response consistently, then add supported draft component editing while preserving line identity and metadata. Use the same behavior in the standalone BOM and Part BOM tab.

Acceptance checks:

- A nested BOM displays matching components and quantities in both views; loading/failure does not render a valid empty BOM.
- Correcting a draft quantity requires no delete/recreate and preserves notes, torque, scrap, optional status and installation instructions.
- Save/cancel/validation retain input; released BOM editing explains the supported revision/unrelease policy.

Live evidence supplied by root: Part `TEST-CLQA-001`, released Rev A, displayed one component (`WERCO-001-01`, quantity 1) in Single Level; switching to Multi-Level displayed “No items to display” while the header retained one item (screenshots 28–29). No BOM mutation was performed.

### UX-12 — Make failure, cached data, counts, and sync status truthful

**Lead FE · Support BE, QA · Size L · Evidence Source · Findings C3, C4, C12, PROD-05, SPC-UX-01, OPS-09**

Adopt one data-state contract for loading, current, stale, unavailable and genuinely empty. Apply it to Dashboard widgets/cache, Setup readiness, WO detail secondary sections, Maintenance and Calibration summary scope. Render core dashboard data independently, preserve last-good content on background failure, and derive shell synchronization status from actual connection/data evidence.

Acceptance checks:

- Fail each auxiliary request independently: unknown NCR/calibration/stock/maintenance/blocker/document state never becomes zero/no issues; Setup never asserts production readiness while health is unavailable.
- Delay an auxiliary request: core data renders when ready; failed refresh retains useful prior data with scope/time and Retry. Error-based cached fallback differs from healthy 304 reuse.
- Disconnect/reconnect: static LIVE/SYNC OK labels become truthful connection/freshness states with last successful synchronization.
- Filtering Calibration changes table results while overall status counts retain documented scope. Choosing a loading skeleton does not imply measurements not yet taken.

### UX-13 — Fix picker overlay layering and finish modal/accessibility contracts

**Lead FE · Support QA · Size M · Evidence Live for Visitor Purpose layering; Source for other adoption gaps · Findings C2, C9, OPS-06**

Unify modal/picker portal and focus-stack behavior. Wire accessible names through ConfirmDialog/InputDialog, migrate the remaining session overlay, make hidden navigation inert, and complete tab/panel relationships. Put receipt/inspection validation inside the active dialog.

Acceptance checks:

- Visitor Purpose and scrap-reason pickers render above their modal and work by pointer/keyboard at small heights; Escape closes the popup before the form.
- Hidden mobile navigation cannot receive focus; drawer/session dialogs have predictable entry/return focus and computed accessible names; selected tabs identify their panels.
- Invalid receipt quantity, over-receipt, missing rejection notes and server failures appear inside the dialog, focus/announce the relevant field and preserve values.
- Preserve existing shared Modal focus trapping and Toast live regions; do not rebuild those as if absent.

Live evidence supplied by root: the Add visit Purpose picker expanded with six options in its DOM listbox, but the list rendered behind the modal and was visually inaccessible (screenshot 30). Source layering is SelectField z-50 below Modal z-60. No visit was submitted.

### UX-14 — Restore account recovery, reauthentication destinations and stable session warning

**Lead FE · Support BE, QA · Size M · Evidence Source; session timer is verification-first · Findings C1, C7, C15**

Replace the inert “Forgot password?” control with a real reset or actionable administrator recovery flow. Carry a validated internal return path through sign-in/expiry and explain session reasons. Verify the inferred warning/timer feedback loop before changing it, then use one expiry deadline if confirmed.

Acceptance checks:

- Mouse/keyboard recovery activation opens a usable flow, preserves the account identifier and states next steps.
- Signed-out deep links retain path/query after successful authorized sign-in; denied targets have an explained fallback; kiosk behavior remains deliberate.
- Test the actual AuthProvider with fake timers: at the warning threshold the prompt remains stable, inactivity reaches expiry, explicit extension renews once, and rerenders do not extend the deadline.
- No real recovery message is sent as part of audit verification.

## P2 and P3 consolidation packages

### UX-15 — Standardize pending writes and recoverable partial outcomes

**Lead FE · Support BE, QA · Size L · Evidence Source · Findings C11, PROD-08, PROD-11, OPS-07, SPC-UX-06; quote create/send pending noted in quoting report**

Adopt scoped pending guards/loading buttons in the identified receipt, inspection, shipment, inventory, NCR/CAR, routing, schedule, MRP, maintenance, tool, downtime, OEE and quote handlers. Preserve input on failure. Keep actionable warnings/errors available, and surface per-record bulk outcomes. Make cross-machine move-and-schedule atomic or reconcile partial success explicitly.

Acceptance checks:

- Delayed responses show progress; repeated click/Enter sends one logical action. Server idempotency is checked for non-idempotent creates rather than assumed from UI gating.
- Failures preserve values and identify the affected entity. Long/partial-success warnings remain available with a next action until dismissed/resolved.
- Bulk scheduling identifies each failed WO/reason and retries only failures; failure after machine reassignment shows the actual saved state.
- Existing protected actions (for example BOM create, PO Send and document Delete) retain their working behavior. Missing pending state alone is not evidence of duplicate persisted rows.

### UX-16 — Replace raw entity IDs with searchable validated selections

**Lead FE · Support BE, QA · Size M · Evidence Source · Findings OPS-08, OPS-14, SPC-UX-02, SPC-UX-03**

Build on current ComboBox/picker primitives for supplier, employee, work center, WO and part selection. Use affected-part chips in ECOs, explicit eligibility and human-readable confirmation. Correct Inventory’s required-part placeholder/guard. Full supplier audit findings are handled in UX-20.

Acceptance checks:

- Create scorecard/audit, maintenance job, tool checkout, certification/training and ECO without knowing a database ID; code/name search shows the chosen identity/status.
- Empty Inventory part selection cannot submit and identifies/focuses the field.
- ECO inputs never silently discard `ABC123` or parse `12ABC` as ID 12; invalid scope is named and editable, and saved scope exactly matches displayed selected parts.
- Inactive/ineligible options explain their state; identifiers remain internal payload details.

### UX-17 — Open purchase orders for review and supported draft correction

**Lead FE · Support BE, QA · Size L · Evidence Source · Findings OPS-10**

Make PO row/number activation open a stable detail view, with vendor, lines, dates, totals, history and receipt progress. Keep Print explicit; support corrections under a defined draft/issued-state policy.

Acceptance checks:

- Row activation does not automatically launch printing; a stable URL identifies the selected PO.
- Authorized users can reopen/correct a draft and review saved results, with governed changes after issuance and read-only final states.
- List filters survive return; Print and Send remain explicit actions, and sending wording follows UX-06.

### UX-18 — Preserve dirty work across all navigation paths

**Lead FE · Support QA · Size M · Evidence Source · Findings C6**

Extend existing refresh/Cancel dirty protection to sidebar, breadcrumbs, in-app navigation and browser Back. Use a supported router-level leave guard or coherent navigation policy, with save/discard/stay and scoped draft restoration where warranted.

Acceptance checks:

- Edit WorkOrderNew and PartEdit, then try sidebar, breadcrumb, Back and refresh: edits require deliberate discard or can be restored.
- Successful saves clear dirty state, failed saves preserve it, and staying keeps focus/input intact.
- Existing beforeunload/confirmDiscard handling is extended rather than described as absent; users are not prompted after unchanged or successfully saved forms.

### UX-19 — Align role-based discovery and help with the current user/workstation

**Lead FE · Support BE, QA · Size M · Evidence Source · Findings C5, C16**

Use common route/action permission metadata across sidebar, mobile navigation and global quick actions. Make help reachable on mobile, scope tour/adaptive-hint state by user/company, and route hints to the relevant action rather than the current create page.

Acceptance checks:

- Test built-in roles and a custom override: offered shortcuts open allowed destinations; empty groups disappear; deliberately discoverable locked features explain access before navigation.
- Preserve backend authorization and useful denied-deep-link recovery.
- User A’s completed tour/dismissal never implies User B completed it; mobile help is reachable; “Open blockers” opens actual blockers rather than reopening New WO.

### UX-20 — Add readable document/revision and supplier-audit detail

**Lead FE · Support BE, QA · Size M · Evidence Source · Findings OPS-13, OPS-08 (full findings)**

Provide document preview/detail with current/older revision navigation and associated historical part identity. Cache lookup catalogs independently of document filters. Allow full supplier audit findings and follow-ups to be read without export.

Acceptance checks:

- PDFs/images can be inspected before download, revision lineage is clear, and inactive linked parts still display their identity.
- Document type changes refresh relevant results without redundantly refetching all lookup catalogs.
- Long supplier audit findings are fully readable with keyboard/pointer, including follow-up context. Existing document pagination/mobile/error states remain intact.

### UX-21 — Reconcile Dispatch Board safely with other stations

**Lead FE · Support BE, QA · Size M · Evidence Source · Findings C13, PROD-12**

Subscribe to relevant production/run-order changes or add guarded background/focus refresh. Display last successful reconciliation and announce deferred updates during active drags/edits. Reuse current focus-preserving reorder and explicit stale-read handling.

Acceptance checks:

- Start/complete/reorder in a second fixture session: the other board refreshes within an agreed interval or clearly announces pending updates with timestamp.
- No incoming refresh silently moves cards under an active drag or destroys keyboard focus.
- Connection, cached-data age and failed refresh are distinct, aligned with UX-12; existing machine/changeover/keyboard/overflow affordances remain.

### UX-22 — Make global search one reliable keyboard and query model

**Lead FE · Support QA · Size M · Evidence Source · Findings C8**

Reset on opening, not when delayed recent-items data changes. Key results to the current query, expose service failure separately from no matches, and unify recents/actions/results into one announced keyboard selection model.

Acceptance checks:

- Delay recents while typing: entered query remains. Return A after typing B: only current B results can be activated.
- API failure provides retryable unavailable state; valid no matches provides search recovery.
- With zero recents, arrows/Enter activate each visible quick action; long lists scroll active selection into view and expose an accessible input/result relationship.

### UX-23 — Reconcile notification bell/inbox and clarify bulk-read scope

**Lead FE · Support BE, QA · Size M · Evidence Source · Findings C14**

Share notification query state or invalidate both surfaces after writes. Keep unread-only lists and counts consistent, make global versus filtered “Mark all read” scope explicit, and reject superseded filter responses.

Acceptance checks:

- Read in either surface: both lists and unread badge reconcile without waiting for the next routine poll.
- Unread-only rows leave the result set; pagination/counts remain truthful.
- Bulk-read enablement uses the count for its stated scope, including other pages; rapid filter changes cannot show older results under new filters.

### UX-24 — Improve mobile task reachability, visual hierarchy and consistent cues

**Lead FE · Support QA · Size M · Evidence Live for specified layout observations; Source for tokens/priorities · Findings C10, PROD-13; LIVE-MOBILE-WO, LIVE-TIMECLOCK**

Correct New WO’s narrow-screen action overflow; make the shop-floor queue reachable without first traversing every machine; use tested dark-surface identifier/active-tab colors and centralized priority definitions. Retain responsive card/table and shared-component improvements already present.

Acceptance checks:

- At 390 CSS px width, New WO actions and required fields fit within the viewport without horizontal document overflow and remain keyboard/touch reachable. Test selected-part and validation states, not just an empty form.
- On Time Clock with 24 machines, the current station and actionable queue are reachable promptly through compact selection/search/disclosure; users can still deliberately browse all stations. Avoid imposing a numerical scroll target before product review.
- Verify rendered identifier/active-tab color pairs on actual surfaces against the selected target (normal essential text at least 4.5:1). Source token calculations alone are not a conformance claim.
- P1–P10 have consistent definitions/treatment across New WO, lists, Scheduling and Shop Floor; numeric urgency remains understandable without color.

Live evidence supplied by root: the narrow New WO action spanned x=303 to right=414.7 in a 390 px viewport; the 24-machine Time Clock placed its queue below y=1252. These are observed layouts, not evidence that a production save failed.

### UX-25 — Keep selected scope, displayed data and mutation refreshes aligned

**Lead FE · Support QA · Size M · Evidence Source · Findings PROD-07, SPC-UX-04, SPC-UX-05**

Key fetched data by the selected MRP run, tool tab and OEE filters. Refresh the same scoped dataset after tool mutations. Prevent obsolete responses from replacing current selections, and label any intentionally retained prior-scope data explicitly. Reuse UX-12’s data-state contract.

Acceptance checks:

- Selecting MRP run B while A’s response arrives late never enables A’s actions under B’s heading.
- Checking a tool in from Checked Out removes it from that queue without filling the tab with all tools; replacement/inspection tabs retain their meaning after writes.
- Change OEE period/work center, then fail trends/records: old series cannot appear as matching the new scope. The actual shown scope, update time and Retry are clear; later obsolete responses are ignored.

## Coverage and dependencies

Every current numbered source finding is represented. A repeated reference denotes a deliberate split of a compound finding, not an extra defect or estimate.

| Source report | Finding coverage |
|---|---|
| Foundations | C1→14; C2→13; C3→12; C4→12; C5→19; C6→18; C7→14; C8→22; C9→13; C10→24; C11→15; C12→12; C13→21; C14→23; C15→14 (verify first); C16→19 |
| Production | PROD-01→01; 02→02; 03→06; 04→11; 05→12; 06→02; 07→25; 08→15; 09→11; 10→05; 11→15; 12→21; 13→24 |
| Operations | OPS-01→05; 02→04; 03→08; 04→10; 05→09; 06→13; 07→15; 08→16/20; 09→12; 10→17; 11→06 (verify integration); 12→08; 13→20; 14→16 |
| Specialist | SPC-UX-01→12; 02→16; 03→16; 04→25; 05→25; 06→15 |
| Quoting/reports | QUOTE-01→07; 02→03; 03→07; 04→06; 05→07; 06→10; 07→07; REPORT-01→10; REPORT-02→05 |

The failure/staleness contract in UX-12 supports UX-21/25; overlay/picker work in UX-13 supports UX-16. Pending/error feedback in UX-15 should be reused by lifecycle packages. Quote review (UX-07), conversion (UX-03) and action semantics (UX-06) need one agreed quote-state contract. URL/query work (UX-05/10) should use the existing working WorkOrders patterns. These are implementation relationships, not reasons to delay the small P1 selection/response-shape repairs.

Source reviews: [Foundations](source-reviews/foundations.md), [Production](source-reviews/production.md), [Operations](source-reviews/operations.md), [Specialist](source-reviews/specialist.md), [Quoting and reports](source-reviews/quoting.md). The main audit supplies screenshots and current source references. Do not resurrect findings explicitly marked fixed in these reports, including old native prompt/cancel bugs, missing shared modal focus trapping, MRP ten-row truncation, missing WorkOrders URL filters, or already protected BOM submissions.

## Review and implementation verification

For each package, implement and test against a current isolated checkout rather than the stale workspace snapshot. Use local/staging fixtures for writes, failure injection, 101+ records, partial quantities, reversed response order, and cross-session changes. Verify the user outcome and contract rather than merely mirroring component state. Capture changed desktop/mobile states and keyboard behavior, including empty, error and pending states. Measure performance before assigning timing claims. This backlog is a reviewable proposal and is not evidence that fixes have shipped.
