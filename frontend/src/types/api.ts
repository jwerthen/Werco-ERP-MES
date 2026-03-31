/**
 * API Request and Response Types
 * These types define the shape of data sent to and received from the API
 */

import {
  User, UserRole, PartType,
  WorkCenter, WorkCenterType
} from './index';

// ============ Generic Types ============

export interface ApiError {
  detail: string;
  status?: number;
}

// ============ Auth Types ============

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
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

// ============ Quality Types ============

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
  address_line1?: string;
  address_line2?: string;
  city?: string;
  state?: string;
  zip?: string;
  zip_code?: string;
  country?: string;
  ship_to_name?: string;
  ship_address_line1?: string;
  ship_city?: string;
  ship_state?: string;
  ship_zip_code?: string;
  payment_terms?: string;
  requires_coc?: boolean;
  requires_fai?: boolean;
  special_requirements?: string;
  notes?: string;
}

export interface CustomerNameOption {
  id: number;
  name: string;
}

export interface CustomerLinkedPart {
  id: number;
  part_number: string;
  name: string;
  revision?: string;
  part_type: string;
  customer_part_number?: string;
  is_active: boolean;
}

export interface CustomerLinkedWorkOrder {
  id: number;
  work_order_number: string;
  status: string;
  due_date?: string;
  quantity_ordered: number;
  created_at?: string;
  part_id?: number;
  part_number?: string;
  part_name?: string;
  customer_name?: string;
  customer_po?: string;
}

export interface CustomerStatsResponse {
  customer_id: number;
  customer_name: string;
  part_count: number;
  work_order_counts: {
    total: number;
    by_status: Record<string, number>;
  };
  parts: CustomerLinkedPart[];
  assemblies: CustomerLinkedPart[];
  current_work_orders: CustomerLinkedWorkOrder[];
  past_work_orders: CustomerLinkedWorkOrder[];
  recent_work_orders: CustomerLinkedWorkOrder[];
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



// ============ Search Types ============

export interface SearchResult {
  type: 'work_order' | 'part' | 'customer' | 'purchase_order' | 'quote';
  id: number;
  title: string;
  subtitle?: string;
  url: string;
}




export interface QMSEvidenceResponse {
  id: number;
  clause_id: number;
  evidence_type: string;
  title: string;
  description?: string;
  document_id?: number;
  module_reference?: string;
  record_type?: string;
  record_id?: number;
  is_verified: boolean;
  verified_by?: number;
  verified_date?: string;
  verification_notes?: string;
  is_auto_linked: boolean;
  auto_link_query?: string;
  last_refreshed?: string;
  live_count?: number;
  created_by?: number;
  created_at: string;
  updated_at?: string;
}

export interface QMSClauseResponse {
  id: number;
  standard_id: number;
  clause_number: string;
  title: string;
  description?: string;
  parent_clause_id?: number;
  sort_order: number;
  compliance_status: string;
  compliance_notes?: string;
  last_assessed_date?: string;
  last_assessed_by?: number;
  next_review_date?: string;
  evidence_links: QMSEvidenceResponse[];
  sub_clauses: QMSClauseResponse[];
  created_at: string;
  updated_at?: string;
}

export interface QMSStandardResponse {
  id: number;
  name: string;
  version?: string;
  description?: string;
  standard_body?: string;
  document_id?: number;
  is_active: boolean;
  created_by?: number;
  created_at: string;
  updated_at?: string;
  clauses: QMSClauseResponse[];
}

export interface QMSStandardListResponse {
  id: number;
  name: string;
  version?: string;
  description?: string;
  standard_body?: string;
  is_active: boolean;
  total_clauses: number;
  compliant_clauses: number;
  partial_clauses: number;
  non_compliant_clauses: number;
  not_assessed_clauses: number;
  created_at: string;
}

export interface QMSAuditReadinessSummary {
  total_standards: number;
  total_clauses: number;
  compliant: number;
  partial: number;
  non_compliant: number;
  not_assessed: number;
  not_applicable: number;
  compliance_percentage: number;
  total_evidence_links: number;
  verified_evidence: number;
  unverified_evidence: number;
  clauses_needing_review: number;
}

// ============ Auto-Evidence Discovery Types ============

export interface AutoEvidenceExample {
  record_id: number;
  record_identifier: string;
  record_type: string;
  summary: string;
  status: string;
  date: string;
  module_link: string;
}

export interface AutoEvidenceResult {
  evidence_type: string;
  title: string;
  description: string;
  module_reference: string;
  total_count: number;
  recent_count: number;
  health_status: 'healthy' | 'warning' | 'critical' | 'no_data';
  health_detail: string;
  examples: AutoEvidenceExample[];
  suggested_compliance: string;
}

export interface ClauseAutoEvidenceResponse {
  clause_id: number;
  clause_number: string;
  discovered_evidence: AutoEvidenceResult[];
  overall_suggested_compliance: string;
}

export interface AutoLinkSummary {
  standard_id: number;
  standard_name: string;
  total_clauses: number;
  clauses_with_evidence: number;
  clauses_without_evidence: number;
  total_evidence_created: number;
  total_evidence_updated: number;
  compliance_summary: Record<string, number>;
}

