"""Canonical spelling for a laser nest's free-text sheet descriptors.

``LaserNest.material`` / ``.thickness`` / ``.sheet_size`` are free text with no
``Part`` foreign key behind them -- sheet recognition on this path is a
deliberate heuristic, not a relation. That is fine for display, but it means the
STRING is the only grouping key anything has, and the strings arrive from an LLM
extraction pass that spells the same sheet more than one way.

It is not hypothetical. A 2026-08-06 reconciliation over production found the
same physical sheet split across two rows purely on whitespace::

    A36 | 0.25 | 144x60    ->  24 runs
    A36 | 0.25 | 144 x 60  ->  ... a separate row

25 output rows for 19 real sheet specs. Anything that groups, counts or
reconciles on these fields silently under-reports, and the under-report looks
like a smaller number rather than like an error.

This module is the ONE place that decides the canonical spelling. Apply it at
every seam that WRITES a nest -- the extraction mapper, the filename-inference
fallback, the package build, the manual-nest create, the nest-update endpoint,
and the work-order duplicate copy -- so a nest cannot be born uncanonical.

**The hard rule: normalize spelling, never meaning.** Every transform here is
reversible in meaning -- case, whitespace, separator glyph, and a leading zero
on a bare ``.25``. Specifically NOT done, and not to be added later:

* **No trailing-zero stripping.** ``0.250`` stays ``0.250``, it does not become
  ``0.25``. On a manufacturing thickness the trailing digits state precision,
  and an auditor reading an as-built record is entitled to the figure as the
  program stated it. (Consequence to know: ``0.25`` and ``0.250`` therefore
  still group apart. That is the accepted cost of not rewriting a spec.)
* **No unit inference.** ``16`` is not promoted to ``16ga``; a bare number stays
  a bare number.
* **No alloy expansion.** ``SS`` is NOT rewritten to ``304 SS``. It could be 304
  or 316 and the nest does not say -- inventing the grade would put a fact into
  the record that no one asserted.
* **No rewriting of anything unrecognized.** ``normalize_sheet_size`` only
  rewrites a value it can parse as two dimensions; everything else falls through
  with whitespace collapsed and nothing else touched. A descriptor the parser
  does not understand must survive verbatim rather than be mangled into
  something tidy and wrong.

Canonical forms::

    material     "a36 "        -> "A36"          (upper, whitespace-collapsed)
    thickness    "16 GA"       -> "16ga"         (unit lowercased, closed up)
                 ".25"         -> "0.25"         (leading zero)
                 "0.250"       -> "0.250"        (precision preserved)
    sheet_size   "144x60"      -> "144 x 60"     (spaced lowercase 'x')
                 "144 X 60.5"  -> "144 x 60.5"
                 "120×48"      -> "120 x 48"     (U+00D7 accepted)

Material is UPPERCASED because these are alloy/grade designations (A36, SS,
304SS, CRS, AR400) where upper is the shop's own convention and the extraction
prompt's pattern list is already uppercase. The cost is that a spelled-out
material reads as ``STAINLESS`` rather than ``Stainless``; that is accepted,
because a grouping key that is right matters more here than a display string
that is pretty, and every caller renders this next to a thickness and a size
rather than as prose.
"""

import re
from typing import Optional

