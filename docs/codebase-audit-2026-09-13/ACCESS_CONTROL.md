# Access-control repair — 2026-09-13

Implemented the next batch after [cleanup and deployment hygiene](CLEANUP.md):
audit findings **A1–A5 and A9**. The work remains local on
`codex/cleanup-deployment-hygiene`, based on
`a4a085a31ae3783f5be7d26e182b91951ab67693`, together with the earlier cleanup.
No deployment, production account inspection, database migration, or environment
change was performed in this batch.

## Changes

| Finding | Implemented behavior |
|---|---|
| A1: tenant-to-platform escalation | Shared provisioning policy refuses platform roles through tenant registration, user creation/import, update and approval. Tenant users are always created without superuser authority. A tenant Admin cannot edit, reset, approve, activate, deactivate or unlock an existing platform principal. Accepted security changes and their required audit evidence commit together. |
| A2: cross-tenant FAI deletion | Every manual characteristic command resolves and locks the authorized tenant parent first, then validates child ownership. Unauthorized roles and foreign identifiers cannot change evidence, totals or audit state. Completed/final inspections refuse mutation. |
| A3: job-cost isolation | The full route family uses shared tenant-aware parent/child resolution, including lists, summaries, entries and variance reports. All five write routes require Admin/Manager authority and atomic audit recording. Malformed foreign relationships are excluded or redacted even when already loaded in the ORM session. Historical same-tenant work-order and part labels remain readable; writes require a live work order. |
| A4: approval and release | FAI authors and final approvers have explicit, separate role gates. Manual document publication, revision and work-order attachment require release-capable office roles. Release actor/time and required metadata audit are recorded. Platform company changes also require audit evidence attributed to the affected company. |
| A5: real-time authorization | HTTP and all three WebSocket routes share live account/company resolution. Sockets require an unscoped interactive access JWT, validate requested work-center/work-order ownership, revalidate before each broadcast and on incoming messages, and check idle connections every 30 seconds. Failed checks close with 1008 and remove connection/presence state. |
| A9: manual FAI creation | Characteristics inherit the authorized parent's company ID, fixing the missing required tenant value. Ordinary create/read/update paths are exercised against persisted records. |

Business rules moved out of the FAI and job-cost HTTP modules into focused services;
job-cost request/response DTOs moved into a schema module. Provisioning and live
identity checks each have one shared policy implementation. The existing audit
service and tenant dependency mechanisms remain authoritative.

The frontend mirrors these fixed role gates for job costing, FAI creation/prefill,
the Documents page, document revisions, and embedded manual uploads in Parts,
Purchasing, Work Order Detail and the manual laser-nest dialog. Readers retain
their existing previews and reports. Broader reactive permissions/session changes
are reserved for A6/A12.

## Role and record contract

The existing platform/superuser bypass remains, subject to credential and active
company context checks. Tenant routes cannot use that bypass to assign a platform
role. Custom UI permission overrides do not grant these fixed server write roles.

| Action | Tenant roles |
|---|---|
| Read authorized FAI, document and job-cost records | Any authenticated tenant user |
| Create/edit/prefill unfinished FAIs and characteristics | Admin, Manager, Supervisor, Quality |
| Finalize an FAI as passed, failed or conditional | Admin, Manager, Quality |
| Upload/revise/release any type through `/documents/upload`; attach its PDF to a work order | Admin, Manager, Quality |
| Delete a document, subject to existing retention guards | Admin, Manager |
| Create/update/recalculate job costs; add/delete cost entries | Admin, Manager |
| Manage ordinary tenant users | Existing Admin gate |

All types through `/documents/upload` currently publish immediately; this repair enforces the
release policy and attribution for that existing model. It does not introduce a
draft document approval workflow. Generated receipt/shipping documents retain
their existing business-route gates.

Final FAI status **or** a recorded completion date makes the inspection immutable
through these commands (409). Unfinished characteristic deletion retains the old
evidence and resulting parent counts in required audit data. Document revisions
preserve previous bytes, type and links. A revision may retain its exact existing
same-tenant Part/Work Order link after that source is removed; a fresh upload or
new work-order attachment cannot select a removed record. The deliberate
historical supplier-document attachment exception also remains.

