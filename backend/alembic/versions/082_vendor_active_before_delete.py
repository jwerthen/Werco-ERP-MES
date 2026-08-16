"""Remember a vendor's is_active state across a soft delete

Revision ID: 082_vendor_active_before_delete
Revises: 081_wo_sequential_operations
Create Date: 2026-08-16

Context
-------
Adds ONE nullable boolean column to the existing ``vendors`` table
(app/models/purchasing.py::Vendor):

- ``is_active_before_delete`` (Boolean, NULLABLE, no default) -- the vendor's
  ``is_active`` value as it stood at the moment it was soft-deleted, so
  ``restore_vendor`` can put that value back instead of unconditionally
  reactivating the supplier.

Why the column exists (and why NOT the obvious fix)
---------------------------------------------------
``DELETE /purchasing/vendors/{id}`` soft-deletes AND sets ``is_active = False``;
``POST /purchasing/vendors/{id}/restore`` clears the tombstone AND sets
``is_active = True``. So a vendor the shop had deliberately DEACTIVATED, and
later deleted, comes back looking ACTIVE. An approved-supplier list is an
AS9100D-controlled artifact: restoring a record must not silently hand back an
active-looking supplier that had been switched off. The owner's decision is that
restore preserves the prior state.

The tempting implementation -- stop writing ``is_active = False`` in
``delete_vendor``, so restore has nothing to put back -- is deliberately NOT
taken, and this column exists precisely so it is not needed. That write is a
load-bearing second layer: six read paths filter ``is_active``, and dropping the
write would also silently change what "deleted" means to any query added later.
So the delete keeps forcing ``is_active = False``, and REMEMBERS the prior value
here first.

The write/read protocol (endpoint side, for reviewers of this file)
-------------------------------------------------------------------
- ``delete_vendor``: record the CURRENT ``is_active`` into
  ``is_active_before_delete``, THEN set ``is_active = False``. Order matters --
  reversed, it always records False.
- ``restore_vendor``: ``is_active = COALESCE(is_active_before_delete, False)``,
  then CLEAR ``is_active_before_delete`` back to NULL so a second delete/restore
  cycle can never read a stale value.

Nothing else reads or writes this column. It is a sidecar to that one pair of
verbs, not a general-purpose flag.

NULL is meaningful -- no backfill, forward-only
-----------------------------------------------
The column is nullable with NO default precisely so NULL can mean "we never
recorded one". Every vendor soft-deleted BEFORE this ships has NULL, and by
OWNER DECISION restore treats that unknown as INACTIVE: it falls back to
``False``, so a legacy vendor comes back SWITCHED OFF and a human must
deliberately reactivate it through the separately audited ``PUT /vendors/{id}``.

That is a DELIBERATE BREAK from the pre-082 unconditional ``is_active = True`` on
restore -- do not "preserve backward compatibility" by flipping it back. On an
AS9100D-controlled approved-supplier list the safe unknown is OFF: for a legacy
row the system genuinely does not know whether the shop had switched the vendor
off before deleting it, and defaulting to ON would silently reinstate a
selectable supplier -- exactly the failure this column exists to prevent. Coming
back inactive is recoverable by an explicit, audited decision; coming back
wrongly active is not detectable at all.

There is deliberately no backfill and no ``UPDATE``: the prior ``is_active`` of an
already-deleted vendor is genuinely unknown (the delete overwrote it in place and
the ``audit_log`` delete row records the deletion, not the flag), so inventing a
value would fabricate supplier-approval state in a quality record. Same posture
as 075 with ``allocation_id``. The tamper-evident ``audit_log`` table is NOT
touched and NOT backfilled -- this migration is pure DDL.

Shape / compliance
------------------
``vendors`` is an EXISTING table: it already carries the TenantMixin non-null
``company_id`` + index, the SoftDeleteMixin columns (added by 071), and it
predates 059, which enabled ROW LEVEL SECURITY on every ``public`` table. **No
new table is created here, so the "ENABLE ROW LEVEL SECURITY on every new table"
convention (docs/SUPABASE_SECURITY.md) does not apply and no RLS DDL is emitted
-- deliberately, not by omission.** ALTERing a table does not change its RLS
state, so the Supabase Security Advisor's ``rls_disabled_in_public`` check cannot
re-flag ``vendors``. (Precedent 065/067/070/071/075/081.)

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), not a
bare ``upgrade head`` on an empty DB.

- Upgrade guards the ADD COLUMN with ``_has_table``/``_has_column`` so a
  create_all-bootstrapped DB -- where the model mapping already built the column
  -- and any re-run are clean no-ops (precedent 070/071/075/081).
- Downgrade really drops the column, guarded; it is not a ``pass`` stub. On
  SQLite the drop runs in batch mode (recreates the table without it; precedent
  063/064/065/070/081) rather than relying on SQLite's version-gated
  ``ALTER TABLE ... DROP COLUMN``; on Postgres it is a plain guarded
  ``DROP COLUMN``. The drop needs no data step and raises on nothing: losing the
  remembered values returns restore to its pre-082 "always reactivate" behavior,
  which is exactly what the downgraded application code does anyway.

Dialect note
------------
Dialect-neutral DDL: no ``_is_postgres`` early return (precedent 068/070/071/081).
No FK and no index on the column, so the plain ``add_column`` works on SQLite too
(contrast 075, whose FK column needed batch mode on the ADD side as well).

Locking / operations note
-------------------------
ADD COLUMN nullable with NO default is metadata-only on PostgreSQL 11+ (catalog
change, brief ACCESS EXCLUSIVE lock, NO table rewrite and no row scan) -- and
``vendors`` is a ~100-row master table besides. Deploy ordering: run BEFORE the
app code that reads/writes the column; old code neither selects nor writes it, so
the reverse ordering only means a delete performed in that window records
nothing and its restore falls back to ``False`` -- that vendor comes back
inactive and needs an explicit reactivation, same as any pre-082 row.

create_all parity
-----------------
This revision and ``Base.metadata.create_all()`` converge: same column name, type
(Boolean), nullability (nullable), and absent server default. Only ordinal
position differs (create_all emits it where it is declared, right after
``is_active``; ``ADD COLUMN`` appends it), which autogenerate does not compare.

Revision id ``082_vendor_active_before_delete`` is 31 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (``alembic_version.version_num``
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "082_vendor_active_before_delete"
down_revision = "081_wo_sequential_operations"
branch_labels = None
depends_on = None

TABLE_NAME = "vendors"
COLUMN_NAME = "is_active_before_delete"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def upgrade() -> None:
    if not _has_table(TABLE_NAME):
        # Partial bootstrap with no vendors table at all: nothing to alter;
        # Base.metadata.create_all builds the column from the ORM mapping
        # (precedent 069/070/071).
        return

    # Nullable, NO server default -> metadata-only ADD COLUMN on PG 11+: no
    # rewrite, no backfill scan, every existing row gets NULL. NULL is the
    # truthful value -- see "NULL is meaningful" in the module docstring.
    # Guarded so the create_all bootstrap path (model already built the column)
    # and any re-run no-op rather than erroring.
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Boolean(), nullable=True))


def downgrade() -> None:
    if not _has_table(TABLE_NAME) or not _has_column(TABLE_NAME, COLUMN_NAME):
        return

    # No data step and no raise: dropping the remembered values returns restore to
    # its pre-082 "always reactivate" behavior, which is what the downgraded
    # application code does regardless.
    if op.get_bind().dialect.name == "sqlite":
        # Batch mode recreates the table without the column (precedent
        # 063/064/065/070/081) rather than relying on SQLite's version-gated
        # ALTER TABLE ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_column(COLUMN_NAME)
    else:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
