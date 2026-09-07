import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { ToastProvider } from '../ui';
import CycleCountsPanel from './CycleCountsPanel';
import { CycleCountDetail } from '../../types/cycleCount';

jest.mock('../../context/AuthContext', () => ({ useAuth: jest.fn() }));
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getCycleCountWorkspace: jest.fn(),
    getCycleCount: jest.fn(),
    getCycleCountCounters: jest.fn(),
    createCycleCount: jest.fn(),
    assignCycleCount: jest.fn(),
    startCycleCount: jest.fn(),
    recordCycleCount: jest.fn(),
    reviewCycleCount: jest.fn(),
    postReviewedCycleCount: jest.fn(),
  },
}));
const mockApi = api as jest.Mocked<typeof api>;
const count: CycleCountDetail = {
  id: 1,
  count_number: 'CC-20260907-001',
  status: 'in_progress',
  scheduled_date: '2026-09-07',
  started_at: '2026-09-07T14:00:00',
  completed_at: null,
  warehouse: 'MAIN',
  location_code: 'A-01',
  part_id: null,
  assigned_to: 5,
  assigned_to_name: 'Pat Counter',
  total_items: 1,
  items_counted: 0,
  items_adjusted: 0,
  total_variance_value: 0,
  notes: 'Count morning delivery',
  items: [
    {
      id: 11,
      inventory_item_id: 21,
      part_id: 31,
      part_number: 'PLATE-001',
      part_name: 'Steel plate',
      unit_of_measure: 'each',
      location: 'A-01',
      lot_number: 'LOT-72',
      serial_number: null,
      system_quantity: 10,
      current_quantity: 10,
      counted_quantity: null,
      variance: null,
      variance_value: null,
      posting_delta: 0,
      stock_changed: false,
      is_counted: false,
      requires_recount: false,
      counted_at: null,
      notes: null,
    },
  ],
};
const counted: CycleCountDetail = {
  ...count,
  items_counted: 1,
  items: [
    {
      ...count.items[0],
      counted_quantity: 9,
      variance: -1,
      posting_delta: -1,
      is_counted: true,
      counted_at: '2026-09-07T14:01:00',
    },
  ],
};
function mount(role = 'manager', url = '/inventory?inventory_tab=counts&cycle_count=1') {
  (useAuth as jest.Mock).mockReturnValue({ user: { id: 5, role, is_superuser: false } });
  return render(
    <MemoryRouter initialEntries={[url]}>
      <ToastProvider>
        <CycleCountsPanel
          locations={[{ code: 'A-01', warehouse: 'MAIN' }]}
          parts={[{ id: 31, part_number: 'PLATE-001', name: 'Steel plate' }]}
        />
      </ToastProvider>
    </MemoryRouter>
  );
}
beforeEach(() => {
  jest.resetAllMocks();
  mockApi.getCycleCount.mockResolvedValue(count);
  mockApi.getCycleCountCounters.mockResolvedValue([{ id: 5, name: 'Pat Counter' }]);
  mockApi.getCycleCountWorkspace.mockResolvedValue({ items: [count], total: 1, has_more: false });
});

it('lets an operator save zero as a physical count without changing stock', async () => {
  mockApi.recordCycleCount.mockResolvedValue({});
  mount('operator');
  fireEvent.click((await screen.findAllByRole('button', { name: 'Count' }))[0]);
  const dialog = screen.getByRole('dialog', { name: 'Count PLATE-001' });
  fireEvent.change(within(dialog).getByLabelText(/Quantity counted/), { target: { value: '0' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save count' }));
  await waitFor(() =>
    expect(mockApi.recordCycleCount).toHaveBeenCalledWith(1, 11, {
      counted_quantity: 0,
      notes: '',
      expected_counted_at: null,
    })
  );
  expect(mockApi.postReviewedCycleCount).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Review adjustments' })).not.toBeInTheDocument();
});

it('requires explicit variance review and returns to fresh data when stock changes', async () => {
  mockApi.getCycleCount.mockResolvedValue(counted);
  mockApi.reviewCycleCount.mockResolvedValue({ ...counted, review_token: 'reviewed-1' });
  mockApi.postReviewedCycleCount.mockRejectedValue({
    response: { data: { detail: 'Stock or counts changed after review.' } },
  });
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Review adjustments' }));
  const dialog = await screen.findByRole('dialog', { name: 'Review cycle count adjustments' });
  const submit = within(dialog).getByRole('button', { name: 'Post adjustments & complete' });
  expect(submit).toBeDisabled();
  fireEvent.click(within(dialog).getByRole('checkbox'));
  fireEvent.click(submit);
  await waitFor(() => expect(mockApi.postReviewedCycleCount).toHaveBeenCalledWith(1, 'reviewed-1'));
  expect(await screen.findByText('Stock or counts changed after review.')).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('gives viewers count history with no counting or assignment controls', async () => {
  mount('viewer');
  await screen.findByText('CC-20260907-001');
  expect(screen.queryByRole('button', { name: 'Count' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Review adjustments' })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Assigned counter')).not.toBeInTheDocument();
  expect(mockApi.getCycleCountCounters).not.toHaveBeenCalled();
});

it('creates an assigned count with the chosen scope and date', async () => {
  mockApi.createCycleCount.mockResolvedValue({ id: 1 });
  mount('manager', '/inventory?inventory_tab=counts');
  fireEvent.click(await screen.findByRole('button', { name: 'Schedule count' }));
  const dialog = screen.getByRole('dialog', { name: 'Schedule cycle count' });
  fireEvent.change(within(dialog).getByLabelText(/Location/), { target: { value: 'A-01' } });
  fireEvent.change(within(dialog).getByLabelText(/Scheduled date/), { target: { value: '2026-09-09' } });
  await waitFor(() => expect(within(dialog).getByRole('option', { name: 'Pat Counter' })).toBeInTheDocument());
  fireEvent.change(within(dialog).getByLabelText('Assigned counter'), { target: { value: '5' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create count' }));
  await waitFor(() =>
    expect(mockApi.createCycleCount).toHaveBeenCalledWith({
      location_code: 'A-01',
      scheduled_date: '2026-09-09',
      assigned_to: 5,
      notes: '',
    })
  );
});
