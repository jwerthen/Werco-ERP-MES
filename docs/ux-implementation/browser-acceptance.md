# Local browser acceptance

These are implementation captures from September 7, 2026, using synthetic records in an isolated SQLite database. The browser used the local Vite frontend on port 5174 and the local API on port 8001. No production records were created, edited, shipped or converted. The original signed-in production tab was retained separately.

## Verified workflows

| Scenario | Observed outcome | Capture |
|---|---|---|
| Fresh sign-in from a selected quote URL | Login restores `/quotes?id=1`; first-use onboarding does not redirect the user to Dashboard. | [Selected quote](screenshots/08-quote-production-links.jpg) |
| Mixed quote conversion | A quantity-1 setup line stays quote-only. The quantity-20 production line creates a separately linked draft WO with quantity 20. Custom/service scope requires acknowledgement before conversion. | [Review](screenshots/07-quote-conversion-review.jpg), [saved link](screenshots/08-quote-production-links.jpg) |
| Receiving history | Queue → History preserves the Warehouse parent route. Browser Back restores both the queue URL and selected tab, including the interrupted-transition regression. | [Restored queue](screenshots/03-receiving-history.jpg) |
| New WO at 390 × 844 | Selected-part validation is visible; document width is 390 px and the Create action ends at x=374 px. | [Mobile validation](screenshots/01-new-wo-mobile-validation.jpg) |
| Unsaved New WO | Stay retains the selected part and entered note. Deliberate discard permits breadcrumb navigation. | [Named Leave/Stay dialog](screenshots/02-unsaved-navigation.jpg) |
| Time Clock with 24 stations | Compact station selection exposes the first queue at y=523.5 px in a 390 × 844 viewport; document width remains 390 px. The queue table can scroll within its own container. | [Mobile station and queue](screenshots/04-time-clock-24-stations-mobile.jpg) |
| Calculator snapshot | Changing quantity 20 to 30 retains the quantity-20 prices and disables Create/Print. Returning to 20 allows review. Review carries 20 × $45.41 = $908.20, lead time and the calculation record. Cancel restores the calculator inputs and result. | [Stale result](screenshots/05-calculator-stale-snapshot.jpg), [review](screenshots/06-calculator-quote-review.jpg) |
| Partial dispatch | Shipping 2 of 10 completed units leaves 8 available, 2 shipped and 0 reserved. The work order remains manufacturing-complete and its remainder can be scheduled. Verified via UI and local API record values. | [Available remainder](screenshots/09-shipping-available-remainder.jpg), [shipment details](screenshots/10-shipping-partial-remainder.jpg) |
| NCR investigation | The deep link opens the full problem, linked WO, lot, quantities and authorized disposition/containment/root-cause form. | [NCR detail](screenshots/11-ncr-investigation-detail.jpg) |
| Purchase order detail | The deep link opens the selected draft, supplier, dates, editable lines and receipt progress. Printing is an explicit action. | [PO detail](screenshots/12-purchase-order-detail.jpg) |
| Document history | Revision B has a local PDF preview and a two-record A/B history, with older revision navigation and explicit revision upload. | [Preview/history](screenshots/13-document-revision-preview.jpg) |
| Modal picker at 390 × 600 | Visitor Purpose opens above its modal; Escape closes the popup while the visit form remains open. No visit is submitted. | [Mobile picker](screenshots/14-mobile-modal-picker.jpg) |

Fresh-session Chromium runs recorded no page runtime errors for these scenarios. Calculator restoration was also exercised with React StrictMode in its regression test. Recorded API fixture identities and machine-readable browser outcomes are in [local-fixtures.json](local-fixtures.json) and [browser-results.json](browser-results.json).

## Rendered readability checks

The Time Clock work-order identifier rendered as `rgb(147, 197, 253)` (`#93c5fd`) on `[20, 27, 38]`, measuring **9.59:1**. The quote's production link uses the same foreground on the selected-record surface `[22, 34, 60]`, measuring **8.77:1**. These values include the rendered ancestor background colors.

The warning confirmation was measured from actual computed browser colors, converted through canvas into sRGB: text `[2, 6, 24]`, background `[254, 154, 0]`, contrast **9.44:1**. All three measured pairs exceed the selected 4.5:1 target for essential normal text.

