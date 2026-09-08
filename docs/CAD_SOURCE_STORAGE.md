# Original DXF evidence for saved nests

Original DXFs can be attached explicitly to an exact saved nest revision. The server
compares raw bytes with the original-byte hash recorded for every selected part.
Completion records what bytes were retained and when they were checked. It does
not approve the reported part revision, prove geometry extraction, certify material,
or create a machine program. The workspace still opens empty.

## Data and failure contract

Four additive company-scoped history tables follow the saved-revision model:

| Record | Purpose |
| --- | --- |
| Source intent | Immutable command, exact saved revision/input hash, selected group/part provenance, expected original hash/size, filename, actor/credential and request UUID. |
| Storage attempt | Fresh generated object UUID, scoped reference, nonsecret provider identity and ordinal. Each application write uses a different pre-recorded attempt. |
| Verified receipt | The one successful attempt for an intent, exact independently read-back byte hash/size and verification timestamp. |
| Part binding | Immutable receipt-to-part mapping, exact saved revision and original provenance. One attachment can cover several profiles; another request cannot replace a bound part's original. |

Intent creation and every fresh attempt require a committed transactional audit
before storage writes. Blob I/O runs after releasing database transactions and
connections. After a complete bounded read-back and hash comparison, a short
database transaction locks the intent, inserts its receipt and all part bindings,
and records required audit. A failed audit rolls back those database records.
Storage bytes and the earlier audited attempt remain accounted for and recoverable.

The existing shared storage adapter is reused without changing generic document
semantics. This path calls `save` at most once for a given attempt. An uncertain
write is checked by reading that same object; it is never overwritten. Explicit
recovery inspects earlier recorded attempts before an explicit resend may allocate
a fresh key. Late concurrent writes have distinct keys, and only one receipt wins.
Incomplete/losing objects remain recorded; there is no automatic cleanup, delete,
rename, repair, cancellation or purge of stored bytes.

CAD S3 operations use a dedicated client with the same configured provider and
credentials, a 3-second connect timeout, an 8-second socket read timeout, and one
SDK attempt per operation. The shared document-storage client is unchanged. Each
content/finalize/download service operation shares a 60-second monotonic budget
across recovery attempts and read-back. Checks run between attempts, before/after
stream chunks, and before allocating or writing a new attempt. Expiry returns 503
and leaves prior attempts available for explicit recovery. Dedicated clients and
response bodies are closed on success and failure.

This is a cooperative limit, not a hard request deadline. An in-flight SDK call or
chunk read can finish after the budget; DNS, local filesystem calls, database work
and receiving the request body are not forcibly interrupted. No extra background cancellation thread is introduced, and no automatic SDK retry
extends recovery across eight attempts. An ambiguous reply still requires checking completion before an
explicit resend.

The application protects immutability with exact tenant/input foreign keys, unique
intent and part bindings, and PostgreSQL/SQLite history triggers. Migration and
model bootstrap share identical guards. RLS is enabled and PUBLIC/anon/authenticated
table and sequence privileges are revoked. These protections do not assert
provider-level Object Lock or protect against an administrator changing a storage
account. Receipt/binding completeness and required audit are service transaction
invariants; raw table inserts are not a supported upload API.

## Bounds and access

- Raw file: 1–4,999,999 bytes; no archives or URL imports.
- JSON commands: 256 KiB; exact selected-part evidence: 512 KiB; at most 1,000 targets.
- At most eight recorded storage attempts per intent. Recovery never adds an attempt.
- Paginated history: ten intents per page. Opening history performs no storage I/O.
- Browser: up to 100 explicitly selected originals, sequential paced writes. An
  explicit 429 pauses the batch and preserves commands for user-directed resumption.
- Read requires effective `purchasing:view`; writes also require `purchasing:create`.
  Existing active-company, read-only-context, kiosk and API-token restrictions apply.
  Only the original actor and credential can resume that upload command; authorized
  members of the same company can view completed evidence and download its original.

The server snapshots reported provenance from the immutable revision, never from a
new caller-supplied geometry or filename match. `utf8-text` fallback fingerprints
cannot establish an original-byte attachment. Reimporting under another saved
revision remains an explicit separate action. No existing revision JSON is rewritten.

## Storage identity and retention limits

Each attempt records a generated company/intent/UUID reference and the nonsecret
provider identity (local absolute root, or S3-compatible endpoint/region/bucket).
The adapter must still address that exact provider identity. A configuration change
does not silently redirect a historical key to another provider; that attempt is
unavailable until its original provider is addressed. Credentials are never stored
in these records or returned in browser DTOs. New explicit attempts may use the
current configured provider while old attempts remain recorded.

`ATTACHED` in history means verified at the receipt timestamp, not a current
availability check. Every authenticated download performs a fresh bounded size/hash
check before releasing bytes, uses attachment disposition, `private, no-store` and
`nosniff`, and fails without returning bytes when they are missing, changed or
unavailable. Original content, filenames and full provenance stay out of routine
logs and audit summaries; audit records use IDs, counts and hashes.

This increment adds no storage credentials, provider, environment flag, worker job
or retention policy. Existing `STORAGE_BACKEND` configuration applies. Local files
on an ephemeral API host do not become durable because a database receipt exists.
Private access, storage location, durability, backups/restore, classification scope,
retention periods and legal holds need separate operational verification before
claiming a durable records-retention policy. Do not infer a retention duration from
the unrelated governance scaffolding or automatically release/delete a source through
the generic Documents API.

## Deployment and validation

Migration `105_nesting_cad_sources` follows `104_stock_piece_observations`. Apply the
schema and API before the frontend. It creates no source records and does not change
inventory, solver inputs/profiles, material prices or operational quote data. Keep
history tables on an application rollback; destructive schema downgrade is for an
explicit database rollback/disposable test only, and does not purge storage objects.

The operations PostgreSQL verifier checks migration/model paths, privileges,
immutable history and concurrent receipt/part binding. Its separate actual-JWT API
process uses synthetic local files and independent database sessions to prove
pre-write audit visibility, late writes, recovery and source isolation. Unit/API tests
also cover original encoding/BOM/newlines, provider changes, missing/corrupt objects,
body caps, credential revocation and audit failure at all write boundaries. No
production/user originals are uploaded by these tests.
