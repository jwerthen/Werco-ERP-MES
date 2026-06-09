"""Multi-carrier shipping integration: carrier accounts, shipping profiles,
shipment packages / rate quotes / tracking events, and Shipment carrier columns.

Revision ID: 046_carrier_shipping
Revises: 045_skillmatrix_company_uq
Create Date: 2026-06-09

Context
-------
The multi-carrier shipping integration (EasyPost / Zenkraft aggregator) adds the
schema behind rate-shop, buy-label/BOL, tracking, and the per-company egress kill
switch. The models live in ``app/models/carrier_account.py`` (CarrierAccount,
CompanyShippingProfile) and ``app/models/shipping.py`` (Shipment gains carrier +
financial + tracking columns and SoftDeleteMixin; new ShipmentPackage /
ShipmentRateQuote / ShipmentTrackingEvent child tables).

What this migration does
------------------------
1. Creates ``carrier_accounts`` (TenantMixin + SoftDeleteMixin + OptimisticLockMixin).
   Holds Fernet-encrypted aggregator credentials; soft-deleted (never hard-deleted)
   because purchased labels/BOLs reference it. UNIQUE (company_id, name).
2. Creates ``company_shipping_profiles`` (TenantMixin + OptimisticLockMixin), one
   row per company (UNIQUE company_id). Carries the decomposed ship-from origin,
   package defaults, and ``allow_carrier_egress`` -- the per-company customer-data
   egress kill switch, ``NOT NULL`` and DEFAULTS FALSE (server_default 'false').
3. ALTERs ``shipments``: adds carrier/financial/tracking/freight columns, all
   NULLABLE so the legacy manual flow is unchanged (no table rewrite, online-safe),
   the carrier_account_id / label_document_id / bol_document_id FKs, and the three
   SoftDeleteMixin columns (is_deleted NOT NULL server_default 'false' + index,
   deleted_at, deleted_by). carrier_accounts is created FIRST so the
   carrier_account_id FK target exists.
4. Creates ``shipment_packages`` (TenantMixin + SoftDeleteMixin),
   ``shipment_rate_quotes`` (TenantMixin), ``shipment_tracking_events`` (TenantMixin)
   -- child tables FK to shipments.id.

Tenant / compliance shape
-------------------------
Every new table uses ``TenantMixin`` -> non-null, indexed ``company_id`` FK to
``companies.id`` (same shape as ``laser_nests`` in 036 / ``certificates_of_conformance``
in 044). Every query against these MUST be company-scoped. SoftDeleteMixin tables
get ``is_deleted`` (NOT NULL, server_default 'false', indexed) / ``deleted_at`` /
``deleted_by``; OptimisticLockMixin tables get ``version`` (NOT NULL, server_default
'1') / ``updated_at`` (NOT NULL, server_default 'now()'). This migration does NOT
touch the tamper-evident ``audit_log`` table and never hard-deletes rows.

Partial unique idempotency index (load-bearing)
-----------------------------------------------
``uq_shipment_idempotency`` UNIQUE on ``shipments (company_id, idempotency_key)``
WHERE ``idempotency_key IS NOT NULL``. The PARTIAL predicate is essential: legacy
shipments (and any non-carrier shipment) have ``idempotency_key = NULL`` and must
NOT collide with each other. It backs the buy-label / buy-bol idempotency guard so
a concurrent double-buy surfaces ``IntegrityError`` (the service treats it as a
no-op -- same precedent as ``uq_wo_inventory_*`` in 041 and ``uq_coc_*`` in 044).
Autogenerate does NOT emit the WHERE clause, so it is hand-written here. SQLite (the
local create_all / pytest path) supports partial indexes too, so the predicate is
applied via ``sqlite_where`` as well -- the model's ``__table_args__`` would emit the
identical partial index on the create_all bootstrap path; keep them in lock-step.

Lock-step with the models (load-bearing)
-----------------------------------------
The column lists, FKs, unique constraints, and indexes below are kept byte-for-byte
in lock-step with the model ``__table__`` definitions so the ``create_all`` bootstrap
path (docs/DEVELOPMENT.md) and a Postgres ``alembic upgrade`` converge on the
IDENTICAL schema. Models declare ``id`` with ``index=True``, ``company_id`` indexed
(TenantMixin), ``shipment_id`` with ``index=True``, ``is_deleted`` indexed
(SoftDeleteMixin), and ``aggregator_shipment_id`` with ``index=True`` -- all
recreated here.

Idempotent and reversible
-------------------------
- Upgrade guards every ``create_table`` with ``_has_table``, every ``add_column``
  with ``_has_column``, and every ``create_index`` with ``_has_index`` (precedents:
  036 / 037 / 044), so a re-run is a clean no-op.
- Downgrade drops the child tables, the shipments alterations (indexes + columns),
  then ``company_shipping_profiles`` and ``carrier_accounts`` -- in FK-safe reverse
  order, all guarded -- so it round-trips cleanly.

Locking / operations note
-------------------------
The new tables are brand-new and empty: ``CREATE TABLE`` + index builds are
instantaneous and take no lock on any existing table. The ``shipments`` ALTERs are
all ADD COLUMN of NULLABLE columns (the ``is_deleted`` server_default 'false' fills
existing rows without a rewrite on modern Postgres), so they are metadata-only / no
table rewrite. No backfill and no deploy-ordering constraint relative to the backend
rollout; all new Shipment columns are nullable, so a pre-rollout schema is fully
backward compatible with the legacy manual flow.

Revision id is 19 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint documented in docs/DEVELOPMENT.md (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "046_carrier_shipping"
down_revision = "045_skillmatrix_company_uq"
branch_labels = None
depends_on = None

SHIPMENTS = "shipments"
CARRIER_ACCOUNTS = "carrier_accounts"
SHIPPING_PROFILES = "company_shipping_profiles"
SHIPMENT_PACKAGES = "shipment_packages"
RATE_QUOTES = "shipment_rate_quotes"
TRACKING_EVENTS = "shipment_tracking_events"

# Partial unique idempotency index on shipments (see module docstring).
SHIPMENT_IDEMPOTENCY_INDEX = "uq_shipment_idempotency"
SHIPMENT_IDEMPOTENCY_WHERE = "idempotency_key IS NOT NULL"

# New Shipment columns (name, column factory). All NULLABLE -> online-safe ADD COLUMN.
# Kept in lock-step with app/models/shipping.py::Shipment.
SHIPMENT_NEW_COLUMNS = [
    ("carrier_account_id", lambda: sa.Column("carrier_account_id", sa.Integer(), nullable=True)),
    ("ship_mode", lambda: sa.Column("ship_mode", sa.String(length=20), nullable=True)),
    ("aggregator_shipment_id", lambda: sa.Column("aggregator_shipment_id", sa.String(length=120), nullable=True)),
    ("selected_rate_id", lambda: sa.Column("selected_rate_id", sa.String(length=120), nullable=True)),
    ("service_code", lambda: sa.Column("service_code", sa.String(length=80), nullable=True)),
    ("label_document_id", lambda: sa.Column("label_document_id", sa.Integer(), nullable=True)),
    ("bol_document_id", lambda: sa.Column("bol_document_id", sa.Integer(), nullable=True)),
    ("estimated_cost", lambda: sa.Column("estimated_cost", sa.Numeric(precision=12, scale=2), nullable=True)),
    ("actual_cost", lambda: sa.Column("actual_cost", sa.Numeric(precision=12, scale=2), nullable=True)),
    ("cost_currency", lambda: sa.Column("cost_currency", sa.String(length=3), nullable=True)),
    ("label_purchased_at", lambda: sa.Column("label_purchased_at", sa.DateTime(), nullable=True)),
    ("voided_at", lambda: sa.Column("voided_at", sa.DateTime(), nullable=True)),
    ("refund_status", lambda: sa.Column("refund_status", sa.String(length=20), nullable=True)),
    ("tracking_status", lambda: sa.Column("tracking_status", sa.String(length=30), nullable=True)),
    ("tracking_status_detail", lambda: sa.Column("tracking_status_detail", sa.String(length=255), nullable=True)),
    ("last_tracking_sync_at", lambda: sa.Column("last_tracking_sync_at", sa.DateTime(), nullable=True)),
    ("freight_class", lambda: sa.Column("freight_class", sa.String(length=10), nullable=True)),
    ("nmfc_code", lambda: sa.Column("nmfc_code", sa.String(length=20), nullable=True)),
    ("pallet_count", lambda: sa.Column("pallet_count", sa.Integer(), nullable=True)),
    ("accessorials", lambda: sa.Column("accessorials", sa.JSON(), nullable=True)),
    ("pro_number", lambda: sa.Column("pro_number", sa.String(length=40), nullable=True)),
    ("bol_number", lambda: sa.Column("bol_number", sa.String(length=60), nullable=True)),
    ("idempotency_key", lambda: sa.Column("idempotency_key", sa.String(length=80), nullable=True)),
    # SoftDeleteMixin (newly added to Shipment). is_deleted is NOT NULL with a
    # server_default so existing rows backfill to false without a rewrite.
    (
        "is_deleted",
        lambda: sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
    ),
    ("deleted_at", lambda: sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)),
    ("deleted_by", lambda: sa.Column("deleted_by", sa.Integer(), nullable=True)),
]

# New Shipment FKs (constraint name, local col, target). Named so downgrade can
# drop them explicitly on Postgres.
SHIPMENT_NEW_FKS = [
    ("fk_shipments_carrier_account_id", "carrier_account_id", "carrier_accounts", "id"),
    ("fk_shipments_label_document_id", "label_document_id", "documents", "id"),
    ("fk_shipments_bol_document_id", "bol_document_id", "documents", "id"),
]

# New non-partial indexes on shipments (name, columns).
SHIPMENT_NEW_INDEXES = [
    ("ix_shipments_aggregator_shipment_id", ["aggregator_shipment_id"]),
    ("ix_shipments_is_deleted", ["is_deleted"]),
]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _inspector(conn=None):
    return sa.inspect(conn or op.get_bind())


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


# ---------------------------------------------------------------------------
# create_table builders (each guarded by _has_table in upgrade)
# ---------------------------------------------------------------------------
def _create_carrier_accounts() -> None:
    op.create_table(
        CARRIER_ACCOUNTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=True),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("carrier_refs", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        # SoftDeleteMixin
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Integer(), nullable=True),
        # OptimisticLockMixin
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "name", name="uq_carrier_account_company_name"),
    )


def _create_shipping_profiles() -> None:
    op.create_table(
        SHIPPING_PROFILES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("ship_from_name", sa.String(length=255), nullable=True),
        sa.Column("ship_from_company", sa.String(length=255), nullable=True),
        sa.Column("ship_from_phone", sa.String(length=50), nullable=True),
        sa.Column("ship_from_email", sa.String(length=255), nullable=True),
        sa.Column("ship_from_street1", sa.String(length=255), nullable=True),
        sa.Column("ship_from_street2", sa.String(length=255), nullable=True),
        sa.Column("ship_from_city", sa.String(length=100), nullable=True),
        sa.Column("ship_from_state", sa.String(length=50), nullable=True),
        sa.Column("ship_from_zip", sa.String(length=20), nullable=True),
        sa.Column("ship_from_country", sa.String(length=2), nullable=True),
        sa.Column("default_package_weight_lbs", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("default_package_length_in", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("default_package_width_in", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("default_package_height_in", sa.Numeric(precision=10, scale=2), nullable=True),
        # Per-company customer-data egress kill switch -- NOT NULL, defaults OFF.
        sa.Column("allow_carrier_egress", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        # OptimisticLockMixin
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", name="uq_company_shipping_profile_company"),
    )


def _create_shipment_packages() -> None:
    op.create_table(
        SHIPMENT_PACKAGES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("shipment_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("package_type", sa.String(length=20), nullable=True),
        sa.Column("length_in", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("width_in", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("height_in", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("weight_lbs", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("tracking_number", sa.String(length=100), nullable=True),
        sa.Column("freight_class", sa.String(length=10), nullable=True),
        sa.Column("nmfc_code", sa.String(length=20), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        # SoftDeleteMixin
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_rate_quotes() -> None:
    op.create_table(
        RATE_QUOTES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("shipment_id", sa.Integer(), nullable=False),
        sa.Column("provider_rate_id", sa.String(length=120), nullable=True),
        sa.Column("carrier", sa.String(length=100), nullable=True),
        sa.Column("service_code", sa.String(length=80), nullable=True),
        sa.Column("service_name", sa.String(length=120), nullable=True),
        sa.Column("mode", sa.String(length=20), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("est_delivery_days", sa.Integer(), nullable=True),
        sa.Column("est_delivery_date", sa.Date(), nullable=True),
        sa.Column("is_selected", sa.Boolean(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_tracking_events() -> None:
    op.create_table(
        TRACKING_EVENTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("shipment_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column("status_detail", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("provider_event_id", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


# (table, [index_name, columns]). Indexes mirror the model index=True / TenantMixin
# / SoftDeleteMixin declarations so create_all and upgrade converge.
NEW_TABLE_INDEXES = {
    CARRIER_ACCOUNTS: [
        ("ix_carrier_accounts_id", ["id"]),
        ("ix_carrier_accounts_company_id", ["company_id"]),
        ("ix_carrier_accounts_is_deleted", ["is_deleted"]),
    ],
    SHIPPING_PROFILES: [
        ("ix_company_shipping_profiles_id", ["id"]),
        ("ix_company_shipping_profiles_company_id", ["company_id"]),
    ],
    SHIPMENT_PACKAGES: [
        ("ix_shipment_packages_id", ["id"]),
        ("ix_shipment_packages_company_id", ["company_id"]),
        ("ix_shipment_packages_shipment_id", ["shipment_id"]),
        ("ix_shipment_packages_is_deleted", ["is_deleted"]),
    ],
    RATE_QUOTES: [
        ("ix_shipment_rate_quotes_id", ["id"]),
        ("ix_shipment_rate_quotes_company_id", ["company_id"]),
        ("ix_shipment_rate_quotes_shipment_id", ["shipment_id"]),
    ],
    TRACKING_EVENTS: [
        ("ix_shipment_tracking_events_id", ["id"]),
        ("ix_shipment_tracking_events_company_id", ["company_id"]),
        ("ix_shipment_tracking_events_shipment_id", ["shipment_id"]),
    ],
}

# Ordered so FK targets precede dependents (carrier_accounts before shipments ALTER;
# child tables after shipments ALTER).
NEW_TABLE_BUILDERS_PRE_SHIPMENTS = [
    (CARRIER_ACCOUNTS, _create_carrier_accounts),
    (SHIPPING_PROFILES, _create_shipping_profiles),
]
NEW_TABLE_BUILDERS_POST_SHIPMENTS = [
    (SHIPMENT_PACKAGES, _create_shipment_packages),
    (RATE_QUOTES, _create_rate_quotes),
    (TRACKING_EVENTS, _create_tracking_events),
]


def _create_table_with_indexes(table_name: str, builder) -> None:
    if not _has_table(table_name):
        builder()
    for index_name, columns in NEW_TABLE_INDEXES.get(table_name, []):
        if not _has_index(table_name, index_name):
            op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create carrier_accounts (FK target for shipments) + company_shipping_profiles.
    for table_name, builder in NEW_TABLE_BUILDERS_PRE_SHIPMENTS:
        _create_table_with_indexes(table_name, builder)

    # 2. ALTER shipments: add new NULLABLE columns + the SoftDeleteMixin columns.
    for col_name, col_factory in SHIPMENT_NEW_COLUMNS:
        if not _has_column(SHIPMENTS, col_name):
            op.add_column(SHIPMENTS, col_factory())

    # 2a. New FKs from shipments. SQLite cannot ADD CONSTRAINT after the fact
    # (no ALTER TABLE ADD FOREIGN KEY); the create_all bootstrap path already wires
    # these from the model, so this is Postgres-only -- precedent: 045's _is_postgres
    # guard. The columns themselves exist on all dialects.
    if _is_postgres(conn):
        for fk_name, local_col, target_table, target_col in SHIPMENT_NEW_FKS:
            if not _has_fk(SHIPMENTS, fk_name):
                op.create_foreign_key(fk_name, SHIPMENTS, target_table, [local_col], [target_col])

    # 2b. Plain indexes on shipments.
    for index_name, columns in SHIPMENT_NEW_INDEXES:
        if not _has_index(SHIPMENTS, index_name):
            op.create_index(index_name, SHIPMENTS, columns)

    # 2c. Partial unique idempotency index (hand-written WHERE clause; applied on
    # both Postgres and SQLite -- both support partial indexes).
    if not _has_index(SHIPMENTS, SHIPMENT_IDEMPOTENCY_INDEX):
        op.create_index(
            SHIPMENT_IDEMPOTENCY_INDEX,
            SHIPMENTS,
            ["company_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text(SHIPMENT_IDEMPOTENCY_WHERE),
            sqlite_where=sa.text(SHIPMENT_IDEMPOTENCY_WHERE),
        )

    # 3. Child tables (FK -> shipments.id), created after shipments exists.
    for table_name, builder in NEW_TABLE_BUILDERS_POST_SHIPMENTS:
        _create_table_with_indexes(table_name, builder)


def _drop_table_with_indexes(table_name: str) -> None:
    if not _has_table(table_name):
        return
    for index_name, _columns in reversed(NEW_TABLE_INDEXES.get(table_name, [])):
        if _has_index(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
    op.drop_table(table_name)


def downgrade() -> None:
    conn = op.get_bind()

    # Reverse order. 3. Drop child tables (FK -> shipments) first.
    for table_name, _builder in reversed(NEW_TABLE_BUILDERS_POST_SHIPMENTS):
        _drop_table_with_indexes(table_name)

    # 2c/2b. Drop shipments indexes (partial idempotency index + plain indexes).
    if _has_index(SHIPMENTS, SHIPMENT_IDEMPOTENCY_INDEX):
        op.drop_index(SHIPMENT_IDEMPOTENCY_INDEX, table_name=SHIPMENTS)
    for index_name, _columns in reversed(SHIPMENT_NEW_INDEXES):
        if _has_index(SHIPMENTS, index_name):
            op.drop_index(index_name, table_name=SHIPMENTS)

    # 2a. Drop the named FKs (Postgres only -- SQLite never created them).
    if _is_postgres(conn):
        for fk_name, _local_col, _target_table, _target_col in SHIPMENT_NEW_FKS:
            if _has_fk(SHIPMENTS, fk_name):
                op.drop_constraint(fk_name, SHIPMENTS, type_="foreignkey")

    # 2. Drop the added shipments columns in reverse order.
    for col_name, _col_factory in reversed(SHIPMENT_NEW_COLUMNS):
        if _has_column(SHIPMENTS, col_name):
            op.drop_column(SHIPMENTS, col_name)

    # 1. Drop company_shipping_profiles then carrier_accounts (carrier_accounts last
    # because shipments.carrier_account_id FK targeted it -- already dropped above).
    for table_name, _builder in reversed(NEW_TABLE_BUILDERS_PRE_SHIPMENTS):
        _drop_table_with_indexes(table_name)
