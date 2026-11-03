"""The fresh-database bootstrap path, run end to end.

An empty database is built here as ``create_all`` -> ``alembic stamp 058_process_sheets``
-> ``alembic upgrade head`` -- by a new Supabase project in production and by
``python -m scripts.reset_dev_db`` locally (CLAUDE.md -> "Migrations -- handle with care";
docs/DEVELOPMENT.md -> Database Migrations). That path has two hazards. The first is the
STAMPED-OVER revision -- DDL living only in a migration below the stamp, silently absent
while ``alembic_version`` claims it ran -- and ``tests/test_migration_088_api_tokens.py``
explains it and holds one revision's model and migration in lock-step against it. This file
covers the second: every revision ABOVE the stamp really does run, over a schema the
models have already built, so each one must guard every object it creates and no-op
wherever ``create_all`` got there first.

WHY THIS FILE EXISTS
--------------------
Nothing ran the chain. Every per-revision test exercises ONE revision in isolation --
stamp its parent, upgrade one step, or render its Postgres DDL offline -- so when
``089``-``098``, ``106`` and ``107`` shipped without per-object guards, the path died at
``089`` (on SQLite, a ``CircularDependencyError`` inside ``batch_alter_table("quotes")``)
and stayed dead, ``reset_dev_db`` included, with the whole suite green. Production was
unaffected only because it migrated incrementally from ``088``.

WHAT IT ASSERTS
---------------
1. The chain completes, and ``alembic_version`` holds the script directory's single head.
   The head is derived, never hard-coded, so the next migration is covered with no edit
   here.
2. The result IS a pure ``create_all`` schema, object for object. Per table: columns
   (type, nullability, server default; order ignored), primary key, indexes, unique
   constraints, foreign keys, CHECK constraints and triggers. A migration that adds
   something the models don't declare, drops something they do, or reshapes a column
   fails here, and the message lists every difference.
3. Re-running is a no-op: re-stamp ``058``, upgrade again over the result, and nothing may
   move. That is the guards' whole contract.
4. Every revision id the path WRITES fits ``alembic_version.version_num`` (a unit check;
   see its test for why the chain run cannot catch this one).
5. No ``ENABLE``/``FORCE ROW LEVEL SECURITY`` or ``REVOKE`` reachable from ``upgrade()`` in
   any revision the path runs sits under a TABLE-existence guard (a static AST check; see
   ``_table_guarded_security``). Every non-production boot (``app/main.py``) and
   ``scripts/seed_data.py`` run ``create_all`` ahead of the migrations, so a table can
   already exist, RLS-off, when its migration runs: a guarded ``ENABLE ROW LEVEL SECURITY``
   would then never fire and the table would stay open on a staging or dev Supabase.

WHAT IT CANNOT SEE
------------------
The suite runs on SQLite by design (CLAUDE.md -> "Why the tests run on SQLite"), so the
Postgres-only statements the migrations gate on dialect -- ``ENABLE ROW LEVEL SECURITY``,
``REVOKE``, trigger functions -- never EXECUTE here. Assertion 5 checks where they sit, not
what they do: it says nothing about whether they are idempotent or correct on Postgres
(the ``scripts/verify_*_postgres.py`` runs in e2e.yml are the only place they execute), it
follows only calls and names it can resolve statically within one module, it treats nested
functions lexically, and by design it flags table-existence guards only -- role, trigger,
dialect and index/column guards pass. Nor does this file cover the INCREMENTAL path: a
guard that no-ops on a ``create_all`` schema is exactly what lets a migration's own DDL
disagree with its model on an upgraded database unnoticed, which is what the per-revision
lock-step tests are for.

HOW IT RUNS
-----------
Every ``create_all`` and alembic command runs in a SUBPROCESS, with ``DATABASE_URL``
pointed at a file under ``tmp_path``. ``alembic/env.py`` takes its URL from ``settings``,
and ``tests/conftest.py`` has already pointed this process's settings at the suite's
shared-cache in-memory database, which every other test on this worker depends on -- so
nothing here may build the app engine in-process. Inspection opens a throwaway engine on
the temp file only, and ``tmp_path`` keeps xdist workers apart.

The whole scenario is ONE test on purpose. Under xdist's default ``load`` scheduling the
tests of a module are dealt round-robin across workers, so a module-scoped fixture shared
by three tests would build the chain up to three times.
"""

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import warnings
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parent.parent

