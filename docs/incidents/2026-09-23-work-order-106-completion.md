# Work order 106: copied operation completion

## Confirmed production evidence

Work order `WO-20260923-003` (ID 106, company 1) has 21 independent component
operations. Their identifiers are 10 through 210; all share routing sequence 10
because they are a parallel dispatch pool. The sequence is precedence, not identity.

On September 23, 2026, at 14:55 UTC (09:55 Central):

- Operation 882, Inlet Neck, recorded 8 good pieces and completed. Audit 52135
  records the explicit completion from iPhone Safari; time entry 496 is its labor
  and production evidence.
- Audits 52136–52155 changed the other 20 operations from ready to complete with
  `source=reconcile_on_read` and empty `time_entry_ids`. Quantities were raised to
  each operation's planned target; operator and timestamps were copied from
  operation 882 without production.
- Audit 52156 marked the work order complete. Inventory RECEIVE 519 added 8
  finished assemblies to inventory item 258 even though only one component was done.
- At investigation, item 258 still held all 8 units, had no allocation and no
  other movement. There were no material allocations or JobCost records for 106.

The same signature appeared on replaced work order 105. The user confirmed that
106 replaced it and explicitly authorized cancelling 105 and reversing its false
8-unit receipt. The guarded cancellation preserved all operation/labor history,
changed the header to cancelled with zero finished quantity, and appended ADJUST
527 against original RECEIVE 518. Inventory item 257 is now zero. Required audit
52317 records the cancellation. The script is
`backend/scripts/cancel_replaced_work_order_105.py`; its eight regression tests
cover preservation, idempotence, changed-evidence refusal, and audit rollback.

## Root cause and correction

`work_order_state_service` grouped operations by sequence and copied completion
evidence between rows on reads. It also used that grouping to count progress.
The work-order detail page independently collapsed rows and displayed sequence
as the operation number. Normal mobile completion sent only the selected ID.

The backend now counts each actual operation and reconciles only its own time
entries. It never borrows completion, quantity, timestamps, or actor from another
operation. The detail page preserves individual IDs and displays the stored
operation number, with sequence only as a fallback for missing identifiers.
Parallel startability is unchanged.

Regression tests cover explicit mobile completion and clock-out followed by
repeated reads, distinct components and identical labels sharing a sequence,
own-operation labor reconciliation, independent progress, and progressive labels.

## Guarded data repair

`backend/scripts/repair_work_order_106_completion.py` defaults to preview. Its
apply mode requires the exact recorded operation, labor, audit, and inventory
evidence; changed guarded evidence aborts. It restores the 20 false completions
to ready with zero production and no borrowed stamps, preserves operation 882 and
entry 496, and returns 106 to in-progress with zero finished assemblies.

The original receipt and audits remain intact. An audited compensating ADJUST
under `work_order_receipt_correction` removes the false 8 units. Only that explicit
correction trail permits a compensating addition at genuine completion; ordinary
shipments or stock adjustments never bypass receipt idempotence. No schema change
or historical ledger rewrite is required.

Deploy the application fix before applying the repair, then verify the persisted
rows and repeat reconciliation. The script records an incident audit and is a
no-op on repeat application. Its caller commits all changes atomically.

After verifying the fixed backend's healthy release, the repair was applied to
106. Audit 52345 records the header correction; ADJUST 528 compensates original
RECEIVE 519. Item 258 is zero. The persisted result is one complete operation
(882, eight inlets), 20 ready operations with zero production, and an in-progress
work order with zero finished assemblies. Both original labor entries and both
original RECEIVE rows remain intact. Repeated reconciliation against these real
records in a read-only transaction kept completion at 1/21 (4.8%), without any
status transitions. The first pass selected the current ready operation from
the parallel pool; the second pass reported no further changes.

The root fix is commit `b7e15687203ce9a8c2c8ac6143216a822b171525`. Validation included
8,719 passing tracked backend tests (12 skipped), 323 targeted frontend tests, a
production frontend build, and TypeScript checks excluding preexisting untracked
duplicate files. The complete CI and production deployment succeeded for this
commit; both the healthy API and the public website served the verified release.

Follow-up commit `5b2c05a9954d50afcca12460e90222f4ec10055f` sorts displayed operations
by routing sequence, then stored operation number numerically, then ID. This
keeps parallel operations in progressive order even when updates change their
database return order. It does not merge rows or change routing precedence.
Validation included 130 focused frontend tests, ESLint, a production build, and
both TypeScript configurations with the same untracked-duplicate exclusion.
The frontend deployment succeeded and `https://wercomfg.app/release.txt`
confirmed this follow-up release. A final read-only check confirmed 105 remained
cancelled, 106 remained in progress, both corrected inventory balances were zero,
and the original receipts plus compensating adjustments were present.
