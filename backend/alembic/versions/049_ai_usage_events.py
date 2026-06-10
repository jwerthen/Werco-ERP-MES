"""Create ai_usage_events (B0.1 AI ops hardening: per-call LLM usage ledger)

Revision ID: 049_ai_usage_events
Revises: 048_time_entry_source
Create Date: 2026-06-09

Context
-------
B0.1 AI ops hardening adds ``app/models/ai_usage.py::AIUsageEvent`` -- one row
per Anthropic API call (tokens, estimated cost, latency, outcome), written by
``app.services.llm_client.run_llm_task`` and read by the
``/api/v1/ai-usage/summary`` cost/latency dashboard. These are telemetry rows,
NOT audit records: this migration does not touch the tamper-evident
``audit_log`` table, writes no data, and backfills nothing.

Tenant / compliance shape
-------------------------
TenantMixin -> ``company_id`` Integer FK to ``companies.id``, NOT NULL, indexed
(``ix_ai_usage_events_company_id``) -- the same shape as ``laser_nests`` (036),
``certificates_of_conformance`` (044), and the 046 shipping tables. Every query
against this table MUST be company-scoped. No SoftDeleteMixin (telemetry is
append-only and never user-deleted) and no OptimisticLockMixin (rows are never
updated after insert), so none of those columns appear here.

Lock-step with the model (load-bearing)
---------------------------------------
Column list, FK, and the five indexes below are kept byte-for-byte in lock-step
with the model so the ``create_all`` bootstrap path (docs/DEVELOPMENT.md) and a
Postgres ``alembic upgrade`` converge on the IDENTICAL schema. The model
declares ``id`` / ``task`` / ``created_at`` with ``index=True``, ``company_id``
indexed via TenantMixin, and the composite ``ix_ai_usage_company_task_created``
(company_id, task, created_at) in ``__table_args__`` -- the dashboard's
group-by-task-over-time query path. Token counters are NOT NULL with
application-side ``default=0`` only (no server_default -- the writer always
supplies them); ``success`` likewise has no server default. ``created_at`` is
``DateTime(timezone=True)``; verified against autogenerate output, which
emitted exactly this DDL (no enum, no server defaults to hand-correct).

Idempotent and reversible
-------------------------
- Upgrade guards ``create_table`` with ``_has_table`` and every
  ``create_index`` with ``_has_index`` (precedent: 036/037/044/046). This
  matters because bootstrap is ``create_all() -> stamp -> upgrade``
  (docs/DEVELOPMENT.md): a DB bootstrapped from the updated model already has
  the table when this migration runs over the stamp, and the guards make that
  a clean no-op. Re-runs are likewise no-ops.
- Downgrade drops the indexes then the table, all guarded, so it round-trips
  cleanly on Postgres and on the SQLite used for local dev / pytest.

Locking / operations note
-------------------------
Brand-new empty table: ``CREATE TABLE`` + index builds are instantaneous and
take no lock on any existing table. No backfill. No deploy-ordering
constraint: old application code never references the table; new code only
inserts into it after this migration (or a create_all bootstrap) has run.

Revision id ``049_ai_usage_events`` is 19 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "049_ai_usage_events"
down_revision = "048_time_entry_source"
branch_labels = None
depends_on = None

TABLE_NAME = "ai_usage_events"

# (index_name, columns). Mirrors the model's index=True / TenantMixin /
# __table_args__ declarations so create_all and upgrade converge.
INDEXES = [
    ("ix_ai_usage_events_id", ["id"]),
    ("ix_ai_usage_events_company_id", ["company_id"]),
    ("ix_ai_usage_events_task", ["task"]),
    ("ix_ai_usage_events_created_at", ["created_at"]),
    ("ix_ai_usage_company_task_created", ["company_id", "task", "created_at"]),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def upgrade() -> None:
    # Guarded so a create_all-bootstrapped DB (table already present) and
    # re-runs no-op. Kept in lock-step with app/models/ai_usage.py::AIUsageEvent.
    if not _has_table(TABLE_NAME):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), nullable=False),
            # TenantMixin -- non-null company scope, FK to companies.id.
            sa.Column("company_id", sa.Integer(), nullable=False),
            # What ran
            sa.Column("task", sa.String(length=80), nullable=False),
            sa.Column("model", sa.String(length=120), nullable=False),
            sa.Column("tier", sa.String(length=20), nullable=True),
            sa.Column("feature", sa.String(length=120), nullable=True),
            sa.Column("prompt_version", sa.String(length=120), nullable=True),
            # Usage / cost (app-side default=0; writer always supplies values)
            sa.Column("input_tokens", sa.Integer(), nullable=False),
            sa.Column("output_tokens", sa.Integer(), nullable=False),
            sa.Column("cache_creation_tokens", sa.Integer(), nullable=False),
            sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
            sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            # Outcome
            sa.Column("success", sa.Boolean(), nullable=False),
            sa.Column("error_type", sa.String(length=120), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
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
