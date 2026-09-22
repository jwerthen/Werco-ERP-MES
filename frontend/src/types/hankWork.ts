import type { HankTaskCommand, HankTaskReference } from './hankTasks';
import type { WorkOrderBlockerCategory, WorkOrderBlockerSeverity } from './aiForward';

export type HankEvidenceKind = 'readiness' | 'knowledge' | 'purchasing' | 'shipping' | 'trace';
export interface HankEvidence {
  company_id: number;
  checked_at: string;
  title: string;
  summary: string;
  checks: Array<{
    key: string;
    title: string;
    status: 'satisfied' | 'attention' | 'unknown' | 'info';
    detail: string;
    references: HankTaskReference[];
  }>;
  coverage_notes: string[];
  draft_text: string | null;
}
export interface HankReceiveInput {
  purchase_order_id: number;
  lines: Array<{
    po_line_id: number;
    quantity_received: number;
    requires_inspection: boolean;
    lot_number?: string;
    heat_number?: string;
    cert_number?: string;
    certificate_document_id?: number;
    location_id?: number;
    packing_slip_number?: string;
    notes?: string;
    serial_numbers?: string;
    coc_attached?: boolean;
    carrier?: string;
    tracking_number?: string;
    over_receive_approved?: false;
  }>;
}
export interface HankProductionInput {
  operation_id: number;
  quantity_complete_delta: number;
  quantity_scrapped_delta: number;
  scrap_reason?: string;
  scrap_reason_code_id?: number;
  notes?: string;
  open_ncr: boolean;
  ncr_description?: string;
  hold?: { category: WorkOrderBlockerCategory; severity: WorkOrderBlockerSeverity; note: string };
}
export interface HankShipmentInput {
  work_order_id: number;
  quantity_shipped: number;
  ship_to_name?: string;
  ship_to_address?: string;
  ship_to_city?: string;
  ship_to_state?: string;
  ship_to_zip?: string;
  carrier?: string;
  service_type?: string;
  weight_lbs?: number;
  num_packages: number;
  packing_notes?: string;
}
export interface HankHandoffContent {
  summary: string;
  completed_work: string;
  remaining_work: string;
  problems: string;
  quantity_remaining?: number | null;
  document_ids: number[];
}
export interface HankHandoffCreate extends HankHandoffContent {
  expected_company_id: number;
  request_key: string;
  work_order_id: number;
  recipient_id: number;
}
export interface HankHandoff extends HankHandoffContent {
  id: number;
  company_id: number;
  version: number;
  status: 'open' | 'acknowledged' | 'completed' | 'cancelled';
  work_order_id: number;
  work_order_number: string;
  sender: { id: number; name: string };
  recipient: { id: number; name: string };
  attachments: Array<{ id: string; filename: string; url: string; mime_type: string }>;
  document_references: HankTaskReference[];
  created_at: string;
  updated_at: string;
  acknowledged_at?: string | null;
  completed_at?: string | null;
  can_acknowledge: boolean;
  can_complete: boolean;
  can_cancel: boolean;
}
export interface HankHandoffList {
  handoffs: HankHandoff[];
  has_more: boolean;
  next_before_id: number | null;
}
export type HankRoutineStepKind =
  | 'readiness'
  | 'knowledge'
  | 'document_intake'
  | 'receive_delivery'
  | 'report_production'
  | 'shipping_packet'
  | 'draft_shipment'
  | 'purchasing_impact'
  | 'handoff'
  | 'checklist';
export interface HankRoutineStep {
  kind: HankRoutineStepKind;
  title: string;
  instruction: string;
}
export interface HankRoutineValues {
  title: string;
  description: string;
  steps: HankRoutineStep[];
}
export interface HankRoutine extends HankRoutineValues {
  id: number;
  company_id: number;
  version: number;
  status: 'draft' | 'approved' | 'archived';
  created_by: number;
  approved_by: number | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
  can_manage: boolean;
  can_approve: boolean;
}
export interface HankRoutineRun {
  id: number;
  company_id: number;
  routine_id: number;
  routine_version: number;
  title: string;
  status: 'active' | 'completed' | 'cancelled';
  version: number;
  current_step: number;
  steps: HankRoutineStep[];
  work_order_id: number | null;
  purchase_order_id: number | null;
  results: Array<{ step_index: number; note: string; completed_at: string; evidence: HankTaskReference[] }>;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  can_edit: boolean;
}
export interface HankRoutineAdvance extends HankTaskCommand {
  note: string;
  task_id?: number;
  intake_file_id?: number;
  handoff_id?: number;
}
export type HankWorkState = 'working' | 'waiting_on_you' | 'waiting_on_other' | 'finished';
export interface HankWorkQueue {
  checked_at: string;
  items: Array<{
    key: string;
    kind: string;
    id: number;
    title: string;
    state: HankWorkState;
    status: string;
    url: string;
    updated_at: string;
  }>;
  truncated: boolean;
}
