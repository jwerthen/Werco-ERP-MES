"""Restore the 008 audit-log immutability triggers lost at prod bootstrap (CMMC AU-3.3.8)

Revision ID: 060_audit_log_immutability
Revises: 059_supabase_rls_hardening
Create Date: 2026-07-07

Context -- WHY this migration exists
------------------------------------
Migration 008 (008_add_audit_log_integrity.py) created two plpgsql trigger
functions -- ``audit_log_immutable_update`` / ``audit_log_immutable_delete`` --
and two triggers -- ``tr_audit_log_no_update`` / ``tr_audit_log_no_delete`` --
on ``audit_logs``, the DB-level enforcement of CMMC AU-3.3.8 (protect audit
information): any UPDATE or DELETE against the tamper-evident hash-chained
audit log raises an exception.

Production, however, was bootstrapped via ``Base.metadata.create_all()`` +
``alembic stamp`` PAST 008 (the documented create_all -> stamp -> upgrade
path, docs/DEVELOPMENT.md). ``create_all`` built the audit_logs COLUMNS and
both indexes from the model (app/models/audit_log.py), but 008's raw DDL --
the two functions, the two triggers, and the COMMENT ON TABLE -- exists only
in the migration, so the stamp skipped it. Verified against the live prod
database on 2026-07-07: neither function nor trigger exists there. Prod audit
rows currently have NO database-level UPDATE/DELETE protection.

008 has been applied elsewhere and must not be edited; this new revision
restores the protection idempotently:

  1. ``CREATE OR REPLACE`` both functions with bodies IDENTICAL in behavior
     to 008's (same RAISE EXCEPTION message text referencing ``OLD.id`` /
     ``OLD.sequence_number``), adding ``SET search_path = ''`` to the function
     definition. That pins resolution (hardening for a trigger owned by a
     superuser-ish role) and pre-empts Supabase's
     ``function_search_path_mutable`` advisor lint; the bodies reference only
     ``OLD``, so an empty search_path is safe. Unconditional CREATE OR
     REPLACE: idempotent, and it also upgrades dev DBs that DO have 008's
     unpinned versions in place.
  2. Creates each trigger only if missing, with the existence check scoped to
     ``public.audit_logs`` (008's check was by tgname alone).
  3. Reapplies 008's COMMENT ON TABLE (COMMENT overwrites; idempotent).

No audit_logs DATA is touched -- no rows written, no integrity columns
(``sequence_number`` / ``previous_hash`` / ``integrity_hash``) altered, no
backfill. app/services/audit_archival_service.py is non-destructive and
explicitly assumes these triggers exist; no code path UPDATEs or DELETEs
audit_logs rows (the test-reset endpoint uses TRUNCATE, a statement-level
operation that does not fire these ROW triggers -- no conflict).

Postgres-only: local dev / pytest SQLite has no plpgsql; both paths return
immediately on non-postgresql dialects (same posture as 008, which predates
SQLite dev but is pg_catalog-bound throughout).

Idempotent and reversible
-------------------------
- Upgrade re-runs are clean: CREATE OR REPLACE is idempotent, triggers are
  guarded by a scoped pg_trigger check, COMMENT overwrites itself.
- Downgrade ONLY reverts what this migration added on top of 008: it RESETs
  search_path on both functions (back to 008's unpinned definition), guarded
  on existence. It deliberately does NOT drop the functions or triggers --
  their lifecycle belongs to 008 (whose downgrade drops them), and dropping
  them here would reopen the CMMC AU-3.3.8 protection gap on any database
  sitting between 008 and 060. The reapplied table COMMENT is likewise left
  in place (documentation-only, harmless; 008's downgrade owns its removal).

Locking / operations note
-------------------------
CREATE OR REPLACE FUNCTION and COMMENT are catalog-only. CREATE TRIGGER takes
a brief ACCESS EXCLUSIVE lock on ``audit_logs`` (no table scan -- the trigger
is not a constraint trigger); with steady audit-insert traffic this is a
momentary queue, not a rewrite. No deploy-ordering constraint: application
behavior is unchanged (the app never updates/deletes audit rows); only
out-of-band tampering starts failing, which is the point.

Revision id ``060_audit_log_immutability`` is 26 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "060_audit_log_immutability"
down_revision = "059_supabase_rls_hardening"
branch_labels = None
depends_on = None

# (function_name, trigger_name, trigger_event, action_word) -- action_word only
# feeds the RAISE message so it stays byte-identical to 008's.
_PROTECTIONS = [
    ("audit_log_immutable_update", "tr_audit_log_no_update", "UPDATE", "updated"),
    ("audit_log_immutable_delete", "tr_audit_log_no_delete", "DELETE", "deleted"),
]

# 008's comment, reapplied (same text; indentation whitespace normalized).
_TABLE_COMMENT = """
    COMMENT ON TABLE public.audit_logs IS
    'CMMC Level 2 AU-3.3.8 Compliant Audit Log.
    This table is protected by database triggers that prevent UPDATE and DELETE operations.
    Integrity is verified via SHA-256 hash chain (integrity_hash, previous_hash).
    Sequence numbers enable gap detection for tamper evidence.';
