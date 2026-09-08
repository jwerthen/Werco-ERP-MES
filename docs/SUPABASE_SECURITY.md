# Supabase Security Posture

How the production Supabase Postgres project (ref `meatfdvteugbeksckgqg`, Postgres 17.6) is
secured, what the Supabase Security Advisor found on 2026-07-07, what migrations `059`/`060`
changed, and the manual dashboard checklist + verification steps. This is the runbook to consult
before touching Supabase project settings or adding tables.

## How Supabase is used (and what is not used)

Supabase is used **purely as managed Postgres**. The FastAPI app, the ARQ worker, and Alembic all
connect as the **`postgres`** role, which owns every table and has `BYPASSRLS`. Nothing in this
system uses PostgREST (the auto-generated "Data API"), `supabase-js`, Supabase Auth, Storage, or
Realtime (verified repo-wide): auth is the in-house JWT stack, and files go to S3 via boto3.
`auth.users` is empty.

That usage model is why the hardening below is a **no-op for the app** — and why the pre-hardening
state was still real exposure: Supabase provisions a REST API over the `public` schema whether or
not you use it.

## What the Security Advisor found (2026-07-07, verified against prod)

- **`rls_disabled_in_public` (ERROR) on all app tables.** 127 `public` tables had RLS off while
  Supabase's Data API roles `anon` and `authenticated` held FULL table privileges (including
  INSERT/UPDATE/DELETE/TRUNCATE). Net effect: the entire ERP database was readable **and
  writable** to anyone holding the project's anon key, via the auto-generated REST API — no app
  credentials required.
- **A stray dashboard-created policy on `companies`** ("Enable read access for all users", SELECT
  to `public`) made tenant company data anon-readable.
- **Discovered along the way: migration `008`'s audit-log immutability DDL did not exist in
  prod.** The CMMC AU-3.3.8 trigger functions (`audit_log_immutable_update` /
  `audit_log_immutable_delete`) and triggers (`tr_audit_log_no_update` / `tr_audit_log_no_delete`)
  were missing — prod was bootstrapped via `Base.metadata.create_all()` + `alembic stamp` past
  `008`, which silently skipped its raw DDL (functions/triggers aren't in SQLAlchemy metadata, so
  `create_all` never creates them). `audit_logs` had **no DB-level UPDATE/DELETE protection** in
  prod. See `docs/DEVELOPMENT.md` → Bootstrap order for the general gotcha.
  The same drift class resurfaced on **2026-07-31**: a read-only prod `pg_indexes` audit found
  ~42 read-path indexes from migrations `001`/`003`/`020`/`026`/`027` missing for the identical
  reason (index DDL only in stamped-over migrations, never in the models). Not a security
  exposure — performance only — restored, and mirrored into the models so `create_all`
  reproduces them, by `079_restore_stamped_over_idx`. A companion `pg_constraint` read the same
  day found the constraint half of that drift: **zero** of migration `003`'s `chk_*` CHECKs and
  **zero** of its 22 named `fk_*` lineage foreign keys existed in prod. Also not a security
  exposure — data integrity only — restored (22 FKs, 19 CHECKs, four deliberately excluded) and
  mirrored into the models by `080_restore_stamped_over_con`; see `docs/DEVELOPMENT.md` →
  Database Migrations for the exclusions. Neither migration creates a table, so the ENABLE-RLS
  new-table convention below does not apply to either.

## The fix — migrations 059 and 060

- **`059_supabase_rls_hardening`** — drops the stray `companies` policy; enables (non-`FORCE`) RLS
  on every `public` table dynamically; revokes ALL table/sequence/function privileges plus schema
  `USAGE` from `anon` and `authenticated`, and revokes their **default privileges for future
  objects** (grantor `postgres`). `service_role` is untouched. Postgres-only guard (no-op on
  SQLite dev) and role-existence guards (the revokes no-op on plain/CI Postgres without Supabase
  roles). The downgrade is a real reversal — it restores the prior (insecure) state.
- **`060_audit_log_immutability`** — idempotently `CREATE OR REPLACE`s the `008` trigger
  functions **with `SET search_path = ''` pinned** (pre-empting the advisor's
  `function_search_path_mutable` lint) and recreates the two triggers if missing. The downgrade
  only `RESET`s `search_path`; it never drops the functions/triggers — `008` owns their lifecycle.

**Why this is a no-op for the app:** the app/worker/Alembic connect as `postgres`, the table owner
with `BYPASSRLS` — non-`FORCE` RLS never evaluates for it, and no privilege it relies on was
revoked. Behavior is identical before and after.

