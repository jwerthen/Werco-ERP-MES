import enum
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class TransactionType(str, enum.Enum):
    RECEIVE = "receive"  # Receiving from PO
    ISSUE = "issue"  # Issue to work order
    RETURN = "return"  # Return to stock
    ADJUST = "adjust"  # Inventory adjustment
    SCRAP = "scrap"  # Scrap/dispose
    TRANSFER = "transfer"  # Location transfer
    SHIP = "ship"  # Ship to customer
    COUNT = "count"  # Physical count adjustment


class LocationType(str, enum.Enum):
    WAREHOUSE = "warehouse"
    RACK = "rack"
    BIN = "bin"
    FLOOR = "floor"
    QUARANTINE = "quarantine"
    SHIPPING = "shipping"
    RECEIVING = "receiving"


class CycleCountStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class InventoryLocation(Base, TenantMixin):
    """Warehouse locations/bins"""

    __tablename__ = "inventory_locations"
    __table_args__ = (UniqueConstraint('company_id', 'code', name='uq_inventory_locations_company_code'),)

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), index=True, nullable=False)  # e.g., WH1-A-01-01
    name = Column(String(255))

    # Hierarchy
    warehouse = Column(String(50), nullable=False)
    zone = Column(String(50))  # A, B, C (for ABC analysis)
    aisle = Column(String(20))
    rack = Column(String(20))
    shelf = Column(String(20))
    bin = Column(String(20))

    location_type = Column(SQLEnum(LocationType), default=LocationType.BIN)

    # Capacity
    max_quantity = Column(Float, nullable=True)
    max_weight = Column(Float, nullable=True)

    # Status
    is_active = Column(Boolean, default=True)
    is_pickable = Column(Boolean, default=True)  # Can pick from this location
    is_receivable = Column(Boolean, default=True)  # Can receive to this location

    # Cycle count
    last_count_date = Column(Date, nullable=True)
    count_frequency_days = Column(Integer, default=90)  # How often to count

    created_at = Column(DateTime, default=datetime.utcnow)


