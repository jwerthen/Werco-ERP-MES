"""GUARD: a Python traceback may never be handed to an HTTP client.

Three handlers in ``app/api/endpoints/bom.py`` (``get_bom``, ``unrelease_bom``,
``add_bom_item``) used to do ``import traceback`` inside their ``except`` block and
splice ``traceback.format_exc()`` into the ``HTTPException`` detail. ``main.py``'s
``http_exception_handler`` passes ``exc.detail`` through verbatim, so a 500 body
shipped absolute filesystem paths, SQLAlchemy/library internals and local frame
contents to **any authenticated caller** — and ``GET /bom/{id}`` is open to every
authenticated user, not just admins.

That contradicted the posture the app already had everywhere else:

===========================================================  ==========================================
the rule                                                     stated by
===========================================================  ==========================================
an unhandled error returns ``{"detail": "Internal server     ``main.py::global_exception_handler``
error"}`` and goes to the log + Sentry
a caught error is logged with its traceback and answered     ``services/pdf_service.py`` (the in-repo
with a generic message                                       precedent this fix copied)
**tomorrow's** handler does the same                         THIS GUARD
===========================================================  ==========================================

The guard is structural: it parses every module under ``backend/app/`` with
:mod:`ast` and asserts a property of the *shape* of the code, so it holds for
handlers that do not exist yet. A value-level test can only pin the three call
sites someone already thought about, and the failure mode here is precisely that
a fourth one gets written — the deleted code was three copies of the same idiom,
which is what a debugging shortcut looks like when it spreads.

**Why AST and not ``git grep``.** ``grep format_exc`` cannot tell the *sink* apart
from the fix: ``logger.error(f"Traceback: {traceback.format_exc()}")`` in
``pdf_service.py`` is the correct pattern and must not be flagged, while
``detail=f"...{traceback.format_exc()}"`` must be. It also cannot follow
``error_detail = f"...{traceback.format_exc()}"`` / ``detail=error_detail``, which
is exactly how ``add_bom_item`` was written. Telling those apart is a job for a
parser. A filesystem ``rglob`` keeps grep's one real advantage — it sees brand-new
**untracked** files, which is when a developer most needs to be told.

===========================================================================
Scope: ``traceback.format_exc()`` now; the ``str(e)`` family is a tracked follow-up
===========================================================================

This guard covers the traceback formatters only. It deliberately does **not** yet
flag the sibling shape ``detail=f"...: {str(e)}"``, of which six live instances
remain in ``app/api/endpoints/`` at the time of writing:

    dxf_parser.analyze_dxf, dxf_parser.preview_dxf,
    qms_standards.upload_pdf_and_extract_clauses (twice: PDF read, AI extraction),
    mrp.create_mrp_run, po_upload._upload_and_extract_document

(re-locate them with ``grep -rn 'detail=f".*str(e)' app/api/endpoints/``)

They are the same class of defect at a lower severity: an exception *message* can
carry a path or an internal identifier, but it does not carry the frame stack. They
are a separate change — each needs its own generic message chosen for a UI that
today surfaces the raw ``detail`` to the user — and folding them in here would mean
shipping this security fix behind six behavioural decisions.

Covering them now would have required an allowlist of those six sites, i.e. a list
whose entries say "this leak is fine" and which the follow-up PR immediately
deletes. An unenforced-but-documented follow-up is honest about the state of the
code; an allowlist looks like enforcement while being the opposite. When those six
are fixed, extend ``_LEAK_SOURCES`` below with the exception-variable shape instead
of adding exemptions.

(``analytics.py``'s ``except ValueError as e: raise HTTPException(404, str(e))`` is
NOT in that population: it catches one narrow, deliberately-worded not-found error
from ``PredictionService`` rather than blanket-``except``-ing and relaying whatever
internals surface. It is a message the code chose, not one it leaked.)

**Known limits, stated rather than hidden.** The scan resolves the ``detail``
expression through f-strings, ``%``/``str.format``, concatenation, dict/list
literals, nested calls, and chains of local-variable rebinding up to
``_REBIND_DEPTH_LIMIT`` hops within the enclosing function — every idiom by which a
traceback and a message get spliced together at the raise site. Three shapes are
outside it: a value that crosses a function boundary (``detail=build_message(exc)``),
which needs data-flow analysis; a detail passed by splat (``HTTPException(**kwargs)``
/ ``HTTPException(*args)``), where there is no ``detail`` expression in the AST to
resolve; and a rebinding chain longer than the depth limit. None of the three is how
the deleted code was written, and all three are more work than pasting
``traceback.format_exc()`` into an f-string, which is the mistake this guards. It
checks ``HTTPException`` (including import aliases); a hand-built ``JSONResponse``
body is not covered, and no handler builds one today.
"""

