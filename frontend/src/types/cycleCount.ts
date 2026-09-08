export interface CycleCountSummary {
  id: number;
  count_number: string;
  status: 'scheduled' | 'in_progress' | 'completed' | 'cancelled';
  scheduled_date: string;
  started_at: string | null;
  completed_at: string | null;
  warehouse: string | null;
  location_code: string | null;
  part_id: number | null;
  assigned_to: number | null;
  assigned_to_name: string | null;
  total_items: number;
  items_counted: number;
  items_adjusted: number;
  total_variance_value: number;
  notes: string | null;
}

export interface CycleCountLine {
  id: number;
  inventory_item_id: number;
  part_id: number | null;
  part_number: string;
  part_name: string;
  unit_of_measure: string;
  location: string | null;
  lot_number: string | null;
  serial_number: string | null;
  system_quantity: number;
  current_quantity: number | null;
  counted_quantity: number | null;
  variance: number | null;
  variance_value: number | null;
  posting_delta: number;
  stock_changed: boolean;
  is_counted: boolean;
  requires_recount: boolean;
  counted_at: string | null;
  notes: string | null;
}

export interface CycleCountDetail extends CycleCountSummary {
  items: CycleCountLine[];
}
export interface CycleCountReview extends CycleCountDetail {
  review_token: string;
}
export interface CycleCountCreate {
  scheduled_date: string;
  warehouse?: string;
  location_code?: string;
  part_id?: number;
  assigned_to?: number;
  notes?: string;
}
