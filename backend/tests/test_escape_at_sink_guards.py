"""Structural guards for the two *unenforced* halves of the escape-at-the-sink decision.

Ingest-time HTML sanitization was removed from this backend (``bleach`` and the
body-rewriting middleware are gone; see ``docs/SECURITY_ADVISORY_SUPPRESSIONS.md``
and the long argument in ``tests/test_frontend_no_raw_html_render_guard.py``).
Storing markup verbatim is safe **only** because nothing interprets it, and the
one place that does is escaped at the point of render. That claim has three
parts, and until this file existed only two of them were enforced:

===========================================================  ==========================================
claim                                                        enforced by
===========================================================  ==========================================
the SPA renders no raw HTML                                  ``test_frontend_no_raw_html_render_guard``
today's ``Paragraph`` interpolations are escaped             ``test_pdf_text_escaping``
**tomorrow's** ``Paragraph`` interpolations are escaped      GUARD 1, below
**no caller ever uses the raw-``body`` HTML email path**     GUARD 2, below
===========================================================  ==========================================

Both new guards are structural: they parse every module under ``backend/app/``
with :mod:`ast` and assert a property of the *shape* of the code, so they hold
for source that does not exist yet. That is the entire point — a value-level
test can only pin the call sites someone already thought about.

**Why AST and not ``git grep``.** The sibling frontend guard greps because its
targets (``dangerouslySetInnerHTML``, ``.innerHTML =``) are lexical tokens in a
language we have no parser for here. These targets are not lexical: telling
``Paragraph(f"<b>C:</b> {pdf_escape(name)}")`` apart from
``Paragraph(f"<b>C:</b> {name}")`` — while *also* accepting the literal-only
``Paragraph("<b>Heading</b>")`` and the bare-name ``Paragraph(pdf_escape(s))``,
and while *not* flagging the deliberately-unescaped
``SimpleDocTemplate(title=f"Quote {n}")`` — is a job for a parser, not a regex.
The one property grep has that a naive walk lacks is that ``--untracked`` sees
brand-new files; a filesystem ``rglob`` gets that for free, which matters
exactly when it matters most: a not-yet-committed new PDF service.

**Neither failure here is a lint nit.** A GUARD 1 failure is a live HTTP 500 on
a quote/CoC download the moment a customer name contains ``&`` or a note
contains ASME Y14.5 notation (``<REF>``, ``<TYP>``, ``<MMC>``) — plus a
records-integrity defect on a compliance artifact when markup renders as
something other than what is stored. A GUARD 2 failure is a live HTML-injection
sink in outbound email. Fix the code; do not add an exemption, and do not put
ingest-time body mutation back.

**Known limit, stated rather than hidden.** GUARD 1 checks *dynamic string
construction* reaching a markup sink: f-strings, ``%``-formatting,
``str.format``, ``+`` concatenation, and one level of local variable
rebinding. It cannot follow a value across a function boundary
(``Paragraph(build_header(note))``), because that needs data-flow analysis, not
a syntax tree. What it does cover is every idiom by which a template string and
a record field get spliced together at the sink itself, which is how every one
of today's interpolations is written and how the next one will be.
"""

import ast
import inspect
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence, Set, Tuple

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
    # Count of things the scan *accepted*, so a positive control can prove the walk
    # is actually reaching the code it claims to be checking rather than parsing air.
    accepted: int


# ---------------------------------------------------------------------------
# Shared AST machinery
# ---------------------------------------------------------------------------


def _iter_app_sources() -> Iterator[Path]:
    """Every Python module under ``backend/app/``.

    A plain filesystem walk rather than ``git grep``: it sees **untracked** files
    inherently, which is the property the sibling frontend guard has to ask for
    with ``--untracked``. A brand-new, not-yet-``git add``-ed PDF service is
    precisely the case where a developer runs the suite and needs to be told.
    """
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


def _is_none_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_str_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _param_names(func: ast.AST) -> Set[str]:
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    args = func.args
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


# ===========================================================================
# GUARD 1 -- every interpolation into a markup-parsing reportlab sink is escaped
# ===========================================================================

