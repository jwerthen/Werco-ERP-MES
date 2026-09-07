# MRP purchases grouped by supplier

Implemented September 7, 2026 in the operations worktree. This extends the existing single-recommendation supply draft flow; it does not send purchase orders or release work orders.

## Planner workflow

The Material Shortages panel allows up to 25 purchase recommendations to be selected. **Review selected purchases** checks current demand and supply, then displays every source part, current shortage, supplier, quantity, required date, suggested standard-cost price and optional note. The summary groups the edited lines by supplier. Acknowledging the review enables creation; changing any field clears that acknowledgement.

Creation produces one **draft PO per supplier**, with a separate PO line and durable MRP supply link for every recommendation. Individual required dates and notes are preserved; the header uses the earliest required date. The result links to each draft in Purchasing. **Mark reviewed** remains a distinct acknowledgement and stays false when supply is created. Single-action PO and WO creation remain available.

If the response is lost, the modal retains its reviewed payload and retry key, locks edits, and offers **Retry same purchase batch**. It does not silently replace that uncertain attempt with a new request.

## API and persistence

- `GET /api/v1/mrp/purchase-batch/review?action_ids=1&action_ids=2` accepts 1–25 unique action IDs and returns `{lines, max_lines}`. Each line uses the existing supply-review shape.
- `POST /api/v1/mrp/purchase-batch/drafts` accepts `{request_key, lines}`. Each line contains `action_id`, `review_token`, `quantity`, `due_date`, `vendor_id`, `unit_price` and optional `notes`.
- The response contains per-action `drafts`, grouped `purchase_orders`, and `replayed`. Each grouped result includes its ID, number, URL, vendor, action IDs, total and draft status.
- Existing administrator/manager/supervisor planning authorization and server company scope apply. Action, part and supplier lookups are tenant scoped. Suppliers must remain active and undeleted.
- The existing planning advisory lock serializes batch creation with MRP runs and single-action creation. Action, part and supplier rows are locked in deterministic order. All current-review checks occur before any PO is added, and the complete batch is transactional.
- Review fingerprints detect changed inventory/planned supply, demand or MRP run. Quantity cannot exceed the current shortage; duplicate-part totals are checked together. Past dates, manufactured recommendations and an already-linked overlapping selection are rejected without partial PO creation.
- Existing `MRPSupplyLink` rows store the durable batch retry identity and canonical edited-payload hash. Reordered retries return the original drafts; reusing the same key with changed fields or membership returns conflict. Unique action links remain the final overlap guard. No migration or new table is needed.

Implementation: [batch service](../../backend/app/services/mrp_purchase_batch_service.py), [MRP endpoints](../../backend/app/api/endpoints/mrp.py), [schema](../../backend/app/schemas/mrp.py), [review modal](../../frontend/src/pages/MRPPurchaseBatchReview.tsx), [MRP page](../../frontend/src/pages/MRP.tsx), [client types](../../frontend/src/types/mrpBatch.ts).

## Verification

- 23 backend tests passed across [batch regression tests](../../backend/tests/api/test_mrp_purchase_batches.py) and the existing single-supply tests. Coverage includes two suppliers/three lines, fractional quantities, per-line dates/totals, replay, edited-key conflict, overlap with an existing single draft, stale inventory/run, inactive or foreign suppliers, foreign actions, role restrictions and all-or-nothing rejection.
- The combined foundation frontend run passed 44 tests in six suites. [Batch modal tests](../../frontend/src/pages/MRPPurchaseBatchReview.test.tsx) cover grouped totals, acknowledgement reset, stable retry after an uncertain response, duplicate-click protection and stale-line blocking; existing MRP cockpit/single-action tests also passed.
- TypeScript app/test checks, targeted zero-warning ESLint, Black/isort, targeted Flake8 and source mypy passed.
- Real Chromium on the isolated local preview selected two synthetic recommendations, changed a quantity to 7.5, and created **one draft PO with two links and a $118.75 total**. It did not send or release anything. No page errors occurred.
- At 390 × 844, the review dialog measured 358 px wide with 16 px margins, the page width stayed 390 px, and the review and action controls remained reachable through the dialog scroll area. A second synthetic demand fixture was used for the mobile review capture; that review was cancelled.

Evidence: [desktop review](screenshots/mrp-batch-review-desktop.png), [created draft](screenshots/mrp-batch-created.png), [mobile review](screenshots/mrp-batch-review-mobile.png), [mobile actions](screenshots/mrp-batch-review-mobile-actions.png).

## Limits

The tests do not constitute a real PostgreSQL contention test or all-device accessibility conformance. The planning lock coordinates the planning workflow, not every inventory writer. Current-shortage validation is performed again at creation; subsequent inventory changes can still change future planning needs. Suggested prices remain editable standard costs, not confirmed supplier quotes. Taxes, shipping, approval and delivery remain in Purchasing. Partial supply leaves a remainder for a later MRP run. A cancelled draft remains linked to its source recommendation; rerunning MRP is the route to a new supply recommendation rather than silently recreating the original action.
