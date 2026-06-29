/**
 * Reports cockpit — instrument-panel overhaul regression.
 *
 * The Reports "Dashboard" tab was rebuilt around the shared cockpit primitives:
 * a MiniStatStrip of headline KPIs (On-Time Delivery, Hours Worked, Scrap Rate,
 * Inventory Value, Total NCRs, Qty Received, Recv Reject Rate) sitting above a
 * grid of CockpitPanel sections (Daily Production Output, Work Center
 * Utilization, Top Vendor Performance, Quality & Work Orders). This locks that
 * the strip and the panels render after the initial parallel data load, and that
 * the headline metrics surface their loaded values.
 *
 * The page renders no recharts, but per the test convention we stub
 * ResizeObserver defensively so jsdom never trips on a charting container.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Reports from './Reports';

// jsdom has no ResizeObserver; stub it defensively (convention for cockpit pages).
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getProductionSummary: jest.fn(),
    getQualityMetrics: jest.fn(),
    getInventoryValue: jest.fn(),
    getVendorPerformance: jest.fn(),
    getWorkCenterUtilization: jest.fn(),
    getDailyOutput: jest.fn(),
    getWorkOrderCosting: jest.fn(),
    getEmployeeTimeReport: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const production = {
  period_days: 30,
  work_orders_by_status: { in_progress: 4, completed: 7 },
  total_completed: 7,
  on_time_delivery_count: 6,
  on_time_delivery_pct: 85.7,
  total_hours_worked: 320,
  total_produced: 540,
  total_scrapped: 12,
  scrap_rate_pct: 2.22,
};

const quality = {
  period_days: 30,
  total_ncrs: 3,
  open_ncrs: 1,
  ncr_by_status: { open: 1, closed: 2 },
  ncr_by_source: { receiving: 2, in_process: 1 },
  receiving_total_qty: 1000,
  receiving_rejected_qty: 8,
  receiving_reject_rate_pct: 0.8,
};

const inventory = { total_value: 125000, total_quantity: 4200, unique_parts: 88 };

const vendors = [
  {
    vendor_id: 1,
    vendor_code: 'ACME',
    vendor_name: 'Acme Metals',
    total_ordered: 100,
    total_received: 95,
    fill_rate_pct: 95,
    reject_rate_pct: 1.5,
    po_count: 12,
  },
];

const utilization = [
  {
    work_center_id: 1,
    work_center_code: 'LASER-1',
    work_center_name: 'Laser cell 1',
    hours_worked: 60,
    available_hours: 80,
    utilization_pct: 75,
  },
];

const dailyOutput = [
  { date: '2026-06-20', completed: 10, scrapped: 1 },
  { date: '2026-06-21', completed: 14, scrapped: 0 },
];

const renderReports = () => render(<MemoryRouter><Reports /></MemoryRouter>);

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getProductionSummary.mockResolvedValue(production as any);
  mockedApi.getQualityMetrics.mockResolvedValue(quality as any);
  mockedApi.getInventoryValue.mockResolvedValue(inventory as any);
  mockedApi.getVendorPerformance.mockResolvedValue(vendors as any);
  mockedApi.getWorkCenterUtilization.mockResolvedValue(utilization as any);
  mockedApi.getDailyOutput.mockResolvedValue(dailyOutput as any);
  mockedApi.getWorkOrderCosting.mockResolvedValue([] as any);
  mockedApi.getEmployeeTimeReport.mockResolvedValue([] as any);
});

test('renders the MiniStat KPI strip after the initial data load', async () => {
  renderReports();

  // Each headline KPI tile from the cockpit strip is present…
  expect(await screen.findByText('On-Time Delivery')).toBeInTheDocument();
  expect(screen.getByText('Hours Worked')).toBeInTheDocument();
  expect(screen.getByText('Scrap Rate')).toBeInTheDocument();
  expect(screen.getByText('Inventory Value')).toBeInTheDocument();
  expect(screen.getByText('Total NCRs')).toBeInTheDocument();
  expect(screen.getByText('Qty Received')).toBeInTheDocument();
  expect(screen.getByText('Recv Reject Rate')).toBeInTheDocument();

  // …and surface their loaded values (formatted per the page).
  expect(screen.getByText('85.7%')).toBeInTheDocument();
  expect(screen.getByText('2.22%')).toBeInTheDocument();
  expect(screen.getByText('$125,000')).toBeInTheDocument();
});

test('renders the CockpitPanel report sections', async () => {
  renderReports();

  expect(await screen.findByRole('heading', { name: 'Daily Production Output' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Work Center Utilization' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Top Vendor Performance' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Quality & Work Orders' })).toBeInTheDocument();

  // Panel content from the loaded data renders too.
  expect(screen.getByText('LASER-1')).toBeInTheDocument();
  expect(screen.getByText('Acme Metals')).toBeInTheDocument();
});