# reportlab flowables whose text argument is fed to ``paraparser`` (the mini-HTML
# dialect: <b>, <i>, <font>, <br/>, &nbsp; ...). ``Paragraph`` is the one in use;
# ``XPreformatted`` parses the same dialect and is listed so adopting it does not
# silently open a second sink. ``Preformatted`` parses nothing and is not a sink.
_MARKUP_SINKS = ("Paragraph", "XPreformatted")

_ESCAPER = "pdf_escape"

# Guards against runaway recursion on self-referential rebinding (``msg = msg + x``).
_REBIND_DEPTH_LIMIT = 4

_GUARD_1_REMEDIATION = (
    "Every value spliced into a reportlab Paragraph must go through "
    "app.services.pdf_text.pdf_escape -- e.g. f\"<b>Note:</b> {pdf_escape(note)}\", wrapping the "
    "VALUE and never the surrounding literal markup (wrap the formatted string for numerics: "
    "pdf_escape(f'{qty:g}')). Paragraph parses a mini-HTML dialect, so an unescaped value is not "
    "cosmetic: Paragraph('Check OD<ID before press', style) raises ValueError('paraparser: syntax "
    "error: parse ended with 1 unclosed tags'), i.e. an HTTP 500 on a quote or Certificate-of-"
    "Conformance download triggered by an ordinary part note. Escaped values that merely render "
    "wrong are a records-integrity defect on a compliance artifact. This sink is the load-bearing "
    "half of the decision to stop sanitizing request bodies at ingest -- see "
    "docs/SECURITY_ADVISORY_SUPPRESSIONS.md and app/services/pdf_text.py. Do NOT silence this test."
)


def _is_escape_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _callee_name(node.func) == _ESCAPER


def _sink_names(tree: ast.Module) -> Set[str]:
    """Local names that refer to a markup sink, including ``import ... as`` aliases.

    ``from reportlab.platypus import Paragraph as P`` must not be a way out, and
    the attribute form (``platypus.Paragraph(...)``) is covered because
    :func:`_callee_name` returns the bare attribute name.
    """
    names = set(_MARKUP_SINKS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name in _MARKUP_SINKS and alias.asname:
                    names.add(alias.asname)
    return names


def _string_bindings(scope: ast.AST) -> Dict[str, List[ast.expr]]:
    """Map ``name -> [assigned expressions]`` within one function (or module) scope.

    Deliberately an over-approximation: it also collects assignments made in nested
    functions, which can only ever cause an *extra* expression to be checked, never
    a missed one.
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


def _unescaped_holes(expr: ast.expr, bindings: Dict[str, List[ast.expr]], depth: int = 0) -> List[Tuple[ast.expr, str]]:
    """Sub-expressions of ``expr`` that splice a value into a string without escaping it.

    Returns ``(offending_node, reason)`` pairs. An expression already wrapped in
    ``pdf_escape(...)`` terminates the descent, which is what makes the nested
    ``pdf_escape(f'{total:,.2f}')`` shape legitimate: the inner f-string hole is
    inside an escape call, so it is never inspected.
    """
    if depth > _REBIND_DEPTH_LIMIT:
        return []
    if _is_escape_call(expr):
        return []

    if isinstance(expr, ast.JoinedStr):
        return [
            (part.value, "f-string interpolation not wrapped in pdf_escape()")
            for part in expr.values
            if isinstance(part, ast.FormattedValue) and not _is_escape_call(part.value)
        ]

    if isinstance(expr, ast.BinOp):
        if isinstance(expr.op, ast.Mod) and _is_str_constant(expr.left):
            return [(expr, "printf-style %-interpolation into markup, not wrapped in pdf_escape()")]
        if isinstance(expr.op, ast.Add):
            return _unescaped_holes(expr.left, bindings, depth) + _unescaped_holes(expr.right, bindings, depth)
        return []

    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr in {"format", "format_map"}
        and _is_str_constant(expr.func.value)
    ):
        return [(expr, "str.format() interpolation into markup, not wrapped in pdf_escape()")]

    if isinstance(expr, ast.IfExp):
        return _unescaped_holes(expr.body, bindings, depth) + _unescaped_holes(expr.orelse, bindings, depth)

    if isinstance(expr, ast.Name):
        # One level of "built the string into a local, then passed the local".
        found: List[Tuple[ast.expr, str]] = []
        for bound in bindings.get(expr.id, []):
            found.extend(_unescaped_holes(bound, bindings, depth + 1))
        return found

    return []


def _sink_text_argument(call: ast.Call) -> Optional[ast.expr]:
    """The first (``text``) argument of a ``Paragraph``-like call.

    Only argument 0 matters: ``Paragraph(text, style=None, bulletText=None, ...)``.
    The style argument is routinely a subscript like ``styles["Normal"]`` and is
    never parsed as markup.
    """
    for keyword in call.keywords:
        if keyword.arg == "text":
            return keyword.value
    if call.args and not isinstance(call.args[0], ast.Starred):
        return call.args[0]
    return None


def scan_markup_sinks(source: str, label: str) -> ScanResult:
    """Check one module's source. ``accepted`` counts the ``pdf_escape`` calls found."""
    tree = ast.parse(source)
    _annotate_parents(tree)
    lines = source.splitlines()
    sink_names = _sink_names(tree)

    violations: List[Violation] = []
    accepted = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node.func) not in sink_names:
            continue
        text_arg = _sink_text_argument(node)
        if text_arg is None:
            continue

        accepted += sum(1 for child in ast.walk(text_arg) if _is_escape_call(child))

        scope = _enclosing_scope(node) or tree
        bindings = _string_bindings(scope)
        for offender, reason in _unescaped_holes(text_arg, bindings):
            lineno = getattr(offender, "lineno", node.lineno)
            violations.append(Violation(f"{label}:{lineno}", reason, _source_line(lines, lineno)))

    return ScanResult(violations, accepted)


