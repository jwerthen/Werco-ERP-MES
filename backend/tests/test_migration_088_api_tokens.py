"""Coverage for 088_api_tokens (the long-lived, revocable, per-user bearer-token table).

088 CREATEs one new tenant-scoped table, ``api_tokens``: the ONE user a token acts
as, a label, the JWT's ``jti`` (the revocation handle -- the JWT itself is never
stored), an optional expiry, the revocation trail, a coarse last-used marker and who
issued it. It alters nothing that already exists -- in particular it does not touch
``users``, which knows nothing about the tokens that name it -- and it backfills
nothing, because no API token has ever existed and minting rows from a migration
would be issuing credentials nobody asked for.

The auth and endpoint behavior lives in ``tests/test_api_tokens.py`` and
``tests/test_api_token_auth.py``; THIS file covers the MIGRATION.

WHY THIS FILE EXISTS
--------------------
Bootstrap here is ``create_all`` -> ``alembic stamp <baseline>`` -> incremental
``upgrade`` (docs/DEVELOPMENT.md), so a freshly provisioned database builds this table
from the MODEL and then stamps straight past this revision. Anything declared only in
the migration is silently absent on every fresh environment while ``alembic_version``
claims it was applied -- the hazard that cost prod the ``008`` audit triggers
(2026-07-07), ~42 read-path indexes (restored by ``079``) and ``003``'s named FKs and
CHECKs (restored by ``080``). And it cuts the other way too: an index declared only on
the model never reaches an already-provisioned database. Either way the two
environments stop being the same schema, and the ``042``/``078``/``079``/``080``/
``085``/``087`` lock-step convention exists to prevent exactly that. This file is what
holds ``alembic/versions/088_api_tokens.py`` and ``app/models/api_token.py`` equal.

Two things about this table are security properties rather than bookkeeping, and both
are pinned here:

* **``ix_api_tokens_jti`` is UNIQUE and GLOBAL** -- over ``jti`` alone, not
  ``(company_id, jti)``. The auth path resolves the row by ``jti`` BEFORE it knows the
  company (the company comes FROM the row), so the handle must be unique across every
  tenant. Section 4 compiles the index for both dialects and section 5 executes the
  refusal on the migrated schema.
* **There is no ``SoftDeleteMixin``.** Revocation is the tombstone; rows are never
  physically deleted. A soft-delete block here would add a third state every reader
  could forget to filter -- the ``085`` reasoning. So the column list ends at
  ``company_id`` with nothing between it and ``created_at``.

Also pinned: ``revoked``'s ``server_default`` is the plain string ``"false"``, never
``sa.text("false")`` (commit ``e90621e`` fixed exactly that divergence on an earlier
table), and ``created_at`` has a Python-side default ONLY on both paths.

LAYERS
------
1. Script wiring (unit) -- one alembic head, the ``087`` -> ``088`` chain, an id that
   fits ``alembic_version``'s varchar(32), callable ``upgrade``/``downgrade``.
2. Source + recorded-DDL invariants (unit) -- both migration functions are RUN with
   ``op`` and the two existence guards stubbed, so what they emit is inspected directly:
   guarded creates, a real guarded downgrade, a Postgres-gated RLS statement, no data
   statement of any kind, and no table named other than this one.
3. Model / migration lock-step (unit) -- the reason this file exists.
4. Index portability (unit) -- dialect-compiled, both engines.
5. A real upgrade -> downgrade -> upgrade round-trip over a disposable SQLite file
   (integration/slow), ending in the BEHAVIORAL contract of the unique index on the
   schema the MIGRATION built: one ``jti`` across every tenant.
"""

import importlib.util
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION = "088_api_tokens"
MIGRATION_FILE = "088_api_tokens.py"
DOWN_REVISION = "087_work_order_templates"

TABLE = "api_tokens"
UNIQUE_INDEX = "ix_api_tokens_jti"

# (column, nullable) in create_all's emission ORDER: the class's own columns, then
# TenantMixin's company_id LAST via the MRO. No SoftDeleteMixin block -- revocation is
# the tombstone.
EXPECTED_COLUMNS = [
    ("id", False),
    ("user_id", False),
    ("label", False),
    ("jti", False),
    ("expires_at", True),
    ("revoked", False),
    ("revoked_at", True),
    ("revoked_by", True),
    ("revoke_reason", True),
    ("last_used_at", True),
    ("created_by", False),
    ("created_at", False),
    ("company_id", False),
]

