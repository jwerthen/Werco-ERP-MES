"""Add NOT_REQUIRED value to the inspectionstatus enum (dock-to-stock records integrity)

Revision ID: 066_inspection_not_required
Revises: 065_display_token_setup_code
Create Date: 2026-07-16

Context (AS9100D records-integrity fix -- compliance-auditor, PR #127)
---------------------------------------------------------------------
The receiving dock-to-stock ("no incoming inspection required") path used to
stamp the ``po_receipts`` row with ``inspection_status = PASSED``,
``inspection_method = VISUAL``, ``inspected_by = <receiver>`` -- i.e. a record
asserting a visual inspection that never actually happened. Under AS9100D that
is a quality-records-integrity defect.

The model + endpoint have already been changed to introduce and use a new
member ``InspectionStatus.NOT_REQUIRED = "not_required"``
(app/models/purchasing.py) so a dock-to-stock receipt records the truth: the lot
was accepted into inventory WITHOUT an incoming inspection because none was
required -- distinct from ``PASSED`` (which asserts a real inspection occurred
and passed). This migration is the schema half: it adds that value to the
existing native PostgreSQL enum type on live data.

The enum label is UPPERCASE ``NOT_REQUIRED`` (load-bearing)
----------------------------------------------------------
``po_receipts.inspection_status`` is a NATIVE PostgreSQL enum whose type name is
``inspectionstatus`` (SQLAlchemy lower-cases the ``InspectionStatus`` class name
for the unnamed ``SQLEnum(InspectionStatus)``). Because that column is declared
WITHOUT ``values_callable``, SQLAlchemy binds/stores the enum MEMBER NAME, not
the ``.value``. Verified against the model:

    POReceipt.__table__.c.inspection_status.type.enums
        -> ['PENDING', 'PASSED', 'FAILED', 'PARTIAL', 'NOT_REQUIRED']
    type.values_callable is None          # -> stores the NAME, not the value
    _db_value_for_elem(NOT_REQUIRED)      -> 'NOT_REQUIRED'

So the label added to the native type is the uppercase name ``NOT_REQUIRED``.
The lowercase ``not_required`` is only the ``.value`` -- the JSON/wire form the
API serializes -- and must NOT be added to the type. (Contrast the
``documenttype`` adds in 047 / 052, which added *lowercase* labels; the
``inspectionstatus`` column stores the uppercase member NAME, so this migration
adds the uppercase form.)

What this migration does
------------------------
On PostgreSQL it runs::

    ALTER TYPE inspectionstatus ADD VALUE IF NOT EXISTS 'NOT_REQUIRED'

Transaction handling (autocommit_block -- deliberate choice)
------------------------------------------------------------
The ALTER is wrapped in ``op.get_context().autocommit_block()``, which suspends
Alembic's surrounding per-migration transaction and runs the statement in
AUTOCOMMIT. This matches the repo's two most recent native-enum ADD VALUE
migrations (047 and 052, both on ``documenttype``) and sidesteps two PostgreSQL
footguns in one move: (a) the historical rule that ``ALTER TYPE ... ADD VALUE``
could not run inside a transaction block at all, and (b) the still-current rule
that an enum value added inside a transaction cannot be USED until that
transaction commits. Neither actually bites here -- prod is Supabase PG 15
(where ``ADD VALUE IF NOT EXISTS`` inside a transaction is legal, which is what
002 relies on) and this migration does not consume the value -- but running in
autocommit is the uniform, conservative choice already established for enum
adds in this codebase.

Idempotent and dialect-aware
----------------------------
- ``IF NOT EXISTS`` makes the ALTER a clean no-op on re-run and on a DB that
  already has the value (e.g. one bootstrapped by ``create_all`` from the
  updated model, then stamped) -- same precedent as 002 / 014 / 017 / 026 /
  047 / 052.
- The ALTER runs ONLY on PostgreSQL (guarded by ``_is_postgres``). On SQLite
  (the local dev / pytest ``create_all`` path) there is no native enum type:
  the column is the VARCHAR/CHECK ``create_all`` renders from the model, which
  already includes ``NOT_REQUIRED``, so this migration is a pure no-op there --
  same dialect-guard precedent as 047 / 052.

No data backfill (deliberate -- forward-only fix)
-------------------------------------------------
Historic dock-to-stock receipts are NOT rewritten: rows already stamped
``PASSED`` / ``VISUAL`` keep those values. AS9100D records integrity favors
correcting FORWARD with an effective date over silently rewriting historical
quality records; the tamper-evident ``audit_log`` chain preserves what was
recorded at the time; and for any given historic row we cannot cleanly
distinguish a genuine visual pass from an auto-accept, so a blanket UPDATE
would itself fabricate a claim. The fix is forward-only -- new receipts land as
``NOT_REQUIRED``; this migration only makes that value insertable.

Downgrade
---------
No-op. PostgreSQL cannot drop a value from an enum type without recreating the
type and rewriting every dependent column -- a fragile, lock-heavy operation we
deliberately avoid (same stance as the 002 / 014 / 017 / 026 / 047 / 052
enum-add downgrades). The added value is harmless if unused, so leaving it in
place on downgrade is safe. NOTE: because this is a no-op, downgrading past this
revision leaves ``NOT_REQUIRED`` in the Postgres enum -- harmless, and a
re-upgrade is a clean ``IF NOT EXISTS`` no-op.

Scope note: this migration only ADDS an enum value -- it creates no table -- so
the "ENABLE ROW LEVEL SECURITY on every new table" convention does not apply and
no RLS DDL is emitted.

Locking / operations note
-------------------------
``ALTER TYPE ... ADD VALUE`` is a catalog-only change: it takes a brief lock on
the type, does not rewrite ``po_receipts``, and is effectively instantaneous
regardless of table size. No backfill pass. Deploy ordering: this value must be
insertable before the updated receiving endpoint that writes ``NOT_REQUIRED``
goes live, so run the migration before (or with) that app deploy. Old code that
never emits the value is unaffected.

Revision id ``066_inspection_not_required`` is 27 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "066_inspection_not_required"
down_revision = "065_display_token_setup_code"
branch_labels = None
depends_on = None

# Native Postgres enum type backing po_receipts.inspection_status. SQLAlchemy
# lower-cases the InspectionStatus class name for the unnamed SQLEnum, so the
# type is "inspectionstatus". The column has NO values_callable, so it stores the
# enum MEMBER NAME -- the label added here is the uppercase name, NOT the
# lowercase .value ("not_required"). Mirrors
# app/models/purchasing.py::InspectionStatus.NOT_REQUIRED.
ENUM_TYPE_NAME = "inspectionstatus"
NEW_ENUM_VALUES = ("NOT_REQUIRED",)


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def upgrade() -> None:
    conn = op.get_bind()

    # Postgres-only: on SQLite/others inspection_status is a VARCHAR/CHECK whose
    # values come from the model, so create_all already has NOT_REQUIRED and this
    # is a no-op.
    if not _is_postgres(conn):
        return

    # ALTER TYPE ... ADD VALUE runs outside the surrounding migration transaction
    # (autocommit). IF NOT EXISTS keeps the statement idempotent.
    with op.get_context().autocommit_block():
        for value in NEW_ENUM_VALUES:
            op.execute(f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # No-op: PostgreSQL cannot drop a value from an enum type without recreating
    # the type and rewriting every dependent column. The added value is harmless
    # if unused. Same stance as the 002 / 014 / 017 / 026 / 047 / 052 enum-add
    # downgrades. See the module docstring for the records-integrity rationale
    # behind NOT backfilling historic rows.
    pass