def scan_app_markup_sinks() -> ScanResult:
    violations: List[Violation] = []
    accepted = 0
    for path in _iter_app_sources():
        result = scan_markup_sinks(path.read_text(), str(path.relative_to(_REPO_ROOT)))
        violations.extend(result.violations)
        accepted += result.accepted
    return ScanResult(violations, accepted)


# --- GUARD 1 round-trip corpus -------------------------------------------------
#
# Each entry is a self-contained module body that the scanner MUST flag, in the
# shape the mistake would realistically be made in. Without these the scanner
# could silently stop matching anything and every assertion below would pass.

_GUARD_1_BAD_SAMPLES = {
    "plain-f-string-hole": 'Paragraph(f"<b>Note:</b> {note}", styles["Normal"])',
    "one-escaped-hole-one-not": 'Paragraph(f"<b>{pdf_escape(label)}:</b> {note}", styles["Normal"])',
    "printf-style": 'Paragraph("<b>Note:</b> %s" % note, styles["Normal"])',
    "str-format": 'Paragraph("<b>Note:</b> {}".format(note), styles["Normal"])',
    "concatenation": 'Paragraph("<b>Note:</b> " + f"{note}", styles["Normal"])',
    "text-keyword": 'Paragraph(text=f"<b>Note:</b> {note}", style=styles["Normal"])',
    "aliased-import": (
        "from reportlab.platypus import Paragraph as P\n" 'P(f"<b>Note:</b> {note}", styles["Normal"])\n'
    ),
    "attribute-call": 'platypus.Paragraph(f"<b>Note:</b> {note}", styles["Normal"])',
    "xpreformatted": 'XPreformatted(f"<b>Note:</b> {note}", styles["Normal"])',
    "built-into-a-local-first": (
        "def build(note):\n" '    header = f"<b>Note:</b> {note}"\n' '    return Paragraph(header, styles["Normal"])\n'
    ),
    "ternary-branch": 'Paragraph(f"{pdf_escape(a)}" if flag else f"{b}", styles["Normal"])',
}

