"""Coverage for 087_work_order_templates (the named-job catalog table).

087 CREATEs one new tenant-scoped, soft-deletable table, ``work_order_templates``: a
NAME, a NOTE and a POINTER at the work order whose plan is the exemplar. It alters
nothing that already exists -- in particular it does not touch ``work_orders``, which
knows nothing about the templates that name it -- and it backfills nothing, because no
template has ever existed and inventing rows over historical work orders would be
fabricating a catalog nobody curated.

The endpoint behavior lives in ``tests/api/test_work_order_templates.py``; THIS file
covers the MIGRATION, and it exists for one specific reason.

WHY THIS FILE EXISTS
--------------------
``087_work_order_templates.py`` says of its partial-index predicate: "Mirrored
byte-for-byte on the model ... a test pins the two equal." Until this file, nothing
did. ``LIVE_NAME_PREDICATE`` and ``UNIQUE_LIVE_NAME_INDEX`` were referenced nowhere
outside their two definitions, so the predicate string and the eight-index list were
DUPLICATED between ``alembic/versions/087_work_order_templates.py`` and
``app/models/work_order_template.py`` with nothing holding them equal -- exactly the
state the ``042``/``078``/``079``/``080``/``085`` lock-step convention exists to
prevent.

That convention is not bookkeeping. Bootstrap here is ``create_all`` -> ``alembic stamp
<baseline>`` -> incremental ``upgrade`` (docs/DEVELOPMENT.md), so a freshly provisioned
database builds this table from the MODEL and then stamps straight past this revision.
Anything declared only in the migration is silently absent on every fresh environment
while ``alembic_version`` claims it was applied -- the hazard that cost prod the ``008``
audit triggers (2026-07-07), ~42 read-path indexes (restored by ``079``) and ``003``'s
named FKs and CHECKs (restored by ``080``). And it cuts the other way too: a partial
unique index present on one path and absent on the other is a rule the test suite
enforces and production does not, or the reverse.

So section 3 below imports BOTH files and compares them: the predicate string, the
index list (names, column tuples, uniqueness, and which single index carries the
predicate), and the ``create_table`` column list against ``__table__.columns`` in NAME,
ORDER, type family and nullability.

Two of those comparisons look pedantic and are not:

* **Column ORDER.** ``create_all`` emits the class's own columns, then
  ``SoftDeleteMixin``'s three, then ``TenantMixin``'s ``company_id`` LAST via the MRO.
  Cosmetic to Postgres -- but a mismatch means a schema diff between a bootstrapped and
  a migrated database never comes back empty, which is how a real divergence stops being
  visible.
* **``is_deleted``'s ``server_default`` is the plain string ``"false"``, never
  ``sa.text("false")``.** Not hypothetical: commit ``e90621e`` fixed precisely that.
  SQLAlchemy quotes the string, so the two bootstrap paths emitted ``DEFAULT 'false'``
  (create_all, via the mixin) against ``DEFAULT false`` (the migration). Both renderings
  are asserted here by compiling them, so the difference is demonstrated rather than
  described.

THE PREDICATE, AND WHY IT IS SPELLED THE WAY IT IS
--------------------------------------------------
``uq_work_order_templates_company_name_live`` is UNIQUE over ``(company_id, name)``
``WHERE NOT is_deleted``: one LIVE template per name per company, and deleting one frees
its name immediately instead of burning it forever.

Section 4 pins the two properties that make that work on both engines:

1. The predicate is ``NOT is_deleted`` -- valid on Postgres AND SQLite. ``is_deleted =
   false`` is Postgres-only (SQLite had no ``false`` literal before 3.23) and
   ``is_deleted = 0`` is SQLite-only (Postgres rejects it as a type error). One string
   has to compile on both, so both wrong spellings are asserted absent.
2. It is declared for BOTH dialects (``postgresql_where`` AND ``sqlite_where``, the
   ``074``/``076`` convention). A ``postgresql_where`` alone is silently ignored by
   SQLite -- the dialect the entire pytest suite runs on -- degrading the index into a
   FULL unique one, so the tests would enforce a rule production does not: refusing to
   reuse a deleted template's name. That is the exact divergence ``076`` was written to
   fix elsewhere in this schema, and it is asserted by COMPILING the index for each
   dialect rather than by executing it and trusting the result (CLAUDE.md -> "Why the
   tests run on SQLite").

LAYERS
------
1. Script wiring (unit) -- one alembic head, the ``086`` -> ``087`` chain, an id that
   fits ``alembic_version``'s varchar(32), callable ``upgrade``/``downgrade``.
2. Source + recorded-DDL invariants (unit) -- both migration functions are RUN with
   ``op`` and the two existence guards stubbed, so what they emit is inspected directly:
   guarded creates, a real guarded downgrade, a Postgres-gated RLS statement, no data
   statement of any kind, and no table named other than this one.
3. Model / migration lock-step (unit) -- the reason this file exists.
4. Predicate portability (unit) -- dialect-compiled, both engines.
5. A real upgrade -> downgrade -> upgrade round-trip over a disposable SQLite file
   (integration/slow), ending in the BEHAVIORAL contract of the partial index on the
   schema the MIGRATION built: a duplicate live name is refused, a soft-deleted row
   frees its name for reuse, and two tombstones may share a name.
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

REVISION = "087_work_order_templates"
MIGRATION_FILE = "087_work_order_templates.py"
DOWN_REVISION = "086_part_active_before_delete"

TABLE = "work_order_templates"
UNIQUE_INDEX = "uq_work_order_templates_company_name_live"
PREDICATE = "NOT is_deleted"

# (column, nullable) in create_all's emission ORDER: the class's own columns, then
# SoftDeleteMixin's three, then TenantMixin's company_id LAST via the MRO.
EXPECTED_COLUMNS = [
    ("id", False),
    ("name", False),
    ("notes", True),
    ("source_work_order_id", False),
    ("default_quantity", True),
    ("created_at", False),
    ("updated_at", False),
    ("created_by", True),
    ("is_deleted", False),
    ("deleted_at", True),
    ("deleted_by", True),
    ("company_id", False),
]

# Every index both paths must build. The first five come from ``index=True`` on the
# columns themselves (is_deleted via SoftDeleteMixin, company_id via TenantMixin); the
# last three are the model's explicit ``__table_args__``.
EXPECTED_INDEXES = {
    "ix_work_order_templates_id",
    "ix_work_order_templates_name",
    "ix_work_order_templates_source_work_order_id",
    "ix_work_order_templates_is_deleted",
    "ix_work_order_templates_company_id",
    "ix_work_order_templates_company_live",
    "ix_work_order_templates_company_source",
    UNIQUE_INDEX,
}

# (local column, target) -- ``deleted_by`` deliberately carries NO foreign key; that is
# SoftDeleteMixin's own shape and is not "corrected" on either path.
EXPECTED_FOREIGN_KEYS = {
    ("source_work_order_id", "work_orders.id"),
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
    spec = importlib.util.spec_from_file_location("_migtest_087", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped.

    The prose names at length every construct the migration deliberately avoids (no
    backfill, no ``UniqueConstraint``, no second copy service) and quotes the audit_log
    by name, so assertions about ABSENT constructs have to look at CODE only or the
    docstring itself would fail them.
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
    EMIT (including the kwargs on each ``create_index``, which is where the partial
    predicate lives) and it can be pointed at either dialect without a Postgres server.
    """

    def __init__(self, dialect_name: str):
        self.dialect_name = dialect_name
        self.calls: list = []

    # -- the slice of the ``op`` surface 087 uses ---------------------------
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
        """name -> (column tuple, unique, predicate text or None)."""
        out = {}
        for call in self.calls:
            if call[0] != "create_index":
                continue
            _kind, name, table_name, columns, kwargs = call
            assert table_name == TABLE, f"{name} was built on {table_name}"
            pg_where = kwargs.get("postgresql_where")
            sqlite_where = kwargs.get("sqlite_where")
            assert (pg_where is None) == (sqlite_where is None), f"{name}: predicate declared for one dialect only"
            if pg_where is not None:
                assert str(pg_where) == str(sqlite_where), f"{name}: the two dialects got different predicates"
            out[name] = (tuple(columns), bool(kwargs.get("unique", False)), None if pg_where is None else str(pg_where))
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
    from app.models.work_order_template import WorkOrderTemplate

    return WorkOrderTemplate.__table__