**Deploy path:** `alembic upgrade head` runs at container boot, so the fix applies on the next
production deploy. Both migrations are idempotent.

## Deny-by-default RLS — no policies, on purpose

RLS is enabled on every `public` table with **zero policies, deliberately**. RLS-on with no
policies means deny-all for any role that isn't the table owner or `BYPASSRLS` — which is exactly
the intent:

- **App-layer tenancy remains the enforcement.** Tenant isolation is `TenantMixin` +
  `tenant_query()` / `tenant_filter()` scoped by `get_current_company_id` (CLAUDE.md invariant 1).
  RLS policies are **not** a second tenancy implementation and must not become one.
- **RLS is a hard stop for the Data API surface.** If the Data API is ever re-exposed (or a new
  Supabase role appears), enabled-RLS-with-no-policies denies it everything, independent of grants.

Do not add RLS policies to "make things work" for a Supabase role — nothing legitimate connects
through one.

## New-table convention (every future migration)

**Every migration that creates a table must also `ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;`.**
The default-privilege revocation from `059` already denies `anon`/`authenticated` any grants on
future objects, but the advisor lints **RLS state itself** — a new table without RLS brings the
`rls_disabled_in_public` ERROR back. Guard it the same way `059` does: Postgres-only (skip on
SQLite). This is also recorded in CLAUDE.md → "Migrations — handle with care".

## Nesting draft revision storage

Migration `101_quote_nesting_drafts` adds company-scoped headers and immutable
input revisions. Both tables enable RLS with no policies, revoke table/sequence
grants from PUBLIC and present `anon`/`authenticated` roles, and keep tenant
filtering in the authenticated API. PostgreSQL guards refuse revision UPDATE,
DELETE and TRUNCATE; the trigger function pins `search_path`. Model create-all
DDL mirrors these guards so bootstrap cannot silently omit them. No existing
operational or audit table is rewritten. App-layer draft writes use required
`AuditService` evidence in the same transaction; audit chain pause settings
retain their existing behavior.

## Saved nesting calculation records

Migration `102_quote_nesting_runs` adds company-scoped run headers and immutable
option checkpoints. Both tables enable deny-default RLS and revoke table/sequence
grants from PUBLIC and existing `anon`/`authenticated` roles. Literal guarded role
statements preserve bootstrap security without interpolated role queries. Model
DDL mirrors migration guards, including pinned function `search_path`.

The run's composite FK binds company, exact saved revision ID/draft/number and
input hash; a supporting unique index is added to the existing revision table.
The active-company partial unique index permits only one QUEUED/RUNNING run.
Run update guards enforce immutable inputs/settings, write-once lease/runtime
identity, legal status transitions, version increments and terminal immutability.
Checkpoints bind the run's company and lease, and reject UPDATE/DELETE/TRUNCATE.
Direct run deletion/truncation is refused. The service adds required audit
evidence atomically for user commands and worker lifecycle/checkpoint writes.

The disposable PostgreSQL verifier exercises migrations 101/102, same-company
active-run races, lease-claim CAS, exact-input/tenant/lease FKs, terminal guards
and immutable checkpoints. Separate unit and PostgreSQL-dialect compilation
tests compare model bootstrap guards with the migrations. This is test evidence, not permission
to modify production records. Ordinary application rollback retains both the
input history and saved calculations; dropping these tables is destructive and
requires a separate retention/rollback decision. Runtime results remain
unapproved and do not create physical inventory or material credits.

## Governed quote-spacing policies

Migration `103_quote_nesting_spacing_policies` adds a single policy header per
company, immutable content revisions, and immutable command events for draft
creation, publication and withdrawal. It creates no policy rows and approves no
defaults. Every command advances the header by exactly one version; only a new
revision advances its revision counter. Publication and withdrawal reference the
exact company, policy, revision number, revision ID and content hash. A withdrawal
must reference a publication of that same content. Company-wide request UUIDs,
policy event versions and publication effective times are unique; a publication
can be withdrawn only once. Publication cannot be backdated before its recorded
approval instant.

All three tables have deny-default RLS, with no Data API policies and explicit
PUBLIC/anon/authenticated table and sequence revokes. Both Alembic and model
`create_all` install the same PostgreSQL guards: header identity/deletion and
revision/event UPDATE, DELETE and TRUNCATE are refused. Trigger functions pin an
empty search path and scope parent checks to their owning schema. ORM and SQLite
guards provide development parity; production protection does not depend on ORM
events alone. Required audit evidence and command writes share one transaction.
The API still enforces active-company, Admin/effective nesting permissions,
credential eligibility and read-only context; the database owner connection can
bypass RLS and is not an authorization boundary by itself.