# The documented baseline. NOT a later one: 059 (RLS / REVOKE) and 060 (the audit-log
# immutability triggers) create what create_all cannot, so they must actually run.
STAMP_BASELINE = "058_process_sheets"

# ``alembic stamp`` creates ``alembic_version`` with ``version_num`` as ``String(32)``
# (alembic/ddl/impl.py). ``014b_widen_alembic_version`` widens it, but sits below the stamp.
VERSION_NUM_MAX_LENGTH = 32

SUBPROCESS_TIMEOUT_SECONDS = 600

# The create_all step of scripts/reset_dev_db.py, verbatim -- the app engine, built from
# settings -- plus a report of which tables the metadata declared.
CREATE_ALL_SCRIPT = (
    "import json\n"
    "from app.db.database import Base, engine\n"
    "import app.models  # noqa: F401 -- registers every model on Base.metadata\n"
    "Base.metadata.create_all(bind=engine)\n"
    "print(json.dumps(sorted(Base.metadata.tables)))\n"
)

_RUNNING_UPGRADE = re.compile(r"Running upgrade ([^\s,]*) -> ([^\s,]+)")


# --------------------------------------------------------------------------- helpers


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _single_head() -> str:
    heads = _script_directory().get_heads()
    assert len(heads) == 1, f"expected exactly one alembic head, found {sorted(heads)}"
    return heads[0]


def _run(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _describe_failure(label: str, result: subprocess.CompletedProcess) -> str:
    """Name the revision that was running when the subprocess died, then the log tail."""
    lines = [f"{label} failed (exit {result.returncode})"]
    running = _RUNNING_UPGRADE.findall(result.stderr)
    if running:
        source, target = running[-1]
        lines.append(f"last revision started: {source or '<base>'} -> {target}")
    lines += ["--- stderr (tail) ---", *result.stderr.splitlines()[-60:]]
    lines += ["--- stdout (tail) ---", *result.stdout.splitlines()[-20:]]
    return "\n".join(lines)


def _create_all(db_path: Path) -> set:
    """``create_all`` on ``db_path``; returns the table names the metadata declared."""
    result = _run(db_path, "-c", CREATE_ALL_SCRIPT)
    assert result.returncode == 0, _describe_failure("create_all", result)
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(db_path, "-m", "alembic", *args)


def _version_rows(db_path: Path) -> list:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        return [row[0] for row in con.execute("SELECT version_num FROM alembic_version")]


def _ws(value) -> str:
    """Collapse whitespace only. No case-folding: ``'OPEN'`` and ``'open'`` in a CHECK or
    a partial-index predicate are different constraints."""
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _schema(db_path: Path) -> dict:
    """Every schema object in ``db_path``, as ``{table: {hashable description, ...}}``.

    Indexes are described by the DDL SQLite stored for them (``sqlite_master.sql``), not by
    the inspector: SQLAlchemy cannot reflect an expression index on SQLite -- it warns
    ("Skipped unsupported reflection of expression-based index") and OMITS it, which is how
    ``uq_oee_company_wc_date_shift`` would drop out of the comparison unseen -- and the
    stored DDL also carries what the column list alone would not: UNIQUE, expressions and
    any partial ``WHERE`` predicate. Implicit autoindexes (``sql IS NULL``) back a PRIMARY
    KEY or UNIQUE constraint, both compared structurally below; their ``_N`` suffixes follow
    constraint order inside CREATE TABLE, which is not a schema property.
    """
    schema: dict = {}
    engine = sa.create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Skipped unsupported reflection of expression-based index",
                category=sa.exc.SAWarning,
            )
            inspector = sa.inspect(engine)
            for table in inspector.get_table_names():
                if table == "alembic_version":
                    continue
                objects = set()
                for column in inspector.get_columns(table):
                    objects.add(
                        (
                            "column",
                            column["name"],
                            _ws(column["type"]).lower(),
                            column["nullable"],
                            _ws(column.get("default")),
                        )
                    )
                objects.add(("primary_key", tuple(inspector.get_pk_constraint(table)["constrained_columns"])))
                for unique in inspector.get_unique_constraints(table):
                    objects.add(("unique", unique["name"], tuple(unique["column_names"])))
                for fk in inspector.get_foreign_keys(table):
                    objects.add(
                        (
                            "foreign_key",
                            fk["name"],
                            tuple(fk["constrained_columns"]),
                            fk["referred_table"],
                            tuple(fk["referred_columns"]),
                            tuple(sorted((fk.get("options") or {}).items())),
                        )
                    )
                for check in inspector.get_check_constraints(table):
                    objects.add(("check", check["name"], _ws(check["sqltext"])))
                schema[table] = objects
    finally:
        engine.dispose()

    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        stored = con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('index', 'trigger', 'view') AND sql IS NOT NULL"
        )
        for kind, name, table, sql in stored:
            if table != "alembic_version":
                schema.setdefault(table, set()).add((kind, name, _ws(sql)))
    return schema