import ast
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence, Set

import pytest

pytestmark = pytest.mark.unit

# backend/tests/ -> backend/ -> repo root
_BACKEND = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND.parent
_APP = _BACKEND / "app"


class Violation(NamedTuple):
    """One offending expression, rendered for a failure message."""

    location: str
    reason: str
    source: str

    def __str__(self) -> str:  # pragma: no cover - exercised only on failure
        return f"{self.location}  {self.reason}\n        {self.source}"


class ScanResult(NamedTuple):
    violations: List[Violation]
    # Count of things the scan *accepted*, so a positive control can prove the walk is
    # actually reaching the code it claims to check rather than parsing air.
    accepted: int


# ---------------------------------------------------------------------------
# AST machinery
#
# Deliberately self-contained rather than imported from
# ``test_escape_at_sink_guards.py``: ``backend/tests/`` is not a package, so a
# cross-module import there depends on pytest's sys.path insertion and would make
# this guard fail for a reason that has nothing to do with what it guards.
# ---------------------------------------------------------------------------


def _iter_app_sources() -> Iterator[Path]:
    """Every Python module under ``backend/app/`` — untracked files included."""
    for path in sorted(_APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _annotate_parents(tree: ast.AST) -> None:
    """Give every node a ``_guard_parent`` link so a call can find its enclosing scope."""
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._guard_parent = parent  # type: ignore[attr-defined]


def _enclosing_scope(node: ast.AST) -> Optional[ast.AST]:
    current = getattr(node, "_guard_parent", None)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            return current
        current = getattr(current, "_guard_parent", None)
    return None


def _callee_name(func: ast.expr) -> Optional[str]:
    """``foo(...)`` -> ``"foo"``; ``mod.foo(...)`` / ``self.foo(...)`` -> ``"foo"``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _source_line(lines: Sequence[str], lineno: int) -> str:
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return "<source unavailable>"  # pragma: no cover


def _is_str_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


# ===========================================================================
# The scan
# ===========================================================================

# The client-facing error sink. ``detail`` reaches the response body untouched via
# main.py::http_exception_handler, so this is the one place a leak becomes a body.
_HTTP_ERROR_SINKS = ("HTTPException",)

# ``HTTPException(status_code, detail=None, headers=None)`` -- the positional slot a
# caller writing ``HTTPException(500, "boom")`` lands on.
_DETAIL_POSITIONAL_SLOT = 1

# ``traceback``'s frame-formatting helpers. Every one of these renders absolute file
# paths and source lines; ``print_*`` writes to a stream and returns None, but a call
# to one inside a detail expression is a mistake worth failing on either way.
# ``format_exception_only`` is absent on purpose: it renders "TypeError: x" with no
# frames, so it belongs to the lower-severity ``str(e)`` family documented above.
_LEAK_SOURCES = {
    "format_exc",
    "format_exception",
    "format_tb",
    "format_stack",
    "extract_tb",
    "extract_stack",
    "print_exc",
    "print_exception",
    "print_stack",
    # ``TracebackException.from_exception(exc).format()`` renders the same frames by the
    # object API. Matched on the constructor, not on ``.format()`` -- that is far too
    # common a method name to flag, and no other ``from_exception`` exists in this app.
    "from_exception",
}

# Guards against runaway recursion on self-referential rebinding (``msg = msg + x``).
_REBIND_DEPTH_LIMIT = 4

_REMEDIATION = (
    "A formatted Python traceback must never be placed in an HTTPException detail: "
    "main.py's http_exception_handler returns exc.detail to the client verbatim, so the "
    "response body would carry absolute filesystem paths, SQLAlchemy/library internals and "
    "local frame contents to any authenticated caller (GET /bom/{id} is open to every "
    "authenticated user). Log it instead and answer with a static message -- "
    "`logger.exception(\"Error doing X %s\", some_id)` then "
    '`raise HTTPException(status_code=500, detail="Error doing X")`. logger.exception '
    "attaches the traceback to the log record on its own, and main.py's global handler "
    "already captures unhandled errors to Sentry, so nothing is lost for debugging. "
    "See app/services/pdf_service.py for the in-repo precedent. Do NOT silence this test."
)


def _sink_names(tree: ast.Module) -> Set[str]:
    """Local names for the error sink, including ``import ... as`` aliases.

    ``from fastapi import HTTPException as HE`` must not be a way out. The attribute
    form (``fastapi.HTTPException(...)``) is already covered because :func:`_callee_name`
    returns the bare attribute name.
    """
    names = set(_HTTP_ERROR_SINKS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name in _HTTP_ERROR_SINKS and alias.asname:
                    names.add(alias.asname)
    return names


def _string_bindings(scope: ast.AST) -> Dict[str, List[ast.expr]]:
    """Map ``name -> [assigned expressions]`` within one function (or module) scope.

    Deliberately an over-approximation: it also collects assignments made in nested
    functions and in other branches, which can only ever cause an *extra* expression
    to be checked, never a missed one.
    """
    bindings: Dict[str, List[ast.expr]] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets: Sequence[ast.expr] = node.targets
            value: Optional[ast.expr] = node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, []).append(value)
    return bindings


def _is_leak_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _callee_name(node.func) in _LEAK_SOURCES


def _leaked_traceback_nodes(
    expr: ast.expr,
    bindings: Dict[str, List[ast.expr]],
    depth: int = 0,
) -> List[ast.expr]:
    """Traceback-formatter calls reachable from ``expr``.

    Walking the whole sub-tree (rather than enumerating string-building shapes one by
    one) covers f-strings, %-formatting, ``str.format``, concatenation, ternaries,
    dict/list details and nested calls in a single rule. Names are followed one level
    at a time through the enclosing scope's assignments, which is what catches the
    ``error_detail = f"...{traceback.format_exc()}"`` / ``detail=error_detail`` shape
    ``add_bom_item`` was actually written in.
    """
    if depth > _REBIND_DEPTH_LIMIT:
        return []

    found: List[ast.expr] = []
    for node in ast.walk(expr):
        if _is_leak_call(node):
            found.append(node)  # type: ignore[arg-type]
        elif isinstance(node, ast.Name):
            for bound in bindings.get(node.id, []):
                found.extend(_leaked_traceback_nodes(bound, bindings, depth + 1))

    # A name reachable by two paths would otherwise be reported twice.
    unique: Dict[int, ast.expr] = {id(node): node for node in found}
    return list(unique.values())


def _detail_argument(call: ast.Call) -> Optional[ast.expr]:
    """What this ``HTTPException(...)`` passes as ``detail``, or ``None``."""
    for keyword in call.keywords:
        if keyword.arg == "detail":
            return keyword.value
    if len(call.args) > _DETAIL_POSITIONAL_SLOT and not isinstance(call.args[_DETAIL_POSITIONAL_SLOT], ast.Starred):
        return call.args[_DETAIL_POSITIONAL_SLOT]
    return None


def scan_http_error_details(source: str, label: str) -> ScanResult:
    """Check one module's source. ``accepted`` counts the error details the scan saw."""
    tree = ast.parse(source)
    _annotate_parents(tree)
    lines = source.splitlines()
    sink_names = _sink_names(tree)

    violations: List[Violation] = []
    accepted = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node.func) not in sink_names:
            continue
        detail = _detail_argument(node)
        if detail is None:
            continue
        accepted += 1

        scope = _enclosing_scope(node) or tree
        bindings = _string_bindings(scope)
        for offender in _leaked_traceback_nodes(detail, bindings):
            lineno = getattr(offender, "lineno", node.lineno)
            violations.append(
                Violation(
                    f"{label}:{lineno}",
                    f"{_callee_name(offender.func)}() reaches an HTTPException detail",  # type: ignore[attr-defined]
                    _source_line(lines, lineno),
                )
            )

    return ScanResult(violations, accepted)


