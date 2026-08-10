"""Sheet/plate stock spec parsing — the Python half of the sheet-recognition grammar.

This is a deliberate port of ``frontend/src/utils/sheetPart.ts``. Read that file's
header first: it explains WHY dimensions are parsed out of the shop's part-number
convention instead of read from ``Part`` columns (there are none, and adding them
means a migration plus re-keying every stock item by hand), and why both functions
fail closed rather than guess.

Why a second copy of the grammar exists
---------------------------------------
The TypeScript version answers two questions for the wizard's picker, on the
client, about a part the planner already chose. This one answers a THIRD question
the client cannot: given a nest's AI-read ``material`` / ``thickness`` /
``sheet_size``, which stock part in the tenant's catalog is it? That needs the
whole catalog, the tie history and on-hand — all of which live behind the DB — so
it has to run server-side.

Two ports of one grammar can drift silently, and a drift here means the picker
offers a part the matcher refuses (or worse, the reverse). ``tests/fixtures/
sheet_part_cases.json`` is read by BOTH test suites, so a divergence fails CI on
both sides instead of shipping.

What this module adds beyond the TS port
----------------------------------------
The TS port compares strings a human is about to read. Matching compares two
machine reads of the same physical property, so it needs numbers:

* ``thickness_inches`` — ``"10 ga"`` and ``"0.1345"`` are the same sheet.
* ``dims_inches`` — sorted ascending, so a nest reading ``60 x 120`` matches a
  part numbered ``...-120X60``. Nest reports and part numbers do not agree on
  which dimension comes first, and neither is wrong.
* ``canonical_alloy`` / ``alloy_family`` — ``304SS``, ``SS304`` and ``304`` are
  one grade; a bare ``SS`` is NOT a grade, it is an under-specification, and
  recognizing that is how the matcher knows to refuse rather than pick.

The scalar helpers are additive: none of them feed the spec STRINGS, which stay
verbatim exactly as the TS port emits them (``0.250`` never becomes ``0.25`` —
that is a precision claim on a manufacturing thickness and not ours to rewrite).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from app.services.sheet_metal_costing_service import GAUGE_TO_INCHES, parse_thickness_to_inches


@dataclass(frozen=True)
class SheetSpec:
    """Thickness and sheet size as the wizard's two free-text fields want them."""

    thickness: Optional[str] = None
    sheet_size: Optional[str] = None


EMPTY_SPEC = SheetSpec()

# All four quote forms plus the double-prime, mirroring the TS `normalize`. Excel
# autocorrects an inch mark after a digit to the CLOSING curly quote, so that is
# the one this data actually contains, but a retyped part number can carry any of
# them and a dimension that fails to parse is invisible.
_QUOTES = re.compile("[\"'‘’“”„″]")
_THK_WORD = re.compile(r"\bTHK\b\.?")
_INCH_WORD = re.compile(r"\bINCHES?\b\.?")
_WHITESPACE = re.compile(r"\s+")


def normalize_part_text(text: Optional[str]) -> str:
    """Uppercase, drop inch marks and the ``THK`` noise word, collapse whitespace.

    EXACT mirror of ``sheetPart.ts::normalize``. ``THK`` has to go before the
    dimension match: ``0.188" THK x 60 x 120`` would otherwise break the thickness
    away from the two dimensions that follow it.
    """
    value = (text or "").upper()
    value = _QUOTES.sub("", value)
    value = _THK_WORD.sub(" ", value)
    value = _INCH_WORD.sub(" ", value)
    return _WHITESPACE.sub(" ", value).strip()


# A sheet's three dimensions, ANCHORED at the start of the normalized string:
# thickness first, then width x length.
#
# THE ANCHOR IS THE ENTIRE SAFEGUARD. Un-anchored, every angle and tube part
# number in the catalog matches: `ANG-A36-1.5X1.5X.25` and `1.50" x 1.50" x
# 0.250" THK A36 Angle` both contain a valid three-number triple and are both
# angle, not sheet. Never relax this "to catch more sheets" -- the cost of a miss
# is one toggle in the picker; the cost of a false match is angle-iron dimensions
# stamped onto a sheet, or a nest tied to bar stock.
_DECIMAL_TRIPLE = re.compile(r"^(\d*\.\d+|\d+)\s*[X-]\s*(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)")
_GAUGE_TRIPLE = re.compile(r"^(\d+)\s*GA\b\.?\s*[X-]?\s*(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)")

