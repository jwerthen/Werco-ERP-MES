"""Add receiving_label value to the documenttype enum

Revision ID: 052_doc_type_receiving
Revises: 051_receiving_label
Create Date: 2026-06-18

Context
-------
The receiving thermal-label printing feature (051_receiving_label) added a new
``DocumentType`` enum value in ``app/models/document.py``::

    RECEIVING_LABEL = "receiving_label"  # 4x6 thermal label for received inventory (PDF)

The rendered label is stored as a ``Document`` row with
``document_type = RECEIVING_LABEL`` and referenced from
``po_receipts.label_document_id`` (the FK added in 051). That value lands in
``documents.document_type``, which is a NATIVE PostgreSQL enum type. SQLAlchemy
derives the enum type name by lower-casing the Python class name, so the unnamed
``SQLEnum(DocumentType)`` maps to the Postgres type ``documenttype`` (no explicit
``name=`` override on the column -> default lower-cased class name). Verified
against the existing 047 migration, which added ``shipping_label`` /
``bill_of_lading`` to this same ``documenttype`` type. On a fresh install the type
is created by ``Base.metadata.create_all()`` with the full value list (new value
included), so new installs are fine. But on any EXISTING Postgres database the
``documenttype`` enum is missing ``receiving_label``; inserting a Document with
that type then fails at runtime with an invalid-enum-value error. SQLite (the local
create_all / pytest path) has no native enum -- the column is a VARCHAR/CHECK that
already carries the values from the model -- so the test suite never surfaces the
gap. This migration closes that existing-DB gap.

What this migration does
------------------------
On PostgreSQL it runs::

    ALTER TYPE documenttype ADD VALUE IF NOT EXISTS 'receiving_label'

Why a separate revision (load-bearing)
--------------------------------------
``ALTER TYPE ... ADD VALUE`` historically could not run inside a transaction block
at all on older PostgreSQL, and even on supported versions a value added inside a
transaction cannot be USED until that transaction commits. It is therefore run
inside ``op.get_context().autocommit_block()``, which suspends Alembic's
surrounding per-migration transaction and runs the statement in AUTOCOMMIT -- the
same mechanism the repo uses for DDL that must escape the migration transaction
(CREATE INDEX CONCURRENTLY in 039 / 041 / 042; the enum adds in 002 / 014 / 017 /
026 / 047). Keeping the enum ALTER in its own revision (rather than folding it into
051) keeps that autocommit handling cleanly isolated -- the exact split 047 used
relative to 046.

Idempotent and dialect-aware
----------------------------
- ``IF NOT EXISTS`` makes the ALTER a clean no-op on re-run and on any DB that
  already has the value (e.g. one bootstrapped by ``create_all`` after the model was
  updated, then stamped) -- same precedent as 002 / 014 / 017 / 026 / 047.
- The ALTER runs ONLY on PostgreSQL, guarded by ``_is_postgres``. On SQLite (and
  any other dialect) the enum is a plain VARCHAR/CHECK whose allowed values come
  from the model, so ``create_all`` already has the value and this migration is a
  no-op -- same dialect guard precedent as 047.

Downgrade
---------
No-op. PostgreSQL cannot drop a value from an enum type without recreating the type
and rewriting every dependent column -- a fragile, lock-heavy operation we
deliberately avoid (same stance as the 002 / 014 / 017 / 026 / 047 enum-add
downgrades). The added value is harmless if unused, so leaving it in place on
downgrade is safe and does not affect the prior schema's behavior. NOTE: because
this is a no-op, downgrading PAST 051 leaves ``receiving_label`` in the Postgres
enum -- harmless, and a subsequent re-upgrade is a clean ``IF NOT EXISTS`` no-op.

Revision id ``052_doc_type_receiving`` is 22 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "052_doc_type_receiving"
down_revision = "051_receiving_label"
branch_labels = None
depends_on = None

# Native Postgres enum type name backing documents.document_type. SQLAlchemy
# lower-cases the Python class name (DocumentType) for the unnamed SQLEnum, so the
# type is "documenttype" -- same type 047 altered. The value mirrors
# app/models/document.py::DocumentType.RECEIVING_LABEL.
ENUM_TYPE_NAME = "documenttype"
NEW_ENUM_VALUES = ("receiving_label",)


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def upgrade() -> None:
    conn = op.get_bind()

    # Postgres-only: on SQLite/others the enum is a VARCHAR/CHECK whose values come
    # from the model, so create_all already has them and this is a no-op.
    if not _is_postgres(conn):
        return

    # ALTER TYPE ... ADD VALUE must run outside the surrounding migration
    # transaction (autocommit). IF NOT EXISTS keeps the statement idempotent.
    with op.get_context().autocommit_block():
        for value in NEW_ENUM_VALUES:
            op.execute(f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # No-op: PostgreSQL cannot drop a value from an enum type without recreating the
    # type and rewriting every dependent column. The added value is harmless if
    # unused. Same stance as the 002 / 014 / 017 / 026 / 047 enum-add downgrades.
    pass