# Shapes that are legitimate and MUST NOT be flagged. Every one of these appears in
# app/services/{quote,coc}_pdf_service.py today; if the scanner ever starts flagging
# one of them it has become a tax on correct code rather than a guard.
_GUARD_1_GOOD_SAMPLES = {
    "literal-only-markup": 'Paragraph("<b>Certificate of Conformance</b>", styles["Title"])',
    "bare-escaped-name": 'Paragraph(pdf_escape(statement), styles["Normal"])',
    "escaped-hole": 'Paragraph(f"<b>Customer:</b> {pdf_escape(customer_name)}", styles["Normal"])',
    # The format spec must keep applying, so the *formatted* string is what gets
    # escaped. The inner hole lives inside a pdf_escape call and is not a violation.
    "escaped-nested-f-string": "Paragraph(f\"<b>Total:</b> ${pdf_escape(f'{total:,.2f}')}\", styles[\"Heading3\"])",
    # reportlab escapes PDF string syntax for document metadata itself; this f-string
    # is deliberately unescaped and must not be mistaken for a Paragraph.
    "simpledoctemplate-title": 'SimpleDocTemplate(buffer, title=f"Quote {quote_number}")',
    # Table cells are plain strings, never run through paraparser.
    "table-row": 'table_rows.append([f"{part}", f"{qty:g}", f"{material}"])',
    "style-subscript-is-not-text": 'Paragraph("<b>x</b>", styles[f"{style_name}"])',
    "escaped-concatenation": 'Paragraph("<b>Note:</b> " + pdf_escape(note), styles["Normal"])',
}


def _samples(corpus: Dict[str, str]) -> List["pytest.ParameterSet"]:
    """Parametrize over a corpus, keeping the sample name as the (readable) test id."""
    return [pytest.param(source, id=name) for name, source in sorted(corpus.items())]


def test_app_package_exists():
    """Premise check: the scans below must not pass by scanning nothing."""
    assert _APP.is_dir(), f"expected the backend package at {_APP}"
    assert any(_iter_app_sources()), f"no Python sources found under {_APP}"


@pytest.mark.parametrize("source", _samples(_GUARD_1_BAD_SAMPLES))
def test_markup_sink_scanner_flags_known_bad_shapes(source: str):
    """Round-trip: prove the scanner detects each way of reintroducing the sink."""
    result = scan_markup_sinks(source, "<sample>")

    assert result.violations, f"the scanner did NOT flag this known-bad sample:\n{source}"


@pytest.mark.parametrize("source", _samples(_GUARD_1_GOOD_SAMPLES))
def test_markup_sink_scanner_accepts_known_good_shapes(source: str):
    """The other half of the round trip: no false positive on correct code."""
    result = scan_markup_sinks(source, "<sample>")

    assert not result.violations, "the scanner falsely flagged this legitimate shape:\n{}\n  {}".format(
        source, "\n  ".join(str(v) for v in result.violations)
    )


def test_the_markup_sink_scan_actually_reaches_the_pdf_builders():
    """Positive control: the walk finds the real, already-correct call sites.

    Without this a broken path, a renamed package or a scanner that stopped
    recognising ``Paragraph`` would make the guard below vacuously green.
    """
    result = scan_app_markup_sinks()

    assert result.accepted >= 20, (
        f"the scan found only {result.accepted} pdf_escape-wrapped values inside Paragraph calls "
        f"under {_APP}; the two PDF builders contain ~24, so the scan is broken, not clean"
    )


def test_every_markup_sink_interpolation_is_escaped():
    """GUARD 1. No unescaped value may be spliced into a reportlab markup sink."""
    result = scan_app_markup_sinks()

    assert not result.violations, "unescaped interpolation into a reportlab markup sink:\n    {}\n\n{}".format(
        "\n    ".join(str(v) for v in result.violations), _GUARD_1_REMEDIATION
    )