# Words that make a part flat stock regardless of its numbering convention.
_SHEET_WORDS = re.compile(r"\b(SHEET|SHEETS|PLATE|PLATES|COIL)\b")

# A plausible sheet/plate thickness in inches, used to reject a parse that is
# arithmetically valid and physically absurd.
#
# This exists for one specific failure: `parse_thickness_to_inches` reads a BARE
# "16" as 16.0 inches (it does not infer gauge, deliberately -- see the
# no-unit-inference rule this codebase already holds). 16 inches of plate is not
# a thing anyone lasers, and letting it through would put a nonsense number into
# a numeric comparison. Bounding the parse turns that into a clean "unreadable",
# which the matcher already fails closed on.
MIN_PLAUSIBLE_THICKNESS_IN = 0.005
MAX_PLAUSIBLE_THICKNESS_IN = 4.0

# A plausible sheet dimension in inches. Same reasoning: a sheet is inches to
# feet, never a thousandth and never a mile.
MIN_PLAUSIBLE_DIM_IN = 1.0
MAX_PLAUSIBLE_DIM_IN = 600.0


def match_triple(normalized: str) -> Optional[SheetSpec]:
    """The anchored thickness + width x length grammar, gauge form tried first."""
    gauge = _GAUGE_TRIPLE.match(normalized)
    if gauge:
        return SheetSpec(thickness=f"{gauge.group(1)} ga", sheet_size=f"{gauge.group(2)}x{gauge.group(3)}")
    decimal = _DECIMAL_TRIPLE.match(normalized)
    if decimal:
        return SheetSpec(thickness=decimal.group(1), sheet_size=f"{decimal.group(2)}x{decimal.group(3)}")
    return None


def derive_sheet_spec(part_number: Optional[str], name: Optional[str] = None) -> SheetSpec:
    """Thickness + sheet size for a stock part, or an empty spec.

    The part NUMBER is tried first and wins: it is the shop's canonical
    identifier, keyed once and reused, while the name is prose that drifts. The
    name is a fallback for parts numbered off-convention, consulted only for its
    own ANCHORED triple -- the same grammar, never a looser one.

    Values come back exactly as written. ``0.250`` does not become ``0.25``.
    """
    from_number = match_triple(normalize_part_text(part_number))
    if from_number:
        return from_number
    from_name = match_triple(normalize_part_text(name))
    if from_name:
        return from_name
    return EMPTY_SPEC


