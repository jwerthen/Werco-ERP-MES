"""Comments with @mentions and record watchers (work orders first)

Revision ID: 125_work_order_comments
Revises: 124_maintenance_assets
Create Date: 2026-11-16

Context
-------
docs/NOTIFICATIONS_PLAN.md §3.6: a discussion thread on a record, @mentions that notify the
person named, and watchers who hear about new comments. The tables are GENERIC -- the target is
a polymorphic ``(entity_type, entity_id)`` with no foreign key -- and v1 enables only
``entity_type = 'work_order'`` in the service layer. There is deliberately NO CHECK on
``entity_type``: enabling the next record type must not need a migration. The service resolves
every target through ``tenant_query()`` against the active company (404 otherwise), because the
database cannot vouch for a polymorphic id.

This revision is purely additive: three new tenant-scoped tables, no change to any existing
table, no enum, no backfill. ``audit_log`` is untouched (comment create / edit / delete are
audited through ``AuditService`` at runtime).

``comments`` (SoftDeleteMixin + TenantMixin)
  id, entity_type String(40) NOT NULL, entity_id Integer NOT NULL, author_id FK users NOT NULL
  (indexed), body Text NOT NULL, edited_at, created_at / updated_at timestamptz NOT NULL,
  is_deleted Boolean NOT NULL DEFAULT 'false' (indexed) / deleted_at / deleted_by, company_id FK
  companies NOT NULL (indexed); ``ix_comments_entity`` (company_id, entity_type, entity_id,
  created_at) for the thread read. Soft-deleted only, never physically removed (invariant 3).

``comment_mentions`` (TenantMixin)
  id, comment_id FK comments NOT NULL (indexed; plain FK, no ON DELETE -- comments are never
  physically deleted), user_id FK users NOT NULL (indexed), created_at, company_id;
  UNIQUE ``uq_comment_mentions_company_comment_user`` (company_id, comment_id, user_id).

``entity_watchers`` (TenantMixin)
  id, entity_type String(40), entity_id, user_id FK users NOT NULL (indexed), created_at,
  company_id; UNIQUE ``uq_entity_watchers_company_entity_user`` (company_id, entity_type,
  entity_id, user_id), which also serves the (company, entity) lookup. A watcher is a
  PREFERENCE: unwatch physically deletes the row, and rows survive the watched record's soft
  delete so a restore keeps them.

Lock-step with ``app/models/comment.py``
----------------------------------------
Every column, index and constraint here is declared on the model with the same name, and this
file mirrors the model rather than the other way round. FKs are UNNAMED on both sides (Postgres
names them ``<table>_<column>_fkey`` either way). Timestamps carry Python-side defaults only, no
``server_default``, and ``is_deleted``'s default is the plain string ``"false"`` the mixin
declares -- never ``sa.text("false")``, which renders ``DEFAULT false`` against create_all's
``DEFAULT 'false'``.

Every object is guarded INDEPENDENTLY (the 088 / 124 style): the fresh bootstrap
(create_all -> stamp 058 -> upgrade) runs this over a schema the models already built, and a
partial bootstrap recovers object by object -- the table, each unique constraint, each foreign
key (matched by its columns and target, since it is unnamed) and each index. RLS / REVOKE sit
OUTSIDE every guard, PostgreSQL-only and idempotent, with the ``anon`` / ``authenticated``
REVOKEs under a ``pg_roles`` existence check (plain Postgres in CI has no Supabase roles); the
model's ``after_create`` listeners carry the same SQL for create_all installations.

Operational note: creating a table with foreign keys briefly takes a SHARE ROW EXCLUSIVE lock
on ``users`` / ``companies``; ``lock_timeout`` bounds that wait at 5s (precedent 059 / 060 /
114 / 117 / 123 / 124). No deploy-ordering constraint beyond landing before the code that reads
these tables.

Downgrade
---------
Real, and LOSSY: drops ``comment_mentions`` and ``entity_watchers``, then ``comments`` (their
indexes, constraints and RLS go with them). Every comment thread and watch list is lost -- export
them first if they matter. The ``audit_log`` rows that recorded each comment write are untouched.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "125_work_order_comments"
down_revision = "124_maintenance_assets"
branch_labels = None
depends_on = None

COMMENTS = "comments"
MENTIONS = "comment_mentions"
WATCHERS = "entity_watchers"

# Creation order: ``comments`` first, because ``comment_mentions.comment_id`` points at it.
TABLES = (COMMENTS, MENTIONS, WATCHERS)
# Drop order: the referencing tables first.
DROP_ORDER = (MENTIONS, WATCHERS, COMMENTS)

MENTIONS_UNIQUE = "uq_comment_mentions_company_comment_user"
WATCHERS_UNIQUE = "uq_entity_watchers_company_entity_user"

# {table: ((name, columns), ...)} -- each mirrored on the model (column ``index=True`` or
# ``__table_args__``).
UNIQUES = {
    MENTIONS: ((MENTIONS_UNIQUE, ("company_id", "comment_id", "user_id")),),
    WATCHERS: ((WATCHERS_UNIQUE, ("company_id", "entity_type", "entity_id", "user_id")),),
}

# {table: ((local columns, referred table), ...)} -- unnamed, as on the model; every one
# references the referred table's ``id``.
FOREIGN_KEYS = {
    COMMENTS: ((("author_id",), "users"), (("company_id",), "companies")),
    MENTIONS: ((("comment_id",), "comments"), (("user_id",), "users"), (("company_id",), "companies")),
    WATCHERS: ((("user_id",), "users"), (("company_id",), "companies")),
}

INDEXES = {
    COMMENTS: (
        ("ix_comments_author_id", ("author_id",)),
        ("ix_comments_is_deleted", ("is_deleted",)),
        ("ix_comments_company_id", ("company_id",)),
        # The thread read: one record's comments in this company, oldest first.
        ("ix_comments_entity", ("company_id", "entity_type", "entity_id", "created_at")),
    ),
    MENTIONS: (
        ("ix_comment_mentions_comment_id", ("comment_id",)),
        ("ix_comment_mentions_user_id", ("user_id",)),
        ("ix_comment_mentions_company_id", ("company_id",)),
    ),
    WATCHERS: (
        ("ix_entity_watchers_user_id", ("user_id",)),
        ("ix_entity_watchers_company_id", ("company_id",)),
    ),
}

# PostgreSQL only, and LOCAL: bounds the wait for the locks the new FKs take on users /
# companies, then hands the normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _columns(table_name: str) -> list:
    """The columns, in create_all's emission order: the class's own, then SoftDeleteMixin's,
    then TenantMixin's ``company_id``."""
    if table_name == COMMENTS:
        return [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("entity_type", sa.String(length=40), nullable=False),
            sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("author_id", sa.Integer(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            # SoftDeleteMixin: the plain-string default, and ``deleted_by`` with no FK.
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Integer(), nullable=True),
            sa.Column("company_id", sa.Integer(), nullable=False),
        ]
    if table_name == MENTIONS:
        return [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("comment_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
        ]
    return [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
    ]


def security_sql(table_name: str) -> tuple:
    """Byte-identical to ``app.models.comment.security_sql`` (a test pins it)."""
    return (
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY",
        f"REVOKE ALL ON TABLE {table_name} FROM PUBLIC",
        f"REVOKE ALL ON SEQUENCE {table_name}_id_seq FROM PUBLIC",
        f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE {table_name} FROM anon;
        REVOKE ALL ON SEQUENCE {table_name}_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE {table_name} FROM authenticated;
        REVOKE ALL ON SEQUENCE {table_name}_id_seq FROM authenticated;
      END IF;
    END $$""",
    )


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
    return bool(op.get_context().as_sql)


def _dialect() -> str:
    return op.get_context().dialect.name


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    if _offline():
        return False
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix.get("name") == index_name for ix in _inspector().get_indexes(table_name))


def _has_unique(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(uq.get("name") == constraint_name for uq in _inspector().get_unique_constraints(table_name))


def _has_foreign_key(table_name: str, columns: tuple, referred_table: str) -> bool:
    """Matched by shape, not name: these FKs are unnamed on the model and in ``create_table``
    (SQLite reflects no name for them; PostgreSQL generates ``<table>_<column>_fkey``)."""
    if not _has_table(table_name):
        return False
    return any(
        tuple(fk.get("constrained_columns") or ()) == tuple(columns) and fk.get("referred_table") == referred_table
        for fk in _inspector().get_foreign_keys(table_name)
    )


def _recovered_fk_name(table_name: str, columns: tuple) -> str:
    """The name PostgreSQL itself gives an unnamed FK, so an FK re-added to a partially
    bootstrapped table is indistinguishable there from the create_all one. (Alembic's batch mode
    refuses an unnamed constraint, so SQLite gets the same name; only that recovery path shows it.)
    """
    return f"{table_name}_{'_'.join(columns)}_fkey"


def _create_table(table_name: str) -> None:
    op.create_table(
        table_name,
        *_columns(table_name),
        sa.PrimaryKeyConstraint("id"),
        # create_all's order (key, unique, then FKs), so even the stored CREATE TABLE text matches.
        *(sa.UniqueConstraint(*columns, name=name) for name, columns in UNIQUES.get(table_name, ())),
        *(sa.ForeignKeyConstraint(list(columns), [f"{referred}.id"]) for columns, referred in FOREIGN_KEYS[table_name]),
    )


def _upgrade_table(table_name: str) -> None:
    if not _has_table(table_name):
        _create_table(table_name)
    elif not _offline():
        # A partial bootstrap: the table exists, possibly without one of its constraints. Each is
        # checked on its own; any that is missing goes in through one batch (a passthrough on
        # PostgreSQL, a single table rebuild on SQLite, which cannot ALTER in a constraint).
        missing_uniques = [(n, c) for n, c in UNIQUES.get(table_name, ()) if not _has_unique(table_name, n)]
        missing_fks = [(c, r) for c, r in FOREIGN_KEYS[table_name] if not _has_foreign_key(table_name, c, r)]
        if missing_uniques or missing_fks:
            with op.batch_alter_table(table_name) as batch:
                for name, columns in missing_uniques:
                    batch.create_unique_constraint(name, list(columns))
                for columns, referred in missing_fks:
                    fk_name = _recovered_fk_name(table_name, columns)
                    batch.create_foreign_key(fk_name, referred, list(columns), ["id"])
    for index_name, columns in INDEXES[table_name]:
        if not _has_index(table_name, index_name):
            op.create_index(index_name, table_name, list(columns), unique=False)


def _secure_table(table_name: str) -> None:
    # RLS / REVOKE run under the dialect check only -- never under a table guard -- because
    # create_all can build these tables before this revision runs. Idempotent on PostgreSQL.
    for statement in security_sql(table_name):
        op.execute(statement)


def upgrade() -> None:
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    for table_name in TABLES:
        _upgrade_table(table_name)

    if postgres:
        for table_name in TABLES:
            _secure_table(table_name)
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    # LOSSY: every comment thread and watch list goes with its table (indexes, constraints and
    # RLS go too). The audit_log rows that recorded the comment writes are untouched.
    for table_name in DROP_ORDER:
        if _offline() or _has_table(table_name):
            op.drop_table(table_name)

    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
