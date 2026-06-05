# Audit Log Retention & Archival Runbook

**Version**: 1.0.0  
**Last Updated**: 2026-06-05  
**Compliance**: CMMC Level 2 AU-3.3.8 (Protect Audit Information) + AS9100D / ISO 9001 records control

---

## Table of Contents

1. [Why audit logs are immutable and never deleted](#why-audit-logs-are-immutable-and-never-deleted)
2. [How the archival job works](#how-the-archival-job-works)
3. [Operating the archival job](#operating-the-archival-job)
4. [Verifying and restoring an archived segment](#verifying-and-restoring-an-archived-segment)
5. [Physical removal: the manual partition-drop procedure](#physical-removal-the-manual-partition-drop-procedure)
6. [Reference](#reference)

---

## Why audit logs are immutable and never deleted

The `audit_logs` table is the system's tamper-evident compliance record. Three properties make it
AS9100D / CMMC-viable, and all three depend on **rows never being removed in the ordinary course of
operation**:

- **Database-enforced immutability.** Migration `008_add_audit_log_integrity` installs two triggers,
  `tr_audit_log_no_update` and `tr_audit_log_no_delete`, that raise an exception on any `UPDATE` or
  `DELETE` against the table. The application cannot mutate or delete an audit row.
- **A single global SHA-256 hash chain.** Every row carries `sequence_number`, `previous_hash`, and
  `integrity_hash`. Each row's hash is computed over its content plus the prior row's hash, so
  altering any historical row breaks the chain from that point forward.
- **Gap detection.** The chain is verified by `AuditIntegrityService`. A **missing sequence number**
  is reported as a `sequence_gap` tamper indicator. Deleting an aged row would therefore read as
  tampering, even though it was "just cleanup."

> **The hash chain is ONE global sequence, interleaved across all tenants.** `sequence_number` is a
> single monotonic counter shared by every company; rows for different `company_id`s are interleaved
> in sequence order. `company_id` is tenant-routing metadata and is **deliberately excluded from the
> integrity hash** (see `compute_audit_hash` in `services/audit_service.py` and the AU-3.3.8 note in
> `CMMC_LEVEL_2_COMPLIANCE.md`). The practical consequence: **you cannot remove one tenant's audit
> rows without creating sequence gaps** in every other tenant's verification view.

### What this means for retention

Records-retention obligations (keep audit records N years, then dispose) appear to conflict with "never
delete." They do not. The system reconciles them by **archiving, never deleting**:

- Aged audit rows (older than the retention window) are **exported to cold storage** in a verifiable
  form by the monthly `archive_aged_audit_logs_task` job. **The live rows stay in place**, so the
  hash chain remains fully verifiable.
- The maintenance cleanup job (`cleanup_old_logs_task`) **no longer touches audit logs**. It used to
  hard-delete audit rows older than 90 days; that deletion was removed. It now only purges genuinely
  ephemeral, non-audit operational data — old `COMPLETED` background-job tracking rows (`jobs`) and
  notification logs (`notification_logs`).
- **Physical removal** of aged rows from the online database — if ever needed for storage — is a
  deliberate, documented DBA **partition-drop** operation (see
  [below](#physical-removal-the-manual-partition-drop-procedure)). It is **never** an automated row
  delete and is **never** done by disabling the immutability triggers.

---

## How the archival job works

The archival logic lives in `AuditArchivalService` (`backend/app/services/audit_archival_service.py`).
The ARQ worker job `archive_aged_audit_logs_job` (in `worker.py`) calls
`archive_aged_audit_logs_task` (in `jobs/maintenance_jobs.py`), which runs `archive_all()`. Each
company is processed independently; one company's failure is isolated (logged, collected in the
result) and never rolls back another company's work or any audit data.

For each active company, `archive_company()` performs:

### 1. Resolve the retention window

The window comes from the per-company `security_audit_record` `RetentionPolicy` row (the governance
policy seeded by migration `030_add_cui_governance_foundation`; default **1095 days / 3 years**). If a
company has no active `security_audit_record` policy — or the policy has no concrete
`default_retention_days` — the service falls back to the `AUDIT_RETENTION_DAYS_DEFAULT` setting
(default 1095). The cutoff is `now - retention_days`.

### 2. Select the candidate segment (idempotent, resumable)

The job reads a **high-water mark** — the `last_sequence` recorded on the most recent archival
`ExportEvent` for the company — and selects rows with `sequence_number > high_water` AND
`timestamp < cutoff`, ordered by `sequence_number`, capped at `AUDIT_ARCHIVE_MAX_ROWS_PER_RUN`
(default 50000). Because each run resumes from the last archived sequence, **repeated runs never
re-export or skip rows**, and a large backlog drains over successive runs. A run that hits the cap
flags `truncated: true` in its summary and the `ExportEvent` refs.

### 3. Verify integrity before archiving

Every candidate row's content hash is re-verified via `AuditIntegrityService.verify_single_record`
before anything is written. If **any** row fails (`hash_mismatch`), the run **aborts** for that
company with status `integrity_failed` and the failing sequence numbers — it never archives a
segment it cannot vouch for, and never writes a partial/tampered file. (Legacy `LEGACY_`-prefixed
rows from before integrity tracking are treated as valid and skipped, consistent with the verifier.)

### 4. Export to NDJSON and hash the bytes

The verified segment is serialized to **NDJSON** (one JSON object per line, keys sorted) preserving
every field including the chain fields (`sequence_number`, `previous_hash`, `integrity_hash`). The
exact bytes are SHA-256 hashed (`content_sha256`). The file is written **first** (before the DB
ledger), so a later DB failure leaves at most an orphan file; the filename is deterministic in the
sequence range (no timestamp), so the next run re-archives the same range and overwrites it
idempotently rather than accumulating duplicates. Files land under:

```
${AUDIT_ARCHIVE_DIR}/company_<id>/audit_archive_company<id>_seq<first>-<last>.ndjson
```

> The archival job verifies `AUDIT_ARCHIVE_DIR` is writable before processing any company and
> fails the whole run loudly if it is not (rather than silently per-company), so a missing mount or
> permissions problem surfaces as a failed job in the worker logs.

### 5. Record the export in the governance ledger

An `ExportEvent` row is written (`record_type="audit_logs"`, `export_type="audit_retention_archive"`,
`export_format="ndjson"`, `data_classification="cui"`) capturing `content_sha256`, the
`destination_reference` (the file path), and an `included_record_refs` block (first/last sequence,
first/last id, count, timestamps, cutoff, retention_days, truncated). **This ledger row is also the
idempotency high-water mark** read in step 2.

### 6. Record the archival in the audit trail itself

Finally, an **`EXPORT`** audit entry is written (via `AuditService.log(..., company_id=<company>)`,
using the per-call company override so the row is tagged to the company being archived). This new row
sequences **after** the archived segment, so it is never part of it. The chain is untouched; nothing
is deleted.

> **Legal holds are observed.** Before archiving, the service counts active `LegalHold` rows for
> `record_type="audit_logs"` and records the count on both the `ExportEvent` and the `EXPORT` audit
> entry. (Archival is non-destructive, so it proceeds regardless; the count is recorded so a later
> **purge** decision can honor the hold — see the partition-drop preconditions.)

---

## Operating the archival job

### Schedule

`archive_aged_audit_logs_job` runs on the **1st of each month at 03:00** (worker cron in `worker.py`:
`cron(archive_aged_audit_logs_job, day=1, hour=3, minute=0)`). It runs in the **ARQ worker** process
(`arq app.worker.WorkerSettings`), not the API process — the worker must be running for archival to
occur.

### Environment variables

See `ENVIRONMENT_VARIABLES.md` → **Audit Log Retention / Archival** for the table. Summary:

| Variable | Default | Notes |
|----------|---------|-------|
| `AUDIT_ARCHIVE_ENABLED` | `true` | Master switch. When `false`, `archive_all()` returns `status="disabled"` and archives nothing. |
| `AUDIT_ARCHIVE_DIR` | `/var/lib/werco/audit-archive` | Cold-storage destination. **In production point this at a mounted, backed-up volume** (or object-store mount). |
| `AUDIT_RETENTION_DAYS_DEFAULT` | `1095` | Fallback window when a company has no active `security_audit_record` policy. 1095 = 3 years. |
| `AUDIT_ARCHIVE_MAX_ROWS_PER_RUN` | `50000` | Per-company safety cap; backlogs drain across runs via the high-water mark. |

### Dry run

`archive_all(dry_run=True)` (and `archive_company(..., dry_run=True)`) reports what **would** be
archived — counts, sequence ranges, `content_sha256`, integrity result — **without writing any file or
ledger row**. Use it to preview a run or validate a backlog before committing. The cron job runs with
`dry_run=False`; to invoke a dry run manually, call the task in a worker/shell context, e.g.:

```python
# In a backend shell (an interactive session with the app importable)
from app.db.session import SessionLocal
from app.services.audit_archival_service import AuditArchivalService

db = SessionLocal()
print(AuditArchivalService(db).archive_all(dry_run=True))
```

### Where archives land & how to confirm a run

Archives are written under `${AUDIT_ARCHIVE_DIR}/company_<id>/`. Each successful run logs
`Archived N audit rows for company <id> (seq <first>-<last>) -> <path>` and writes both an
`ExportEvent` and an `EXPORT` audit entry. To confirm what was archived, inspect the
`export_events` rows with `record_type='audit_logs'` and `export_type='audit_retention_archive'`
(their `included_record_refs` and `content_sha256` are the authoritative record), or list the archive
directory.

### Verifying an archive's SHA-256

The `ExportEvent.content_sha256` is the SHA-256 of the **exact archived file bytes**. To verify a file
on disk matches its ledger entry:

```bash
sha256sum "${AUDIT_ARCHIVE_DIR}/company_1/audit_archive_company1_seq1001-1500.ndjson"
# compare the hex digest against export_events.content_sha256 for that destination_reference
```

---

## Verifying and restoring an archived segment

An archived NDJSON file is a **lossless** copy of the audit rows, including their chain fields, so it
can be re-verified offline without the live database.

**Confirm the file is intact (byte integrity):**

```bash
sha256sum <archive.ndjson>   # must equal the ExportEvent.content_sha256 for that file
```

**Re-verify each row's content hash offline (logical integrity):** recompute each row's hash with the
same function the live system uses and compare to the stored `integrity_hash`. `company_id` is **not**
part of the hash input, so archived rows verify identically to live rows:

```python
import json
from app.services.audit_service import compute_audit_hash

with open("archive.ndjson") as fh:
    for line in fh:
        r = json.loads(line)
        if (r.get("integrity_hash") or "").startswith("LEGACY_"):
            continue  # pre-integrity-tracking rows have placeholder hashes
        expected = compute_audit_hash(
            sequence_number=r["sequence_number"],
            timestamp=r["timestamp"],          # ISO string; compute_audit_hash stringifies inputs
            user_id=r["user_id"],
            user_email=r["user_email"],
            action=r["action"],
            resource_type=r["resource_type"],
            resource_id=r["resource_id"],
            resource_identifier=r["resource_identifier"],
            description=r["description"],
            old_values=r["old_values"],
            new_values=r["new_values"],
            ip_address=r["ip_address"],
            session_id=r["session_id"],
            success=r["success"],
            previous_hash=r["previous_hash"],
        )
        assert expected == r["integrity_hash"], f"hash mismatch at seq {r['sequence_number']}"
```

**Chain continuity:** within and across files, each row's `previous_hash` must equal the
`integrity_hash` of the row at `sequence_number - 1`. Because files are named with their
`seq<first>-<last>` range, adjacent archives stitch together by sequence number to reconstruct a
contiguous chain for an auditor.

> Restoring is **read/verify only.** Do not re-insert archived rows into `audit_logs` — the
> immutability triggers and the live sequence make re-insertion impossible and unnecessary. The
> archive is the disposition record; the live table remains the chain.

---

## Physical removal: the manual partition-drop procedure

Physical removal of aged audit rows from the **online** database is **only** ever done when storage
genuinely requires it, as a deliberate, human-authorized DBA operation — a **partition drop**, never
an automated row delete and never by disabling the immutability triggers. It is out of band from the
application and must be performed by a DBA with explicit sign-off.

### Hard preconditions (all must hold)

1. **The segment is already archived AND sha256-verified to cold storage.** There is an
   `ExportEvent` (`export_type="audit_retention_archive"`) covering the full sequence range, and the
   archive file on cold storage matches its `content_sha256`. Never drop rows that are not first
   exported and verified.
2. **No active legal holds.** No active `LegalHold` row with `record_type="audit_logs"` covers the
   range (for any affected company). The archival ledger records the hold count at export time; re-
   check live before dropping.
3. **Legal review where the policy requires it.** The `security_audit_record` `RetentionPolicy` ships
   with `requires_legal_review_before_purge = True`. Obtain and document the legal/compliance sign-off
   before removal.
4. **Contiguous range across ALL tenants.** Because the hash chain is one global sequence interleaved
   across companies, you must drop a **contiguous time/sequence range spanning every tenant** — you
   **cannot** remove one company's rows without leaving sequence gaps that read as tampering for the
   others. Choose the cut so the remaining online chain stays gap-free from its new first sequence
   forward.
5. **Triggers stay enabled.** Do **not** `DISABLE`/`DROP` `tr_audit_log_no_update` /
   `tr_audit_log_no_delete` to "make the delete work." Removal is achieved by dropping a table
   partition at the storage layer, which is a DDL operation outside the row-DML the triggers guard —
   not by row `DELETE`.

### After removal

- Verify the remaining online chain: run `GET /audit/integrity/verify` (Platform Admin) over the new
  online range and confirm `chain_valid` with no `sequence_gap` issues from the new first sequence
  forward. The dropped range is expected to be absent; document the new online floor.
- Retain the cold-storage archive (and its `ExportEvent`) for the full records-retention obligation.
  The archive — not the online table — is now the system of record for the removed period.
- Record the partition-drop as a maintenance/change action with the legal sign-off reference.

> **There is no application code path, endpoint, or job that performs this removal.** If you find code
> attempting to row-delete `audit_logs`, treat it as a compliance defect.

---

## Reference

| Component | Location |
|-----------|----------|
| Archival service | `backend/app/services/audit_archival_service.py` (`AuditArchivalService`) |
| Worker job / cron | `backend/app/worker.py` (`archive_aged_audit_logs_job`, monthly cron) |
| Task wrapper | `backend/app/jobs/maintenance_jobs.py` (`archive_aged_audit_logs_task`) |
| Cleanup job (audit-safe) | `backend/app/jobs/maintenance_jobs.py` (`cleanup_old_logs_task` — jobs + notifications only) |
| Integrity verifier | `backend/app/services/audit_integrity_service.py` (`AuditIntegrityService`) |
| Hash function | `backend/app/services/audit_service.py` (`compute_audit_hash`) |
| Immutability triggers | `backend/alembic/versions/008_add_audit_log_integrity.py` |
| Retention/governance models | `backend/app/models/governance.py` (`RetentionPolicy`, `ExportEvent`, `LegalHold`) |
| Retention policy seed | `backend/alembic/versions/030_add_cui_governance_foundation.py` (`security_audit_record`, 1095 days) |
| Settings | `backend/app/core/config.py` (`AUDIT_ARCHIVE_*`, `AUDIT_RETENTION_DAYS_DEFAULT`) |
| Compliance mapping | `docs/CMMC_LEVEL_2_COMPLIANCE.md` (AU-3.3.8) |