def is_sheet_like(
    part_number: Optional[str],
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> bool:
    """Is this material part flat stock a laser nest is cut from?

    True when EITHER signal is present: the part number parses as an anchored
    triple, or the part's own text says "sheet", "plate" or "coil".

    Both are POSITIVE tests. A veto list ("not a beam, not a tube, not a bolt...")
    is unbounded and silently drops the stock it forgot to name, which is the
    failure that hides a real sheet from the planner who needs it.
    """
    if match_triple(normalize_part_text(part_number)):
        return True
    text = normalize_part_text(f"{part_number or ''} {name or ''} {description or ''}")
    return bool(_SHEET_WORDS.search(text))


def thickness_inches(value: Optional[str]) -> Optional[float]:
    """A thickness string as inches, or ``None`` when it cannot be read.

    Delegates the grammar to ``sheet_metal_costing_service.parse_thickness_to_inches``
    (gauge table, fractions, mm, decimal inches) rather than growing a second one,
    then applies the plausibility bound above.

    ``None`` means UNREADABLE and must never be coerced to 0.0 by a caller: a
    gauge outside ``GAUGE_TO_INCHES`` (9ga, 13ga) and a bare "16" both land here,
    and a 0.0 would compare equal to nothing while looking like a real number.
    """
    parsed = parse_thickness_to_inches(value)
    if parsed is None:
        return None
    if not (MIN_PLAUSIBLE_THICKNESS_IN <= parsed <= MAX_PLAUSIBLE_THICKNESS_IN):
        return None
    return parsed


# Two numbers around an `x`-ish separator. Unanchored ON PURPOSE and unlike the
# part-number triple: this reads a value that is ALREADY known to be a sheet size
# (either the nest's own sheet_size field or the size half of a matched triple),
# so there is no other number in the string to be confused by.
_DIM_PAIR = re.compile(r"(\d*\.?\d+)\s*[X×*]\s*(\d*\.?\d+)")


def dims_inches(sheet_size: Optional[str]) -> Optional[Tuple[float, float]]:
    """A sheet size as ``(smaller, larger)`` inches, or ``None``.

    SORTED ASCENDING, which is the whole point: a nest report reading ``60 x 120``
    and a part numbered ``0.250-120X60`` describe one sheet. Neither order is
    wrong and neither side is authoritative, so the pair is normalized instead of
    one being preferred.
    """
    match = _DIM_PAIR.search(normalize_part_text(sheet_size))
    if not match:
        return None
    try:
        first = float(match.group(1))
        second = float(match.group(2))
    except ValueError:  # pragma: no cover - the regex only matches numerics
        return None
    low, high = (first, second) if first <= second else (second, first)
    if not (MIN_PLAUSIBLE_DIM_IN <= low and high <= MAX_PLAUSIBLE_DIM_IN):
        return None
    return (low, high)


def single_dim_inches(sheet_size: Optional[str]) -> Optional[float]:
    """The lone dimension of a one-number sheet size (``"72.5"``), or ``None``.

    Nest reports sometimes state only one edge. That is weaker evidence than a
    pair, never stronger, so it feeds the SOFT size score and never the hard gate.
    Returns ``None`` when the value holds a pair (use ``dims_inches``) or nothing.
    """
    if dims_inches(sheet_size) is not None:
        return None
    match = re.search(r"(\d*\.?\d+)", normalize_part_text(sheet_size))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:  # pragma: no cover
        return None
    if not (MIN_PLAUSIBLE_DIM_IN <= value <= MAX_PLAUSIBLE_DIM_IN):
        return None
    return value


# Alloy grades this shop's data actually contains, sourced from the vocabularies
# already in the codebase (`part_number_service._find_grade` and
# `laser_nest_service._infer_material`). The list is LITERAL on purpose: a
# pattern like "any 3-4 digit number is a grade" would read a sheet size as an
# alloy.
#
# Order matters -- longest first -- so `304L` is not shortened to `304` and
# `A572` is not read as `A5`.
_ALLOY_TOKENS = (
    "17-4PH",
    "AR400",
    "AR500",
    "A514",
    "A572",
    "304L",
    "316L",
    "6061",
    "5052",
    "7075",
    "4140",
    "1018",
    "A36",
    "304",
    "316",
    "CRS",
    "HRS",
    "CS",
)

# Spellings that mean an existing token. `304SS`/`SS304` -> `304`; the temper
# suffixes (`6061-T6`, `5052-H32`) fall out of substring matching.
_ALLOY_ALIASES = {
    "COLD ROLLED": "CRS",
    "COLD-ROLLED": "CRS",
    "HOT ROLLED": "HRS",
    "HOT-ROLLED": "HRS",
    "MILD STEEL": "A36",
    "CARBON STEEL": "CS",
}

# Bare family words: a real statement that the grade was NOT stated. This is how
# under-specification is RECOGNIZED, never resolved -- `SS` could be 304 or 316
# and the nest does not say, so the matcher refuses to pick between them.
_FAMILY_PATTERNS = (
    ("SS", re.compile(r"\b(SS|STAINLESS)\b")),
    ("AL", re.compile(r"\b(AL|ALUM|ALUMINUM|ALUMINIUM)\b")),
)


def _strip_leading_triple(normalized: str) -> str:
    """Drop the anchored dimension triple so grade matching never reads a dimension.

    Without this, substring matching for a short grade token finds one inside the
    numbers: ``0.06X60X144-304SS`` and ``0.25-60X120-A36`` both carry digit runs
    that a bare ``in`` test can collide with. The grade always follows the
    dimensions in this shop's convention, so searching only the remainder is both
    safer and more faithful to how the numbers are actually keyed.
    """
    for pattern in (_GAUGE_TRIPLE, _DECIMAL_TRIPLE):
        match = pattern.match(normalized)
        if match:
            return normalized[match.end() :]
    return normalized


def canonical_alloy(text: Optional[str]) -> Optional[str]:
    """The alloy grade named in ``text``, canonicalized, or ``None``.

    ``None`` means "no grade stated here", which is a DIFFERENT answer from a
    grade that disagrees -- the matcher treats the two differently (an absent
    grade can never earn a pre-fill; a conflicting one drops the candidate), so
    it has to be able to tell them apart.

    Matching is word-boundary first, because the short tokens are the dangerous
    ones: a bare ``in`` test for ``CS`` hits any word containing those letters.
    The squeezed pass exists only for the compound stainless spellings
    (``304SS`` / ``SS304``), where no word boundary exists between the grade and
    the family, and it is restricted to the NUMERIC grades for the same reason.
    """
    normalized = normalize_part_text(text)
    if not normalized:
        return None
    for phrase, token in _ALLOY_ALIASES.items():
        if phrase in normalized:
            return token

    remainder = _strip_leading_triple(normalized)
    if not remainder.strip():
        return None

    # Pass 1: whole-token match. Handles `A36`, `CS`, `304L`, `6061-T6`
    # (the hyphen is a word boundary) and `A572-50`.
    for token in _ALLOY_TOKENS:
        if re.search(rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])", remainder):
            return token

    # Pass 2: the compound stainless/aluminum spellings only. Numeric grades
    # cannot collide with an alloy word, and the dimensions are already gone.
    squeezed = re.sub(r"[\s_/-]+", "", remainder)
    for token in _ALLOY_TOKENS:
        if token[0].isdigit() and token in squeezed:
            return token
    return None