Document audit failure removes only the newly uploaded file and rolls back its
row. Existing files survive refused edits/revisions/deletion. Successful physical
file deletion follows the committed row deletion and audit; storage cleanup
failure retains orphan bytes and is logged. Existing receipt and revision-history
retention guards still refuse deletion.

Wrong-role writes return 403; authorized requests for foreign/missing records return
404. New job-cost enum/null-field validation returns 422. Required audit storage
failure returns 503 and rolls back the repaired command. Audit behavior for
unchanged requests remains endpoint-specific; cost recalculation is itself an
audited command.

An inactive company blocks ordinary HTTP data access and WebSockets. An active
user may still log out or switch away; a stored platform principal retains access
to platform company administration so it can recover an inactive home/sole tenant.
These exceptions never permit an inactive user or missing company.

## Verification

Results and captured excerpts are recorded separately from the original audit and
cleanup evidence in [access-verification.json](evidence/access-verification.json)
and [access-check-output.txt](evidence/access-check-output.txt).

- Final full backend run: **8,817 passed, 19 skipped**, **87.28% coverage**
  against the unchanged 78% floor, with Docker hygiene integration enabled.
  Skips cover the optional built Node/image nesting checks and PostgreSQL-specific
  remnant checks; they are not access-control regressions. The first sweep's sole
  failure was an older cross-tenant test that created a user without its Company
  row. The fixture now persists an active company and retains its 404 expectation
  plus explicit no-inspection/no-audit assertions. The final full run includes that
  fix and all subsequent review corrections.
- Focused verification: 270 account/auth/token cases, 198 FAI/process-sheet cases,
  135 job-cost HTTP cases, and 182 WebSocket/shared HTTP/auth cases passed. The
  document/storage/BOM sweep passed 134 cases; all 30 final document access cases
  passed after adding the Manager/Quality lifecycle role split. These suites
  overlap and their counts must not be added together.
- Frontend: **440 suites / 4,344 tests passed**; zero-warning lint (including four
  actual Hook-configuration tests), all three TypeScript programs and production
  build passed.
- Backend Black, isort, Flake8 and mypy passed; Bandit reported no medium/high
  severity findings at the CI threshold. Repository validity/private-key hooks
  passed, including new files and verification artifacts.

The new regression modules exercise real HTTP/WebSocket routes with synthetic
tenant data, including refusal side effects, malformed historical references,
required-audit rollback, platform context switching and live connection revocation.
The original WebSocket resolver failed 24 selected regression cases when injected
into the actual mounted routes in an isolated harness. No production resolver or
working-tree changes were reverted for that reproduction.

Independent test-engineer and code/compliance review covered tenant scope, role
boundaries, audit transactions and retained history. The API, RBAC, kiosk and
developer conventions were updated to the final behavior. Existing environment
and deployment instructions remain applicable.

## Remaining work and limits

- **Next: A6/A12**, session-generation isolation for refresh/retry/cache/company
  switching and reactive effective permissions. This batch does not repair those
  frontend transport findings.
- A7/A8/A10/A11 remain: MRP recurrence, per-tenant worker transactions, optional
  completion-cost savepoints and numbering concurrency.
- Existing platform principals still need a production inventory; local code fixes
  do not establish whether prior escalation occurred.
- Access JWTs retain the existing stateless lifetime: logout does not revoke an
  otherwise unexpired access JWT. Account/company/resource changes and expiry are
  checked on socket delivery; no new server-side logout-revocation system was added.
- The legacy FAI update `version` field is not an optimistic-lock guarantee because
  the model has no corresponding version column. The repaired commands serialize
  through a parent row lock. FAI numbering's global uniqueness versus tenant
  allocation is also outside this batch.
- Backend regression tests use the repository's intentional SQLite configuration.
  They do not establish PostgreSQL concurrency behavior, production performance,
  or browser E2E coverage. No live Supabase data or customer credentials were read.

Release the API and frontend together through the existing deployment pipeline
when this branch is approved for release; no new migration is required for these
access-control changes. The earlier cleanup's lockfile and managed-Redis rollout
requirements still apply.