# ===========================================================================
# GUARD 2 -- nothing may use the raw-`body` (unescaped HTML) email path
# ===========================================================================
#
# ``EmailService.send_email(to, subject, body=None, template=None, context=None,
# html=True)`` has two mutually exclusive body paths:
#
#   template=...  ->  Jinja2 Environment(autoescape=select_autoescape(['html','xml']))
#   body=...      ->  html_body = body  ->  MIMEText(html_body, "html")   # NO escaping
#
# Autoescape protects only the first. The second hands a caller-composed string
# straight to an HTML MIME part. No caller uses it today; this keeps it that way.
#
# The chain that reaches ``send_email`` is:
#
#   notification_dispatch._enqueue_email  --enqueue_job("send_email_job", body=None, ...)-->
#   worker.send_email_job(ctx, to, subject, body, ...)  --positional-->
#   email_jobs.send_email_task(to, subject, body, ...)  --body=body-->
#   EmailService.send_email(...)
#
# The two middle frames forward ``body`` verbatim, so a rule of "no call passes a
# non-None body" would flag them. They are not exempted by name. The rule is
# inductive instead: a bare pass-through of the enclosing function's own ``body``
# parameter is allowed **only when that enclosing function's own call sites are
# themselves scanned by this guard** (i.e. its name is in ``_EMAIL_BODY_SLOTS``).
# Origination must be a literal ``None``; forwarding is only legal between guarded
# frames; therefore the chain is closed. A brand-new
# ``async def notify(body): await send_email(..., body=body)`` is NOT a guarded
# frame and is flagged.

# callee name -> index of `body` in its POSITIONAL arguments, as callers write them.
_EMAIL_BODY_SLOTS = {
    "send_email": 2,  # bound method: (to, subject, body, template, context, html)
    "send_email_task": 2,  # (to, subject, body, template, context)
    "send_email_job": 3,  # (ctx, to, subject, body, template, context)
}

# Enqueued by name rather than called: enqueue_job("send_email_job", ...). The job-name
# string occupies argument 0, exactly where ARQ later injects ``ctx``, so the positional
# slot is the same one as a direct call.
_EMAIL_JOB_NAME = "send_email_job"
_EMAIL_JOB_BODY_SLOT = _EMAIL_BODY_SLOTS["send_email_job"]
_ENQUEUE_CALLEES = {
    "enqueue_job",
    "enqueue_job_best_effort",
    "enqueue_job_fire_and_forget_fastfail",
    "enqueue",
}

_GUARD_2_REMEDIATION = (
    "EmailService.send_email's raw `body=` path bypasses Jinja2 autoescape: it assigns "
    "html_body = body and attaches it as MIMEText(html_body, 'html') with no escaping, so any "
    "record field spliced into that string is an HTML-injection sink in outbound mail. Jinja "
    "autoescape only covers the `template=` path. Send through a template instead (pass the "
    "values in `context=` and let the template escape them), or -- if a genuinely plain-text "
    "email is wanted -- change send_email so the raw-body path attaches text/plain only, and "
    "update this guard with it. This matters because request bodies are no longer sanitized at "
    "ingest, so stored strings may contain arbitrary markup: see "
    "docs/SECURITY_ADVISORY_SUPPRESSIONS.md. Do NOT silence this test."
)


def _body_argument(call: ast.Call, positional_slot: int) -> Optional[Tuple[ast.expr, str]]:
    """Resolve what a call passes as ``body``. Returns ``(node, how)`` or ``None``.

    An explicit ``body=`` wins over a ``**kwargs`` splat in the same call: Python
    permits ``send_email(**base, body=None)``, and there the keyword is the value
    (a ``base`` that also carried ``body`` would be a duplicate-argument TypeError).
    Checking the splat first would flag that as unresolvable, which is a false
    positive on a call that states plainly it wants no raw body.
    """
    for keyword in call.keywords:
        if keyword.arg == "body":
            return keyword.value, "body="
    for keyword in call.keywords:
        if keyword.arg is None:
            # `send_email(**payload)` -- the body cannot be resolved statically.
            return keyword.value, "**kwargs splat"
    if len(call.args) > positional_slot:
        return call.args[positional_slot], f"positional argument {positional_slot}"
    return None


def _is_guarded_passthrough(node: ast.expr, scope: Optional[ast.AST]) -> bool:
    """``body=body`` inside a frame whose own callers this guard also checks."""
    return (
        isinstance(node, ast.Name)
        and node.id == "body"
        and isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
        and scope.name in _EMAIL_BODY_SLOTS
        and "body" in _param_names(scope)
    )