def _model_indexes() -> dict:
    """name -> (column tuple, unique, predicate text or None), read off the model."""
    out = {}
    for index in _model_table().indexes:
        pg_where = index.dialect_options["postgresql"].get("where")
        sqlite_where = index.dialect_options["sqlite"].get("where")
        assert (pg_where is None) == (
            sqlite_where is None
        ), f"{index.name}: predicate declared for one dialect only -- a postgresql_where alone degrades on SQLite"
        if pg_where is not None:
            assert str(pg_where) == str(sqlite_where), f"{index.name}: the two dialects got different predicates"
        out[index.name] = (
            tuple(column.name for column in index.columns),
            bool(index.unique),
            None if pg_where is None else str(pg_where),
        )
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
    """One head, and 087 hangs off 086 (the part restore-sidecar column).

    A second head is not cosmetic: ``upgrade head`` fails outright, so a branched chain
    is a deploy that cannot run.

    Deliberately does NOT assert that 087 IS the head. That assertion is wrong by
    construction -- it fails for whoever writes the NEXT migration no matter what they
    write, which is how 086's version of this test broke when 087 arrived. The two
    things worth pinning survive: exactly one head, and 087's own parent.
    """
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert REVISION in chain, f"087 is not an ancestor of head {heads[0]}"
    assert DOWN_REVISION in chain, "087 must sit above 086 on the same path"


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
    """Order matters: the table, then all eight indexes, then RLS."""
    recorder = _record("upgrade", objects_exist=False)
    assert recorder.names("create_table") == [TABLE]
    assert set(recorder.names("create_index")) == EXPECTED_INDEXES
    assert recorder.kinds[0] == "create_table", "indexes cannot precede the table"
    assert recorder.kinds.count("create_table") == 1