def scan_app_http_error_details() -> ScanResult:
    violations: List[Violation] = []
    accepted = 0
    for path in _iter_app_sources():
        result = scan_http_error_details(path.read_text(), str(path.relative_to(_REPO_ROOT)))
        violations.extend(result.violations)
        accepted += result.accepted
    return ScanResult(violations, accepted)


# --- round-trip corpus ---------------------------------------------------------
#
# Each entry is a self-contained module body the scanner MUST flag, in the shape the
# mistake would realistically be made in. Without these the scanner could silently
# stop matching anything and every assertion below would pass.

_BAD_SAMPLES = {
    # The literal shape deleted from get_bom / unrelease_bom.
    "f-string-hole": 'raise HTTPException(status_code=500, detail=f"Error getting BOM: {traceback.format_exc()}")',
    # The literal shape deleted from add_bom_item: built into a local first.
    "built-into-a-local-first": (
        "def add_item(bom_id):\n"
        "    try:\n"
        "        return work()\n"
        "    except Exception as e:\n"
        '        error_detail = f"Error adding BOM item: {str(e)}\\n{traceback.format_exc()}"\n'
        "        raise HTTPException(status_code=500, detail=error_detail)\n"
    ),
    "positional-detail": "raise HTTPException(500, traceback.format_exc())",
    "bare-detail-name": "raise HTTPException(status_code=500, detail=traceback.format_exc())",
    "from-import-form": (
        "from traceback import format_exc\n" 'raise HTTPException(status_code=500, detail=f"boom {format_exc()}")\n'
    ),
    "concatenation": 'raise HTTPException(status_code=500, detail="Error: " + traceback.format_exc())',
    "printf-style": 'raise HTTPException(status_code=500, detail="Error: %s" % traceback.format_exc())',
    "str-format": 'raise HTTPException(status_code=500, detail="Error: {}".format(traceback.format_exc()))',
    "structured-dict-detail": (
        'raise HTTPException(status_code=500, detail={"message": "boom", "trace": traceback.format_exc()})'
    ),
    "aliased-import": (
        "from fastapi import HTTPException as HE\n" 'raise HE(status_code=500, detail=f"{traceback.format_exc()}")\n'
    ),
    "attribute-call": 'raise fastapi.HTTPException(status_code=500, detail=f"{traceback.format_exc()}")',
    "format_exception-variant": ('raise HTTPException(status_code=500, detail="".join(traceback.format_exception(e)))'),
    # The object API for the same frames.
    "tracebackexception-object": (
        "def handler(e):\n"
        "    text = ''.join(traceback.TracebackException.from_exception(e).format())\n"
        "    raise HTTPException(status_code=500, detail=text)\n"
    ),
    "two-hops-through-locals": (
        "def handler():\n"
        "    try:\n"
        "        return work()\n"
        "    except Exception:\n"
        "        tb = traceback.format_exc()\n"
        '        message = f"failed: {tb}"\n'
        "        raise HTTPException(status_code=500, detail=message)\n"
    ),
}

