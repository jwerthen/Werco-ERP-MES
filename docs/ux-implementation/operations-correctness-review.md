# Independent operations correctness review

The bounded review covered the UX changes to shipping, purchasing, quality, documents, their schemas and migration 090. Three API regressions were reproduced in isolated SQLite fixtures and corrected before final validation. The reviewer did not edit product code; the operations owner implemented the corrections and permanent tests.

## Corrected regressions

| Finding reproduced during review | Final behavior | Permanent evidence |
|---|---|---|
| PO line-only changes with unchanged totals left the parent timestamp unchanged, allowing a stale editor to overwrite the first save. | Accepted line edits advance `updated_at`; a second save with the original timestamp returns 409 and retains the first edit. | `test_po_child_only_edits_advance_parent_token_and_reject_stale_save` in [backend/tests/api/test_ux_operations.py](../../backend/tests/api/test_ux_operations.py). |
| Explicit `quantity_rejected: null` passed schema validation and then raised a TypeError during comparison. | Explicit null returns 422; omission still permits unrelated NCR changes. | `test_ncr_explicit_null_rejected_quantity_returns_validation_error` in the same suite. |
| The shipment transition guard rejected the valid shipped-to-delivered update. | Forward delivery succeeds and stamps actual delivery when absent, without repeating dispatch or inventory effects. Reopening/cancelling dispatched shipments and changing their quantities remain blocked. | `test_shipping_delivery_advances_without_dispatch_or_inventory_side_effects` in the same suite. |

The review also exposed ambiguous revision identity: branches and A→B→A were accepted. The final contract is linear. Upload locks the selected predecessor, returns 409 if it already has a newer child, and returns 422 for a case-insensitive ancestor-label duplicate. Permanent document tests in [backend/tests/api/test_ux_operations.py](../../backend/tests/api/test_ux_operations.py) cover both rejections and retained history.

## Reviewed contracts

- Shipping allocation sums include pending, packed, shipped and delivered quantities and exclude cancellations. The work-order closure waits for cumulative dispatch to cover completed quantity. Creation, edit/cancel and dispatch use the same work-order-then-shipment lock order.
- PO draft-line IDs belong to the selected order; part IDs are tenant validated; received/non-draft line edits are rejected. Totals retain existing tax and shipping amounts.
- NCR/CAR writes are role constrained and tenant scoped. CAR closure requires supporting evidence; NCR closure rejects pending disposition.
- Cross-tenant document predecessor upload and history lookup returned 404. Referenced-ancestor deletion returned 409. Migration 090's nullable predecessor foreign key and composite index match the model, preserving existing document numbers and independent roots.

## Evidence and limits

Permanent corrected-behavior tests passed. [backend/tests/test_migration_090_documents.py](../../backend/tests/test_migration_090_documents.py) exercises SQLite upgrade/downgrade/upgrade and PostgreSQL SQL compilation. Final combined team results are maintained in [validation.md](validation.md).

The original scratch reproducers in `/tmp/werco_operations_review.py` intentionally asserted the pre-correction behavior and are not the release regression suite. The permanent tests above are the durable evidence.

SQLite fixtures do not exercise PostgreSQL lock contention. Code review found no additional allocation blocker, but this does not establish concurrency correctness under every production interleaving. Document history still reads the tenant's document rows before traversing lineage; no performance improvement is claimed for that operation. This backend review makes no all-device or assistive-technology conformance claim and is not a broad security audit.