@pytest.mark.unit
def test_the_bootstrapped_database_is_a_clean_no_op():
    """THE idempotency claim, executed rather than grepped.

    On the ``create_all`` -> ``stamp`` -> ``upgrade`` path the model has already built
    the table and all eight indexes, and a re-run over a partially applied upgrade is a
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
    061/084/085). The repo-wide gate in test_migration_059_060_supabase_hardening.py
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
    """Nothing to backfill: no template has ever existed, and manufacturing rows over
    historical work orders would invent a catalog nobody curated (the same reasoning as
    085's empty combine table and 084's empty alias table).

    Being DDL-only is also what makes the migration structurally incapable of touching
    the tamper-evident ``audit_log`` hash chain (invariant 2).
    """
    body = _body()
    for forbidden in ("op.bulk_insert", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in body, f"087 must run no data statement; found {forbidden!r}"

    # The only statement either direction executes is the RLS one.
    for recorder in (_record("upgrade", objects_exist=False), _record("downgrade", objects_exist=True)):
        for statement in recorder.executed:
            assert "ROW LEVEL SECURITY" in statement, f"unexpected op.execute: {statement}"


@pytest.mark.unit
def test_no_existing_table_is_touched():
    """087 is purely additive. It creates no enum, alters no enum, adds no column
    anywhere else and -- in particular -- does not touch ``work_orders``: a template
    names a work order, the work order knows nothing about it.
    """
    body = _body()
    for forbidden in ("op.add_column", "op.drop_column", "op.alter_column", "op.batch_alter_table", "sa.Enum("):
        assert forbidden not in body, f"087 must not {forbidden} -- it only creates a new table"

    for recorder in (_record("upgrade", objects_exist=False), _record("downgrade", objects_exist=True)):
        for name in recorder.names("create_table") + recorder.names("drop_table"):
            assert name == TABLE
        for statement in recorder.executed:
            assert TABLE in statement and "work_orders" not in statement.replace(TABLE, "")


# ---------------------------------------------------------------------------
# 3. Model / migration LOCK-STEP -- the reason this file exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_predicate_string_is_shared_byte_for_byte():
    """The migration's docstring claims "a test pins the two equal". This is that test.

    Both files declare the predicate as a module constant precisely so it can be
    compared; before this file, neither constant was referenced anywhere outside its own
    definition, and two copies of a string with nothing holding them equal is how a
    partial index ends up partial on one bootstrap path and full on the other.
    """
    from app.models.work_order_template import LIVE_NAME_PREDICATE, UNIQUE_LIVE_NAME_INDEX

    module = _load_module()
    assert module.LIVE_NAME_PREDICATE == LIVE_NAME_PREDICATE
    assert module.LIVE_NAME_PREDICATE == PREDICATE
    assert UNIQUE_LIVE_NAME_INDEX == UNIQUE_INDEX
    # …and the migration really uses the constant for the index it names.
    assert module.INDEXES[-1] == (UNIQUE_INDEX, ["company_id", "name"], True, LIVE_NAME_PREDICATE)


@pytest.mark.unit
def test_every_index_matches_the_model_exactly():
    """Same names, same column tuples, same uniqueness, same single partial predicate.

    An index declared only in the migration is skipped entirely by the ``create_all`` +
    ``stamp`` bootstrap (how prod lost ~42 read-path indexes, restored by 079); one
    declared only on the model never reaches an already-provisioned database. Either way
    the two environments stop being the same schema.
    """
    module = _load_module()
    migration = {
        name: (tuple(columns), bool(unique), predicate) for name, columns, unique, predicate in module.INDEXES
    }
    assert set(migration) == EXPECTED_INDEXES
    assert len(module.INDEXES) == len(EXPECTED_INDEXES), "duplicate entry in the migration's INDEXES list"
    assert migration == _model_indexes()

    # Uniqueness and the predicate belong to exactly one index, on both sides.
    assert {name for name, (_c, unique, _p) in migration.items() if unique} == {UNIQUE_INDEX}
    assert {name for name, (_c, _u, predicate) in migration.items() if predicate is not None} == {UNIQUE_INDEX}

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

    ``create_all`` emits the class's own columns, then ``SoftDeleteMixin``'s three, then
    ``TenantMixin``'s ``company_id`` last via the MRO. If the migration emits a different
    order, a schema diff between a bootstrapped and a migrated database never comes back
    empty -- and a diff that is never empty is a diff nobody reads.
    """
    migration_columns = _record("upgrade", objects_exist=False).table_columns
    model_columns = list(_model_table().columns)

    assert [column.name for column in migration_columns] == [name for name, _ in EXPECTED_COLUMNS]
    assert [column.name for column in model_columns] == [name for name, _ in EXPECTED_COLUMNS]
    assert [_fingerprint(column) for column in migration_columns] == [_fingerprint(column) for column in model_columns]
    for column, (name, nullable) in zip(migration_columns, EXPECTED_COLUMNS):
        assert column.name == name and column.nullable is nullable, f"{name} nullability drifted"

    # The mixin block sits between the class's own columns and company_id.
    names = [name for name, _ in EXPECTED_COLUMNS]
    assert names[-4:] == ["is_deleted", "deleted_at", "deleted_by", "company_id"]


