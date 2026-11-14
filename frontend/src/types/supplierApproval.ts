/**
 * Approved Supplier List (AS9100D 8.4, Werco form QL 7.4) on the vendor record.
 *
 * Wire shapes for the vendor ASL fields and the four ASL endpoints under
 * /purchasing (see backend/app/schemas/purchasing.py). Status / scope / basis values
 * travel as SLUGS; the display labels live in utils/supplierApproval.ts, held to the
 * backend vocabulary by a parity test.
 */

export type AslStatus = 'approved' | 'conditional' | 'disqualified' | 'approved_customer_directed';

/** The ASL fields VendorResponse carries. READ-ONLY on the vendor: written only by
 *  PUT /purchasing/vendors/{id}/approval and the ASL import. NULL status = not on the ASL. */
export interface VendorAslFields {
  approval_status?: AslStatus | null;
  approved_scopes?: string[] | null;
  approval_bases?: string[] | null;
  approval_date?: string | null;
  reevaluation_due?: string | null;
  last_performance_evaluation?: string | null;
  approval_notes?: string | null;
}

/** A vendor as the ASL views read it (GET /purchasing/vendors rows). */
export interface AslVendor extends VendorAslFields {
  id: number;
  code: string;
  name: string;
  is_approved: boolean;
  is_active?: boolean;
  city?: string | null;
  state?: string | null;
  updated_at?: string | null;
}

/** GET /purchasing/vendors/reevaluation-due row. days_overdue < 0 = due inside the window. */
export interface VendorReevaluationDue extends AslVendor {
  days_overdue: number;
}

/**
 * PUT /purchasing/vendors/{id}/approval body. `approval_status` is always sent (null takes
 * the vendor off the ASL); every other optional field is applied ONLY when present — an
 * omitted key leaves the column alone, an explicit null clears it. `reason` is required;
 * `expected_updated_at` is the vendor's `updated_at` as read (409 if it moved since).
 */
export interface VendorApprovalUpdate {
  approval_status: AslStatus | null;
  approved_scopes?: string[] | null;
  approval_bases?: string[] | null;
  approval_date?: string | null;
  reevaluation_due?: string | null;
  last_performance_evaluation?: string | null;
  approval_notes?: string | null;
  reason: string;
  expected_updated_at: string | null;
}

export type SupplierApprovalWarningCode =
  | 'vendor_not_on_asl'
  | 'vendor_conditional'
  | 'vendor_disqualified'
  | 'reevaluation_overdue'
  | 'scope_not_approved'
  | 'scope_unverified';

/** One advisory ASL warning for a purchase (never a refusal). */
export interface SupplierApprovalWarning {
  code: SupplierApprovalWarningCode | string;
  message: string;
  part_id?: number | null;
  part_number?: string | null;
}

/** POST /purchasing/supplier-approval-check response (a pure read). */
export interface SupplierApprovalCheck {
  vendor_id: number;
  vendor_name: string;
  approval_status: AslStatus | null;
  is_approved: boolean;
  reevaluation_due: string | null;
  /** True when a purchase order to this vendor would be REFUSED (Disqualified). */
  refused: boolean;
  /** The exact 409 text every PO-create door answers with. */
  refusal: string | null;
  warnings: SupplierApprovalWarning[];
}

export interface AslImportDocument {
  form_number?: string | null;
  revision?: string | null;
  rev_date?: string | null;
}

export interface AslImportSummary {
  total_rows: number;
  matched_rows: number;
  update_count: number;
  unchanged_count: number;
  error_count: number;
  warning_count: number;
  not_on_asl_count: number;
}

export type AslImportOutcome = 'update' | 'unchanged' | 'error';

export interface AslImportRow {
  row: number;
  supplier_name: string;
  vendor_id?: number | null;
  vendor_code?: string | null;
  vendor_name?: string | null;
  match_by?: string | null;
  outcome: AslImportOutcome | string;
  /** field -> {old, new}; slugs / slug lists / ISO dates / booleans. */
  changes: Record<string, { old: unknown; new: unknown }>;
  warnings: string[];
  error?: string | null;
}

/** Both POST /purchasing/vendors/asl-import/preview and /commit answer with this. */
export interface AslImportResponse {
  filename?: string | null;
  source_hash: string;
  /** What the commit would write, read against the vendors now; the commit echoes it (409 on drift). */
  plan_hash: string;
  document: AslImportDocument;
  committed: boolean;
  summary: AslImportSummary;
  rows: AslImportRow[];
}
