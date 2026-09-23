export type HankIntakeStatus =
  | 'queued'
  | 'analyzing'
  | 'awaiting_review'
  | 'planned'
  | 'completed'
  | 'failed'
  | 'cancelled';
export type HankIntakeFieldName =
  | 'document_number'
  | 'vendor_name'
  | 'customer_name'
  | 'po_number'
  | 'receipt_number'
  | 'packing_slip_number'
  | 'part_number'
  | 'work_order_number'
  | 'revision'
  | 'heat_number'
  | 'lot_number'
  | 'quantity'
  | 'unit_price'
  | 'total'
  | 'currency'
  | 'date'
  | 'due_date';
export type HankSourceFormat = 'pdf' | 'docx' | 'xlsx' | 'xls';
export interface HankSourcePreview {
  format: HankSourceFormat;
  units: string[];
  labels: string[];
  warnings: string[];
}
export interface HankIntakeEvidence {
  page: number;
  locator?: string | null;
  excerpt: string;
}
export interface HankIntakeMatch {
  kind: 'part' | 'work_order' | 'vendor' | 'purchase_order' | 'receipt';
  id: number;
  label: string;
  href: string;
  reason: string;
}
export interface HankIntakeAnalysis {
  source_format?: HankSourceFormat;
  source_labels?: string[];
  classification: 'purchase_order' | 'vendor_quote' | 'packing_slip' | 'material_certificate' | 'drawing' | 'other';
  confidence: 'high' | 'low' | 'unknown';
  summary: string;
  evidence: HankIntakeEvidence[];
  fields: Array<{
    name: HankIntakeFieldName;
    value: string | null;
    confidence: 'high' | 'low' | 'unknown';
    evidence: HankIntakeEvidence[];
  }>;
  lines: Array<{
    description: string;
    part_number: string | null;
    quantity: string | null;
    unit_price: string | null;
    unit_of_measure?: string | null;
    lot_number: string | null;
    heat_number: string | null;
    confidence: 'high' | 'low' | 'unknown';
    evidence: HankIntakeEvidence[];
  }>;
  warnings: string[];
  matches: HankIntakeMatch[];
  has_duplicates?: boolean;
  duplicate_file_ids: number[];
  duplicate_document_ids: number[];
}
export interface HankIntakePlan {
  filing_mode: 'draft' | 'release_receipt_certificate';
  title: string;
  document_type: string;
  revision: string;
  description?: string | null;
  part_id?: number | null;
  work_order_id?: number | null;
  vendor_id?: number | null;
  purchase_order_id?: number | null;
  receipt_id?: number | null;
  reviewed_fields: Array<{ name: HankIntakeFieldName; value: string | null }>;
  acknowledge_duplicate: boolean;
}
export interface HankIntakeFile {
  source_format?: HankSourceFormat;
  source_labels?: string[];
  id: number;
  batch_id: number;
  company_id: number;
  filename: string;
  file_size: number;
  content_sha256: string;
  page_count: number | null;
  status: HankIntakeStatus;
  version: number;
  source_url: string;
  analysis: HankIntakeAnalysis | null;
  plan: { input: HankIntakePlan; changes: string[]; warnings: string[]; references: HankIntakeMatch[] } | null;
  result: {
    document_id: number;
    document_number: string;
    href: string;
    summary: string;
    references: HankIntakeMatch[];
    warnings: string[];
  } | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}
export interface HankIntakeBatch {
  id: number;
  company_id: number;
  request_key: string;
  created_at: string;
  files: HankIntakeFile[];
}
export interface HankIntakeBatchList {
  batches: HankIntakeBatch[];
  has_more: boolean;
  next_before_id: number | null;
}

/** Source evidence and deterministic PO matches; this read does not receive inventory. */
export interface HankIntakeReceivingDraft {
  file_id: number;
  file_version: number;
  company_id: number;
  filename: string;
  purchase_order_id: number | null;
  purchase_orders: Array<{ id: number; po_number: string; vendor_name: string; reason: string }>;
  packing_slip_number: string | null;
  lines: Array<{
    source_line_index: number;
    description: string;
    part_number: string | null;
    quantity: string | null;
    unit_of_measure: string | null;
    lot_number: string | null;
    heat_number: string | null;
    confidence: 'high' | 'low' | 'unknown';
    evidence: HankIntakeEvidence[];
    po_line_id: number | null;
    candidates: Array<{
      po_line_id: number;
      line_number: number;
      part_id: number;
      part_number: string;
      description: string;
      quantity_remaining: number;
      unit_of_measure: string;
    }>;
    quantity_received: number | null;
    warnings: string[];
  }>;
  warnings: string[];
  has_duplicates: boolean;
  requires_duplicate_acknowledgement: boolean;
}

/** Verified source values and scoped matches; importing is a separate reviewed task. */
export interface HankIntakePurchaseOrderDraft {
  file_id: number;
  file_version: number;
  company_id: number;
  filename: string;
  po_number: string | null;
  order_date: string | null;
  required_date: string | null;
  vendor_id: number | null;
  vendors: Array<{ id: number; code: string; name: string; reason: string }>;
  lines: Array<
    HankIntakeAnalysis['lines'][number] & {
      source_line_index: number;
      part_id: number | null;
      candidates: Array<{ id: number; part_number: string; name: string; unit_of_measure: string }>;
      quantity_ordered: number | null;
      unit_price_amount: number | null;
      warnings: string[];
    }
  >;
  warnings: string[];
  has_duplicates: boolean;
  blocked_reason: string | null;
  can_ready_for_receiving: boolean;
  existing_purchase_orders: Array<{ id: number; po_number: string; href: string }>;
}
