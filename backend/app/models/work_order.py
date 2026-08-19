import enum
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


class WorkOrderStatus(str, enum.Enum):
    DRAFT = "draft"
    RELEASED = "released"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    COMPLETE = "complete"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class WorkOrderType(str, enum.Enum):
    PRODUCTION = "production"
    LASER_CUTTING = "laser_cutting"


class OperationStatus(str, enum.Enum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    ON_HOLD = "on_hold"


class WorkOrder(Base, SoftDeleteMixin, TenantMixin):
    """Manufacturing Work Order / Job"""

    __tablename__ = "work_orders"
    __table_args__ = (
        UniqueConstraint('company_id', 'work_order_number', name='uq_work_orders_company_wo_number'),
        # part_id is nullable ONLY for standalone laser-cutting nest WOs (sheet-run
        # jobs born from an Ermaksan nest package, no finished-good part). Every
        # other work_order_type still requires a part -- enforced here at the model
        # level so create_all test DBs / fresh bootstraps carry the CHECK, and
        # mirrored byte-identically in Alembic migration 067 for migrated Postgres.
        CheckConstraint(
            "part_id IS NOT NULL OR work_order_type = 'laser_cutting'",
            name="ck_work_orders_part_required_unless_laser",
        ),
        # Lock-step with migration 079_restore_stamped_over_idx (originally
        # migrations 001/026/027, which prod's create_all+stamp bootstrap skipped
        # because they were never mirrored here). Declared on the model so
        # create_all reproduces them and a future stamp can't skip them again.
        # ix_work_orders_status / ix_work_orders_due_date are NOT here -- those
        # two come from Column(index=True) below and already exist in prod.
        Index("ix_work_orders_status_due_date", "status", "due_date"),
        Index("ix_work_orders_created_at", "created_at"),
        Index("ix_work_orders_customer_name", "customer_name"),
        Index("ix_work_orders_actual_end", "actual_end"),
        Index("ix_work_orders_company_status", "company_id", "status"),
        Index("ix_work_orders_company_due_date", "company_id", "due_date"),
        # Lock-step with migration 080_restore_stamped_over_con (originally
        # migration 003, which the create_all+stamp bootstrap skipped). Names
        # and predicate text must stay byte-identical to the migration.
        CheckConstraint("quantity_ordered > 0", name="chk_work_orders_quantity_ordered_positive"),
        CheckConstraint("quantity_complete >= 0", name="chk_work_orders_quantity_complete_non_negative"),
        CheckConstraint("quantity_scrapped >= 0", name="chk_work_orders_quantity_scrapped_non_negative"),
        CheckConstraint("priority >= 1 AND priority <= 10", name="chk_work_orders_priority_range"),
    )

    id = Column(Integer, primary_key=True, index=True)
    work_order_number = Column(String(50), index=True, nullable=False)

    # Part/Assembly being made. NULLABLE only for work_order_type='laser_cutting'
    # (standalone nest WOs) -- see the table CHECK constraint above.
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    parent_work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True, index=True)
    work_order_type = Column(String(50), default=WorkOrderType.PRODUCTION.value, nullable=False, index=True)
    # Operation sequencing mode. True = a real ROUTING: an operation only becomes READY
    # once every lower-sequence operation is COMPLETE, its own work center included.
    # False = a DISPATCH POOL: operations sharing a work center are mutually startable
    # and promote together (``allow_same_work_center``), which is what the 18-item
    # press-brake / weld-subassembly batch WOs and the imported laser packages need.
    #
    # THE TWO DEFAULTS ARE DELIBERATELY DIFFERENT, and this is the whole migration
    # story: ``server_default=false`` so every row that already existed when migration
    # 081 ran keeps the pooled behavior it was released under byte-for-byte, while
    # ``default=True`` makes every WO created from here on a sequenced routing -- the
    # common case, and the one the pooled rule got wrong (a 4-op weld assembly on one
    # cell unlocked all 4 at once). Existing jobs are converted by flipping this field,
    # never by a backfill.
    #
    # IGNORED on laser_cutting WOs: ``is_laser_dispatch_work_order`` short-circuits
    # above this flag at every seam and is STRICTLY FULLER (it drops predecessor gating
    # entirely, across work centers). Setting this True on a nest WO changes nothing.
    sequential_operations = Column(Boolean, nullable=False, server_default="false", default=True)
    quantity_ordered = Column(Float, nullable=False)
    quantity_complete = Column(Float, default=0.0)
    quantity_scrapped = Column(Float, default=0.0)
    scrap_reason = Column(String(255), nullable=True)
    # Lean Phase 1: structured scrap categorization. Nullable -- historical rows and
    # scrap=0 writes have no code; the free-text scrap_reason stays as narrative detail.
    scrap_reason_code_id = Column(Integer, ForeignKey("scrap_reason_codes.id"), nullable=True)

    # Status tracking
    status = Column(SQLEnum(WorkOrderStatus), default=WorkOrderStatus.DRAFT, index=True)
    priority = Column(Integer, default=5, index=True)  # 1=highest, 10=lowest

    # Scheduling
    scheduled_start = Column(DateTime, nullable=True)
    scheduled_end = Column(DateTime, nullable=True)
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)
    due_date = Column(Date, nullable=True, index=True)
    must_ship_by = Column(Date, nullable=True)  # "Must Leave By" date

    # Customer/Sales Order reference
    customer_name = Column(String(255))
    customer_po = Column(String(100))
    po_line_item = Column(String(50), nullable=True)
    po_date = Column(Date, nullable=True)
    sales_order_id = Column(Integer, nullable=True)

    # Lot/Serial tracking for AS9100D traceability
    lot_number = Column(String(100), index=True)
    serial_numbers = Column(Text)  # JSON array for serialized items

    # Build identity for a ONE-UNIT-PER-WORK-ORDER job -- the weld assemblies, whose
    # "Unit #" the office used to type into ``notes`` where it was unfindable (and
    # unshowable on the wallboard, since notes is unbounded free text). Migration 083.
    #
    # Deliberately NOT ``serial_numbers``: that column is a LIST validated as exactly
    # one entry per unit (``count == quantity_ordered``) and it is the switch that puts
    # a work order into per-serial process-sheet capture -- writing one unit id there
    # would either break the count validator or silently change how steps are recorded.
    # Deliberately NOT ``lot_number`` either: that one is auto-assigned at completion as
    # ``LOT-<wo_number>`` by ``_assign_finished_good_lot`` and drives FG receipt/backflush
    # matching, so a hand-typed value there would collide with the completion path.
    #
    # Optional and UNCONSTRAINED: most work orders never carry one, and a rework work
    # order legitimately names the same unit as the original, so there is no unique
    # index. Indexed only because both search paths match on it.
    unit_number = Column(String(50), nullable=True, index=True)

    # Notes
    notes = Column(Text)
    special_instructions = Column(Text)

    # Current operation tracking. The FK mirrors migration 080 (originally 003's
    # fk_work_orders_current_operation, skipped by the create_all+stamp
    # bootstrap). use_alter=True: this FK closes a cycle with
    # work_order_operations.work_order_id, so create_all must emit it as a
    # post-CREATE ALTER on Postgres (SQLite renders it inline; unenforced there).
    current_operation_id = Column(
        Integer,
        ForeignKey(
            "work_order_operations.id",
            ondelete="SET NULL",
            name="fk_work_orders_current_operation",
            use_alter=True,
        ),
        nullable=True,
    )

    # Costing
    estimated_hours = Column(Float, default=0.0)
    actual_hours = Column(Float, default=0.0)
    estimated_cost = Column(Float, default=0.0)
    actual_cost = Column(Float, default=0.0)

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Lineage FKs mirror migration 080 (originally 003; skipped by the
    # create_all+stamp bootstrap).
    created_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL", name="fk_work_orders_created_by"), nullable=True
    )
    released_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL", name="fk_work_orders_released_by"), nullable=True
    )
    released_at = Column(DateTime, nullable=True)

    # Optimistic locking. The ``version`` column was added at the DB level by
    # migration ``004_add_optimistic_locking`` but was never mapped, leaving the
    # PUT-level locking inert: WorkOrderUpdate.version was required and the update
    # endpoint blind-setattr'd it as a transient attribute while the row's counter
    # never moved. Mapped here in the WorkOrderOperation/TimeEntry style (directly,
    # NOT via the shared OptimisticLockMixin — the mixin also declares a tz-aware
    # ``updated_at`` with server_default that collides with this model's existing
    # tz-naive ``updated_at``) so SQLAlchemy enforces ``version_id_col`` on UPDATE:
    # a concurrent stale write raises StaleDataError, translated to HTTP 409 by the
    # endpoint layer / the app-wide handler in ``app.main``. Requires every row to
    # have a non-null version; migration 004 set server_default='1' and migration
    # 069 backfills any residual NULLs and re-asserts NOT NULL + server_default.
    version = Column(Integer, nullable=False, server_default="1", default=1)
    __mapper_args__ = {"version_id_col": version}

    # Relationships
    part = relationship("Part")
    parent_work_order = relationship("WorkOrder", remote_side=[id], backref="child_work_orders")
    # foreign_keys is pinned: work_orders and work_order_operations are joined
    # by TWO FK paths (work_order_id here, current_operation_id above) since the
    # 080 FK restore, so the join must name which one it follows.
    operations = relationship(
        "WorkOrderOperation",
        back_populates="work_order",
        order_by="WorkOrderOperation.sequence",
        foreign_keys="WorkOrderOperation.work_order_id",
    )
    time_entries = relationship("TimeEntry", back_populates="work_order")
    scrap_reason_code = relationship("ScrapReasonCode", foreign_keys=[scrap_reason_code_id])


