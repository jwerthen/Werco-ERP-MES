/** Wire contract for the versioned fabrication quote engine. Decimal strings preserve precision. */
export type Decimal = string | null;
export interface Evidence { reviewed: boolean; source: string | null; status: 'assumption' | 'measured' | 'validated'; note: string | null }
export interface PartDefinition { id: string; name: string; make_or_buy: 'make' | 'buy'; costing_complete: boolean; purchase_unit_cost: Decimal; evidence: Evidence }
export interface RootDemand { part_id: string; quantity: string }
export interface BomEdge { id: string; parent_id: string; child_id: string; quantity: string }
export interface MaterialLine { id: string; part_id: string; description: string; quantity_basis: 'per_unit' | 'per_batch'; batch_size: string; consumed_quantity: Decimal; unit: 'kg' | 'lb' | 'mm2' | 'm2' | 'ft2' | 'sheet' | 'each'; unit_cost: Decimal; evidence: Evidence }
export interface ManualRecipe { kind: 'manual'; labor_seconds: Decimal; machine_seconds: Decimal }
export interface LaserCutClass { cut_length_mm: string; speed_mm_per_second: Decimal; pierces: number; pierce_seconds: Decimal }
export interface LaserRecipe { kind: 'laser'; cuts: LaserCutClass[]; noncut_machine_seconds: Decimal; labor_seconds: Decimal; speed_includes_dynamics: boolean; dynamics_allowance_seconds: Decimal }
export interface BrakeRecipe { kind: 'brake'; hits: number; seconds_per_hit: Decimal; handling_seconds: Decimal; inspection_seconds: Decimal; crew_size: number; machine_seconds: Decimal; feasibility_reviewed: boolean }
export interface WeldRecipe { kind: 'weld'; process: 'MIG' | 'TIG' | 'fiber_laser'; weld_length_mm: Decimal; weld_size_mm: Decimal; travel_speed_mm_per_second: Decimal; nonweld_labor_seconds: Decimal; nonweld_machine_seconds: Decimal; crew_size: number; procedure_reference: string | null }
export type Recipe = ManualRecipe | LaserRecipe | BrakeRecipe | WeldRecipe;
export interface OperationLine { id: string; part_id: string; name: string; process: string; setup_basis: 'per_quote' | 'per_batch'; run_basis: 'per_unit' | 'per_batch'; batch_size: string; setup_labor_seconds: Decimal; setup_machine_seconds: Decimal; labor_rate_per_hour: Decimal; machine_rate_per_hour: Decimal; consumables_cost_per_run: Decimal; outside_cost_per_run: Decimal; recipe: Recipe; evidence: Evidence }
export interface PriceBreak { minimum_quantity: string; price: string }
export interface SupplierOffer { id: string; manufacturer: string; mpn: string; supplier: string; currency: string; price_unit_quantity: string; price_breaks: PriceBreak[]; pack_quantity: number; minimum_order_quantity: number; order_multiple: number; quoted_on: string | null; valid_until: string | null; max_age_days: number; applicable: boolean; freight: Decimal; evidence: Evidence }
export interface HardwareLine { id: string; part_id: string; manufacturer: string; mpn: string; quantity_per_part: string; stock_available: number; stock_unit_value: Decimal; offer: SupplierOffer | null; evidence: Evidence }
export interface Assumption { id: string; description: string; reviewed: boolean; source: string | null }
export interface SourceReview { file_id: number; sha256: string; disposition: 'reviewed' | 'excluded'; note: string }
export interface QuotePlan { schema_version: '1'; currency: string; parts: PartDefinition[]; roots: RootDemand[]; bom: BomEdge[]; materials: MaterialLine[]; operations: OperationLine[]; hardware: HardwareLine[]; assumptions: Assumption[]; source_reviews: SourceReview[]; target_margin: Decimal }
export interface CalculationIssue { severity: 'blocking' | 'warning'; code: string; message: string; path: string }
export interface CalculationResult { engine_version: string; input_hash: string; can_approve: boolean; issues: CalculationIssue[]; totals: Record<string, string | null>; demand?: { part_id: string; quantity: string; make_or_buy: string }[]; operation_lines?: Record<string, unknown>[]; material_lines?: Record<string, unknown>[]; hardware_lines?: Record<string, unknown>[]; purchased_part_lines?: Record<string, unknown>[]; [key: string]: unknown }
export interface QuoteFile { id: number; file_name: string; sha256: string; byte_count?: number; content_type?: string; analysis?: Record<string, unknown>; [key: string]: unknown }
export interface QuoteRecord { id: number; title: string; customer_id: number | null; status: string; revision: number; plan: QuotePlan; calculation: CalculationResult | null; files: QuoteFile[]; approved_at?: string | null; erp_quote_id?: number | null; updated_at?: string }
export type QuoteSummary = Pick<QuoteRecord, 'id' | 'title' | 'customer_id' | 'status' | 'revision' | 'updated_at'>;
export interface QuoteWrite { title: string; customer_id: number | null; plan: QuotePlan }
export interface ActualObservation { request_key: string; quote_revision: number; operation_id: string; observed_on: string; good_quantity: string; scrap_quantity: string; setup_labor_seconds: Decimal; run_labor_seconds: Decimal; machine_seconds: Decimal; observed_cost: Decimal; source: string; note: string; completeness: 'partial' | 'complete' }
export interface RevisionSummary { revision: number; action: string; note: string | null; created_at: string; content_sha256: string }
export interface ProcessProfileWrite { key?: string; expected_revision?: number; name: string; process: string; machine: string | null; material: string | null; thickness_mm: Decimal; currency: string; template: OperationLine; evidence_note: string }
export interface ProcessProfile extends Omit<ProcessProfileWrite, 'expected_revision'> { id: number; key: string; revision: number; created_at: string; created_by: number; content_sha256?: string }
export const currencies = ['USD', 'CAD', 'EUR', 'GBP', 'MXN', 'JPY', 'CNY', 'CHF', 'AUD', 'NZD', 'SEK', 'NOK', 'DKK', 'INR', 'KRW', 'SGD'];

export const newEvidence = (): Evidence => ({ reviewed: false, source: null, status: 'assumption', note: null });
export const emptyPlan = (): QuotePlan => ({ schema_version: '1', currency: 'USD', parts: [], roots: [], bom: [], materials: [], operations: [], hardware: [], assumptions: [], source_reviews: [], target_margin: null });
export function newId(prefix: string): string { return `${prefix}-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`; }
export function newRecipe(kind: Recipe['kind']): Recipe {
  switch (kind) {
    case 'laser': return { kind, cuts: [], noncut_machine_seconds: null, labor_seconds: null, speed_includes_dynamics: false, dynamics_allowance_seconds: null };
    case 'brake': return { kind, hits: NaN, seconds_per_hit: null, handling_seconds: null, inspection_seconds: null, crew_size: 1, machine_seconds: null, feasibility_reviewed: false };
    case 'weld': return { kind, process: 'MIG', weld_length_mm: null, weld_size_mm: null, travel_speed_mm_per_second: null, nonweld_labor_seconds: null, nonweld_machine_seconds: null, crew_size: 1, procedure_reference: null };
    default: return { kind: 'manual', labor_seconds: null, machine_seconds: null };
  }
}
