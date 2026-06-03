export type WorkOrderBlockerCategory =
  | 'material_missing'
  | 'machine_down'
  | 'tooling_missing'
  | 'quality_hold'
  | 'labor_unavailable'
  | 'engineering_question'
  | 'previous_operation'
  | 'other';

export type WorkOrderBlockerSeverity = 'low' | 'medium' | 'high' | 'critical';
export type WorkOrderBlockerStatus = 'open' | 'acknowledged' | 'resolved' | 'dismissed';

export interface WorkOrderBlocker {
  id: number;
  company_id: number;
  work_order_id: number;
  operation_id?: number | null;
  material_part_id?: number | null;
  category: WorkOrderBlockerCategory;
  severity: WorkOrderBlockerSeverity;
  status: WorkOrderBlockerStatus;
  title: string;
  note?: string | null;
  resolution_note?: string | null;
  reported_by?: number | null;
  assigned_to?: number | null;
  resolved_by?: number | null;
  reported_at: string;
  acknowledged_at?: string | null;
  resolved_at?: string | null;
  created_at: string;
  updated_at: string;
  work_order_number?: string | null;
  operation_name?: string | null;
  material_part_number?: string | null;
}

export interface WorkOrderBlockerInput {
  operation_id?: number;
  material_part_id?: number;
  category: WorkOrderBlockerCategory;
  severity?: WorkOrderBlockerSeverity;
  title?: string;
  note?: string;
  assigned_to?: number;
  put_operation_on_hold?: boolean;
}

export interface NaturalLanguageSearchResult {
  id: number;
  type: string;
  title: string;
  subtitle?: string;
  url: string;
  icon: string;
  explanation: string;
  matched_filters: string[];
}

export interface NaturalLanguageSearchResponse {
  query: string;
  confidence: number;
  interpreted_filters: Record<string, unknown>;
  used_fallback: boolean;
  results: NaturalLanguageSearchResult[];
}

export interface AdaptivePrompt {
  id: string;
  title: string;
  detail: string;
  href?: string;
  action_label?: string;
}
