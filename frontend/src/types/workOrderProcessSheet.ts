/**
 * Process-sheet links on work-order creation (wo_change/2).
 *
 * `GET /work-orders/preview-operations/{part_id}` rows carry the routing
 * operation each row came from and the sheet attached to it. New Work Order
 * sends `routing_operation_id` back on every supplied operation that came from
 * a preview row, which is what makes the server snapshot that step's process
 * sheet. The server matches the link BY ID, never by sequence or name.
 *
 * The new row fields are optional in this type: the SPA and API deploy
 * separately, and an older API returns rows without them.
 */

/** One row of `operations_preview`. */
export interface WorkOrderOperationPreviewRow {
  name: string;
  work_center_id: number;
  work_center_name?: string;
  setup_hours?: number;
  run_hours_per_unit?: number;
  setup_instructions?: string | null;
  run_instructions?: string | null;
  requires_inspection?: boolean | null;
  /** Null on the job part's own routing rows. */
  component_part_id?: number | null;
  component_part_number?: string | null;
  component_quantity?: number | null;
  operation_group?: string | null;
  routing_operation_id?: number;
  /** The sequence the routed create would assign. */
  sequence?: number;
  /** The operation number the routed create would assign (verbatim from the routing on a part with no BOM). */
  operation_number?: string;
  process_sheet_id?: number | null;
  process_sheet_number?: string | null;
  /** `false`: the sheet has no released revision, so creating would be refused 409 PROCESS_SHEET_UNAVAILABLE. `null`: no sheet. */
  process_sheet_released?: boolean | null;
}

/** `GET /work-orders/preview-operations/{part_id}`. */
export interface WorkOrderOperationsPreview {
  part_id?: number;
  part_number?: string;
  part_type?: string;
  is_assembly?: boolean;
  quantity?: number;
  bom_found: boolean;
  bom_status?: string | null;
  bom_items_count?: number;
  operations_preview: WorkOrderOperationPreviewRow[];
}

export type RoutingOperationUnmatchedReason = 'not_found' | 'not_on_released_routing' | 'component_mismatch';

/** 409 `detail` when a supplied `routing_operation_id` does not match the part's released routing. */
export interface RoutingOperationUnmatchedDetail {
  code: 'ROUTING_OPERATION_UNMATCHED';
  detail: string;
  operations: Array<{
    sequence: number;
    operation_number: string;
    routing_operation_id: number;
    reason: RoutingOperationUnmatchedReason;
  }>;
}

/** A sheet-bearing routing step the work order would carry if created from its routing today. */
export interface WorkOrderProcessSheetGapStep {
  routing_operation_id: number;
  operation_number: string;
  operation_sequence: number;
  name: string;
  component_part_id: number | null;
  component_part_number: string | null;
  process_sheet_id: number | null;
  sheet_number: string | null;
}

/** One live work order whose part's released routing carries process sheets, holding none. */
export interface WorkOrderProcessSheetGap {
  work_order_id: number;
  work_order_number: string;
  status: string;
  part_id: number;
  part_number: string;
  part_name: string | null;
  quantity_ordered: number;
  /** Date only, `YYYY-MM-DD`. */
  due_date: string | null;
  customer_name: string | null;
  /** UTC ISO-8601 with `Z`. */
  created_at: string | null;
  operation_count: number;
  expected_process_sheet_steps: WorkOrderProcessSheetGapStep[];
}

/** `GET /work-orders/process-sheet-gaps`. */
export interface WorkOrderProcessSheetGapResponse {
  /** Every matching work order, before `limit`. */
  total: number;
  items: WorkOrderProcessSheetGap[];
}

export function isRoutingOperationUnmatchedDetail(value: unknown): value is RoutingOperationUnmatchedDetail {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as { code?: unknown; operations?: unknown };
  return candidate.code === 'ROUTING_OPERATION_UNMATCHED' && Array.isArray(candidate.operations);
}
