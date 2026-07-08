"""Pydantic schemas for the estimate workbench API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.base import UTCModel


# ---------------------------------------------------------------------------
# Recalc (stateless)
# ---------------------------------------------------------------------------


class FabLineRecalcIn(BaseModel):
    material: str = ""
    qty: int = 1
    thickness_in: Optional[float] = None
    width_in: Optional[float] = None
    length_in: Optional[float] = None
    cut_length_in: Optional[float] = None
    pierce_count: int = 0
    bend_count: int = 0
    weld_length_in: Optional[float] = None
    weld_minutes_ea: Optional[float] = None
    material_family_override: Optional[str] = None  # mild|stainless|aluminum
    include_material: bool = True
    include_laser: bool = True
    include_brake: bool = True
    include_weld: bool = True
    price_per_lb: float = 0.0
    density_lb_per_in3: float = 0.284
    detail_name: Optional[str] = None
    part_number: Optional[str] = None


class BuyoutLineRecalcIn(BaseModel):
    qty: float = 1.0
    unit_cost: float = 0.0
    description: str = ""
    category: Optional[str] = None
    vendor: Optional[str] = None
    part_number: Optional[str] = None
    part_id: Optional[int] = None
    price_source: Optional[str] = None
    confidence: Optional[str] = None
    verification_note: Optional[str] = None


class MachinedLineRecalcIn(BaseModel):
    material: str = ""
    qty: int = 1
    stock_dia_in: Optional[float] = None
    stock_length_in: Optional[float] = None
    turning_minutes: float = 0.0
    milling_minutes: float = 0.0
    price_per_lb: float = 0.0
    density_lb_per_in3: float = 0.284
    description: Optional[str] = None
    part_number: Optional[str] = None
    confidence: Optional[str] = None
    verification_note: Optional[str] = None


class AssemblyRecalcIn(BaseModel):
    name: str = "Assembly"
    assembly_labor_hrs: float = 0.0
    electrical_labor_hrs: float = 0.0
    notes: Optional[str] = None
    sort_order: Optional[int] = None
    fab_lines: List[FabLineRecalcIn] = Field(default_factory=list)
    buyout_lines: List[BuyoutLineRecalcIn] = Field(default_factory=list)


class RatesOverrideIn(BaseModel):
    """Optional overrides; missing fields fall back to DB / Excel defaults."""

    laser_rate: Optional[float] = None
    brake_rate: Optional[float] = None
    weld_rate: Optional[float] = None
    scrap_factor: Optional[float] = None
    laser_speed_fallback: Optional[float] = None
    pierce_time_fallback: Optional[float] = None
    target_margin: Optional[float] = None
    overhead_markup: Optional[float] = None
    buyout_markup: Optional[float] = None
    consumables_per_job: Optional[float] = None
    assembly_labor_rate: Optional[float] = None
    electrical_labor_rate: Optional[float] = None
    cnc_turning_rate: Optional[float] = None
    cnc_mill_rate: Optional[float] = None


class RecalcRequest(BaseModel):
    assemblies: List[AssemblyRecalcIn] = Field(default_factory=list)
    machined_parts: List[MachinedLineRecalcIn] = Field(default_factory=list)
    rates: Optional[RatesOverrideIn] = None


class CalcMessageOut(BaseModel):
    code: str
    message: str
    field: Optional[str] = None
    suggested_value: Optional[float] = None


class FabLineRecalcOut(BaseModel):
    detail_name: Optional[str] = None
    part_number: Optional[str] = None
    material_family: str
    weight_ea_lb: float
    material_cost: float
    laser_cost: float
    laser_hours: float
    brake_cost: float
    brake_hours: float
    weld_cost: float
    weld_hours: float
    weld_minutes_ea: float
    line_total: float
    cut_length_used: float
    errors: List[CalcMessageOut] = Field(default_factory=list)
    warnings: List[CalcMessageOut] = Field(default_factory=list)


class MachinedLineRecalcOut(BaseModel):
    description: Optional[str] = None
    weight_ea_lb: float
    material_cost: float
    turning_cost: float
    turning_hours: float
    milling_cost: float
    milling_hours: float
    line_total: float


class BidSummaryOut(BaseModel):
    fab_material: float
    fab_laser: float
    fab_brake: float
    fab_weld: float
    fab_subtotal: float
    buyout_subtotal: float
    buyout_marked_up: float
    assembly_labor_cost: float
    electrical_labor_cost: float
    machined_subtotal: float
    laser_hours: float
    brake_hours: float
    weld_hours: float
    assembly_hours: float
    electrical_hours: float
    subtotal_before_oh: float
    overhead: float
    consumables: float
    cogs: float
    sell_price: float
    target_margin: float
    errors: List[CalcMessageOut] = Field(default_factory=list)


class RecalcResponse(BaseModel):
    fab_lines: List[FabLineRecalcOut]
    machined_parts: List[MachinedLineRecalcOut]
    bid_summary: BidSummaryOut
    shop_data_source: str = "defaults"


# ---------------------------------------------------------------------------
# Persist tree (GET / PUT / POST create)
# ---------------------------------------------------------------------------


class FabLinePersistIn(FabLineRecalcIn):
    sort_order: Optional[int] = None
    confidence: Optional[str] = None
    verification_note: Optional[str] = None


class BuyoutLinePersistIn(BuyoutLineRecalcIn):
    sort_order: Optional[int] = None


class MachinedLinePersistIn(MachinedLineRecalcIn):
    sort_order: Optional[int] = None


class AssemblyPersistIn(BaseModel):
    name: str = "Assembly"
    assembly_labor_hrs: float = 0.0
    electrical_labor_hrs: float = 0.0
    notes: Optional[str] = None
    sort_order: Optional[int] = None
    fab_lines: List[FabLinePersistIn] = Field(default_factory=list)
    buyout_lines: List[BuyoutLinePersistIn] = Field(default_factory=list)


class WorkbenchSaveRequest(BaseModel):
    version: int = Field(..., description="Optimistic lock — must match current estimate.version")
    assemblies: List[AssemblyPersistIn] = Field(default_factory=list)
    machined_parts: List[MachinedLinePersistIn] = Field(default_factory=list)


class WorkbenchCreateRequest(BaseModel):
    rfq_package_id: int


class FabLineOut(UTCModel):
    id: int
    sort_order: int
    part_number: Optional[str] = None
    detail_name: str
    material: str
    material_family_override: Optional[str] = None
    qty: int
    thickness_in: Optional[float] = None
    width_in: Optional[float] = None
    length_in: Optional[float] = None
    cut_length_in: Optional[float] = None
    pierce_count: int
    bend_count: int
    weld_length_in: Optional[float] = None
    weld_minutes_ea: Optional[float] = None
    include_material: bool
    include_laser: bool
    include_brake: bool
    include_weld: bool
    weight_ea_lb: Optional[float] = None
    material_cost: float
    laser_cost: float
    laser_hours: float
    brake_cost: float
    brake_hours: float
    weld_cost: float
    weld_hours: float
    line_total: float
    calc_warnings: Optional[List[Dict[str, Any]]] = None
    calc_errors: Optional[List[Dict[str, Any]]] = None
    confidence: str
    verification_note: Optional[str] = None
    version: int


class BuyoutLineOut(UTCModel):
    id: int
    sort_order: int
    category: Optional[str] = None
    vendor: Optional[str] = None
    part_number: Optional[str] = None
    part_id: Optional[int] = None
    description: str
    qty: float
    unit_cost: float
    extended_cost: float
    price_source: Optional[str] = None
    confidence: str
    verification_note: Optional[str] = None
    version: int


class MachinedLineOut(UTCModel):
    id: int
    sort_order: int
    part_number: Optional[str] = None
    description: str
    material: str
    qty: int
    stock_dia_in: Optional[float] = None
    stock_length_in: Optional[float] = None
    turning_minutes: float
    milling_minutes: float
    weight_ea_lb: Optional[float] = None
    material_cost: float
    turning_cost: float
    turning_hours: float
    milling_cost: float
    milling_hours: float
    line_total: float
    confidence: str
    verification_note: Optional[str] = None
    version: int


class AssemblyOut(UTCModel):
    id: int
    name: str
    sort_order: int
    assembly_labor_hrs: float
    electrical_labor_hrs: float
    notes: Optional[str] = None
    version: int
    fab_lines: List[FabLineOut] = Field(default_factory=list)
    buyout_lines: List[BuyoutLineOut] = Field(default_factory=list)


class WorkbenchResponse(UTCModel):
    estimate_id: int
    rfq_package_id: int
    quote_id: Optional[int] = None
    version: int
    currency: str
    grand_total: float
    material_total: float
    hardware_consumables_total: float
    shop_labor_oh_total: float
    margin_total: float
    internal_breakdown: Optional[Dict[str, Any]] = None
    assemblies: List[AssemblyOut] = Field(default_factory=list)
    machined_parts: List[MachinedLineOut] = Field(default_factory=list)
    shop_data_source: Optional[str] = None
    verification: Optional["VerificationReportOut"] = None


# ---------------------------------------------------------------------------
# Phase 3 — Verification + finalize
# ---------------------------------------------------------------------------


class PriorityActionOut(BaseModel):
    category: str
    line_id: int
    assembly_id: Optional[int] = None
    assembly_name: Optional[str] = None
    label: str
    confidence: str
    reason: str
    anchor: str
    line_total: float = 0.0


class CategorySummaryOut(BaseModel):
    label: str
    total: float
    count: int
    confirmed: int
    majority: int
    review: int


class VerificationReportOut(BaseModel):
    estimate_id: int
    status: str
    can_finalize: bool
    review_count: int
    blocker_count: int
    categories: List[CategorySummaryOut]
    priority_actions: List[PriorityActionOut]
    blockers: List[Dict[str, Any]] = Field(default_factory=list)
    banner: Optional[str] = None


class FinalizeRequest(BaseModel):
    valid_days: int = 30
    force: bool = False  # admin escape hatch — still audited


class FinalizeResponse(BaseModel):
    estimate_id: int
    quote_id: int
    quote_number: str
    grand_total: float
    forced: bool = False
    verification: VerificationReportOut


# ---------------------------------------------------------------------------
# Phase 4 — PDF / RFQ extraction assist
# ---------------------------------------------------------------------------


class ExtractFromRfqRequest(BaseModel):
    rfq_package_id: Optional[int] = None  # default: estimate's package
    use_llm: bool = True
    apply: bool = False  # if True, replace workbench tree with draft
    replace: bool = True  # when apply: replace vs merge into first assembly
    version: Optional[int] = None  # required when apply=True


class ExtractionSummaryOut(BaseModel):
    fab_count: int = 0
    buyout_count: int = 0
    review_count: int = 0
    majority_count: int = 0
    confirmed_count: int = 0


class FabLineDraftOut(BaseModel):
    """Pre-persist fab line from extraction (no DB id yet)."""

    detail_name: str = "Detail"
    part_number: Optional[str] = None
    material: str = ""
    qty: int = 1
    thickness_in: Optional[float] = None
    width_in: Optional[float] = None
    length_in: Optional[float] = None
    cut_length_in: Optional[float] = None
    pierce_count: int = 0
    bend_count: int = 0
    weld_length_in: Optional[float] = None
    include_material: bool = True
    include_laser: bool = True
    include_brake: bool = True
    include_weld: bool = False
    confidence: Optional[str] = None
    verification_note: Optional[str] = None
    field_confidence: Optional[Dict[str, Any]] = None


class BuyoutLineDraftOut(BaseModel):
    description: str = "Buyout"
    part_number: Optional[str] = None
    category: Optional[str] = None
    vendor: Optional[str] = None
    qty: float = 1.0
    unit_cost: float = 0.0
    price_source: Optional[str] = None
    confidence: Optional[str] = None
    verification_note: Optional[str] = None
    field_confidence: Optional[Dict[str, Any]] = None


class AssemblyDraftOut(BaseModel):
    name: str = "Assembly"
    sort_order: int = 0
    assembly_labor_hrs: float = 0.0
    electrical_labor_hrs: float = 0.0
    fab_lines: List[FabLineDraftOut] = Field(default_factory=list)
    buyout_lines: List[BuyoutLineDraftOut] = Field(default_factory=list)


class ExtractFromRfqResponse(BaseModel):
    mode: str
    assemblies: List[AssemblyDraftOut] = Field(default_factory=list)
    machined_parts: List[Dict[str, Any]] = Field(default_factory=list)
    summary: ExtractionSummaryOut
    warnings: List[str] = Field(default_factory=list)
    applied: bool = False
    workbench: Optional[WorkbenchResponse] = None
    extraction_artifact: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Phase 5 — Shop Data + quoted-vs-actual
# ---------------------------------------------------------------------------


class CutBendRowOut(BaseModel):
    id: int
    table_id: int
    sort_order: int
    thickness_in: Optional[float] = None
    gauge: Optional[int] = None
    mild_steel: Optional[float] = None
    stainless: Optional[float] = None
    aluminum: Optional[float] = None
    value: Optional[float] = None
    fillet_leg_in: Optional[float] = None
    arc_in_per_min: Optional[float] = None
    min_per_in: Optional[float] = None
    notes: Optional[str] = None


class CutBendTableOut(BaseModel):
    id: int
    kind: str
    name: str
    description: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    rows: List[CutBendRowOut] = Field(default_factory=list)
    updated_at: Optional[str] = None


class ShopDataTablesResponse(BaseModel):
    tables: List[CutBendTableOut]
    source: str = "db"


class CutBendRowUpdateRequest(BaseModel):
    note: str = Field(..., min_length=1, description="Required: why this cell changed")
    thickness_in: Optional[float] = None
    gauge: Optional[int] = None
    mild_steel: Optional[float] = None
    stainless: Optional[float] = None
    aluminum: Optional[float] = None
    value: Optional[float] = None
    fillet_leg_in: Optional[float] = None
    arc_in_per_min: Optional[float] = None
    min_per_in: Optional[float] = None
    notes: Optional[str] = None


class CutBendRowCreateRequest(BaseModel):
    note: str = Field(..., min_length=1)
    thickness_in: Optional[float] = None
    gauge: Optional[int] = None
    mild_steel: Optional[float] = None
    stainless: Optional[float] = None
    aluminum: Optional[float] = None
    value: Optional[float] = None
    fillet_leg_in: Optional[float] = None
    arc_in_per_min: Optional[float] = None
    min_per_in: Optional[float] = None
    notes: Optional[str] = None


class ShopDataHistoryItemOut(BaseModel):
    id: int
    entity_id: Optional[int] = None
    entity_name: Optional[str] = None
    action: str
    field_changed: Optional[str] = None
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    note: Optional[str] = None
    changed_by: Optional[int] = None
    changed_at: Optional[str] = None


class JobActualOut(BaseModel):
    id: int
    quote_estimate_id: Optional[int] = None
    work_order_id: Optional[int] = None
    job_label: Optional[str] = None
    quoted_laser_hours: float = 0.0
    quoted_brake_hours: float = 0.0
    quoted_weld_hours: float = 0.0
    actual_laser_hours: Optional[float] = None
    actual_brake_hours: Optional[float] = None
    actual_weld_hours: Optional[float] = None
    delta_laser_pct: Optional[float] = None
    delta_brake_pct: Optional[float] = None
    delta_weld_pct: Optional[float] = None
    notes: Optional[str] = None
    entered_by: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    propose_tune: List[Dict[str, Any]] = Field(default_factory=list)


class JobActualUpsertRequest(BaseModel):
    quote_estimate_id: Optional[int] = None
    work_order_id: Optional[int] = None
    job_label: Optional[str] = None
    actual_laser_hours: Optional[float] = None
    actual_brake_hours: Optional[float] = None
    actual_weld_hours: Optional[float] = None
    quoted_laser_hours: Optional[float] = None
    quoted_brake_hours: Optional[float] = None
    quoted_weld_hours: Optional[float] = None
    notes: Optional[str] = None
