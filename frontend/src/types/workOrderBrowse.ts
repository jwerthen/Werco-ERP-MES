import { WorkOrderSummary } from './index';
export interface WorkOrderBrowseParams {
  skip?: number;
  limit?: number;
  status?: string;
  search?: string;
  customer?: string;
  hide_cots?: boolean;
  scope?: 'overdue' | 'due_today';
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
}
