export interface MaterialReadiness {
  status: 'ready' | 'unknown' | 'not_defined';
  ready_date: string | null;
  basis: string;
  warnings: string[];
  lines: {
    part_id: number | null;
    part_number: string;
    unit_of_measure: string | null;
    required_quantity: number;
    covered_quantity: number;
    shortage_quantity: number;
    reason: string | null;
    sources: {
      kind: 'stock' | 'purchase_order';
      id: number;
      line_id?: number;
      label: string;
      quantity: number;
      available_date: string;
      expires_on?: string | null;
    }[];
  }[];
}

export interface DeliveryPrediction {
  predicted_completion: string | null;
  basis: string;
  warnings: string[];
  materials: MaterialReadiness | null;
  operations: {
    operation_id: number;
    operation_name: string;
    work_center_name: string;
    predicted_start: string | null;
    predicted_end: string | null;
    estimated_hours: number;
  }[];
}

export type TimelineCategory = 'job' | 'production' | 'labor' | 'material' | 'quality' | 'blocker' | 'audit';
export interface JobTimelineEntry {
  id: string;
  occurred_at: string;
  category: TimelineCategory;
  evidence: 'business_record' | 'audit' | 'telemetry';
  title: string;
  detail: string | null;
  actor_id: number | null;
  actor_name: string | null;
  source_label: string;
  source_url: string;
}
export interface JobTimelineResponse {
  items: JobTimelineEntry[];
  next_cursor: string | null;
  coverage: string;
}
