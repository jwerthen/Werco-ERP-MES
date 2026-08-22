"""084 - part_number_aliases: retired part numbers keep resolving

Adds ONE new table, ``part_number_aliases``, backing the in-place part renumber
(``POST /parts/{id}/renumber``). One immutable row per retired number, pointing at
the part that now carries a different one, so a number printed on a traveler, an
MTR, a customer PO or a shop spreadsheet still finds the part it always named.

Lock-step with ``app/models/part_number_alias.py``
--------------------------------------------------
Columns, the unique constraint and the three indexes are declared on the model as
well, so the ``create_all`` bootstrap and this migration converge on the same
shape. That convergence is not cosmetic: bootstrap here is ``create_all`` ->
``alembic stamp <baseline>`` -> incremental ``upgrade``, so a fresh database gets
this table from ``create_all`` and then STAMPS PAST this revision. Anything
declared only here and not on the model would be silently missing on every
freshly-provisioned environment while ``alembic_version`` claimed it existed --
the stamped-over hazard that cost prod the ``008`` audit triggers (2026-07-07),
~42 read-path indexes (restored by ``079``) and ``003``'s named FKs and CHECKs
(restored by ``080``). The model is therefore the source of truth and this file
mirrors it, per the ``042``/``078``/``079``/``080`` convention.

RLS
---
``ENABLE ROW LEVEL SECURITY`` on the new table (Postgres only). This revision runs
after ``059``/``060``, which enabled RLS across ``public`` and left it
deny-by-default with zero policies, so a table created afterwards must enable it
itself or it re-flags the Supabase Security Advisor's ``rls_disabled_in_public``
ERROR (precedent ``061``; see ``docs/SUPABASE_SECURITY.md``). The app role bypasses
RLS -- app-layer tenancy via ``tenant_query`` remains the enforcement.

Idempotent and reversible
-------------------------
Every DDL op is guarded (``_has_table`` / ``_has_index``), so a create_all-
bootstrapped database and any re-run are clean no-ops. The downgrade drops the
indexes then the table, in FK-safe order.

Pure DDL. No backfill: there are no retired numbers until someone renumbers a
part, and inventing alias rows for historical data would be fabricating a record
of renames that never happened.

Revision ID: 084_part_number_aliases
Revises: 083_wo_unit_number
"""

import sqlalchemy as sa
from alembic import op

revision = "084_part_number_aliases"
down_revision = "083_wo_unit_number"
branch_labels = None
depends_on = None


TABLE = "part_number_aliases"

# (index_name, columns, unique) -- mirrored on the model.
INDEXES = [
    ("ix_part_number_aliases_id", ["id"], False),
    ("ix_part_number_aliases_part_id", ["part_id"], False),
    ("ix_part_number_aliases_company_id", ["company_id"], False),
]

# Enforced as a real UNIQUE constraint (not an index) so it matches the model's
# __table_args__ UniqueConstraint exactly.
UNIQUE_CONSTRAINT = "uq_part_number_aliases_company_key"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _create_table() -> None:
    # Column order matches create_all's emission order: the class's own columns
    # first, then TenantMixin's company_id (appended via MRO).
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        # String(100) matches parts.part_number -- NOT the narrower PartNumber
        # annotated type. Production holds numbers that type rejects (sheet stock
        # like '1/4" PLATE 48 X 96'), and those are exactly the rows most likely
        # to be renumbered. See the model docstring.
        sa.Column("alias_number", sa.String(length=100), nullable=False),
        sa.Column("alias_number_key", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["part_id"], ["parts.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "alias_number_key", name=UNIQUE_CONSTRAINT),
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1) The table. Guarded so the create_all bootstrap path (model already built
    #    it) no-ops rather than erroring.
    if not _has_table(TABLE):
        _create_table()

    # 2) Indexes, each guarded independently -- create_all emits them too.
    for index_name, columns, unique in INDEXES:
        if not _has_index(TABLE, index_name):
            op.create_index(index_name, TABLE, columns, unique=unique)

    # 3) RLS on the new table (Postgres only; runs after 059/060). Deny-by-default
    #    with zero policies; the app role bypasses it and app-layer tenancy stays
    #    the enforcement.
    if is_postgres:
        op.execute(f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    # Indexes first, then the table (FK-safe order). Each guarded so a partial
    # upgrade downgrades cleanly.
    for index_name, _columns, _unique in INDEXES:
        if _has_index(TABLE, index_name):
            op.drop_index(index_name, table_name=TABLE)
    if _has_table(TABLE):
        op.drop_table(TABLE)
