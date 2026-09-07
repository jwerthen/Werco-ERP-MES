# Reviewed quote and PO email delivery

The composer prepares a reviewable PDF and message, then requires an explicit Send. Preparing or opening history never contacts SMTP. Existing manual “mark sent” workflows remain distinct from actual email transmission.

## Contracts

All routes are under `/api/v1/document-deliveries`.

| Route | Behavior |
| --- | --- |
| `GET ?entity_type=quote\|purchase_order&entity_id=<id>` | Last 50 snapshots/attempts, newest first. Bounded history; older audit records remain in the database. |
| `POST /preview` | `{entity_type, entity_id, new_attempt?:false}` prepares an immutable PDF and default editable recipient/subject/body. Reuses the matching prepared snapshot or accepted attempt unless a new email is explicitly requested. |
| `GET /{id}` | Current recorded status, version, verification fields and attachment metadata. |
| `GET /{id}/attachment` | The exact reviewed PDF bytes, authenticated and `private, no-store`. |
| `POST /{id}/send` | `{expected_version, request_key, recipient, subject, body}` explicitly submits that PDF. Validates recipient/message, current source fingerprint, role and company context before claiming the attempt. |
| `POST /{id}/reconcile` | `{expected_version, outcome:accepted\|failed, verification_note}` records a manager's mail-server verification, with no SMTP call. Unknown outcomes or sending attempts older than ten minutes only. |

TypeScript contracts are in `frontend/src/types/documentDelivery.ts`; backend schemas are in `app/schemas/document_delivery.py`. Responses distinguish `prepared`, `sending`, `accepted`, `failed`, and `unknown`. `provider_message_id` stores the generated SMTP Message-ID header for mail-log correlation, not an external delivery receipt. `delivered` remains null: SMTP acceptance is not a recipient delivery receipt. `manually_verified`, `verified_at`, and `verification_note` distinguish operator verification from transport acceptance.

## Behavior and safeguards

- Quote access/send is limited to admin, manager or supervisor (plus privileged platform/superuser access). PO access/send also honors the company's current `purchasing:view`/`purchasing:approve` permission overrides; fixed PO send roles are admin/manager. Reconciliation is admin/manager only. Tenant-scoped attachment/history reads cannot expose another company's records. Read-only company context disables sending and server write authorization blocks mutations.
- A snapshot stores immutable PDF bytes, SHA-256, source fingerprint, document number and reviewed PO issue date. Recipient, subject and body are validated and captured with the explicit send. Limits: 250 document lines, 5 MB PDF, 10,000 body characters, 200 subject characters and one validated recipient. Header newline injection and stale email review versions are rejected.
- The draft's proposed PO issue date is visible in its PDF. A next-day send of an undated draft requires a fresh review. Acceptance of an unchanged source stamps the exact reviewed date and sent status. Source edits while SMTP is in flight retain the current source workflow state; the delivered attachment remains the reviewed snapshot.
- A company-scoped transaction lock and source/header/child locks validate and durably claim the attempt before network I/O. Joined source queries use `FOR UPDATE OF` the parent; child, part, supplier and relevant quote metadata locks are separate. The SMTP payload is a frozen detached object: no expired ORM lookup or deferred PDF read holds a database transaction across SMTP waits.
- The request key and payload fingerprint recover a recorded attempt without sending it again. Unknown/sending attempts block another preview/send, including an older prepared snapshot. An explicit new email is supported after an accepted attempt. Failed deliberate resends can prepare a fresh review rather than being redirected to an older accepted attempt.
- SMTP uses one TLS negotiation: required STARTTLS on port 587 (and other non-465 ports), implicit TLS on port 465. Connect/TLS failures before submission are definite failures; ambiguous transport failures during submission remain unknown. There are no automatic transport retries.
- Managers can reconcile an unknown outcome after checking the mail server, with a required verification note and optimistic version. Active sends cannot be reconciled until ten minutes have elapsed. The action audits the operator's statement and never creates a provider delivery receipt. Changed source documents are not blindly marked sent.
- A late transport worker only finalizes its own sending/version claim. If it returns after manual verification, it retains the manager's note. A contradictory definite late result creates a visible, audited unknown conflict and blocks another email until reviewed again.
- History/status queries defer the PDF BLOB and read a stored attachment size, avoiding up to 250 MB of PDF data for a full 50-item history page.
- Migration `094_document_deliveries` follows `093_mrp_supply_links`. New public-table access remains behind the custom FastAPI authentication: RLS enabled, table/sequence grants revoked from PUBLIC and Supabase `anon`/`authenticated` roles. Table constraints bound attachment/body sizes and restrict entity/status values.