# Every index both paths must build. The first four come from ``index=True`` /
# ``unique=True`` on the columns themselves (company_id via TenantMixin); the last two
# are the model's explicit ``__table_args__``.
EXPECTED_INDEXES = {
    "ix_api_tokens_id",
    "ix_api_tokens_user_id",
    UNIQUE_INDEX,
    "ix_api_tokens_company_id",
    "ix_api_tokens_company_user",
    "ix_api_tokens_company_revoked",
}

# (local column, target). Three plain FKs to users.id with NO ondelete -- a user row
# that still anchors a token cannot be physically removed, so the record of who held
# access survives.
EXPECTED_FOREIGN_KEYS = {
    ("user_id", "users.id"),
    ("revoked_by", "users.id"),
    ("created_by", "users.id"),
    ("company_id", "companies.id"),
}


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    """A FRESH module object each call, so a test that stubs its globals cannot leak."""
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_088", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped.

    The prose names at length every construct the migration deliberately avoids (no
    backfill, no soft-delete, no CASCADE) and quotes the audit_log by name, so
    assertions about ABSENT constructs have to look at CODE only or the docstring
    itself would fail them.
    """
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


# ---------------------------------------------------------------------------
# Recording the DDL the migration actually emits
# ---------------------------------------------------------------------------


class _RecordedDDL:
    """A stand-in for alembic's ``op`` that records calls instead of executing them.

    Running the migration functions beats grepping their source: it proves what they
    EMIT (including the kwargs on each ``create_index``) and it can be pointed at
    either dialect without a Postgres server.
    """

    def __init__(self, dialect_name: str):
        self.dialect_name = dialect_name
        self.calls: list = []

    # -- the slice of the ``op`` surface 088 uses ---------------------------
    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect_name))

    def create_table(self, table_name, *args, **kwargs):
        self.calls.append(("create_table", table_name, args, kwargs))

    def create_index(self, index_name, table_name, columns, **kwargs):
        self.calls.append(("create_index", index_name, table_name, list(columns), kwargs))

    def drop_index(self, index_name, **kwargs):
        self.calls.append(("drop_index", index_name, kwargs))

    def drop_table(self, table_name, **kwargs):
        self.calls.append(("drop_table", table_name, kwargs))

    def execute(self, statement):
        self.calls.append(("execute", str(statement)))

    # -- readers ------------------------------------------------------------
    @property
    def kinds(self) -> list:
        return [call[0] for call in self.calls]

    def names(self, kind: str) -> list:
        return [call[1] for call in self.calls if call[0] == kind]

    @property
    def executed(self) -> list:
        return [call[1] for call in self.calls if call[0] == "execute"]

    @property
    def table_columns(self) -> list:
        [call] = [c for c in self.calls if c[0] == "create_table"]
        return [arg for arg in call[2] if isinstance(arg, sa.Column)]

    @property
    def table_constraints(self) -> list:
        [call] = [c for c in self.calls if c[0] == "create_table"]
        return [arg for arg in call[2] if not isinstance(arg, sa.Column)]

    @property
    def indexes(self) -> dict:
        """name -> (column tuple, unique)."""
        out = {}
        for call in self.calls:
            if call[0] != "create_index":
                continue
            _kind, name, table_name, columns, kwargs = call
            assert table_name == TABLE, f"{name} was built on {table_name}"
            assert "postgresql_where" not in kwargs and "sqlite_where" not in kwargs, f"{name}: no index is partial"
            out[name] = (tuple(columns), bool(kwargs.get("unique", False)))
        return out


def _record(function_name: str, *, dialect: str = "postgresql", objects_exist: bool = False) -> _RecordedDDL:
    """Run ``upgrade()`` / ``downgrade()`` against a recorder instead of a database.

    ``_has_table`` / ``_has_index`` are stubbed to a constant so both sides of every
    guard are reachable: ``objects_exist=False`` is a virgin database (everything gets
    built), ``True`` is the create_all-bootstrapped database (everything must no-op).
    """
    module = _load_module()
    recorder = _RecordedDDL(dialect)
    module.op = recorder
    module._has_table = lambda _table: objects_exist
    module._has_index = lambda _table, _index: objects_exist
    getattr(module, function_name)()
    return recorder


def _model_table():
    from app.models.api_token import ApiToken

    return ApiToken.__table__


def _model_indexes() -> dict:
    """name -> (column tuple, unique), read off the model."""
    out = {}
    for index in _model_table().indexes:
        assert index.dialect_options["postgresql"].get("where") is None, f"{index.name}: no index is partial"
        assert index.dialect_options["sqlite"].get("where") is None, f"{index.name}: no index is partial"
        out[index.name] = (tuple(column.name for column in index.columns), bool(index.unique))
    return out


def _fingerprint(column: sa.Column) -> tuple:
    """(name, type family, nullable, the type detail that matters) for one column."""
    detail = {}
    if isinstance(column.type, sa.String):
        detail["length"] = column.type.length
    if isinstance(column.type, sa.DateTime):
        detail["timezone"] = column.type.timezone
    return (column.name, type(column.type).__name__, column.nullable, tuple(sorted(detail.items())))


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    """One head, and 088 hangs off 087 (the work-order templates table).

    A second head is not cosmetic: ``upgrade head`` fails outright, so a branched chain
    is a deploy that cannot run.

    Deliberately does NOT assert that 088 IS the head -- that assertion fails for
    whoever writes the NEXT migration no matter what they write. The two things worth
    pinning survive: exactly one head, and 088's own parent.
    """
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert REVISION in chain, f"088 is not an ancestor of head {heads[0]}"
    assert DOWN_REVISION in chain, "088 must sit above 087 on the same path"


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32) -- the
    # create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION) <= 32


@pytest.mark.unit
def test_module_loads_and_names_exactly_the_object_it_owns():
    module = _load_module()
    assert module.revision == REVISION
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert module.TABLE == TABLE
    # The guard idiom that makes the create_all-bootstrapped path a clean no-op.
    assert callable(module._has_table)
    assert callable(module._has_index)


# ---------------------------------------------------------------------------
# 2. What the migration functions actually emit (guards, downgrade, RLS, no DML)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_virgin_database_gets_the_table_then_every_index():
    """Order matters: the table, then all six indexes, then RLS."""
    recorder = _record("upgrade", objects_exist=False)
    assert recorder.names("create_table") == [TABLE]
    assert set(recorder.names("create_index")) == EXPECTED_INDEXES
    assert recorder.kinds[0] == "create_table", "indexes cannot precede the table"
    assert recorder.kinds.count("create_table") == 1


@pytest.mark.unit
def test_the_bootstrapped_database_is_a_clean_no_op():
    """THE idempotency claim, executed rather than grepped.

    On the ``create_all`` -> ``stamp`` -> ``upgrade`` path the model has already built
    the table and all six indexes, and a re-run over a partially applied upgrade is a
    real state too. Both guards must fire, leaving only the RLS statement (which is
    itself idempotent).
    """
    recorder = _record("upgrade", objects_exist=True)
    assert recorder.names("create_table") == []
    assert recorder.names("create_index") == []
    assert recorder.kinds == ["execute"], f"a guarded re-run emitted {recorder.kinds}"

    source = _source()
    assert "if not _has_table(TABLE):" in source
    assert "if _has_index(TABLE, index_name):" in source


@pytest.mark.unit
def test_downgrade_is_real_and_drops_the_indexes_before_the_table():
    """Not a ``pass`` stub, and each drop is guarded so a partially applied upgrade still
    downgrades cleanly."""
    recorder = _record("downgrade", objects_exist=True)
    assert set(recorder.names("drop_index")) == EXPECTED_INDEXES
    assert recorder.names("drop_table") == [TABLE]
    assert recorder.kinds[-1] == "drop_table", "the table must go last (FK-safe order)"

    # Guarded: nothing to drop means nothing is emitted, rather than an error.
    assert _record("downgrade", objects_exist=False).calls == []
    assert "\n    pass" not in _source()


@pytest.mark.unit
def test_the_rls_statement_is_present_and_postgres_gated():
    """Post-059 the posture is deny-by-default RLS with ZERO policies, so a table
    created afterwards must enable it itself or the Supabase Security Advisor re-flags
    ``rls_disabled_in_public`` as an ERROR (docs/SUPABASE_SECURITY.md; precedent
    061/084/085/087). The repo-wide gate in test_migration_059_060_supabase_hardening.py
    checks that the string is present; this pins the statement and its dialect gate.
    """
    postgres = _record("upgrade", dialect="postgresql", objects_exist=False)
    assert postgres.executed == [f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY']

    sqlite_run = _record("upgrade", dialect="sqlite", objects_exist=False)
    assert sqlite_run.executed == [], "SQLite has no RLS -- the statement must be gated"

    body = _body()
    assert "CREATE POLICY" not in body, "deny-by-default means zero policies"
    assert "FORCE ROW LEVEL SECURITY" not in body, "the app role must keep bypassing RLS"


@pytest.mark.unit
def test_there_is_no_backfill_and_no_data_statement_at_all():
    """Nothing to backfill: no API token has ever existed, and minting rows from a
    migration would be issuing credentials nobody asked for -- a token is an Admin's
    audited act.

    Being DDL-only is also what makes the migration structurally incapable of touching
    the tamper-evident ``audit_log`` hash chain (invariant 2), and of ever holding a
    token's plaintext.
    """
    body = _body()
    for forbidden in ("op.bulk_insert", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in body, f"088 must run no data statement; found {forbidden!r}"

    # The only statement either direction executes is the RLS one.
    for recorder in (_record("upgrade", objects_exist=False), _record("downgrade", objects_exist=True)):
        for statement in recorder.executed:
            assert "ROW LEVEL SECURITY" in statement, f"unexpected op.execute: {statement}"


@pytest.mark.unit
def test_no_existing_table_is_touched():
    """088 is purely additive. It creates no enum, alters no enum, adds no column
    anywhere else and -- in particular -- does not touch ``users``: a token names a
    user, the user knows nothing about it.
    """
    body = _body()
    for forbidden in ("op.add_column", "op.drop_column", "op.alter_column", "op.batch_alter_table", "sa.Enum("):
        assert forbidden not in body, f"088 must not {forbidden} -- it only creates a new table"

    for recorder in (_record("upgrade", objects_exist=False), _record("downgrade", objects_exist=True)):
        for name in recorder.names("create_table") + recorder.names("drop_table"):
            assert name == TABLE
        for statement in recorder.executed:
            assert TABLE in statement and "users" not in statement.replace(TABLE, "")


# ---------------------------------------------------------------------------
# 3. Model / migration LOCK-STEP -- the reason this file exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_index_matches_the_model_exactly():
    """Same names, same column tuples, same uniqueness, no partial predicate anywhere.

    An index declared only in the migration is skipped entirely by the ``create_all`` +
    ``stamp`` bootstrap (how prod lost ~42 read-path indexes, restored by 079); one
    declared only on the model never reaches an already-provisioned database. Either way
    the two environments stop being the same schema.
    """
    module = _load_module()
    migration = {name: (tuple(columns), bool(unique)) for name, columns, unique in module.INDEXES}
    assert set(migration) == EXPECTED_INDEXES
    assert len(module.INDEXES) == len(EXPECTED_INDEXES), "duplicate entry in the migration's INDEXES list"
    assert migration == _model_indexes()

    # Uniqueness belongs to exactly one index, on both sides: the GLOBAL jti handle.
    assert {name for name, (_c, unique) in migration.items() if unique} == {UNIQUE_INDEX}
    assert migration[UNIQUE_INDEX] == (("jti",), True), "jti must be unique ALONE, not per company"

    # Postgres identifier limit -- a silently truncated name breaks the guards.
    for name in migration:
        assert len(name) <= 63, f"{name} exceeds the Postgres identifier limit"


@pytest.mark.unit
def test_what_the_upgrade_builds_is_what_the_model_declares():
    """The same comparison again, one level down: against the kwargs ``create_index``
    actually receives, not against the declaration list it reads from."""
    assert _record("upgrade", objects_exist=False).indexes == _model_indexes()


@pytest.mark.unit
def test_every_column_matches_the_model_in_name_order_type_and_nullability():
    """ORDER is part of the comparison, and it is not cosmetic.

    ``create_all`` emits the class's own columns, then ``TenantMixin``'s ``company_id``
    last via the MRO. If the migration emits a different order, a schema diff between a
    bootstrapped and a migrated database never comes back empty -- and a diff that is
    never empty is a diff nobody reads.
    """
    migration_columns = _record("upgrade", objects_exist=False).table_columns
    model_columns = list(_model_table().columns)

    assert [column.name for column in migration_columns] == [name for name, _ in EXPECTED_COLUMNS]
    assert [column.name for column in model_columns] == [name for name, _ in EXPECTED_COLUMNS]
    assert [_fingerprint(column) for column in migration_columns] == [_fingerprint(column) for column in model_columns]
    for column, (name, nullable) in zip(migration_columns, EXPECTED_COLUMNS):
        assert column.name == name and column.nullable is nullable, f"{name} nullability drifted"

    # company_id is appended last by the MRO, directly after the class's own columns.
    names = [name for name, _ in EXPECTED_COLUMNS]
    assert names[-2:] == ["created_at", "company_id"]


@pytest.mark.unit
def test_the_table_stores_the_jti_and_never_the_token():
    """The JWT is never stored -- only its id. A column that could hold the plaintext
    (``token``, ``secret``, a hash of it) must not appear on either path: the row is the
    revocation anchor, not a credential store.
    """
    names = {column.name for column in _model_table().columns}
    assert "jti" in names
    assert _model_table().columns["jti"].type.length == 64
    for forbidden in ("token", "secret", "token_hash", "jwt", "access_token"):
        assert forbidden not in names, f"{forbidden!r} has no business on api_tokens"
    assert "sa.Column(\"token\"" not in _body()


@pytest.mark.unit
def test_there_is_no_soft_delete_block_on_either_path():
    """Revocation is the tombstone. A ``SoftDeleteMixin`` here would add a third state
    every reader could forget to filter (the ``085`` reasoning), so neither path
    carries ``is_deleted`` / ``deleted_at`` / ``deleted_by``.
    """
    from app.db.mixins import SoftDeleteMixin
    from app.models.api_token import ApiToken

    assert not issubclass(ApiToken, SoftDeleteMixin)
    names = {column.name for column in _model_table().columns}
    for tombstone in ("is_deleted", "deleted_at", "deleted_by"):
        assert tombstone not in names, f"{tombstone} must not exist -- revoked is the tombstone"
        assert f'"{tombstone}"' not in _body()
    assert "revoked" in names and _model_table().columns["revoked"].nullable is False


@pytest.mark.unit
def test_revoked_carries_the_plain_string_server_default_not_sa_text():
    """``server_default="false"``, never ``sa.text("false")``. Commit e90621e fixed
    exactly this on an earlier table, so it is pinned rather than trusted.

    SQLAlchemy QUOTES a plain string and inlines a ``text()`` verbatim, so the two spell
    different DDL: ``DEFAULT 'false'`` (what the model renders, i.e. what ``create_all``
    builds) against ``DEFAULT false``. Both renderings are compiled below so the
    difference is demonstrated, not asserted from memory -- on BOTH dialects.
    """
    from sqlalchemy.dialects import postgresql, sqlite

    migration_column = {column.name: column for column in _record("upgrade", objects_exist=False).table_columns}[
        "revoked"
    ]
    model_column = _model_table().columns["revoked"]

    for column in (migration_column, model_column):
        assert column.server_default is not None
        assert isinstance(column.server_default.arg, str), "sa.text() renders DEFAULT false, the model DEFAULT 'false'"
        assert column.server_default.arg == "false"
    assert 'server_default="false"' in _body()
    assert 'server_default=sa.text("false")' not in _body()

    # The divergence itself, compiled: quoted vs bare, on both engines.
    quoted = sa.Table(
        "_quoted", sa.MetaData(), sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false")
    )
    bare = sa.Table(
        "_bare", sa.MetaData(), sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        quoted_ddl = str(sa.schema.CreateColumn(quoted.c.revoked).compile(dialect=dialect))
        bare_ddl = str(sa.schema.CreateColumn(bare.c.revoked).compile(dialect=dialect))
        assert "DEFAULT 'false'" in quoted_ddl
        assert "DEFAULT false" in bare_ddl
        assert quoted_ddl != bare_ddl, "if these ever converge, this test is protecting nothing"
        # …and the real column renders the quoted form on both paths.
        assert "DEFAULT 'false'" in str(sa.schema.CreateColumn(model_column).compile(dialect=dialect))


@pytest.mark.unit
def test_the_primary_key_and_foreign_keys_match_the_model():
    """Three plain FKs to ``users.id`` and one to ``companies.id`` on BOTH paths, none
    with an ``ondelete``: a user row that still anchors a token cannot be physically
    removed, so the record of who held access survives. A ``CASCADE`` on either side
    would silently destroy it.
    """
    table = _model_table()
    assert [column.name for column in table.primary_key.columns] == ["id"]
    assert 'sa.PrimaryKeyConstraint("id")' in _body()

    model_fks = {(column.name, fk.target_fullname) for column in table.columns for fk in column.foreign_keys}
    assert model_fks == EXPECTED_FOREIGN_KEYS
    for column in table.columns:
        for fk in column.foreign_keys:
            assert fk.ondelete is None, f"{column.name}: no ondelete -- who held access is a record"

    migration_fks = set()
    for constraint in _record("upgrade", objects_exist=False).table_constraints:
        if isinstance(constraint, sa.ForeignKeyConstraint):
            [local] = constraint.column_keys
            [element] = constraint.elements
            migration_fks.add((local, element.target_fullname))
            assert constraint.ondelete is None, f"{local}: no ondelete on the migration path either"
    assert migration_fks == EXPECTED_FOREIGN_KEYS
    assert "ondelete" not in _body()


@pytest.mark.unit
def test_created_at_is_python_side_only_on_both_paths():
    """NOT NULL with a ``datetime.utcnow`` default and NO ``server_default``, matching
    the model and ``display_tokens``. Adding ``server_default=now()`` to the migration
    only would hand the two bootstrap paths two different schemas.
    """
    migration_columns = {column.name: column for column in _record("upgrade", objects_exist=False).table_columns}
    assert migration_columns["created_at"].server_default is None
    assert _model_table().columns["created_at"].server_default is None
    assert _model_table().columns["created_at"].default is not None, "the ORM must supply the value"

    # And the nullable timestamps carry no default at all -- NULL means "never" for
    # expires_at and "not yet" for revoked_at / last_used_at, and nothing may invent one.
    for name in ("expires_at", "revoked_at", "last_used_at"):
        assert migration_columns[name].server_default is None
        assert _model_table().columns[name].server_default is None
        assert _model_table().columns[name].default is None, f"{name} must default to NULL"


@pytest.mark.unit
def test_the_table_carries_the_tenant_shape_and_no_scope_column():
    """Invariant 1, asserted on the new tenant surface this migration adds.

    ``company_id`` is the TenantMixin shape (non-null, indexed, FK to companies.id) and
    leads every composite index, so the tenant-scoped reads are index-backed. App-layer
    tenancy via ``tenant_query`` stays the enforcement -- RLS here is deny-by-default
    with zero policies.

    And the table carries NO role, scope or permission column: a token is only as
    powerful as the user it belongs to, and nothing on the row can widen that.
    """
    table = _model_table()
    company_id = table.columns["company_id"]
    assert company_id.nullable is False
    assert company_id.index is True
    assert {fk.target_fullname for fk in company_id.foreign_keys} == {"companies.id"}

    for index in table.indexes:
        columns = [column.name for column in index.columns]
        assert len(columns) == 1 or columns[0] == "company_id", f"{index.name} is not tenant-led"

    names = {column.name for column in table.columns}
    for forbidden in ("role", "scope", "scopes", "permissions", "is_superuser"):
        assert forbidden not in names, f"{forbidden!r} would let a token widen its user's power"


# ---------------------------------------------------------------------------
# 4. Index portability -- dialect-COMPILED, never executed and trusted
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_the_jti_index_compiles_as_a_full_unique_index_on_both_engines(dialect_name: str):
    """UNIQUE over ``jti`` ALONE with no predicate, on both engines. The auth path
    resolves the row by jti before it knows the company, so the handle must be unique
    across every tenant -- a ``(company_id, jti)`` uniqueness would let two tenants hold
    one revocation handle. Compiling for each dialect is the house rule for
    engine-specific claims (CLAUDE.md -> "Why the tests run on SQLite").
    """
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    [index] = [ix for ix in _model_table().indexes if ix.name == UNIQUE_INDEX]
    rendered = str(sa.schema.CreateIndex(index).compile(dialect=dialect))

    assert "CREATE UNIQUE INDEX" in rendered.upper()
    assert "WHERE" not in rendered.upper(), f"{dialect_name} rendered a PARTIAL unique index: {rendered}"
    assert [column.name for column in index.columns] == ["jti"]


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_no_other_index_becomes_unique_or_partial_on_either_engine(dialect_name: str):
    """The five read-path indexes are plain. A stray predicate on one of them would
    quietly shrink a read index; a stray UNIQUE would refuse legitimate rows -- a user
    may hold several tokens, and a company many."""
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    for index in _model_table().indexes:
        if index.name == UNIQUE_INDEX:
            continue
        rendered = str(sa.schema.CreateIndex(index).compile(dialect=dialect))
        assert "UNIQUE" not in rendered.upper(), f"{index.name} became unique"
        assert "WHERE" not in rendered.upper(), f"{index.name} became partial"


# ---------------------------------------------------------------------------
# 5. Real round-trip over a bootstrapped SQLite database
# ---------------------------------------------------------------------------


def _alembic(db_url: str, *args: str) -> None:
    """Run the alembic CLI in a subprocess pointed at the scratch DB."""
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed rc={result.returncode}\n" f"{result.stdout}\n{result.stderr}"
    )


def _fresh_engine(db_url: str):
    """A NEW engine per inspection.

    Not fussiness: the DDL is applied by a SUBPROCESS, and a pooled SQLite connection
    opened beforehand keeps a stale schema, so ``PRAGMA index_list`` comes back EMPTY for
    a table that demonstrably exists. Reusing one engine across the alembic calls makes
    this test claim the migration built no indexes at all.
    """
    return sa.create_engine(db_url)


def _has_table(db_url: str, table: str = TABLE) -> bool:
    engine = _fresh_engine(db_url)
    try:
        return sa.inspect(engine).has_table(table)
    finally:
        engine.dispose()


def _index_names(db_url: str, table: str = TABLE) -> set:
    engine = _fresh_engine(db_url)
    try:
        inspector = sa.inspect(engine)
        if not inspector.has_table(table):
            return set()
        return {index["name"] for index in inspector.get_indexes(table)}
    finally:
        engine.dispose()


def _column_shape(db_url: str, table: str = TABLE) -> list:
    engine = _fresh_engine(db_url)
    try:
        return [(column["name"], column["nullable"]) for column in sa.inspect(engine).get_columns(table)]
    finally:
        engine.dispose()


def _index_sql(db_url: str, index_name: str) -> str:
    engine = _fresh_engine(db_url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"), {"n": index_name}
            ).scalar()
    finally:
        engine.dispose()


def _bootstrap(db_url: str) -> None:
    """create_all -> stamp, exactly as production bootstraps an empty database."""
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = _fresh_engine(db_url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def _seed_reference_rows(db_url: str) -> None:
    """Two companies and one user in each -- the FK targets a token needs.

    The password hash is a placeholder string, not a credential: nothing here logs in.
    """
    engine = _fresh_engine(db_url)
    try:
        with engine.begin() as conn:
            for company_id, name, slug in ((1, "Werco", "werco"), (2, "Other Shop", "other-shop")):
                conn.execute(
                    sa.text("INSERT INTO companies (id, name, slug, is_active) VALUES (:i, :n, :s, 1)"),
                    {"i": company_id, "n": name, "s": slug},
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO users "
                        "(id, company_id, employee_id, email, hashed_password, first_name, last_name, role, is_active) "
                        "VALUES (:i, :c, :e, :m, 'not-a-hash', 'Assistant', 'Bot', 'ADMIN', 1)"
                    ),
                    {"i": company_id, "c": company_id, "e": f"BOT-{company_id}", "m": f"bot{company_id}@example.com"},
                )
    finally:
        engine.dispose()


def _insert_token(db_url: str, jti: str, *, company_id: int = 1) -> None:
    engine = _fresh_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"INSERT INTO {TABLE} "
                    "(user_id, label, jti, revoked, created_by, created_at, company_id) "
                    "VALUES (:u, 'Werco Assistant', :j, 0, :u, '2026-09-02 12:00:00', :c)"
                ),
                {"u": company_id, "j": jti, "c": company_id},
            )
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_migration_088_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig088.db"
    db_url = f"sqlite:///{db_path}"

    _bootstrap(db_url)
    # create_all must build the post-088 shape straight from the model -- table and all
    # six indexes. If this fails, the migration is the only place they exist.
    assert _has_table(db_url), "create_all did not build the api_tokens table"
    assert _index_names(db_url) == EXPECTED_INDEXES

    _alembic(db_url, "stamp", DOWN_REVISION)

    # 1. Upgrade over the bootstrapped schema: every guard fires, so this is a clean
    #    no-op rather than a "table already exists" error.
    _alembic(db_url, "upgrade", REVISION)
    assert _index_names(db_url) == EXPECTED_INDEXES

    # 2. Downgrade: a REAL drop of the six indexes and the table.
    _alembic(db_url, "downgrade", "-1")
    assert not _has_table(db_url), "downgrade left the table behind"

    # 3. Re-upgrade: the guards do NOT fire, so the genuine CREATE TABLE + six CREATE
    #    INDEX statements run. This is the only place a backfill could happen, and the
    #    table must come back EMPTY -- a migration never mints a credential.
    _alembic(db_url, "upgrade", REVISION)
    assert _has_table(db_url)
    assert _index_names(db_url) == EXPECTED_INDEXES
    assert _column_shape(db_url) == EXPECTED_COLUMNS, "the migration built a different column shape than the model"

    engine = _fresh_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar() == 0
    finally:
        engine.dispose()

    # The migration-built jti index really is a FULL unique index on SQLite.
    ddl = _index_sql(db_url, UNIQUE_INDEX)
    assert "UNIQUE" in ddl.upper()
    assert "WHERE" not in ddl.upper(), f"{UNIQUE_INDEX} came back partial: {ddl}"

    # 4. Re-runnability at the DDL level rather than via alembic's bookkeeping: stamp
    #    back and run upgrade() again over a database that already has everything.
    _alembic(db_url, "stamp", DOWN_REVISION)
    _alembic(db_url, "upgrade", REVISION)
    assert _index_names(db_url) == EXPECTED_INDEXES


@pytest.mark.integration
@pytest.mark.slow
def test_the_jti_is_unique_across_tenants_on_the_migrated_schema(tmp_path):
    """The user-visible contract of the unique index, on the schema the MIGRATION built.

    One ``jti`` per row across EVERY tenant: a second row with the same handle is
    refused even from another company, because the auth path resolves the row by jti
    before it knows which company is calling. Executed rather than reasoned about,
    because "the index is declared" and "the index is enforced" are different claims.

    The jti values below are opaque placeholders, not tokens -- no JWT is minted here.
    """
    db_path = tmp_path / "mig088_unique.db"
    db_url = f"sqlite:///{db_path}"

    _bootstrap(db_url)
    # create_all already built the post-088 shape, so reach the MIGRATION's version of
    # the table by stamping this revision, running ITS downgrade, then upgrading for
    # real. Testing create_all's index would prove nothing about the migration.
    _alembic(db_url, "stamp", REVISION)
    _alembic(db_url, "downgrade", "-1")
    assert not _has_table(db_url)
    _alembic(db_url, "upgrade", REVISION)
    _seed_reference_rows(db_url)

    _insert_token(db_url, "jti-placeholder-A")
    # Same tenant, same handle: refused.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_token(db_url, "jti-placeholder-A")
    # OTHER tenant, same handle: still refused -- the index is global, not per company.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_token(db_url, "jti-placeholder-A", company_id=2)
    # A different handle in the other tenant is fine; a user may hold several tokens.
    _insert_token(db_url, "jti-placeholder-B", company_id=2)
    _insert_token(db_url, "jti-placeholder-C")

    engine = _fresh_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar() == 3
    finally:
        engine.dispose()

    # NOT probed here: what ``revoked``'s server default lands when a raw INSERT omits
    # the column. On Postgres ``DEFAULT 'false'`` casts to boolean false; on SQLite --
    # which has no boolean type -- it stores the TEXT ``'false'``, which is truthy. That
    # is a property of the quoted spelling every table in this schema shares (the
    # mixin's, pinned above by compiling), not of this migration, and it never reaches
    # production: the ORM supplies ``default=False`` Python-side on every write.
    # Engine-specific behavior is asserted by dialect-compiling, never by executing on
    # SQLite and trusting the result (CLAUDE.md -> "Why the tests run on SQLite").


@pytest.mark.integration
@pytest.mark.slow
def test_created_at_has_no_server_default_on_the_migrated_schema(tmp_path):
    """The docstring's claim, executed: a raw INSERT that omits ``created_at`` FAILS.

    That is what "NOT NULL with a Python-side default only" means at the database, and
    it is why every writer goes through the ORM. If someone adds ``server_default=now()``
    to only one of the two bootstrap paths, this is the assertion that notices.
    """
    db_path = tmp_path / "mig088_timestamps.db"
    db_url = f"sqlite:///{db_path}"

    _bootstrap(db_url)
    _alembic(db_url, "stamp", REVISION)
    _alembic(db_url, "downgrade", "-1")
    _alembic(db_url, "upgrade", REVISION)
    _seed_reference_rows(db_url)

    engine = _fresh_engine(db_url)
    try:
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        f"INSERT INTO {TABLE} (user_id, label, jti, created_by, company_id) "
                        "VALUES (1, 'No timestamp', 'jti-placeholder-E', 1, 1)"
                    )
                )
    finally:
        engine.dispose()
