"""Add certificates_of_conformance table (Batch 11C / G6-B Certificate of Conformance)

Revision ID: 044_certificate_of_conformance
Revises: 043_wc_required_cert_type
Create Date: 2026-06-08

Context
-------
G6-B introduces a real Certificate of Conformance (CoC) artifact. The decision is
a DB **frozen-snapshot** model (``CertificateOfConformance`` in
``app/models/shipping.py``): the row stores the immutable certified facts -- the
part/lot/serial/quantity snapshot plus the full rendered content -- captured at
issue time, and the PDF is rendered DETERMINISTICALLY on download from those
stored facts. There is no filesystem blob.

Scope / compliance shape
------------------------
- Tenant-scoped: the table uses ``TenantMixin``, giving a non-null, indexed
  ``company_id`` FK to ``companies.id`` -- the same shape every other TenantMixin
  table declares in its migration (cf. ``laser_nests`` in 036). Every query against
  it MUST be company-scoped.
- A CoC is an APPEND-ONLY issued compliance record (like an audit entry), so it
  deliberately does NOT use ``SoftDeleteMixin`` -- there are no soft-delete columns
  and nothing here hard-deletes existing rows.
- This migration does NOT touch the tamper-evident ``audit_log`` table.

DB-enforced idempotency (load-bearing)
--------------------------------------
``uq_coc_company_shipment`` UNIQUE ``(company_id, shipment_id)`` enforces at most
one CoC per (company, shipment). This is what makes ``generate_coc_for_shipment``
safe under a concurrent double-ship: the second writer raises ``IntegrityError``,
which the service treats as an idempotent no-op. This mirrors the
``uq_wo_inventory_*`` idempotency precedent from migration 041. A second unique
constraint, ``uq_coc_company_number`` UNIQUE ``(company_id, coc_number)``, keeps
the human-facing certificate number unique per tenant.

Lock-step with the model (load-bearing)
----------------------------------------
The column list, FKs, both unique constraints, and the four non-unique indexes
below are kept byte-for-byte in lock-step with
``CertificateOfConformance.__table__`` so the ``create_all`` bootstrap path
(docs/DEVELOPMENT.md) and a Postgres ``alembic upgrade`` converge on the IDENTICAL
schema. The model declares ``id`` with ``index=True`` and ``shipment_id`` /
``work_order_id`` with ``index=True``, plus ``company_id`` (indexed via
TenantMixin), so all four indexes are emitted on the bootstrap path and are
recreated here. Keep this migration and the model in lock-step.

Idempotent and reversible
-------------------------
- Upgrade guards ``create_table`` with a ``_has_table`` check and each index with a
  ``_has_index`` check (precedent: 036), so a re-run is a clean no-op. Both the
  SQLite (``create_all`` / pytest) path and a Postgres ``upgrade`` produce the same
  table.
- Downgrade drops the indexes then the table, all guarded, so it round-trips
  cleanly.

Locking / operations note
-------------------------
``certificates_of_conformance`` is a brand-new empty table, so ``CREATE TABLE`` +
index builds are instantaneous and take no lock on any existing table. No backfill
and no deploy-ordering constraint relative to the backend rollout.

Revision id is 30 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint documented in docs/DEVELOPMENT.md (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "044_certificate_of_conformance"
down_revision = "043_wc_required_cert_type"
branch_labels = None
depends_on = None

TABLE_NAME = "certificates_of_conformance"

# (index_name, columns). Non-unique indexes, in lock-step with the model:
# id (index=True), company_id (TenantMixin index), shipment_id, work_order_id.
INDEXES = [
    ("ix_certificates_of_conformance_id", ["id"]),
    ("ix_certificates_of_conformance_company_id", ["company_id"]),
    ("ix_certificates_of_conformance_shipment_id", ["shipment_id"]),
    ("ix_certificates_of_conformance_work_order_id", ["work_order_id"]),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(index["name"] == index_name for index in _inspector().get_indexes(table_name))


def upgrade() -> None:
    if not _has_table(TABLE_NAME):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("coc_number", sa.String(length=50), nullable=False),
            sa.Column("shipment_id", sa.Integer(), nullable=False),
            sa.Column("work_order_id", sa.Integer(), nullable=False),
            sa.Column("part_id", sa.Integer(), nullable=True),
            sa.Column("customer_name", sa.String(length=255), nullable=True),
            sa.Column("customer_po", sa.String(length=100), nullable=True),
            sa.Column("part_number", sa.String(length=100), nullable=True),
            sa.Column("part_name", sa.String(length=255), nullable=True),
            sa.Column("revision", sa.String(length=50), nullable=True),
            sa.Column("quantity", sa.Float(), nullable=True),
            sa.Column("lot_number", sa.String(length=100), nullable=True),
            sa.Column("serial_numbers", sa.Text(), nullable=True),
            sa.Column("conformance_statement", sa.Text(), nullable=True),
            sa.Column("content_snapshot", sa.Text(), nullable=True),
            sa.Column("issued_by", sa.Integer(), nullable=True),
            sa.Column("issued_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"]),
            sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"]),
            sa.ForeignKeyConstraint(["part_id"], ["parts.id"]),
            sa.ForeignKeyConstraint(["issued_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", "shipment_id", name="uq_coc_company_shipment"),
            sa.UniqueConstraint("company_id", "coc_number", name="uq_coc_company_number"),
        )

    for index_name, columns in INDEXES:
        if not _has_index(TABLE_NAME, index_name):
            op.create_index(index_name, TABLE_NAME, columns)


def downgrade() -> None:
    if not _has_table(TABLE_NAME):
        return

    for index_name, _columns in reversed(INDEXES):
        if _has_index(TABLE_NAME, index_name):
            op.drop_index(index_name, table_name=TABLE_NAME)

    op.drop_table(TABLE_NAME)
