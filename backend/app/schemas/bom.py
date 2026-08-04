from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.bom import BOMItemType, BOMLineType
from app.schemas.base import UTCModel


class BOMItemBase(UTCModel):
    component_part_id: int
    item_number: int = Field(..., gt=0)
    quantity: float = Field(default=1.0, gt=0)
    item_type: BOMItemType
    line_type: BOMLineType = BOMLineType.COMPONENT
    # DELIBERATELY UNSET, NOT "each" (owner decision, 2026-07-27). A BOM line with no
    # stated unit inherits the COMPONENT PART's unit of measure, and that resolution can
    # only happen where the component part is loadable -- which Pydantic is not. Every
    # BOM-line write path therefore runs the caller's value (or the absence of one) through
    # ``api/endpoints/bom.py`` -> ``_resolve_line_uom`` before constructing the ``BOMItem``.
    #
    # The literal "each" this replaced was a default NOBODY CHOSE, and it is read as a
    # STATED CLAIM by the BLOCKING ``unit_of_measure_mismatch`` diagnostic
    # (``completion_inventory_service``): on a sheet-metal shop's data -- components stocked
    # in sheets / lbs / ft -- it refused to arm almost every part for automatic backflush.
    # Do not restore a literal default here; the fix belongs at the write path, not in a
    # schema that cannot see the part it is defaulting for.
    unit_of_measure: Optional[str] = None
    reference_designator: Optional[str] = None
    find_number: Optional[str] = None
    notes: Optional[str] = None
    torque_spec: Optional[str] = None
    installation_notes: Optional[str] = None
    work_center_id: Optional[int] = None
    operation_sequence: int = Field(default=10, gt=0)
    # le=1 matches chk_bom_items_scrap_factor_range, the DB CHECK migration 080
    # restored (0 <= scrap_factor <= 1). This column is a FRACTION -- 0.05 is a
    # 5% allowance -- so entering "5" meaning 5% is the natural user mistake; the
    # ceiling makes it a 422 that names the bound instead of an IntegrityError
    # 500. (The bulk importer never sets scrap_factor; this is the only inbound
    # write path.)
    scrap_factor: float = Field(default=0.0, ge=0, le=1)
    lead_time_offset: int = Field(default=0, ge=0)
    is_optional: bool = False
    is_alternate: bool = False
    alternate_group: Optional[str] = None

    class Config:
        use_enum_values = True


class BOMItemCreate(BOMItemBase):
    class Config:
        use_enum_values = True


class BOMItemUpdate(BaseModel):
    quantity: Optional[float] = Field(None, gt=0)
    item_type: Optional[BOMItemType] = None
    line_type: Optional[BOMLineType] = None
    unit_of_measure: Optional[str] = None
    reference_designator: Optional[str] = None
    find_number: Optional[str] = None
    notes: Optional[str] = None
    torque_spec: Optional[str] = None
    installation_notes: Optional[str] = None
    work_center_id: Optional[int] = None
    operation_sequence: Optional[int] = Field(None, gt=0)
    scrap_factor: Optional[float] = Field(None, ge=0, le=1)
    lead_time_offset: Optional[int] = Field(None, ge=0)
    is_optional: Optional[bool] = None
    is_alternate: Optional[bool] = None
    alternate_group: Optional[str] = None


class ComponentPartInfo(BaseModel):
    """Embedded part info for BOM item responses"""

    id: int
    part_number: str
    name: str
    revision: str
    part_type: str
    has_bom: bool = False

    class Config:
        from_attributes = True


class BOMItemResponse(BOMItemBase):
    id: int
    bom_id: int
    # Re-tightened to non-optional on the way OUT. The base leaves it unset so the INPUT
    # side can mean "resolve this from the component part"; a stored row always has a
    # value, and every response builder in ``api/endpoints/bom.py`` passes one explicitly
    # (falling back to the column's own "each" default for legacy NULL rows). Clients that
    # render a UOM column should not have to handle a null that the write paths cannot
    # produce.
    unit_of_measure: str = "each"
    component_part: Optional[ComponentPartInfo] = None
    # Set only on the three BOM-LINE WRITE responses (add / update / delete), and only when
    # the edited BOM helps state demand for a part armed via ``Part.backflush_components``.
    # It is a WARNING, never a refusal: the opt-in gate is a one-time check at the instant
    # of the flip and the plan deliberately declined a second gate on the engineering-edit
    # path (docs/MATERIAL_CONSUMPTION_PLAN.md -> "Exposing the flag"), so the write
    # SUCCEEDS and says so. Absent (None) on every read path -- a GET is not an edit and
    # has nothing to warn about.
    backflush_armed_warning: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BOMItemWithChildren(BOMItemResponse):
    """BOM item with nested children for multi-level explosion"""

    children: List["BOMItemWithChildren"] = Field(default_factory=list)
    level: int = 0
    extended_quantity: float = 0.0  # quantity * parent quantities

    class Config:
        from_attributes = True


