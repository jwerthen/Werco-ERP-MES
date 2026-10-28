import type { WorkOrderSummary, OperationStatus } from './index';

export interface CustomerPOStats {
  total_work_orders: number;
  active_work_orders: number;
  completed_work_orders: number;
  cancelled_work_orders: number;
  in_progress_work_orders: number;
  on_hold_work_orders: number;
  overdue_work_orders: number;
  due_today_work_orders: number;
  /** Completed/closed jobs divided by non-cancelled jobs; not production quantity. */
  completion_percent: number;
  next_due_date: string | null;
}

export interface CustomerPOSummary {
  customer_name: string | null;
  customer_po: string;
  stats: CustomerPOStats;
}

export interface CustomerPOWorkOrder extends WorkOrderSummary {
  customer_po: string;
  po_line_item: string | null;
  po_date: string | null;
  must_ship_by: string | null;
  quantity_scrapped: number;
  is_overdue: boolean;
  current_operations: Array<{
    id: number;
    operation_number: string | null;
    name: string;
    status: OperationStatus;
    work_center_name: string | null;
  }>;
}

interface CustomerPOPage {
  total: number;
  skip: number;
  limit: number;
  has_next: boolean;
  as_of_date: string;
}

export interface CustomerPOListResponse extends CustomerPOPage {
  items: CustomerPOSummary[];
}

export interface CustomerPODashboardResponse extends CustomerPOPage, CustomerPOSummary {
  items: CustomerPOWorkOrder[];
}

export interface CustomerPOListParams {
  skip?: number;
  limit?: number;
  search?: string;
}

export interface CustomerPODashboardParams {
  customer_name: string;
  customer_po: string;
  skip?: number;
  limit?: number;
}

export interface CustomerPOSuggestion {
  customer_name: string | null;
  customer_po: string;
}

export interface CustomerPOSuggestionParams {
  search: string;
  customer_name?: string;
  limit?: number;
}

export interface CustomerPOSuggestionsResponse {
  items: CustomerPOSuggestion[];
  has_more: boolean;
}
