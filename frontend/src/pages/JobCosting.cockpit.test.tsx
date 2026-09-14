/**
 * JobCosting cockpit — instrument-panel overhaul regression.
 *
 * The Job Costing page was reworked into the cockpit aesthetic: a MiniStat KPI
 * strip (Total WIP Value / Average Margin / Over Budget / Completed This Month),
 * a side-by-side variance CockpitPanel ("Estimated vs Actual Cost") wrapping a
 * recharts BarChart, and the estimated-vs-actual job-costs table. This locks
 * that those three regions render after the initial data load.
 *
 * On mount the page calls api.get('/job-costs/') and api.get('/job-costs/summary')
 * (generic axios methods on the default export — there are no named helpers).
 * recharts' ResponsiveContainer needs ResizeObserver, which jsdom lacks, so we
 * stub it at the top of the file.
 */
import React from 'react';
import { render, screen, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import JobCosting from './JobCosting';
import type { UserRole } from '../types';
let mockRole: UserRole = 'admin';

// recharts ResponsiveContainer relies on ResizeObserver, absent in jsdom.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    get: jest.fn(),
    post: jest.fn(),
    delete: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const summary = {
  total_wip_value: 125000,
  average_margin_percent: 18.5,
  jobs_over_budget: 2,
  jobs_completed_this_month: 4,
  total_jobs: 10,
  in_progress_count: 6,
  completed_count: 4,
  total_actual_cost: 80000,
  total_estimated_cost: 90000,
};

const jobCosts = [
  {
    id: 1,
    work_order_id: 101,
    estimated_material_cost: 1000,
    estimated_labor_cost: 2000,
    estimated_overhead_cost: 500,
    estimated_total_cost: 3500,
    actual_material_cost: 1200,
    actual_labor_cost: 2100,
    actual_overhead_cost: 600,
    actual_total_cost: 3900,
    material_variance: 200,
    labor_variance: 100,
    overhead_variance: 100,
    total_variance: 400,
    margin_amount: 1100,
    margin_percent: 22.0,
    revenue: 5000,
    status: 'in_progress',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-02T00:00:00Z',
    work_order_number: 'WO-1001',
    part_number: 'PN-ABC',
    part_name: 'Bracket',
    customer_name: 'Acme Aero',
  },
];

beforeEach(() => {
  mockRole = 'admin';
  mockedApi.get.mockImplementation((url: string) => {
    if (url === '/job-costs/summary') {
      return Promise.resolve({ data: summary });
    }
    if (url === '/job-costs/') {
      return Promise.resolve({ data: jobCosts });
    }
    if (url.endsWith('/entries')) return Promise.resolve({ data: [{ id: 7, job_cost_id: 1, entry_type: 'material', description: 'Recorded cost', quantity: 2, unit_cost: 5, total_cost: 10, source: 'manual', entry_date: '2026-09-13' }] });
    return Promise.resolve({ data: [] });
  });
});

afterEach(() => {
  jest.clearAllMocks();
});

function renderPage() {
  return render(
    <MemoryRouter>
      <JobCosting />
    </MemoryRouter>
  );
}

describe('JobCosting cockpit', () => {
  it('renders the MiniStat KPI strip from the summary load', async () => {
    renderPage();

    // KPI labels appear only after the summary resolves.
    expect(await screen.findByText('Total WIP Value')).toBeInTheDocument();
    expect(screen.getByText('Average Margin')).toBeInTheDocument();
    expect(screen.getByText('Over Budget')).toBeInTheDocument();
    expect(screen.getByText('Completed This Month')).toBeInTheDocument();

    // Formatted/derived KPI values.
    expect(screen.getByText('$125,000.00')).toBeInTheDocument();
    expect(screen.getByText('18.5%')).toBeInTheDocument();
  });

  it('renders the variance-chart CockpitPanel', async () => {
    renderPage();

    expect(await screen.findByText('Estimated vs Actual Cost')).toBeInTheDocument();
    expect(screen.getByText('Top 15 jobs in current view')).toBeInTheDocument();
  });

  it('mounts the job-costs table with the loaded row', async () => {
    renderPage();

    // Wait for the row to land (loading -> rows).
    const woCell = await screen.findByText('WO-1001');
    const row = woCell.closest('tr') as HTMLElement;
    expect(row).toBeInTheDocument();

    // The table renders the estimated/actual/variance columns for the job.
    expect(within(row).getByText('$3,500.00')).toBeInTheDocument();
    expect(within(row).getByText('$3,900.00')).toBeInTheDocument();
    expect(screen.getByText('Acme Aero')).toBeInTheDocument();

    // Page heading is present.
    expect(
      screen.getByRole('heading', { name: /Job Costing & Financial Integration/i })
    ).toBeInTheDocument();
  });
});

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: mockRole, isSuperuser: false }),
}));


it.each<UserRole>(['admin', 'manager', 'platform_admin', 'supervisor', 'operator', 'quality', 'shipping', 'viewer'])(
  '%s gets financial reads with only authorized write controls', async role => {
    mockRole = role;
    renderPage();
    fireEvent.click(await screen.findByText('WO-1001'));
    expect(await screen.findByText('Recorded cost')).toBeInTheDocument();
    const allowed = ['admin', 'manager', 'platform_admin'].includes(role);
    expect(!!screen.queryByRole('button', { name: 'New Job Cost' })).toBe(allowed);
    expect(!!screen.queryByTitle('Add Cost Entry')).toBe(allowed);
    expect(!!screen.queryByTitle('Recalculate from Time Entries')).toBe(allowed);
    expect(!!screen.queryByTitle('Delete entry')).toBe(allowed);
    expect(!!screen.queryByRole('button', { name: 'Add Entry' })).toBe(allowed);
    expect(mockedApi.post).not.toHaveBeenCalled();
    expect(mockedApi.delete).not.toHaveBeenCalled();
  }
);

it('a viewer empty state does not offer job-cost creation', async () => {
  mockRole = 'viewer';
  mockedApi.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/job-costs/summary' ? summary : [] }));
  renderPage();
  expect(await screen.findByText('No job costs found')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'New Job Cost' })).not.toBeInTheDocument();
});