@pytest.mark.unit
def test_is_deleted_carries_the_plain_string_server_default_not_sa_text():
    """``server_default="false"``, never ``sa.text("false")``. Commit e90621e fixed
    exactly this, so it is pinned rather than trusted.

    SQLAlchemy QUOTES a plain string and inlines a ``text()`` verbatim, so the two spell
    different DDL: ``DEFAULT 'false'`` (what the mixin renders, i.e. what ``create_all``
    builds) against ``DEFAULT false``. Both renderings are compiled below so the
    difference is demonstrated, not asserted from memory -- and it is demonstrated on
    BOTH dialects, since this is the one column whose default text differs between the
    bootstrap paths.
    """
    from sqlalchemy.dialects import postgresql, sqlite

    migration_column = {column.name: column for column in _record("upgrade", objects_exist=False).table_columns}[
        "is_deleted"
    ]
    model_column = _model_table().columns["is_deleted"]

    for column in (migration_column, model_column):
        assert column.server_default is not None
        assert isinstance(column.server_default.arg, str), "sa.text() renders DEFAULT false, the mixin DEFAULT 'false'"
        assert column.server_default.arg == "false"
    assert 'server_default="false"' in _body()
    assert 'server_default=sa.text("false")' not in _body()

    # The divergence itself, compiled: quoted vs bare, on both engines.
    quoted = sa.Table(
        "_quoted", sa.MetaData(), sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false")
    )
    bare = sa.Table(
        "_bare", sa.MetaData(), sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        quoted_ddl = str(sa.schema.CreateColumn(quoted.c.is_deleted).compile(dialect=dialect))
        bare_ddl = str(sa.schema.CreateColumn(bare.c.is_deleted).compile(dialect=dialect))
        assert "DEFAULT 'false'" in quoted_ddl
        assert "DEFAULT false" in bare_ddl
        assert quoted_ddl != bare_ddl, "if these ever converge, this test is protecting nothing"
        # …and the real column renders the quoted form on both paths.
        assert "DEFAULT 'false'" in str(sa.schema.CreateColumn(model_column).compile(dialect=dialect))


@pytest.mark.unit
def test_the_primary_key_and_foreign_keys_match_the_model():
    """``deleted_by`` is a bare Integer with NO foreign key on either path -- that is
    SoftDeleteMixin's own shape, deliberately not made symmetric with ``created_by``.
    Asserted so nobody "fixes" it on one side only.
    """
    table = _model_table()
    assert [column.name for column in table.primary_key.columns] == ["id"]
    assert 'sa.PrimaryKeyConstraint("id")' in _body()

    model_fks = {(column.name, fk.target_fullname) for column in table.columns for fk in column.foreign_keys}
    assert model_fks == EXPECTED_FOREIGN_KEYS

    migration_fks = set()
    for constraint in _record("upgrade", objects_exist=False).table_constraints:
        if isinstance(constraint, sa.ForeignKeyConstraint):
            [local] = constraint.column_keys
            [element] = constraint.elements
            migration_fks.add((local, element.target_fullname))
    assert migration_fks == EXPECTED_FOREIGN_KEYS
    assert not table.columns["deleted_by"].foreign_keys, "deleted_by must stay FK-less (the mixin's shape)"


@pytest.mark.unit
def test_the_timestamps_are_python_side_only_on_both_paths():
    """NOT NULL with ``datetime.utcnow`` defaults and NO ``server_default``, matching the
    model and ``WorkOrder`` itself. Adding ``server_default=now()`` to the migration only
    would hand the two bootstrap paths two different schemas -- the exact drift this
    convention exists to prevent.
    """
    migration_columns = {column.name: column for column in _record("upgrade", objects_exist=False).table_columns}
    for name in ("created_at", "updated_at"):
        assert migration_columns[name].server_default is None
        assert _model_table().columns[name].server_default is None
        assert _model_table().columns[name].default is not None, "the ORM must supply the value"


@pytest.mark.unit
def test_the_table_carries_the_tenant_and_soft_delete_shapes():
    """Invariants 1 and 3, asserted on the new tenant surface this migration adds.

    ``company_id`` is the TenantMixin shape (non-null, indexed, FK to companies.id) and
    leads every composite index, so the tenant-scoped reads are index-backed. App-layer
    tenancy via ``tenant_query`` stays the enforcement -- RLS here is deny-by-default
    with zero policies.
    """
    table = _model_table()
    company_id = table.columns["company_id"]
    assert company_id.nullable is False
    assert company_id.index is True
    assert {fk.target_fullname for fk in company_id.foreign_keys} == {"companies.id"}

    for index in table.indexes:
        columns = [column.name for column in index.columns]
        assert len(columns) == 1 or columns[0] == "company_id", f"{index.name} is not tenant-led"

    for tombstone in ("is_deleted", "deleted_at", "deleted_by"):
        assert tombstone in table.columns, f"{tombstone} is missing -- the partial predicate depends on it"
    assert table.columns["is_deleted"].nullable is False, "a NULL is_deleted would fall out of the partial index"


# ---------------------------------------------------------------------------
# 4. Predicate portability -- dialect-COMPILED, never executed and trusted
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_predicate_is_spelled_the_one_way_that_compiles_on_both_engines():
    """``NOT is_deleted`` -- not ``is_deleted = false`` (Postgres-only; SQLite had no
    ``false`` literal before 3.23) and not ``is_deleted = 0`` (SQLite-only; Postgres
    rejects it as a type error). ONE string has to serve both dialects.
    """
    from app.models.work_order_template import LIVE_NAME_PREDICATE

    assert LIVE_NAME_PREDICATE == "NOT is_deleted"
    normalized = " ".join(LIVE_NAME_PREDICATE.lower().split())
    assert "is_deleted = false" not in normalized, "Postgres-only spelling"
    assert "is_deleted = 0" not in normalized, "SQLite-only spelling"
    assert "=" not in LIVE_NAME_PREDICATE


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_the_unique_index_compiles_as_a_PARTIAL_unique_index_on_both_engines(dialect_name: str):
    """THE portability assertion, and the whole reason ``sqlite_where`` is declared.

    A ``postgresql_where`` alone is silently IGNORED by SQLite, degrading this into a
    FULL unique index -- so the suite (which runs on SQLite) would enforce a rule
    production does not: refusing to reuse a deleted template's name. That is exactly the
    divergence 076 was written to fix elsewhere in this schema. Compiling for each
    dialect is the house rule for engine-specific claims (CLAUDE.md -> "Why the tests run
    on SQLite").
    """
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    [index] = [ix for ix in _model_table().indexes if ix.name == UNIQUE_INDEX]
    rendered = str(sa.schema.CreateIndex(index).compile(dialect=dialect))

    assert "CREATE UNIQUE INDEX" in rendered.upper()
    assert f"WHERE {PREDICATE}" in rendered, f"{dialect_name} rendered a FULL unique index: {rendered}"
    assert [column.name for column in index.columns] == ["company_id", "name"]


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_no_other_index_becomes_unique_or_partial_on_either_engine(dialect_name: str):
    """The seven read-path indexes are plain. A stray predicate on one of them would
    quietly shrink a read index; a stray UNIQUE would refuse legitimate rows."""
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    for index in _model_table().indexes:
        if index.name == UNIQUE_INDEX:
            continue
        rendered = str(sa.schema.CreateIndex(index).compile(dialect=dialect))
        assert "UNIQUE" not in rendered.upper(), f"{index.name} became unique"
        assert "WHERE" not in rendered.upper(), f"{index.name} became partial"


@pytest.mark.unit
def test_the_uniqueness_is_an_index_not_a_table_constraint():
    """A ``UniqueConstraint`` cannot carry a predicate, so it would burn a deleted
    template's name forever and force the planner to invent "Miratech nest group 2"."""
    table = _model_table()
    assert not [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    assert "sa.UniqueConstraint(" not in _body()


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
    """Two companies, a part and a work order -- the FK targets a template needs."""
    engine = _fresh_engine(db_url)
    try:
        with engine.begin() as conn:
            for company_id, name, slug in ((1, "Werco", "werco"), (2, "Other Shop", "other-shop")):
                conn.execute(
                    sa.text("INSERT INTO companies (id, name, slug, is_active) VALUES (:i, :n, :s, 1)"),
                    {"i": company_id, "n": name, "s": slug},
                )
            # A production WO needs a part (ck_work_orders_part_required_unless_laser).
            conn.execute(
                sa.text(
                    "INSERT INTO parts (id, part_number, name, part_type, unit_of_measure, company_id) "
                    "VALUES (1, 'MIRA-HOUSING', 'Miratech housing', 'manufactured', 'each', 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO work_orders "
                    "(id, work_order_number, work_order_type, part_id, quantity_ordered, status, company_id, version) "
                    "VALUES (1, 'WO-087-SRC', 'production', 1, 3, 'released', 1, 1)"
                )
            )
    finally:
        engine.dispose()


def _insert_template(db_url: str, name: str, *, company_id: int = 1, is_deleted: int = 0) -> None:
    engine = _fresh_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"INSERT INTO {TABLE} "
                    "(name, source_work_order_id, created_at, updated_at, is_deleted, company_id) "
                    "VALUES (:n, 1, '2026-08-25 12:00:00', '2026-08-25 12:00:00', :d, :c)"
                ),
                {"n": name, "d": is_deleted, "c": company_id},
            )
    finally:
        engine.dispose()