def _email_body_slot_for(call: ast.Call) -> Optional[int]:
    """The positional ``body`` slot for this call, or ``None`` if it is not an email send."""
    name = _callee_name(call.func)
    if name in _EMAIL_BODY_SLOTS:
        return _EMAIL_BODY_SLOTS[name]
    if name in _ENQUEUE_CALLEES and call.args and _is_str_constant(call.args[0]):
        if call.args[0].value == _EMAIL_JOB_NAME:  # type: ignore[attr-defined]
            return _EMAIL_JOB_BODY_SLOT
    return None


def scan_email_body_path(source: str, label: str) -> ScanResult:
    """Check one module's source. ``accepted`` counts the email sends the scan saw."""
    tree = ast.parse(source)
    _annotate_parents(tree)
    lines = source.splitlines()

    violations: List[Violation] = []
    accepted = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        slot = _email_body_slot_for(node)
        if slot is None:
            continue
        accepted += 1

        supplied = _body_argument(node, slot)
        if supplied is None:
            continue  # body omitted entirely -- template-only send
        value, how = supplied

        if _is_none_literal(value):
            continue
        if _is_guarded_passthrough(value, _enclosing_scope(node)):
            continue

        lineno = getattr(value, "lineno", node.lineno)
        violations.append(
            Violation(
                f"{label}:{lineno}",
                f"passes a non-None {how} into the unescaped raw-body HTML email path",
                _source_line(lines, lineno),
            )
        )

    return ScanResult(violations, accepted)


def scan_app_email_body_path() -> ScanResult:
    violations: List[Violation] = []
    accepted = 0
    for path in _iter_app_sources():
        result = scan_email_body_path(path.read_text(), str(path.relative_to(_REPO_ROOT)))
        violations.extend(result.violations)
        accepted += result.accepted
    return ScanResult(violations, accepted)


def _body_passthrough_frames(source: str) -> Set[str]:
    """Names of functions that forward a bare ``body`` into an email send.

    Reports every such frame, *including* ones the pass-through allowance would
    reject -- the point is to count who relies on the allowance, not to re-apply it.
    """
    tree = ast.parse(source)
    _annotate_parents(tree)

    frames: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        slot = _email_body_slot_for(node)
        if slot is None:
            continue
        supplied = _body_argument(node, slot)
        if supplied is None:
            continue
        value, _how = supplied
        if not (isinstance(value, ast.Name) and value.id == "body"):
            continue
        scope = _enclosing_scope(node)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            frames.add(scope.name)
    return frames


_GUARD_2_BAD_SAMPLES = {
    "keyword-body": 'await email_service.send_email(to=u.email, subject="Hi", body=rendered_html)',
    "positional-body": 'await email_service.send_email(u.email, "Hi", rendered_html)',
    "f-string-body": 'await email_service.send_email(to=u.email, subject="Hi", body=f"<b>{note}</b>")',
    "via-the-job-task": 'await send_email_task(u.email, "Hi", rendered_html)',
    "via-the-enqueue": 'await enqueue_job("send_email_job", to=u.email, subject="Hi", body=rendered_html)',
    "via-the-enqueue-positional": 'await enqueue_job("send_email_job", u.email, "Hi", rendered_html)',
    "kwargs-splat": "await email_service.send_email(**payload)",
    "unguarded-forwarder": (
        "async def notify(body):\n" '    await email_service.send_email(to="a@b.c", subject="Hi", body=body)\n'
    ),
}

_GUARD_2_GOOD_SAMPLES = {
    "explicit-none": 'await enqueue_job("send_email_job", to=u.email, subject=t, body=None, template="notification")',
    "body-omitted": 'await email_service.send_email(to=u.email, subject=t, template="daily_digest", context=ctx)',
    "guarded-forwarder": (
        "async def send_email_task(to, subject, body=None, template=None, context=None):\n"
        "    return await email_service.send_email(to=to, subject=subject, body=body, template=template)\n"
    ),
    "guarded-positional-forwarder": (
        "async def send_email_job(ctx, to, subject, body, template=None, context=None):\n"
        "    return await send_email_task(to, subject, body, template, context)\n"
    ),
    "a-different-job-entirely": 'await enqueue_job("send_sms_job", body=text)',
    # An explicit body= is the value even alongside a splat; see _body_argument.
    "splat-plus-explicit-none": "await email_service.send_email(**base, body=None)",
}