"""


def _function_exists(connection, function_name):
    """Check if a function exists in schema public."""
    result = connection.execute(
        text(
            "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.proname = :function_name AND n.nspname = 'public'"
        ),
        {"function_name": function_name},
    )
    return result.fetchone() is not None


def _trigger_exists(connection, trigger_name):
    """Check if a trigger exists ON public.audit_logs (scoped, unlike 008's name-only check).

    to_regclass() returns NULL (-> no match) instead of erroring if the table
    is somehow absent; the CREATE TRIGGER would then fail loudly, as it should.
    """
    result = connection.execute(
        text("SELECT 1 FROM pg_trigger WHERE tgname = :trigger_name AND tgrelid = to_regclass('public.audit_logs')"),
        {"trigger_name": trigger_name},
    )
    return result.fetchone() is not None


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # Local dev / pytest SQLite: no plpgsql triggers to restore.
        return

    # CREATE TRIGGER takes a brief ACCESS EXCLUSIVE lock on audit_logs and this
    # runs unattended at container boot -- fail fast rather than queue behind a
    # long-running query; everything here is idempotent, so the retry is safe.
    op.execute("SET lock_timeout = '5s'")

    for function_name, trigger_name, trigger_event, action_word in _PROTECTIONS:
        # Body behavior identical to 008's; SET search_path = '' is the only
        # addition (safe: the body references only OLD). CREATE OR REPLACE is
        # unconditional so dev DBs holding 008's unpinned versions get pinned.
        op.execute(f"""
            CREATE OR REPLACE FUNCTION public.{function_name}()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            SET search_path = ''
            AS $$
            BEGIN
                RAISE EXCEPTION 'CMMC AU-3.3.8 VIOLATION: Audit logs are immutable and cannot be {action_word}. Record ID: %, Sequence: %',
                    OLD.id, OLD.sequence_number;
                RETURN NULL;
            END;
            $$;
        """)  # noqa: E501 -- RAISE message kept byte-identical to 008's

        if not _trigger_exists(conn, trigger_name):
            op.execute(f"""
                CREATE TRIGGER {trigger_name}
                BEFORE {trigger_event} ON public.audit_logs
                FOR EACH ROW
                EXECUTE FUNCTION public.{function_name}();
            """)

    # Reapply 008's compliance comment (lost with the rest of 008's raw DDL at
    # bootstrap). COMMENT overwrites, so this is idempotent.
    op.execute(_TABLE_COMMENT)


def downgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    # Revert ONLY this migration's addition: unpin search_path, restoring
    # 008's original (unpinned) function definitions. The functions and
    # triggers themselves are deliberately NOT dropped -- their lifecycle
    # belongs to 008, and dropping them here would reopen the CMMC AU-3.3.8
    # protection gap (audit rows updatable/deletable at the DB level) on any
    # database sitting between revisions 008 and 060.
    for function_name, _trigger_name, _trigger_event, _action_word in _PROTECTIONS:
        if _function_exists(conn, function_name):
            op.execute(f"ALTER FUNCTION public.{function_name}() RESET search_path")