# Two dimensions around a separator, each an integer or decimal, with optional
# surrounding whitespace and an optional trailing unit mark. Anchored: a partial
# match is NOT a sheet size, and a value this does not match is left alone.
#
# The separator accepts ASCII x/X, the multiplication sign U+00D7, and '*'
# (which shows up when a program strips non-ASCII). The trailing group captures
# an inch mark or unit so `48 x 96"` keeps its mark instead of losing it.
_SHEET_SIZE_RE = re.compile(
    r"""^\s*
        # \d*\.?\d+ (not \d+(\.\d+)?) so a bare ".5" matches -- otherwise the
        # leading-zero pass below is unreachable on this path and ".5x.25" would
        # fall through to the unrecognized branch.
        (?P<a>\d*\.?\d+)            # first dimension
        \s*[x×*]\s*            # separator: x, X, U+00D7, or *
        (?P<b>\d*\.?\d+)            # second dimension
        \s*(?P<unit>["'″]|in|inch|mm|cm)?   # optional trailing unit mark
        \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# A numeric thickness with an optional alphabetic unit or inch mark. The unit is
# lowercased and closed up against the number; a bare number keeps no unit.
_THICKNESS_RE = re.compile(
    r"""^\s*
        (?P<num>\d*\.?\d+)
        \s*(?P<unit>ga|gauge|in|inch|mm|cm|["'″])?
        \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _collapse(value: Optional[str]) -> Optional[str]:
    """Strip, collapse internal whitespace runs to one space; '' -> None.

    The floor every normalizer here starts from, and on its own the whole
    transform for a value the field-specific parser does not recognize. Empty
    collapses to ``None`` to match the ``or None`` idiom the nest write paths
    already use -- a blank descriptor and an absent one are the same fact, and
    storing ``''`` would make them group apart.
    """
    if value is None:
        return None
    collapsed = " ".join(str(value).split())
    return collapsed or None


def _leading_zero(number: str) -> str:
    """``.25`` -> ``0.25``. Never touches the digits after the point."""
    return f"0{number}" if number.startswith(".") else number


def normalize_material(value: Optional[str]) -> Optional[str]:
    """Canonical material designation: whitespace-collapsed and UPPERCASE.

    Deliberately does not expand abbreviations (see the module docstring): ``SS``
    stays ``SS`` because the nest does not say which stainless it is.
    """
    collapsed = _collapse(value)
    return collapsed.upper() if collapsed is not None else None


def normalize_thickness(value: Optional[str]) -> Optional[str]:
    """Canonical thickness: unit lowercased and closed up, leading zero added.

    ``16 GA`` -> ``16ga``, ``.25`` -> ``0.25``, ``0.250`` -> ``0.250`` (the
    trailing zero is precision and is preserved -- see the module docstring).
    A value that is not a plain number-with-optional-unit falls through with
    whitespace collapsed and nothing else changed.
    """
    collapsed = _collapse(value)
    if collapsed is None:
        return None
    match = _THICKNESS_RE.match(collapsed)
    if not match:
        return collapsed
    number = _leading_zero(match.group("num"))
    unit = match.group("unit")
    if not unit:
        return number
    # An inch mark stays the glyph it is; a word unit lowercases.
    unit = unit if unit in ("\"", "'", "″") else unit.lower()
    return f"{number}{unit}"


def normalize_sheet_size(value: Optional[str]) -> Optional[str]:
    """Canonical sheet size: ``<a> x <b>``, spaced, lowercase ``x``.

    THE transform this module exists for -- it is the separator spelling, not
    the numbers, that fragmented production data. ``144x60``, ``144 x 60``,
    ``144X60`` and ``120×48`` all collapse onto one key.

    Numbers are preserved exactly as written apart from a leading zero, so
    ``72.5`` stays ``72.5`` and ``72.50`` stays ``72.50``. A trailing unit mark
    is kept. Anything that does not parse as two dimensions is returned with
    whitespace collapsed and otherwise untouched.
    """
    collapsed = _collapse(value)
    if collapsed is None:
        return None
    match = _SHEET_SIZE_RE.match(collapsed)
    if not match:
        return collapsed
    a = _leading_zero(match.group("a"))
    b = _leading_zero(match.group("b"))
    unit = match.group("unit")
    unit = "" if not unit else (unit if unit in ("\"", "'", "″") else unit.lower())
    return f"{a} x {b}{unit}"


def normalize_nest_descriptors(
    material: Optional[str],
    thickness: Optional[str],
    sheet_size: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """All three at once, in ``(material, thickness, sheet_size)`` order.

    The convenience form every write seam calls, so a caller cannot normalize two
    of the three and leave the one that actually fragmented the data behind.
    """
    return (
        normalize_material(material),
        normalize_thickness(thickness),
        normalize_sheet_size(sheet_size),
    )
