/**
 * Mock Data for Tests
 * 
 * Centralized mock data that matches the application's types.
 */

import { Part, WorkOrderSummary, Customer, WorkCenter } from '../types';

// Mock Parts
export const mockPart: Part = {
  id: 1,
  part_number: 'TEST-001',
  name: 'Test Part',
  revision: 'A',
  part_type: 'manufactured',
  description: 'A test part for unit testing',
  unit_of_measure: 'each',
  standard_cost: 100,
  material_cost: 50,
  labor_cost: 30,
  overhead_cost: 20,
  lead_time_days: 5,
  safety_stock: 10,
  reorder_point: 20,
  reorder_quantity: 50,
  is_critical: false,
  requires_inspection: true,
  is_active: true,
  status: 'active',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  version: 1,
};

export const mockParts: Part[] = [
  mockPart,
  {
    ...mockPart,
    id: 2,
    part_number: 'TEST-002',
    name: 'Test Assembly',
    part_type: 'assembly',
  },
  {
    ...mockPart,
    id: 3,
    part_number: 'RAW-001',
    name: 'Raw Material',
    part_type: 'raw_material',
  },
];

// Mock Work Orders
export const mockWorkOrder: WorkOrderSummary = {
  id: 1,
  work_order_number: 'WO-20240101-001',
  part_id: 1,
  part_number: 'TEST-001',
  part_name: 'Test Part',
  quantity_ordered: 100,
  quantity_complete: 50,
  status: 'in_progress',
  priority: 3,
  due_date: '2024-02-01',
  scheduled_start: '2024-01-15',
  created_at: '2024-01-01T00:00:00Z',
  customer_name: 'Test Customer',
};

export const mockWorkOrders: WorkOrderSummary[] = [
  mockWorkOrder,
  {
    ...mockWorkOrder,
    id: 2,
    work_order_number: 'WO-20240101-002',
    status: 'released',
    priority: 1,
  },
  {
    ...mockWorkOrder,
    id: 3,
    work_order_number: 'WO-20240101-003',
    status: 'complete',
    quantity_complete: 100,
  },
];

// Mock Customers
export const mockCustomer: Customer = {
  id: 1,
  name: 'Test Customer Inc',
  code: 'TC001',
  contact_name: 'John Doe',
  email: 'john@testcustomer.com',
  phone: '555-1234',
  address_line1: '123 Test Street',
  city: 'Test City',
  state: 'TS',
  zip_code: '12345',
  requires_coc: true,
  requires_fai: false,
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
};

export const mockCustomers: Customer[] = [
  mockCustomer,
  {
    ...mockCustomer,
    id: 2,
    name: 'Another Customer LLC',
    code: 'AC001',
  },
];

// Mock Work Centers
export const mockWorkCenter: WorkCenter = {
  id: 1,
  code: 'CNC-01',
  name: 'CNC Machine 1',
  work_center_type: 'cnc_machining',
  description: 'Primary CNC machining center',
  hourly_rate: 75,
  capacity_hours_per_day: 8,
  efficiency_factor: 0.85,
  is_active: true,
  current_status: 'available',
  version: 1,
};

export const mockWorkCenters: WorkCenter[] = [
  mockWorkCenter,
  {
    ...mockWorkCenter,
    id: 2,
    code: 'LASER-01',
    name: 'Laser Cutter 1',
    work_center_type: 'laser',
  },
];

// Mock Dashboard Data
export const mockDashboardData = {
  active_work_orders: 15,
  late_work_orders: 2,
  work_orders_due_today: 3,
  pending_operations: 45,
  on_time_delivery_pct: 95.5,
  work_center_statuses: [
    { id: 1, code: 'CNC-01', name: 'CNC Machine 1', current_status: 'in_use' },
    { id: 2, code: 'LASER-01', name: 'Laser Cutter 1', current_status: 'available' },
  ],
  recent_completions: [],
  upcoming_due: [],
};

// Mock API service
export const createMockApi = () => ({
  getParts: jest.fn().mockResolvedValue(mockParts),
  getPart: jest.fn().mockResolvedValue(mockPart),
  createPart: jest.fn().mockResolvedValue(mockPart),
  updatePart: jest.fn().mockResolvedValue(mockPart),
  deletePart: jest.fn().mockResolvedValue({ message: 'Deleted' }),
  
  getWorkOrders: jest.fn().mockResolvedValue(mockWorkOrders),
  getWorkOrder: jest.fn().mockResolvedValue(mockWorkOrder),
  
  getCustomers: jest.fn().mockResolvedValue(mockCustomers),
  getCustomer: jest.fn().mockResolvedValue(mockCustomer),
  
  getWorkCenters: jest.fn().mockResolvedValue(mockWorkCenters),
  
  getDashboard: jest.fn().mockResolvedValue(mockDashboardData),
  getDashboardWithCache: jest.fn().mockResolvedValue({ data: mockDashboardData, changed: true }),
  
  login: jest.fn().mockResolvedValue({ access_token: 'test-token', refresh_token: 'test-refresh' }),
  logout: jest.fn().mockResolvedValue(undefined),
  getCurrentUser: jest.fn().mockResolvedValue({
    id: 1,
    email: 'test@werco.com',
    first_name: 'Test',
    last_name: 'User',
    role: 'admin',
    is_active: true,
    is_superuser: true,
  }),
});
