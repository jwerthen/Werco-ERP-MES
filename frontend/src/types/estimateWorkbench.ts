/** Types for the Estimate Workbench (Excel-replacement quoting). */

export type ConfidenceLevel = 'confirmed' | 'majority' | 'review';
export type MaterialFamily = 'mild' | 'stainless' | 'aluminum';

export interface CalcMessage {
  code: string;
  message: string;
  field?: string | null;
  suggested_value?: number | null;
}

export interface FabLineDraft {
  detail_name: string;
  part_number?: string | null;
  material: string;
  material_family_override?: MaterialFamily | string | null;
  qty: number;
  thickness_in?: number | null;
  width_in?: number | null;
  length_in?: number | null;
  cut_length_in?: number | null;
  pierce_count: number;
  bend_count: number;
  weld_length_in?: number | null;
  weld_minutes_ea?: number | null;
  include_material: boolean;
  include_laser: boolean;
  include_brake: boolean;
  include_weld: boolean;
  price_per_lb?: number;
  density_lb_per_in3?: number;
  confidence?: ConfidenceLevel | string;
  verification_note?: string | null;
  sort_order?: number;
  // Cached outputs (from server)
  id?: number;
  weight_ea_lb?: number | null;
  material_cost?: number;
  laser_cost?: number;
  laser_hours?: number;
  brake_cost?: number;
  brake_hours?: number;
  weld_cost?: number;
  weld_hours?: number;
  line_total?: number;
  calc_warnings?: CalcMessage[] | null;
  calc_errors?: CalcMessage[] | null;
  version?: number;
}

export interface BuyoutLineDraft {
  description: string;
  qty: number;
  unit_cost: number;
  category?: string | null;
  vendor?: string | null;
  part_number?: string | null;
  part_id?: number | null;
  price_source?: string | null;
  confidence?: ConfidenceLevel | string;
  verification_note?: string | null;
  sort_order?: number;
  id?: number;
  extended_cost?: number;
  version?: number;
}

export interface MachinedLineDraft {
  description: string;
  material: string;
  qty: number;
  part_number?: string | null;
  stock_dia_in?: number | null;
  stock_length_in?: number | null;
  turning_minutes: number;
  milling_minutes: number;
  price_per_lb?: number;
  density_lb_per_in3?: number;
  confidence?: ConfidenceLevel | string;
  verification_note?: string | null;
  sort_order?: number;
  id?: number;
  weight_ea_lb?: number | null;
  material_cost?: number;
  turning_cost?: number;
  turning_hours?: number;
  milling_cost?: number;
  milling_hours?: number;
  line_total?: number;
  version?: number;
}

export interface AssemblyDraft {
  name: string;
  assembly_labor_hrs: number;
  electrical_labor_hrs: number;
  notes?: string | null;
  sort_order?: number;
  fab_lines: FabLineDraft[];
  buyout_lines: BuyoutLineDraft[];
  id?: number;
  version?: number;
}

export interface BidSummary {
  fab_material: number;
  fab_laser: number;
  fab_brake: number;
  fab_weld: number;
  fab_subtotal: number;
  buyout_subtotal: number;
  buyout_marked_up: number;
  assembly_labor_cost: number;
  electrical_labor_cost: number;
  machined_subtotal: number;
  laser_hours: number;
  brake_hours: number;
  weld_hours: number;
  assembly_hours: number;
  electrical_hours: number;
  subtotal_before_oh: number;
  overhead: number;
  consumables: number;
  cogs: number;
  sell_price: number;
  target_margin: number;
  errors?: CalcMessage[];
}

export interface RecalcResponse {
  fab_lines: Array<{
    detail_name?: string | null;
    part_number?: string | null;
    material_family: string;
    weight_ea_lb: number;
    material_cost: number;
    laser_cost: number;
    laser_hours: number;
    brake_cost: number;
    brake_hours: number;
    weld_cost: number;
    weld_hours: number;
    weld_minutes_ea: number;
    line_total: number;
    cut_length_used: number;
    errors: CalcMessage[];
    warnings: CalcMessage[];
  }>;
  machined_parts: Array<{
    description?: string | null;
    weight_ea_lb: number;
    material_cost: number;
    turning_cost: number;
    turning_hours: number;
    milling_cost: number;
    milling_hours: number;
    line_total: number;
  }>;
  bid_summary: BidSummary;
  shop_data_source: string;
}

