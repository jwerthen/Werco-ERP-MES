"""Quotes always carry the drawing revision: the line default, the entry rule and the send gate.

Werco's quote of 2026-10-01 went out with the Rev column blank on every line. A customer
quote that does not say which drawing revision it prices is ambiguous the day the
customer revises the drawing. Three rules, one module:

* **Default** (:func:`default_quote_line_revision`). A new quote line takes its part's
  current revision (``parts.revision``) when it names a part of the same company. Every
  ``QuoteLine`` construction site calls it. A part-less line (setup charge, freight, the
  Fabrication Quoting hand-off's "complete quoted package" line) gets nothing, and nothing
  is auto-filled at send: ``parts.revision`` defaults to ``'A'``, so filling a blank at
  send would print a plausible but unreviewed revision.
* **Entry** (:func:`normalize_line_revision`). Stripped, upper-cased, 1-20 characters of
  ``A-Z`` / ``0-9`` (the ``parts.revision`` shape, the ``String(20)`` column width), or the
  explicit ``N/A`` for a line that has no drawing.
* **Gate** (:func:`quote_send_blockers` / :func:`assert_quote_sendable`). Every door that
  puts a quote in front of the customer refuses (409, :data:`MISSING_REVISION_DETAIL`)
  while any line -- part-less lines included, or the gate would never fire on the hand-off
  quote -- lacks a revision: ``POST /quotes/{id}/send`` (when the status actually moves),
  ``PUT /quotes/{id}`` moving a quote into sent or accepted from any other status (rejected
  and expired included -- both are reachable from draft without a send), the document
  delivery preview and send, and ``POST /quotes/{id}/generate-pdf`` (it produces the
  customer document) while the quote has not been issued. Each refusal is raised before
  anything is written.

  ``generate-pdf`` on an ISSUED quote (:data:`ISSUED_QUOTE_STATUSES`) reprints the document
  the customer already holds and is not gated (:func:`assert_quote_printable`): every quote
  issued since the gate shipped passed it on its way to SENT, and an issued quote's lines are
  frozen, so the only blank revisions it can carry are a quote issued before the gate existed
  -- which must stay reproducible as issued (its Rev column prints "-"). The email delivery
  is a NEW send and stays gated in every status.
* **N/A** (:func:`refuse_na_on_drawn_parts`). ``N/A`` is for a line with no drawing; a line
  naming a manufactured or assembly part (``ENGINEERING_PART_TYPES``) has one, so ``N/A``
  there is refused (422) rather than letting it satisfy the gate unread.
"""

import re
from typing import Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.part import ENGINEERING_PART_TYPES, Part
from app.models.quote import QuoteStatus

# The explicit value for a line with no drawing (a setup charge, freight, a packaged line).
NO_DRAWING_REVISION = "N/A"
QUOTE_LINE_REVISION_MAX_LENGTH = 20  # quote_lines.part_revision is String(20), like parts.revision
_REVISION_PATTERN = re.compile(r"^[A-Z0-9]{1,20}$")

MISSING_REVISION_DETAIL = "Every quote line needs a drawing revision before the quote can be sent."

# The customer already holds a quote in these statuses: only the gated doors reach SENT, and
# conversion (to ACCEPTED / CONVERTED) requires a SENT or ACCEPTED quote.
ISSUED_QUOTE_STATUSES = frozenset({QuoteStatus.SENT, QuoteStatus.ACCEPTED, QuoteStatus.CONVERTED})


def normalize_line_revision(value: str) -> str:
    """The stored form of a requested line revision; ``ValueError`` when it is not one.

    Stripped and upper-cased (``"b "`` -> ``"B"``, ``"n/a"`` -> ``"N/A"``). Blank is refused:
    a revision, once entered, is corrected rather than erased.
    """
    cleaned = (value or "").strip().upper()
    if cleaned == NO_DRAWING_REVISION:
        return cleaned
    if not cleaned:
        raise ValueError("part_revision must not be blank (use N/A for a line with no drawing)")
    if len(cleaned) > QUOTE_LINE_REVISION_MAX_LENGTH:
        raise ValueError(f"part_revision must be at most {QUOTE_LINE_REVISION_MAX_LENGTH} characters")
    if not _REVISION_PATTERN.match(cleaned):
        raise ValueError("part_revision must be letters and digits only (or N/A for a line with no drawing)")
    return cleaned


