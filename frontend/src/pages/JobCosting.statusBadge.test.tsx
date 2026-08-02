/**
 * JobCosting — shared-primitive adoption locks:
 *   - the loading state renders Skeleton rows spanning the table (not a bare
 *     "Loading..." cell),
 *   - the status column renders through <StatusBadge> with the CENTRAL
 *     statusColors classes (the private du-badge map is gone) — `reviewed`
 *     resolves blue, matching the du-badge-info look it had before.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import JobCosting from './JobCosting';

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
  total_wip_value: 0,
  average_margin_percent: 0,
  jobs_over_budget: 0,
  jobs_completed_this_month: 0,
  total_jobs: 1,
  in_progress_count: 0,
  completed_count: 0,
  total_actual_cost: 0,
  total_estimated_cost: 0,
};

const jobCost = (status: string) => ({
  id: 1,
  work_order_id: 101,
  estimated_material_cost: 0,
  estimated_labor_cost: 0,
  estimated_overhead_cost: 0,
  estimated_total_cost: 0,
  actual_material_cost: 0,
  actual_labor_cost: 0,
  actual_overhead_cost: 0,
  actual_total_cost: 0,
  material_variance: 0,
  labor_variance: 0,
  overhead_variance: 0,
  total_variance: 0,
  margin_amount: 0,
  margin_percent: 0,
  revenue: 0,
  status,
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-02T00:00:00Z',
  work_order_number: 'WO-1001',
  part_number: 'PN-ABC',
  part_name: 'Bracket',
  customer_name: 'Acme Aero',
});

function mockLoad(status: string) {
  mockedApi.get.mockImplementation((url: string) => {
    if (url === '/job-costs/summary') return Promise.resolve({ data: summary });
    if (url === '/job-costs/') return Promise.resolve({ data: [jobCost(status)] });
    return Promise.resolve({ data: [] });
  });
}

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

it('shows Skeleton rows while loading, then the table', async () => {
  mockLoad('in_progress');
  renderPage();

  // While the fetch is pending, skeleton cells fill the table.
  expect(screen.getAllByTestId('skeleton').length).toBeGreaterThan(0);
  expect(screen.queryByText('Loading...')).not.toBeInTheDocument();

  expect(await screen.findByText('WO-1001')).toBeInTheDocument();
});

it.each([
  // reviewed keeps its info look via the central map (blue), not a local du-badge.
  ['reviewed', ['bg-blue-500/20', 'text-blue-300']],
  ['in_progress', ['bg-blue-500/20', 'text-blue-300']],
  ['completed', ['bg-green-500/20', 'text-emerald-300']],
])('renders the %s status through the central StatusBadge classes', async (status, classes) => {
  mockLoad(status);
  renderPage();
  await screen.findByText('Total WIP Value');

  // completed/reviewed rows live on the Completed tab (default tab is Active).
  if (status !== 'in_progress') {
    fireEvent.click(screen.getByRole('button', { name: /completed/i }));
  }

  const badge = await screen.findByText(status.replace(/_/g, ' '));
  expect(badge).toHaveClass(...classes);
});