def test_email_body_positional_slots_match_the_real_signatures():
    """The positional-slot table is an assumption about signatures -- verify it.

    If someone inserts a parameter before ``body``, a positional call would land on
    the wrong slot and GUARD 2 would quietly check the wrong argument. Reading the
    live signatures makes that drift a failure here instead.
    """
    from app.jobs.email_jobs import send_email_task
    from app.services.email_service import EmailService
    from app.worker import send_email_job

    unbound = list(inspect.signature(EmailService.send_email).parameters)
    assert unbound[0] == "self"
    # Callers use the bound method, so drop `self` before comparing.
    assert unbound.index("body") - 1 == _EMAIL_BODY_SLOTS["send_email"], unbound

    task_params = list(inspect.signature(send_email_task).parameters)
    assert task_params.index("body") == _EMAIL_BODY_SLOTS["send_email_task"], task_params

    job_params = list(inspect.signature(send_email_job).parameters)
    assert job_params[0] == "ctx"
    assert job_params.index("body") == _EMAIL_BODY_SLOTS["send_email_job"], job_params
    # The enqueue form puts the job-name string where ARQ injects `ctx`, so the slot matches.
    assert _EMAIL_JOB_BODY_SLOT == job_params.index("body")


def test_only_the_two_known_forwarders_lean_on_the_passthrough_allowance():
    """The pass-through allowance stays a two-frame relay, not a growing exemption list.

    ``_is_guarded_passthrough`` is what stops GUARD 2 from flagging the ARQ relay, and
    an allowance nobody counts is how an exemption list starts. This pins the real
    population of frames that forward ``body`` verbatim in ``app/``: exactly the two
    ARQ hops between ``notification_dispatch``'s ``body=None`` and ``send_email``.

    A third forwarder fails this test even when it is otherwise well-formed, which is
    the intent -- each new link in that relay lengthens the distance between the value
    and the unescaped MIMEText and deserves to be looked at.
    """
    observed = sorted(frame for path in _iter_app_sources() for frame in _body_passthrough_frames(path.read_text()))

    assert observed == ["send_email_job", "send_email_task"], (
        f"the set of frames forwarding `body` verbatim into an email send changed: {observed}. "
        f"Expected exactly the two ARQ relay hops (worker.send_email_job -> "
        f"email_jobs.send_email_task -> EmailService.send_email). {_GUARD_2_REMEDIATION}"
    )


@pytest.mark.parametrize("source", _samples(_GUARD_2_BAD_SAMPLES))
def test_email_body_scanner_flags_known_bad_shapes(source: str):
    result = scan_email_body_path(source, "<sample>")

    assert result.violations, f"the scanner did NOT flag this known-bad sample:\n{source}"


@pytest.mark.parametrize("source", _samples(_GUARD_2_GOOD_SAMPLES))
def test_email_body_scanner_accepts_known_good_shapes(source: str):
    result = scan_email_body_path(source, "<sample>")

    assert not result.violations, "the scanner falsely flagged this legitimate shape:\n{}\n  {}".format(
        source, "\n  ".join(str(v) for v in result.violations)
    )


def test_the_email_body_scan_actually_reaches_the_send_sites():
    """Positive control: the walk finds the real send/enqueue/forward call sites."""
    result = scan_app_email_body_path()

    assert result.accepted >= 3, (
        f"the scan found only {result.accepted} email-send call sites under {_APP}; the dispatch "
        f"enqueue, the ARQ job forward, the task forward and the digest send are all expected, so "
        f"the scan is broken, not clean"
    )


def test_no_caller_uses_the_raw_body_html_email_path():
    """GUARD 2. Outbound HTML email is composed by an autoescaping template, always."""
    result = scan_app_email_body_path()

    assert not result.violations, "a caller now uses the unescaped raw-body email path:\n    {}\n\n{}".format(
        "\n    ".join(str(v) for v in result.violations), _GUARD_2_REMEDIATION
    )
