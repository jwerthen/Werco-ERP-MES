# Werco current quoting and reports UX source review

> Persistent source review. Source citations are pinned to [audited commit `0a383b8c63926dd02e2a37c5153b43a3b095631b`](https://github.com/jwerthen/Werco-ERP-MES/tree/0a383b8c63926dd02e2a37c5153b43a3b095631b). The document is stored in the working checkout, but its evidence refers only to this pinned current revision, originally extracted at `/tmp/werco-ux-audit-current`. Bare continuation line numbers refer to the named source in their paragraph. This is source-review evidence, not a claim of visual coverage.

Bounded read-only review of **origin/main 0a383b8**, extracted at `/tmp/werco-ux-audit-current`. All paths/line references below refer to that current tree. Evidence is source-verified, with backend/schema/service checks where relevant; no production writes or visual observations claimed. Focus is software behavior and task completion, not pricing, financial or business advice.

## QUOTE-01 — P1: Calculator's Create Quote button discards the calculated result

Evidence: [frontend/src/pages/QuoteCalculator.tsx:265](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/QuoteCalculator.tsx#L265)–267 implements createQuoteFromResult solely as `navigate('/quotes')`. Result exists only in component state at line 106, filled at 239; no navigation state, persisted draft or API create is passed. CTA explicitly reads 'Create Quote' at 997–999. Destination [frontend/src/pages/Quotes.tsx:73](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L73)–82 initializes a blank new quote and `showCreateModal` is false at 68.

Impact: After entering geometry/process choices and calculating, users click the primary continuation action and arrive at the quote list with neither a quote nor the calculated data. They must reconstruct the estimate manually; navigating back remounts the calculator without that state.

Recommendation: Create/persist an estimate draft from the exact calculation input/result snapshot and open a populated quote review form. Capture customer and business part identity before final save; keep calculation details and origin traceable.

Acceptance: Calculate → Create Quote opens review with the same quantity, unit price, lead time and breakdown; cancelling review preserves the calculation; successful save opens the created quote's stable URL; no values require retyping.

## QUOTE-02 — P1: Conversion can pair a part with another line's quantity and silently omit other parts

Evidence: [backend/app/api/endpoints/quotes.py:487](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/quotes.py#L487)–490 selects the first line with a part_id, but [quotes.py:516](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/quotes.py#L516) always takes quantity from `quote.lines[0]`. It creates exactly one WorkOrder at 513–521, then marks the entire quote CONVERTED and stores one work_order_id at 527–528. Quote schema explicitly permits custom unlinked lines ([quotes.py:27](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/quotes.py#L27) optional part; frontend Custom item at [frontend/src/pages/Quotes.tsx:532](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L532)), and multiline entry is supported at [Quotes.tsx:210](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L210)–214. [backend/app/models/quote.py:75](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/models/quote.py#L75) also lacks an explicit line ordering on the relationship.

Impact: A quote containing a first custom/setup line of 1 and a later linked part line of 20 can become a WO for that part with quantity 1. A quote containing several linked parts produces only one WO while marking all lines converted. The generic confirmation does not preview the mapping.

Recommendation: Define an explicit conversion plan per quote line: eligible part, quantity, target WO, deferred/service lines, and remaining unconverted scope. Quantity must come from the selected line. Persist line-level conversion links and mark complete only after the intended scope is handled. Show this plan before committing.

Acceptance: Custom-first/part-second uses the part line's quantity; multiple linked lines convert or remain visibly pending according to explicit user selection; no line is silently lost; generated WOs are linked back to their source lines; line reordering cannot change the meaning; success opens/links the created WOs.

## QUOTE-03 — P1: Approved RFQ quote lands in a status with no next action

Evidence: RFQ approval endpoint [backend/app/api/endpoints/rfq_quotes.py:1032](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/rfq_quotes.py#L1032) sets QuoteStatus.PENDING and package approved. The frontend success path at [frontend/src/pages/RFQQuoting.tsx:215](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/RFQQuoting.tsx#L215)–217 navigates only to '/quotes' despite receiving quote_id. Quote row actions at [frontend/src/pages/Quotes.tsx:244](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L244) only show Send for draft, and 257 shows Convert for sent/accepted; pending gets neither. Quote selected panel at 392–410 is a summary only and supplies no alternate lifecycle action.

Impact: The advertised 'Approve & Create Quote' flow succeeds but leaves users unable to progress the resulting pending quote through the main Quotes interface, and does not even select that quote on arrival.

Recommendation: Align backend status transitions with explicit review/approval/send actions in the Quotes UI. Open the returned quote ID, show what approval accomplished and the next supported action. Keep any deliberate second review stage visible and actionable.

Acceptance: Upload RFQ → Generate → Approve lands on that specific quote; Pending shows a permitted, understandable next action; users can complete the intended quote-to-WO lifecycle through UI without an API workaround.

## QUOTE-04 — P2: Send to Customer reports delivery for a mark-as-sent endpoint

Evidence: [frontend/src/pages/Quotes.tsx:251](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L251) labels action Send to Customer; [Quotes.tsx:170](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L170) toasts 'Quote sent to customer'. [backend/app/api/endpoints/quotes.py:453](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/quotes.py#L453) explicitly describes 'Mark quote as sent to customer'; it only sets status and commits at 458–459, returning 'Quote marked as sent' at 461. This endpoint has no message/provider dispatch or operational-event invocation. A search for quote_sent/send_quote handlers in backend found no alternate dispatch path for this action.

Impact: The action and success copy tell users that a customer has been contacted when the verified implementation only updates a record. Unlike the PO case, the quote endpoint does not even emit a dispatch-adjacent event.

Recommendation: Label it 'Mark as sent' with explicit method/date if manual. If actual sending is intended, implement recipient/document review and observable queued/sent/failed provider outcomes. Preserve the distinction between status and delivery.

Acceptance: Wording describes the actual side effect; a recipient/email/provider result is present only for actual dispatch; mark-as-sent cannot be confused with message delivery. No test should send a real customer message for this audit.

## QUOTE-05 — P2: Calculator shows old prices alongside newly edited quantities

Evidence: Inputs update form only, e.g. [frontend/src/pages/QuoteCalculator.tsx:527](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/QuoteCalculator.tsx#L527) (CNC quantity), 798 (sheet quantity), 355 (dimension), 539 (rush). Stored result clears on a new calculation at 230 or calculator type change, not ordinary input changes. Total/unit price render from stored result at 893–895, but displayed multiplier reads live cncForm.quantity/sheetForm.quantity at 895.

Impact: After calculating 10 units, changing quantity to 20 leaves the old total/unit price while displaying 'per unit × 20'. Geometry, material or rush edits likewise leave apparently current results until recalculation.

Recommendation: Couple result to an immutable input snapshot; mark result stale immediately after relevant edits and disable Create/Print until recalculated, or recalculate explicitly with clear pending state. Always display the quantity belonging to the result snapshot.

Acceptance: Changing any pricing input shows 'Inputs changed — recalculate'; total, unit price and displayed quantity always belong to one calculation; stale results cannot be promoted into a quote without review.

## QUOTE-06 — P2: Quote table's client pagination hides older and converted/expired records

Evidence: [frontend/src/pages/Quotes.tsx:134](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L134) calls api.getQuotes() once; [frontend/src/services/api.ts:3189](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L3189)–3191 makes a single request with no offset/limit. Backend defaults limit 100 at [backend/app/api/endpoints/quotes.py:224](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/quotes.py#L224) and excludes CONVERTED/EXPIRED when no status is specified at 244–247. UI DataTable paginates only the loaded array at [Quotes.tsx:413](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L413)–423 and has no status/history filter. Its empty copy is 'No quotes yet' at 427.

Impact: Older active quotes become unreachable through normal browsing after 100 newer records. Converted/expired quote history disappears from the list, with no route to recover it except a previously known ID deep link; users may interpret an empty list as never having quoted.

Recommendation: Server-side query/status/history pagination with total count; explicit Open/Converted/Expired views and searchable customer/quote number. Preserve the current direct-ID fetch fallback.

Acceptance: The 101st matching quote is reachable; converted and expired records can be browsed without knowing IDs; count indicates scope; empty copy distinguishes no open quotes from no quote history.

## QUOTE-07 — P2: Selecting a quote does not open its lines or editable review

Evidence: Row click at [frontend/src/pages/Quotes.tsx:417](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L417) calls selectQuote; selection panel at 392–410 only repeats quote number/revision, customer and total. It does not show line descriptions, quantities, prices, notes, terms or line-level conversion. Component supports creation at 147 but no quote edit action. Backend supports quote detail/update at [backend/app/api/endpoints/quotes.py:353](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/backend/app/api/endpoints/quotes.py#L353) and 410, though full line-edit contract may need extension.

Impact: Users can create and mark a quote sent, but cannot review its complete contents or correct a saved draft from the Quotes page. The row interaction promises more detail than it provides.

Recommendation: Real quote detail with line items, revision/terms/notes, source estimate, lifecycle history and supported draft edits. Show conversion plan and linked WOs there.

Acceptance: A saved quote can be opened and fully reviewed before send/convert; draft corrections are supported under explicit revision policy; returning preserves list state; existing selected-ID URLs still work.

## REPORT-01 — P2: Employee Time summary silently omits entries after ten

Evidence: [frontend/src/pages/Reports.tsx:641](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Reports.tsx#L641) displays employee total_hours, while [Reports.tsx:658](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Reports.tsx#L658) renders `emp.entries.slice(0,10)`. The table ends at 672 and employee block at 674 with no More, pagination, omitted-count label or export action. The backend employee report returns all entries for its requested range; frontend truncation is separate.

Impact: Employees with over ten entries show a summary total that cannot be reconciled against visible rows. Users cannot inspect the omitted operations from this report.

Recommendation: Expand all/paginate entries and show displayed/total entry counts; expose full-detail export or links to source time/operation records. Distinguish summary total from visible subtotal when limiting rows.

Acceptance: With 11+ entries all records are reachable; visible/full totals are clear and reconcile; no silent omissions.

## REPORT-02 — P2: Reports tabs and period do not round-trip through browser history/share

Evidence: [frontend/src/pages/Reports.tsx:161](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Reports.tsx#L161)–164 initializes activeTab from URL once. Click handler at 165–169 changes state and URL, but no subsequent effect reads URL back into activeTab; the only effect at 210–212 loads data. Period lives in local state with default 30 at 180, and tab changes replace all query parameters at 167–168.

Impact: Browser Back/Forward can change ?tab while the displayed tab remains unchanged. Refresh/share loses the chosen report period and resets to 30 days, making shared analysis context incomplete.

Recommendation: Derive tab/period from validated URL state (or synchronize them), preserve unrelated parameters, and retain same-view history behavior intentionally.

Acceptance: Clicking tabs then Back/Forward updates both URL and visible tab; copied URL/refresh restores tab and period; invalid query values fall back safely.

## Do not overstate remaining report issues

- Daily Output intentionally uses 14 days and labels 'Last 14 days' ([Reports.tsx:342](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Reports.tsx#L342)); Vendor Performance uses/labels 90 days ([Reports.tsx:425](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Reports.tsx#L425)); Employee Time uses/labels 7 days ([Reports.tsx:632](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Reports.tsx#L632)). The generic top-level period selector's applicability could be clarified, but these panels are not falsely labelled as the selected period.
- Reports uses independent Promise.allSettled results with section-specific errors and Retry ([Reports.tsx:182](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Reports.tsx#L182)–207); don't describe it as the older all-or-nothing failure behavior.
- Quote conversion already has pending state/confirmation ([Quotes.tsx:181](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Quotes.tsx#L181)–193). The substantive defect is incorrect/incomplete line mapping, not lack of a confirmation dialog.
- Quoting send/create still lack individual pending protection, but that is covered by the shared async-action initiative in the main operations report rather than counted again here.
