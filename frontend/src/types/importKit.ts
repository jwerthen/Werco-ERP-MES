/**
 * A0.2 Excel migration kit — frontend contracts for the import endpoints.
 *
 * Mirrors backend/app/schemas/import_kit.py and the per-entity
 * `*CsvImportResponse` models. Every import endpoint accepts CSV or XLSX and a
 * `dry_run` query param; dry-run responses are identical in shape so the
 * Import Center can render a preview before anything is written.
 */

export interface ImportTemplateSummary {
  entity: string;
  title: string;
  description: string;
  columns: string[];
  download_path: string;
}

export interface ImportTemplateIndexResponse {
  templates: ImportTemplateSummary[];
}

/** Row-level error. Identifier field varies by entity; all are optional. */
export interface ImportRowError {
  row: number;
  reason: string;
  identifier?: string;
  employee_id?: string;
  email?: string;
  part_number?: string;
  code?: string;
  name?: string;
  wo_number?: string;
  po_number?: string;
}

/**
 * Users/parts/materials/customers/vendors/work-centers import response.
 * Users reports `created_count`; the others report `imported_count`.
 */
export interface EntityImportResponse {
  total_rows: number;
  created_count?: number;
  imported_count?: number;
  skipped_count: number;
  created_ids: number[];
  errors: ImportRowError[];
  dry_run: boolean;
}

/** Users import always reports `created_count` (never `imported_count`). */
export interface UserImportResponse extends EntityImportResponse {
  created_count: number;
}

export interface WorkOrderImportRowResult {
  row: number;
  /** Null in dry-run when the WO number would be generated at commit. */
  wo_number: string | null;
  part_number: string;
  quantity: number;
  due_date: string | null;
  customer_name: string | null;
  status: string;
  operation_count: number;
  completed_operation_count: number;
  next_operation_sequence: number | null;
}

export interface WorkOrderImportResponse {
  dry_run: boolean;
  total_rows: number;
  created_count: number;
  skipped_count: number;
  created_ids: number[];
  results: WorkOrderImportRowResult[];
  errors: ImportRowError[];
}

/** One entry per purchase order (rows sharing a po_number become its lines). */
export interface PurchaseOrderImportRowResult {
  rows: number[];
  /** Null in dry-run when the PO number would be generated at commit. */
  po_number: string | null;
  vendor_code: string;
  line_count: number;
  total: number;
  status: string;
}

export interface PurchaseOrderImportResponse {
  dry_run: boolean;
  total_rows: number;
  /** Purchase orders created (not lines). */
  created_count: number;
  created_line_count: number;
  skipped_count: number;
  created_ids: number[];
  results: PurchaseOrderImportRowResult[];
  errors: ImportRowError[];
}

export type AnyImportResponse =
  | EntityImportResponse
  | WorkOrderImportResponse
  | PurchaseOrderImportResponse;
