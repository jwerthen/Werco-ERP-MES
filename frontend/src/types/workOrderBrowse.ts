import { WorkOrderSummary } from './index';

export type WorkOrderTypeFilter = 'production' | 'laser_cutting';
export interface WorkOrderBrowseParams {
  skip?: number;
  limit?: number;
  status?: string;
  search?: string;
  customer?: string;
  hide_cots?: boolean;
  scope?: 'overdue' | 'due_today';
  /** `laser_cutting` = nest packages; `production` = every other work order. */
  work_order_type?: WorkOrderTypeFilter;
  sort?: 'work_order_number' | 'part' | 'customer' | 'due_date' | 'priority' | 'status';
  direction?: 'asc' | 'desc';
  group?: 'none' | 'customer' | 'part' | 'status';
}
export interface WorkOrderBrowseResponse {
  items: WorkOrderSummary[];
  total: number;
  skip: number;
  limit: number;
  has_next: boolean;
  stats: { overdue: number; in_progress: number; due_today: number };
  customers: string[];
  customers_truncated: boolean;
  group_totals: Record<string, number>;
  /**
   * Rows per work-order type under every OTHER filter (the type filter itself is
   * excluded), so each quick-filter chip can show its own count. Both types are
   * always present, zero-filled.
   */
  type_counts: Record<string, number>;
}
