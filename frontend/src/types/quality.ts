/**
 * Quality records — the CAR form fields, the FAI record and the FAI log of record.
 *
 * Shapes mirror the backend response schemas in `backend/app/schemas/quality.py`
 * (`CARResponse`, `FAIResponse`, `FAICharacteristicResponse`, `FAILogRow`). Money
 * columns are `Numeric(12, 2)` serialized by Pydantic as decimal STRINGS, so the
 * CAR cost fields are typed `string | number` and never summed client-side — the
 * server returns `cost_total`.
 */

/** The "Reason for initiation" boxes on Werco's controlled CAR form (one per CAR). */
export type CARInitiationReason =
  | 'management_review'
  | 'nonconformance_trend'
  | 'customer_feedback'
  | 'internal_audit'
  | 'production_problems'
  | 'external_audit'
  | 'continuous_improvement'
  | 'other';

/** In the form's order, with the form's own wording (`car_pdf_service.REASON_BOXES`). */
export const CAR_INITIATION_REASONS: ReadonlyArray<{ value: CARInitiationReason; label: string }> = [
  { value: 'management_review', label: 'Management Review' },
  { value: 'nonconformance_trend', label: 'Non Conformance Trend' },
  { value: 'customer_feedback', label: 'Customer Feedback' },
  { value: 'internal_audit', label: 'Internal Audit' },
  { value: 'production_problems', label: 'Production Problems' },
  { value: 'external_audit', label: 'External Audit' },
  { value: 'continuous_improvement', label: 'Continuous Improvement' },
  { value: 'other', label: 'Other' },
];

export const carInitiationReasonLabel = (value?: string | null): string =>
  CAR_INITIATION_REASONS.find(reason => reason.value === value)?.label ?? '';

type Money = string | number | null;

/** GET /quality/car/{id} — the CAR record plus the controlled-form fields (migration 122). */
export interface CARRecord {
  id: number;
  car_number: string;
  car_type: string;
  status: string;
  priority: number;
  title: string;
  problem_description: string;
  root_cause_analysis?: string | null;
  root_cause?: string | null;
  containment_action?: string | null;
  corrective_action?: string | null;
  preventive_action?: string | null;
  verification_method?: string | null;
  verification_results?: string | null;
  effectiveness_check?: string | null;
  due_date?: string | null;
  verification_due?: string | null;
  containment_due?: string | null;
  corrective_due?: string | null;
  closed_date?: string | null;
  created_at: string;
  updated_at?: string | null;
  version?: number;

  initiated_by?: number | null;
  initiated_by_name?: string | null;
  assigned_to?: number | null;
  assigned_to_name?: string | null;
  verified_by?: number | null;
  verified_by_name?: string | null;
  closed_by?: number | null;
  closed_by_name?: string | null;

  initiation_reason?: CARInitiationReason | null;
  initiation_reason_other?: string | null;
  rma_number?: string | null;
  submitted_date?: string | null;
  part_id?: number | null;
  part_number?: string | null;
  part_name?: string | null;
  /** The part's revision TODAY, read from the part. The CAR records no revision of its own. */
  part_current_revision?: string | null;
  customer_id?: number | null;
  /** Snapshotted from the customer record when the customer is set; never sent. */
  customer_name?: string | null;
  vendor_id?: number | null;
  vendor_name?: string | null;
  vendor_po_number?: string | null;
  cost_labor?: Money;
  cost_material?: Money;
  cost_outside_services?: Money;
  cost_shipping?: Money;
  /** Computed by the server at read time; null when no cost is entered. */
  cost_total?: Money;
  management_action?: string | null;
  vendor_approval_name?: string | null;
  vendor_approval_title?: string | null;
  vendor_approval_date?: string | null;
  customer_approval_name?: string | null;
  customer_approval_date?: string | null;
  /** Server-stamped by POST /quality/car/{id}/quality-approval; never sent in a body. */
  quality_approved_by?: number | null;
  quality_approved_by_name?: string | null;
  quality_approved_at?: string | null;
}

