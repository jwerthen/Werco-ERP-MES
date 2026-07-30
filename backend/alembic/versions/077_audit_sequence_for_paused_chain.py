"""Create the Postgres sequence that backs audit_logs.sequence_number when the
hash chain is paused.

Revision ID: 077_audit_seq_paused_chain
Revises: 076_uq_wo_inv_sqlite_parity
Create Date: 2026-07-29

WHY THIS EXISTS
---------------
``AuditService`` allocates ``sequence_number`` with a tail read (``MAX + 1``)
serialized by ONE global transaction-scoped advisory lock held until the caller's
transaction commits. That lock is the system-wide funnel this change removes.

Removing the lock while keeping ``MAX + 1`` would NOT remove the serialization: every
concurrent writer computes the same next value, blocks on the ``sequence_number``
unique index until the winner commits, and after the 5-attempt retry budget is
exhausted the audit row is DROPPED (``audit_service.log()`` returns None). A state
change with no audit row is strictly worse than one with no hash. So the paused path
needs a genuinely lock-free allocator, and ``nextval`` is it.

This migration only CREATES the sequence. Nothing consumes it until
``AUDIT_HASH_CHAIN_ENABLED`` is set to False, so applying this is a no-op in behavior.

SAFETY NOTES
------------
* **Idempotent** — ``CREATE SEQUENCE IF NOT EXISTS``, and the restart is computed
  from the live table every time, so re-running cannot regress the counter.
* **Reversible** — ``downgrade`` drops the sequence. Safe because nothing depends on
  it while the chain is enabled. If you downgrade while PAUSED, re-enable the chain
  first or audit writes will fail.
* **Postgres-only** — guarded on the dialect, like migrations 059/060. SQLite (the
  test backend) has no sequences; the service falls back to MAX+1 there.
* **Start value** — ``MAX(sequence_number) + 1000``. This is a convenience, NOT the
  collision guarantee. It only covers the window between applying this migration and
  flipping the flag; the flag may be flipped months later, by which time the still-
  running chain has allocated far past the margin. The actual guarantee is
  ``AuditService._resync_paused_sequence()``, which jumps the sequence past the live
  MAX on the collision-retry path — that is what makes pause -> resume -> pause safe.
  Do not reason about correctness from this number.
* **No RLS clause** — the repo convention is ENABLE ROW LEVEL SECURITY on every new
  TABLE. A sequence is not a table and has no RLS. Grants are not widened here:
  ``audit_logs`` itself already had anon/authenticated privileges revoked by 059, and
  this sequence inherits the default (owner-only) with no explicit GRANT.
"""

from alembic import op

revision = "077_audit_seq_paused_chain"
down_revision = "076_uq_wo_inv_sqlite_parity"
branch_labels = None
depends_on = None

SEQUENCE_NAME = "audit_logs_sequence_number_seq"
# Headroom over the live MAX so the sequence cannot hand out a value the still-running
# chain is about to allocate via MAX+1. See "Start value" above.
START_MARGIN = 1000


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE_NAME} AS BIGINT")
    # Recompute the restart point from the live table on every run, so this is safe to
    # re-apply and safe to apply long after the sequence was first created. setval with
    # is_called=true means the NEXT nextval() returns the given value + 1.
    op.execute(
        f"""
        SELECT setval(
            '{SEQUENCE_NAME}',
            GREATEST(
                (SELECT COALESCE(MAX(sequence_number), 0) + {START_MARGIN} FROM audit_logs),
                (SELECT last_value FROM {SEQUENCE_NAME})
            ),
            true
        )
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME}")