def alloy_family(text: Optional[str]) -> Optional[str]:
    """``"SS"`` / ``"AL"`` when the text names a family but no grade, else ``None``."""
    if canonical_alloy(text) is not None:
        return None
    normalized = normalize_part_text(text)
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(normalized):
            return family
    return None


def thickness_bucket(value: Optional[str]) -> str:
    """A thickness folded to a stable grouping token.

    Snaps to a gauge when it lands within tolerance of one, so ``10ga`` and
    ``0.1345`` bucket together; otherwise rounds to 4 decimal places, which folds
    ``0.25`` and ``0.250`` (the exact pair PR #210's normalizer deliberately
    leaves apart, because IT is preserving a precision claim and this is only
    grouping).
    """
    inches = thickness_inches(value)
    if inches is None:
        return "?"
    for gauge_name, gauge_in in GAUGE_TO_INCHES.items():
        if abs(gauge_in - inches) <= 0.002:
            return gauge_name
    return f"{inches:.4f}"


def spec_key(material: Optional[str], thickness: Optional[str], sheet_size: Optional[str]) -> str:
    """A grouping key for one physical sheet spec.

    READ-TIME ONLY and NEVER PERSISTED. That is what lets it work on the ~65
    pre-normalization production nests as well as on post-PR-#210 rows: it takes
    no dependency on the stored spelling, so it needs no backfill of records that
    are deliberately forward-only.

    Collapses ``144x60``/``144 X 60``/``60x144``, ``0.25``/``0.250``, and
    ``10ga``/``0.1345``.
    """
    alloy = canonical_alloy(material) or alloy_family(material) or "?"
    dims = dims_inches(sheet_size)
    size = f"{dims[0]:g}x{dims[1]:g}" if dims else "?"
    return f"{alloy}|{thickness_bucket(thickness)}|{size}"
