# Quote and reporting implementation

## Quote review and conversion

Quote detail now exposes line identities, descriptions, quantities, prices, line notes, terms, contact information and production links. Draft/pending quotes support correction in a guarded editor; issued quotes retain reviewed content. The validity editor shows and saves the actual expiration date. Customer PO and phone fields carry through review and conversion.

Conversion is an explicit line-by-line review. The backend validates the current tenant, part eligibility, quantity and prior conversion while locking the quote. Each selected production line receives a separate draft work order and its own association. A custom/setup line cannot supply another line's quantity. Purchased/material lines are identified as nonproduction; inactive production lines remain unresolved. A partially converted quote cannot be reopened as a draft to remove existing production associations.

The review explains that routing operations must be reviewed/added before releasing the generated draft WOs. Existing quotes retain the old aggregate work-order association without guessing historical per-line mappings. Successful conversion remains successful if the subsequent detail fetch fails, and the local detail cache cannot overwrite fresh associations with an old list row.

Quote content updates carry the reviewed `updated_at` timestamp. Stale updates return 409 before changing lines; entered fields remain available. Child-only edits advance the parent timestamp even if the total stays unchanged. Quote creation carries a stable request key and canonical payload hash: an ambiguous-response retry returns the existing quote; a changed payload using a committed key receives an explicit recovery link. Older keyless API clients remain compatible.

## Calculator and RFQ handoff

The calculator records its inputs, type, quantity, result and capture time as one snapshot. Changing quantity, geometry, material, finish or rush makes the result stale and disables Create/Print. The displayed quantity remains the snapshot's quantity. Draft state is scoped to the current user/workspace in session storage and is restored when returning from review.

Create Quote transfers quantity, cent-rounded unit price, lead time and cost data into the quote editor. The original calculation is retained as an internal record. Customer and production-part selection remain explicit review choices. Calculator display and quote line total now reconcile the rounded unit price; a visible unit-price rounding adjustment explains any difference from the raw estimate. The markup amount is no longer labelled with a hard-coded 25%.

RFQ approval opens the quote ID returned by the server. Pending quotes have an actionable Mark as sent step. The wording discloses that this action changes status and does not dispatch a customer message.

## History and reporting

Quotes expose open, all-history and individual status filters, with server search and stable URL state. The API client retrieves all pages in the requested scope, allowing historical records beyond the old initial page to be reached and exported. Part catalog failures expose a local Retry while retaining editor entries.

Reports tab and period are validated URL state and survive history navigation. Their tabs have keyboard behavior and explicit panel relationships. Period applicability is documented per report instead of implying that every panel uses the same window. Employee Time shows its visible/full count, makes all entries accessible beyond the first ten, and explains that totals include all entries. Currency and utilization denominator labels make values easier to interpret.

## Regression evidence

- Backend cases cover custom-first-line quantity, selected multi-line conversion, separate tenant numbering, nonproduction lines, preserved draft identity/notes/costs, issued/linked edit refusal, quote history, stale update refusal, creation replay/recovery and migration 089 preservation/rollback/SQL compilation.
- Frontend cases cover reviewed conversion payloads, cancellation, a committed conversion with a failed refresh, stale list/cache ordering, expiration edits, update conflicts with retained input, pending-field protection and creation replay identity.
- Calculator cases cover stale input/result ownership, session restoration, exact handoff and cent-rounding reconciliation.
- Reporting cases cover section failures, unavailable headline values, the eleventh time entry and URL/history restoration.

The quote, calculator and reporting suites passed in the final combined frontend run. Final combined totals belong in [validation.md](validation.md); overlapping targeted runs are not summed. Fresh Chromium acceptance confirmed that sign-in retains the selected quote URL after the onboarding correction. Browser evidence uses synthetic local data; no production quotes were sent or converted.

Migration `089_quote_line_conversion` is implemented by [backend/alembic/versions/089_quote_line_conversion_track_work_orders_per_quote_line.py](../../backend/alembic/versions/089_quote_line_conversion_track_work_orders_per_quote_line.py); permanent API and migration coverage lives in [backend/tests/api/test_quote_conversion_ux.py](../../backend/tests/api/test_quote_conversion_ux.py) and [backend/tests/test_migration_089_quote_lines.py](../../backend/tests/test_migration_089_quote_lines.py). SQLite round trips and PostgreSQL SQL compilation do not prove staging PostgreSQL upgrade behavior or concurrent lock contention. Browser coverage is limited to exercised paths/viewports and does not assert all-device or assistive-technology conformance.
