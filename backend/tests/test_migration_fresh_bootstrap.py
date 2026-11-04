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
6. The guarded creates are not dead code. ``create_all`` -> ``stamp heads`` -> ``downgrade
   088_api_tokens`` -> ``upgrade head`` must again yield the ``create_all`` schema, object for
   object. On the chain above every ``089``+ revision's own create is no-op'd by its guard,
   so a guarded create deleted from a migration, or a ``create_table`` that drifts from its
   model, cannot show there -- and did not: ``100`` names four FKs and its downgrade drops
   them BY NAME, the model declared them unnamed, and the chain stayed green. The round-trip
   makes every ``089``+ create actually execute and every downgrade from head to ``089`` run
   over a ``create_all`` schema. Its floor is ``088`` and not ``058``: see
   ``ROUND_TRIP_FLOOR``.
7. Any SQL literal reachable from ``upgrade()`` that NAMES ``anon``, ``authenticated`` or
   ``service_role`` runs under a role-existence check, in its own SQL (``DO $$ ... IF EXISTS
   (SELECT ... FROM pg_roles ...)``) or in Python (059's ``_postgrest_roles_exist``). Those
   roles exist only on Supabase, and any statement naming a missing one aborts the migration:
   ``093`` shipped a bare ``REVOKE ... FROM anon, authenticated`` and killed the chain at
   ``093`` on every other Postgres. "Names" is deliberately wide (``_role_naming_text``): the
   role spelled anywhere outside SQL comments and a ``rolname`` comparison -- a grantee, a
   membership ``GRANT anon TO x``, a policy's ``TO``, ``has_table_privilege('anon', ...)`` --
   or a role position (grantee, membership, ``FOR ROLE``, policy ``TO``) filled at run time by
   an f-string, ``str.format``, ``%`` or ``+``. ``PUBLIC`` is exempt. Model ``after_create``
   DDL runs inside ``create_all``, where no Python guard can reach it, so every model literal
   naming one of those roles must carry the ``pg_roles`` check in its own SQL
   (``_model_role_literals``). Both are static TRIPWIRES for the 093 class of regression, not
   proofs that the guarded SQL is right: the chain above runs on SQLite, where no
   ``postgresql`` branch executes. What executes the whole path on a Postgres WITHOUT those
   roles is ``scripts/verify_fresh_bootstrap_postgres.py``, in the non-blocking ``e2e.yml``.

WHAT IT CANNOT SEE
------------------
The suite runs on SQLite by design (CLAUDE.md -> "Why the tests run on SQLite"), so the
Postgres-only statements the migrations gate on dialect -- ``ENABLE ROW LEVEL SECURITY``,
``REVOKE``, trigger functions -- never EXECUTE here. Assertions 5 and 7 check where they sit,
not what they do: they say nothing about whether they are idempotent or correct on Postgres
(the ``scripts/verify_*_postgres.py`` runs in e2e.yml are the only place they execute), they
follow only calls and names they can resolve statically within one module, and they treat
nested functions lexically. By design 5 flags table-existence guards only -- role, trigger,
dialect and index/column guards pass.

7 is a tripwire with known blind spots, beyond the reach limits above:

* An in-SQL guard is any ``IF EXISTS (SELECT ... FROM pg_roles`` outside a comment in the same
  literal. Nothing proves its ``IF`` encloses the statement, or that it tests the role the
  statement names.
* A Python guard is a CALL named like a role check (``_role_exists(...)``), a module helper
  returning one, or a local BOUND from one. Its polarity is not checked -- ``if not
  _role_exists(...)`` passes, as does the ``else`` branch -- and a helper is trusted on its
  name, not on what its body queries.
* A local read anywhere is judged at its reads, so a value that EXECUTES at its binding and is
  then read only under a guard (``x = op.execute(...)``, then ``if _role_exists(...): use(x)``)
  is judged by that guard. A local never read at all is judged at its binding.
* A role chosen at run time is seen only in a grantee, membership, ``FOR ROLE`` or policy
  ``TO`` position, and a concatenation only when it leaves a grantee, ``FOR ROLE`` or policy
  ``TO`` open at the very END of a literal. ``has_table_privilege('{}', ...)``,
  ``'{}'::regrole``, ``SET ROLE``, ``OWNER TO``, or ``'GRANT ' + role + ' TO x'`` are seen only
  when the role is spelled out.
* Only ``anon``, ``authenticated`` and ``service_role`` are named; Supabase's other roles
  (``authenticator``, ``supabase_admin``, ...) are not. In the other direction it fails
  closed: one of those words in a Python message or a SQL string literal outside a
  ``rolname`` comparison counts as naming the role.
* The model check reads literal role NAMES only, never an interpolated role:
  ``quote_nesting_source`` joins its ``pg_roles`` check and its ``FROM {_role}`` REVOKE with
  ``+`` through a loop variable, which no literal-level rule can see as one statement, so its
  REVOKEs -- and the self-guarded f-strings in ``fabrication_quote`` /
  ``fabrication_quote_profile`` -- are outside it. Listeners registered from other packages,
  and DDL built anywhere but a literal, are unseen too.

The round-trip (6) runs on an EMPTY schema, so no data-moving logic executes, and it
proves each downgrade COMPLETES, not that it removes everything: an object a downgrade
leaves behind is found again by its upgrade's guard, so that one create stays unexercised
and the diff stays clean. ``059``-``088`` stay outside it (see ``ROUND_TRIP_FLOOR``); for
them, and for the incremental path generally, a guard that no-ops on a ``create_all``
schema is exactly what lets a migration's own DDL disagree with its model unnoticed, which
is what the per-revision lock-step tests are for.

HOW IT RUNS
-----------
Every ``create_all`` and alembic command runs in a SUBPROCESS, with ``DATABASE_URL``
pointed at a file under ``tmp_path``. ``alembic/env.py`` takes its URL from ``settings``,
and ``tests/conftest.py`` has already pointed this process's settings at the suite's
shared-cache in-memory database, which every other test on this worker depends on -- so
nothing here may build the app engine in-process. Inspection opens a throwaway engine on
the temp file only, and ``tmp_path`` keeps xdist workers apart.

The whole chain scenario (1-3) is ONE test on purpose. Under xdist's default ``load``
scheduling the tests of a module are dealt round-robin across workers, so a module-scoped
fixture shared by three tests would build the chain up to three times. The round-trip (6)
is a test of its own for the converse reason: it shares no chain with 1-3, only the
``create_all`` yardstick, so splitting it costs one sub-second ``create_all`` and buys the
two subprocess sequences running on separate workers, each failing on its own.
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
from typing import Callable, NamedTuple

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parent.parent

# The documented baseline. NOT a later one: 059 (RLS / REVOKE) and 060 (the audit-log
# immutability triggers) create what create_all cannot, so they must actually run.
STAMP_BASELINE = "058_process_sheets"

# How far the round-trip downgrades before upgrading back. ``089`` on is where every
# migration guards its creates, so the floor sits just below it. NOT the stamp
# baseline: a ``058`` round-trip surfaces 34 known, accepted model-vs-migration differences in
# ``059``-``088`` (as of ``113``) -- server defaults spelled ``false`` vs ``'false'``, FKs named
# on one side only, FKs SQLite's ``ALTER TABLE ADD COLUMN`` cannot add -- and an allowlist of
# them would hide the next real one. Lower it only together with fixing those.
ROUND_TRIP_FLOOR = "088_api_tokens"

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

# Alembic logs each step as ``Running upgrade <from> -> <to>`` or ``Running downgrade <from> ->
# <to>``, either side empty at the base.
_RUNNING_STEP = re.compile(r"Running (upgrade|downgrade) ([^\s,]*) -> ([^\s,]*)")


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
    """Name the revision that was running when the subprocess died, then the log tail.

    An upgrade ``a -> b`` runs ``b``'s ``upgrade()``; a downgrade ``b -> a`` runs ``b``'s
    ``downgrade()`` -- the revision to open is the target of one and the source of the other.
    """
    lines = [f"{label} failed (exit {result.returncode})"]
    running = _RUNNING_STEP.findall(result.stderr)
    if running:
        direction, source, target = running[-1]
        ran = target if direction == "upgrade" else source
        lines.append(
            f"last revision started: {direction} {source or '<base>'} -> {target or '<base>'}, "
            f"i.e. {ran}'s {direction}()"
        )
    lines += ["--- stderr (tail) ---", *result.stderr.splitlines()[-60:]]
    lines += ["--- stdout (tail) ---", *result.stdout.splitlines()[-20:]]
    return "\n".join(lines)


def _create_all(db_path: Path) -> set:
    """``create_all`` on ``db_path``; returns the table names the metadata declared."""
    result = _run(db_path, "-c", CREATE_ALL_SCRIPT)
    assert result.returncode == 0, _describe_failure("create_all", result)
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def _create_all_yardstick(db_path: Path) -> tuple:
    """``(declared tables, schema)`` of a pure ``create_all`` on ``db_path``: what the models
    alone declare, the schema every scenario here must reproduce."""
    declared_tables = _create_all(db_path)
    expected = _schema(db_path)
    # Proves the subprocess really wrote OUR temp file. Without it, a URL that silently
    # resolved elsewhere would leave both files empty and every diff vacuously clean.
    assert set(expected) == declared_tables, "create_all did not build the declared tables in the temp file"
    return declared_tables, expected


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
    return _names_a_check(expr, predicates, _TABLE_CHECK_IDENTIFIER)


def _names_a_check(expr: ast.AST, predicates: set, identifier: re.Pattern) -> bool:
    """Does ``expr`` mention a predicate helper, or a name / attribute matching ``identifier``?"""
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and (node.id in predicates or identifier.search(node.id)):
            return True
        if isinstance(node, ast.Attribute) and identifier.search(node.attr):
            return True
    return False


def _literal_text(node: ast.AST):
    """A str literal's text, or an f-string's with each interpolation rendered as ``{}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
    return None


def _security_text(node: ast.AST):
    """The SQL text of a str literal or f-string that issues RLS / REVOKE, else None."""
    text = _literal_text(node)
    return text if text is not None and _SECURITY_SQL.search(text) else None


class _Rule(NamedTuple):
    """What one static check tracks, so ``_GuardScan`` serves both.

    ``statement`` returns the SQL text of a literal the check tracks, else None; ``is_check``
    says whether an expression tests the guarded condition; ``literal_guard`` names a guard a
    literal carries in its own SQL, else None; ``defer_bindings`` judges a local bound from a
    tracked statement where it is LOADED (and under its binding site's guard) instead of where
    it is bound -- needed when a statement is assembled in a local and only then wrapped in its
    guard, as 113's ``revokes`` is. A deferred local that is never loaded at all is judged where
    it is bound (``_unguarded_role_grants``), the only place its value runs.
    """

    statement: Callable
    is_check: Callable
    literal_guard: Callable
    defer_bindings: bool


# Assertion 5: RLS / REVOKE anywhere; a table-existence test is the guard (and the defect).
_TABLE_GUARD_RULE = _Rule(_security_text, _is_table_check, lambda node: None, False)


class _GuardScan:
    """Walk one function body, recording every tracked USE and the guard over it.

    A use is a tracked literal (``rule.statement``), or a load of a name that carries one: a
    module-level function or constant, or a local bound from either. A guard is any construct
    that makes the code below it run only for one answer to ``rule.is_check`` ("does the
    table exist" for assertion 5): both branches of an ``if`` / conditional expression on such
    a test, the operands after one in ``and`` / ``or``, a comprehension filtered on one, and
    everything after an ``if`` on one whose branch returns, raises, ``continue``s or
    ``break``s -- plus, under a rule that has them, a guard the literal states in its own SQL,
    which covers its interpolations too. Bare string statements (docstrings) never execute and
    are skipped, so prose that mentions REVOKE cannot trip the check.
    """

    def __init__(
        self, predicates: set, bearing: set, local_checks: set, rule: _Rule = _TABLE_GUARD_RULE, deferred=frozenset()
    ):
        self.predicates = predicates
        self.bearing = bearing
        self.local_checks = local_checks
        self.rule = rule
        self.deferred = deferred  # locals judged where loaded; see _Rule.defer_bindings
        self.uses = []  # (line, description, guard or None, referenced name or None)
        self.bindings = []  # (names, value, guard at the binding) for each deferred binding
        self.loaded = set()  # every bearing name loaded anywhere in this scope, guarded or not

    def _is_guard(self, expr: ast.AST) -> bool:
        return self.rule.is_check(expr, self.predicates | self.local_checks)

    def _deferred_binding(self, node: ast.AST):
        """``(names, value)`` when ``node`` binds only deferred locals, else None. Never a walrus:
        its value is used right where it is bound."""
        if not self.deferred:
            return None
        if isinstance(node, (ast.For, ast.AsyncFor)):
            targets, value = [node.target], node.iter
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            return None
        names = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
        return (names, value) if names and names <= self.deferred else None

    def fork(self, expr: ast.AST, guard) -> "_GuardScan":
        """A scan of ``expr`` alone in this scan's scope: the same locals and bindings."""
        scan = _GuardScan(self.predicates, self.bearing, self.local_checks, self.rule, self.deferred)
        scan.bindings, scan.loaded = self.bindings, self.loaded
        scan.node(expr, guard)
        return scan

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
        deferred = self._deferred_binding(node)
        if deferred is not None:
            self.bindings.append((*deferred, guard))
            if isinstance(node, (ast.For, ast.AsyncFor)):
                self.block(node.body, guard)
                self.block(node.orelse, guard)
            return
        text = self.rule.statement(node)
        own_guard = self.rule.literal_guard(node)
        if text is not None or own_guard is not None:
            inner = guard or own_guard
            if text is not None:
                self.uses.append((node.lineno, repr(_ws(text)[:70]), inner, None))
            for part in getattr(node, "values", ()):  # an f-string's interpolations
                if isinstance(part, ast.FormattedValue):
                    self.node(part.value, inner)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in self.bearing:
            self.uses.append((node.lineno, f"`{node.id}`", guard, node.id))
            self.loaded.add(node.id)
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


def _carries_security(value: ast.AST, names: set, statement: Callable = _security_text) -> bool:
    return any(
        statement(node) is not None or (isinstance(node, ast.Name) and node.id in names) for node in ast.walk(value)
    )


def _scanner(function: ast.AST, predicates: set, bearing: set, rule: _Rule = _TABLE_GUARD_RULE) -> _GuardScan:
    """A ``_GuardScan`` for one function, its local aliases resolved (see ``_scan``)."""
    local_checks = _local_names(function, lambda value, names: rule.is_check(value, predicates | names))
    local_bearing = _local_names(
        function, lambda value, names: _carries_security(value, bearing | names, rule.statement)
    )
    deferred = local_bearing if rule.defer_bindings else frozenset()
    return _GuardScan(predicates, bearing | local_bearing, local_checks, rule, deferred)


def _scan(function: ast.AST, predicates: set, bearing: set, rule: _Rule = _TABLE_GUARD_RULE) -> list:
    """Security uses in one function. Local aliases count both ways: ``exists = _has_table(t)``
    is a guard wherever ``exists`` is tested, and ``statements = POSTGRES_DDL`` (104) is a use
    wherever ``statements`` -- or a loop variable drawn from it -- is loaded."""
    scan = _scanner(function, predicates, bearing, rule)
    scan.block(function.body, None)
    return scan.uses


def _module_scope(tree: ast.Module, rule: _Rule) -> tuple:
    """``(functions, constants, predicates, bearing)`` of one parsed migration under ``rule``.

    ``predicates``: the module's helpers whose ``return`` is a ``rule.is_check`` (transitively).
    ``bearing``: the module-level functions and constants that carry a tracked statement --
    directly, or through another bearing name (transitively).
    """
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
                isinstance(node, ast.Return) and node.value is not None and rule.is_check(node.value, predicates)
                for node in ast.walk(function)
            )
        }
        if found <= predicates:
            break
        predicates |= found

    bearing: set = set()
    while True:
        found = {name for name, function in functions.items() if _scan(function, predicates, bearing, rule)}
        found |= {
            name for name, value in constants.items() if _carries_security(value, bearing - {name}, rule.statement)
        }
        if found <= bearing:
            break
        bearing |= found
    return functions, constants, predicates, bearing


def _table_guarded_security(source: str, label: str) -> tuple:
    """``(violations, uses)`` for one migration: every RLS / REVOKE use reachable from
    ``upgrade()`` that sits under a table-existence guard, and how many uses were reached.

    Reachability follows loads of module-level names, transitively: a helper holding the
    literal (106/107's ``_security``) or a constant holding the statements (102-105's
    ``POSTGRES_DDL``) is a use AT EACH REFERENCE, so a guard at the call site in ``upgrade``
    is caught even though the literal itself sits unguarded at module scope. ``downgrade()``
    is never a root, so a guard there (072, 074) or in a helper only it reaches is exempt.
    """
    functions, _, predicates, bearing = _module_scope(ast.parse(source), _TABLE_GUARD_RULE)
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


# ------------------------------- static check: Supabase-only roles are named under a role check

# The roles a Supabase project has and a plain Postgres does not. A statement that NAMES a
# missing one aborts, whatever kind it is: a GRANT / REVOKE grantee, a granted role (``GRANT anon
# TO x``), a policy's ``TO`` list, ``ALTER DEFAULT PRIVILEGES``, ``has_table_privilege('anon',
# ...)``, ``'anon'::regrole``. So the rule is about naming, not about a statement list.
_SUPABASE_ROLES = r"(?:anon|authenticated|service_role)"
_SUPABASE_ROLE = re.compile(rf"\b{_SUPABASE_ROLES}\b", re.IGNORECASE)

# A literal that is nothing but role names is a Python VALUE -- 059's ``_POSTGREST_ROLES``, every
# ``for role in ('anon', 'authenticated')`` -- not SQL. What it is interpolated into is judged.
_ROLE_VALUE = re.compile(rf"\s*{_SUPABASE_ROLES}(?:\s*,\s*{_SUPABASE_ROLES})*\s*", re.IGNORECASE)

# The one spelling that is safe with no guard: a ``rolname`` comparison reads ``pg_roles`` and never
# resolves the role, which is how every in-SQL guard asks its question. Its operand is not a use.
_ROLNAME_OPERAND = re.compile(
    r"\brolname\s*(?:(?:=|<>|!=)\s*(?:'[^']*'|ANY\s*\(\s*ARRAY\s*\[[^\]]*\]\s*\))|(?:NOT\s+)?IN\s*\([^)]*\))",
    re.IGNORECASE,
)

# The positions where SQL takes a ROLE, so a value interpolated there is a role chosen at run time:
# a GRANT / REVOKE grantee clause, to the end of the statement and from the FIRST ``TO`` / ``FROM``
# after the verb (a DO block's ``SELECT 1 FROM pg_roles`` precedes its REVOKE and is never read as
# one); a role-membership GRANT / REVOKE (no ``ON``); ``FOR ROLE`` in ``ALTER DEFAULT PRIVILEGES``;
# and a ``CREATE`` / ``ALTER POLICY`` ``TO`` list.
_ROLE_POSITIONS = (
    re.compile(r"\b(?:REVOKE\b[^;]*?\bFROM|GRANT\b[^;]*?\bTO)\b(?P<roles>[^;]*)", re.IGNORECASE),
    re.compile(r"\b(?:GRANT|REVOKE)\b(?P<roles>(?:(?!\bON\b)[^;])*?)\b(?:TO|FROM)\b", re.IGNORECASE),
    re.compile(r"\bFOR\s+(?:ROLE|USER)\b(?P<roles>[^;]*?)(?=\bIN\s+SCHEMA\b|\bGRANT\b|\bREVOKE\b|;|$)", re.IGNORECASE),
    re.compile(
        r"\b(?:CREATE|ALTER)\s+POLICY\b[^;]*?\bTO\b(?P<roles>[^;]*?)(?=\bUSING\b|\bWITH\s+CHECK\b|;|$)", re.IGNORECASE
    ),
)

# A value supplied at run time: an f-string interpolation (rendered ``{}`` by ``_literal_text``), a
# ``str.format`` field, a ``%`` conversion -- Python's ``%s`` / ``%(role)s``, or PL/pgSQL
# ``format()``'s ``%I`` / ``%L``. ``PUBLIC`` is never one: every Postgres has it.
_INTERPOLATION = re.compile(r"\{[^{}]*\}|%(?:\(\w+\)|\d+\$)?[sIL]")

# The in-SQL guard every Data API REVOKE since 091 uses: ``DO $$ BEGIN IF EXISTS (SELECT [1]
# FROM pg_roles WHERE rolname = '...') THEN ... END IF; END $$``.
_PG_ROLES_CHECK = re.compile(r"\bIF\s+EXISTS\s*\(\s*SELECT\b[^)]*?\bFROM\s+pg_roles\b", re.IGNORECASE)

# A CALL that is a role-existence check: 059's ``_role_exists(...)`` / ``_postgrest_roles_exist(...)``.
_ROLE_CHECK_IDENTIFIER = re.compile(r"roles?_exists?")


def _strip_sql_comments(text: str) -> str:
    """``text`` with each SQL comment -- ``--`` to end of line, ``/* */`` nested as Postgres nests
    them -- replaced by a space. Quoted strings and identifiers are kept whole, a comment marker
    inside one being text; dollar quotes are read through, since a DO block's body is code."""
    out, i, end = [], 0, len(text)
    while i < end:
        if text.startswith("--", i):
            newline = text.find("\n", i)
            i = end if newline < 0 else newline
            out.append(" ")
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < end and depth:
                step = 2 if text.startswith(("/*", "*/"), i) else 1
                depth += 1 if text.startswith("/*", i) else -1 if text.startswith("*/", i) else 0
                i += step
            out.append(" ")
        elif text[i] in "'\"":
            # A doubled quote reads as two adjacent strings, which is the same text.
            close = text.find(text[i], i + 1)
            close = end - 1 if close < 0 else close
            out.append(text[i : close + 1])
            i = close + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _names_a_supabase_role(sql: str) -> bool:
    """Does comment-free ``sql`` spell a Supabase-only role anywhere but a ``rolname`` operand?"""
    return not _ROLE_VALUE.fullmatch(sql) and bool(_SUPABASE_ROLE.search(_ROLNAME_OPERAND.sub(" ", sql)))


def _role_naming_text(node: ast.AST):
    """The SQL text of a str literal or f-string that names a Supabase-only role, else None.

    It names one by spelling it anywhere outside a ``rolname`` comparison, or by leaving a role
    position to run time: an interpolation there, or the position left open at the very end of
    the literal (``'... FROM ' + role``). SQL comments are read as nothing.
    """
    text = _literal_text(node)
    if text is None:
        return None
    sql = _strip_sql_comments(text)
    if _names_a_supabase_role(sql):
        return text
    for position in _ROLE_POSITIONS:
        for match in position.finditer(sql):
            roles = match.group("roles")
            left_open = match.end("roles") == len(sql) and (not roles.strip() or roles.rstrip().endswith(","))
            if left_open or _INTERPOLATION.search(roles):
                return text
    return None


def _is_role_check(expr: ast.AST, names: set) -> bool:
    """Does evaluating ``expr`` ask whether a database role exists?

    A CALL named like a role check (``_role_exists(...)``), or a load of one of ``names``: the
    module's predicate helpers, and the locals BOUND from a role check (``_scanner`` resolves those
    through ``_local_names``). Unlike ``_is_table_check``, a bare name is never matched on its
    spelling, so ``role_exists = True`` guards nothing.
    """
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            return True
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if _ROLE_CHECK_IDENTIFIER.search(called):
                return True
    return False


def _pg_roles_guard(node: ast.AST):
    """The guard a literal states in its own SQL -- a ``pg_roles`` existence check outside any SQL
    comment -- else None."""
    text = _literal_text(node)
    if text is not None and _PG_ROLES_CHECK.search(_strip_sql_comments(text)):
        return f"the pg_roles check in its own SQL (line {node.lineno})"
    return None


# Assertion 7: SQL naming a Supabase-only role; a role-existence test is the guard, and its
# ABSENCE the defect.
_ROLE_GUARD_RULE = _Rule(_role_naming_text, _is_role_check, _pg_roles_guard, True)


def _unguarded_role_grants(source: str, label: str) -> tuple:
    """``(violations, uses)`` for one migration: every literal reachable from ``upgrade()`` that
    names a Supabase-only role (``_role_naming_text``) and runs with no role-existence check over
    it, and how many such uses were reached.

    ``_table_guarded_security`` turned inside out: there a guard is the defect, here its
    absence is. So a guarded use is settled where it stands, and an UNGUARDED load of a name is
    followed into what the name holds -- a helper's body, a module constant's value, a local's
    binding (under the guard at that binding) -- where its statements are judged in turn. A
    literal whose own SQL carries the ``pg_roles`` check guards itself and everything
    interpolated into it, which is how 113's ``{revokes}`` passes. A local that is bound and
    never read at all is judged at its binding instead, since that is the only place its value
    runs: ``done = _revoke(conn)`` executes the helper there. ``downgrade()`` is never a root,
    so 059's re-GRANT there is out of scope.
    """
    functions, constants, predicates, bearing = _module_scope(ast.parse(source), _ROLE_GUARD_RULE)
    violations, followed, reached = [], set(), 0

    def settle_function(name: str, chain: list) -> None:
        scan = _scanner(functions[name], predicates, bearing, _ROLE_GUARD_RULE)
        scan.block(functions[name].body, None)
        settle(scan, name, chain)
        # Latest binding first, so ``b = a`` (never read) judges ``a`` through ``b``, once.
        for names, value, site in reversed(scan.bindings):
            if not names & scan.loaded:
                settle(scan.fork(value, site), name, chain + [", ".join(sorted(names))])

    def settle(scan: _GuardScan, scope, chain: list) -> None:
        nonlocal reached
        for line, what, guard, referenced in scan.uses:
            reached += 1
            if guard is not None:
                continue
            if referenced is None:
                violations.append(f"{label}:{line} [{' -> '.join(chain)}] {what} runs with no role-existence check")
                continue
            bound = [(value, site) for names, value, site in scan.bindings if referenced in names]
            key = (scope if bound else None, referenced)
            if key in followed:
                continue
            followed.add(key)
            if bound:
                for value, site in bound:
                    settle(scan.fork(value, site), scope, chain + [referenced])
            elif referenced in functions:
                settle_function(referenced, chain + [referenced])
            elif referenced in constants:
                module_level = _GuardScan(predicates, bearing, set(), _ROLE_GUARD_RULE)
                settle(module_level.fork(constants[referenced], None), None, chain + [referenced])

    if "upgrade" in functions:
        settle_function("upgrade", ["upgrade"])
    return violations, reached


def _model_role_literals(source: str, label: str) -> tuple:
    """``(violations, count)`` for one model module: every str literal / f-string that names a
    Supabase-only role BY NAME, and how many; a violation is one whose own SQL carries no
    ``pg_roles`` check.

    Model DDL runs from ``after_create`` listeners during ``create_all`` -- the first step of the
    fresh path and of every non-production boot -- where a Python guard cannot reach it: the
    listener executes the string as written. So each such literal must guard itself. Docstrings
    and the pieces of an f-string are skipped; the f-string is judged whole.
    """
    tree = ast.parse(source)
    skipped = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skipped.update(id(part) for part in node.values)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skipped.add(id(node.value))
    violations, count = [], 0
    for node in ast.walk(tree):
        text = None if id(node) in skipped else _literal_text(node)
        if text is None or not _names_a_supabase_role(_strip_sql_comments(text)):
            continue
        count += 1
        if _pg_roles_guard(node) is None:
            violations.append(f"{label}:{node.lineno} {_ws(text)[:70]!r} names a role with no pg_roles check")
    return violations, count


# Synthetic migrations the role check must classify. Each is (source, expected): "flagged" -- a
# violation; "guarded" -- uses reached, none unguarded (so a clean result cannot come from a
# pattern that matched nothing); "untracked" -- nothing the rule tracks is reachable at all.
_ROLE_PREAMBLE = _PREAMBLE + (
    "def _role_exists(connection, name):\n"
    "    query = sa.text('SELECT 1 FROM pg_roles WHERE rolname = :name')\n"
    "    return connection.execute(query, {'name': name}).first() is not None\n"
    "def _postgrest_roles_exist(connection):\n"
    "    return all(_role_exists(connection, role) for role in ('anon', 'authenticated'))\n"
)
_ROLE_GUARD_CHECK_CASES = {
    # The shape the rule exists for: 093's upgrade() tail as it shipped (before 2026-11-03),
    # which killed the chain on any Postgres without the Supabase roles.
    "093 as shipped: bare REVOKE ... FROM anon, authenticated under only a dialect check": (
        "def upgrade():\n"
        "    if op.get_bind().dialect.name == 'postgresql':\n"
        "        op.execute('ALTER TABLE public.mrp_supply_links ENABLE ROW LEVEL SECURITY')\n"
        "        # App users authenticate through FastAPI, not Supabase auth.uid().\n"
        "        op.execute('REVOKE ALL ON TABLE public.mrp_supply_links FROM anon, authenticated')\n"
        "        op.execute('REVOKE ALL ON SEQUENCE public.mrp_supply_links_id_seq FROM anon, authenticated')\n",
        "flagged",
    ),
    "an interpolated grantee with no guard": (
        "def upgrade():\n"
        "    for role in ('anon', 'authenticated'):\n"
        "        op.execute(f'REVOKE ALL ON TABLE t FROM {role}')\n",
        "flagged",
    ),
    "a GRANT to a Data API role": (
        "def upgrade():\n    op.execute('GRANT SELECT ON TABLE t TO authenticated')\n",
        "flagged",
    ),
    "a bare statement in a module constant, run unguarded": (
        "DDL = ('REVOKE ALL ON TABLE t FROM PUBLIC', 'REVOKE ALL ON TABLE t FROM anon')\n"
        "def upgrade():\n"
        "    for statement in DDL:\n"
        "        op.execute(statement)\n",
        "flagged",
    ),
    "a helper holding the bare statement, called unguarded": (
        "def _revoke(table):\n"
        "    op.execute(f'REVOKE ALL ON TABLE {table} FROM anon')\n"
        "def upgrade():\n"
        "    _revoke('t')\n",
        "flagged",
    ),
    "a statement held in a local and run OUTSIDE the DO block that checks pg_roles": (
        "def upgrade():\n"
        "    revoke = 'REVOKE ALL ON TABLE t FROM anon'\n"
        "    op.execute(\"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') \"\n"
        "               \"THEN NULL; END IF; END $$\")\n"
        "    op.execute(revoke)\n",
        "flagged",
    ),
    "a table-existence guard is not a role guard": (
        "def upgrade():\n"
        "    if _has_table('t'):\n"
        "        op.execute('REVOKE ALL ON TABLE t FROM authenticated')\n",
        "flagged",
    ),
    # A binding that is never read runs where it is bound, so it is judged there.
    "a helper's return value bound to a local that is never read": (
        "def _revoke(conn):\n"
        "    op.execute('REVOKE ALL ON TABLE t FROM anon')\n"
        "    return True\n"
        "def upgrade():\n"
        "    done = _revoke(op.get_bind())\n",
        "flagged",
    ),
    "op.execute's result bound to a local that is never read": (
        "def upgrade():\n    x = op.execute('REVOKE ALL ON TABLE t FROM anon')\n",
        "flagged",
    ),
    # Every spelling of a grantee chosen at run time, and the third Supabase role.
    "a %s grantee": (
        "def upgrade():\n"
        "    for role in ('anon', 'authenticated'):\n"
        "        op.execute('REVOKE ALL ON TABLE t FROM %s' % role)\n",
        "flagged",
    ),
    "a %(role)s grantee": (
        "def upgrade():\n    op.execute('REVOKE ALL ON TABLE t FROM %(role)s' % {'role': 'anon'})\n",
        "flagged",
    ),
    "a str.format grantee in a plain literal": (
        "def upgrade():\n    op.execute('REVOKE ALL ON TABLE t FROM {role}'.format(role='anon'))\n",
        "flagged",
    ),
    "a grantee concatenated onto the end of the literal": (
        "def upgrade():\n    role = 'anon'\n    op.execute('REVOKE ALL ON TABLE t FROM ' + role)\n",
        "flagged",
    ),
    "service_role": (
        "def upgrade():\n    op.execute('GRANT SELECT ON TABLE t TO service_role')\n",
        "flagged",
    ),
    # Statements beyond a grantee clause that also resolve the role, so also abort without it.
    "a role-membership GRANT": (
        "def upgrade():\n    op.execute('GRANT anon TO app_user')\n",
        "flagged",
    ),
    "a role-membership GRANT of an interpolated role": (
        "def upgrade():\n    for role in ('anon',):\n        op.execute(f'GRANT {role} TO app_user')\n",
        "flagged",
    ),
    "CREATE POLICY ... TO anon": (
        "def upgrade():\n    op.execute('CREATE POLICY p ON t FOR SELECT TO anon USING (true)')\n",
        "flagged",
    ),
    "ALTER POLICY ... TO an interpolated role": (
        "def upgrade():\n    for role in ('authenticated',):\n        op.execute(f'ALTER POLICY p ON t TO {role}')\n",
        "flagged",
    ),
    "ALTER DEFAULT PRIVILEGES ... TO anon": (
        "def upgrade():\n    op.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO anon')\n",
        "flagged",
    ),
    "has_table_privilege('anon', ...)": (
        "def upgrade():\n    op.execute(\"SELECT has_table_privilege('anon', 't', 'SELECT')\")\n",
        "flagged",
    ),
    # A guard that is not one.
    "the pg_roles check only in a -- comment": (
        "def upgrade():\n"
        "    op.execute('''-- IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')\n"
        "REVOKE ALL ON TABLE t FROM anon''')\n",
        "flagged",
    ),
    "the pg_roles check only in a nested /* */ comment": (
        "def upgrade():\n"
        "    op.execute('/* x /* y */ IF EXISTS (SELECT 1 FROM pg_roles) */ REVOKE ALL ON TABLE t FROM anon')\n",
        "flagged",
    ),
    "a local merely NAMED like a role check": (
        "def upgrade():\n"
        "    role_exists = True\n"
        "    if role_exists:\n"
        "        op.execute('REVOKE ALL ON TABLE t FROM anon')\n",
        "flagged",
    ),
    # Must stay clean.
    "the DO-block shape (091-112)": (
        "def upgrade():\n"
        "    if op.get_bind().dialect.name == 'postgresql':\n"
        "        op.execute('''DO $$ BEGIN\n"
        "          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN\n"
        "            REVOKE ALL ON TABLE t FROM anon;\n"
        "          END IF;\n"
        "        END $$''')\n",
        "guarded",
    ),
    "059's Python guards, grantees interpolated, one check nested in another": (
        "def upgrade():\n"
        "    conn = op.get_bind()\n"
        "    if _postgrest_roles_exist(conn):\n"
        "        roles = ', '.join(('anon', 'authenticated'))\n"
        "        for statement in (f'REVOKE USAGE ON SCHEMA public FROM {roles}',):\n"
        "            op.execute(statement)\n"
        "        if _role_exists(conn, 'postgres'):\n"
        "            op.execute(f'ALTER DEFAULT PRIVILEGES FOR ROLE postgres REVOKE ALL ON TABLES FROM {roles}')\n",
        "guarded",
    ),
    "113's loop: statements assembled in a local, interpolated into the pg_roles DO block": (
        "TABLES = ('a', 'b')\n"
        "def upgrade():\n"
        "    for role in ('anon', 'authenticated'):\n"
        "        revokes = '\\n'.join(f'REVOKE ALL ON TABLE {table} FROM {role};' for table in TABLES)\n"
        "        op.execute(f'''DO $$ BEGIN\n"
        "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        "{revokes}\n"
        "  END IF;\n"
        "END $$''')\n",
        "guarded",
    ),
    "104's shape: self-guarded constant statements reached through local aliases": (
        "DDL = {'t': ('REVOKE ALL ON TABLE t FROM PUBLIC',\n"
        "             \"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN\\n\"\n"
        "             'REVOKE ALL ON TABLE t FROM anon;\\n'\n"
        "             'END IF; END $$')}\n"
        "def upgrade():\n"
        "    statements = DDL if op.get_bind().dialect.name == 'postgresql' else {}\n"
        "    for statement in statements['t']:\n"
        "        op.execute(statement)\n",
        "guarded",
    ),
    "an early exit on a role check": (
        "def upgrade():\n"
        "    if not _role_exists(op.get_bind(), 'anon'):\n"
        "        return\n"
        "    op.execute('REVOKE ALL ON TABLE t FROM anon')\n",
        "guarded",
    ),
    "a local bound only under a role check, run where the check is out of sight": (
        "def upgrade():\n"
        "    statement = None\n"
        "    if _role_exists(op.get_bind(), 'anon'):\n"
        "        statement = 'REVOKE ALL ON TABLE t FROM anon'\n"
        "    if statement:\n"
        "        op.execute(statement)\n",
        "guarded",
    ),
    "106/107's loop: role names in a tuple, interpolated into a self-guarded DO block": (
        "def upgrade():\n"
        "    for role in ('anon', 'authenticated', 'service_role'):\n"
        "        op.execute(f\"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN \"\n"
        "                   f'REVOKE ALL ON TABLE t FROM {role}; END IF; END $$')\n",
        "guarded",
    ),
    "a local BOUND from a role check": (
        "def upgrade():\n"
        "    exists = _role_exists(op.get_bind(), 'anon')\n"
        "    if exists:\n"
        "        op.execute('REVOKE ALL ON TABLE t FROM anon')\n",
        "guarded",
    ),
    "a never-read local bound under a role check": (
        "def _revoke(conn):\n"
        "    op.execute('REVOKE ALL ON TABLE t FROM anon')\n"
        "    return True\n"
        "def upgrade():\n"
        "    if _role_exists(op.get_bind(), 'anon'):\n"
        "        done = _revoke(op.get_bind())\n",
        "guarded",
    ),
    "FROM PUBLIC, the table interpolated": (
        "def upgrade():\n"
        "    for table in ('a', 'b'):\n"
        "        op.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC')\n"
        "        op.execute(f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC')\n",
        "untracked",
    ),
    "FROM PUBLIC, the table concatenated in": (
        "def upgrade():\n    op.execute('REVOKE ALL ON TABLE ' + 't' + ' FROM PUBLIC')\n",
        "untracked",
    ),
    "a rolname comparison is a catalog read, not a use of the role": (
        "def upgrade():\n"
        "    found = op.get_bind().execute(sa.text(\"SELECT 1 FROM pg_roles WHERE rolname = 'anon'\")).first()\n"
        "    if found is None:\n"
        "        op.execute(\"SELECT 1 FROM pg_roles WHERE rolname IN ('anon', 'authenticated')\")\n",
        "untracked",
    ),
    "a role named only in a SQL comment": (
        "def upgrade():\n    op.execute('REVOKE ALL ON TABLE t FROM PUBLIC -- anon: see 113')\n",
        "untracked",
    ),
    "a format() %I outside a role position (104's trigger body)": (
        "def upgrade():\n"
        "    op.execute(\"CREATE FUNCTION f() RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN \"\n"
        "               \"EXECUTE format('SELECT 1 FROM %I.t', TG_TABLE_SCHEMA); RETURN NEW; END $$\")\n",
        "untracked",
    ),
    "059's unguarded re-GRANT in downgrade": (
        "def upgrade():\n"
        "    pass\n"
        "def downgrade():\n"
        "    op.execute('GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated')\n",
        "untracked",
    ),
}

# Synthetic model modules for ``_model_role_literals``, classified as above.
_MODEL_PREAMBLE = "from sqlalchemy import DDL, event\nfrom app.models.thing import Thing\n"
_MODEL_ROLE_CASES = {
    "a bare REVOKE in an after_create DDL": (
        "event.listen(Thing.__table__, 'after_create', DDL('REVOKE ALL ON TABLE t FROM anon'))\n",
        "flagged",
    ),
    "the pg_roles check only in a comment": (
        "event.listen(Thing.__table__, 'after_create', DDL(\n"
        "    '/* IF EXISTS (SELECT 1 FROM pg_roles) */ CREATE POLICY p ON t TO service_role USING (true)'))\n",
        "flagged",
    ),
    "the DO-block shape, table interpolated (continuous_improvement / hank_*)": (
        "for _table in ('a', 'b'):\n"
        "    _statement = f'''DO $$ BEGIN\n"
        "      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN\n"
        "        REVOKE ALL ON TABLE {_table} FROM anon;\n"
        "      END IF;\n"
        "    END $$'''\n"
        "    event.listen(Thing.__table__, 'after_create', DDL(_statement).execute_if(dialect='postgresql'))\n",
        "guarded",
    ),
    "role names as values, and prose naming them": (
        "'''Revokes anon and authenticated; service_role is untouched.'''\n" "ROLES = ('anon', 'authenticated')\n",
        "untracked",
    ),
}


# ----------------------------------------------------------------------------- tests


@pytest.mark.integration
@pytest.mark.slow
def test_fresh_bootstrap_reaches_head_with_exactly_the_create_all_schema(tmp_path):
    head = _single_head()
    bootstrapped_db = tmp_path / "bootstrapped.db"

    # The yardstick: what the models alone declare.
    declared_tables, expected = _create_all_yardstick(tmp_path / "create_all_only.db")

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


# Its own test rather than a fourth step above: it shares no chain with that test, only the
# create_all yardstick, so the split costs one sub-second create_all and lets xdist run the
# two subprocess sequences on separate workers (see the module docstring, HOW IT RUNS).
@pytest.mark.integration
@pytest.mark.slow
def test_downgrade_to_the_floor_and_back_rebuilds_exactly_the_create_all_schema(tmp_path):
    """Every ``089``+ create really runs, and builds what its model declares.

    The chain test cannot see a migration's own DDL: over a ``create_all`` schema every
    guard finds its object already there and skips the create, so a guarded create deleted
    outright, or a ``create_table`` / index / FK that drifts from its model, leaves that
    test green. That is how ``100``'s FKs drifted unseen until 2026-11-03: it creates four by
    name and its downgrade drops them by name, the model declared them unnamed, and nothing
    failed (``test_migration_100_*`` now pins the names too).

    Here the revisions above ``ROUND_TRIP_FLOOR`` are first downgraded off a ``create_all``
    schema -- every downgrade branch from head to ``089`` must complete there -- and then
    upgraded again, so each object they own is rebuilt by its migration's code, not by
    ``create_all``. The result must be the ``create_all`` schema, object for object
    (``_schema`` compares FK names too). Nothing is allowlisted: a difference is either a
    migration or a model to fix.
    """
    head = _single_head()
    round_trip_db = tmp_path / "round_trip.db"
    declared_tables, expected = _create_all_yardstick(tmp_path / "create_all_only.db")

    assert _create_all(round_trip_db) == declared_tables
    stamp = _alembic(round_trip_db, "stamp", "heads")
    assert stamp.returncode == 0, _describe_failure("alembic stamp heads", stamp)
    assert _version_rows(round_trip_db) == [head]

    downgrade = _alembic(round_trip_db, "downgrade", ROUND_TRIP_FLOOR)
    assert downgrade.returncode == 0, _describe_failure(
        f"alembic downgrade {ROUND_TRIP_FLOOR} over a create_all schema", downgrade
    )
    assert _version_rows(round_trip_db) == [ROUND_TRIP_FLOOR]
    # The upgrade below must have work to do, or the comparison after it proves nothing: if
    # every downgrade were a no-op, every guard would skip and the diff would be clean.
    removed = _schema_diff(_schema(round_trip_db), expected, f"the DB at {ROUND_TRIP_FLOOR}", "create_all")
    assert removed, f"downgrading to {ROUND_TRIP_FLOOR} removed nothing: the round-trip would exercise no create"

    upgrade = _alembic(round_trip_db, "upgrade", "head")
    assert upgrade.returncode == 0, _describe_failure(f"alembic upgrade head from {ROUND_TRIP_FLOOR}", upgrade)
    assert _version_rows(round_trip_db) == [head]

    diff = _schema_diff(_schema(round_trip_db), expected, "the round-tripped DB", "create_all")
    assert not diff, _format_diff(
        f"downgrade to {ROUND_TRIP_FLOOR} and upgrade head did not rebuild the create_all schema "
        f"(the downgrade left {len(removed)} difference(s) for migration code to close)",
        diff,
    )


@pytest.mark.unit
def test_a_failed_step_is_reported_by_the_revision_whose_code_ran():
    """A failed upgrade names its TARGET's ``upgrade()``, a failed downgrade its SOURCE's
    ``downgrade()`` -- the round-trip's failure message points at the right file either way."""

    def described(stderr: str) -> str:
        return _describe_failure("step", subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr))

    log = "INFO  [alembic.runtime.migration] Running {} {} -> {}, message\n"
    upgrade = log.format("upgrade", "088_a", "089_b") + log.format("upgrade", "089_b", "090_c") + "Traceback\n"
    assert "last revision started: upgrade 089_b -> 090_c, i.e. 090_c's upgrade()" in described(upgrade)
    downgrade = log.format("downgrade", "101_d", "100_c") + log.format("downgrade", "100_c", "099_b") + "Traceback\n"
    assert "last revision started: downgrade 100_c -> 099_b, i.e. 100_c's downgrade()" in described(downgrade)
    to_base = log.format("downgrade", "001_a", "") + "Traceback\n"
    assert "last revision started: downgrade 001_a -> <base>, i.e. 001_a's downgrade()" in described(to_base)


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


@pytest.mark.unit
def test_sql_naming_a_supabase_role_runs_only_under_a_role_existence_check():
    """SQL naming ``anon`` / ``authenticated`` / ``service_role`` must not assume the role exists.

    Those roles are Supabase's. On any other Postgres -- CI, local Docker, a self-hosted
    restore -- a statement naming a missing role fails with ``role "anon" does not exist`` and
    aborts the migration, and with it every later one: ``093`` shipped bare and the fresh path
    died there. The chain test cannot see it (SQLite runs no ``postgresql`` branch) and the
    ``scripts/verify_*_postgres.py`` runs that can live only in the non-blocking e2e.yml, so
    this static tripwire is what blocks CI. It is not limited to GRANT / REVOKE: a membership
    grant, a policy's ``TO`` list or ``has_table_privilege('anon', ...)`` resolves the role
    too. A role left to run time in a role position counts as naming one: nothing at parse
    time says it is ``PUBLIC``.

    The checker is proved on synthetic migrations first -- every unguarded shape flagged, every
    guarded one reached AND passed -- so a green result on the real tree means something.
    """
    misclassified = []
    for case, (source, expected) in _ROLE_GUARD_CHECK_CASES.items():
        violations, reached = _unguarded_role_grants(_ROLE_PREAMBLE + source, "synthetic")
        actual = "flagged" if violations else "guarded" if reached else "untracked"
        if actual != expected:
            misclassified.append(f"{case}: expected {expected}, got {actual} {violations}")
    assert not misclassified, "the role-guard check misclassifies:\n  " + "\n  ".join(misclassified)

    scripts = _scripts_run_on_the_fresh_path()
    assert scripts[-1].down_revision == STAMP_BASELINE, "the walk must start at the first revision after the stamp"
    violations, reached = [], 0
    for script in scripts:
        found, count = _unguarded_role_grants(Path(script.path).read_text(), Path(script.path).name)
        violations += found
        reached += count
    assert reached > 0, "no SQL naming a Supabase-only role is reachable from any upgrade(): the walk is broken"
    assert not violations, (
        "SQL naming a Supabase-only role runs with no role-existence check, so the migration aborts on any "
        "Postgres without that role -- wrap it in DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE "
        "rolname = '...') THEN ... END IF; END $$ (or 059's _role_exists):\n  " + "\n  ".join(violations)
    )


@pytest.mark.unit
def test_model_ddl_naming_a_supabase_role_carries_its_own_role_check():
    """Every model literal that names a Supabase-only role states the ``pg_roles`` check itself.

    ``after_create`` listeners run during ``create_all``, the first step of the fresh path and
    of every non-production boot, and execute their DDL exactly as written: no Python guard can
    wrap it, so the guard must be in the SQL. Literal role NAMES only -- see
    ``_model_role_literals`` and the module docstring for what that leaves out.
    """
    misclassified = []
    for case, (source, expected) in _MODEL_ROLE_CASES.items():
        violations, count = _model_role_literals(_MODEL_PREAMBLE + source, "synthetic")
        actual = "flagged" if violations else "guarded" if count else "untracked"
        if actual != expected:
            misclassified.append(f"{case}: expected {expected}, got {actual} {violations}")
    assert not misclassified, "the model role check misclassifies:\n  " + "\n  ".join(misclassified)

    violations, count, files = [], 0, 0
    for path in sorted((BACKEND_DIR / "app" / "models").glob("*.py")):
        found, found_count = _model_role_literals(path.read_text(), path.name)
        violations += found
        count += found_count
        files += bool(found_count)
    # 24 literals in 10 modules as of 113: consumable, continuous_improvement, hank, hank_intake,
    # hank_preferences, hank_teamwork, quote_nesting_draft/run/spacing_policy, stock_piece.
    assert count > 0 and files > 0, "no model literal names a Supabase-only role: the scan or the pattern is broken"
    assert not violations, (
        "model DDL names a Supabase-only role with no pg_roles check in its own SQL, so create_all aborts on any "
        "Postgres without that role -- wrap it in DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE "
        "rolname = '...') THEN ... END IF; END $$:\n  " + "\n  ".join(violations)
    )