class WorkOrderOperation(Base, TenantMixin):
    """Individual operation/step in a work order routing"""

    __tablename__ = "work_order_operations"
    # Lock-step with migration 042_wo_completion_perf_indexes: backs
    # has_incomplete_predecessors (WHERE work_order_id=? AND sequence<?) and
    # release_next_ready_operation (WHERE work_order_id=? ORDER BY sequence) in
    # app/services/work_order_state_service.py.
    __table_args__ = (
        Index("ix_woo_work_order_sequence", "work_order_id", "sequence"),
        # Lock-step with migration 079_restore_stamped_over_idx (originally
        # migration 001; skipped by the create_all+stamp bootstrap): the
        # kiosk/dispatch per-work-center queue, cross-WC status scans, and
        # schedule-ordered reads.
        Index("ix_woo_work_center_status", "work_center_id", "status"),
        Index("ix_woo_status", "status"),
        Index("ix_woo_scheduled_start", "scheduled_start"),
        # Lock-step with migration 080_restore_stamped_over_con (originally
        # migration 003, which the create_all+stamp bootstrap skipped).
        CheckConstraint("setup_time_hours >= 0", name="chk_work_order_ops_setup_time_non_negative"),
        CheckConstraint("run_time_hours >= 0", name="chk_work_order_ops_run_time_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Optimistic locking (Batch 2 / SFI-2 / LOCK-1). The ``version`` column was
    # added at the DB level by migration ``004_add_optimistic_locking`` but was
    # never mapped, leaving locking inert. We map it here (scoped to this
    # completion-path model rather than the shared OptimisticLockMixin) so
    # SQLAlchemy enforces ``version_id_col`` on UPDATE: a concurrent stale write
    # to the same operation row raises StaleDataError, which the endpoint layer
    # translates to HTTP 409. Requires every row to have a non-null version;
    # migration 004 set server_default='1' and the Batch 2 migration backfills
    # any residual NULLs and re-asserts NOT NULL + server_default.
    version = Column(Integer, nullable=False, server_default="1", default=1)
    __mapper_args__ = {"version_id_col": version}

    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)

    # Component tracking (for assembly WOs with BOM)
    component_part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    component_quantity = Column(Float, default=0.0)  # Qty of this component needed

    # Operation details
    sequence = Column(Integer, nullable=False)  # 10, 20, 30...
    # Operation IDENTIFIER, not a display label: the mint sites store the bare
    # sequence ("10"); the UI adds the "Op " prefix at render time
    # (frontend/src/utils/operationLabel.ts). Free text -- the office may type
    # "OP10" -- and rows written before that change still hold "Op 10", which is
    # deliberately not backfilled. Read it with the digits, never by slicing.
    operation_number = Column(String(20))
    name = Column(String(255), nullable=False)
    description = Column(Text)

    # Grouping for batch operations
    operation_group = Column(String(50), nullable=True)  # e.g., "LASER", "BEND", "WELD"

    # Work instructions
    setup_instructions = Column(Text)
    run_instructions = Column(Text)

    # Time estimates
    setup_time_hours = Column(Float, default=0.0)
    run_time_hours = Column(Float, default=0.0)
    run_time_per_piece = Column(Float, default=0.0)

    # Actual time tracking
    actual_setup_hours = Column(Float, default=0.0)
    actual_run_hours = Column(Float, default=0.0)

    # Status
    status = Column(SQLEnum(OperationStatus), default=OperationStatus.PENDING)
    quantity_complete = Column(Float, default=0.0)
    quantity_scrapped = Column(Float, default=0.0)
    # Lean Phase 1: rework quantity alongside complete/scrapped. server_default so
    # pre-existing rows read as 0 rather than NULL (migration 063 backfills via DEFAULT).
    quantity_reworked = Column(Float, default=0.0, server_default="0")
    scrap_reason = Column(String(255), nullable=True)
    # Lean Phase 1: structured scrap categorization. Nullable -- historical rows and
    # scrap=0 writes have no code; the free-text scrap_reason stays as narrative detail.
    scrap_reason_code_id = Column(Integer, ForeignKey("scrap_reason_codes.id"), nullable=True)

    # Kiosk telemetry (Foundry redesign): the most recent production-evidence report
    # on this operation -- stamped by POST /shop-floor/operations/{id}/production and
    # by a quantity-carrying clock-out. The good/scrapped values are THAT report's
    # deltas (the kiosk renders "LAST REPORT 14:02 +48"), not running totals. All
    # three nullable: historical rows have no last report (correct-forward, no
    # backfill -- migration 070). Naive-UTC DateTime like the sibling timestamps.
    last_reported_at = Column(DateTime, nullable=True)
    last_reported_good = Column(Float, nullable=True)
    last_reported_scrapped = Column(Float, nullable=True)

    # Scheduling
    scheduled_start = Column(DateTime, nullable=True)
    scheduled_end = Column(DateTime, nullable=True)
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)

    # Manual dispatch rank within this operation's CURRENT work center, set by a
    # manager on the dispatch board: dense 1..N per work center, NULL = unranked
    # (the kiosk queue sorts unranked work AFTER all ranked work). NOT the same
    # thing as ``sequence``: ``sequence`` is routing-step precedence WITHIN one
    # work order and drives predecessor gating, while ``run_order`` is
    # cross-work-order, ADVISORY only, and never gates anything. Deliberately
    # NOT unique-constrained -- ranks are rewritten wholesale for a work center
    # in a single transaction and transient duplicates during that rewrite are
    # acceptable; a partial unique index would fight the rewrite. Indexed
    # because the kiosk/dispatch queue orders by it (migration 068).
    run_order = Column(Integer, nullable=True, index=True)

    # Quality requirements
    requires_inspection = Column(Boolean, default=False)
    inspection_type = Column(String(100))  # first_article, in_process, final
    inspection_complete = Column(Boolean, default=False)

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Lineage FKs mirror migration 080 (originally 003; skipped by the
    # create_all+stamp bootstrap).
    started_by = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_work_order_operations_started_by"),
        nullable=True,
    )
    completed_by = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_work_order_operations_completed_by"),
        nullable=True,
    )

    # Relationships
    # foreign_keys pinned for the same two-FK-path reason as WorkOrder.operations.
    work_order = relationship("WorkOrder", back_populates="operations", foreign_keys=[work_order_id])
    work_center = relationship("WorkCenter", back_populates="operations")
    time_entries = relationship("TimeEntry", back_populates="operation")
    component_part = relationship("Part", foreign_keys=[component_part_id])
    laser_nest = relationship("LaserNest", back_populates="operation", uselist=False)
    scrap_reason_code = relationship("ScrapReasonCode", foreign_keys=[scrap_reason_code_id])