class CycleCount(Base, TenantMixin):
    """Cycle count session"""

    __tablename__ = "cycle_counts"
    __table_args__ = (
        UniqueConstraint('company_id', 'count_number', name='uq_cycle_counts_company_count_number'),
        # Lock-step with migration 079_restore_stamped_over_idx (originally
        # migration 001; skipped by the create_all+stamp bootstrap): the count
        # schedule queue (status + scheduled_date).
        Index("ix_cycle_counts_status_scheduled", "status", "scheduled_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    count_number = Column(String(50), index=True, nullable=False)

    # Scope
    location_id = Column(Integer, ForeignKey("inventory_locations.id"), nullable=True)
    warehouse = Column(String(50), nullable=True)  # Count entire warehouse
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)  # Count specific part

    # Status
    status = Column(SQLEnum(CycleCountStatus), default=CycleCountStatus.SCHEDULED)
    scheduled_date = Column(Date, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Results
    total_items = Column(Integer, default=0)
    items_counted = Column(Integer, default=0)
    items_adjusted = Column(Integer, default=0)
    total_variance_value = Column(Float, default=0.0)

    # Assignment
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    completed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    notes = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationships
    items = relationship("CycleCountItem", back_populates="cycle_count", cascade="all, delete-orphan")


class CycleCountItem(Base, TenantMixin):
    """Individual item in a cycle count"""

    __tablename__ = "cycle_count_items"

    id = Column(Integer, primary_key=True, index=True)
    cycle_count_id = Column(Integer, ForeignKey("cycle_counts.id"), nullable=False)
    inventory_item_id = Column(Integer, ForeignKey("inventory_items.id"), nullable=False)

    # Expected vs Actual
    system_quantity = Column(Float, nullable=False)  # What system shows
    counted_quantity = Column(Float, nullable=True)  # What was counted
    variance = Column(Float, nullable=True)  # counted - system

    # Cost impact
    unit_cost = Column(Float, default=0.0)
    variance_value = Column(Float, default=0.0)

    # Status
    is_counted = Column(Boolean, default=False)
    requires_recount = Column(Boolean, default=False)

    notes = Column(Text)
    counted_at = Column(DateTime, nullable=True)
    counted_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationships
    cycle_count = relationship("CycleCount", back_populates="items")
    inventory_item = relationship("InventoryItem")


class InventoryItem(Base, TenantMixin):
    """Inventory on hand - tracks quantity at location"""

    __tablename__ = "inventory_items"
    # Lock-step with migration 079_restore_stamped_over_idx (originally migration
    # 001; skipped by the create_all+stamp bootstrap): per-part on-hand rollups,
    # quarantine/hold status filters, and per-warehouse views.
    __table_args__ = (
        Index("ix_inventory_items_part_active", "part_id", "is_active"),
        Index("ix_inventory_items_status", "status"),
        Index("ix_inventory_items_warehouse", "warehouse"),
        # Lock-step with migration 080_restore_stamped_over_con (originally
        # migration 003, which the create_all+stamp bootstrap skipped).
        # 003's chk_inventory_items_quantity_non_negative (quantity_on_hand >= 0)
        # is DELIBERATELY not restored: the material-consumption shortage posture
        # lets a short completion drive a lot negative (record-and-alert, never a
        # rolled-back write) -- see CLAUDE.md invariant 6 and migration 080's
        # DELIBERATE EXCLUSIONS. quantity_allocated, by contrast, is only ever
        # written as 0 at item creation and never decremented.
        CheckConstraint("quantity_allocated >= 0", name="chk_inventory_items_allocated_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)

    # Location
    location = Column(String(100), nullable=False)  # Warehouse/bin location
    warehouse = Column(String(50), default="MAIN")

    # Quantity
    quantity_on_hand = Column(Float, default=0.0)
    quantity_allocated = Column(Float, default=0.0)  # Reserved for work orders
    quantity_available = Column(Float, default=0.0)  # on_hand - allocated

    # Lot/Serial tracking for AS9100D traceability
    lot_number = Column(String(100), index=True)
    serial_number = Column(String(100), index=True)

    # Receiving info
    received_date = Column(DateTime)
    # Lineage FK mirrors migration 080 (originally 003; skipped by the
    # create_all+stamp bootstrap).
    supplier_id = Column(
        Integer, ForeignKey("vendors.id", ondelete="SET NULL", name="fk_inventory_items_supplier"), nullable=True
    )
    po_number = Column(String(100))

    # Certificate/Documentation for compliance
    cert_number = Column(String(100))
    heat_lot = Column(String(100))  # Material heat lot

    # Expiration for shelf-life items
    expiration_date = Column(DateTime, nullable=True)

    # Cost tracking
    unit_cost = Column(Float, default=0.0)

    # Status
    status = Column(String(50), default="available")  # available, on_hold, quarantine, rejected
    is_active = Column(Boolean, default=True)

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    part = relationship("Part", back_populates="inventory_items")


# Predicates for the two work-order completion idempotency indexes
# (uq_wo_inventory_receipt / uq_wo_inventory_issue -- migration 041, dialect parity
# restored by migration 076_uq_wo_inventory_sqlite_parity).
#
# The values are the UPPERCASE enum MEMBER NAMES ('RECEIVE'/'ISSUE'), not the
# lowercase ``str`` values, because SQLAlchemy's Enum binds ``enum.name``. That is
# true on BOTH dialects -- verified at the bind-processor level:
#
#     Enum(TransactionType).dialect_impl(<dialect>).bind_processor(<dialect>)
#         postgresql: TransactionType.RECEIVE -> 'RECEIVE'   (native enum label)
#         sqlite    : TransactionType.RECEIVE -> 'RECEIVE'   (VARCHAR(8), stored TEXT)
#
# and end-to-end (an ORM-inserted row stores TEXT 'RECEIVE' under SQLite). So ONE
# literal is correct for both dialects and the predicate does NOT need to be written
# per-dialect. These MUST stay aligned with the columns/filters in
# ``_existing_work_order_receipt`` / ``_component_already_issued``
# (completion_inventory_service.py) and with migration 041/076.
WO_RECEIPT_INDEX_PREDICATE = "reference_type = 'work_order' AND transaction_type = 'RECEIVE'"
WO_ISSUE_INDEX_PREDICATE = "reference_type = 'work_order' AND transaction_type = 'ISSUE'"


class InventoryTransaction(Base, TenantMixin):
    """Transaction history for inventory movements"""

    __tablename__ = "inventory_transactions"

    # DB-enforced idempotency for work-order completion inventory side-effects
    # (completion_inventory_service.py). These PARTIAL UNIQUE indexes back the
    # application-level check-then-insert guards so a concurrent double-complete /
    # reconcile-on-read race cannot double-receive finished goods or double-issue a
    # backflushed component (the second insert raises IntegrityError, which the
    # service catches and treats as an idempotent no-op).
    #
    # Each declares BOTH ``postgresql_where`` AND ``sqlite_where`` from the SAME
    # predicate constant. Declaring only ``postgresql_where`` (the shape 041 shipped)
    # silently degraded these to FULL unique indexes under SQLite -- dev and the whole
    # pytest suite -- so the test environment enforced a constraint production does
    # not: any two ledger rows sharing (company, reference_type, reference_id,
    # transaction_type[, part_id]) collided regardless of reference_type. That made
    # legitimate movements fail locally (e.g. a second compensating ADJUST against one
    # po_receipt) and blanketed reference types the guard was never meant to cover.
    # Migration 076_uq_wo_inventory_sqlite_parity restores parity on existing SQLite
    # DBs; this declaration is what the ``create_all`` bootstrap path builds.
    #
    # Coverage is UNCHANGED for the rows these guards exist for: with the predicate
    # applied, reference_type='work_order' RECEIVE/ISSUE rows are still uniquely
    # constrained exactly as before, on both dialects.
    #
    # Defined here so the ``create_all`` bootstrap path produces the same indexes a
    # stamped+migrated DB gets (migrations 041 + 076, plus 075's reference index and
    # 078's lot/serial trace indexes below). All five are mirrored; keep them in
    # lock-step.
    __table_args__ = (
        # At most one finished-goods RECEIPT per (company, work_order).
        Index(
            "uq_wo_inventory_receipt",
            "company_id",
            "reference_type",
            "reference_id",
            "transaction_type",
            unique=True,
            postgresql_where=text(WO_RECEIPT_INDEX_PREDICATE),
            sqlite_where=text(WO_RECEIPT_INDEX_PREDICATE),
        ),
        # At most one backflush ISSUE per (company, work_order, component part).
        Index(
            "uq_wo_inventory_issue",
            "company_id",
            "reference_type",
            "reference_id",
            "transaction_type",
            "part_id",
            unique=True,
            postgresql_where=text(WO_ISSUE_INDEX_PREDICATE),
            sqlite_where=text(WO_ISSUE_INDEX_PREDICATE),
        ),
        # NON-unique composite for the reference-scoped reads. The two partial
        # unique indexes above only cover reference_type = 'work_order'; the
        # material-consumption path (work_order_material.py) posts with
        # reference_type = 'work_order_operation', which is deliberately OUTSIDE
        # those predicates so it can never collide with the backflush idempotency
        # guards. This index backs the genealogy/history reads for that path
        # ("every ledger row for this operation") and any other reference_type.
        # Added by migration 075_inventory_txn_allocation_ref.
        Index(
            "ix_inventory_txn_company_reference",
            "company_id",
            "reference_type",
            "reference_id",
        ),
        # NON-unique PARTIAL indexes for the lot/serial traceability reads
        # (traceability.py lot/serial trace, the inventory ledger lot filter).
        # Partial because most ledger rows carry no lot/serial, and every serving
        # query filters on lot_number/serial_number equality (which implies IS NOT
        # NULL), so the predicate shrinks the index without excluding any serveable
        # row. Both declare postgresql_where AND sqlite_where from the same literal,
        # per the 076 dialect-parity convention above. Added by migration
        # 078_golive_perf_indexes; keep in lock-step with it.
        Index(
            "ix_inv_txn_company_lot",
            "company_id",
            "lot_number",
            postgresql_where=text("lot_number IS NOT NULL"),
            sqlite_where=text("lot_number IS NOT NULL"),
        ),
        Index(
            "ix_inv_txn_company_serial",
            "company_id",
            "serial_number",
            postgresql_where=text("serial_number IS NOT NULL"),
            sqlite_where=text("serial_number IS NOT NULL"),
        ),
        # Lock-step with migration 079_restore_stamped_over_idx (originally
        # migrations 001/003; skipped by the create_all+stamp bootstrap). Two
        # deliberately distinct shapes: the 3-col serves per-part per-movement-type
        # history; the 2-col serves the hot part-ledger reads (exports / inventory
        # ledger / prediction cutoff window: part_id equality + created_at
        # range/ORDER BY, no transaction_type) that the 3-col cannot cover once
        # transaction_type sits between part_id and created_at. NOTE: 001's
        # single-column ix_inv_txn_created_at is deliberately NOT restored --
        # created_at below is Column(index=True), which already builds
        # ix_inventory_transactions_created_at on the same column.
        Index("ix_inv_txn_part_type_created", "part_id", "transaction_type", "created_at"),
        Index("ix_inventory_transactions_part_created", "part_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    inventory_item_id = Column(Integer, ForeignKey("inventory_items.id"), nullable=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)

    # Transaction details
    transaction_type = Column(SQLEnum(TransactionType), nullable=False, index=True)
    quantity = Column(Float, nullable=False)  # Positive for in, negative for out

    # Reference
    reference_type = Column(String(50))  # work_order, work_order_operation, purchase_order, sales_order
    reference_id = Column(Integer)
    reference_number = Column(String(100))

    # Durable genealogy key for the material-consumption path: which
    # work_order_material_allocations row caused this movement. Nullable and
    # additive — every pre-existing ledger row (and every movement that is not
    # allocation-driven: receipts, manual adjusts, backflush issues) truthfully has
    # NULL here, and is NEVER backfilled. It survives a re-tie (the allocation row
    # is CANCELLED, not deleted), so an audit can always walk a transaction back to
    # the tie that produced it. Added by migration 075_inventory_txn_allocation_ref.
    allocation_id = Column(Integer, ForeignKey("work_order_material_allocations.id"), nullable=True, index=True)

    # Location
    from_location = Column(String(100))
    to_location = Column(String(100))

    # Lot tracking
    lot_number = Column(String(100))
    serial_number = Column(String(100))

    # Cost at time of transaction
    unit_cost = Column(Float, default=0.0)
    total_cost = Column(Float, default=0.0)

    # Notes
    notes = Column(Text)
    reason_code = Column(String(100))

    # Audit fields - CMMC Level 2 requires full audit trail
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Relationships
    user = relationship("User")
    part = relationship("Part")
