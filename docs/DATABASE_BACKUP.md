# Werco ERP - Backup & Restore Guide

How production's data is backed up, how to prove a backup can be restored, and what a
database restore does **not** bring back. Read [SUPABASE_SECURITY.md](SUPABASE_SECURITY.md)
first: it describes the production Supabase project and its security posture.

Blanks (`____`) are facts nobody has confirmed yet. They are listed under
[Owner to confirm](#owner-to-confirm). Do not fill one in from memory or from a generic
Supabase page; read it off the dashboard.

## What production runs on

| Part | Where it lives | Backed up by |
|------|----------------|--------------|
| Database | Supabase Postgres, production project ref `meatfdvteugbeksckgqg` | Supabase (see below) |
| API | Railway service `werco-api` | Nothing to back up: rebuilt from `main` on deploy |
| Worker | Railway service `werco-worker` (ARQ, background jobs and crons) | Nothing to back up: rebuilt from `main` on deploy |
| Redis | The ARQ job queue, cache, rate-limit counters, runtime heartbeats | **Nothing.** Not part of any backup |
| Uploaded documents | The configured storage backend (`STORAGE_BACKEND`): `local` disk or an `s3` bucket | **Not the database backup.** See [Uploaded documents](#uploaded-documents) |
| Configuration | Railway service variables; Supabase dashboard settings | **Not the database backup.** Record them separately |

Supabase is used only as managed Postgres. Nothing uses Supabase Storage, Auth or the
Data API, so a Supabase backup is a backup of the database and nothing else.

## What Supabase backs up

Supabase's backups are listed in the production project's dashboard under
**Database → Backups**. What is available depends on the plan:

| Fact | Value |
|------|-------|
| Supabase plan tier | ____ |
| Scheduled (daily) backups shown in the dashboard | ____ |
| How many days of scheduled backups are kept | ____ |
| Point-in-time recovery (PITR) enabled | ____ |
| PITR window, if enabled | ____ |
| "Restore to a new project" offered on this plan | ____ |

**Point-in-time recovery** restores the database to a chosen moment rather than to the
last scheduled backup. Without it, the newest point you can return to is the last
scheduled backup, and everything written after it is lost.

**Never use the in-place Restore on the production project for a drill.** It replaces
the production database. A drill restores into a new, separate project.

## Before a risky change

Some runbooks ask for a backup first (for example a migration, or
[WORKER_DEPLOYMENT_RUNBOOK.md](WORKER_DEPLOYMENT_RUNBOOK.md) §5.6 steps 7 and 11).
Until the owner settles how a pre-change copy is taken (see
[Owner to confirm](#owner-to-confirm)):

1. Write down the current UTC time. That is the restore point you would ask for.
2. Open **Database → Backups** and confirm the change is covered: PITR is on and its
   window includes that time, or a scheduled backup exists from after the last write you
   care about.
3. If neither is true, stop and ask the owner before making the change.

## Restore drill: into a NEW scratch project

The drill proves a backup can be restored and read. It never touches production.

1. **Create the scratch project from the backup.** In the production project, open
   **Database → Backups** and choose to restore into a new project (if the plan offers
   it). Pick the restore point: a scheduled backup, or a PITR time. Name the new project
   so it cannot be mistaken for production, for example `werco-restore-drill-YYYY-MM-DD`.
   Write down its project ref.
2. **Lock the scratch project down before anything else.** It holds a full copy of
   production data. Dashboard settings are per project and do **not** come across in a
   restore, so repeat the
   [SUPABASE_SECURITY.md dashboard checklist](SUPABASE_SECURITY.md#dashboard-checklist-manual--cannot-be-done-via-sql)
   on it: disable the Data API and enforce SSL.
3. **Check the target.** Everything below runs against the scratch project only. Before
   each session, confirm the dashboard URL or the connection host carries the **scratch
   project's ref** and does **not** carry `meatfdvteugbeksckgqg`. If it carries the
   production ref, stop.
4. **Check the copy** (next section).
5. **Record the drill** in the [drill log](#drill-log).
6. **Delete the scratch project** when the checks are done. It is a full copy of
   production data and should not outlive the drill.

## Checking the restored copy

All of these are read-only `SELECT`s, run in the scratch project's SQL editor after the
target check above.

**1. The schema is at the expected migration.** Compare with production's value:

```sql
SELECT version_num FROM alembic_version;
```

**2. Row counts per table.** Run the same query on production (it is read-only) and on
the copy, and compare. Tables written to after the restore point will differ; a table
that is empty on the copy but not on production is a failed restore.

```sql
SELECT table_name,
       (xpath('/row/n/text()',
              query_to_xml(format('SELECT count(*) AS n FROM public.%I', table_name),
                           false, true, '')))[1]::text::bigint AS row_count
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
```

**3. The security posture came across.** Run the four queries in
[SUPABASE_SECURITY.md → Verification](SUPABASE_SECURITY.md#verification) on the copy:
RLS on every table, no `anon` / `authenticated` grants, the two `audit_logs`
immutability triggers, and their pinned `search_path`.

**Do not use the `backend/scripts/verify_*_postgres.py` scripts for this.** They are
race and bootstrap tests, not read-only checks: they create and drop scratch databases
or schemas and write fixtures. Every one refuses any host other than `localhost`,
`127.0.0.1` or `postgres`, so they cannot reach a Supabase project at all.

**Optional: the audit hash-chain check.** The chain is verified through the API
(`GET /api/v1/audit/integrity/verify`, platform admin only), so it needs an API process
pointed at the copy. Only do this on a machine with no production credentials:

- `DATABASE_URL` is the scratch project's connection string. Check it with the target
  check above.
- `REDIS_URL` is a local or empty Redis, **never production's**: the production worker
  would pick up and run any job the copy enqueues.
- No worker runs against the copy.
- No email, SMS, carrier, print or Anthropic credentials are set. The copied companies
  keep their egress switches, so a set credential could send real messages from copied
  data.
- An API started with any `ENVIRONMENT` other than `production` or `test` runs
  `create_all` on boot, which adds any table the copy lacks. Take the row counts first.

Rows written while `AUDIT_HASH_CHAIN_ENABLED` was off carry the `LEGACY_CHAIN_PAUSED`
placeholder and are skipped by the verifier; gaps inside that window are expected. See
[AUDIT_LOG_RETENTION_RUNBOOK.md → Pausing the hash chain](AUDIT_LOG_RETENTION_RUNBOOK.md).

## What a database restore does NOT bring back

- **Uploaded document bytes.** Quality documents, shipping labels and Bills of Lading,
  receiving labels, PO source documents and other uploads live in the storage backend.
  The database holds only a reference to each file (`s3://…` or a local path). A
  restored row can point at a file that no longer exists, and a file uploaded after the
  restore point has no row pointing at it.
- **Redis.** Queued background jobs, the cache, rate-limit counters and runtime
  heartbeats are gone. The notification outbox is in the database and survives: once
  its cron runs, the worker's relay sweeper (`relay_pending_notifications_job`) sends
  recent events that were not yet marked notified at the restore point, within its
  look-back window.
- **Railway service variables** (`werco-api`, `werco-worker`) and **Supabase dashboard
  settings.** Neither is in the database.

## Uploaded documents

Which backend new uploads use is set by `STORAGE_BACKEND` (`local` by default, or `s3`;
see [ENVIRONMENT_VARIABLES.md → File Storage](ENVIRONMENT_VARIABLES.md#file-storage)).
Stored references are read per row, so switching the backend does not move old files.

**Read what the API is using** from either of these. Both describe the API process
only; the worker reads its own environment.

- **Setup Wizard** (`/setup`) → **Document Storage** panel, for any signed-in user. It
  shows the backend, the bucket and endpoint host for `s3` (to an Admin signed in to the
  app; other roles and API tokens see the backend without the location), and for `local`
  whether the upload folder sits on a mounted volume.
- `curl -s <api>/health/detailed | jq .checks.storage` (no sign-in). This shows the
  backend, `durability` and any `warning`, but never the bucket, host or region.

`durability` reads `durable` (object storage), `volume` (local disk on what looks like a
mounted volume), `ephemeral` (local disk, no mount found: files are lost on redeploy) or
`unknown`. The volume test is a guess: it says a mount exists, not that it survives a
redeploy.

| Fact | Value |
|------|-------|
| Production `STORAGE_BACKEND` on `werco-api` | ____ |
| Production `STORAGE_BACKEND` on `werco-worker` | ____ |
| If `s3`: the bucket has versioning or its own backup | ____ |
| If `local`: a persistent volume is mounted at `UPLOAD_DIR` on both services | ____ |

## If production itself must be restored

This has not been drilled. Treat it as an outline, not a tested procedure.

1. The owner decides to restore, and chooses the restore point.
2. Stop the worker (`werco-worker`) so no cron writes or sends during the restore.
3. Restore. Either restore in place on the production project, or restore into a new
   project and point `DATABASE_URL` on both `werco-api` and `werco-worker` at it.
4. Before the worker starts again, set `WORKER_CRON_JOBS=none` on it (see
   [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md)), then re-arm the crons one at
   a time.
5. Run the checks in [Checking the restored copy](#checking-the-restored-copy) against
   the restored database.
6. Tell users that work entered after the restore point must be re-entered, and that
   documents uploaded after it may have no record.

## Drill log

| Date | Who | Restore point used | Scratch project ref | Migration matched | Row counts checked | Security SQL passed | Time to usable copy | Scratch project deleted | Notes |
|------|-----|--------------------|---------------------|-------------------|--------------------|---------------------|---------------------|-------------------------|-------|
| | | | | | | | | | |

## Owner to confirm

- Supabase plan tier, scheduled-backup retention, whether PITR is enabled and its
  window, and whether "restore to a new project" is available on the plan.
- Which storage backend production uses on `werco-api` and on `werco-worker`; for `s3`,
  whether the bucket is versioned or backed up; for `local`, whether a persistent
  volume is mounted.
- How a copy is taken before a risky change, if PITR is not enabled.
- How often the drill runs, and who may start a production restore.
- Whether to delete the retired backup scripts still in the repository:
  `scripts/db-backup.ps1`, `scripts/db-restore.ps1`, `scripts/db-backup-utils.ps1`,
  `scripts/backup_database.py` and `scripts/backup_db_simple.py`. Nothing in this guide
  uses them.
