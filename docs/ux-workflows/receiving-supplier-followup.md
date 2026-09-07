# Delivery receiving and supplier follow-up

Implemented September 7, 2026. This extends the existing single-line receiving, purchase-order detail, Documents storage, and Operational Action Inbox workflows. It does not send supplier emails or automatically approve incoming material.

## Receiving a delivery

Open **Warehouse → Receiving**, select an issued PO, and choose **Receive delivery lines**. Select 1–50 PO lines, enter a quantity, lot/heat and optional serial/certificate numbers for each, and set the shared packing slip, receiving location, carrier and tracking reference. Each selected line has its own inspection and explicit over-receipt choice. Review shows the part, quantity, lot/heat, inspection destination and presence of a stored certificate before posting.

All lines post in one database transaction through the same receipt/inventory routine as single-line receiving. A later line failure rolls back every new receipt, stock movement, PO quantity/status change, batch and audit entry in that submission. Existing receipt-number/PO/stock row locks and tenant filters remain in place. Print jobs are queued after the complete transaction commits, and are not requeued on replay. Materials requiring inspection do not enter available inventory until the existing inspection workflow accepts them. The established receiving default remains **no inspection**; a part's inspection flag is advisory and does not silently override the receiver's explicit selection.

An uncertain response retains the exact submission and idempotency key in **this tab's session storage, scoped to the company and user**. Reopen **Review pending delivery submission** and use **Retry same delivery**. The server returns the original committed result for the same company, actor, key and body, or performs the transaction if it did not commit. A conflicting body or actor returns 409. Known 400/401/403/404/422 rejections return the entries to an editable state. A pending submission cannot be edited into a second request while its outcome is unknown. Browser/session storage loss is not a recovery guarantee: inspect receipt history before creating a new submission. Stored replay results describe the original submission, even if receipts are later corrected or voided.

The existing single-line **Receive** action remains available and can link an uploaded certificate too.

## Actual certificate files

The per-line file field accepts a nonempty PDF, PNG or JPEG up to 20 MB, checked by file signature. It stores bytes using the configured document storage backend and creates an audited, tenant-scoped material-certificate Document tied to the PO's supplier and part. Posting validates the document, supplier/part match and stored-file availability. Receipt history, inspection details and the completed delivery expose an authenticated download of the linked bytes.

A certificate received after the material can be attached through **History → Certificate** without reposting stock or changing receipt quantities. Historical attachment remains possible when the original vendor or PO has been soft-deleted. New stock posting still rejects a deleted PO. Existing receipt links cannot be replaced or the document deleted; use Documents revision history for corrected evidence while preserving the original receipt link.

There is one linked certificate document per receipt; use a multipage PDF when one receipt needs several certificate pages. Legacy `coc_attached` flags remain for compatibility and are not retroactively proof of an uploaded file. Uploading then cancelling the receipt leaves the uploaded file in Documents for later use. An upload failure before committing removes its newly stored bytes. If the commit outcome is uncertain, bytes are preserved and the API returns 503 with instructions to refresh the receipt certificate or inspect Documents before re-uploading. This favors recoverable evidence over deleting a file that may already be linked by a committed transaction. Unlinked uploaded files require normal document cleanup; there is no automatic orphan deletion.

## Supplier confirmation and ownership

On an issued or partially received PO, **Supplier confirmation and follow-up** records acknowledgment, supplier-confirmed arrival, the supplier's reference, a required response/reason note, an eligible buyer, and the next follow-up date. Requested `required_date` and the earlier `expected_date` remain unchanged and separately labeled. Withdrawing acknowledgment clears the confirmed date and acknowledgment metadata. Editing the follow-up owner/note preserves the original acknowledgment timestamp; every revision is audited and changes the PO concurrency token.

Saving requires the current `updated_at`; stale versions return 409 and retain entered text. **Reload supplier response** explicitly discards local changes after confirmation. A successful save remains visible in the PO detail and updates the token for the next edit. Closing/navigating protects unsaved supplier evidence; closing is disabled while saving.

The Action Inbox includes issued, outstanding POs awaiting acknowledgment and POs whose explicit follow-up date is due. Its owner is the PO follow-up owner; assigning this source in the Inbox updates that owner and audits the PO. Revised source facts reappear after an earlier acknowledgment/snooze. Inbox acknowledgment and snooze remain triage only: neither confirms the supplier nor resolves missing receipts. Follow-up disappears when no open PO line has remaining quantity. Existing overdue-PO items use the confirmed arrival when present, then the existing estimate/requested-date fallback. Scheduling separately distinguishes confirmed supply evidence from tentative dates.