export type FAIStatus = 'pending' | 'in_progress' | 'passed' | 'failed' | 'conditional';

/** The final dispositions (Admin / Manager / Quality); each freezes the evidence. */
export const FAI_FINAL_STATUSES: ReadonlyArray<FAIStatus> = ['passed', 'failed', 'conditional'];

export interface FAICharacteristic {
  id: number;
  fai_id?: number;
  char_number: number;
  characteristic: string;
  nominal?: string | null;
  tolerance_plus?: string | null;
  tolerance_minus?: string | null;
  specification?: string | null;
  actual_value?: string | null;
  measuring_device?: string | null;
  is_conforming?: boolean | null;
  is_critical: boolean;
  is_major: boolean;
  notes?: string | null;
}

/** POST /quality/fai/{id}/characteristics */
export interface FAICharacteristicCreate {
  char_number: number;
  characteristic: string;
  nominal?: string | null;
  tolerance_plus?: string | null;
  tolerance_minus?: string | null;
  specification?: string | null;
  is_critical?: boolean;
  is_major?: boolean;
}

/** PUT /quality/fai/{id}/characteristics/{char_id} — the measurement record only. */
export interface FAICharacteristicUpdate {
  actual_value?: string | null;
  measuring_device?: string | null;
  is_conforming?: boolean | null;
  notes?: string | null;
}

/** GET /quality/fai/{id} (and the list rows, which carry the same shape). */
export interface FAIRecord {
  id: number;
  fai_number: string;
  part_id: number;
  part?: { id: number; part_number: string; name: string } | null;
  part_revision?: string | null;
  work_order_id?: number | null;
  serial_number?: string | null;
  fai_type: string;
  reason?: string | null;
  status: string;
  total_characteristics: number;
  characteristics_passed: number;
  characteristics_failed: number;
  notes?: string | null;
  deviations?: string | null;
  inspection_date?: string | null;
  due_date?: string | null;
  completed_date?: string | null;
  customer_approval_required?: boolean;
  customer_approved?: boolean;
  customer_approval_date?: string | null;
  characteristics?: FAICharacteristic[];
  created_at: string;
  updated_at?: string | null;
  inspector_id?: number | null;
  inspector_name?: string | null;
  approved_by?: number | null;
  approved_by_name?: string | null;
  work_order_number?: string | null;
  customer_name?: string | null;
  customer_po?: string | null;
  part_current_revision?: string | null;
}

/** PUT /quality/fai/{id}. `version` is required by the schema but carries no lock. */
export interface FAIUpdatePayload {
  version: number;
  status?: FAIStatus;
  notes?: string | null;
  deviations?: string | null;
  inspection_date?: string | null;
  inspector_id?: number | null;
}

/** One line of GET /quality/fai/log — the FAI log of record. */
export interface FAILogRow {
  fai_id: number;
  fai_number: string;
  /** Completion date, else inspection date, else the Central day it was created. */
  log_date: string;
  part_id: number;
  part_number?: string | null;
  part_name?: string | null;
  part_revision?: string | null;
  part_current_revision?: string | null;
  customer?: string | null;
  customer_po?: string | null;
  work_order_id?: number | null;
  job_number?: string | null;
  serial_number?: string | null;
  status: FAIStatus;
  is_complete: boolean;
  fai_type?: string | null;
  reason?: string | null;
  total_characteristics: number;
  characteristics_passed: number;
  characteristics_failed: number;
  inspection_date?: string | null;
  completed_date?: string | null;
  customer_approval_required: boolean;
  customer_approved: boolean;
  customer_approval_date?: string | null;
  created_at: string;
}

/** Filters shared by GET /quality/fai/log and GET /exports/fai/export (inclusive Central dates). */
export interface FAILogFilters {
  start_date?: string;
  end_date?: string;
  status?: FAIStatus;
  customer?: string;
  part_id?: number;
  work_order_id?: number;
  search?: string;
}

export interface FAILogParams extends FAILogFilters {
  limit?: number;
  offset?: number;
}
