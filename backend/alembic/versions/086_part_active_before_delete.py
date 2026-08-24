"""Remember a part's is_active state across a soft delete

Revision ID: 086_part_active_before_delete
Revises: 085_inventory_combines
Create Date: 2026-08-23

Context
-------
Adds ONE nullable boolean column to the existing ``parts`` table
(app/models/part.py::Part):

- ``is_active_before_delete`` (Boolean, NULLABLE, no default) -- the part's
  ``is_active`` value as it stood at the moment it was soft-deleted, so
  ``restore_part`` can put that value back instead of unconditionally
  reactivating the SKU.

This is the exact control migration ``082`` established for ``Vendor``, applied
to ``Part``. Read 082's docstring alongside this one: the reasoning is the same
and the two must not drift.

Why the column exists NOW (the hazard is newly reachable)
---------------------------------------------------------
``DELETE /parts/{id}`` soft-deletes AND forces ``is_active = False`` /
``status = 'obsolete'``; ``POST /parts/{id}/restore`` cleared the tombstone and
then hard-coded ``is_active = True`` / ``status = 'active'``. So a part the shop
had deliberately RETIRED, and later deleted, came back looking ACTIVE.

Until the Combine/Merge SKUs feature shipped, "inactive but not deleted" was
essentially unreachable for a part: ``PartUpdate`` carries neither ``is_active``
nor ``status``, and the only writer of those columns was ``delete_part`` itself
(which also sets ``is_deleted``). The unconditional re-activate was therefore
harmless in practice. **The combine feature deliberately creates that state at
scale** -- ``POST /parts/{id}/deactivate`` and the combine's ``deactivate_source``
flag retire the folded-away SKU as a routine, reasoned, audited step -- so the
hazard is now ours:

    Fold ``.0625-60X144-304SS`` onto ``SH-A240-304-0.0625-60X144-2B`` and
    deactivate the source (audited, with a reason). Someone later deletes the
    empty husk, then someone else restores it. Pre-086 it came back ACTIVE and
    ``status='active'`` -- back in every picker, selectable again for receiving
    and BOM lines -- the deliberate retirement silently reversed, with no audit
    row saying anybody decided to re-activate it.

Invariant 3 (CLAUDE.md) names this directly: **a restore returns the RECORD, not
the permission**, and it names ``is_active`` as a delete MASK rather than a
filter.

Why NOT the obvious fix
-----------------------
The tempting implementation -- stop writing ``is_active = False`` in
``delete_part``, so restore has nothing to put back -- is deliberately NOT taken,
and this column exists precisely so it is not needed. That write is a
load-bearing second layer: ``GET /parts/`` and ``GET /materials/`` both default
``active_only=True`` and filter on the flag, as do the pickers built on them, and
dropping the write would silently change what "deleted" means to any query added
later. So the delete keeps forcing ``is_active = False``, and REMEMBERS the prior
value here first.

The write/read protocol (endpoint side, for reviewers of this file)
-------------------------------------------------------------------
- ``delete_part`` (app/api/endpoints/parts.py): record the CURRENT ``is_active``
  into ``is_active_before_delete``, THEN set ``is_active = False``. Order matters
  -- reversed, it always records False.
- ``restore_part``: ``is_active = COALESCE(is_active_before_delete, False)``, then
  CLEAR ``is_active_before_delete`` back to NULL so a second delete/restore cycle
  can never read a stale value. ``status`` is set CONSISTENTLY with whatever
  ``is_active`` resolves to -- never hard-coded ``'active'``.

Nothing else reads or writes this column. It is a sidecar to that one pair of
verbs, not a general-purpose flag, and it is deliberately kept out of
``PartBase`` / ``PartCreate`` / ``PartUpdate`` so no ``PUT /parts/{id}`` or
``PUT /materials/{id}`` -- both blind ``setattr`` loops -- can pre-seed what the
restore will read back.

``parts`` has TWO soft-delete doors and only ONE restore door, and all three now
agree. ``DELETE /materials/{id}`` (app/api/endpoints/materials.py::delete_material)
writes the same ``parts`` rows and has no restore verb of its own, so its deletes
come back through ``POST /parts/{id}/restore`` -- which reads this sidecar. Both
doors therefore record it, immediately before the ``is_active = False`` mask write
(the order is load-bearing: reversed, it always records ``False``). Had only one
door captured it, every part deleted through the other would restore INACTIVE no
matter how it was really switched -- the safe direction, but a record asserting
something that was never true.

NULL is meaningful -- no backfill, forward-only
-----------------------------------------------
The column is nullable with NO default precisely so NULL can mean "we never
recorded one". Every part soft-deleted BEFORE this ships has NULL, and restore
treats that unknown as INACTIVE: it falls back to ``False``, so a legacy part
comes back SWITCHED OFF and a human must deliberately reactivate it through the
separately audited ``POST /parts/{id}/activate``.

That is a DELIBERATE BREAK from the pre-086 unconditional ``is_active = True`` on
restore -- do not "preserve backward compatibility" by flipping it back. The
asymmetry is the whole argument, and it is the same one 082 makes: restoring too
restrictively costs one explicit, audited re-activation and is visible
immediately (the part is missing from the list somebody expected it in);
restoring too permissively is indistinguishable from a legitimate approval and so
is never detected at all. A part number is a controlled identifier on drawings,
BOM lines and receiving transactions -- reinstating one as selectable is an
engineering decision, not a side effect of undoing a delete.

There is deliberately no backfill and no ``UPDATE``: the prior ``is_active`` of an
already-deleted part is genuinely unknown (the delete overwrote it in place, and
the ``audit_log`` delete row records the deletion, not the flag), so inventing a
value would fabricate catalog state in a quality record. Same posture as 075 with
``allocation_id`` and 082 with vendors. The tamper-evident ``audit_log`` table is
NOT touched and NOT backfilled -- this migration is pure DDL.

The column must also never be tightened to NOT NULL for the same reason: NULL is
the encoding of "deleted before 086 shipped", and it has to stay reachable.

Shape / compliance
------------------
``parts`` is an EXISTING table: it already carries the TenantMixin non-null
``company_id`` + index, the SoftDeleteMixin columns, and it predates 059, which
enabled ROW LEVEL SECURITY on every ``public`` table. **No new table is created
here, so the "ENABLE ROW LEVEL SECURITY on every new table" convention
(docs/SUPABASE_SECURITY.md) does not apply and no RLS DDL is emitted --
deliberately, not by omission.** ALTERing a table does not change its RLS state,
so the Supabase Security Advisor's ``rls_disabled_in_public`` check cannot
re-flag ``parts``. (Precedent 065/067/070/071/075/081/082/084.)

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), not a
bare ``upgrade head`` on an empty DB.

- Upgrade guards the ADD COLUMN with ``_has_table``/``_has_column`` so a
  create_all-bootstrapped DB -- where the model mapping already built the column
  -- and any re-run are clean no-ops (precedent 070/071/075/081/082).
- Downgrade really drops the column, guarded; it is not a ``pass`` stub. On
  SQLite the drop runs in batch mode (recreates the table without it; precedent
  063/064/065/070/081/082) rather than relying on SQLite's version-gated
  ``ALTER TABLE ... DROP COLUMN``; on Postgres it is a plain guarded
  ``DROP COLUMN``. The drop needs no data step and raises on nothing: losing the
  remembered values returns restore to its pre-086 "always reactivate" behavior,
  which is exactly what the downgraded application code does anyway.

Batch mode on ``parts`` is safe to reach for despite the table's constraints:
``batch_alter_table`` copies ``__table_args__`` from the reflected table, and the
column carries no FK, no index and no CHECK of its own.

Dialect note
------------
Dialect-neutral DDL: no ``_is_postgres`` early return (precedent
068/070/071/081/082). No FK and no index on the column, so the plain
``add_column`` works on SQLite too (contrast 075, whose FK column needed batch
mode on the ADD side as well).

Locking / operations note
-------------------------
ADD COLUMN nullable with NO default is metadata-only on PostgreSQL 11+ (catalog
change, brief ACCESS EXCLUSIVE lock, NO table rewrite and no row scan). ``parts``
is a few-thousand-row master table, so even the lock wait is bounded by whatever
transaction currently holds a conflicting lock, not by table size.

Deploy ordering: run BEFORE the app code that reads/writes the column. Old code
neither selects nor writes it, so the reverse ordering only means a delete
performed in that window records nothing and its restore falls back to ``False``
-- that part comes back inactive and needs an explicit reactivation, same as any
pre-086 row. No ordering hazard in either direction, just that one degraded case.

create_all parity
-----------------
This revision and ``Base.metadata.create_all()`` converge: same column name, type
(Boolean), nullability (nullable), and absent server default. Only ordinal
position differs (create_all emits it where it is declared, right after
``status``; ``ADD COLUMN`` appends it), which autogenerate does not compare.

Revision id ``086_part_active_before_delete`` is 29 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (``alembic_version.version_num``
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "086_part_active_before_delete"
down_revision = "085_inventory_combines"
branch_labels = None
depends_on = None

TABLE_NAME = "parts"
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
        # Partial bootstrap with no parts table at all: nothing to alter;
        # Base.metadata.create_all builds the column from the ORM mapping
        # (precedent 069/070/071/082).
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
    # its pre-086 "always reactivate" behavior, which is what the downgraded
    # application code does regardless.
    if op.get_bind().dialect.name == "sqlite":
        # Batch mode recreates the table without the column (precedent
        # 063/064/065/070/081/082) rather than relying on SQLite's version-gated
        # ALTER TABLE ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_column(COLUMN_NAME)
    else:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