# Shapes that are legitimate and MUST NOT be flagged. If the scanner ever starts
# flagging one of these it has become a tax on correct code rather than a guard.
_GOOD_SAMPLES = {
    # The fix itself, verbatim.
    "logged-then-generic-detail": (
        "def get_bom(bom_id):\n"
        "    try:\n"
        "        return work()\n"
        "    except HTTPException:\n"
        "        raise\n"
        "    except Exception:\n"
        '        logger.exception("Error getting BOM %s", bom_id)\n'
        '        raise HTTPException(status_code=500, detail="Error getting BOM")\n'
    ),
    # The traceback is captured -- but into the LOG, not into the response.
    "traceback-into-the-log-only": (
        "def handler():\n"
        "    try:\n"
        "        return work()\n"
        "    except Exception:\n"
        '        logger.error("Traceback: %s", traceback.format_exc())\n'
        '        raise HTTPException(status_code=500, detail="Internal server error")\n'
    ),
    # app/services/pdf_service.py's real shape: formats a traceback, raises nothing.
    "pdf-service-precedent": 'logger.error(f"[DOCX] Traceback: {traceback.format_exc()}")',
    "interpolated-but-safe": 'raise HTTPException(status_code=404, detail=f"BOM {bom_id} not found")',
    "structured-conflict-detail": (
        'raise HTTPException(status_code=409, detail={"code": "backflush_blocked", "bom_id": bom_id})'
    ),
    "status-code-only": "raise HTTPException(status_code=403)",
    # `detail` is the second POSITIONAL argument; headers is the third and is not it.
    "headers-are-not-the-detail": (
        'raise HTTPException(status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})'
    ),
}


