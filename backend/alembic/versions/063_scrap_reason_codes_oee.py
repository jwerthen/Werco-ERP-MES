"""Lean Phase 1 schema: scrap reason codes + scrap FKs, operation rework qty, OEE source + uniqueness

Revision ID: 063_scrap_reason_codes_oee
Revises: 062_estimate_job_actuals
Create Date: 2026-07-09

Context (Lean Roadmap Phase 1, issue #88)
-----------------------------------------
1. NEW TABLE ``scrap_reason_codes`` -- tenant-scoped reason-code vocabulary for scrap,
   modeled on ``downtime_reason_codes`` (app/models/scrap_reason.py). Deliberate
   difference from its template: ``code`` is unique PER TENANT via
   ``uq_scrap_reason_codes_company_code (company_id, code)``, NOT globally
   ``unique=True`` (DowntimeReasonCode's global unique is a known cross-tenant
   defect; not copied). ``category`` is a plain VARCHAR(50) (vocabulary in the
   ``ScrapCategory`` str-enum), not a native SQLEnum, so new categories never
   need an ALTER TYPE (precedent: 013/018/019/021 all converted enums to varchar).
2. NULLABLE FK ``scrap_reason_code_id -> scrap_reason_codes.id`` on ``time_entries``,
   ``work_order_operations``, and ``work_orders``. The existing free-text
   ``scrap_reason`` columns (055) are untouched and become narrative detail.
   NULL means "no code recorded" (all historical rows, scrap=0 writes) -- never
   backfilled or guessed.
3. ``work_order_operations.quantity_reworked`` -- Float, nullable, DEFAULT 0
   (matches adjacent quantity_complete/quantity_scrapped conventions;
   server_default so every pre-existing row reads 0, not NULL).
4. ``oee_records.calculation_source`` -- VARCHAR(20) NOT NULL DEFAULT 'manual'.
   Every existing row backfills to 'manual' via the server default (all history
   was hand-entered); the Phase 1 auto-calculator will mint its own token.
5. OEE uniqueness: at most one ``oee_records`` row per (company_id,
   work_center_id, record_date, shift). ``shift`` is NULLABLE and Postgres
   treats NULLs as distinct in plain unique constraints, so the rule is a UNIQUE
   EXPRESSION INDEX ``uq_oee_company_wc_date_shift`` on
   ``(company_id, work_center_id, record_date, COALESCE(shift, ''))`` -- a NULL
   shift and an empty-string shift are deliberately the same "no shift" key.

Pre-flight dedupe of oee_records (documented per brief)
-------------------------------------------------------
Existing data may already hold duplicate rows for the same
(company_id, work_center_id, record_date, COALESCE(shift, '')) key, which would
make the unique index build FAIL. Before creating the index we DELETE the losers,
KEEPING the most recently updated row per key (greatest ``updated_at``, NULLs
last; ties broken by greatest ``created_at`` then greatest ``id``). OEERecord is
a DERIVED daily metric snapshot (recomputable, not audit data and not a
SoftDeleteMixin table), so a physical delete of stale duplicates is acceptable
here and is the correct resolution. For traceability the migration SELECTs the
doomed rows first and prints their count + identifying fields (id, company_id,
work_center_id, record_date, shift, updated_at) to the migration/deploy log --
the 039_uq_open_time_entry convention. Nothing is written to ``audit_log`` (a
tamper-evident hash chain that must never be backfilled out of band). The dedupe
and the index build run in the SAME transaction so no new duplicate can slip in
between; deleted duplicates are NOT restored by downgrade (one-way data fix).

Idempotent and reversible
-------------------------
- Every create/add is guarded (``_has_table`` / ``_has_column`` / ``_has_index``
  / FK-by-constrained-column), so re-runs and the create_all -> stamp -> upgrade
  bootstrap path (docs/DEVELOPMENT.md -- objects already built from the models)
  are clean no-ops. The FK guard checks the CONSTRAINED COLUMN, not just the
  name, so a create_all-bootstrapped Postgres DB (FK exists under the
  auto-generated ``<table>_<col>_fkey`` name) is not given a duplicate constraint.
- SQLite (local dev / pytest create_all path): the table create and column adds
  run fine; the named FKs are Postgres-only (SQLite cannot ADD CONSTRAINT after
  the fact -- precedent 046/051; create_all wires the model's inline FK) and the
  OEE dedupe + unique index are Postgres-only (precedent 039/045: SQLite gets
  ``uq_oee_company_wc_date_shift`` from the model's ``__table_args__`` at
  create_all; the index only touches columns this migration never drops, so the
  SQLite downgrade leaving it in place is harmless).
- Downgrade drops, in dependency order: the OEE unique index (Postgres), the two
  added columns, the three FK constraints (any FK on the column, by reflected
  name -- covers both the named and auto-named variants) and their columns, then
  the ``scrap_reason_codes`` indexes and table. RLS disappears with the table.

RLS (docs/SUPABASE_SECURITY.md new-table convention)
----------------------------------------------------
``scrap_reason_codes`` gets ``ENABLE ROW LEVEL SECURITY`` (Postgres-only, like
059/061/062) -- deny-by-default posture with zero policies; app-layer tenancy
(TenantMixin ``company_id`` NOT NULL + index) stays the enforcement.

Locking / operations notes
--------------------------
- ADD COLUMN (nullable, and NOT NULL with constant default on PG11+) is
  metadata-only: brief ACCESS EXCLUSIVE, no table rewrite -- including the
  defaulted ``quantity_reworked`` / ``calculation_source`` backfills.
- ADD CONSTRAINT ... FOREIGN KEY takes SHARE ROW EXCLUSIVE on both tables and
  scans the referencing table to validate; the column is brand-new and all-NULL
  so validation is trivial, but ``time_entries``/``work_order_operations`` are
  the busiest tables -- expect a short lock, deploy off-shift if possible.
- The OEE unique index is built WITHOUT ``CONCURRENTLY`` (unlike 039):
  ``oee_records`` is a small, low-write derived table, and a transactional build
  keeps the dedupe + index atomic. Lock window is negligible.
- Deploy ordering: run this migration BEFORE app code that writes the new
  columns. Old app code ignores them (nullable/defaulted). Operational caveat:
  once the unique index exists, a second manual OEE record for the same
  (company, work center, date, shift) key is rejected by the DB (IntegrityError
  -> HTTP 500 until the endpoint surfaces it as a validation error) -- the
  Phase 1 API layer should catch and return 409/422.

Revision id ``063_scrap_reason_codes_oee`` is 26 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "063_scrap_reason_codes_oee"
down_revision = "062_estimate_job_actuals"
branch_labels = None
depends_on = None

SCRAP_CODES = "scrap_reason_codes"
OEE = "oee_records"
FK_COLUMN = "scrap_reason_code_id"

# (table, named FK constraint) for the three scrap_reason_code_id adds. Named so
# downgrade can drop them explicitly on Postgres (pattern: 046/051).
FK_TARGETS = (
    ("time_entries", "fk_time_entries_scrap_reason_code_id"),
    ("work_order_operations", "fk_wo_operations_scrap_reason_code_id"),
    ("work_orders", "fk_work_orders_scrap_reason_code_id"),
)

# Mirrors the model's id/code index=True + TenantMixin company_id index so
# create_all and upgrade converge (pattern: 051/062).
SCRAP_CODE_INDEXES = (
    ("ix_scrap_reason_codes_id", ["id"], False),
    ("ix_scrap_reason_codes_code", ["code"], False),
    ("ix_scrap_reason_codes_company_id", ["company_id"], False),
)

OEE_UNIQUE_INDEX = "uq_oee_company_wc_date_shift"

# Rank duplicates within each logical OEE key; rn=1 is the keeper (most recently
# updated, NULLs last, then newest created_at, then highest id).
_OEE_RANKED_DUPES = """
    SELECT id, company_id, work_center_id, record_date, shift, updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY company_id, work_center_id, record_date, COALESCE(shift, '')
               ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
           ) AS rn
    FROM oee_records