def _schema_diff(actual: dict, expected: dict, actual_label: str, expected_label: str) -> list:
    lines = []
    for table in sorted(set(actual) | set(expected)):
        if table not in expected:
            lines.append(f"table {table!r} exists only in {actual_label}")
        elif table not in actual:
            lines.append(f"table {table!r} exists only in {expected_label}")
        else:
            for obj in sorted(actual[table] - expected[table], key=repr):
                lines.append(f"{table}: only in {actual_label}: {obj}")
            for obj in sorted(expected[table] - actual[table], key=repr):
                lines.append(f"{table}: only in {expected_label}: {obj}")
    return lines


def _format_diff(title: str, lines: list) -> str:
    return f"{title} -- {len(lines)} difference(s):\n  " + "\n  ".join(lines)


def _overlong(revision_ids, limit: int = VERSION_NUM_MAX_LENGTH) -> list:
    return [(revision_id, len(revision_id)) for revision_id in revision_ids if len(revision_id) > limit]


def _revision_ids_written_on_the_fresh_path() -> list:
    """The stamp writes ``058``; each upgrade step then writes its own id. Head first."""
    script = _script_directory()
    return [rev.revision for rev in script.walk_revisions(base=STAMP_BASELINE, head="heads")]


def _scripts_run_on_the_fresh_path() -> list:
    """Every revision ``upgrade head`` executes after the stamp (``059`` on). Head first."""
    script = _script_directory()
    return [rev for rev in script.walk_revisions(base=STAMP_BASELINE, head="heads") if rev.revision != STAMP_BASELINE]


# ------------------------------------------------- static check: RLS / REVOKE placement

_SECURITY_SQL = re.compile(r"\b(?:ENABLE|FORCE)\s+ROW\s+LEVEL\s+SECURITY\b|\bREVOKE\b", re.IGNORECASE)

# An identifier that IS a table-existence check: has_table / _has_table / inspector.has_table,
# table_exists / _table_exists, get_table_names. Deliberately not ``_exists`` alone -- that is
# what 059's ``_role_exists`` and 060's ``_trigger_exists`` end in, and both are correct.
_TABLE_CHECK_IDENTIFIER = re.compile(r"has_tables?|tables?_exists?|get_table_names")

_EXITS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _is_table_check(expr: ast.AST, predicates: set) -> bool:
    """Does evaluating ``expr`` ask whether a table exists?

    ``predicates`` holds the module's own helpers that RETURN such a check -- 102-105 guard on
    ``_exists(table)``, whose body is ``...has_table(table)``, and a name-only match would let
    that shape through.
    """
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and (node.id in predicates or _TABLE_CHECK_IDENTIFIER.search(node.id)):
            return True
        if isinstance(node, ast.Attribute) and _TABLE_CHECK_IDENTIFIER.search(node.attr):
            return True
    return False


