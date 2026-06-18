"""Receiving thermal-label printing: company_print_profiles + po_receipts.label_document_id

Revision ID: 051_receiving_label
Revises: 050_display_tokens
Create Date: 2026-06-18

Context
-------
The receiving thermal-label printing feature adds a per-company ProxyBox /
WHTP203e printer configuration and lets a PO receipt reference the 4x6 label it
produced. The models live in ``app/models/print_profile.py::CompanyPrintProfile``
(new table) and ``app/models/purchasing.py::POReceipt`` (gains the nullable
``label_document_id`` FK). The new ``DocumentType.RECEIVING_LABEL`` enum value
that the stored label rows use is added SEPARATELY in 052 -- ``ALTER TYPE ... ADD
VALUE`` must escape the migration transaction (autocommit), so it is isolated in
its own revision exactly as 047 isolated the shipping-label enum values from the
046 schema work (see 052's docstring).

What this migration does
------------------------
1. Creates ``company_print_profiles`` (TenantMixin + OptimisticLockMixin), one row
   per company (UNIQUE company_id, ``uq_company_print_profile_company``). Mirrors
   ``company_shipping_profiles`` from 046: holds the ProxyBox connection, the
   Fernet-encrypted API key (``encrypted_api_key`` + display-only ``api_key_last4``,
   the plaintext is never stored), print defaults, ``auto_print_on_receipt``, and
   ``allow_print_egress`` -- the per-company outbound-egress kill switch, ``NOT
   NULL`` and DEFAULTS FALSE (server_default 'false'), same shape as 046's
   ``allow_carrier_egress``.
2. ALTERs ``po_receipts``: adds the NULLABLE ``label_document_id`` column and a
   named FK to ``documents.id`` (Postgres-only ADD CONSTRAINT, same handling as the
   046 shipments label_document_id FK). NULLABLE -> online-safe ADD COLUMN, no
   table rewrite, set only once a label is rendered.

Tenant / compliance shape
-------------------------
``company_print_profiles`` uses ``TenantMixin`` -> non-null, indexed ``company_id``
FK to ``companies.id`` (same shape as ``company_shipping_profiles`` in 046 /
``display_tokens`` in 050). Every query against it MUST be company-scoped. The
OptimisticLockMixin columns are ``version`` (NOT NULL, server_default '1') /
``updated_at`` (NOT NULL, server_default 'now()'). This migration does NOT touch
the tamper-evident ``audit_log`` table, writes no data, and backfills nothing. No
SoftDeleteMixin on this table (config row; deactivate via ``is_active`` rather than
delete), so no soft-delete columns to preserve.

Lock-step with the models (load-bearing)
-----------------------------------------
The column list, FKs, unique constraint, and indexes below are kept byte-for-byte
in lock-step with ``CompanyPrintProfile.__table__`` and the ``po_receipts`` ALTER
mirrors ``POReceipt.label_document_id`` so the ``create_all`` bootstrap path
(docs/DEVELOPMENT.md) and a Postgres ``alembic upgrade`` converge on the IDENTICAL
schema. The model declares ``id`` with ``index=True`` and ``company_id`` indexed
(TenantMixin), so ``ix_company_print_profiles_id`` /
``ix_company_print_profiles_company_id`` are recreated here. ``po_receipts``
declares ``label_document_id`` with NO ``index=True``, so -- matching the 046
shipments precedent -- only the FK is created, no index (an index the model does
not declare would diverge create_all from upgrade).

Server-default notes (autogenerate misses these)
------------------------------------------------
The model leaves ``default_paper_size`` / ``default_copies`` / ``is_active`` as
Python-side ``default=`` only (NO ``server_default``) and they are NULLABLE on the
table, exactly like ``carrier_accounts.is_active`` in 046; those are emitted here as
plain nullable columns to stay in lock-step with create_all. Only
``auto_print_on_receipt`` and ``allow_print_egress`` are NOT NULL with
server_default 'false' (the model declares both ``nullable=False,
server_default="false"``), so existing-row backfill is automatic and they are
written that way below.

Idempotent and reversible
-------------------------
- Upgrade guards ``create_table`` with ``_has_table``, every ``create_index`` with
  ``_has_index``, the ``add_column`` with ``_has_column``, and the FK with
  ``_has_fk`` (precedents: 046 / 050). Bootstrap is ``create_all -> stamp ->
  upgrade`` (docs/DEVELOPMENT.md): a DB bootstrapped from the updated models already
  has the table, the column, and (on Postgres) the FK when this runs over the
  stamp, so the guards make it a clean no-op. Re-runs are likewise no-ops.
- Downgrade drops the ``po_receipts`` FK (Postgres only) and column, then the
  ``company_print_profiles`` indexes (reverse order) and the table, all guarded, so
  it round-trips cleanly on Postgres and on the SQLite used for local dev / pytest.

Locking / operations note
-------------------------
``company_print_profiles`` is a brand-new empty table: CREATE TABLE + index builds
are instantaneous and take no lock on any existing table. The ``po_receipts`` ALTER
is a single ADD COLUMN of a NULLABLE column (metadata-only, no rewrite). No
backfill. No deploy-ordering constraint: old application code never references the
column/table; new code only reads/writes them after this migration (or a create_all
bootstrap) has run. NOTE: a POReceipt row cannot store the RECEIVING_LABEL document
type until 052 has also run -- 052 must be applied together with (immediately
after) this revision before the auto-print path is enabled.

Revision id ``051_receiving_label`` is 18 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "051_receiving_label"
down_revision = "050_display_tokens"
branch_labels = None
depends_on = None

PRINT_PROFILES = "company_print_profiles"
PO_RECEIPTS = "po_receipts"

# Named FK from po_receipts.label_document_id -> documents.id. Named so downgrade
# can drop it explicitly on Postgres (same pattern as 046's shipments FKs).
PO_RECEIPTS_LABEL_FK = "fk_po_receipts_label_document_id"

# (index_name, columns, unique). Mirrors the model's id index=True + TenantMixin
# company_id index so create_all and upgrade converge.
PRINT_PROFILE_INDEXES = [
    ("ix_company_print_profiles_id", ["id"], False),
    ("ix_company_print_profiles_company_id", ["company_id"], False),
]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _has_fk(table_name: str, fk_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(fk.get("name") == fk_name for fk in _inspector().get_foreign_keys(table_name))


def _create_print_profiles() -> None:
    op.create_table(
        PRINT_PROFILES,
        sa.Column("id", sa.Integer(), nullable=False),
        # TenantMixin -- non-null, indexed company scope (index created below).
        sa.Column("company_id", sa.Integer(), nullable=False),
        # ProxyBox bridge connection (full base incl. /api/v1 path) + target printer.
        sa.Column("proxybox_base_url", sa.String(length=255), nullable=True),
        sa.Column("proxybox_target", sa.String(length=120), nullable=True),
        # Fernet-encrypted API key (plaintext never stored) + display-only last4.
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("api_key_last4", sa.String(length=8), nullable=True),
        # Print defaults. Model uses Python-side default= only (no server_default),
        # nullable -> emitted as plain nullable columns to match create_all.
        sa.Column("default_paper_size", sa.String(length=20), nullable=True),
        sa.Column("default_copies", sa.Integer(), nullable=True),
        # Auto-print-on-receipt gate -- NOT NULL, defaults OFF (model server_default).
        sa.Column("auto_print_on_receipt", sa.Boolean(), nullable=False, server_default="false"),
        # Per-company outbound-egress kill switch -- NOT NULL, defaults OFF
        # (mirrors 046's allow_carrier_egress).
        sa.Column("allow_print_egress", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        # OptimisticLockMixin
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", name="uq_company_print_profile_company"),
    )


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create company_print_profiles (guarded for the create_all-bootstrap /
    #    re-run no-op path).
    if not _has_table(PRINT_PROFILES):
        _create_print_profiles()
    for index_name, columns, unique in PRINT_PROFILE_INDEXES:
        if not _has_index(PRINT_PROFILES, index_name):
            op.create_index(index_name, PRINT_PROFILES, columns, unique=unique)

    # 2. ALTER po_receipts: add the NULLABLE label_document_id column (online-safe).
    if not _has_column(PO_RECEIPTS, "label_document_id"):
        op.add_column(PO_RECEIPTS, sa.Column("label_document_id", sa.Integer(), nullable=True))

    # 2a. Named FK po_receipts.label_document_id -> documents.id. SQLite cannot ADD
    #     CONSTRAINT after the fact; the create_all bootstrap path already wires it
    #     from the model, so this is Postgres-only (precedent: 046's shipments FKs).
    if _is_postgres(conn) and not _has_fk(PO_RECEIPTS, PO_RECEIPTS_LABEL_FK):
        op.create_foreign_key(
            PO_RECEIPTS_LABEL_FK,
            PO_RECEIPTS,
            "documents",
            ["label_document_id"],
            ["id"],
        )


def downgrade() -> None:
    conn = op.get_bind()

    # 2a. Drop the named FK (Postgres only -- SQLite never created it).
    if _is_postgres(conn) and _has_fk(PO_RECEIPTS, PO_RECEIPTS_LABEL_FK):
        op.drop_constraint(PO_RECEIPTS_LABEL_FK, PO_RECEIPTS, type_="foreignkey")

    # 2. Drop the po_receipts column.
    if _has_column(PO_RECEIPTS, "label_document_id"):
        op.drop_column(PO_RECEIPTS, "label_document_id")

    # 1. Drop company_print_profiles indexes (reverse order) then the table.
    if _has_table(PRINT_PROFILES):
        for index_name, _columns, _unique in reversed(PRINT_PROFILE_INDEXES):
            if _has_index(PRINT_PROFILES, index_name):
                op.drop_index(index_name, table_name=PRINT_PROFILES)
        op.drop_table(PRINT_PROFILES)
