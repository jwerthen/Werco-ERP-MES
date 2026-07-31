"""SMS provider delivery provenance: notification_logs.provider_message_id / provider_status

Revision ID: 073_sms_provider_delivery
Revises: 072_notifications_foundation
Create Date: 2026-07-24

Context
-------
PR 4 of the notification system (docs/NOTIFICATIONS_PLAN.md) adds the SMS channel
(Twilio, behind the ``Company.allow_sms_egress`` CUI kill switch shipped in 072).
The backend work added two columns to ``app/models/notification.py::NotificationLog``
so a Werco delivery-log row can be tied back to the carrier's own record for an
audit; this migration makes an established Postgres schema match them. Without it a
real Postgres raises ``UndefinedColumn`` on the very first SMS send, because
``app/jobs/sms_jobs.py`` and ``app/api/endpoints/users.py`` write both columns on
every attempt.

Head note (2026-07-24)
----------------------
``alembic heads`` reports exactly ONE head, ``072_notifications_foundation``, and
``alembic history`` shows the linear chain ``070_operation_last_report ->
071_display_token_show_customer -> 071_soft_delete_purchasing_ncr ->
072_notifications_foundation`` (two files share the ``071_`` numeric prefix, but the
graph is single-headed -- the same trap 072 documented). 072 is also merged to
``main`` (PR 1, ``efad784``), so this revision chains onto a revision that exists on
the mainline, not onto an unmerged branch tip.

What this migration does
------------------------
``notification_logs`` (an EXISTING tenant-scoped table) gains two nullable,
additive columns, lock-step with the model:

1. ``provider_message_id`` -- ``String(64)``, nullable. The Twilio message SID
   (34 chars today; 64 leaves headroom and matches the model's declared width).
2. ``provider_status`` -- ``String(40)``, nullable. The provider-reported status
   string ("queued" / "accepted" / ...), or the stringified Twilio error status on
   a failed attempt.

Both are deliberately channel-agnostic rather than SMS-only: the email channel
leaves them NULL today, and a future ESP that returns message ids can reuse them.

NO indexes are created. The model declares none (no ``index=True``, no
``__table_args__`` on ``NotificationLog``), and nothing in the PR-4 code queries
either column -- both are write-only provenance (verified: every reference in
``app/`` is an assignment or a response-schema field, never a filter predicate).
There is no Twilio status-callback webhook looking a row up by SID. If one is ever
added, index ``provider_message_id`` in BOTH the model and a new revision.

NO backfill is needed -- and that is a deliberate contrast with 072
------------------------------------------------------------------
Both columns are nullable and purely additive. Existing ``notification_logs`` rows
are pre-SMS email/in-app delivery attempts that genuinely have no provider message
id or provider status, so NULL is the correct and truthful value for every one of
them; inventing a value would fabricate delivery provenance in a compliance record.
This is the opposite of 072's ``operational_events.notified_at``, where leaving the
column NULL on historical rows would have made the 5-minute relay sweeper
re-dispatch the entire event history as a go-live in-app + email storm -- there the
backfill was load-bearing. Here nothing reads these columns, so there is nothing to
storm and nothing to stamp.

No new table, so no RLS statement
---------------------------------
``notification_logs`` is a pre-existing table, not a new one, so the
"``ENABLE ROW LEVEL SECURITY`` on every new table" convention
(docs/SUPABASE_SECURITY.md) does not apply here. It is already covered: 059 swept
every public table lacking RLS and enabled it (``_public_tables_without_rls``), and
that sweep predates this revision. Confirmed -- ALTERing an existing table does not
change its RLS state, so the Security Advisor's ``rls_disabled_in_public`` check
cannot re-flag it. (072 needed an explicit statement only because it CREATEd
``notifications``.)

The tamper-evident ``audit_log`` table is NOT touched and NOT backfilled.

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), not a bare
``upgrade head`` on an empty DB. Both ``add_column`` calls are guarded by the
``_has_table`` / ``_has_column`` inspector idiom (precedent 058/061/071/072), so a
create_all-bootstrapped DB -- where the model already built both columns -- and any
re-run are clean no-ops. ``_has_index`` is deliberately not defined here: this
revision creates no index, so the guard would be dead code.

``downgrade`` really drops both columns (guarded, reverse of the add order); it is
not a ``pass`` stub. Ordering is FK-safe by construction: neither column
participates in a foreign key, and dropping them cannot affect
``notification_logs.notification_id`` (the 072 FK -> ``notifications.id``), which
stays untouched.

Dialect note for the drops (SQLite): a PLAIN ``DROP COLUMN`` is used, not a batch
table-recreate. SQLite 3.35+ refuses to plain-drop only columns that are a PK, are
UNIQUE, are indexed, or participate in an FK/CHECK/trigger/view -- these two are
none of those. Avoiding ``batch_alter_table`` here is the safer choice anyway: a
table recreate would have to faithfully reconstruct the ``notification_id`` FK that
072 added. Round-tripped on SQLite 3.50 in-env.

Locking / operations note
-------------------------
Two nullable ``ADD COLUMN``s with NO default -- metadata-only on PostgreSQL 11+
(catalog-only change, brief ACCESS EXCLUSIVE lock to take the DDL, NO table rewrite
and no row scan), so this is effectively instant regardless of how large
``notification_logs`` has grown. No index build, so no SHARE lock is held for a
scan, and there is no ``CONCURRENTLY`` consideration. No data statement at all.

Deploy ordering: run this migration BEFORE the app deploy that ships the SMS
channel and before the notification worker restarts -- the PR-4 SMS job writes both
columns on the first send, so an app-first deploy would 500 (``UndefinedColumn``) on
every SMS attempt. Old code neither writes nor selects these columns, so running the
migration early is harmless; there is no window where the reverse ordering is safe.

create_all parity
-----------------
This revision and ``Base.metadata.create_all()`` converge: same two column names,
same types (``String(64)`` / ``String(40)``), same nullability (both nullable), no
server defaults on either side, and no index on either side. The only difference is
ordinal position -- create_all emits them where they are declared (before
``related_type``), while ``ADD COLUMN`` appends them at the end of an
already-migrated table. That is cosmetic, not a schema difference; autogenerate
compares by name, not position (identical to how 072's added columns land).

Revision id ``073_sms_provider_delivery`` is 25 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (``alembic_version.version_num``
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "073_sms_provider_delivery"
down_revision = "072_notifications_foundation"
branch_labels = None
depends_on = None

LOGS_TABLE = "notification_logs"

# (column name, type) -- lock-step with app/models/notification.py::NotificationLog.
# Both nullable, no server default, no index. Added in this order; dropped in reverse.
PROVIDER_COLUMNS = [
    ("provider_message_id", sa.String(length=64)),
    ("provider_status", sa.String(length=40)),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def upgrade() -> None:
    # Guarded so the create_all bootstrap path (model already built both columns)
    # and any re-run are clean no-ops. Nullable + no default => metadata-only on
    # PG 11+; no backfill (NULL is the truthful value for pre-SMS rows).
    if not _has_table(LOGS_TABLE):
        return
    for column_name, column_type in PROVIDER_COLUMNS:
        if not _has_column(LOGS_TABLE, column_name):
            op.add_column(LOGS_TABLE, sa.Column(column_name, column_type, nullable=True))


def downgrade() -> None:
    # Reverse of the add order, guarded. FK-safe by construction: neither column
    # participates in a foreign key, so nothing depends on drop ordering relative to
    # notification_logs.notification_id (the 072 FK), which is left untouched.
    # Plain DROP COLUMN on every dialect -- valid on SQLite 3.35+ because neither
    # column is a PK / UNIQUE / indexed / FK / CHECK participant.
    if not _has_table(LOGS_TABLE):
        return
    for column_name, _column_type in reversed(PROVIDER_COLUMNS):
        if _has_column(LOGS_TABLE, column_name):
            op.drop_column(LOGS_TABLE, column_name)
