"""Guard for the **removal of ingest-time HTML sanitization**, documented in
``docs/SECURITY_ADVISORY_SUPPRESSIONS.md``.

This backend used to run every JSON request body through
``bleach.clean(v, tags=[], strip=True)`` in a middleware that rewrote
``request._body`` — so the stripped text was what got **persisted**. That control
was removed, and ``bleach`` left the dependency tree with it. The removal was not
a risk acceptance; it rested on a specific, checkable claim:

    Stored HTML is never interpreted anywhere, so storing it is not dangerous.
    The one place markup *is* interpreted gets escaped at the point of render.

Both halves were audited before the removal:

* **Frontend.** React escapes text nodes on output. The SPA contained zero uses
  of ``dangerouslySetInnerHTML`` and zero direct ``innerHTML`` writes, so a
  ``<script>`` persisted in a part note could not execute in a browser — it
  rendered as those literal characters.
* **Backend sinks.** HTML email renders through a Jinja2 ``Environment`` with
  ``autoescape=select_autoescape(['html', 'xml'])`` and every caller goes through
  a template. Thermal labels use ``canvas.drawString``, which parses nothing.
  reportlab ``Table`` cells take plain strings. The single markup-interpreting
  sink is reportlab ``Paragraph``, which parses a mini-HTML dialect — and every
  interpolation into one now goes through ``pdf_escape``
  (``app/services/pdf_text.py``), covered by ``tests/test_pdf_text_escaping.py``
  for today's call sites and by ``tests/test_escape_at_sink_guards.py`` for the
  ones nobody has written yet (a structural AST scan of ``backend/app/``, which
  also pins that no caller reaches the unescaped raw-``body`` email path).

The cost of *keeping* the sanitizer was concrete: it corrupted manufacturing
records. ASME Y14.5 drawing notation is angle-bracketed (``<REF>``, ``<TYP>``,
``<MMC>``, ``<BASIC>``, ``<MIN>``), so an inspection note reading
``"Dim is 2.500 <REF> per print"`` was silently persisted as
``"Dim is 2.500  per print"``. In an AS9100D / ISO 9001 system that is a
records-integrity defect.

**What this file enforces.** The backend half is pinned by its own tests. This
file pins the frontend half, which is the assumption most likely to be
invalidated by someone who has never heard of it — a Markdown renderer, a
rich-text description field, a charting library wired up with ``innerHTML``. Any
of those reintroduces an XSS sink against data that is deliberately no longer
sanitized on the way in.

**A failure here is not a lint nit.** It means the safety argument above no
longer holds. Fix it by escaping/sanitizing at that new sink (the pattern
``pdf_escape`` follows), or by rendering through React's normal text path —
**not** by adding this path to an exemption list, and **not** by putting
ingest-time body mutation back.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# repo root: backend/tests/ -> backend/ -> repo
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"

# Test files may legitimately mention these names (e.g. asserting a component does
# NOT use them, or a jsdom fixture setting up markup). Production source may not.
_TEST_FILE_PATTERN = re.compile(r"(\.test\.|\.spec\.|__tests__/|__mocks__/|/setupTests\.)")

# The sinks that hand an unparsed string to the HTML parser. `dangerouslySetInnerHTML`
# is React's escape hatch; the rest are the DOM ones. Reading `.innerHTML` is harmless,
# so the assignment forms (`=` and `+=`) are matched specifically.
#
# POSIX ERE ONLY. `git grep -E` does not implement the Perl shorthands — `\s`, `\d`,
# `\w`, `\b` — and does not error on them either, it just silently fails to match. An
# earlier revision of this file used `\s*` and three of these five patterns were
# vacuous. Use `[[:space:]]`, and see `test_every_pattern_matches_a_known_bad_sample`,
# which exists solely to catch that mistake.
_RAW_HTML_SINKS = (
    "dangerouslySetInnerHTML",
    r"\.innerHTML[[:space:]]*\+?=",
    r"\.outerHTML[[:space:]]*\+?=",
    "insertAdjacentHTML",
    r"document\.write[[:space:]]*\(",
)

# One line of source per pattern that the pattern MUST match, in the shape it would
# realistically appear in. Ordered to match _RAW_HTML_SINKS.
_KNOWN_BAD_SAMPLES = (
    'return <div dangerouslySetInnerHTML={{ __html: note }} />;',
    "  el.innerHTML = userNote;",
    "  el.outerHTML += userNote;",
    '  el.insertAdjacentHTML("beforeend", userNote);',
    "  document.write(userNote);",
)

# Perl-only shorthands that git grep's default ERE silently ignores.
_NON_POSIX_SHORTHANDS = (r"\s", r"\d", r"\w", r"\b", r"\S", r"\D", r"\W")

_REMEDIATION = (
    "Introducing raw-HTML rendering invalidates the decision to drop ingest-time input "
    "sanitization (bleach was removed from the backend entirely, so persisted strings may "
    "contain arbitrary markup). Render through React's normal text path, or escape at that "
    "sink the way app/services/pdf_text.py::pdf_escape does for reportlab Paragraph. Do NOT "
    "silence this test. See docs/SECURITY_ADVISORY_SUPPRESSIONS.md."
)


def _grep_frontend_src(pattern: str) -> list[str]:
    """Return ``path:line:text`` hits for ``pattern`` under frontend/src, minus tests.

    ``git grep`` rather than a Python walk so the scan respects ``.gitignore``:
    ``frontend/node_modules`` is enormous and full of legitimate
    ``dangerouslySetInnerHTML`` uses, and a plain walk would drown in vendor hits.

    ``--untracked`` matters and is not incidental. Plain ``git grep`` searches only
    *tracked* files, so a brand-new component that has not been ``git add``-ed yet
    is invisible — which is precisely when a developer runs the suite and wants to
    be told. ``--untracked`` still honours ``.gitignore``, so it adds new source
    files without pulling ``node_modules`` back in. ``-I`` skips binaries, ``-n``
    numbers lines, ``-E`` is POSIX ERE.
    """
    completed = subprocess.run(
        ["git", "grep", "--untracked", "-InE", pattern, "--", "frontend/src"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    # git grep exits 1 for "no matches" (not an error) and >1 for real failures.
    assert completed.returncode in (0, 1), f"git grep failed ({completed.returncode}): {completed.stderr}"

    return [line for line in completed.stdout.splitlines() if line and not _TEST_FILE_PATTERN.search(line)]


def test_frontend_src_directory_exists():
    """Premise check, so the scans below cannot pass by scanning nothing.

    If the SPA is ever moved or renamed, this fails loudly instead of every
    raw-HTML assertion quietly succeeding against an empty file set.
    """
    assert (
        _FRONTEND_SRC.is_dir()
    ), f"expected the SPA source at {_FRONTEND_SRC}; the guard below scans nothing without it"


def test_the_scan_can_actually_find_things():
    """Control: the same machinery finds a token that is definitely present.

    Without this, a broken invocation (wrong cwd, wrong pathspec, git grep
    unavailable) would make every guard below vacuously green.
    """
    hits = _grep_frontend_src("useState")
    assert hits, "the git grep scan found no useState in frontend/src — the scan itself is broken, not clean"


@pytest.mark.parametrize("pattern", _RAW_HTML_SINKS)
def test_patterns_use_only_posix_ere(pattern: str):
    """No Perl shorthand may appear in a pattern handed to ``git grep -E``.

    ``git grep -E`` is POSIX ERE. Given ``\\s`` it neither matches whitespace nor
    reports an error — it looks for a literal ``s`` — so a pattern written with Perl
    habits fails silently and the guard it backs becomes decorative while still
    reporting green.
    """
    for shorthand in _NON_POSIX_SHORTHANDS:
        assert shorthand not in pattern, (
            f"pattern {pattern!r} uses the Perl shorthand {shorthand!r}, which git grep -E does not "
            f"implement and does not reject. Use a POSIX class such as [[:space:]] instead."
        )


@pytest.mark.parametrize("pattern,sample", zip(_RAW_HTML_SINKS, _KNOWN_BAD_SAMPLES))
def test_every_pattern_matches_a_known_bad_sample(pattern: str, sample: str, tmp_path):
    """End-to-end proof that each pattern detects the thing it is meant to detect.

    Run through the *real* ``git grep`` binary against a real file, because the whole
    class of bug being defended against here is a regex that behaves differently in
    that engine than the author expected. A pure-Python ``re`` check would reproduce
    the author's expectation rather than test it.

    The sample file lives in ``tmp_path`` — outside the repo, so it can never trip the
    guard itself or be left behind in ``frontend/src``. ``--no-index`` is what lets
    ``git grep`` search a path that is not in any working tree. The pathspec must be
    *relative* to ``cwd``: ``--no-index`` rejects an absolute path with "is outside the
    directory tree".
    """
    sample_file = tmp_path / "Sample.tsx"
    sample_file.write_text(sample + "\n")

    completed = subprocess.run(
        ["git", "grep", "--no-index", "-InE", pattern, "--", sample_file.name],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0 and completed.stdout.strip(), (
        f"pattern {pattern!r} did NOT match its known-bad sample {sample!r} under git grep -E "
        f"(rc={completed.returncode}, stderr={completed.stderr.strip()!r}). The guard using this "
        f"pattern is silently scanning for nothing."
    )


@pytest.mark.parametrize("pattern", _RAW_HTML_SINKS)
def test_frontend_never_renders_raw_html(pattern: str):
    """No production SPA source may hand an unparsed string to the HTML parser."""
    hits = _grep_frontend_src(pattern)

    assert not hits, "frontend/src now contains a raw-HTML sink matching {!r}:\n  {}\n\n{}".format(
        pattern, "\n  ".join(hits), _REMEDIATION
    )


def test_backend_no_longer_depends_on_bleach():
    """The other half of the same decision: the sanitizer is gone, not just unused.

    Pinned here rather than left implicit because "we removed the dependency" is
    the claim that makes the archived, permanently-unmaintained bleach a
    non-issue for this codebase's supply chain. A re-add — even transitively
    listed in requirements.txt — should be a deliberate, reviewed act.
    """
    requirements = (_REPO_ROOT / "backend" / "requirements.txt").read_text()
    pinned = [
        line
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and line.split("==")[0].strip().lower() == "bleach"
    ]
    assert not pinned, f"bleach is pinned again in backend/requirements.txt: {pinned}. {_REMEDIATION}"

    # POSIX ERE, same rule as _RAW_HTML_SINKS above — `\s*` here would look for a
    # literal "s" and so would miss an INDENTED import, which is how the removed
    # middleware imported its sanitizer (a function-local `from ... import`).
    # `--untracked` so a not-yet-committed re-add is caught too.
    completed = subprocess.run(
        [
            "git",
            "grep",
            "--untracked",
            "-InE",
            r"^[[:space:]]*(import bleach|from bleach)",
            "--",
            "backend/app",
            "backend/tests",
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode in (0, 1), f"git grep failed: {completed.stderr}"
    assert not completed.stdout.strip(), f"bleach is imported again:\n{completed.stdout}\n\n{_REMEDIATION}"
