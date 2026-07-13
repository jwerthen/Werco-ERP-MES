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
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react';
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

// Resilience regression (go-live blocker FIX 3): the loader was changed from
// Promise.all to Promise.allSettled with per-section error state. ONE failing
// report endpoint must scope its error to that section (an ErrorState with a
// Retry) and MUST NOT dark-screen the rest of the page — the other sections
// keep rendering their own data.
test('a single failing report scopes its error and leaves the other sections intact', async () => {
  // Only the work-center utilization endpoint fails; everything else resolves.
  mockedApi.getWorkCenterUtilization.mockRejectedValue(new Error('boom: utilization 500'));

  renderReports();

  // The failed section renders a scoped ErrorState WITH a Retry affordance…
  expect(await screen.findByText("Couldn't load utilization")).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  // …and does NOT render its (never-loaded) data.
  expect(screen.queryByText('LASER-1')).not.toBeInTheDocument();

  // The page did NOT blank: sibling sections still render their loaded data,
  // and the headline KPI strip is intact.
  expect(screen.getByText('Acme Metals')).toBeInTheDocument(); // vendor panel
  expect(screen.getByText('On-Time Delivery')).toBeInTheDocument(); // KPI strip
  expect(screen.getByRole('heading', { name: 'Reports & Analytics' })).toBeInTheDocument();

  // Only the one failing section shows an error — no other Retry buttons.
  expect(screen.getAllByRole('button', { name: /retry/i })).toHaveLength(1);
});

// KPI-strip resilience (follow-up to FIX 3): when a headline source errors, its
// tile must render an em dash "—" + "Unavailable" (NOT a fabricated $0 / 0.0%
// that reads as real data), a strip-level ErrorState appears above the strip
// with a Retry that re-runs loadData, and the rest of the page stays intact.
test('a failed KPI source renders — / Unavailable (not a fabricated $0) and a retryable strip error', async () => {
  // Only the inventory source (the Inventory Value tile) fails.
  mockedApi.getInventoryValue.mockRejectedValue(new Error('boom: inventory 500'));

  renderReports();

  // Strip-level ErrorState appears above the KPI strip.
  expect(await screen.findByText("Some headline metrics couldn't load")).toBeInTheDocument();

  // The Inventory Value tile shows the em dash + "Unavailable", never a fake $0.
  const invTile = screen.getByText('Inventory Value').closest('.card') as HTMLElement;
  expect(invTile).not.toBeNull();
  expect(within(invTile).getByText('—')).toBeInTheDocument(); // "—"
  expect(within(invTile).getByText('Unavailable')).toBeInTheDocument();
  expect(within(invTile).queryByText(/\$/)).not.toBeInTheDocument(); // no $0 / $ amount fabricated

  // The page did NOT blank: production KPIs (real values) and sibling panels render.
  expect(screen.getByText('On-Time Delivery')).toBeInTheDocument();
  expect(screen.getByText('85.7%')).toBeInTheDocument(); // real, loaded production value
  expect(screen.getByText('LASER-1')).toBeInTheDocument(); // utilization panel
  expect(screen.getByText('Acme Metals')).toBeInTheDocument(); // vendor panel

  // Inventory is a strip-only metric (no dedicated panel), so exactly one Retry
  // exists — the strip-level one — and clicking it re-runs the full loadData.
  const retryButtons = screen.getAllByRole('button', { name: /retry/i });
  expect(retryButtons).toHaveLength(1);
  expect(mockedApi.getInventoryValue).toHaveBeenCalledTimes(1);
  fireEvent.click(retryButtons[0]);
  await waitFor(() => expect(mockedApi.getInventoryValue).toHaveBeenCalledTimes(2));
});
