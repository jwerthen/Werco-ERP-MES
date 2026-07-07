"""Supabase surface hardening: drop stray anon policy, enable RLS everywhere, revoke PostgREST grants

Revision ID: 059_supabase_rls_hardening
Revises: 058_process_sheets
Create Date: 2026-07-07

Context
-------
Production is Supabase Postgres. The application, the ARQ worker, and Alembic
all connect as the ``postgres`` role (via the Supavisor pooler user
``postgres.<project-ref>`` -- same underlying role), which OWNS every table in
schema ``public`` and has ``rolbypassrls = true``. Nothing in this codebase
uses PostgREST / supabase-js / the anon key (verified repo-wide). Yet, as
audited against the live database on 2026-07-07:

  * Supabase's PostgREST roles ``anon`` and ``authenticated`` hold FULL table
    privileges (SELECT/INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER) on
    all tables in ``public``, plus sequence and function grants, plus DEFAULT
    PRIVILEGES (grantor ``postgres``) that re-grant them on future objects,
    plus USAGE on the schema itself.
  * Row-level security is DISABLED on every table except ``companies``.
  * ``companies`` carries a stray dashboard-created policy
    ("Enable read access for all users", FOR SELECT, roles {public}) that
    makes the tenant roster anon-readable through PostgREST.

That is pure, unused API exposure of multi-tenant CUI-adjacent data (CMMC
AC.L2-3.1.1 / AC.L2-3.1.2 least-privilege posture). This migration closes it:

  1. Drops the stray ``companies`` read-all policy.
  2. Enables ROW LEVEL SECURITY on every table in ``public`` that has it off,
     discovered dynamically from ``pg_tables`` (covers ``alembic_version`` and
     any future drift -- nothing is hardcoded). With RLS on and no policies,
     PostgREST roles get zero rows even if grants ever reappear; the owning
     ``postgres`` role bypasses RLS entirely, so the app, worker, and Alembic
     are unaffected. Deliberately NOT ``FORCE ROW LEVEL SECURITY`` -- the
     owner/app connection must keep bypassing.
  3. If (and only if) BOTH roles ``anon`` and ``authenticated`` exist -- they
     do on Supabase but not on plain/CI Postgres -- revokes ALL on all tables,
     sequences, and functions in ``public``, revokes USAGE on the schema, and
     strips the ``postgres``-granted DEFAULT PRIVILEGES so future objects are
     not re-exposed. ``service_role``, ``PUBLIC``, and every other schema are
     left untouched.

Postgres-only: local dev / pytest run SQLite, where none of this exists, so
both paths return immediately on non-postgresql dialects. This migration
writes no rows, alters no columns, and never touches the tamper-evident
``audit_logs`` table's data or integrity columns.

Idempotent and reversible
-------------------------
- Upgrade re-runs are clean no-ops: ``DROP POLICY IF EXISTS``; the RLS enable
  loop only sees tables where ``rowsecurity`` is still false; REVOKE of a
  privilege that is not held is a native no-op.
- Downgrade faithfully restores the (insecure) pre-migration state and is
  itself re-runnable: disables RLS on every ``public`` table EXCEPT
  ``companies`` (which had RLS enabled before this migration), recreates the
  stray read-all policy (guarded on ``pg_policies``), and -- if both PostgREST
  roles exist -- re-grants the Supabase-default table/sequence/function/schema
  privileges and default privileges.

Locking / operations note
-------------------------
``ALTER TABLE ... ENABLE ROW LEVEL SECURITY`` is a catalog-only flag flip, but
each one takes a brief ACCESS EXCLUSIVE lock on its table, and because Alembic
runs the upgrade in one transaction those locks are HELD UNTIL COMMIT across
all ~127 tables. Each statement is instantaneous; the risk is queueing behind
(and ahead of) long-running queries. Run during a quiet window. GRANT/REVOKE
and DROP POLICY are likewise brief catalog updates. No table data is read or
written, so there is no backfill and no deploy-ordering constraint: app code
neither knows nor cares about RLS flags it bypasses.

Revision id ``059_supabase_rls_hardening`` is 26 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "059_supabase_rls_hardening"
down_revision = "058_process_sheets"
branch_labels = None
depends_on = None

# The stray Supabase-dashboard policy found on prod (FOR SELECT, roles {public}).
_STRAY_POLICY = "Enable read access for all users"

# PostgREST roles whose grants are pure unused exposure. service_role is
# deliberately NOT in this list -- we never touch it.
_POSTGREST_ROLES = ("anon", "authenticated")


def _quote_ident(name):
    """Quote a SQL identifier (double quotes, embedded quotes doubled)."""
    return '"' + name.replace('"', '""') + '"'


def _role_exists(connection, role_name):
    """Check if a database role exists."""
    result = connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"), {"role_name": role_name})
    return result.fetchone() is not None


def _postgrest_roles_exist(connection):
    """True only if BOTH PostgREST roles exist (Supabase yes; plain/CI Postgres no)."""
    return all(_role_exists(connection, role) for role in _POSTGREST_ROLES)


def _policy_exists(connection, table_name, policy_name):
    """Check if a row-level-security policy exists on a public-schema table."""
    result = connection.execute(
        text(
            "SELECT 1 FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = :table_name AND policyname = :policy_name"
        ),
        {"table_name": table_name, "policy_name": policy_name},
    )
    return result.fetchone() is not None


def _public_tables_without_rls(connection):
    """Ordinary tables in schema public with row-level security still disabled.

    Filtered to tables the migration role owns: ALTER TABLE requires ownership,
    and a non-owned table appearing in public (e.g. a future extension's) must
    not abort the boot-time upgrade -- it would merely stay RLS-off, which the
    Security Advisor surfaces. Prod was verified all-postgres-owned 2026-07-07.
    """
    result = connection.execute(
        text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND NOT rowsecurity AND tableowner = current_user "
            "ORDER BY tablename"
        )
    )
    return [row[0] for row in result]


def _public_tables_with_rls(connection):
    """Ordinary tables in schema public with row-level security enabled (owned; see above)."""
    result = connection.execute(
        text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND rowsecurity AND tableowner = current_user "
            "ORDER BY tablename"
        )
    )
    return [row[0] for row in result]


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # Local dev / pytest SQLite: no RLS, no roles, nothing to harden.
        return

    # Fail fast instead of queueing: the ~127 brief ACCESS EXCLUSIVE locks below
    # are held until commit, and this runs unattended at container boot. On
    # timeout the migration aborts, the container restart retries, and every
    # statement here is idempotent -- a retry is safe.
    op.execute("SET lock_timeout = '5s'")

    # 1) Drop the stray dashboard-created anon-read policy on companies.
    op.execute(f'DROP POLICY IF EXISTS "{_STRAY_POLICY}" ON public.companies')

    # 2) Enable RLS on every public table that still has it off (dynamic --
    #    covers alembic_version and future drift; nothing hardcoded). The
    #    owner/app role has rolbypassrls, so the app is unaffected. No FORCE.
    for table_name in _public_tables_without_rls(conn):
        op.execute(f"ALTER TABLE public.{_quote_ident(table_name)} ENABLE ROW LEVEL SECURITY")

    # 3) Strip the unused PostgREST grants -- only when both Supabase roles
    #    exist (plain/CI Postgres won't have them). REVOKE is a no-op for
    #    privileges not held, so re-runs are clean. service_role, PUBLIC, and
    #    other schemas are deliberately untouched.
    if _postgrest_roles_exist(conn):
        roles = ", ".join(_POSTGREST_ROLES)
        for statement in (
            f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {roles}",
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {roles}",
            f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {roles}",
            f"REVOKE USAGE ON SCHEMA public FROM {roles}",
        ):
            op.execute(statement)
        # The default-privilege grantor on Supabase is the postgres role. A
        # non-Supabase Postgres that has the PostgREST roles but a different
        # superuser name would error on FOR ROLE postgres -- guard separately.
        if _role_exists(conn, "postgres"):
            for statement in (
                f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM {roles}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {roles}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM {roles}",
            ):
                op.execute(statement)


def downgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    # Same fail-fast posture as upgrade (the DISABLE loop below takes the same
    # brief ACCESS EXCLUSIVE locks); all statements are idempotent.
    op.execute("SET lock_timeout = '5s'")

    # 1) Re-grant the Supabase-default PostgREST privileges (only if both
    #    roles exist). GRANT is idempotent, so re-runs are clean.
    if _postgrest_roles_exist(conn):
        roles = ", ".join(_POSTGREST_ROLES)
        for statement in (
            f"GRANT USAGE ON SCHEMA public TO {roles}",
            f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {roles}",
            f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {roles}",
            f"GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO {roles}",
        ):
            op.execute(statement)
        if _role_exists(conn, "postgres"):
            for statement in (
                f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO {roles}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO {roles}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO {roles}",
            ):
                op.execute(statement)

    # 2) Disable RLS on every public table EXCEPT companies -- companies had
    #    RLS enabled before this migration, so leaving it on (and re-enabling
    #    it if somehow off) is the faithful pre-migration state.
    for table_name in _public_tables_with_rls(conn):
        if table_name == "companies":
            continue
        op.execute(f"ALTER TABLE public.{_quote_ident(table_name)} DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.companies ENABLE ROW LEVEL SECURITY")

    # 3) Recreate the stray dashboard policy. This deliberately RESTORES THE
    #    INSECURE PRE-MIGRATION STATE (companies anon-readable via PostgREST)
    #    because a downgrade must be faithful; do not keep it in production.
    if not _policy_exists(conn, "companies", _STRAY_POLICY):
        op.execute(f'CREATE POLICY "{_STRAY_POLICY}" ON public.companies FOR SELECT USING (true)')
