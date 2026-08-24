import enum
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


class PartType(str, enum.Enum):
    MANUFACTURED = "manufactured"  # Parts we make
    PURCHASED = "purchased"  # Off the shelf / buy parts
    ASSEMBLY = "assembly"  # Assemblies we build
    RAW_MATERIAL = "raw_material"  # Raw stock material (sheets, bars, etc.)
    HARDWARE = "hardware"  # COTS hardware (bolts, nuts, washers, fasteners)
    CONSUMABLE = "consumable"  # Consumables (adhesives, lubricants, etc.)


ENGINEERING_PART_TYPES = (PartType.MANUFACTURED, PartType.ASSEMBLY)
MATERIAL_SUPPLY_PART_TYPES = (
    PartType.PURCHASED,
    PartType.RAW_MATERIAL,
    PartType.HARDWARE,
    PartType.CONSUMABLE,
)


def normalize_part_type_value(value) -> str:
    if hasattr(value, "value"):
        return str(value.value).strip().lower()
    if isinstance(value, str):
        return value.strip().lower()
    return str(value).strip().lower()


def is_engineering_part_type(value) -> bool:
    return normalize_part_type_value(value) in {part_type.value for part_type in ENGINEERING_PART_TYPES}


def is_material_supply_part_type(value) -> bool:
    return normalize_part_type_value(value) in {part_type.value for part_type in MATERIAL_SUPPLY_PART_TYPES}


class UnitOfMeasure(str, enum.Enum):
    EACH = "each"
    FEET = "feet"
    INCHES = "inches"
    POUNDS = "pounds"
    KILOGRAMS = "kilograms"
    SHEETS = "sheets"
    GALLONS = "gallons"
    LITERS = "liters"


def uom_label(value) -> str:
    """A ``unit_of_measure`` (enum, enum value or raw string) as a comparable lowercase str.

    ``Part.unit_of_measure`` is a native enum column and ``BOMItem.unit_of_measure`` is a
    free ``String(20)``, so the two sides of any line-vs-part question arrive in different
    shapes. This flattens both. ``None`` / blank flattens to ``""`` -- a MISSING unit, which
    is not the same claim as a WRONG one.
    """
    return str(getattr(value, "value", value) or "").strip().lower()


def uom_disagrees(line_value, part_value) -> bool:
    """True when a BOM line's stated unit of measure CONTRADICTS its component part's.

    THE single predicate behind every line-vs-part unit comparison in the platform. Two
    readers depend on it agreeing with itself exactly:

    * ``completion_inventory_service._record_bom_line_diagnostics`` raises the BLOCKING
      ``unit_of_measure_mismatch`` diagnostic, which refuses ``Part.backflush_components``
      at opt-in AND refuses that component at completion.
    * ``GET /bom/uom-mismatches`` lists the rows a human has to correct first.

    A report that disagreed with the gate would be worse than no report: it would either
    hide a row that still blocks, or send someone to fix a row that never did.

    Blank on either side is NOT a disagreement. A line that states no unit makes no claim
    to contradict, and a part with no stocking unit gives nothing to contradict it -- so
    both are silent here, exactly as the diagnostic has always been.

    Comparison is by exact normalised label, deliberately: ``ea`` does NOT equal ``each``
    here. Teaching this synonyms would make the gate accept lines it currently refuses,
    which is a softening of a blocking control, not a bug fix. The report surfaces those
    rows so a human normalises the stored value instead.
    """
    line = uom_label(line_value)
    part = uom_label(part_value)
    return bool(line) and bool(part) and line != part


