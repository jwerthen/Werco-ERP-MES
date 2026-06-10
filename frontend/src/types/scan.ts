// A0.4 QR traveler / badge scan plumbing — typed contract for
// POST /api/v1/scanner/resolve-action. Mirrors backend/app/schemas/scanner.py.
//
// The response is a discriminated union on `kind`; unknown codes are a
// STRUCTURED MISS (kind: 'unknown') returned with HTTP 200, so consumers
// switch on `kind` instead of catching errors.

export type ScanAction = 'clock_in' | 'report_production' | 'complete' | 'hold' | 'resume';

export interface ScanResolveRequest {
  code: string;
  work_center_id?: number;
}

export interface RoutingRevisionCheck {
  current_released_revision: string | null;
  released_routing_changed_after_wo_creation: boolean | null;
  checked_against: string | null;
  note: string;
}

export interface OperationScanSummary {
  id: number;
  sequence: number;
  operation_number: string | null;
  name: string;
  status: string;
  work_order_id: number;
  work_order_number: string;
  work_order_status: string;
  part_number: string | null;
  part_name: string | null;
  work_center_id: number | null;
  work_center_name: string | null;
  /** true/false when the request carried a work_center_id; null otherwise. */
  work_center_match: boolean | null;
  quantity_complete: number;
  target_quantity: number;
}

export interface OperationScanResult {
  kind: 'operation';
  code: string;
  operation: OperationScanSummary;
  /** Actions the calling user could perform right now (same gates as the shop-floor endpoints). */
  legal_actions: ScanAction[];
  /** action -> human-readable reasons; present only for actions NOT in legal_actions. */
  blockers: Partial<Record<ScanAction, string[]>>;
  warning: 'routing_revision_changed' | null;
  routing_revision_check: RoutingRevisionCheck | null;
}

export interface WorkOrderOperationBrief {
  id: number;
  sequence: number;
  operation_number: string | null;
  name: string;
  status: string;
}

export interface WorkOrderScanSummary {
  id: number;
  work_order_number: string;
  status: string;
  quantity_ordered: number;
  quantity_complete: number;
  part_number: string | null;
  part_name: string | null;
  /** First non-complete operation by sequence — "jump to current op" target. */
  current_operation_id: number | null;
}

export interface WorkOrderScanResult {
  kind: 'work_order';
  code: string;
  work_order: WorkOrderScanSummary;
  operations: WorkOrderOperationBrief[];
}

/** Badge lookup only — NO auth side effects (login stays on /auth/employee-login). */
export interface EmployeeScanResult {
  kind: 'employee';
  code: string;
  employee_id: string;
  first_name: string;
  last_initial: string;
}

export interface UnknownScanResult {
  kind: 'unknown';
  code: string;
  reason: string;
}

export type ScanResolveResult =
  | OperationScanResult
  | WorkOrderScanResult
  | EmployeeScanResult
  | UnknownScanResult;
