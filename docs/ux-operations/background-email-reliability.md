# Background notification email reliability

Implemented September 7, 2026. This applies to the older notification/digest `EmailService`; the separate reviewed quote/PO document sender retains its existing explicit-send workflow.

## Transport and durable outcomes

The SMTP adapter now disables aiosmtplib's automatic STARTTLS and negotiates TLS exactly once: implicit TLS on port 465, explicit STARTTLS otherwise. Failures before submission are distinct from a server rejection and from a submission whose outcome is uncertain. Only safe pre-submission failures are retried automatically, with at most three attempts for queued notification jobs. Explicit rejection and uncertain submission are never automatically resent.

New dispatcher jobs carry the tenant, recipient user and existing `NotificationLog` ID. Enqueueing writes **queued**, with `sent=false`; enqueue success is not delivery success. The worker locks and claims that durable row, commits, then passes detached values to SMTP. No database transaction is held during the network operation. Settlement checks the same claim ID before changing the outcome. Replayed jobs cannot resend accepted, failed, skipped, unknown or still-sending claims.

| Recorded state | Meaning shown to users | Automatic retry |
| --- | --- | --- |
| queued | Awaiting worker | Queue execution |
| sending | Worker claimed the attempt | No duplicate claim |
| retrying | Failure before submission; retry pending | Bounded safe retry |
| accepted | Mail server accepted the message | No |
| failed | Preparation/recipient/queue failure or explicit SMTP rejection | No |
| skipped | SMTP is not configured; no email was sent | No |
| unknown | SMTP may have accepted the message | No; check server logs |

Accepted is **not proof of inbox delivery**. Stalled sending records older than ten minutes appear among failures/unknown outcomes as an unfinished worker outcome. Old records that merely set `sent=true` without provider provenance are labelled **Legacy queue record — delivery unverified**.

Daily digests also use durable delivery claims. Their exact digest-item IDs are recorded before submission, so a worker restart after acceptance cannot resend the same digest items. Known-unsent digest items remain available for a later scheduled digest; accepted and uncertain item sets are not automatically resent. The digest count increments only for SMTP acceptance, not for an unconfigured or failed attempt.

## User visibility and scope

Notifications includes **Background email activity**, with failures/unknown outcomes shown by default, explicit loading/error states, refresh, and a bounded list of up to 50 records. The default scope is the signed-in user's emails. Existing administrator/manager/supervisor access permits company scope; the backend enforces tenant and recipient permissions. Switching company refreshes the result and invalidates old requests. Failed requests do not turn into a false empty-state message.

A failed, skipped or uncertain attempt creates a durable in-app warning for its recipient, with an exact `/notifications?delivery=<id>` link. This warning does not dispatch another email. The page offers no resend action; users are told to check mail-server records before resending an uncertain message. Error text stored for users is bounded static guidance rather than raw SMTP exceptions or credentials.

`GET /api/v1/notifications/logs` adds optional `channel=email|sms` and `delivery_id` filters. Existing scope and list limits remain in force. The failure filter excludes queued/actively sending/retrying attempts, while including a stale sending claim. No new tables or migration are required.

Implementation: [SMTP adapter](../../backend/app/services/email_service.py), [email jobs](../../backend/app/jobs/email_jobs.py), [dispatcher](../../backend/app/services/notification_dispatch.py), [in-app failure helper](../../backend/app/services/background_email_status.py), [log endpoint](../../backend/app/api/endpoints/notifications.py), [activity component](../../frontend/src/components/BackgroundEmailActivity.tsx).

## Verification

- 118 backend tests passed across the focused reliability, dispatcher, outbox-job, email content/escaping and notification API suites. [New regression coverage](../../backend/tests/services/test_background_email_reliability.py) exercises TLS negotiation (including a faithful implicit-upgrade fake), explicit rejection, connection failure, partial/uncertain submission, safe retry exhaustion, replay suppression, tenant/recipient identity, transaction-free transport, skipped delivery, daily digest counts and restart recovery.
- An additional 15 transition-gate, recipient-tenant and scheduled-job tenant-isolation tests passed.
- 44 combined frontend tests passed across six foundation suites, including [activity tests](../../frontend/src/components/BackgroundEmailActivity.test.tsx) for truthful labels, scope boundaries, stale response suppression, refresh error and company switching. Existing inbox/link-navigation tests remain green.
- TypeScript app/test checks, targeted zero-warning ESLint, Black/isort, targeted Flake8 and source mypy passed.
- Real Chromium at 1440 px and 390 px displayed the exact synthetic skipped-email record and its in-app warning. The local worker had blank SMTP credentials and returned `skipped`; it made no SMTP network call. Both mobile page widths remained 390 px, and no browser page errors occurred.

Evidence: [desktop failure and inbox warning](screenshots/background-email-failure-desktop.png), [mobile failure](screenshots/background-email-failure-mobile.png). All transport tests use fakes; no real outbound messages were sent during implementation or acceptance.

## Operational limits

Deployment requires updating both API and worker code; Redis and scheduled workers must still be running. A five-second queue defer and bounded missing-row retry allow the dispatch transaction to commit before a worker claims the attempt. This is not a transactional email outbox: if a transaction is unusually long or the worker/queue is unavailable, a queued log can remain pending and needs operational investigation. The UI reports that state honestly.

Already queued pre-upgrade jobs that contain none of the new identity fields retain the legacy call signature and cannot gain retroactive per-recipient durable provenance. New jobs with incomplete identity never send. A worker killed after SMTP submission leaves an unfinished claim rather than automatically duplicating an email; background-email reconciliation/resend remains an administrator/mail-server procedure, not a new action on this page. No provider-delivered webhooks or real PostgreSQL contention/device-conformance claims are made.
