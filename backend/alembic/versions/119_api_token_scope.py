"""Add api_tokens.scope + api_tokens.ai_external (read-only and AI-external API tokens)

Revision ID: 119_api_token_scope
Revises: 118_dashboard_layouts
Create Date: 2026-11-13

Context
-------
An API token (``088_api_tokens``) has always been exactly as powerful as the user it is
bound to. The Claude manufacturing-plugin brief (P0.1) needs a token that can only READ,
whatever its user's role -- the owner's Admin assistant keeps a full token, the outside
AI connector gets a narrowed one -- so the narrowing goes on the TOKEN. This revision
adds the two columns that carry it, over the EXISTING ``api_tokens`` table
(``app/models/api_token.py::ApiToken``):

- ``scope``        ``VARCHAR(16) NOT NULL DEFAULT 'full'`` with
                   ``CONSTRAINT ck_api_tokens_scope CHECK (scope IN ('full', 'read_only'))``.
                   ``full`` = the user's whole role (today's behavior); ``read_only`` = GET
                   routes only, never a route under a tag in ``WERCO_MCP_READ_ONLY_DENY_TAGS``.
- ``ai_external``  ``BOOLEAN NOT NULL DEFAULT 'false'``. True only on a read-only token whose
                   answers go to an outside AI service (the export-control rules key on it).

Both can only NARROW what the bound user can do, never widen it; both are fixed at mint
(changing either is revoke + re-mint, there is no update verb).

No backfill on PostgreSQL (deliberate)
--------------------------------------
Every existing row reads ``full`` / ``false`` from the server defaults, which is exactly
how every pre-119 token behaves today, so nothing in flight changes underneath a running
bot. There is no ``UPDATE`` on PostgreSQL: on PostgreSQL 11+ ``ADD COLUMN ... NOT NULL
DEFAULT <constant>`` is metadata-only, and the defaults ARE the backfill. A String + CHECK
rather than a native ENUM: the recent house style, and it never needs an ``ALTER TYPE`` on
live Postgres.

SQLite only (local dev databases): the batch rebuild fills existing rows with the quoted
default as the TEXT ``'false'``, and SQLAlchemy's Boolean reads any non-empty text as
``True`` -- every pre-119 token would have read as AI-external, disagreed with its JWT and
answered 401 (code review P0, finding 3). So on SQLite, and only for rows the rebuild just
filled, the upgrade normalizes ``'false'`` to ``0`` (``SQLITE_NORMALIZE_AI_EXTERNAL_SQL``).
The DDL default itself stays byte-identical with the model's.

Shape / compliance
------------------
- ``api_tokens`` is an EXISTING tenant table (TenantMixin non-null ``company_id`` + index;
  RLS enabled by ``088``). This revision creates no table, so the ENABLE-RLS new-table
  convention does not apply, and it emits no RLS / ``REVOKE`` / role-naming statement.
- Pure DDL: no row is read or written. The tamper-evident ``audit_log`` table is untouched.
- ``088``'s docstring says no scope column would be added; ``088`` is an APPLIED migration
  and is not edited (CLAUDE.md -> Migrations). Its lock-step test now treats ``scope`` /
  ``ai_external`` as columns introduced HERE.

Guards (the 088 / 117 / 118 style)
----------------------------------
Each of the three objects -- ``scope``, ``ai_external``, ``ck_api_tokens_scope`` -- is
guarded independently, because the fresh bootstrap runs every revision after the ``058``
stamp over a schema ``create_all`` already built from the model (where all three exist):
there it is a clean no-op. ``--sql`` (offline) has no database to inspect and renders the
canonical statements.

SQLite cannot add a CHECK to an existing table with ``ALTER TABLE``, so on SQLite the work
runs in ``batch_alter_table`` (table recreate; named CHECK constraints are reflected and
carried by alembic's batch mode). The downgrade drops the CHECK BEFORE the column it names,
as batch mode requires.

Downgrade is LOSSY: it discards every token's scope, so a read-only or AI-external token
minted after 119 would come back as a FULL token -- writes allowed, no export-control
redaction -- if it stayed live across a downgrade. So the downgrade REFUSES (raises, before
any DDL) while any such token is live, naming how many (code review P0, finding 4): revoke
every one first (``SELECT id, label FROM api_tokens WHERE (scope <> 'full' OR ai_external)
AND revoked = false``). ``--sql`` (offline) has no database to check: run that query by
hand first. A CODE-only rollback (database left at 119) is not covered by this check -- the
pre-119 code ignores the narrowing claims, so revoke the narrowed tokens before that too.

Lock-step with the model: ``ApiToken.scope`` / ``ApiToken.ai_external`` and the model-level
``CheckConstraint`` carry the identical type, nullability, server default and condition text;
``tests/test_migration_119_api_token_scope.py`` pins the two equal.

Revision id ``119_api_token_scope`` is 19 chars (<= 32) per the create_all -> stamp ->
upgrade bootstrap constraint (``alembic_version.version_num`` is varchar(32)).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "119_api_token_scope"
down_revision = "118_dashboard_layouts"
branch_labels = None
depends_on = None

TABLE_NAME = "api_tokens"
SCOPE_COLUMN = "scope"
AI_EXTERNAL_COLUMN = "ai_external"

# Kept byte-identical with ``app.models.api_token.API_TOKEN_SCOPE_CHECK_*``.
CHECK_NAME = "ck_api_tokens_scope"
CHECK_CONDITION = "scope IN ('full', 'read_only')"

# PostgreSQL only, and LOCAL: it bounds the wait for the ACCESS EXCLUSIVE lock on
# api_tokens and is handed back as soon as the DDL is done (precedent 114/117).
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


# SQLite only: the rows the batch rebuild filled with the TEXT default 'false' (see the
# module docstring). Never run on PostgreSQL, where the column is a real boolean.
SQLITE_NORMALIZE_AI_EXTERNAL_SQL = "UPDATE api_tokens SET ai_external = 0 WHERE ai_external = 'false'"


def _scope_column() -> sa.Column:
    return sa.Column(SCOPE_COLUMN, sa.String(length=16), nullable=False, server_default="full")


def _ai_external_column() -> sa.Column:
    # Plain-string server default, exactly like ``revoked`` on this table (never sa.text).
    return sa.Column(AI_EXTERNAL_COLUMN, sa.Boolean(), nullable=False, server_default="false")


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
    return bool(op.get_context().as_sql)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_context().dialect.name == "sqlite"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_check_constraint(table_name: str, constraint_name: str) -> bool:
    """Name membership is sufficient: the name is explicit on both paths (067's helper)."""
    if not _has_table(table_name):
        return False
    return any(ck.get("name") == constraint_name for ck in _inspector().get_check_constraints(table_name))


def _live_narrowed_tokens(has_scope: bool, has_ai_external: bool) -> int:
    """How many un-revoked tokens are read-only or AI-external (what a downgrade would WIDEN)."""
    tokens = sa.table(
        TABLE_NAME,
        sa.column("revoked", sa.Boolean()),
        sa.column(SCOPE_COLUMN, sa.String()),
        sa.column(AI_EXTERNAL_COLUMN, sa.Boolean()),
    )
    narrowed = []
    if has_scope:
        narrowed.append(tokens.c[SCOPE_COLUMN] != "full")
    if has_ai_external:
        narrowed.append(tokens.c[AI_EXTERNAL_COLUMN] == sa.true())
    if not narrowed or not _has_table(TABLE_NAME):
        return 0
    query = sa.select(sa.func.count()).select_from(tokens).where(tokens.c.revoked == sa.false(), sa.or_(*narrowed))
    return int(op.get_bind().execute(query).scalar() or 0)


def upgrade() -> None:
    if _offline():
        need_scope = need_ai_external = need_check = True
    else:
        need_scope = not _has_column(TABLE_NAME, SCOPE_COLUMN)
        need_ai_external = not _has_column(TABLE_NAME, AI_EXTERNAL_COLUMN)
        need_check = not _has_check_constraint(TABLE_NAME, CHECK_NAME)
    if not (need_scope or need_ai_external or need_check):
        # The create_all bootstrap already built all three from the model.
        return

    if _is_sqlite():
        # SQLite has no ALTER TABLE ... ADD CONSTRAINT: recreate in batch mode.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            if need_scope:
                batch_op.add_column(_scope_column())
            if need_ai_external:
                batch_op.add_column(_ai_external_column())
            if need_check:
                batch_op.create_check_constraint(CHECK_NAME, CHECK_CONDITION)
        if need_ai_external:
            op.execute(SQLITE_NORMALIZE_AI_EXTERNAL_SQL)
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if need_scope:
        op.add_column(TABLE_NAME, _scope_column())
    if need_ai_external:
        op.add_column(TABLE_NAME, _ai_external_column())
    if need_check:
        op.create_check_constraint(CHECK_NAME, TABLE_NAME, CHECK_CONDITION)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY: see the module docstring -- refused while a narrowed token is live.
    if _offline():
        has_scope = has_ai_external = has_check = True
    else:
        has_scope = _has_column(TABLE_NAME, SCOPE_COLUMN)
        has_ai_external = _has_column(TABLE_NAME, AI_EXTERNAL_COLUMN)
        has_check = _has_check_constraint(TABLE_NAME, CHECK_NAME)
        live = _live_narrowed_tokens(has_scope, has_ai_external)
        if live:
            raise RuntimeError(
                f"Refusing to downgrade 119_api_token_scope: {live} live read-only / AI-external API "
                "token(s) would become FULL tokens (writes allowed, no export-control redaction). Revoke "
                "them first: SELECT id, label FROM api_tokens WHERE (scope <> 'full' OR ai_external) "
                "AND revoked = false"
            )
    if not (has_scope or has_ai_external or has_check):
        return

    if _is_sqlite():
        # The CHECK names ``scope``, so batch mode needs it dropped before the column.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            if has_check:
                batch_op.drop_constraint(CHECK_NAME, type_="check")
            if has_ai_external:
                batch_op.drop_column(AI_EXTERNAL_COLUMN)
            if has_scope:
                batch_op.drop_column(SCOPE_COLUMN)
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if has_check:
        op.drop_constraint(CHECK_NAME, TABLE_NAME, type_="check")
    if has_ai_external:
        op.drop_column(TABLE_NAME, AI_EXTERNAL_COLUMN)
    if has_scope:
        op.drop_column(TABLE_NAME, SCOPE_COLUMN)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
