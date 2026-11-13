/**
 * JobCosting — the SUBCONTRACT cost element (migration 121).
 *
 * Outside processing (plating, heat treat) is its own entry type and its own actual,
 * never folded into overhead the way OTHER is. Locks: the Add Entry form offers it and
 * posts the server's enum value `subcontract`; an entry row labels it; and the expanded
 * breakdown shows `actual_subcontract_cost` so the four elements reconcile to the total.
 * Response shapes mirror backend/app/schemas/job_costing.py (JobCostResponse /
 * CostEntryResponse).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import JobCosting from './JobCosting';

global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: 'admin', isSuperuser: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const summary = {
  total_wip_value: 0,
  average_margin_percent: 0,
  jobs_over_budget: 0,
  jobs_completed_this_month: 0,
  total_jobs: 1,
  in_progress_count: 1,
  completed_count: 0,
  total_actual_cost: 1250,
  total_estimated_cost: 1000,
};

const jobCost = {
  id: 1,
  work_order_id: 101,
  estimated_material_cost: 400,
  estimated_labor_cost: 500,
  estimated_overhead_cost: 100,
  estimated_total_cost: 1000,
  actual_material_cost: 400,
  actual_labor_cost: 500,
  actual_overhead_cost: 100,
  actual_subcontract_cost: 250,
  actual_total_cost: 1250,
  material_variance: 0,
  labor_variance: 0,
  overhead_variance: 0,
  total_variance: 250,
  margin_amount: 0,
  margin_percent: 0,
  revenue: 0,
  status: 'in_progress',
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-02T00:00:00Z',
  work_order_number: 'WO-1001',
  part_number: 'PN-ABC',
  part_name: 'Bracket',
  customer_name: 'Acme Aero',
};

const subcontractEntry = {
  id: 7,
  job_cost_id: 1,
  entry_type: 'subcontract',
  description: 'Zinc plating',
  quantity: 1,
  unit_cost: 250,
  total_cost: 250,
  source: 'manual',
  reference: 'PO-88',
  entry_date: '2026-11-02',
  created_at: '2026-11-02T15:00:00Z',
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.get.mockImplementation((url: string) => {
    if (url === '/job-costs/summary') return Promise.resolve({ data: summary } as any);
    if (url === '/job-costs/') return Promise.resolve({ data: [jobCost] } as any);
    if (url === '/job-costs/1/entries') return Promise.resolve({ data: [subcontractEntry] } as any);
    return Promise.resolve({ data: [] } as any);
  });
  mockedApi.post.mockResolvedValue({ data: {} } as any);
});

const renderPage = () =>
  render(
    <MemoryRouter>
      <JobCosting />
    </MemoryRouter>
  );

test('the expanded row labels subcontract entries and shows the subcontract actual', async () => {
  renderPage();
  fireEvent.click(await screen.findByText('WO-1001'));
  expect(await screen.findByText('Zinc plating')).toBeInTheDocument();
  const row = screen.getByText('Zinc plating').closest('tr') as HTMLElement;
  expect(within(row).getByText('Subcontract')).toHaveClass('du-badge-info');
  // Its own element in the breakdown, beside material / labor / overhead.
  expect(within(screen.getByTestId('subcontract-actual')).getByText('Act: $250.00')).toBeInTheDocument();
});

test('Add Entry offers Subcontract and posts the server enum value', async () => {
  renderPage();
  fireEvent.click(await screen.findByTitle('Add Cost Entry'));
  const typeSelect = await screen.findByRole('combobox', { name: 'Type' });
  expect(within(typeSelect).getByRole('option', { name: /subcontract/i })).toHaveValue('subcontract');

  fireEvent.change(typeSelect, { target: { value: 'subcontract' } });
  fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Heat treat' } });
  fireEvent.click(screen.getByRole('button', { name: 'Add Entry' }));

  await waitFor(() => expect(mockedApi.post).toHaveBeenCalled());
  const [url, body] = mockedApi.post.mock.calls[0] as [string, Record<string, unknown>];
  expect(url).toBe('/job-costs/1/entries');
  expect(body).toMatchObject({ entry_type: 'subcontract', description: 'Heat treat' });
});
