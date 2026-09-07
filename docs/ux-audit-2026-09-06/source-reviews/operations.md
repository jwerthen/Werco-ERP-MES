# Werco current operations UX source review

> Persistent source review. Source citations are pinned to [audited commit `0a383b8c63926dd02e2a37c5153b43a3b095631b`](https://github.com/jwerthen/Werco-ERP-MES/tree/0a383b8c63926dd02e2a37c5153b43a3b095631b). The document is stored in the working checkout, but its evidence refers only to this pinned current revision, originally extracted at `/tmp/werco-ux-audit-current`. Bare continuation line numbers refer to the named source in their paragraph. This is source-review evidence, not a claim of visual coverage.

**This report replaces the stale-checkout draft in full.** Verified against current origin/main **0a383b8**, extracted read-only at `/tmp/werco-ux-audit-current` (2026-09-06). Every path/line below refers to that tree, not the older working checkout. All findings are **source-verified** with high confidence in the code path; root is handling live reproduction/screenshots. No production writes or UI visual claims were made by this reviewer.

Scope: Warehouse, Inventory, Purchasing, Receiving, Shipping, Quality, Documents, Traceability, Supplier Scorecards, Calibration, with supporting service/backend review. P1 = blocked/wrong workflow or hidden unresolved work; P2 = substantial friction or feedback/accessibility gap.

## OPS-01 — P1: Nested warehouse tabs overwrite the outer tab

Evidence: [frontend/src/pages/Warehouse.tsx:29](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Warehouse.tsx#L29)–35 accepts only `tab=receiving` or `shipping`, otherwise selects Inventory; it embeds Receiving at line 81. [frontend/src/pages/Receiving.tsx:1430](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Receiving.tsx#L1430)–1432 writes the SAME URL key with `receive`, `queue`, `history`.

Impact: Warehouse > Receiving > Inspection Queue/History changes the URL to a value the parent treats as Inventory, unmounting Receiving. This is still present in current source.

Recommendation: Distinct keys (`tab=receiving&view=queue`) or route segments; preserve unrelated filter parameters.

Acceptance: Nested receive/queue/history stay inside the selected warehouse module; refresh, share URL, Back/Forward all restore the intended view.

## OPS-02 — P1: Partial shipments disappear from the Ready to Ship workflow

Evidence: Manual Create Shipment permits partial quantity at [frontend/src/pages/Shipping.tsx:569](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Shipping.tsx#L569)–578. [backend/app/api/endpoints/shipping.py:190](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/shipping.py#L190)–196 removes a WO from Ready to Ship after ANY non-cancelled shipment exists, irrespective of quantity. Shipping then closes the WO once at [shipping.py:392](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/shipping.py#L392)–394. Current backend explicitly supports later partial shipments in its comments/guards ([shipping.py:363](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/shipping.py#L363)–370), so this is an end-to-end discoverability/queue mismatch, not a claim that subsequent API shipments are impossible.

Impact: Completing 10 units and creating a shipment of 2 removes the remaining 8 from the page's ready queue. After dispatch, the WO is closed even though the remaining units are not shipped. Users cannot continue the apparent split-shipment path from this page.

Recommendation: Track completed, reserved on pending shipments, shipped and remaining quantities; keep remaining units actionable; define completion versus shipment closure deliberately. Add a clear Create Remaining Shipment action.

Acceptance: A 2-of-10 shipment leaves 8 visible/actionable; a later shipment is easy to create; cancellation releases reservations; cumulative over-shipping remains blocked; WO/fulfillment status clearly reflects partial dispatch.

## OPS-03 — P1: Serial trace results are routed to the lot endpoint

Evidence: Current backend search emits `type: serial` at [backend/app/api/endpoints/traceability.py:578](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/traceability.py#L578)–583. [frontend/src/pages/Traceability.tsx:84](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Traceability.tsx#L84)–86 and line 169 call `loadLotTrace(number)` for any result type; line 100 calls `api.traceLot`. The correct `api.traceSerial` exists at [frontend/src/services/api.ts:3554](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L3554).

Impact: Selecting a serial may render an unknown/nonexistent lot view rather than the individual unit's trace history.

Recommendation: Dispatch based on typed search result, with appropriate Lot/Serial title, quantities and relationship data.

Acceptance: Lot-only, serial-only and mixed results load the right endpoint and record; a serial is never labelled as a lot; cert/heat searches preserve trace context.

## OPS-04 — P1: Quality list pagination hides records beyond the first 100

Evidence: [frontend/src/pages/Quality.tsx:287](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quality.tsx#L287)–289 calls `getNCRs/getCARs/getFAIs` with no pagination. Services [frontend/src/services/api.ts:2459](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L2459), 2487, 2502 make one request. Backend defaults limit 100 at [backend/app/api/endpoints/quality.py:81](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/quality.py#L81), 416, 558. NCR filter at [Quality.tsx:509](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quality.tsx#L509) operates on this already-truncated array; DataTable's 25-row pages ([Quality.tsx:941](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quality.tsx#L941), 982, 1032) only paginate that array.

Impact: Older open NCRs/CARs/FAIs can be inaccessible and filtered results disagree with total open summary counts. Client-side pagination creates a misleading impression of complete coverage.

Recommendation: Server-side query/filter/pagination with total count, or explicitly fetch all unresolved records before client pagination.

Acceptance: The 101st older matching record is reachable; Open searches all records, not only recent 100; page count and summary agree on documented scope. Parts/Documents already autopaginate in their services; do not apply this 100-record finding to those modules.

## OPS-05 — P1: NCR/CAR management remains creation/listing without lifecycle detail

Evidence: NCR columns [frontend/src/pages/Quality.tsx:531](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quality.tsx#L531)–595 are display fields plus optional Void action; table at 935–963 has no row handler/detail link. CAR columns 597–652 and table 976–1013 likewise have no detail/action. NCR/CAR create handlers at 306/345 exist; update services at [frontend/src/services/api.ts:2469](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L2469) and 2497 are available. FAI NOW has a working detail action at [Quality.tsx:1034](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quality.tsx#L1034) and should not be included in this finding.

Impact: From Quality Management, users cannot read full NCR/CAR problem details, investigate root cause, assign disposition/action, or verify closure. Void is available where ordinary resolution is missing.

Recommendation: Linkable NCR/CAR detail with ownership, due dates, evidence, disposition/corrective action and history, reusing existing backend actions.

Acceptance: Every NCR/CAR has an accessible detail link; authorized users can progress and close the record with appropriate validation; returning preserves filters. Keep the existing FAI detail pattern consistent.

## OPS-06 — P2: Receive/inspect validation is displayed behind the active modal

Evidence: Current [frontend/src/pages/Receiving.tsx:639](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Receiving.tsx#L639)–651 and 744–754 set page `error`; its only render is at 1367–1375, outside modal. Modals at 1894–2125 and 2128–2346 do not render this error. Invalid quantity/over-receipt/rejection notes leave the modal open.

Impact: Submit appears unresponsive because its explanation is in the obscured background, potentially above the viewport. The shared Modal improves keyboard behavior but does not move these errors into the dialog.

Recommendation: Field errors and an in-dialog summary, focus first invalid control, announce failures, retain entered values.

Acceptance: Invalid receipt quantity/over-receipt and missing rejection notes/defect visibly explain failure inside the dialog without closing it; server errors behave identically.

## OPS-07 — P2: Several high-frequency writes still have no in-flight feedback or submit guard

Evidence: [frontend/src/pages/Receiving.tsx:636](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Receiving.tsx#L636)–692 and its Button at 2119; inspection at 740–783 / 2340; [frontend/src/pages/Shipping.tsx:121](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Shipping.tsx#L121)–134 / 636; [frontend/src/pages/Quality.tsx:306](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quality.tsx#L306)–319, 345–365; [frontend/src/pages/Inventory.tsx:218](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Inventory.tsx#L218)–238. None carries a save/pending guard for those operations. Shared `Button` does not guard async work ([frontend/src/components/ui/Button.tsx:36](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Button.tsx#L36)–42); its own docs say to use LoadingButton for this. Note current PO Send, NCR Void, document Delete, and receipt correction have pending protections—do not overgeneralize to all writes.

Impact: Slow requests leave operators unsure whether a click registered and invite double entry. Backend protections vary; absence of a UI guard does not alone prove duplicate database writes.

Recommendation: Adopt existing LoadingButton with an immediate in-flight guard, outcome-specific success state, and preserved retryable input. Ensure receipt creation's server idempotency matches UI behavior.

Acceptance: Delayed API response disables repeated logical submit and announces progress; double-click produces one logical action; failure preserves fields and allows retry; success identifies the resulting receipt/shipment/record.

## OPS-08 — P2: Supplier scorecards/audits ask people for database IDs

Evidence: [frontend/src/pages/SupplierScorecards.tsx:629](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/SupplierScorecards.tsx#L629)–635 and 697–703 display required numeric Vendor ID input rather than supplier selection. Current errors ARE toasted at 226–249; the old silent-save issue is fixed. Audit table findings are truncated at 432, with no row detail handler at 594–610.

Impact: Managers need an internal identifier outside their current task; an arbitrary number is easy to mistype. Long supplier findings cannot be read from the desktop table without exporting.

Recommendation: Searchable supplier code/name picker, entry point from supplier detail, full audit detail/expand view including findings and follow-ups.

Acceptance: Create an audit/scorecard without knowing an ID; show the chosen supplier's identity before saving; full findings are readable with keyboard/mouse without export.

## OPS-09 — P2: Calibration summary counts collapse when the user filters

Evidence: [frontend/src/pages/Calibration.tsx:134](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Calibration.tsx#L134) fetches status-filtered equipment; counts at 258–260 derive from that subset; cards at 456–487 present Overdue, Due Soon and Current values and are now clickable filters.

Impact: Selecting Overdue makes the other summary tiles display zero even when current/due-soon equipment exists. A user trying to navigate between categories sees misleading counts.

Recommendation: Stable overall summary query independent of the filtered table; show result count alongside table. If deliberately scoped counts, label scope explicitly instead.

Acceptance: Clicking Overdue filters the table while Due Soon/Current totals remain accurate; navigating among tiles preserves global context.

## OPS-10 — P2: PO row interaction opens printing rather than an editable detail workflow

Evidence: [frontend/src/pages/Purchasing.tsx:1225](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Purchasing.tsx#L1225) and 1767 map card/row clicks to `handlePrintPO`; action list 1137–1162 offers Print, Send and Delete. [Purchasing.tsx:960](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Purchasing.tsx#L960) opens an autoprint URL. Legacy `/purchasing/:poId` is just redirect at [frontend/src/App.tsx:599](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/App.tsx#L599)–602. This component has a create-PO form but no edit-PO/detail flow; vendor editing is fully separate.

Impact: A buyer trying to inspect a saved draft launches a print flow. Correcting a date, vendor or line item after draft creation has no visible path from the PO list.

Recommendation: PO number/row opens a detail screen with lines, totals, vendor, status, change history and receipt progress. Keep Print an explicit secondary action, with editable drafts and governed changes after issuance.

Acceptance: Draft can be reopened, reviewed and corrected; row click does not unexpectedly print; stable PO URL identifies a particular order; fields become appropriately read-only after final states.

## OPS-11 — P2: PO 'Send to vendor' wording lacks verified delivery outcome

Evidence: Current confirm says `Send [PO] to the vendor?` at [frontend/src/pages/Purchasing.tsx:2718](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Purchasing.tsx#L2718). Backend [backend/app/api/endpoints/purchasing.py:1183](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/purchasing.py#L1183)–1212 changes status/date, emits an operational event, writes audit and returns 'PO sent'; no provider dispatch appears in this endpoint.

Impact: Users may infer actual supplier delivery from an issuance/state transition. A separate worker/provider could exist, so treat actual delivery as an integration validation question, not a proved absence of all delivery infrastructure.

Recommendation: If manual, 'Mark as sent' with method/date. If automated, recipient and document review plus explicit queued/sent/failed delivery feedback.

Acceptance: UI wording matches verified side effects; user can identify destination/method and delivery status; failure is distinguishable from successful issuance.

## OPS-12 — P2: Trace investigations have dead-end related records and no search result recovery

Evidence: [frontend/src/pages/Traceability.tsx:236](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Traceability.tsx#L236)–241 shows PO/cert as text, work orders at 267–268, shipments at 283–284, NCRs at 299–300, timeline references at 327–330 are plain text. State is local at 65–70. Successful zero-result search has no explicit empty message (search results only render if >1 at 162). Exactly one result calls unawaited load at 86, then outer finally clears loading at 91 before trace fetch completes.

Impact: Investigators copy numbers into other modules, cannot share/refresh a trace URL, and cannot distinguish a zero-match search from an ignored action. Loading can end before detail appears.

Recommendation: Typed linked entities, URL-based query/selection, Back to results, no-result guidance, separate search/detail loading with awaited detail fetch.

Acceptance: Lot → receipt/PO/WO/shipment/NCR traversal returns to same search context; copied URL restores record; no match explicitly reports query and recovery; loading remains truthful until detail is ready.

## OPS-13 — P2: Document workflows need preview and revision navigation

Evidence: [frontend/src/pages/Documents.tsx:251](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Documents.tsx#L251)–276 actions remain Download/Delete; revision is displayed at 210 and entered as text at 425. [Documents.tsx:102](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Documents.tsx#L102) loads active parts only; part identity is derived from that list at 193–195. Each type change reloads documents + full parts + types through Promise.all at 96–114. Current shared table DOES have pagination, clear filtered-empty copy, retry and mobile cards—those old problems are fixed.

Impact: Users download multiple candidates to inspect them; revision lineage/current status is not navigable here. A historical document associated with an inactive part displays '-'. Changing document type redundantly refetches full lookup catalogs.

Recommendation: In-app document preview/detail with explicit current revision and older versions; return associated part identity on document records regardless of current active state; cache lookups independently.

Acceptance: PDF/image can be inspected before download; current/older revision relationship is clear; inactive linked part still has its identity; changing filters refreshes results without redundant catalog loads.

## OPS-14 — P2: Inventory receipt permits an unselected required part through browser validation

Evidence: [frontend/src/pages/Inventory.tsx:836](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Inventory.tsx#L836)–837 uses required select with placeholder value={0}; handler at 218–226 directly calls API with no selected-part guard. String '0' satisfies native required. Purchasing now DOES validate vendor/line IDs at [frontend/src/pages/Purchasing.tsx:903](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Purchasing.tsx#L903)–915 and should not be cited as currently broken.

Impact: An operator completes other receipt fields, submits without choosing a part, and discovers the missing entity only from an API error.

Recommendation: Empty-string placeholder or explicit positive-ID validation and inline field guidance; searchable parts chooser for growing catalogs.

Acceptance: Unselected part cannot submit; field is identified and focused; part is selectable by code/name without scanning a full native dropdown.

## Already improved in current main: do not re-report as unresolved

- Pending inspections no longer age out after 30 days: Receiving calls getInspectionQueue() at 530; backend receiving.py:495/502 defaults to no cutoff.
- Receive Material now clears prior lot/heat/cert/CoC/notes/approval fields through BLANK_PER_ITEM_RECEIVE_FIELDS at Receiving.tsx:627.
- The Shipping native prompt/cancel bug is gone; manual Ship now directly calls markShipped. Backend has an idempotent shipped guard, so do not claim repeated shipping necessarily double-decrements stock.
- Most operations pages now use DataTable, ErrorState, responsive mobile cards, FormField and shared Modal. Broad claims that all pages lack sorting/pagination/mobile/error/modal semantics are stale.
- FAI has detail/characteristic workflow; NCR and CAR are the remaining lifecycle gaps identified here.
- Purchasing's old duplicate receiving/inspection tabs have been removed; do not recommend consolidation as if those legacy tabs still exist.
- Purchasing create validates selected vendor/part and numeric/date values; PO Send uses a named confirmation with pending state.

## Suggested verification order for root

1. Read-only Warehouse nested tabs (high confidence, readily reproducible).
2. Read-only quality NCR/CAR rows versus FAI detail, Supplier audit form Vendor ID, PO row auto-print behavior (avoid actually sending/printing if disruptive), Traceability serial and zero-result search.
3. Safe staging fixtures for 101+ quality records, 2-of-10 partial shipment and pending-save behavior; no production mutation needed for the audit.
4. Receiving validation in a local/staging dialog, calibration filtered counts, inactive-part document identity.
