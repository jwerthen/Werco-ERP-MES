"""Add shipping_label / bill_of_lading values to the documenttype enum

Revision ID: 047_doc_type_shipping
Revises: 046_carrier_shipping
Create Date: 2026-06-09

Context
-------
The multi-carrier shipping integration (046_carrier_shipping) added two new
``DocumentType`` enum values in ``app/models/document.py``::

    SHIPPING_LABEL = "shipping_label"   # purchased carrier parcel label (PDF/PNG/ZPL)
    BILL_OF_LADING = "bill_of_lading"   # purchased LTL freight Bill of Lading

Those values are stored in ``documents.document_type``, which is a NATIVE
PostgreSQL enum type. SQLAlchemy derives the enum type name by lower-casing the
Python class name, so ``SQLEnum(DocumentType)`` maps to the Postgres type
``documenttype`` (no explicit ``name=`` override on the column -> default
lower-cased class name). On a fresh install the type is created by
``Base.metadata.create_all()`` with the full value list, so new installs are
fine. But 046 was authored before these two values existed and never altered the
type, so on any EXISTING Postgres database the ``documenttype`` enum is missing
``shipping_label`` and ``bill_of_lading``. Inserting a Document with either type
then fails at runtime with an invalid-enum-value error. SQLite (the local
create_all / pytest path) has no native enum -- the column is a VARCHAR/CHECK that
already carries the values from the model -- so the test suite never surfaced the
gap. This migration closes that existing-DB gap.

What this migration does
------------------------
On PostgreSQL it runs::

    ALTER TYPE documenttype ADD VALUE IF NOT EXISTS 'shipping_label'
    ALTER TYPE documenttype ADD VALUE IF NOT EXISTS 'bill_of_lading'

Idempotent and dialect-aware
----------------------------
- ``IF NOT EXISTS`` makes each ALTER a clean no-op on re-run and on any DB that
  already has the value (e.g. one created by ``create_all`` after the model was
  updated) -- same precedent as 002 (``workcentertype``), 014/017 (``parttype``),
  and 026 (``userrole``).
- The ALTERs run ONLY on PostgreSQL, guarded by ``_is_postgres``. On SQLite (and
  any other dialect) the enum is a plain VARCHAR/CHECK whose allowed values come
  from the model, so ``create_all`` already has them and this migration is a
  no-op -- same dialect guard precedent as 045 / 046's Postgres-only FK block.

Transaction handling (load-bearing)
-----------------------------------
``ALTER TYPE ... ADD VALUE`` historically could not run inside a transaction
block at all on older PostgreSQL, and even on supported versions a value added in
a transaction cannot be USED until that transaction commits. This migration only
ADDS values (it never inserts a row using them), but to keep the enum ALTER
isolated with its own commit -- and to remain correct on older PG -- the ALTERs
run inside an ``op.get_context().autocommit_block()``, which suspends Alembic's
surrounding per-migration transaction and runs the statements in AUTOCOMMIT. This
is the same mechanism the repo uses for DDL that must escape the migration
transaction (CREATE INDEX CONCURRENTLY in 039 / 041 / 042). Keeping the ALTERs in
their own migration (rather than folding them into 046) keeps that autocommit
handling cleanly isolated.

Downgrade
---------
No-op. PostgreSQL cannot drop a value from an enum type without recreating the
type and rewriting every dependent column -- a fragile, lock-heavy operation we
deliberately avoid (same stance as 002 / 014 / 017 / 026 downgrades). The two
added values are harmless if unused, so leaving them in place on downgrade is
safe and does not affect the prior schema's behavior.

Revision id ``047_doc_type_shipping`` is 21 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "047_doc_type_shipping"
down_revision = "046_carrier_shipping"
branch_labels = None
depends_on = None

# Native Postgres enum type name backing documents.document_type. SQLAlchemy
# lower-cases the Python class name (DocumentType) for the unnamed SQLEnum, so the
# type is "documenttype". The values mirror app/models/document.py::DocumentType.
ENUM_TYPE_NAME = "documenttype"
NEW_ENUM_VALUES = ("shipping_label", "bill_of_lading")


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def upgrade() -> None:
    conn = op.get_bind()

    # Postgres-only: on SQLite/others the enum is a VARCHAR/CHECK whose values come
    # from the model, so create_all already has them and this is a no-op.
    if not _is_postgres(conn):
        return

    # ALTER TYPE ... ADD VALUE must run outside the surrounding migration
    # transaction (autocommit). IF NOT EXISTS keeps each statement idempotent.
    with op.get_context().autocommit_block():
        for value in NEW_ENUM_VALUES:
            op.execute(f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # No-op: PostgreSQL cannot drop a value from an enum type without recreating
    # the type and rewriting every dependent column. The added values are harmless
    # if unused. Same stance as the 002 / 014 / 017 / 026 enum-add downgrades.
    pass
