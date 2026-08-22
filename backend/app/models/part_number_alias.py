"""Retired part numbers, and the part each one still resolves to.

WHAT THIS IS
------------
When a part is renumbered in place (``POST /parts/{id}/renumber``), the number it
used to carry does not stop existing in the world. It is printed on travelers in
the rack, on MTRs in the file cabinet, on the customer's PO, and in the shop's
spreadsheets. This table is what lets all of those keep finding the part: one
immutable row per retired number, pointing at the part that now carries a
different one.

It is a **RESOLVER INDEX, NOT THE RECORD.** The record of a renumber is the
tamper-evident ``audit_log`` row (invariant 2 -- the ``008``/``060`` triggers
refuse UPDATE and DELETE, and the hash chain covers it). That distinction is why
this model is deliberately thin, and it decides several things below.

WHY EACH CHOICE, BECAUSE EACH WILL BE QUESTIONED
------------------------------------------------
* **No ``SoftDeleteMixin``, no ``is_active``, no ``status``.** A third state would
  be a mask that every future reader must remember to filter, which is exactly the
  trap invariant 3 documents after the 2026-08-16 ``Vendor`` sweep. An alias row
  exists or it does not.

* **No ``superseded_by_number`` column.** The part's current number is
  ``part.part_number``, reachable through ``part_id``. A second stored copy would
  go stale on the *second* renumber -- the identical bug class to
  ``WorkOrderOperation.name``, which this whole feature had to work around. It
  will be proposed as a join-saving denormalization. Refuse it.

* **``String(100)``, matching ``parts.part_number`` -- NOT the ``PartNumber``
  annotated type** (3-50 chars, ``^[A-Za-z0-9\\-_\\.#]+$``). Production holds
  numbers the pattern rejects (``1/4" PLATE 48 X 96``), because ``bom.py`` and
  ``po_upload.py`` construct ``Part(...)`` directly and bypass the schema
  validator. Those legacy-numbered rows are precisely the ones most likely to be
  renumbered, so an alias column that cannot hold what ``parts`` holds would make
  renumbering them impossible. ``PartResponse.part_number`` made the same call.

* **Two columns for one value.** ``alias_number`` is verbatim, as it appears on
  paper. ``alias_number_key`` is ``.strip().upper()`` and carries the uniqueness
  constraint. A normalized key column rather than a functional
  ``UNIQUE (company_id, upper(alias_number))`` index because it is dialect-neutral
  -- the SQLite test suite then enforces exactly the rule prod Postgres does -- and
  because every alias read stays plain equality. No ``ilike``, so this tier cannot
  inherit the un-escaped-wildcard hazard that ``scanner.py``'s matching carries.

* **``reason`` is NOT NULL.** Every other identity-affecting verb in this system
  requires a written reason (receiving void, NCR void, vendor delete). A renumber
  is more consequential than any of them.

WHAT MUST NEVER READ THIS TABLE
-------------------------------
**The laser sheet matcher.** ``sheet_stock_matcher`` and ``sheet_stock_spec``
derive thickness, size and alloy *from the part-number string*, because ``Part``
carries no such columns. An alias is a **stale spec string**. Gate A in the
matcher is a set-membership test, so a part reachable under two numbers would
present two different material specs at once -- becoming a pre-fill candidate for
two incompatible nests simultaneously, and tripping the matcher's own ambiguity
refusals on a phantom. Live numbers only, forever. This paragraph exists so that
nobody "improves" the matcher into reading it later.

See ``docs/API.md`` -> Parts -> "Renumbering a part".
"""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


def normalize_alias_key(value: str) -> str:
    """The one spelling of the alias lookup key: stripped and upper-cased.

    THE single normalizer for this tier -- the service writes ``alias_number_key``
    through it and every resolver reads through it, so the two can never disagree.
    It matters that it is one function: the codebase already resolves part numbers
    five different ways (``==``, ``ilike``, ``func.lower``, ``func.upper``, and an
    in-Python ``.strip().upper()``), and a sixth spelling that only *mostly* agrees
    is how a rename would appear to work while silently missing on one path.

    Chosen to match ``migration_import_service``'s ``func.upper(...)``, since that
    is the seam behind all three go-live spreadsheet loaders.
    """
    return (value or "").strip().upper()


class PartNumberAlias(Base, TenantMixin):
    """One retired part number, and the part it still resolves to."""

    __tablename__ = "part_number_aliases"
    __table_args__ = (
        # Company-scoped, and on the NORMALIZED key: two aliases differing only by
        # case or surrounding whitespace are the same number on paper.
        UniqueConstraint("company_id", "alias_number_key", name="uq_part_number_aliases_company_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)

    # Verbatim, as printed on the traveler / MTR / customer PO.
    alias_number = Column(String(100), nullable=False)
    # normalize_alias_key(alias_number). Service-written; never set at a call site.
    alias_number_key = Column(String(100), nullable=False)

    reason = Column(Text, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    part = relationship("Part", foreign_keys=[part_id])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PartNumberAlias {self.alias_number!r} -> part_id={self.part_id}>"