These are specific rendered samples, not an app-wide WCAG conformance claim. The original audit's observations of New WO overflow and the queue below all 24 stations are available in the [historical screenshot walkthrough](../ux-audit-2026-09-06/screenshot-walkthrough.md).

## Reproduction protocol

1. Create a separate local database and use the existing seed script with explicit local settings. Use the seeded local administrator; do not connect a browser test to production.
2. Add work centers until the fixture contains 24, and create a two-line quote with a setup service first and a manufactured part second. Use a stable quote request key so retries cannot create a second quote.
3. Seed a completed quantity-10 WO and a pending quantity-2 shipment, one draft PO, one NCR/CAR, and linked A/B document revisions with a synthetic PDF. The retained fixture IDs are descriptive evidence, not production identifiers.
4. Start a fresh browser context. Begin from the signed-out quote deep link; perform the workflows in the table and check persisted quantities after conversion/dispatch. Reuse already-converted/shipped fixtures only for read-back verification.
5. Exercise the calculator in a separate fresh context. Capture the stale quantity-20 result after editing to 30, then return to 20, review, cancel, and verify restoration.
6. Use full Chromium for the native PDF viewer; the smaller headless shell does not provide an equivalent PDF rendering surface. Restrict external requests while allowing local blob URLs and Chromium's built-in viewer resources.

The automated tests additionally cover failure injection, repeated submission, reversed response order, stale edits, complete paginated history and role rules. Those cases are recorded in the [combined validation](validation.md) and domain reports. Screenshots do not independently prove all those contracts.

## PR browser regression follow-up

The initial PR run passed 65 tests, skipped one station-credential scenario, and timed out in the process-sheet journey after creating a sheet. The saved form had not cleared its unsaved state before opening the new sheet detail, so the leave confirmation blocked that navigation. The success path now clears the guard before navigation; the existing [complete process-sheet journey](../../frontend/e2e/process-sheets.spec.ts) passed without changing its steps or assertions.

Separately, the [login fixture](../../frontend/e2e/fixtures.ts) now seeds onboarding completion for the actual authenticated user/company after the real UI login. The [fixture regression](../../frontend/e2e/fixtures.spec.ts) verifies that scoped completion, preserves another workspace/user's history, and exercises Dashboard-to-Work-Orders navigation. It failed against the former global-key helper before the correction.

The final full Chromium suite ran against an isolated copy of the synthetic database on API port 8002 and Vite port 5175, with test rate limits disabled as in CI: **67 passed, 1 expected station-credential skip in 1.5 minutes**, one worker, no retries. The focused authentication/navigation/fixture slice passed **27 tests**. The edited E2E files also passed strict TypeScript compilation and Prettier checks. Production data and onboarding behavior were not changed by the fixture correction.

## Late-response logout regression

A subsequent CI run exposed a logout race: a pending protected request could return 401 after the router opened Login, causing another hard redirect with Login itself as `returnTo`. The [API interceptor](../../frontend/src/services/api.ts) now preserves an existing Login URL, including its reason, valid return destination and fragment. Four new [interceptor regressions](../../frontend/src/services/api.kioskInterceptor.test.ts) failed before this change and pass afterward, covering ordinary 401 responses and a refresh failure arriving after navigation. Existing protected-page redirects and kiosk session clearing remain tested.

The logout fixture requires the exact `/login` pathname and rejects a return destination pointing to Login; a legitimate protected destination remains allowed. Logout passed **20 consecutive Chromium runs without retries**. The service/authentication slice passed **14 suites / 116 tests**, with TypeScript, targeted ESLint and formatting checks passing.

The final complete Chromium rerun passed **67 tests with 1 expected station-credential skip in 1.5 minutes**, one worker and no retries, using a fresh local database created by the same `scripts.seed_data` entry point as CI on ports 8002/5175. A prior attempt against the reused database passed 66 tests but timed out in ProcessSheets: a seeded work order left active by the preceding full run occupied the kiosk's active-job panel. Fresh seeding resolved that fixture conflict; the process-sheet journey and its assertions were unchanged. Create a fresh database before repeating the entire suite, since its shop-floor scenarios leave active jobs.
