/**
 * API Request and Response Types
 * These types define the shape of data sent to and received from the API
 */

import { 
  User, UserRole, Part, PartType, WorkOrder, WorkOrderStatus, 
  WorkCenter, WorkCenterType, WorkOrderOperation, OperationStatus 
} from './index';

// ============ Generic Types ============

export interface ApiError {
  detail: string;
  status?: number;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// ============ Auth Types ============

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface RefreshTokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

export interface RegisterRequest {
  email: string;
  password: string;
  first_name: string;
  last_name: string;
  employee_id?: string;
  department?: string;
  role?: UserRole;
}

// ============ User Types ============

export interface UserCreate {
  email: string;
  password: string;
  first_name: string;
  last_name: string;
  employee_id?: string;
  department?: string;
  role?: UserRole;
  is_active?: boolean;
}

export interface UserUpdate {
  email?: string;
  first_name?: string;
  last_name?: string;
  employee_id?: string;
  department?: string;
  role?: UserRole;
  is_active?: boolean;
}

// ============ Part Types ============

export interface PartCreate {
  part_number: string;
  name: string;
  description?: string;
  revision?: string;
  part_type: PartType | string;
  unit_of_measure?: string;
  standard_cost?: number;
  is_critical?: boolean;
  requires_inspection?: boolean;
  customer_name?: string;
  customer_part_number?: string;
  drawing_number?: string;
}

export interface PartUpdate {
  name?: string;
  description?: string;
  revision?: string;
  part_type?: PartType | string;
  unit_of_measure?: string;
  standard_cost?: number;
  is_critical?: boolean;
  requires_inspection?: boolean;
  is_active?: boolean;
  customer_name?: string;
  customer_part_number?: string;
  drawing_number?: string;
  version?: number; // For optimistic locking
}

export interface PartListParams {
  search?: string;
  part_type?: PartType;
  active_only?: boolean;
  limit?: number;
  offset?: number;
}

// ============ Work Order Types ============

export interface WorkOrderCreate {
  part_id: number;
  quantity_ordered: number;
  priority?: number;
  scheduled_start?: string;
  scheduled_end?: string;
  due_date?: string;
  must_ship_by?: string;
  customer_name?: string;
  customer_po?: string;
  lot_number?: string;
  notes?: string;
  special_instructions?: string;
}

export interface WorkOrderUpdate {
  quantity_ordered?: number;
  priority?: number;
  scheduled_start?: string;
  scheduled_end?: string;
  due_date?: string;
  must_ship_by?: string;
  customer_name?: string;
  customer_po?: string;
  lot_number?: string;
  notes?: string;
  special_instructions?: string;
  version?: number; // For optimistic locking
}

export interface WorkOrderListParams {
  status?: WorkOrderStatus | WorkOrderStatus[];
  priority?: number;
  part_id?: number;
  search?: string;
  limit?: number;
  offset?: number;
}

// ============ Work Center Types ============

export interface WorkCenterCreate {
  code: string;
  name: string;
  work_center_type: WorkCenterType;
  description?: string;
  hourly_rate?: number;
  capacity_hours_per_day?: number;
  efficiency_factor?: number;
  building?: string;
  area?: string;
}

export interface WorkCenterUpdate {
  code?: string;
  name?: string;
  work_center_type?: WorkCenterType;
  description?: string;
  hourly_rate?: number;
  capacity_hours_per_day?: number;
  efficiency_factor?: number;
  is_active?: boolean;
  building?: string;
  area?: string;
  version?: number; // For optimistic locking
}

// ============ BOM Types ============

export interface BOMLine {
  id?: number;
  component_id: number;
  quantity: number;
  unit_of_measure?: string;
  reference_designator?: string;
  notes?: string;
  find_number?: number;
  is_critical?: boolean;
  line_type?: 'component' | 'reference' | 'phantom' | 'option';
}

export interface BOMCreate {
  parent_part_id: number;
  revision?: string;
  effective_date?: string;
  lines: BOMLine[];
}

export interface BOMUpdate {
  revision?: string;
  effective_date?: string;
  expiration_date?: string;
  is_active?: boolean;
  lines?: BOMLine[];
  version?: number;
}

export interface BOMResponse {
  id: number;
  parent_part_id: number;
  parent_part_number: string;
  parent_part_name: string;
  revision: string;
  is_active: boolean;
  effective_date?: string;
  expiration_date?: string;
  lines: BOMLineResponse[];
  created_at: string;
  updated_at: string;
  version: number;
}

export interface BOMLineResponse extends BOMLine {
  id: number;
  component_part_number: string;
  component_part_name: string;
  component_part_type: PartType;
}

// ============ Inventory Types ============

export interface InventoryItem {
  id: number;
  part_id: number;
  part_number: string;
  part_name: string;
  location_id?: number;
  location_name?: string;
  quantity_on_hand: number;
  quantity_allocated: number;
  quantity_available: number;
  lot_number?: string;
  serial_number?: string;
  unit_cost?: number;
  last_count_date?: string;
}

export interface InventoryTransaction {
  part_id: number;
  transaction_type: 'receipt' | 'issue' | 'adjustment' | 'transfer' | 'scrap';
  quantity: number;
  location_id?: number;
  to_location_id?: number;
  lot_number?: string;
  serial_number?: string;
  reference_type?: string;
  reference_id?: number;
  notes?: string;
}

// ============ Quality Types ============

export interface InspectionRecord {
  id: number;
  work_order_id?: number;
  part_id: number;
  operation_id?: number;
  inspection_type: string;
  status: 'pending' | 'in_progress' | 'passed' | 'failed' | 'conditional';
  inspector_id?: number;
  inspection_date?: string;
  quantity_inspected: number;
  quantity_accepted: number;
  quantity_rejected: number;
  notes?: string;
}

export interface NCR {
  id: number;
  ncr_number: string;
  work_order_id?: number;
  part_id: number;
  status: 'open' | 'under_review' | 'disposition' | 'closed';
  severity: 'minor' | 'major' | 'critical';
  description: string;
  root_cause?: string;
  corrective_action?: string;
  quantity_affected: number;
  created_at: string;
  closed_at?: string;
}

// ============ Purchasing Types ============

export interface PurchaseOrder {
  id: number;
  po_number: string;
  vendor_id: number;
  vendor_name: string;
  status: 'draft' | 'pending' | 'approved' | 'ordered' | 'partial' | 'received' | 'cancelled';
  order_date?: string;
  expected_date?: string;
  total_amount: number;
  lines: PurchaseOrderLine[];
  notes?: string;
  created_at: string;
  updated_at: string;
}

export interface PurchaseOrderLine {
  id?: number;
  part_id: number;
  part_number?: string;
  part_name?: string;
  quantity_ordered: number;
  quantity_received: number;
  unit_price: number;
  line_total: number;
  due_date?: string;
}

export interface PurchaseOrderCreate {
  vendor_id: number;
  expected_date?: string;
  notes?: string;
  lines: Omit<PurchaseOrderLine, 'id' | 'part_number' | 'part_name' | 'quantity_received' | 'line_total'>[];
}

// ============ Quote Types ============

export interface Quote {
  id: number;
  quote_number: string;
  customer_id?: number;
  customer_name: string;
  status: 'draft' | 'sent' | 'accepted' | 'rejected' | 'expired';
  valid_until?: string;
  total_amount: number;
  notes?: string;
  created_at: string;
}

export interface QuoteCreate {
  customer_id?: number;
  customer_name: string;
  valid_until?: string;
  notes?: string;
  lines: QuoteLineCreate[];
}

export interface QuoteLineCreate {
  part_id?: number;
  description: string;
  quantity: number;
  unit_price: number;
}

// ============ Routing Types ============

export interface Routing {
  id: number;
  part_id: number;
  part_number: string;
  revision: string;
  is_active: boolean;
  operations: RoutingOperation[];
}

export interface RoutingOperation {
  id?: number;
  sequence: number;
  operation_number?: string;
  name: string;
  description?: string;
  work_center_id: number;
  work_center?: WorkCenter;
  setup_time_hours: number;
  run_time_hours: number;
  run_time_per_piece: number;
  setup_instructions?: string;
  run_instructions?: string;
  requires_inspection?: boolean;
  inspection_type?: string;
}

export interface RoutingCreate {
  part_id: number;
  revision?: string;
  operations: Omit<RoutingOperation, 'id' | 'work_center'>[];
}

// ============ Customer Types ============

export interface Customer {
  id: number;
  name: string;
  code?: string;
  contact_name?: string;
  email?: string;
  phone?: string;
  address?: string;
  city?: string;
  state?: string;
  zip?: string;
  country?: string;
  is_active: boolean;
  notes?: string;
  created_at: string;
}

export interface CustomerCreate {
  name: string;
  code?: string;
  contact_name?: string;
  email?: string;
  phone?: string;
  address?: string;
  city?: string;
  state?: string;
  zip?: string;
  country?: string;
  notes?: string;
}

// ============ Vendor Types ============

export interface Vendor {
  id: number;
  name: string;
  code?: string;
  contact_name?: string;
  email?: string;
  phone?: string;
  address?: string;
  is_active: boolean;
  notes?: string;
}

export interface VendorCreate {
  name: string;
  code?: string;
  contact_name?: string;
  email?: string;
  phone?: string;
  address?: string;
  notes?: string;
}

// ============ Report Types ============

export interface ReportParams {
  start_date?: string;
  end_date?: string;
  work_center_id?: number;
  part_id?: number;
  status?: string;
  format?: 'json' | 'csv' | 'pdf';
}

export interface DashboardMetrics {
  work_orders: {
    total: number;
    in_progress: number;
    completed_today: number;
    overdue: number;
  };
  quality: {
    first_pass_yield: number;
    ncrs_open: number;
    inspections_pending: number;
  };
  inventory: {
    low_stock_items: number;
    pending_receipts: number;
  };
  production: {
    efficiency: number;
    utilization: number;
  };
}

// ============ Search Types ============

export interface SearchResult {
  type: 'work_order' | 'part' | 'customer' | 'purchase_order' | 'quote';
  id: number;
  title: string;
  subtitle?: string;
  url: string;
}

export interface GlobalSearchParams {
  query: string;
  types?: string[];
  limit?: number;
}

// ============ Audit Types ============

export interface AuditLogEntry {
  id: number;
  user_id?: number;
  user_email?: string;
  action: string;
  resource_type: string;
  resource_id?: number;
  description?: string;
  changes?: Record<string, { old: unknown; new: unknown }>;
  ip_address?: string;
  user_agent?: string;
  timestamp: string;
}

// ============ Admin Settings Types ============

export interface SystemSettings {
  company_name: string;
  company_logo?: string;
  timezone: string;
  date_format: string;
  currency: string;
  work_order_prefix: string;
  po_prefix: string;
  quote_prefix: string;
}

export interface RolePermissions {
  role: UserRole;
  permissions: string[];
}

// ============ Error Handling ============

export interface ApiErrorResponse {
  detail: string;
  errors?: Record<string, string[]>;
}

export function isApiError(error: unknown): error is { response: { data: ApiErrorResponse } } {
  return (
    typeof error === 'object' &&
    error !== null &&
    'response' in error &&
    typeof (error as { response: unknown }).response === 'object' &&
    (error as { response: { data: unknown } }).response !== null &&
    'data' in (error as { response: { data: unknown } }).response
  );
}

export function getErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    return error.response.data.detail || 'An error occurred';
  }
  if (error instanceof Error) {
    return error.message;
  }
  return 'An unexpected error occurred';
}
