export interface User {
  id: number;
  version: number;  // For optimistic locking
  employee_id: string;
  email: string;
  first_name: string;
  last_name: string;
  role: UserRole;
  department?: string;
  is_active: boolean;
  is_superuser: boolean;
  created_at: string;
  updated_at: string;
}

export type UserRole = 'admin' | 'manager' | 'supervisor' | 'operator' | 'quality' | 'shipping' | 'viewer';

export interface WorkCenter {
  id: number;
  version: number;  // For optimistic locking
  code: string;
  name: string;
  work_center_type: WorkCenterType;
  description?: string;
  hourly_rate: number;
  capacity_hours_per_day: number;
  efficiency_factor: number;
  is_active: boolean;
  current_status: string;
  building?: string;
  area?: string;
  created_at: string;
  updated_at: string;
}

export type WorkCenterType = 'fabrication' | 'cnc_machining' | 'laser' | 'press_brake' | 'paint' | 'powder_coating' | 'assembly' | 'welding' | 'inspection' | 'shipping';

export interface Part {
  id: number;
  version: number;  // For optimistic locking
  part_number: string;
  revision: string;
  name: string;
  description?: string;
  part_type: PartType;
  unit_of_measure: string;
  standard_cost: number;
  is_critical: boolean;
  requires_inspection: boolean;
  is_active: boolean;
  status: string;
  customer_name?: string;
  customer_part_number?: string;
  drawing_number?: string;
  created_at: string;
  updated_at: string;
}

export type PartType = 'manufactured' | 'purchased' | 'assembly' | 'raw_material';

export interface WorkOrder {
  id: number;
  version: number;  // For optimistic locking
  work_order_number: string;
  part_id: number;
  quantity_ordered: number;
  quantity_complete: number;
  quantity_scrapped: number;
  status: WorkOrderStatus;
  priority: number;
  scheduled_start?: string;
  scheduled_end?: string;
  actual_start?: string;
  actual_end?: string;
  due_date?: string;
  must_ship_by?: string;
  customer_name?: string;
  customer_po?: string;
  lot_number?: string;
  notes?: string;
  special_instructions?: string;
  estimated_hours: number;
  actual_hours: number;
  created_at: string;
  updated_at: string;
  operations: WorkOrderOperation[];
}

export type WorkOrderStatus = 'draft' | 'released' | 'in_progress' | 'on_hold' | 'complete' | 'closed' | 'cancelled';

export interface WorkOrderOperation {
  id: number;
  version: number;  // For optimistic locking
  work_order_id: number;
  work_center_id: number;
  sequence: number;
  operation_number?: string;
  name: string;
  description?: string;
  setup_instructions?: string;
  run_instructions?: string;
  setup_time_hours: number;
  run_time_hours: number;
  run_time_per_piece: number;
  actual_setup_hours: number;
  actual_run_hours: number;
  status: OperationStatus;
  quantity_complete: number;
  quantity_scrapped: number;
  requires_inspection: boolean;
  inspection_complete: boolean;
  scheduled_start?: string;
  scheduled_end?: string;
  actual_start?: string;
  actual_end?: string;
  created_at: string;
  updated_at: string;
}

export type OperationStatus = 'pending' | 'ready' | 'in_progress' | 'complete' | 'on_hold';

export interface WorkOrderSummary {
  id: number;
  work_order_number: string;
  part_id: number;
  part_number?: string;
  part_name?: string;
  part_type?: string;
  status: WorkOrderStatus;
  priority: number;
  quantity_ordered: number;
  quantity_complete: number;
  due_date?: string;
  customer_name?: string;
  current_operation?: string;
}

export interface TimeEntry {
  id: number;
  user_id: number;
  work_order_id?: number;
  operation_id?: number;
  work_center_id?: number;
  entry_type: TimeEntryType;
  clock_in: string;
  clock_out?: string;
  duration_hours?: number;
  quantity_produced: number;
  quantity_scrapped: number;
  notes?: string;
  scrap_reason?: string;
  downtime_reason?: string;
  created_at: string;
  updated_at: string;
}

export type TimeEntryType = 'setup' | 'run' | 'rework' | 'inspection' | 'downtime' | 'break';

export interface DashboardData {
  summary: {
    active_work_orders: number;
    due_today: number;
    overdue: number;
  };
  work_centers: WorkCenterStatus[];
  recent_completions: {
    work_order_number: string;
    completed_at: string;
    quantity_complete: number;
  }[];
}

export interface WorkCenterStatus {
  id: number;
  code: string;
  name: string;
  type: WorkCenterType;
  status: string;
  active_operations: number;
  queued_operations: number;
}

export interface QueueItem {
  operation_id: number;
  work_order_id: number;
  work_order_number: string;
  part_number?: string;
  part_name?: string;
  operation_number?: string;
  operation_name: string;
  status: OperationStatus;
  quantity_ordered: number;
  quantity_complete: number;
  priority: number;
  due_date?: string;
  setup_time_hours: number;
  run_time_hours: number;
}

export interface ActiveJob {
  time_entry_id: number;
  clock_in: string;
  entry_type: TimeEntryType;
  work_order_number?: string;
  part_number?: string;
  part_name?: string;
  operation_name?: string;
  operation_number?: string;
}