"""


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


def _has_fk_on_column(table_name: str, column_name: str) -> bool:
    """True if ANY foreign key constrains exactly this column.

    Checked by constrained column rather than name so the create_all-bootstrapped
    path (FK auto-named ``<table>_<col>_fkey`` by the model) idempotently no-ops.
    """
    if not _has_table(table_name):
        return False
    return any(fk.get("constrained_columns") == [column_name] for fk in _inspector().get_foreign_keys(table_name))


def _fk_names_on_column(table_name: str, column_name: str) -> list:
    if not _has_table(table_name):
        return []
    return [
        fk["name"]
        for fk in _inspector().get_foreign_keys(table_name)
        if fk.get("constrained_columns") == [column_name] and fk.get("name")
    ]


def upgrade() -> None:
    conn = op.get_bind()

    # ---- 1. scrap_reason_codes table (tenant-scoped reason vocabulary) -----
    if not _has_table(SCRAP_CODES):
        op.create_table(
            SCRAP_CODES,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=50), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            # Plain varchar; vocabulary lives in app.models.scrap_reason.ScrapCategory.
            sa.Column("category", sa.String(length=50), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("display_order", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            # TenantMixin shape: non-null company scope (index created below).
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
            # Tenant-scoped uniqueness -- deliberately NOT a global unique on code.
            sa.UniqueConstraint("company_id", "code", name="uq_scrap_reason_codes_company_code"),
        )
    for index_name, columns, unique in SCRAP_CODE_INDEXES:
        if not _has_index(SCRAP_CODES, index_name):
            op.create_index(index_name, SCRAP_CODES, columns, unique=unique)

    # Deny-by-default RLS posture (docs/SUPABASE_SECURITY.md new-table convention):
    # Postgres-only, like 059/061/062; app-layer tenancy stays the enforcement.
    # Idempotent catalog flag flip.
    if _is_postgres(conn):
        op.execute('ALTER TABLE public."scrap_reason_codes" ENABLE ROW LEVEL SECURITY')

    # ---- 2. scrap_reason_code_id FK columns (nullable, no backfill) --------
    for table_name, fk_name in FK_TARGETS:
        if not _has_column(table_name, FK_COLUMN):
            op.add_column(table_name, sa.Column(FK_COLUMN, sa.Integer(), nullable=True))
        # Named FK, Postgres-only: SQLite cannot ADD CONSTRAINT after the fact and
        # its create_all path already wires the model's inline FK (precedent 046/051).
        if _is_postgres(conn) and not _has_fk_on_column(table_name, FK_COLUMN):
            op.create_foreign_key(fk_name, table_name, SCRAP_CODES, [FK_COLUMN], ["id"])

    # ---- 3. work_order_operations.quantity_reworked ------------------------
    # server_default '0' so every pre-existing row reads 0 (PG11+ fast default:
    # metadata-only, no rewrite). Nullable to match adjacent quantity_* columns.
    if not _has_column("work_order_operations", "quantity_reworked"):
        op.add_column(
            "work_order_operations",
            sa.Column("quantity_reworked", sa.Float(), nullable=True, server_default="0"),
        )

    # ---- 4. oee_records.calculation_source ---------------------------------
    # NOT NULL with server_default 'manual': all historical rows were hand-entered.
    if not _has_column(OEE, "calculation_source"):
        op.add_column(
            OEE,
            sa.Column("calculation_source", sa.String(length=20), nullable=False, server_default="manual"),
        )

    # ---- 5. OEE dedupe + unique expression index (Postgres-only) -----------
    # SQLite builds this index from the model __table_args__ at create_all
    # (precedent 039/045: dialect-guarded index work). Skipped entirely when the
    # index already exists -- duplicates then cannot exist either.
    if _is_postgres(conn) and not _has_index(OEE, OEE_UNIQUE_INDEX):
        doomed = conn.execute(sa.text(f"""
                SELECT id, company_id, work_center_id, record_date, shift, updated_at
                FROM ({_OEE_RANKED_DUPES}) ranked
                WHERE rn > 1
                ORDER BY company_id, work_center_id, record_date, id
                """)).fetchall()

        if doomed:
            print(
                f"[063_scrap_reason_codes_oee] Deleting {len(doomed)} duplicate oee_records "
                f"rows (derived daily metrics; keeper = most recently updated per "
                f"(company_id, work_center_id, record_date, COALESCE(shift, '')) key). "
                f"Deleted-row details for traceability:"
            )
            for row in doomed:
                print(
                    f"[063_scrap_reason_codes_oee]   deleted oee_record id={row.id} "
                    f"company_id={row.company_id} work_center_id={row.work_center_id} "
                    f"record_date={row.record_date!s} shift={row.shift!r} "
                    f"updated_at={row.updated_at!s}"
                )
        else:
            print("[063_scrap_reason_codes_oee] No duplicate oee_records found; no rows deleted.")

        op.execute(sa.text(f"""
                DELETE FROM oee_records
                WHERE id IN (SELECT id FROM ({_OEE_RANKED_DUPES}) ranked WHERE rn > 1)
                """))

        # Same transaction as the dedupe, so no duplicate can slip in between.
        # Small, low-write derived table: a plain (non-CONCURRENT) build is fine.
        op.create_index(
            OEE_UNIQUE_INDEX,
            OEE,
            ["company_id", "work_center_id", "record_date", sa.text("COALESCE(shift, '')")],
            unique=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    conn = op.get_bind()

    # ---- 5. OEE unique index (Postgres; SQLite keeps its create_all copy --
    # harmless, it only touches columns that survive the downgrade). Deleted
    # duplicate rows are NOT restored (one-way dedupe of derived metrics).
    if _is_postgres(conn) and _has_index(OEE, OEE_UNIQUE_INDEX):
        op.drop_index(OEE_UNIQUE_INDEX, table_name=OEE, if_exists=True)

    # ---- 4./3. added columns -----------------------------------------------
    if _has_column(OEE, "calculation_source"):
        op.drop_column(OEE, "calculation_source")
    if _has_column("work_order_operations", "quantity_reworked"):
        op.drop_column("work_order_operations", "quantity_reworked")

    # ---- 2. FK constraints + columns (before the referenced table drop) ----
    for table_name, _fk_name in FK_TARGETS:
        if _is_postgres(conn):
            # Drop by reflected name so both the named (migration path) and the
            # auto-named (create_all path) constraint variants are covered.
            for actual_fk_name in _fk_names_on_column(table_name, FK_COLUMN):
                op.drop_constraint(actual_fk_name, table_name, type_="foreignkey")
            if _has_column(table_name, FK_COLUMN):
                op.drop_column(table_name, FK_COLUMN)
        elif _has_column(table_name, FK_COLUMN):
            # SQLite cannot DROP a column named in an inline FK clause (the
            # create_all-built table carries REFERENCES scrap_reason_codes(id)),
            # so batch mode recreates the table without the column + FK.
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_column(FK_COLUMN)

    # ---- 1. scrap_reason_codes indexes + table -----------------------------
    if _has_table(SCRAP_CODES):
        for index_name, _columns, _unique in reversed(SCRAP_CODE_INDEXES):
            if _has_index(SCRAP_CODES, index_name):
                op.drop_index(index_name, table_name=SCRAP_CODES)
        op.drop_table(SCRAP_CODES)
