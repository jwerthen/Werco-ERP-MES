export interface ImportBatchRow {
  row_key: string;
  source_row: number;
  status: 'ready' | 'invalid' | 'failed' | 'created';
  data: Record<string, string>;
  error?: string | null;
  result?: { record_id?: number; entity?: string; [key: string]: unknown } | null;
}
export interface ImportBatch {
  id: number;
  entity: string;
  filename: string;
  version: number;
  created_at: string;
  updated_at: string;
  total_rows: number;
  counts: Record<string, number>;
  created_records: number;
  rows: ImportBatchRow[];
  row_offset: number;
  has_more_rows: boolean;
  requires_credentials: boolean;
}
export interface ImportBatchHistory {
  batches: ImportBatch[];
  has_more: boolean;
}