## Document fidelity

Quote download and email use the same deterministic customer-ready renderer. Saved quote line quantities and totals remain authoritative after editing; matching AI estimate material/finish metadata and assumptions can remain contextual. A regression seeds an obsolete AI estimate price and proves current download and email PDF bytes agree. Internal operation times remain excluded.

The PO PDF reproduces the existing supplier-facing print-data template: company/supplier/contact blocks, buyer/date/shipping details, delivery-date groups, ordered/received/backorder quantities, material descriptions, unit/extended prices, totals and notes. Descriptions wrap and table headers repeat. Fractional PO quantities now remain precise in print data and the print page's backorder display.

Synthetic samples: [five-page supplier PO](samples/po-email-review.pdf) and [current customer quote](samples/quote-email-review.pdf). All five PO pages and the quote page were rendered with Poppler and visually inspected for wrapping, clipping, headings, groups, totals and notes. These samples contain fictional data.

### Browser PDF review

The composer uses the reusable `PdfPreview` canvas viewer instead of a browser PDF iframe. PDF.js and its worker URL load behind a dynamic renderer-module boundary when the preview opens. The canvas fits its container, tracks width changes, supports previous/next pages, and offers loading, retry, and a persistent download fallback. Changing pages or attachments cancels prior tasks; closing destroys the PDF loading task. The caller remains responsible for revoking its attachment URL.

Seven focused viewer regressions cover fit width/paging, download after document failure, render failure/retry, cancellation and late results, resize/unmount cleanup, attachment replacement, and StrictMode effect replay. All seven plus nine composer integration tests passed with source/test TypeScript and scoped zero-warning ESLint. The real generated synthetic `QA-QUOTE-001` PDF was also opened through the local authenticated composer at 1440px and 390px: the painted customer details, line item, $125 total, and notes were visually inspected at both widths. Canvas sizes were 478×619 and 308×399 CSS pixels respectively. There were no browser page errors, horizontal overflow in either the dialog or page, or scoped Axe violations in either viewport. Network inspection confirmed no PDF.js/worker request before opening the composer. The structured browser evidence is in [pdf-preview-browser-result.json](pdf-preview-browser-result.json). This verifies the local Chromium flow; it does not claim all-device conformance.

Screenshots: [desktop reviewed quote](screenshots/quote-email-review.png), [phone-width reviewed quote](screenshots/quote-email-review-mobile.png). SMTP was intentionally unconfigured, and no email was sent.

## Validation

**80 focused/compatibility backend tests passed** with `pytest --no-cov -n 0` across:

- `tests/api/test_document_deliveries.py` — 36 endpoint/transport cases, including actual transaction/state checks, permissions, edited fields, byte-identical reviewed attachments, replay, TLS negotiation, pre-submission errors, unresolved recovery, late-worker conflicts, issue-date rollover and no open DB transaction during SMTP.
- `tests/test_migration_094_document_delivery.py` — 2 migration roundtrip/generated PostgreSQL SQL checks.
- `tests/test_pdf_text_escaping.py` — 25 existing PDF fidelity cases.
- `tests/api/test_quote_conversion_ux.py` — 14 compatibility cases.
- `tests/api/test_rfq_quotes.py` — 3 compatibility cases.

Seven backend source modules passed mypy; targeted Flake8 and Black checks passed. Targeted frontend ESLint passed with zero warnings; the full frontend application and test TypeScript checks passed.

Every delivery test replaces the transport with a fake, or runs the real MIME adapter against a faithful in-memory SMTP context. No actual outbound message was sent. PostgreSQL lock SQL was reviewed/compiled independently; no real PostgreSQL contention, deployed provider delivery or all-device conformance is claimed. SMTP configuration/credentials and a real recipient-delivery receipt remain outside these local checks. Broader integration/browser results are maintained by the root validation record.
