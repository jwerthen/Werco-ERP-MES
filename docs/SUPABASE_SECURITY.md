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
SQLite). Keep it **outside** any table-existence guard: `create_all` can build the table before its
migration runs — every non-production boot calls it (`app/main.py`), as does `scripts/seed_data.py`
— and it enables RLS only where a model's `after_create` listener does, so an `ENABLE` nested under
`if not has_table` would never fire and the table would stay RLS-off.

Revoke the way `092` and every revision from `094` through `112` do: the table **and** its id
sequence, from `PUBLIC`, `anon` and `authenticated`. Any SQL that names `anon`, `authenticated` or
`service_role` runs under a **role-existence check** (`DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles
WHERE rolname = 'anon') THEN … END IF; END $$`, or a Python check like `059`'s `_role_exists`). That
covers a `REVOKE`/`GRANT` grantee, a role membership grant, a policy's `TO`, `ALTER DEFAULT
PRIVILEGES` and `has_table_privilege('anon', …)`. Those roles exist only on Supabase, and on any
other Postgres a statement naming a missing role aborts the migration. A model's `after_create` DDL
that names one must carry the `pg_roles` check in its own SQL, because `create_all` executes it as
written and no Python guard can wrap it. `093` shipped a bare `FROM anon, authenticated` and stopped
the fresh bootstrap there on plain Postgres until its role checks were retrofitted on 2026-11-03.
`PUBLIC` needs no guard.

`backend/tests/test_migration_fresh_bootstrap.py` fails the build on three things. The first is an
RLS/`REVOKE` reachable from `upgrade()` under a table-existence guard. The second is SQL reachable from
`upgrade()` that names `anon`/`authenticated`/`service_role` outside a role-existence check. SQL
comments are ignored, a `rolname` comparison is exempt, and a role filled in at run time counts when it
sits in a grantee, membership, `FOR ROLE` or policy `TO` position. The third is a model DDL literal
naming one of those roles without its own `pg_roles` check. All three are static **tripwires** for the
`093` class of regression, not proofs. On SQLite no Postgres branch executes. Some guards are also
accepted without proof that they work: a `pg_roles` test anywhere in the same literal counts, whether
or not its `IF` encloses the statement, and a Python guard's polarity is not checked. The test
module's docstring lists every blind spot.

The statements themselves execute only in the `scripts/verify_*_postgres.py` steps of the
non-blocking `e2e.yml`. The per-migration verifiers create `anon`/`authenticated` as
transaction-scoped temporary roles. They assert RLS and Data API denial under simulated inherited
grants. `verify_fresh_bootstrap_postgres` runs the real `create_all` → stamp → `upgrade head` chain on
CI's role-less Postgres, which executes the role guards' skip branch. It also requires RLS on every
`public` table. `docs/DEVELOPMENT.md` → Database Migrations lists what each verifier covers. This is
also recorded in CLAUDE.md → "Migrations — handle with care".

## Data API revoke parity (migration 113)

Migration `113_data_api_revoke_parity` brings two earlier tables to that shape:

- `091_user_workspaces` revoked `user_workspace_records` from `PUBLIC`/`anon`/`authenticated` but
  never touched `user_workspace_records_id_seq`;
- `093_mrp_supply_links` revoked `mrp_supply_links` and its sequence from `anon`/`authenticated`
  but never from `PUBLIC`.

`has_table_privilege` / `has_sequence_privilege` count a `PUBLIC` grant for every role, so a
default privilege granting `PUBLIC` (or the Data API roles) would have left those objects reachable.
`113` issues the missing statements for both tables and both sequences — `FROM PUBLIC`
unconditionally, `FROM anon` / `FROM authenticated` each under its role-existence check. It is a new
revision rather than an edit to `091`/`093` because it adds an effect those applied revisions never
had. Postgres-only (nothing on SQLite), never under a table-existence guard, and idempotent. It
changes catalog ACLs only: no rows read or written, no table rewrite, no backfill, no deploy
ordering — the app connects as the owning `postgres` role, which a `REVOKE ... FROM PUBLIC` does not
affect. On prod it is very likely a no-op for `FROM PUBLIC`: no migration ever granted `PUBLIC`
these objects, and Postgres grants `PUBLIC` nothing on tables or sequences by default. That has not
been checked against prod; this read-only query shows it (expect no `relacl` entry that begins
with `=`, which is `PUBLIC`, and none for `anon` or `authenticated`):

```sql
SELECT relname, relacl FROM pg_class
WHERE relnamespace = 'public'::regnamespace
  AND relname IN ('mrp_supply_links', 'mrp_supply_links_id_seq',
                  'user_workspace_records', 'user_workspace_records_id_seq');
```

**The downgrade is a deliberate no-op** — unlike `059`'s, which restores a pre-state it audited on
the live database and documented. Nothing records what `113` revoked: no migration granted `PUBLIC`
these privileges, and whether `anon`/`authenticated` held the `091` sequence depended on each
database's default privileges when `091` ran (`059` strips only the `postgres`-granted ones).
Re-granting to undo a `REVOKE` could open Data API access that never existed, so the unrecoverable
prior value resolves to the restrictive setting (CLAUDE.md invariant 3's rule). Downgrading past
`113` leaves the objects revoked, which every older revision already tolerates.

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

Migration `103_nesting_spacing_policies` adds a single policy header per
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
