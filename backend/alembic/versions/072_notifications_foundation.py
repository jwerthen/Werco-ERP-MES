"""Notification system foundation: notifications table + outbox marker + SMS/phone columns

Revision ID: 072_notifications_foundation
Revises: 071_soft_delete_purchasing_ncr
Create Date: 2026-07-24

Context
-------
PR 1 of the notification system (docs/NOTIFICATIONS_PLAN.md §5). The models were
added to code by the backend work (app/models/notification.py::Notification,
NotificationLog.notification_id; app/models/operational_event.py::notified_at;
app/models/user.py::phone; app/models/company.py::allow_sms_egress); this
migration makes an established Postgres schema match them, plus a one-time,
idempotent JSON normalization of existing notification preferences.

Head note (2026-07-24)
----------------------
The linear head on ``main`` is ``071_soft_delete_purchasing_ncr`` -- the
wallboard revision ``071_display_token_show_customer`` chains INTO it (verified
with ``alembic history``; two files share the ``071_`` numeric prefix but the
graph is single-headed). ``down_revision`` is that true head revision id.

What this migration does
------------------------
1. CREATE TABLE ``notifications`` -- the canonical per-user in-app inbox row
   (``Notification``, TenantMixin). Non-null ``company_id`` FK + index, the
   model's columns, and the composite ``ix_notifications_user_unread``. The
   single-column ``index=True`` indexes (id/user_id/event_key/company_id) are
   created explicitly -- ``op.create_table`` builds only the table + PK/FK, not
   the model's ``Index`` objects.
2. ``notification_logs`` += nullable ``notification_id`` Integer FK ->
   ``notifications.id`` (+ index) -- back-links an email/SMS delivery-attempt row
   to the in-app inbox row it delivered.
3. ``operational_events`` += nullable ``notified_at`` DateTime(tz) -- the
   transactional-outbox idempotency marker (§3.1), plus a plain index on it so
   the 5-min relay sweeper's ``notified_at IS NULL`` scan is index-backed. The
   index is also declared on the model (``__table_args__``) so create_all and
   this migration converge. **The column is immediately backfilled**
   (``notified_at = created_at WHERE notified_at IS NULL``) so every event that
   predates the notification system is marked already-dispatched -- see the
   go-live-storm note below.
4. ``users`` += nullable ``phone`` String(32) -- realizes the previously-phantom
   phone field (E.164 for SMS, PR 4).
5. ``companies`` += ``allow_sms_egress`` Boolean NOT NULL server_default false --
   the Twilio egress kill switch, byte-for-byte the ``allow_ai_egress`` (054)
   column form. New capability, default OFF for EVERY tenant (no grandfather
   UPDATE -- unlike 054, SMS is brand-new, so the server_default is the correct
   final state for all existing rows).
6. RLS: ``ENABLE ROW LEVEL SECURITY`` on the new ``notifications`` table
   (Postgres-only), per the deny-by-default new-table convention
   (docs/SUPABASE_SECURITY.md). This revision runs AFTER 059/060, so the new
   table must enable RLS itself (precedent 061). The four existing tables it
   ALTERs already have RLS from 059.
7. DATA (no DDL): one-time idempotent normalization of every
   ``notification_preferences.preferences`` JSON -- each per-event channel dict
   widens from ``{email, digest}`` to ``{in_app, email, sms, digest}`` by ADDING
   only missing keys (``in_app`` -> True, ``sms`` -> False); existing
   email/digest values are never overwritten.

The tamper-evident ``audit_log`` table is NOT touched and NOT backfilled.

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), not a
bare ``upgrade head`` on an empty DB. Every DDL op is guarded (``_has_table`` /
``_has_column`` / ``_has_index``) so a create_all-bootstrapped DB and re-runs are
clean no-ops. The JSON normalization is idempotent by construction
(``dict.setdefault`` only adds absent keys). The ``notified_at`` go-live backfill
is scoped inside the add-column guard AND predicated on ``WHERE notified_at IS
NULL``, so it runs once (first column creation) and is a no-op on any re-run --
crucially, it can never re-stamp events emitted live after go-live. The FK column
on ``notification_logs`` takes the batch (table-recreate) path on SQLite --
alembic cannot ALTER-add an FK constraint there (precedent 058) -- and a plain
inline-FK ALTER on Postgres. Downgrade reverses every DDL op in FK-safe order
(``notification_logs.notification_id`` before the ``notifications`` drop) and is
guarded; the ``notified_at`` backfill needs no explicit reversal (the column is
dropped on downgrade, taking the stamped values with it), and the JSON widening
is deliberately a documented NO-OP on downgrade (reversing a widening is lossy,
and the extra keys are harmless -- old code that reads ``{email, digest}``
ignores them).

Dialect notes for the column drops (SQLite): ``users`` and ``companies`` are
referenced by many FKs, so their column drops use a PLAIN ``DROP COLUMN``
(precedent 054; SQLite 3.35+ native, verified 3.50 in-env) rather than a
batch table-recreate, which would break the inbound references.
``operational_events.notified_at`` is not part of any FK and its index is dropped
first, so a plain ``DROP COLUMN`` is valid on SQLite too.
``notification_logs.notification_id`` IS a foreign-key column, which SQLite
refuses to plain-drop, so it takes the batch path (``notification_logs`` has no
inbound FKs, so the recreate is safe).

Locking / operations note
-------------------------
The single CREATE TABLE is a brand-new empty table (instantaneous). Each ADD
COLUMN is nullable-or-constant-default -> metadata-only on PostgreSQL 11+ (brief
ACCESS EXCLUSIVE lock, no table rewrite). ``allow_sms_egress`` NOT NULL with a
CONSTANT server_default is likewise metadata-only.

The one heavyweight statement is the ``operational_events`` go-live backfill: a
single set-based ``UPDATE ... SET notified_at = created_at WHERE notified_at IS
NULL`` over the event stream, run once right after the ADD COLUMN, inside the
migration transaction. It takes row-level write locks on the events it touches
(every existing row, on first run) and generates dead tuples that autovacuum
reclaims afterward. Batching by id range was CONSIDERED and deliberately NOT
done: at Werco's single-plant scale the event table is tens-of-thousands to low
hundreds-of-thousands of rows, where one UPDATE is a sub-second-to-seconds
operation and the added complexity of a chunked loop is unwarranted (same
single-UPDATE reasoning as 054's grandfather backfill). If ``operational_events``
is ever materially larger (millions of rows) or this must run hot, do the backfill
out-of-band in id-range batches BEFORE deploying (``UPDATE ... WHERE notified_at
IS NULL AND id BETWEEN ...``) and let the guarded migration find nothing to do --
the same escape hatch as the CONCURRENTLY index note below.

Each CREATE INDEX (non-CONCURRENT) takes a SHARE lock on its table for the build;
``notifications`` is empty, ``notification_logs`` is small, ``operational_events``
is the largest (the event stream) -- if it is ever materially large, build
``ix_operational_events_notified_at`` CONCURRENTLY out-of-band and let the guarded
``create_index`` no-op. The JSON normalization touches only
``notification_preferences`` (at most one row per user), so it is trivial. Deploy
ordering: run BEFORE the app deploy that reads/writes these columns AND before the
notification worker/sweeper starts, so the sweeper's first pass sees a fully
backfilled table; old code neither writes nor selects these columns.

Revision id ``072_notifications_foundation`` is 28 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import json

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "072_notifications_foundation"
down_revision = "071_soft_delete_purchasing_ncr"
branch_labels = None
depends_on = None

NOTIFICATIONS = "notifications"

# The notifications indexes create_all builds (single-column index=True + the
# composite __table_args__ index). op.create_table does NOT build these -- they
# are separate Index objects -- so they are created explicitly, guarded.
NOTIFICATIONS_INDEXES = [
    ("ix_notifications_id", ["id"], False),
    ("ix_notifications_user_id", ["user_id"], False),
    ("ix_notifications_event_key", ["event_key"], False),
    ("ix_notifications_company_id", ["company_id"], False),
    ("ix_notifications_user_unread", ["user_id", "is_read"], False),
]

# (table, column) additions on existing tables, plus their single-column indexes.
LOGS_TABLE = "notification_logs"
LOGS_COLUMN = "notification_id"
LOGS_INDEX = "ix_notification_logs_notification_id"
LOGS_FK = "fk_notification_logs_notification_id"

EVENTS_TABLE = "operational_events"
EVENTS_COLUMN = "notified_at"
EVENTS_INDEX = "ix_operational_events_notified_at"

USERS_TABLE = "users"
USERS_COLUMN = "phone"

COMPANIES_TABLE = "companies"
COMPANIES_COLUMN = "allow_sms_egress"

PREFS_TABLE = "notification_preferences"


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


def _create_notifications() -> None:
    # Lock-step with app/models/notification.py::Notification. TenantMixin's
    # company_id/company are appended after the class's own columns (MRO order),
    # so company_id sits last -- matching create_all's emission order. Server
    # defaults mirror the model byte-for-byte (is_read DEFAULT false, created_at
    # DEFAULT now()); severity has an app-side default only (no server_default).
    op.create_table(
        NOTIFICATIONS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=80), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=500), nullable=True),
        sa.Column("related_type", sa.String(length=100), nullable=True),
        sa.Column("related_id", sa.Integer(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # TenantMixin -- non-null company scope, stamped from the triggering event.
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def _normalize_notification_preferences() -> None:
    """Widen every stored per-event channel dict to the 4-channel shape.

    ``{email, digest}`` -> ``{in_app, email, sms, digest}`` by ADDING only the
    missing keys (``in_app`` True, ``sms`` False). Existing keys -- including any
    already-widened row -- are never overwritten, so this is safe to re-run.

    Uses a typed SQLAlchemy Core table so the JSON bind/result processors apply
    on BOTH Postgres (JSON/JSONB) and SQLite (serialized TEXT); a defensive
    ``json.loads`` also covers drivers that hand back a raw string.
    """
    if not _has_table(PREFS_TABLE):
        return

    prefs = sa.table(
        PREFS_TABLE,
        sa.column("id", sa.Integer),
        sa.column("preferences", sa.JSON),
    )
    conn = op.get_bind()

    rows = conn.execute(sa.select(prefs.c.id, prefs.c.preferences)).fetchall()
    for row_id, raw in rows:
        value = raw
        if isinstance(value, (str, bytes, bytearray)):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                continue
        if not isinstance(value, dict):
            continue

        changed = False
        for event_channels in value.values():
            # Each event's value is the per-channel dict; skip anything unexpected.
            if not isinstance(event_channels, dict):
                continue
            if "in_app" not in event_channels:
                event_channels["in_app"] = True
                changed = True
            if "sms" not in event_channels:
                event_channels["sms"] = False
                changed = True

        if changed:
            conn.execute(sa.update(prefs).where(prefs.c.id == row_id).values(preferences=value))


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    is_postgres = bind.dialect.name == "postgresql"

    # 1) The new notifications table + its indexes. Guarded so the create_all
    #    bootstrap path (model already built table + indexes) no-ops.
    if not _has_table(NOTIFICATIONS):
        _create_notifications()
    for index_name, columns, unique in NOTIFICATIONS_INDEXES:
        if not _has_index(NOTIFICATIONS, index_name):
            op.create_index(index_name, NOTIFICATIONS, columns, unique=unique)

    # 2) RLS on the new table only (Postgres-only; runs after 059/060). The app
    #    role bypasses RLS; app-layer tenancy stays the enforcement.
    if is_postgres:
        op.execute(f'ALTER TABLE public."{NOTIFICATIONS}" ENABLE ROW LEVEL SECURITY')

    # 3) notification_logs.notification_id (nullable FK -> notifications.id) + index.
    #    SQLite cannot ALTER-add an FK constraint (alembic raises), so it takes the
    #    batch path; Postgres keeps a plain inline-FK ALTER (precedent 058).
    if _has_table(LOGS_TABLE):
        if not _has_column(LOGS_TABLE, LOGS_COLUMN):
            if is_sqlite:
                with op.batch_alter_table(LOGS_TABLE) as batch_op:
                    batch_op.add_column(sa.Column(LOGS_COLUMN, sa.Integer(), nullable=True))
                    batch_op.create_foreign_key(LOGS_FK, NOTIFICATIONS, [LOGS_COLUMN], ["id"])
            else:
                op.add_column(
                    LOGS_TABLE,
                    sa.Column(LOGS_COLUMN, sa.Integer(), sa.ForeignKey("notifications.id"), nullable=True),
                )
        if not _has_index(LOGS_TABLE, LOGS_INDEX):
            op.create_index(LOGS_INDEX, LOGS_TABLE, [LOGS_COLUMN], unique=False)

    # 4) operational_events.notified_at (outbox marker) + sweeper index. Nullable,
    #    added WITHOUT a server default, then IMMEDIATELY backfilled so every
    #    pre-existing event is marked already-dispatched.
    if _has_table(EVENTS_TABLE):
        if not _has_column(EVENTS_TABLE, EVENTS_COLUMN):
            op.add_column(EVENTS_TABLE, sa.Column(EVENTS_COLUMN, sa.DateTime(timezone=True), nullable=True))
            # CRITICAL go-live backfill. Production ALREADY emits the event_types
            # the new notification catalog maps (work_order_completed, ncr_created,
            # work_order_released, purchase_order_received, operation_completed,
            # downtime_started, ...). Without this, on first deploy every historical
            # row has notified_at IS NULL and the 5-min relay sweeper would
            # re-dispatch the ENTIRE event history -> a go-live in-app + EMAIL storm
            # to real users for months-old events. Stamp pre-existing events as
            # already handled ("these predate the notification system") by setting
            # notified_at = created_at. Scoped INSIDE the add-column guard (precedent
            # 054's grandfather UPDATE) so it runs ONLY when the column is first
            # created: a later re-run must NEVER re-stamp events emitted live after
            # go-live -- those legitimately keep notified_at IS NULL for the sweeper.
            # The WHERE notified_at IS NULL predicate additionally makes the
            # statement a no-op over any already-stamped row. Single set-based
            # UPDATE, no batching (see the locking note in the module docstring).
            op.execute(sa.text("UPDATE operational_events SET notified_at = created_at WHERE notified_at IS NULL"))
        if not _has_index(EVENTS_TABLE, EVENTS_INDEX):
            op.create_index(EVENTS_INDEX, EVENTS_TABLE, [EVENTS_COLUMN], unique=False)

    # 5) users.phone (nullable, no index) -- realizes the phantom phone field.
    if _has_table(USERS_TABLE) and not _has_column(USERS_TABLE, USERS_COLUMN):
        op.add_column(USERS_TABLE, sa.Column(USERS_COLUMN, sa.String(length=32), nullable=True))

    # 6) companies.allow_sms_egress -- NOT NULL, constant server_default false
    #    (metadata-only on PG 11+). Default OFF for every tenant; no grandfather
    #    UPDATE (SMS is a brand-new capability, unlike 054's always-on AI).
    if _has_table(COMPANIES_TABLE) and not _has_column(COMPANIES_TABLE, COMPANIES_COLUMN):
        op.add_column(
            COMPANIES_TABLE,
            sa.Column(COMPANIES_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )

    # 7) One-time idempotent JSON normalization (data only, no DDL).
    _normalize_notification_preferences()


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    is_postgres = bind.dialect.name == "postgresql"

    # The JSON widening (upgrade step 7) is deliberately NOT reversed: reversing a
    # widening is lossy, and the extra {in_app, sms} keys are harmless -- pre-072
    # code reads only {email, digest} and ignores them. Documented no-op.

    # Reverse the DDL in FK-safe order. companies/users use a PLAIN DROP COLUMN
    # even on SQLite (both tables are referenced by many FKs, so a batch
    # table-recreate would break inbound references; SQLite 3.35+ native drop,
    # precedent 054).
    if _has_column(COMPANIES_TABLE, COMPANIES_COLUMN):
        op.drop_column(COMPANIES_TABLE, COMPANIES_COLUMN)

    if _has_column(USERS_TABLE, USERS_COLUMN):
        op.drop_column(USERS_TABLE, USERS_COLUMN)

    # operational_events.notified_at: drop its index first (SQLite refuses to drop
    # an indexed column), then a plain DROP COLUMN (not part of any FK; nothing
    # references operational_events).
    if _has_index(EVENTS_TABLE, EVENTS_INDEX):
        op.drop_index(EVENTS_INDEX, table_name=EVENTS_TABLE)
    if _has_column(EVENTS_TABLE, EVENTS_COLUMN):
        op.drop_column(EVENTS_TABLE, EVENTS_COLUMN)

    # notification_logs.notification_id: MUST go before dropping notifications (it
    # references notifications.id). Drop the index, then the FK column -- batch on
    # SQLite (FK column can't be plain-dropped; notification_logs has no inbound
    # FKs so the recreate is safe), plain on Postgres.
    if _has_index(LOGS_TABLE, LOGS_INDEX):
        op.drop_index(LOGS_INDEX, table_name=LOGS_TABLE)
    if _has_column(LOGS_TABLE, LOGS_COLUMN):
        if is_sqlite:
            with op.batch_alter_table(LOGS_TABLE) as batch_op:
                batch_op.drop_column(LOGS_COLUMN)
        else:
            op.drop_column(LOGS_TABLE, LOGS_COLUMN)

    # notifications: disable RLS (Postgres, defensive -- the drop removes it too),
    # drop its indexes, then drop the table.
    if _has_table(NOTIFICATIONS):
        if is_postgres:
            op.execute(f'ALTER TABLE public."{NOTIFICATIONS}" DISABLE ROW LEVEL SECURITY')
        for index_name, _columns, _unique in reversed(NOTIFICATIONS_INDEXES):
            if _has_index(NOTIFICATIONS, index_name):
                op.drop_index(index_name, table_name=NOTIFICATIONS)
        op.drop_table(NOTIFICATIONS)
