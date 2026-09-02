"""088 - api_tokens: long-lived, revocable, per-user bearer tokens for bots and agents

Adds ONE new table, ``api_tokens``, backing the ``/api/v1/api-tokens`` router and
the ``type="api"`` JWT that ``app.api.deps.get_current_user`` accepts beside the
15-minute interactive access token. One row per issued token: the ONE user it acts
as, a label, the JWT's ``jti`` (the revocation handle — the JWT itself is never
stored), an optional expiry, the revocation trail, a coarse last-used marker, and
who issued it.

Why a row per token, looked up on every request
-----------------------------------------------
The interactive access token is stateless and short-lived; a bot needs the
opposite — a credential that outlives a shift — and a long-lived stateless JWT
cannot be revoked. So the JWT is anchored to THIS row, exactly the shape
``display_tokens`` (``050``) uses for wallboard TVs: the auth path resolves the row
by ``jti`` on every request and refuses when the row is missing, ``revoked``, past
``expires_at``, or when the JWT's ``user_id`` / ``company_id`` claims disagree with
the row. **The row is authoritative, the claims are not** — that is what keeps a
forged claim from widening tenancy, and it is why ``company_id`` here is NOT NULL
and pinned at issuance rather than read from the client.

What the table deliberately does NOT hold
-----------------------------------------
* The token. Only ``jti``. The plaintext is returned once at issuance and must
  never land in a log line, an ``audit_log`` row, or an error message.
* A role, a scope, or a permission list. A token is only as powerful as the user
  it belongs to — ``require_role`` decides as it does for the SPA. No column here
  can widen that, and none is being added later.
* A ``SoftDeleteMixin``. Revocation is the tombstone: rows are never physically
  deleted (who held access is a record), and there is no third state to forget to
  filter — the same reasoning as ``085``'s ``inventory_combines``.

Lock-step with ``app/models/api_token.py``
------------------------------------------
Every column and all SIX indexes are declared on the model too, and this file
mirrors the model rather than the other way round. Bootstrap here is ``create_all``
-> ``alembic stamp <baseline>`` -> incremental ``upgrade``, so a freshly-provisioned
database builds this table from ``create_all`` and then STAMPS PAST this revision.
Anything declared only here would be silently missing on every fresh environment
while ``alembic_version`` claimed it existed — the stamped-over hazard that cost
prod the ``008`` audit triggers (2026-07-07), ~42 read-path indexes (restored by
``079``) and ``003``'s named FKs and CHECKs (restored by ``080``). Hence the
``042``/``078``/``079``/``080``/``085``/``087`` convention: model and migration
change in the same PR, and ``tests/test_migration_088_api_tokens.py`` pins them
equal.

Two consequences of "mirror the model, don't improve on it":

* Column ORDER matches ``create_all``'s emission exactly — the class's own columns
  first, then ``TenantMixin``'s ``company_id`` appended last via the MRO. Cosmetic
  to Postgres; it is what lets a schema diff between a bootstrapped and a migrated
  database come back empty.
* ``created_at`` is naive ``DateTime`` NOT NULL with a **Python-side**
  ``datetime.utcnow`` default and no ``server_default``, matching the model and
  ``display_tokens``. Adding ``server_default=now()`` HERE only would give the two
  bootstrap paths two different schemas. A raw-SQL INSERT that omits it therefore
  fails; every writer goes through the ORM.
* ``revoked``'s ``server_default`` is the plain string ``"false"``, never
  ``sa.text("false")`` — SQLAlchemy quotes the string, so the two spellings emit
  different DDL (``DEFAULT 'false'`` vs ``DEFAULT false``), and the model's is the
  quoted one. Same spelling as ``050``/``087``.

The ``jti`` index is UNIQUE and GLOBAL, on purpose
-------------------------------------------------
``ix_api_tokens_jti`` is unique over ``jti`` alone, not ``(company_id, jti)``. The
auth path resolves the row by ``jti`` BEFORE it knows which company the caller
belongs to — the company comes FROM the row — so the handle has to be unique across
every tenant. ``jti`` is 43 characters of ``secrets.token_urlsafe(32)``; a collision
is not a realistic event, and if one ever happened the unique index turns it into a
refused insert rather than two tokens sharing a revocation handle.

Shape / compliance
------------------
* Tenant-scoped (invariant 1): ``company_id`` INTEGER NOT NULL, FK ``companies.id``,
  indexed — the ``TenantMixin`` shape. Both composite indexes are tenant-led.
  App-layer tenancy via ``tenant_query`` is the enforcement.
* Three FKs to ``users.id`` (``user_id``, ``created_by``, ``revoked_by``), all plain
  with no ``ondelete``: a user row that still anchors a token cannot be physically
  removed, so "the user is gone" can only mean deactivated — which the auth path
  already refuses. Do not add a ``CASCADE`` here: it would silently destroy the
  record of who held access.
* ``ENABLE ROW LEVEL SECURITY`` on the new table, Postgres only. This revision runs
  after ``059``/``060``, which enabled RLS across ``public`` and left it
  deny-by-default with zero policies, so a table created afterwards must enable it
  itself or it re-flags the Supabase Security Advisor's ``rls_disabled_in_public``
  ERROR (precedent ``061``/``084``/``085``/``087``; see ``docs/SUPABASE_SECURITY.md``).
  The app role bypasses RLS.
* The tamper-evident ``audit_log`` table is untouched, and so is every other existing
  table. This revision is purely additive DDL: it creates no enum, alters no enum,
  adds no column anywhere else, and — in particular — does NOT touch ``users``.
  A token names a user; the user knows nothing about it.

No backfill
-----------
There is nothing to backfill. No API token has ever existed, and minting rows here
would be issuing credentials nobody asked for — a token is an Admin's audited act,
never a migration's.

Idempotent and reversible
-------------------------
Upgrade guards the table and each index independently (``_has_table`` / ``_has_index``),
so the create_all-bootstrapped path and any re-run are clean no-ops. Downgrade drops
the indexes then the table, each guarded, so a partially applied upgrade still
downgrades cleanly. Round-tripped locally: ``upgrade head`` -> ``downgrade -1`` ->
``upgrade head``.

Operational note: ``CREATE TABLE`` on a table that does not yet exist takes no lock
anything else can be waiting on, and there is no backfill pass, so this migration is
safe to run against live Postgres during traffic and carries no deploy-ordering
constraint of its own — beyond the obvious one that it must land before the code that
reads the table. Downgrading DROPS every issued token: every bot holding one is
locked out at once, which is the correct consequence of unmaking the schema and is
not silently recoverable — re-issue, don't restore.

Revision ID: 088_api_tokens
Revises: 087_work_order_templates
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "088_api_tokens"
down_revision = "087_work_order_templates"
branch_labels = None
depends_on = None


TABLE = "api_tokens"

# (index_name, columns, unique) -- every one of these is mirrored on the model. The
# first four come from ``index=True`` / ``unique=True`` on the columns themselves
# (``company_id`` via TenantMixin); the last two are the model's explicit
# ``__table_args__``.
INDEXES = [
    ("ix_api_tokens_id", ["id"], False),
    ("ix_api_tokens_user_id", ["user_id"], False),
    # UNIQUE and GLOBAL -- the auth path resolves the row by jti before it knows
    # the company; see the docstring.
    ("ix_api_tokens_jti", ["jti"], True),
    ("ix_api_tokens_company_id", ["company_id"], False),
    # "Which tokens does this user hold?"
    ("ix_api_tokens_company_user", ["company_id", "user_id"], False),
    # "Which live tokens exist in this company?" (the default list read)
    ("ix_api_tokens_company_revoked", ["company_id", "revoked"], False),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _create_table() -> None:
    # Column order matches create_all's emission order exactly: the class's own
    # columns first, then TenantMixin's ``company_id`` (appended via the MRO).
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        # The ONLY identity the token can act as.
        sa.Column("user_id", sa.Integer(), nullable=False),
        # Human label for the holder. Stored verbatim -- there is no ingest-time
        # sanitization in this system and nothing renders it as raw HTML.
        sa.Column("label", sa.String(length=100), nullable=False),
        # JWT ID claim -- the revocation handle and the only thing stored about the
        # JWT. Unique across all tenants via ix_api_tokens_jti below.
        sa.Column("jti", sa.String(length=64), nullable=False),
        # Authoritative expiry; NULL = never expires. The auth path checks THIS
        # column, not the JWT's ``exp``.
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        # Revocation trail -- revoke, never delete. ``server_default="false"`` is the
        # plain string the model declares, NOT ``sa.text("false")``; see the
        # docstring on why the two render different DDL.
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.Integer(), nullable=True),
        sa.Column("revoke_reason", sa.String(length=255), nullable=True),
        # Coarse liveness marker -- touched at most once per five minutes.
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        # The Admin who issued it. NOT NULL with a Python-side default only on
        # ``created_at`` -- mirrors the model; no server default is added here.
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # TenantMixin, last.
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["revoked_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1) The table. Guarded so the create_all bootstrap path (where the model already
    #    built it) no-ops rather than erroring on a duplicate table.
    if not _has_table(TABLE):
        _create_table()

    # 2) Indexes, each guarded independently -- create_all emits all six too, and a
    #    re-run or a partially applied upgrade must be a no-op rather than a failure.
    for index_name, columns, unique in INDEXES:
        if _has_index(TABLE, index_name):
            continue
        op.create_index(index_name, TABLE, columns, unique=unique)

    # 3) RLS on the new table (Postgres only; this revision runs after 059/060).
    #    Deny-by-default with zero policies; the app role bypasses it and app-layer
    #    tenancy via tenant_query stays the enforcement. Without this the Supabase
    #    Security Advisor re-flags rls_disabled_in_public as an ERROR.
    if is_postgres:
        op.execute(f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    # Indexes first, then the table (FK-safe order). Each guarded so a partial upgrade
    # downgrades cleanly. Dropping the table takes the FKs and the RLS setting with it
    # -- there is nothing left behind to un-enable.
    #
    # This drops every issued token, locking out every bot that holds one. That is
    # the correct consequence of unmaking the schema; the tamper-evident account of
    # every issue/revoke is the ``audit_log`` row, which both directions of this
    # revision leave alone. Tokens are re-issued, never restored.
    for index_name, _columns, _unique in INDEXES:
        if _has_index(TABLE, index_name):
            op.drop_index(index_name, table_name=TABLE)

    if _has_table(TABLE):
        op.drop_table(TABLE)
