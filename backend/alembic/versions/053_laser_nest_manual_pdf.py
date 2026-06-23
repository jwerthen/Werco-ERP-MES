"""Laser nest manual CNC number, reference PDF, and soft delete

Revision ID: 053_laser_nest_manual
Revises: 052_doc_type_receiving
Create Date: 2026-06-23

Context
-------
The laser-nest enhancement lets an operator create a *manual* nest -- one with a
hand-/machine-keyed program number and no uploaded CNC file -- attach a reference
PDF, and (soft) delete it. The model changes live in
``app/models/laser_nest.py::LaserNest``:

- ``cnc_number`` (``String(100)``, NULLABLE, indexed) -- the operator-facing
  program number. Deliberately NOT unique: the same program number recurs across
  jobs/materials, so no unique constraint is added.
- ``document_id`` (``Integer`` FK -> ``documents.id``, NULLABLE, indexed) plus a
  ``relationship("Document")`` -- an optional reference PDF stored via the existing
  ``Document`` model. Mirrors ``Shipment.label_document_id`` in
  ``app/models/shipping.py`` (and ``po_receipts.label_document_id`` from 051).
- ``cnc_file_name`` RELAXED to NULLABLE -- manual nests have no uploaded file.
- ``SoftDeleteMixin`` added to ``LaserNest`` -> ``is_deleted`` (NOT NULL, default
  false, indexed), ``deleted_at`` (NULLABLE), ``deleted_by`` (NULLABLE). The
  ``LaserNestPackage.nests`` relationship keeps its ``cascade="all,
  delete-orphan"`` (untouched here); the new delete PATH (a later pass) soft-deletes
  instead of relying on that cascade -- this migration only makes the columns exist.

What this migration does (on EXISTING databases)
------------------------------------------------
ALTERs ``laser_nests``:
1. ADD ``cnc_number VARCHAR(100) NULL`` + index ``ix_laser_nests_cnc_number``.
2. ADD ``document_id INTEGER NULL`` + named FK ``fk_laser_nests_document_id`` ->
   ``documents.id`` (Postgres-only ADD CONSTRAINT, same handling as 046/051) +
   index ``ix_laser_nests_document_id``.
3. ADD SoftDeleteMixin columns: ``is_deleted BOOLEAN NOT NULL DEFAULT false`` +
   index ``ix_laser_nests_is_deleted``, ``deleted_at TIMESTAMP NULL``,
   ``deleted_by INTEGER NULL``.
4. RELAX ``cnc_file_name`` to NULLABLE (Postgres-only -- see below).
5. BACKFILL ``cnc_number`` from the ``cnc_file_name`` stem (filename without its
   final extension) for existing rows, so current nests get a sensible CNC number.

Why ``cnc_file_name`` relax is Postgres-only
--------------------------------------------
``ALTER COLUMN ... DROP NOT NULL`` is a metadata-only, instant change on Postgres.
SQLite cannot drop NOT NULL in place -- it requires a full table rebuild
(``batch_alter_table``), which on ``laser_nests`` would have to reconstruct the two
unique constraints and every index. The SQLite path here is the create_all
bootstrap (docs/DEVELOPMENT.md ``create_all`` -> ``stamp`` -> ``upgrade``): a DB
built from the updated model ALREADY has ``cnc_file_name`` nullable, so the relax is
a no-op on SQLite and we skip the fragile rebuild. Same Postgres-only precedent as
051's FK ADD CONSTRAINT.

Backfill -- ``cnc_number`` from the ``cnc_file_name`` stem
----------------------------------------------------------
Only touches rows where ``cnc_number IS NULL AND cnc_file_name IS NOT NULL`` -- so it
is idempotent (a second run finds no NULL ``cnc_number`` to fill) and never
overwrites a value. The stem is the filename with its FINAL extension removed
(``NEST_A.cnc`` -> ``NEST_A``; ``NEST.B.din`` -> ``NEST.B``; a name with no dot is
taken whole, matching ``os.path.splitext``). Dialect-specific:
- Postgres: a single set-based UPDATE using
  ``regexp_replace(cnc_file_name, '\\.[^.]*$', '')``.
- SQLite / others: row-by-row in Python with ``os.path.splitext`` (SQLite has no
  REVERSE and the last-dot trick in raw SQL is fragile -- this dev/test path is
  small, so the explicit loop is exact and clear).

Tenant / compliance shape
-------------------------
``laser_nests`` already carries ``TenantMixin`` (non-null indexed ``company_id``);
this migration does not touch it. It adds ``SoftDeleteMixin`` so deletes are soft --
no destructive cleanup, no hard DELETE. It does NOT touch the tamper-evident
``audit_log`` table and backfills no audit rows. The only data written is the
``cnc_number`` backfill on ``laser_nests`` itself.

Idempotent and reversible
-------------------------
- Upgrade guards every ``add_column`` with ``_has_column``, every ``create_index``
  with ``_has_index``, and the FK with ``_has_fk`` (precedents 036/046/051). On the
  create_all-bootstrap path the columns/indexes/FK already exist, so each guard is a
  clean no-op; re-runs are likewise no-ops. The nullable relax only fires on
  Postgres and only when the column is currently NOT NULL.
- Downgrade drops the FK (Postgres only), then the indexes and columns it added
  (reverse order), and restores ``cnc_file_name NOT NULL`` on Postgres -- but ONLY
  if no row has a NULL ``cnc_file_name`` (a manual nest), since re-adding NOT NULL
  over NULLs would fail. If manual nests exist the relax is left in place and a
  notice is emitted (documented, safe: the prior schema simply tolerated the wider
  nullability). SQLite downgrade drops columns/indexes via ``batch_alter_table``
  guards but leaves ``cnc_file_name`` as the model defines it.

Revision id ``053_laser_nest_manual`` is 20 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import os

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "053_laser_nest_manual"
down_revision = "052_doc_type_receiving"
branch_labels = None
depends_on = None

LASER_NESTS = "laser_nests"

# Named FK laser_nests.document_id -> documents.id. Named so downgrade can drop it
# explicitly on Postgres (same pattern as 046's shipments FKs / 051's po_receipts).
DOCUMENT_FK = "fk_laser_nests_document_id"

# (index_name, column). Mirrors the model: cnc_number index=True, document_id
# index=True, is_deleted index=True (SoftDeleteMixin) -- so create_all and upgrade
# converge on the same indexes.
NEW_INDEXES = [
    ("ix_laser_nests_cnc_number", "cnc_number"),
    ("ix_laser_nests_document_id", "document_id"),
    ("ix_laser_nests_is_deleted", "is_deleted"),
]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _column(table_name: str, column_name: str):
    if not _has_table(table_name):
        return None
    for col in _inspector().get_columns(table_name):
        if col["name"] == column_name:
            return col
    return None


def _has_column(table_name: str, column_name: str) -> bool:
    return _column(table_name, column_name) is not None


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _has_fk(table_name: str, fk_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(fk.get("name") == fk_name for fk in _inspector().get_foreign_keys(table_name))


def _backfill_cnc_number(conn) -> None:
    """Fill cnc_number from the cnc_file_name stem for existing rows.

    Idempotent: only touches rows with NULL cnc_number and a non-NULL
    cnc_file_name. Strips the final extension from the filename.
    """
    if not _has_column(LASER_NESTS, "cnc_number") or not _has_column(LASER_NESTS, "cnc_file_name"):
        return

    if _is_postgres(conn):
        # Set-based: regexp_replace strips the last '.ext' (no dot -> name whole).
        op.execute(
            sa.text(
                "UPDATE laser_nests "
                "SET cnc_number = regexp_replace(cnc_file_name, '\\.[^.]*$', '') "
                "WHERE cnc_number IS NULL AND cnc_file_name IS NOT NULL"
            )
        )
    else:
        # SQLite / others: compute the stem in Python (os.path.splitext) per row.
        rows = conn.execute(
            sa.text(
                "SELECT id, cnc_file_name FROM laser_nests " "WHERE cnc_number IS NULL AND cnc_file_name IS NOT NULL"
            )
        ).fetchall()
        update = sa.text("UPDATE laser_nests SET cnc_number = :stem WHERE id = :id")
        for row in rows:
            stem = os.path.splitext(row.cnc_file_name)[0]
            conn.execute(update, {"stem": stem, "id": row.id})


def upgrade() -> None:
    conn = op.get_bind()

    # 1. cnc_number column + index.
    if not _has_column(LASER_NESTS, "cnc_number"):
        op.add_column(LASER_NESTS, sa.Column("cnc_number", sa.String(length=100), nullable=True))

    # 2. document_id column + named FK (Postgres-only ADD CONSTRAINT) + index.
    if not _has_column(LASER_NESTS, "document_id"):
        op.add_column(LASER_NESTS, sa.Column("document_id", sa.Integer(), nullable=True))
    if _is_postgres(conn) and not _has_fk(LASER_NESTS, DOCUMENT_FK):
        op.create_foreign_key(DOCUMENT_FK, LASER_NESTS, "documents", ["document_id"], ["id"])

    # 3. SoftDeleteMixin columns. is_deleted is NOT NULL with server_default 'false'
    #    so existing rows backfill automatically to "not deleted".
    if not _has_column(LASER_NESTS, "is_deleted"):
        op.add_column(
            LASER_NESTS,
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        )
    if not _has_column(LASER_NESTS, "deleted_at"):
        op.add_column(LASER_NESTS, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column(LASER_NESTS, "deleted_by"):
        op.add_column(LASER_NESTS, sa.Column("deleted_by", sa.Integer(), nullable=True))

    # Indexes for cnc_number / document_id / is_deleted (match the model).
    for index_name, column in NEW_INDEXES:
        if _has_column(LASER_NESTS, column) and not _has_index(LASER_NESTS, index_name):
            op.create_index(index_name, LASER_NESTS, [column])

    # 4. Relax cnc_file_name to NULLABLE. Postgres-only metadata change; on SQLite
    #    the create_all bootstrap already built it nullable from the model.
    col = _column(LASER_NESTS, "cnc_file_name")
    if _is_postgres(conn) and col is not None and not col["nullable"]:
        op.alter_column(
            LASER_NESTS,
            "cnc_file_name",
            existing_type=sa.String(length=255),
            nullable=True,
        )

    # 5. Backfill cnc_number from the cnc_file_name stem (all dialects).
    _backfill_cnc_number(conn)


def downgrade() -> None:
    conn = op.get_bind()

    # Restore cnc_file_name NOT NULL (Postgres only), but ONLY if no NULL exists --
    # re-adding NOT NULL over a manual nest's NULL filename would fail. If manual
    # nests are present, leave the column nullable (wider nullability is harmless to
    # the prior schema) and emit a notice.
    if _is_postgres(conn):
        col = _column(LASER_NESTS, "cnc_file_name")
        if col is not None and col["nullable"]:
            null_count = conn.execute(sa.text("SELECT COUNT(*) FROM laser_nests WHERE cnc_file_name IS NULL")).scalar()
            if null_count == 0:
                op.alter_column(
                    LASER_NESTS,
                    "cnc_file_name",
                    existing_type=sa.String(length=255),
                    nullable=False,
                )
            else:
                print(
                    f"[053 downgrade] laser_nests has {null_count} row(s) with NULL cnc_file_name "
                    "(manual nests); leaving cnc_file_name NULLABLE to avoid a failed NOT NULL "
                    "re-add. This is safe: the prior schema tolerates the wider nullability."
                )

    # Drop the FK first (Postgres only -- SQLite never created it).
    if _is_postgres(conn) and _has_fk(LASER_NESTS, DOCUMENT_FK):
        op.drop_constraint(DOCUMENT_FK, LASER_NESTS, type_="foreignkey")

    # Drop indexes (reverse order), guarded.
    for index_name, _col in reversed(NEW_INDEXES):
        if _has_index(LASER_NESTS, index_name):
            op.drop_index(index_name, table_name=LASER_NESTS)

    # Drop the columns this migration added (reverse order), guarded.
    for column_name in ("deleted_by", "deleted_at", "is_deleted", "document_id", "cnc_number"):
        if _has_column(LASER_NESTS, column_name):
            op.drop_column(LASER_NESTS, column_name)