def _samples(corpus: Dict[str, str]) -> List["pytest.ParameterSet"]:
    """Parametrize over a corpus, keeping the sample name as the (readable) test id."""
    return [pytest.param(source, id=name) for name, source in sorted(corpus.items())]


def test_app_package_exists():
    """Premise check: the scan below must not pass by scanning nothing."""
    assert _APP.is_dir(), f"expected the backend package at {_APP}"
    assert any(_iter_app_sources()), f"no Python sources found under {_APP}"


@pytest.mark.parametrize("source", _samples(_BAD_SAMPLES))
def test_scanner_flags_known_bad_shapes(source: str):
    """Round-trip: prove the scanner detects each way of leaking a traceback."""
    result = scan_http_error_details(source, "<sample>")

    assert result.violations, f"the scanner did NOT flag this known-bad sample:\n{source}"


@pytest.mark.parametrize("source", _samples(_GOOD_SAMPLES))
def test_scanner_accepts_known_good_shapes(source: str):
    """The other half of the round trip: no false positive on correct code."""
    result = scan_http_error_details(source, "<sample>")

    assert not result.violations, "the scanner falsely flagged this legitimate shape:\n{}\n  {}".format(
        source, "\n  ".join(str(v) for v in result.violations)
    )


def test_the_scan_actually_reaches_the_endpoint_handlers():
    """Positive control: the walk finds the real, already-correct raise sites.

    Without this a broken path, a renamed package or a scanner that stopped
    recognising ``HTTPException`` would make the guard below vacuously green.
    """
    result = scan_app_http_error_details()

    assert result.accepted >= 500, (
        f"the scan found only {result.accepted} HTTPException details under {_APP}; there are "
        f"~890 HTTPException call sites in this backend, so the scan is broken, not clean"
    )


def test_no_traceback_reaches_an_http_error_response():
    """THE GUARD. A formatted traceback is log/Sentry material, never a response body."""
    result = scan_app_http_error_details()

    assert not result.violations, "a formatted traceback reaches an HTTP response body:\n    {}\n\n{}".format(
        "\n    ".join(str(v) for v in result.violations), _REMEDIATION
    )