def _soft_delete_templates(db_url: str, name: str, company_id: int = 1) -> None:
    engine = _fresh_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE {TABLE} SET is_deleted = 1 WHERE name = :n AND company_id = :c AND is_deleted = 0"),
                {"n": name, "c": company_id},
            )
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_migration_087_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig087.db"
    db_url = f"sqlite:///{db_path}"

    _bootstrap(db_url)
    # create_all must build the post-087 shape straight from the model -- table and all
    # eight indexes. If this fails, the migration is the only place they exist.
    assert _has_table(db_url), "create_all did not build the templates table"
    assert _index_names(db_url) == EXPECTED_INDEXES

    _alembic(db_url, "stamp", DOWN_REVISION)

    # 1. Upgrade over the bootstrapped schema: every guard fires, so this is a clean
    #    no-op rather than a "table already exists" error.
    _alembic(db_url, "upgrade", REVISION)
    assert _index_names(db_url) == EXPECTED_INDEXES

    # 2. Downgrade: a REAL drop of the eight indexes and the table.
    _alembic(db_url, "downgrade", "-1")
    assert not _has_table(db_url), "downgrade left the table behind"

    # 3. Re-upgrade: the guards do NOT fire, so the genuine CREATE TABLE + eight CREATE
    #    INDEX statements run. This is the only place a backfill could happen, and the
    #    table must come back EMPTY.
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

    # The migration-built unique index really is PARTIAL on SQLite (a full one would
    # refuse to reuse a deleted template's name).
    ddl = _index_sql(db_url, UNIQUE_INDEX)
    assert "UNIQUE" in ddl.upper()
    assert f"WHERE {PREDICATE}" in ddl, f"{UNIQUE_INDEX} came back as a FULL unique index: {ddl}"

    # 4. Re-runnability at the DDL level rather than via alembic's bookkeeping: stamp
    #    back and run upgrade() again over a database that already has everything.
    _alembic(db_url, "stamp", DOWN_REVISION)
    _alembic(db_url, "upgrade", REVISION)
    assert _index_names(db_url) == EXPECTED_INDEXES