Resolution selects the latest effective publication first, then checks withdrawal.
It never falls back to an earlier approval. Withdrawing a future publication leaves
the current policy effective until that scheduled instant, when policy resolution
becomes unavailable unless another publication replaces it. Existing saved input
and queued-run snapshots remain immutable. Fresh conformance claims on save/new
run must match current server records. Policy approval does not approve a nest,
material grade, machine parameters or projected remnants.

Independent validation is in
`backend/tests/test_migration_103_quote_nesting_spacing_policies.py` and
`backend/tests/api/test_quote_nesting_spacing_contract.py`. The existing disposable
PostgreSQL verifier now includes migration 103 plus
`verify_nesting_spacing_postgres.py` and the bounded subprocess
`verify_nesting_spacing_api_postgres.py`. Local PostgreSQL 15 execution verified
upgrade/downgrade twice, inherited Data API grant revocation, immutable SQL guards,
concurrent publication/withdrawal CAS, real JWT/API 200/409 conflicts, identical
same-key event recovery, audit-failure rollback and save-versus-withdraw serialization.
API race sessions use a unique disposable schema and real authorization; no policy
runtime or queue is mocked. This is local test evidence, not a statement that
migration 103 has been applied to production. The verifier rejects remote or
non-test databases and removes its generated schemas. Ordinary application rollback
must retain policy history; a destructive schema downgrade requires its own
retention decision.

## Dashboard checklist (manual — cannot be done via SQL)

These are Supabase dashboard settings; migrations can't reach them. Where the current state wasn't
captured, the action is "verify + set".

| # | Action | Where | Status |
|---|--------|-------|--------|
| 1 | **Disable the Data API entirely** (nothing uses it) — or at minimum remove `public` from the exposed schemas | Project Settings → Data API | ⬜ Verify + set |
| 2 | **Enable SSL enforcement** (the app already connects with `sslmode=require`) | Project Settings → Database | ⬜ Verify + set |
| 3 | **Consider Network Restrictions.** ⚠️ CAUTION: Railway does not guarantee static egress IPs on all plans — verify the app's egress IPs before enabling, or you will lock the app out of its own database | Project Settings → Database → Network Restrictions | ⬜ Evaluate first |
| 4 | **Apply Postgres minor-version upgrades** when the dashboard offers them | Project Settings → Infrastructure | 🔁 Ongoing |
| 5 | **Keep MFA enabled** on the Supabase account | Account settings | 🔁 Ongoing |

Supabase **Auth**-related advisor warnings (e.g. leaked-password protection) are moot: Supabase
Auth is unused and `auth.users` is empty.

## Verification

Read-only checks — run via the Supabase SQL editor:

```sql
-- 1. Every public table has RLS enabled → expect 0
SELECT count(*) FROM pg_tables WHERE schemaname='public' AND NOT rowsecurity;

-- 2. anon/authenticated hold no table grants in public → expect no rows
SELECT grantee, count(*) FROM information_schema.role_table_grants
WHERE table_schema='public' AND grantee IN ('anon','authenticated')
GROUP BY grantee;

-- 3. Audit-log immutability triggers exist
--    → expect tr_audit_log_no_update, tr_audit_log_no_delete
SELECT tgname FROM pg_trigger
WHERE tgrelid='public.audit_logs'::regclass AND NOT tgisinternal;

-- 4. Trigger functions have search_path pinned
--    → both audit_log_immutable_* functions show search_path= in proconfig
SELECT proname, proconfig FROM pg_proc p
JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='public' AND p.proname LIKE 'audit_log_immutable_%';
```

Then re-run **Dashboard → Advisors → Security Advisor** and expect **zero errors**.

## Reference

- Migrations: `backend/alembic/versions/059_supabase_rls_hardening.py`,
  `backend/alembic/versions/060_audit_log_immutability.py`; the original immutability DDL
  is `backend/alembic/versions/008_add_audit_log_integrity.py`.
- Compliance claims: `docs/CMMC_LEVEL_2_COMPLIANCE.md` → Access Control and AU-3.3.8.
- Bootstrap gotcha (why prod lost the `008` triggers, and how to stamp correctly on a fresh
  database): `docs/DEVELOPMENT.md` → Database Migrations → Bootstrap order.
- Tenant-isolation enforcement (the app-layer control RLS sits beneath): CLAUDE.md →
  Compliance-critical invariants → Tenant isolation.