class BOMBase(UTCModel):
    part_id: int
    revision: str = "A"
    description: Optional[str] = None
    bom_type: str = "standard"


class BOMCreate(BOMBase):
    items: List[BOMItemCreate] = Field(default_factory=list)


class BOMUpdate(BaseModel):
    revision: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    bom_type: Optional[str] = None
    effective_date: Optional[datetime] = None


class PartInfo(BaseModel):
    """Embedded part info for BOM responses"""

    id: int
    part_number: str
    name: str
    revision: str
    part_type: str

    class Config:
        from_attributes = True


class BOMResponse(BOMBase):
    id: int
    status: str
    is_active: bool
    effective_date: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    part: Optional[PartInfo] = None
    items: List[BOMItemResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class BOMExploded(BaseModel):
    """Fully exploded multi-level BOM"""

    bom_id: int
    part_id: int
    part_number: str
    part_name: str
    revision: str
    total_levels: int
    items: List[BOMItemWithChildren]

    class Config:
        from_attributes = True


class BOMFlatItem(BaseModel):
    """Flattened BOM item for reports/MRP"""

    level: int
    item_number: int
    find_number: Optional[str]
    part_id: int
    part_number: str
    part_name: str
    part_type: str
    item_type: BOMItemType
    line_type: BOMLineType = BOMLineType.COMPONENT
    quantity_per: float
    extended_quantity: float
    unit_of_measure: str
    scrap_factor: float
    lead_time_offset: int
    is_optional: bool
    is_alternate: bool
    has_children: bool
    torque_spec: Optional[str] = None
    installation_notes: Optional[str] = None


class BOMFlattened(BaseModel):
    """Flattened BOM for tabular display"""

    bom_id: int
    part_number: str
    part_name: str
    revision: str
    total_items: int
    total_unique_parts: int
    items: List[BOMFlatItem]


class BOMLineUomMismatch(UTCModel):
    """One BOM line whose STATED unit of measure contradicts its component part's.

    Every field is here to make the row actionable without a second lookup: the assembly
    it belongs to, the line to open, the component to compare against, and both units side
    by side. ``line_unit_of_measure`` / ``component_unit_of_measure`` are the NORMALISED
    labels the comparison actually used (``models.part.uom_label``), not the raw column
    text, so what the row shows is what the gate compared.

    ``component_is_deleted`` is disclosed rather than filtered: the readiness explosion
    resolves soft-deleted components of this company on purpose (they get their own
    blocking diagnostic and are refused), so dropping them from this list would hide a row
    that still blocks. It answers "why does this line name a part I cannot find".
    """

    bom_id: int
    bom_revision: Optional[str] = None
    bom_status: Optional[str] = None
    bom_is_active: bool = True
    part_id: int
    part_number: str
    bom_item_id: int
    item_number: Optional[int] = None
    component_part_id: int
    component_part_number: str
    component_part_name: Optional[str] = None
    component_is_deleted: bool = False
    line_unit_of_measure: str
    component_unit_of_measure: str
    # False on a line the backflush would never issue anyway (alternate / optional /
    # reference), which therefore raises no diagnostic and refuses no opt-in. Sort these
    # to the bottom of the worklist: they are cosmetic, not blocking.
    blocks_backflush: bool = True


class BOMUomMismatchReport(UTCModel):
    """The pre-arming remediation worklist for ``Part.backflush_components``.

    ``total`` is every disagreeing line the scan found for this tenant under the requested
    filters; ``items`` is the requested page of them. ``truncated`` means the scan hit its
    own candidate ceiling and ``total`` is therefore a FLOOR, not a count -- narrow the
    filters and run it again rather than reading the number as complete.
    """

    total: int
    returned: int
    truncated: bool = False
    items: List[BOMLineUomMismatch] = Field(default_factory=list)


# Required for self-referencing model
BOMItemWithChildren.model_rebuild()