def _security_text(node: ast.AST):
    """The SQL text of a str literal or f-string that issues RLS / REVOKE, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        text = "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
    else:
        return None
    return text if _SECURITY_SQL.search(text) else None


class _GuardScan:
    """Walk one function body, recording every security USE and the table guard over it.

    A use is a security literal, or a load of a name that carries one: a module-level
    function or constant, or a local bound from either. A table guard is any construct that
    makes the code below it run only for one answer to "does the table exist": both branches
    of an ``if`` / conditional expression on such a test, the operands after one in ``and`` /
    ``or``, a comprehension filtered on one, and everything after an ``if`` on one whose
    branch returns, raises, ``continue``s or ``break``s. Bare string statements (docstrings)
    never execute and are skipped, so prose that mentions REVOKE cannot trip the check.
    """

    def __init__(self, predicates: set, bearing: set, local_checks: set):
        self.predicates = predicates
        self.bearing = bearing
        self.local_checks = local_checks
        self.uses = []  # (line, description, guard or None, referenced name or None)

    def _is_guard(self, expr: ast.AST) -> bool:
        return _is_table_check(expr, self.predicates | self.local_checks)

    def block(self, statements: list, guard) -> None:
        for statement in statements:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                continue
            self.node(statement, guard)
            if (
                guard is None
                and isinstance(statement, ast.If)
                and self._is_guard(statement.test)
                and any(branch and isinstance(branch[-1], _EXITS) for branch in (statement.body, statement.orelse))
            ):
                guard = f"the early exit on `{ast.unparse(statement.test)}` (line {statement.lineno})"

    def node(self, node: ast.AST, guard) -> None:
        if isinstance(node, (ast.If, ast.IfExp)):
            self.node(node.test, guard)
            inner = guard or (f"`if {ast.unparse(node.test)}`" if self._is_guard(node.test) else None)
            for branch in (node.body, node.orelse):
                if isinstance(branch, list):
                    self.block(branch, inner)
                else:
                    self.node(branch, inner)
            return
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                self.node(value, guard)
                if guard is None and self._is_guard(value):
                    guard = f"the short-circuit on `{ast.unparse(value)}`"
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for generator in node.generators:
                self.node(generator.iter, guard)
                for condition in generator.ifs:
                    self.node(condition, guard)
                    if guard is None and self._is_guard(condition):
                        guard = f"the comprehension filter `{ast.unparse(condition)}`"
            for element in (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,):
                self.node(element, guard)
            return
        text = _security_text(node)
        if text is not None:
            self.uses.append((node.lineno, repr(_ws(text)[:70]), guard, None))
            for part in getattr(node, "values", ()):  # an f-string's interpolations
                if isinstance(part, ast.FormattedValue):
                    self.node(part.value, guard)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in self.bearing:
            self.uses.append((node.lineno, f"`{node.id}`", guard, node.id))
            return
        for _, value in ast.iter_fields(node):
            if isinstance(value, list) and value and all(isinstance(item, ast.stmt) for item in value):
                self.block(value, guard)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.node(item, guard)
            elif isinstance(value, ast.AST):
                self.node(value, guard)


def _bindings(function: ast.AST):
    """``(bound names, value)`` for every local binding: assignments, walrus, loop targets."""
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets, value = (node.targets if isinstance(node, ast.Assign) else [node.target]), node.value
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            targets, value = [node.target], node.iter
        else:
            continue
        names = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
        yield names, value


def _local_names(function: ast.AST, binds) -> set:
    """Fixpoint: local names bound from a value for which ``binds(value, names_so_far)`` holds."""
    names: set = set()
    while True:
        found = {name for bound, value in _bindings(function) if binds(value, names) for name in bound}
        if found <= names:
            return names
        names |= found


def _carries_security(value: ast.AST, names: set) -> bool:
    return any(
        _security_text(node) is not None or (isinstance(node, ast.Name) and node.id in names)
        for node in ast.walk(value)
    )


def _scan(function: ast.AST, predicates: set, bearing: set) -> list:
    """Security uses in one function. Local aliases count both ways: ``exists = _has_table(t)``
    is a guard wherever ``exists`` is tested, and ``statements = POSTGRES_DDL`` (104) is a use
    wherever ``statements`` -- or a loop variable drawn from it -- is loaded."""
    local_checks = _local_names(function, lambda value, names: _is_table_check(value, predicates | names))
    local_bearing = _local_names(function, lambda value, names: _carries_security(value, bearing | names))
    scan = _GuardScan(predicates, bearing | local_bearing, local_checks)
    scan.block(function.body, None)
    return scan.uses


def _table_guarded_security(source: str, label: str) -> tuple:
    """``(violations, uses)`` for one migration: every RLS / REVOKE use reachable from
    ``upgrade()`` that sits under a table-existence guard, and how many uses were reached.

    Reachability follows loads of module-level names, transitively: a helper holding the
    literal (106/107's ``_security``) or a constant holding the statements (102-105's
    ``POSTGRES_DDL``) is a use AT EACH REFERENCE, so a guard at the call site in ``upgrade``
    is caught even though the literal itself sits unguarded at module scope. ``downgrade()``
    is never a root, so a guard there (072, 074) or in a helper only it reaches is exempt.
    """
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    constants = {}
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and node.value is not None:
                constants[target.id] = node.value

    predicates: set = set()
    while True:
        found = {
            name
            for name, function in functions.items()
            if any(
                isinstance(node, ast.Return) and node.value is not None and _is_table_check(node.value, predicates)
                for node in ast.walk(function)
            )
        }
        if found <= predicates:
            break
        predicates |= found

    bearing: set = set()
    while True:
        found = {name for name, function in functions.items() if _scan(function, predicates, bearing)}
        found |= {name for name, value in constants.items() if _carries_security(value, bearing - {name})}
        if found <= bearing:
            break
        bearing |= found

    violations, reached, visited = [], 0, set()

    def visit(name: str, chain: list) -> None:
        nonlocal reached
        if name in visited or name not in functions:
            return
        visited.add(name)
        for line, what, guard, referenced in _scan(functions[name], predicates, bearing):
            reached += 1
            if guard is not None:
                violations.append(f"{label}:{line} [{' -> '.join(chain)}] {what} runs only under {guard}")
            elif referenced is not None:
                visit(referenced, chain + [referenced])

    visit("upgrade", ["upgrade"])
    return violations, reached


# Synthetic migrations the check must classify correctly. Each is (source, flagged?).
_PREAMBLE = (
    "from alembic import op\n"
    "import sqlalchemy as sa\n"
    "def _has_table(name):\n"
    "    return sa.inspect(op.get_bind()).has_table(name)\n"
)
_GUARD_CHECK_CASES = {
    # The four the rule exists for.
    "a: RLS directly under `if not _has_table` in upgrade": (
        "def upgrade():\n"
        "    if not _has_table('t'):\n"
        "        op.create_table('t', sa.Column('id', sa.Integer(), primary_key=True))\n"
        "        op.execute('ALTER TABLE t ENABLE ROW LEVEL SECURITY')\n",
        True,
    ),
    "b: guarded call to a helper that holds the literal": (
        "def _security(table):\n"
        "    if op.get_bind().dialect.name == 'postgresql':\n"
        "        op.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC')\n"
        "def upgrade():\n"
        "    if not _has_table('t'):\n"
        "        _security('t')\n",
        True,
    ),
    "c: the same nesting in downgrade is exempt": (
        "def upgrade():\n"
        "    pass\n"
        "def downgrade():\n"
        "    if _has_table('t'):\n"
        "        op.execute('ALTER TABLE t ENABLE ROW LEVEL SECURITY')\n"
        "        op.drop_table('t')\n",
        False,
    ),
    "d: a role-existence guard is not a table guard": (
        "def _role_exists(name):\n"
        "    return True\n"
        "def upgrade():\n"
        "    if _role_exists('anon'):\n"
        "        op.execute('REVOKE ALL ON TABLE t FROM anon')\n",
        False,
    ),
    # The shapes that make it more than a lexical grep.
    "else-branch of a table test": (
        "def upgrade():\n"
        "    if _has_table('t'):\n"
        "        pass\n"
        "    else:\n"
        "        op.execute('ALTER TABLE t ENABLE ROW LEVEL SECURITY')\n",
        True,
    ),
    "early exit on a table test": (
        "def upgrade():\n"
        "    if _has_table('t'):\n"
        "        return\n"
        "    op.execute('ALTER TABLE t ENABLE ROW LEVEL SECURITY')\n",
        True,
    ),
    "continue on a table test inside a loop": (
        "def upgrade():\n"
        "    for table in ('a', 'b'):\n"
        "        if _has_table(table):\n"
        "            continue\n"
        "        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')\n",
        True,
    ),
    "module constant run under a custom `_exists` predicate (the 102-105 shape)": (
        "DDL = ('ALTER TABLE t ENABLE ROW LEVEL SECURITY', 'REVOKE ALL ON TABLE t FROM PUBLIC')\n"
        "def _exists(table):\n"
        "    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(table)\n"
        "def upgrade():\n"
        "    if not _exists('t'):\n"
        "        for statement in DDL:\n"
        "            op.execute(statement)\n",
        True,
    ),
    "a constant aliased into a local, then run under a guard (the 104 shape)": (
        "DDL = {'t': ('ALTER TABLE t ENABLE ROW LEVEL SECURITY',)}\n"
        "def upgrade():\n"
        "    statements = DDL if op.get_bind().dialect.name == 'postgresql' else {}\n"
        "    for table in ('t',):\n"
        "        if _has_table(table):\n"
        "            continue\n"
        "        for statement in statements[table]:\n"
        "            op.execute(statement)\n",
        True,
    ),
    "guard two helpers deep": (
        "def _security():\n"
        "    op.execute('REVOKE ALL ON TABLE t FROM PUBLIC')\n"
        "def _finish():\n"
        "    if not _has_table('t'):\n"
        "        _security()\n"
        "def upgrade():\n"
        "    _finish()\n",
        True,
    ),
    "table check held in a local variable": (
        "def upgrade():\n"
        "    already = _has_table('t')\n"
        "    if not already:\n"
        "        op.execute('alter table t enable row level security')\n",
        True,
    ),
    # Must stay clean.
    "the guarded-create / unconditional-security shape every fixed migration uses": (
        "def _security(table):\n"
        "    op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')\n"
        "def upgrade():\n"
        "    offline = op.get_context().as_sql\n"
        "    if offline or not _has_table('t'):\n"
        "        op.create_table('t', sa.Column('id', sa.Integer(), primary_key=True))\n"
        "    if op.get_bind().dialect.name == 'postgresql':\n"
        "        _security('t')\n",
        False,
    ),
    "a docstring that mentions REVOKE, in a helper called under a guard": (
        "def _create():\n"
        "    '''Creates the table; REVOKE and RLS stay unconditional in upgrade().'''\n"
        "    op.create_table('t', sa.Column('id', sa.Integer(), primary_key=True))\n"
        "def upgrade():\n"
        "    if not _has_table('t'):\n"
        "        _create()\n"
        "    op.execute('ALTER TABLE t ENABLE ROW LEVEL SECURITY')\n",
        False,
    ),
    "a helper only downgrade reaches": (
        "def _drop(table):\n"
        "    if _has_table(table):\n"
        "        op.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC')\n"
        "def upgrade():\n"
        "    pass\n"
        "def downgrade():\n"
        "    _drop('t')\n",
        False,
    ),
}


# ----------------------------------------------------------------------------- tests


@pytest.mark.integration
@pytest.mark.slow
def test_fresh_bootstrap_reaches_head_with_exactly_the_create_all_schema(tmp_path):
    head = _single_head()
    reference_db = tmp_path / "create_all_only.db"
    bootstrapped_db = tmp_path / "bootstrapped.db"

    # The yardstick: what the models alone declare.
    declared_tables = _create_all(reference_db)
    expected = _schema(reference_db)
    # Proves the subprocess really wrote OUR temp file. Without it, a URL that silently
    # resolved elsewhere would leave both files empty and the diff below vacuously clean.
    assert set(expected) == declared_tables, "create_all did not build the declared tables in the temp file"

    # The bootstrap, step for step as reset_dev_db runs it.
    assert _create_all(bootstrapped_db) == declared_tables
    stamp = _alembic(bootstrapped_db, "stamp", STAMP_BASELINE)
    assert stamp.returncode == 0, _describe_failure(f"alembic stamp {STAMP_BASELINE}", stamp)
    assert _version_rows(bootstrapped_db) == [STAMP_BASELINE]

    # 1. The chain completes and lands on the single head.
    upgrade = _alembic(bootstrapped_db, "upgrade", "head")
    assert upgrade.returncode == 0, _describe_failure("alembic upgrade head over a create_all schema", upgrade)
    assert _version_rows(bootstrapped_db) == [head]

    # Assertions 2 and 3 are independent, so collect both before failing: a migration can
    # be mis-shaped AND non-idempotent, and the message should say so in one run.
    failures = []

    # 2. Object for object, the bootstrapped schema is the create_all schema.
    after_first_run = _schema(bootstrapped_db)
    diff = _schema_diff(after_first_run, expected, "the bootstrapped DB", "create_all")
    if diff:
        failures.append(_format_diff("bootstrap result differs from a pure create_all schema", diff))

    # 3. Run every post-baseline revision again over the result: nothing may move.
    restamp = _alembic(bootstrapped_db, "stamp", STAMP_BASELINE)
    assert restamp.returncode == 0, _describe_failure(f"re-stamp {STAMP_BASELINE}", restamp)
    rerun = _alembic(bootstrapped_db, "upgrade", "head")
    if rerun.returncode != 0:
        failures.append(_describe_failure("second alembic upgrade head over the bootstrapped DB", rerun))
    else:
        assert _version_rows(bootstrapped_db) == [head]
        rerun_diff = _schema_diff(_schema(bootstrapped_db), after_first_run, "the second run", "the first run")
        if rerun_diff:
            failures.append(_format_diff("re-running upgrade head changed the schema", rerun_diff))

    assert not failures, "\n\n".join(failures)


@pytest.mark.unit
def test_every_revision_id_written_on_the_fresh_path_fits_version_num():
    """Each id ``stamp`` and ``upgrade`` record must fit ``varchar(32)``.

    ``alembic stamp`` creates ``alembic_version.version_num`` as ``String(32)``. The revision
    that widens it, ``014b_widen_alembic_version``, sits BELOW the ``058`` stamp and never
    runs on a fresh bootstrap, so on Postgres any later id over 32 characters fails to
    record with ``value too long for type character varying(32)``. SQLite enforces no
    VARCHAR length, so the chain test above passes regardless. A few per-revision tests
    (``test_migration_104_*``, ``test_migration_105_*``) pin their own id; this is the
    chain-wide check, so a new revision is covered without a test of its own.

    The ``revision`` attribute, never the filename: ``099_recoverable_import_batches.py`` is
    33 characters, its id 30. Everything below ``058`` is out of scope -- seven old ids
    exceed 32, but the fresh path stamps past them and never writes them.
    """
    revision_ids = _revision_ids_written_on_the_fresh_path()
    # The walk must actually span the path, or the check passes on nothing.
    assert revision_ids[0] == _single_head()
    assert revision_ids[-1] == STAMP_BASELINE
    # And the check must be able to fail -- at exactly one character past the limit.
    at_limit, past_limit = "x" * VERSION_NUM_MAX_LENGTH, "x" * (VERSION_NUM_MAX_LENGTH + 1)
    assert _overlong([at_limit, past_limit]) == [(past_limit, VERSION_NUM_MAX_LENGTH + 1)]

    offenders = _overlong(revision_ids)
    assert not offenders, (
        f"revision ids over {VERSION_NUM_MAX_LENGTH} characters would fail to record in "
        f"alembic_version on a freshly bootstrapped Postgres: {offenders}"
    )


@pytest.mark.unit
def test_reset_dev_db_stamps_the_baseline_this_file_tests():
    """``scripts/reset_dev_db.py`` is the path this file claims to cover; keep them one path.

    A later stamp there would skip 059/060's RLS and triggers on every rebuilt dev DB, and
    this test would go on certifying a baseline nobody runs.
    """
    from scripts.reset_dev_db import STAMP_BASELINE as RESET_DEV_DB_BASELINE

    assert RESET_DEV_DB_BASELINE == STAMP_BASELINE


@pytest.mark.unit
def test_rls_and_revoke_never_run_under_a_table_existence_guard():
    """``ENABLE ROW LEVEL SECURITY`` / ``REVOKE`` must not depend on whether a table exists.

    ``create_all`` enables no RLS and revokes nothing, and it runs ahead of the migrations on
    every non-production boot and in ``scripts/seed_data.py``. A security statement nested
    under ``if not _has_table(...)`` (or its else, or after an early exit on one, or inside a
    helper CALLED there) therefore silently never fires on any database whose tables
    ``create_all`` built first, and that table stays RLS-off. The fresh-bootstrap test above
    cannot see this: on SQLite the ``postgresql`` branches never execute.

    The checker is proved on synthetic migrations first -- it must flag every guarded shape
    and pass every legitimate one -- so a green result on the real tree means something.
    """
    misclassified = []
    for case, (source, should_flag) in _GUARD_CHECK_CASES.items():
        violations, _ = _table_guarded_security(_PREAMBLE + source, "synthetic")
        if bool(violations) is not should_flag:
            misclassified.append(f"{case}: expected {'a violation' if should_flag else 'none'}, got {violations}")
    assert not misclassified, "the guard check misclassifies:\n  " + "\n  ".join(misclassified)

    scripts = _scripts_run_on_the_fresh_path()
    assert scripts[-1].down_revision == STAMP_BASELINE, "the walk must start at the first revision after the stamp"
    violations, reached = [], 0
    for script in scripts:
        found, count = _table_guarded_security(Path(script.path).read_text(), Path(script.path).name)
        violations += found
        reached += count
    assert reached > 0, "no RLS / REVOKE is reachable from any upgrade(): the pattern or the walk is broken"
    assert not violations, (
        "RLS / REVOKE reachable from upgrade() is skipped whenever the table already exists -- move it out "
        "of the table-existence guard and keep it unconditional and idempotent:\n  " + "\n  ".join(violations)
    )