export interface WorkbenchResponse {
  estimate_id: number;
  rfq_package_id: number;
  quote_id?: number | null;
  version: number;
  currency: string;
  grand_total: number;
  material_total: number;
  hardware_consumables_total: number;
  shop_labor_oh_total: number;
  margin_total: number;
  internal_breakdown?: Record<string, number | string> | null;
  assemblies: Array<{
    id: number;
    name: string;
    sort_order: number;
    assembly_labor_hrs: number;
    electrical_labor_hrs: number;
    notes?: string | null;
    version: number;
    fab_lines: FabLineDraft[];
    buyout_lines: BuyoutLineDraft[];
  }>;
  machined_parts: MachinedLineDraft[];
  shop_data_source?: string | null;
  verification?: VerificationReport | null;
}

export interface PriorityAction {
  category: string;
  line_id: number;
  assembly_id?: number | null;
  assembly_name?: string | null;
  label: string;
  confidence: string;
  reason: string;
  anchor: string;
  line_total: number;
}

export interface CategorySummary {
  label: string;
  total: number;
  count: number;
  confirmed: number;
  majority: number;
  review: number;
}

export interface VerificationReport {
  estimate_id: number;
  status: string;
  can_finalize: boolean;
  review_count: number;
  blocker_count: number;
  categories: CategorySummary[];
  priority_actions: PriorityAction[];
  blockers: Array<Record<string, unknown>>;
  banner?: string | null;
}

export interface ExtractionSummary {
  fab_count: number;
  buyout_count: number;
  review_count: number;
  majority_count: number;
  confirmed_count: number;
}

export interface ExtractFromRfqResponse {
  mode: string;
  assemblies: AssemblyDraft[];
  machined_parts: MachinedLineDraft[];
  summary: ExtractionSummary;
  warnings: string[];
  applied: boolean;
  workbench?: WorkbenchResponse | null;
  extraction_artifact?: Record<string, unknown> | null;
}

export function emptyFabLine(index = 0): FabLineDraft {
  return {
    detail_name: `Detail ${index + 1}`,
    material: 'A36 Mild Steel',
    qty: 1,
    pierce_count: 0,
    bend_count: 0,
    include_material: true,
    include_laser: true,
    include_brake: true,
    include_weld: true,
    confidence: 'review',
    material_cost: 0,
    laser_cost: 0,
    laser_hours: 0,
    brake_cost: 0,
    brake_hours: 0,
    weld_cost: 0,
    weld_hours: 0,
    line_total: 0,
  };
}

export function emptyBuyoutLine(): BuyoutLineDraft {
  return {
    description: '',
    qty: 1,
    unit_cost: 0,
    confidence: 'review',
    extended_cost: 0,
  };
}

export function emptyMachinedLine(index = 0): MachinedLineDraft {
  return {
    description: `Machined ${index + 1}`,
    material: '1018 CD Bar',
    qty: 1,
    turning_minutes: 0,
    milling_minutes: 0,
    confidence: 'review',
    material_cost: 0,
    turning_cost: 0,
    turning_hours: 0,
    milling_cost: 0,
    milling_hours: 0,
    line_total: 0,
  };
}

export function emptyAssembly(index = 0): AssemblyDraft {
  return {
    name: `Assembly ${index + 1}`,
    assembly_labor_hrs: 0,
    electrical_labor_hrs: 0,
    fab_lines: [emptyFabLine(0)],
    buyout_lines: [],
  };
}

export function workbenchToDrafts(wb: WorkbenchResponse): {
  assemblies: AssemblyDraft[];
  machined_parts: MachinedLineDraft[];
} {
  return {
    assemblies: (wb.assemblies || []).map((a) => ({
      id: a.id,
      name: a.name,
      sort_order: a.sort_order,
      assembly_labor_hrs: a.assembly_labor_hrs,
      electrical_labor_hrs: a.electrical_labor_hrs,
      notes: a.notes,
      version: a.version,
      fab_lines: (a.fab_lines || []).map((f) => ({ ...f })),
      buyout_lines: (a.buyout_lines || []).map((b) => ({ ...b })),
    })),
    machined_parts: (wb.machined_parts || []).map((m) => ({ ...m })),
  };
}
