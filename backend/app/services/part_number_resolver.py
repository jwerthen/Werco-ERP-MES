"""Resolving a part number, including numbers a part used to carry.

THE ONE SEAM
------------
A part can be renumbered in place (``POST /parts/{id}/renumber``), which retires
its old number into ``part_number_aliases``. Every code path that turns a part
number STRING into a part must therefore be able to answer with the part that
number used to name -- otherwise a renumber breaks a scan, a spreadsheet import,
or, worst of all, causes an importer to CREATE a second part carrying the retired
number and silently fork the catalog.

This module is that seam. The codebase already resolves part numbers five
different ways (``==``, ``ilike``, ``func.lower()``, ``func.upper()``, and an
in-Python ``.strip().upper()``); a sixth spelling that only *mostly* agrees is
exactly how a renumber would appear to work while missing on one path. So the
alias tier has ONE implementation, here, and callers opt into it.

PRECEDENCE IS ABSOLUTE: A LIVE NUMBER ALWAYS WINS
--------------------------------------------------
``resolve_part_by_number`` checks live ``parts.part_number`` first and only falls
back to aliases. This matters more than it looks. If a retired number were ever
re-issued to a genuinely different article, every alias hit would resolve old
paperwork to the WRONG physical part -- an untraceable lot-traceability break,
because both answers look legitimate. ``find_part_number_conflict`` exists to make
that state unreachable at every door that mints or renames a part; the precedence
rule here is the second line of defence, not the first.

WHAT MUST NOT USE THIS
----------------------
* **The laser sheet matcher.** ``sheet_stock_matcher`` / ``sheet_stock_spec``
  parse thickness, size and alloy out of the part-number string. An alias is a
  STALE SPEC. Resolving through it would let one physical part present two
  different material specs at once. Live numbers only -- see the model docstring.
* **The fuzzy matching tier.** ``matching_service``'s scored tier loads up to
  1000 live parts and guesses. Feeding retired numbers into a guess produces a
  high-confidence bind with worse provenance than the guess itself. The EXACT
  tier may consult; the fuzzy tier may not.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias, normalize_alias_key


@dataclass(frozen=True)
class PartNumberResolution:
    """A resolved part, and whether the caller reached it by a retired number.

    ``matched_alias`` is what lets a caller warn ("line 12 used a retired number")
    without re-querying. It is ``None`` on a live hit -- the overwhelmingly common
    case -- so ``if resolution.matched_alias:`` reads as "this was a stale input".
    """

    part: Part
    matched_alias: Optional[str] = None

    @property
    def via_alias(self) -> bool:
        return self.matched_alias is not None


def _live_part_by_number(db: Session, company_id: int, number: str) -> Optional[Part]:
    """Case-insensitive live lookup, tenant-scoped, tombstones excluded.

    ``func.upper`` rather than ``==``: mixed-case rows EXIST, because ``bom.py``
    and ``po_upload.py`` construct ``Part(...)`` directly and bypass
    ``PartBase.uppercase_part_number``. Matches the normalization
    ``migration_import_service._find_part`` already uses, and the alias key.
    """
    return (
        tenant_query(db, Part, company_id)
        .filter(
            func.upper(Part.part_number) == normalize_alias_key(number),
            Part.is_deleted == False,  # noqa: E712
        )
        .first()
    )


def resolve_part_by_number(
    db: Session,
    company_id: int,
    number: str,
    *,
    include_aliases: bool = True,
) -> Optional[PartNumberResolution]:
    """Resolve a part number to a part, falling back to retired numbers.

    Returns ``None`` when nothing matches -- callers keep their existing
    miss behavior (create, 404, ``found=false``, raise), which is deliberately
    NOT this function's business.

    ``include_aliases=False`` is for the callers that must see only live numbers
    (see the module docstring). It is a parameter rather than a separate function
    so that a reader at the call site can see which posture was chosen.

    A soft-deleted part is never returned, by either tier: an alias pointing at a
    tombstoned part resolves to nothing, because "this number used to mean a part
    somebody has since deleted" is a miss, not a hit.
    """
    if not (number or "").strip():
        return None

    live = _live_part_by_number(db, company_id, number)
    if live is not None:
        return PartNumberResolution(part=live)

    if not include_aliases:
        return None

    key = normalize_alias_key(number)
    row = tenant_query(db, PartNumberAlias, company_id).filter(PartNumberAlias.alias_number_key == key).first()
    if row is None:
        return None

    part = (
        tenant_query(db, Part, company_id)
        .filter(Part.id == row.part_id, Part.is_deleted == False)  # noqa: E712
        .first()
    )
    if part is None:
        return None
    return PartNumberResolution(part=part, matched_alias=row.alias_number)


def alias_part_ids_subquery(db: Session, company_id: int, text: str):
    """Part ids whose RETIRED numbers contain ``text`` -- for search filters.

    OR this into an existing ``part_number ILIKE`` filter so a search for a
    retired number surfaces the part that now carries a different one. Substring,
    to match how the search filters this joins already behave.

    Render such a hit as the CURRENT part with the matched alias as a subtitle
    ("Formerly OLD-123"), never as a separate result row -- two rows for one part
    is how a searcher concludes the catalog forked.
    """
    like = f"%{(text or '').strip()}%"
    return (
        tenant_query(db, PartNumberAlias, company_id)
        .filter(PartNumberAlias.alias_number_key.ilike(like.upper()))
        .with_entities(PartNumberAlias.part_id)
        .subquery()
    )


@dataclass(frozen=True)
class PartNumberConflict:
    """Why a part number cannot be used."""

    code: str  # LIVE_PART | DELETED_PART | RETIRED_ALIAS
    detail: str
    part_id: Optional[int] = None


def find_part_number_conflict(
    db: Session,
    company_id: int,
    number: str,
    *,
    excluding_part_id: Optional[int] = None,
) -> Optional[PartNumberConflict]:
    """Is this number free to mint or rename onto? ``None`` means yes.

    THREE holders must be checked, and skipping any one of them is a real bug:

    1. **A live part.** The obvious one.
    2. **A SOFT-DELETED part.** ``uq_parts_company_part_number`` has NO partial
       predicate, so a tombstone still owns its number -- this is invariant 3's
       named duplicate-probe exception, and probing live rows only would pass here
       and then hit a raw ``IntegrityError`` (which ``main.py`` has no handler for,
       so: a 500). Reported separately from (1) so the message can say something
       actionable -- restore it, or pick another number.
    3. **A RETIRED ALIAS.** Nothing at the database level stops this, and it is the
       most dangerous of the three: re-issuing a retired number to a different
       article makes every old traveler and MTR bearing it resolve to the WRONG
       physical part, and the precedence rule works perfectly *against* you --
       both answers look legitimate, and it is undetectable afterwards.

    ``excluding_part_id`` lets a rename ignore the subject part's own current
    number and its own aliases: re-stating a value you already hold changes
    nothing and must not be a conflict.
    """
    key = normalize_alias_key(number)

    live_q = tenant_query(db, Part, company_id).filter(
        func.upper(Part.part_number) == key,
        Part.is_deleted == False,  # noqa: E712
    )
    if excluding_part_id is not None:
        live_q = live_q.filter(Part.id != excluding_part_id)
    live = live_q.first()
    if live is not None:
        return PartNumberConflict(
            code="LIVE_PART",
            # "already exists" is deliberate wording, not incidental: it is the phrase
            # this app has always used for a duplicate part number, and an E2E test
            # asserts a user sees it. What is NEW is the second half -- naming the part
            # that holds it, so the operator knows whether they typed the wrong number
            # or are duplicating something real. Saying "is already used by <the same
            # number again>" (the first draft) told them nothing.
            detail=f"Part number '{number}' already exists — it belongs to {live.name}.",
            part_id=live.id,
        )

    deleted_q = tenant_query(db, Part, company_id).filter(
        func.upper(Part.part_number) == key,
        Part.is_deleted == True,  # noqa: E712
    )
    if excluding_part_id is not None:
        deleted_q = deleted_q.filter(Part.id != excluding_part_id)
    deleted = deleted_q.first()
    if deleted is not None:
        return PartNumberConflict(
            code="DELETED_PART",
            detail=(
                f"Part number '{number}' already exists on a deleted part. Restore that part, or "
                "choose a different number."
            ),
            part_id=deleted.id,
        )

    alias_q = tenant_query(db, PartNumberAlias, company_id).filter(PartNumberAlias.alias_number_key == key)
    if excluding_part_id is not None:
        alias_q = alias_q.filter(PartNumberAlias.part_id != excluding_part_id)
    alias = alias_q.first()
    if alias is not None:
        # Name the part it still points at -- "a retired number" alone leaves the
        # operator with nothing to check. Resolved rather than stored on the alias,
        # because a second stored copy of a part's current number goes stale on the
        # NEXT renumber (see the model docstring on why there is no such column).
        holder = (
            tenant_query(db, Part, company_id)
            .filter(Part.id == alias.part_id, Part.is_deleted == False)  # noqa: E712
            .first()
        )
        holder_label = holder.part_number if holder else "another part"
        return PartNumberConflict(
            code="RETIRED_ALIAS",
            detail=(
                f"Part number '{number}' already exists as a retired number for {holder_label}. "
                "Reusing it would make older paperwork resolve to the wrong part."
            ),
            part_id=alias.part_id,
        )

    return None
