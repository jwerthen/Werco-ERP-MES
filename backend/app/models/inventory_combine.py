"""One folding of two SKUs into one, as a first-class record.

WHAT THIS IS
------------
A materials-numbering recut can leave two part numbers describing the SAME
physical article -- the same sheet, on the same rack, counted twice. Combining
them moves the stock from the SOURCE number onto the TARGET number without
inventing or destroying a single unit, and this table is the header that makes
that a record rather than a pile of adjustments.

Every combine writes **2N ledger rows** (one ``ADJUST`` out of the source lot and
one ``ADJUST`` into the target lot, per moved lot line) that sum to exactly zero.
Without a header those rows are only related by having happened at the same
instant, which is not a relation anything can query. This row is the group id
they all carry (``reference_type='inventory_combine'``, ``reference_id=<this
row>``), the same way ``po_receipts`` groups a receipt's movements.

WHY EACH CHOICE, BECAUSE EACH WILL BE QUESTIONED
------------------------------------------------
* **No new ``TransactionType`` member.** ``COMBINE`` as an enum value would mean
  an ``ALTER TYPE`` on live Postgres plus teaching every consumer -- analytics,
  exports, traceability, job costing, the frontend label maps -- a value they
  currently cannot see. That is a broad blast radius on live data for a labelling
  gain. ``ADJUST`` is ALREADY this repo's compensating/reconciliation shape
  (invariant 3; receiving void/correct), and it is the one type whose SIGN
  carries direction. The pair is distinguished by ``reason_code``
  (``COMBINE_OUT`` / ``COMBINE_IN``) and grouped by this row.

* **No ``SoftDeleteMixin``, no ``is_active``, no ``status``.** Same argument as
  ``PartNumberAlias``: a third state is a mask every future reader must remember
  to filter, which is precisely the trap invariant 3 documents after the
  2026-08-16 ``Vendor`` sweep. A combine happened or it did not. Reversing one is
  a NEW, reasoned, audited combine in the other direction -- never an edit of
  this row, and never a physical delete.

* **``reason`` is NOT NULL.** Every identity-affecting verb in this system
  requires a written reason (receiving void, NCR void, vendor delete, part
  renumber). Merging two article identities under AS9100D 8.5.2 is at least as
  consequential as any of them.

* **The before/after quantity columns are a SNAPSHOT, not a source of truth.**
  The ledger is authoritative for what moved; these four columns record what the
  operator was shown at the moment they approved it, which is not reconstructable
  afterwards once later movements have posted. Same reasoning as
  ``CycleCount.total_variance_value``.

THIS ROW IS NOT THE AUDIT RECORD
--------------------------------
The tamper-evident record of a combine is the ``audit_log`` row written through
``AuditService`` (invariant 2 -- the ``008``/``060`` triggers refuse UPDATE and
DELETE, and the hash chain covers it). This table is a QUERYABLE INDEX over the
ledger rows, which is why it is deliberately thin and why nothing here may ever
be edited after the fact.

See ``app/services/inventory_combine_service.py`` for the rules, and
``docs/API.md`` -> Inventory -> "Combining two SKUs".
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from app.db.database import Base
from app.db.mixins import TenantMixin

# The ledger ``reference_type`` every combine row carries.
#
# CRITICAL, and pinned by a test: this literal sits OUTSIDE both partial unique
# predicates ``uq_wo_inventory_receipt`` / ``uq_wo_inventory_issue`` (which are
# ``reference_type = 'work_order' AND transaction_type = 'RECEIVE'/'ISSUE'``), so
# combine rows can never collide with the work-order completion idempotency
# guards. It is also outside ``work_order_ledger_filter``'s three reference types
# ('work_order', 'work_order_backflush', 'work_order_operation') -- a combine is
# NOT work-order material and must never appear in job costing or lot genealogy
# for a job. Do not add it to either set.
COMBINE_REFERENCE_TYPE = "inventory_combine"

# The two halves of one moved lot line, distinguished on the ledger row itself so
# a reader scanning ``inventory_transactions`` alone can tell which direction a
# row is without joining back to this header.
COMBINE_OUT_REASON_CODE = "COMBINE_OUT"
COMBINE_IN_REASON_CODE = "COMBINE_IN"

COMBINE_NUMBER_PREFIX = "COMB-"


def format_combine_number(combine_id: int) -> str:
    """The human-facing number for a combine, minted from its own primary key.

    Deliberately NOT the ``acquire_generator_lock`` + ``MAX(number)`` scan the
    work-order / PO / NCR / process-sheet numbers use. That pattern exists because
    those numbers embed a date or must be per-company sequential, so the next value
    cannot be derived from anything the row already carries; it costs an advisory
    lock held for the rest of the transaction plus an index scan. Here the id IS a
    unique, monotonic, per-row value the INSERT already produced, so deriving from
    it is collision-free by construction with no lock and no scan.

    The cost, stated so nobody is surprised: the sequence is global to the
    deployment, not per company, so one tenant's numbering has gaps where another
    tenant's combines fell. The unique constraint is still ``(company_id,
    combine_number)`` -- belt and braces over a value that cannot repeat anyway.
    """
    return f"{COMBINE_NUMBER_PREFIX}{combine_id:06d}"


class InventoryCombine(Base, TenantMixin):
    """One combine: source SKU, target SKU, how much moved, and who said so."""

    __tablename__ = "inventory_combines"
    __table_args__ = (
        # Per-company uniqueness on the minted number. ``format_combine_number``
        # derives it from the primary key so it cannot repeat; this is the
        # constraint that keeps that true if the minting rule ever changes.
        UniqueConstraint("company_id", "combine_number", name="uq_inventory_combines_company_number"),
        # "What was ever folded into / out of this part?" -- the two reads the
        # Parts screen and any investigation start from. Mirrored into migration
        # 085 in the same PR (the 042/078/079/080 lock-step convention: an index
        # declared only in a migration is skipped entirely by the
        # ``create_all`` + ``alembic stamp`` bootstrap, which is how prod lost ~42
        # read-path indexes and 22 lineage FKs).
        Index("ix_inventory_combines_company_source", "company_id", "source_part_id"),
        Index("ix_inventory_combines_company_target", "company_id", "target_part_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    combine_number = Column(String(50), index=True, nullable=False)

    # The SKU the stock left. It is never deleted -- it stays in the catalog at
    # qty 0, optionally deactivated (see ``source_deactivated``), because every
    # historical document naming it must keep resolving.
    source_part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)
    # The SKU the stock landed on.
    target_part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)

    quantity = Column(Float, nullable=False)
    reason = Column(Text, nullable=False)

    # How many (location, lot) lines the quantity spread across. One combine of 92
    # sheets can be one line or nine, and which it was matters when reading the
    # ledger back.
    lines_moved = Column(Integer, default=0)

    # Snapshot of what the operator approved -- see the module docstring. The
    # ledger, not these columns, is authoritative for what actually moved.
    source_quantity_before = Column(Float)
    source_quantity_after = Column(Float)
    target_quantity_before = Column(Float)
    target_quantity_after = Column(Float)

    # True when this combine also set the source part inactive/obsolete. Recorded
    # here because the part row itself carries no history of when that happened.
    source_deactivated = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<InventoryCombine {self.combine_number!r} "
            f"part {self.source_part_id} -> {self.target_part_id} qty={self.quantity}>"
        )
