"""Regression guard: every SQLAlchemy model module must be wired into app/models/__init__.py.

Alembic builds ``target_metadata`` from ``from app.models import *`` (see
``alembic/env.py``). A table only registers on ``Base.metadata`` if its defining
module is actually imported by ``app/models/__init__.py``. If someone adds a new
model file but forgets to import it there, ``alembic revision --autogenerate``
silently stops seeing that table -- exactly the drift this test prevents.

Why a static (AST) check instead of inspecting ``Base.metadata`` at runtime:
the test session's conftest imports ``app.main``, which transitively imports
every model via the API endpoints. So ``Base.metadata`` is fully populated at
test time *regardless* of what ``__init__.py`` imports -- a naive
"is this table in ``Base.metadata``" assertion would pass even with the bug.
We therefore compare, purely from source:

    {modules under app/models/ that define a SQLAlchemy table}
        must be a subset of
    {modules imported via ``from .<module> import ...`` in __init__.py}
"""

import ast
from pathlib import Path
from typing import Set

import pytest

MODELS_DIR = Path(__file__).resolve().parents[1] / "app" / "models"
INIT_FILE = MODELS_DIR / "__init__.py"


def _module_defines_table(source: str) -> bool:
    """Return True if the module source defines a SQLAlchemy mapped class.

    A model class either lists ``Base`` among its bases or sets ``__tablename__``
    in its body. AST parsing (rather than a text/grep scan) avoids false matches
    on ``__tablename__`` mentioned only inside comments, docstrings, or strings.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Case 1: a base named ``Base`` (declarative base) -- e.g. ``class X(Base):``
        # or ``class X(Base, TenantMixin):``.
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "Base":
                return True
            if isinstance(base, ast.Attribute) and base.attr == "Base":
                return True
        # Case 2: the class body assigns ``__tablename__`` (covers any indirect
        # base class that still produces a mapped table).
        for stmt in node.body:
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AnnAssign):
                targets = [stmt.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__":
                    return True
    return False


def _table_defining_modules() -> Set[str]:
    """Names of modules under app/models/ that define at least one table."""
    modules = set()
    for path in MODELS_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        if _module_defines_table(path.read_text(encoding="utf-8")):
            modules.add(path.stem)
    return modules


def _modules_imported_in_init() -> Set[str]:
    """Modules imported via ``from .<module> import ...`` in app/models/__init__.py."""
    tree = ast.parse(INIT_FILE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        # ``level == 1`` and a module name == a relative import: ``from .module import X``.
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            # Only the top-level package component matters (model files are flat,
            # but this stays correct if a subpackage is ever introduced).
            imported.add(node.module.split(".")[0])
    return imported


@pytest.mark.unit
def test_models_dir_has_table_defining_modules():
    """Sanity: discovery actually finds model modules (guards against a broken glob)."""
    table_modules = _table_defining_modules()
    assert table_modules, (
        f"No table-defining model modules were discovered under {MODELS_DIR}. "
        "The discovery logic in this test is likely broken."
    )
    # ``company`` is a load-bearing core table that must always exist.
    assert "company" in table_modules


@pytest.mark.unit
def test_every_model_module_is_wired_into_init():
    """Every model module that defines a table must be imported in app/models/__init__.py.

    Otherwise its table never registers on ``Base.metadata`` for alembic's
    autogenerate, and migrations silently drift from the schema.
    """
    table_modules = _table_defining_modules()
    imported_modules = _modules_imported_in_init()

    missing = sorted(table_modules - imported_modules)
    assert not missing, (
        "The following model module(s) define SQLAlchemy tables but are NOT imported in "
        "app/models/__init__.py: "
        + ", ".join(missing)
        + ". Add each one to app/models/__init__.py (e.g. `from .<module> import <Model>`) "
        "so alembic autogenerate sees its tables. Without this, "
        "`alembic revision --autogenerate` will silently miss these tables."
    )