## API and authorization

| Endpoint | Contract |
| --- | --- |
| `POST /api/v1/receiving/deliveries` | JSON `{idempotency_key, purchase_order_id, lines: ReceiptCreate[]}`; returns `{batch_id, idempotency_key, receipts: ReceiptResponse[]}`. Key is 8–80 letters/digits/underscore/hyphen; 1–50 lines, all within one company PO. |
| `POST /api/v1/receiving/certificates` | Multipart `po_line_id`, `file`, optional `receipt_id` for a historical attachment; returns `{id, file_name, document_number}`. |
| `GET /api/v1/receiving/receipt/{id}` | Includes `po_line_id`, `certificate_document_id`, and `delivery_batch_id`; existing tenant/read rules apply. |
| `GET /api/v1/documents/{id}/download` | Existing authenticated, tenant-scoped file download. |
| `PUT /api/v1/purchasing/purchase-orders/{id}/supplier-confirmation` | `expected_updated_at` (required, nullable only for legacy NULL versions), `acknowledged`, optional `supplier_confirmed_date`, `supplier_confirmation_reference`, required nonblank `supplier_confirmation_note`, optional `follow_up_owner_id`, `follow_up_due_date`; returns updated PO. |

The new write endpoints require the existing admin/manager/supervisor role gate plus the actual company's `receiving:view` + `receiving:create`, or `purchasing:view` + `purchasing:create` permissions respectively. Read-only company contexts are denied. Standard platform/superuser permission exemptions follow the existing server contract; they do not bypass company scoping or read-only context restrictions. Follow-up owners must be active members of the current company with purchasing access. No new role permission string or client-provided company ID is trusted.

Migration `100_receiving_supplier_followup` follows `099_recoverable_import_batches`. It adds the delivery batch table, receipt/document batch references, supplier metadata and lookup indexes. Rerunning upgrade/downgrade uses schema inspection guards. PostgreSQL RLS is enabled on the new table; all table and generated sequence grants are revoked from PUBLIC, anon and authenticated. Application access remains through the custom-auth server, with no speculative `auth.uid()` policy. Downgrade preserves preceding PO/receipt rows but removes the new batch/supplier metadata and certificate links, so do not use downgrade as a business-data recovery operation.

## Validation

- 26 focused backend tests passed: atomic rollback including audits, one-time inventory/printing, immutable replay/conflicting request, actual certificate bytes and delete protection, late evidence for removed vendor, lost commit response with successful subsequent file download, tenant/permission/owner enforcement, requested-date preservation, timezone-normalized CAS, Inbox follow-up and migration round trip.
- Migration tests execute SQLite upgrade twice, downgrade twice, and upgrade again with prior receipt quantity/lot and requested-date preservation; PostgreSQL offline SQL checks RLS, foreign keys and table/sequence revocations. Root's separate PostgreSQL runtime preflight supplies the actual PostgreSQL DDL result; these isolated tests make no concurrency-load claim.
- 93 additional existing backend receiving/compliance/queue/label-printing and PO audit-persistence regressions passed.
- 20 frontend suites / 180 tests passed covering the new delivery/supplier components, parent PO dirty/close integration, existing Receiving, Warehouse nested tabs and Purchasing regressions.
- Synthetic Chromium at `127.0.0.1:5180` completed a two-line receipt with quantities 3 and 4, distinct lots, one inspection hold, a stored PDF and authenticated download; the PO's requested date remained unchanged and confirmed date persisted. Supplier follow-up appeared in the Inbox. Zero browser page errors. Downloaded bytes were saved for inspection under `/tmp/werco-downloaded-receiving-certificate.pdf`.
- Browser evidence: [supplier desktop](screenshots/supplier-follow-up-desktop.png), [supplier mobile](screenshots/supplier-follow-up-mobile.png), [delivery review mobile](screenshots/delivery-review-mobile.png), [delivery completed mobile](screenshots/delivery-complete-mobile.png). All fixtures are synthetic and use no outbound email.

Source/test TypeScript, scoped zero-warning ESLint, Black/isort and scoped Flake8 passed. Final consolidated gates are recorded with the workflow validation; no deployment is part of this implementation task.