class Part(Base, SoftDeleteMixin, TenantMixin):
    __tablename__ = "parts"
    __table_args__ = (
        UniqueConstraint('company_id', 'part_number', name='uq_parts_company_part_number'),
        # Lock-step with migration 079_restore_stamped_over_idx (originally
        # migration 026's tenancy composite; skipped by the create_all+stamp
        # bootstrap): tenant-scoped active-part lists.
        Index("ix_parts_company_active", "company_id", "is_active"),
        # Lock-step with migration 080_restore_stamped_over_con (originally
        # migration 003, which the create_all+stamp bootstrap skipped).
        CheckConstraint("standard_cost >= 0", name="chk_parts_standard_cost_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    part_number = Column(String(100), index=True, nullable=False)
    revision = Column(String(20), default="A")
    name = Column(String(255), nullable=False)
    description = Column(Text)
    part_type = Column(
        SQLEnum(
            PartType, name="parttype", values_callable=lambda enum: [e.value for e in enum], validate_strings=False
        ),
        nullable=False,
    )
    unit_of_measure = Column(
        SQLEnum(
            UnitOfMeasure,
            name="unitofmeasure",
            values_callable=lambda enum: [e.value for e in enum],
            validate_strings=False,
        ),
        default=UnitOfMeasure.EACH,
    )

    # Costing
    standard_cost = Column(Float, default=0.0)
    material_cost = Column(Float, default=0.0)
    labor_cost = Column(Float, default=0.0)
    overhead_cost = Column(Float, default=0.0)

    # Lead times (in days)
    lead_time_days = Column(Integer, default=0)

    # Inventory settings
    safety_stock = Column(Float, default=0.0)
    reorder_point = Column(Float, default=0.0)
    reorder_quantity = Column(Float, default=0.0)

    # Classification for AS9100D
    is_critical = Column(Boolean, default=False)  # Critical characteristic tracking
    requires_inspection = Column(Boolean, default=True)
    inspection_requirements = Column(Text)

    # Manufacturing behavior
    # When True, completing an operation / work order for this part auto-issues
    # (backflushes) its BOM components from inventory. OPT-IN per part, default
    # OFF so it never double-counts material a shop issues manually.
    backflush_components = Column(Boolean, nullable=False, server_default="false", default=False)

    # Status
    is_active = Column(Boolean, default=True)
    status = Column(String(50), default="active")  # active, obsolete, pending_approval

    # Lock-step with migration 086_part_active_before_delete. A SIDECAR to the
    # delete/restore pair in app/api/endpoints/parts.py -- NOT a general-purpose
    # activity flag, and nothing else reads or writes it:
    #   delete_part  records the CURRENT is_active here, THEN forces
    #                is_active = False (order matters -- reversed, it always
    #                records False).
    #   restore_part sets is_active = COALESCE(is_active_before_delete, False),
    #                sets `status` consistently with that resolution, then clears
    #                this back to NULL so a second delete/restore cycle cannot
    #                read a stale value.
    #
    # THE BUG IT FIXES: restore_part used to hard-code `is_active = True` /
    # `status = "active"`. That was harmless while "inactive but not deleted" was
    # unreachable for a part -- PartUpdate carries neither column and delete_part
    # was their only writer. The Combine/Merge SKUs feature CREATES that state at
    # scale (POST /parts/{id}/deactivate, and the combine's deactivate_source),
    # so a deliberately retired SKU that somebody deleted and somebody else
    # restored came back ACTIVE and selectable again in every picker, with no
    # audit row saying anyone decided to re-activate it. Invariant 3: a restore
    # returns the RECORD, not the permission.
    #
    # NULL means "we never recorded one" -- every part deleted before 086 shipped,
    # plus anything deleted through DELETE /materials/{id}, which is a second
    # soft-delete writer of parts.is_active that does not (yet) record the
    # sidecar. Restore treats that unknown as INACTIVE and falls back to False,
    # NOT to the pre-086 unconditional True: restoring too restrictively costs one
    # explicit audited re-activation and is visible immediately, while restoring
    # too permissively is indistinguishable from a legitimate approval and is
    # never detected. Deliberate break from the old behavior -- do not "restore
    # compatibility" by flipping it back.
    #
    # Nullable for exactly that reason; forward-only, never backfilled, and never
    # tightened to NOT NULL -- NULL has to stay reachable to mean "deleted before
    # 086". Kept OUT of PartBase/PartCreate/PartUpdate so no blind-setattr PUT can
    # pre-seed what the restore reads back.
    #
    # (The delete keeps writing is_active = False on purpose -- GET /parts/ and
    # GET /materials/ both default active_only=True and filter that flag; it is a
    # deliberate second layer behind the is_deleted filters, not redundancy.)
    is_active_before_delete = Column(Boolean, nullable=True, default=None)

    # Customer/Supplier info
    customer_name = Column(String(255), nullable=True)
    customer_part_number = Column(String(100))
    # Lineage FK mirrors migration 080 (originally 003; skipped by the
    # create_all+stamp bootstrap).
    primary_supplier_id = Column(
        Integer, ForeignKey("vendors.id", ondelete="SET NULL", name="fk_parts_primary_supplier"), nullable=True
    )

    # Drawing/Document references
    drawing_number = Column(String(100))

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL", name="fk_parts_created_by"), nullable=True)

    # Relationships
    bom = relationship("BOM", back_populates="part", uselist=False)
    inventory_items = relationship("InventoryItem", back_populates="part")
    documents = relationship("Document", back_populates="part")