@pytest.mark.integration
@pytest.mark.slow
def test_the_partial_unique_index_frees_a_deleted_templates_name(tmp_path):
    """The user-visible contract of the partial index, on the schema the MIGRATION built.

    Three claims, and the third is the one a full unique index would break:

    1. Two LIVE templates in one company may not share a name.
    2. Soft-deleting one FREES the name immediately -- the planner renames nothing and
       does not have to invent "Miratech nest group 2".
    3. Any number of TOMBSTONES may share a name, so deleting the replacement later
       cannot fail either.

    Executed rather than reasoned about, because "the predicate is declared" and "the
    predicate is enforced" are different claims.
    """
    db_path = tmp_path / "mig087_partial.db"
    db_url = f"sqlite:///{db_path}"
    name = "Miratech nest group"

    _bootstrap(db_url)
    # create_all already built the post-087 shape, so reach the MIGRATION's version of
    # the table by stamping this revision, running ITS downgrade, then upgrading for
    # real. Testing create_all's index would prove nothing about the migration.
    _alembic(db_url, "stamp", REVISION)
    _alembic(db_url, "downgrade", "-1")
    assert not _has_table(db_url)
    _alembic(db_url, "upgrade", REVISION)
    _seed_reference_rows(db_url)

    # 1. One live template per name per company.
    _insert_template(db_url, name)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_template(db_url, name)

    # …scoped to the company: another tenant may use the same name (invariant 1).
    _insert_template(db_url, name, company_id=2)

    # 2. Deleting it frees the name at once.
    _soft_delete_templates(db_url, name)
    _insert_template(db_url, name)

    # 3. Tombstones may pile up under one name.
    _soft_delete_templates(db_url, name)
    _insert_template(db_url, name, is_deleted=1)

    engine = _fresh_engine(db_url)
    try:
        with engine.connect() as conn:
            live, deleted = conn.execute(
                sa.text(
                    f"SELECT sum(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END), "
                    f"sum(CASE WHEN is_deleted = 1 THEN 1 ELSE 0 END) FROM {TABLE} WHERE name = :n"
                ),
                {"n": name},
            ).one()
    finally:
        engine.dispose()
    assert (live, deleted) == (1, 3), "expected one live template (company 2) and three tombstones"


@pytest.mark.integration
@pytest.mark.slow
def test_the_timestamps_have_no_server_default_on_the_migrated_schema(tmp_path):
    """The docstring's claim, executed: a raw INSERT that omits ``created_at`` FAILS.

    That is what "NOT NULL with Python-side defaults only" means at the database, and it
    is why every writer goes through the ORM. If someone adds ``server_default=now()`` to
    only one of the two bootstrap paths, this is the assertion that notices.
    """
    db_path = tmp_path / "mig087_timestamps.db"
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
                        f"INSERT INTO {TABLE} (name, source_work_order_id, is_deleted, company_id) "
                        "VALUES ('No timestamps', 1, 0, 1)"
                    )
                )
    finally:
        engine.dispose()
