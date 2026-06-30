"""Add scrap_reason to work_orders and work_order_operations (AS9100D defect traceability)

Revision ID: 055_wo_scrap_reason
Revises: 054_company_allow_ai
Create Date: 2026-06-30

Context
-------
Data-model foundation for an AS9100D defect-traceability enforcement change. Today
only ``TimeEntry`` carries a ``scrap_reason`` column
(``app/models/time_entry.py``); the ``WorkOrder`` and ``WorkOrderOperation``
aggregates carry ``quantity_scrapped`` but have NO companion reason column, so
several office/admin endpoints can persist a positive scrap quantity with no
recorded reason. This migration adds the reason column to both aggregates. The
models (``app/models/work_order.py``) gained, mirroring ``TimeEntry.scrap_reason``::

    scrap_reason = Column(String(255), nullable=True)   # on WorkOrder
    scrap_reason = Column(String(255), nullable=True)   # on WorkOrderOperation

Shape / safety
--------------
- NULLABLE, no server default, no index, no CHECK constraint. Historical rows
  already carry scrapped quantities with no reason, so the column CANNOT be
  backfilled or made NOT NULL. NULL means "reason not recorded" (legacy rows,
  paper-era scrap), never a guessed value.
- The "reason required when scrap > 0" rule is enforced at the API/validator layer
  (a follow-up agent), deliberately NOT as a DB constraint -- a CHECK tying reason
  to quantity would reject the existing unreasoned-scrap history and block ordinary
  scrap=0 writes that legitimately leave the reason NULL.
- A plain ``VARCHAR(255)`` mirroring ``TimeEntry.scrap_reason`` (not an enum / not a
  constrained type) so the set of allowed reasons can evolve in the application
  layer without an ``ALTER TYPE`` / constraint rewrite.
- Both ``work_orders`` and ``work_order_operations`` are pre-existing tenant tables
  (``TenantMixin``; ``work_orders`` is also ``SoftDeleteMixin``). This migration
  only ADDS a nullable column to each -- no ``company_id``/index work is needed (the
  tables already have it), no soft-delete columns are touched, and no rows are
  hard-deleted. It does NOT touch the tamper-evident ``audit_log`` table and
  backfills no audit rows.

Idempotent and reversible
-------------------------
- Upgrade guards each ADD COLUMN with an inspector ``_has_column`` check (precedent:
  048's ``_has_column`` guard on ``time_entries.source``, and 006/036/040/043/046
  before it). Bootstrap is ``create_all() -> stamp -> upgrade``
  (docs/DEVELOPMENT.md): a DB bootstrapped from the updated model already has both
  columns when this migration runs over the stamp, so the adds are clean no-ops.
  Re-runs are likewise no-ops.
- Downgrade drops both columns, each guarded by the same check. Dialect-agnostic:
  plain ``op.drop_column`` works on Postgres and on the modern SQLite used for local
  dev / pytest (same as 048's downgrade).

Locking / operations note
-------------------------
Adding a NULLABLE column with NO default is a metadata-only change on PostgreSQL:
no table rewrite, no backfill, only a brief ACCESS EXCLUSIVE lock to update the
catalog (same note as 048). ``work_order_operations`` can be a high-row table, but
this change does not scan or rewrite it. No deploy-ordering constraint: old
application code ignores the column; new enforcement code that writes/reads
``scrap_reason`` must not ship before this migration runs, or it would touch a
missing column -- apply this migration, then deploy the validator/endpoint layer.

Revision id ``055_wo_scrap_reason`` is 19 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "055_wo_scrap_reason"
down_revision = "054_company_allow_ai"
branch_labels = None
depends_on = None

COLUMN_NAME = "scrap_reason"
TARGETS = ("work_orders", "work_order_operations")


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    # Plain nullable VARCHAR(255), no default, no index, no constraint -- in
    # lock-step with app/models/work_order.py (WorkOrder.scrap_reason and
    # WorkOrderOperation.scrap_reason), mirroring TimeEntry.scrap_reason. Each add
    # is guarded so a create_all-bootstrapped DB (column already present) and
    # re-runs no-op.
    for table_name in TARGETS:
        if not _has_column(table_name, COLUMN_NAME):
            op.add_column(table_name, sa.Column(COLUMN_NAME, sa.String(length=255), nullable=True))


def downgrade() -> None:
    for table_name in TARGETS:
        if _has_column(table_name, COLUMN_NAME):
            op.drop_column(table_name, COLUMN_NAME)