def default_quote_line_revision(db: Session, company_id: int, part_id: Optional[int]) -> Optional[str]:
    """The revision a new quote line starts with: its part's current revision, else ``None``.

    Tenant-scoped: a part of another company yields nothing. Not filtered on ``is_deleted``
    -- the line names that part either way, and its revision is what the part last carried.
    """
    if part_id is None:
        return None
    row = tenant_query(db, Part, company_id).filter(Part.id == part_id).with_entities(Part.revision).first()
    revision = (row.revision or "").strip().upper() if row is not None else ""
    return revision[:QUOTE_LINE_REVISION_MAX_LENGTH] or None


def lines_missing_revision(lines: Iterable) -> List[int]:
    """Line numbers (in line order) whose drawing revision is blank."""
    ordered = sorted(lines, key=lambda line: (line.line_number, line.id or 0))
    return [line.line_number for line in ordered if not (line.part_revision or "").strip()]


def quote_send_blockers(quote) -> Optional[str]:
    """The 409 detail that keeps ``quote`` from the customer, or ``None`` when it may go."""
    missing = lines_missing_revision(quote.lines)
    if not missing:
        return None
    return f"{MISSING_REVISION_DETAIL} Missing on line(s): {', '.join(str(number) for number in missing)}."


def assert_quote_sendable(quote) -> None:
    """Raise the gate's 409 (before any write) when a line lacks its drawing revision."""
    detail = quote_send_blockers(quote)
    if detail is not None:
        raise HTTPException(status_code=409, detail=detail)


def assert_quote_printable(quote) -> None:
    """The ``generate-pdf`` gate: the send gate until the quote is issued, none after.

    An issued quote (:data:`ISSUED_QUOTE_STATUSES`) reprints the document the customer already
    holds. Its lines are frozen, so the only way it lacks a revision is that it was issued
    before the gate existed -- and refusing it would leave that issued document unreproducible
    (a converted quote cannot be reopened to enter revisions). A draft, pending, rejected or
    expired quote has not been issued -- rejected and expired are reachable from draft without
    a send -- so it holds the send gate.
    """
    if quote.status in ISSUED_QUOTE_STATUSES:
        return
    assert_quote_sendable(quote)


def refuse_na_on_drawn_parts(db: Session, company_id: int, lines_and_revisions: Iterable) -> None:
    """422 (before any write) when ``N/A`` is entered on a line naming a drawn part.

    ``lines_and_revisions`` is ``(line, requested_revision)`` pairs. A drawn part is a
    manufactured or assembly part of this company (``ENGINEERING_PART_TYPES``) -- it has a
    drawing revision, so ``N/A`` would satisfy the send gate without one ever being entered.
    Purchased, hardware, raw-material and consumable parts may carry ``N/A``; so may a
    part-less line, and a line whose part is another company's (it names no part here).
    """
    na_lines = [
        line for line, revision in lines_and_revisions if revision == NO_DRAWING_REVISION and line.part_id is not None
    ]
    if not na_lines:
        return
    drawn: Dict[int, str] = {
        row.id: row.part_number
        for row in tenant_query(db, Part, company_id)
        .filter(Part.id.in_(sorted({line.part_id for line in na_lines})), Part.part_type.in_(ENGINEERING_PART_TYPES))
        .with_entities(Part.id, Part.part_number)
    }
    refused = sorted((line.line_number, drawn[line.part_id]) for line in na_lines if line.part_id in drawn)
    if refused:
        named = ", ".join(f"line {number} ({part_number})" for number, part_number in refused)
        raise HTTPException(
            status_code=422,
            detail=(
                f"N/A is only for a line with no drawing; {named} names a manufactured or assembly part. "
                "Enter its drawing revision."
            ),
        )
