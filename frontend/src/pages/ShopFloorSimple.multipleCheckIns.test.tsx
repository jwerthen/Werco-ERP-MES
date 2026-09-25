import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    startOperation: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: 'operator' } }),
}));

jest.mock('../context/CompanyContext', () => ({
  useCompany: () => ({ currentCompany: { id: 1 } }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const operations = Array.from({ length: 12 }, (_, index) => ({
  id: index + 1,
  work_order_id: index + 101,
  work_order_number: `WO-WELD-${index + 1}`,
  part_number: 'WELD-FRAME',
  part_name: 'Frame',
  operation_number: '10',
  operation_name: `Weldout ${index + 1}`,
  description: null,
  work_center_id: 1,
  work_center_name: 'Weld shop',
  status: 'in_progress',
  quantity_ordered: 20,
  quantity_complete: 3,
  quantity_scrapped: 0,
  priority: 3,
  due_date: null,
  customer_name: null,
  customer_po: null,
  actual_start: null,
  setup_instructions: null,
  run_instructions: null,
  requires_inspection: false,
  can_check_in: true,
}));

const jobs = operations.map((operation) => ({
  ...operation,
  operation_id: operation.id,
  time_entry_id: operation.id + 1000,
  clock_in: '2026-09-24T12:00:00Z',
  entry_type: 'run' as const,
}));

function renderPage() {
  return render(<MemoryRouter><ShopFloorSimple /></MemoryRouter>);
}

beforeEach(() => {
  jest.resetAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: jobs });
});

it('shows every active check-in even when its operation is outside the current queue', async () => {
  renderPage();
  const list = await screen.findByRole('list', { name: 'Checked-in operations' });
  expect(screen.getByRole('heading', { name: 'You are checked into 12 operations' })).toBeInTheDocument();
  expect(within(list).getAllByRole('listitem')).toHaveLength(12);
  for (const job of jobs) {
    expect(within(list).getByText(new RegExp(`${job.work_order_number} ·`))).toBeInTheDocument();
  }
  expect(within(list).getAllByText(/3\/20 complete/)).toHaveLength(12);
});

it.each(['ready', 'in_progress'])('allows a ninth through twelfth check-in to %s operations', async (status) => {
  let activeJobs = jobs.slice(0, 8);
  mockedApi.getMyActiveJob.mockImplementation(async () => ({ active_jobs: activeJobs }));
  mockedApi.getShopFloorOperations.mockImplementation(async () => ({
    operations: operations.slice(8).map((operation) => ({
      ...operation,
      status: activeJobs.some((job) => job.operation_id === operation.id) ? 'in_progress' : status,
    })),
  }));
  mockedApi.startOperation.mockImplementation(async (id) => {
    activeJobs = [...activeJobs, jobs.find((job) => job.operation_id === id)!];
    return {};
  });
  mockedApi.clockIn.mockImplementation(async (data) => {
    activeJobs = [...activeJobs, jobs.find((job) => job.operation_id === data.operation_id)!];
    return {};
  });
  renderPage();
  await screen.findByRole('heading', { name: 'You are checked into 8 operations' });
  for (const operation of operations.slice(8)) {
    const card = await screen.findByTestId(`shop-floor-op-${operation.id}`);
    fireEvent.click(within(card).getByRole('button', { name: 'Check In' }));
    await screen.findByRole('heading', { name: `You are checked into ${operation.id} operations` });
  }
  expect(within(screen.getByRole('list', { name: 'Checked-in operations' })).getAllByRole('listitem')).toHaveLength(12);
  expect(status === 'ready' ? mockedApi.startOperation : mockedApi.clockIn).toHaveBeenCalledTimes(4);
  expect(mockedApi.clockOut).not.toHaveBeenCalled();
});

it('checks out the twelfth entry without ending any other check-ins', async () => {
  mockedApi.clockOut.mockImplementation(async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: jobs.slice(0, 11) });
    return {};
  });
  renderPage();
  const list = await screen.findByRole('list', { name: 'Checked-in operations' });
  const lastRow = within(list).getAllByRole('listitem')[11];
  fireEvent.click(within(lastRow).getByRole('button', { name: 'Check Out' }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText(/WO-WELD-12/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: 'End time and save' }));
  await waitFor(() => expect(mockedApi.clockOut).toHaveBeenCalledWith(1012, expect.objectContaining({
    quantity_produced: 0,
    quantity_scrapped: 0,
  })));
  await screen.findByRole('heading', { name: 'You are checked into 11 operations' });
  expect(mockedApi.clockOut).toHaveBeenCalledTimes(1);
  expect(within(list).getAllByRole('listitem')).toHaveLength(11);
  expect(within(list).queryByText(/WO-WELD-12/)).not.toBeInTheDocument();
});

it('explains that Waiting is caused by earlier operations even with twelve active check-ins', async () => {
  mockedApi.getShopFloorOperations.mockResolvedValue({
    operations: [{ ...operations[0], id: 99, status: 'pending', can_check_in: false, blocked_by_previous_operations: true }],
  });
  renderPage();
  const card = await screen.findByTestId('shop-floor-op-99');
  expect(within(card).getByRole('button', { name: 'Waiting' })).toBeDisabled();
  expect(within(card).getByText(/Waiting for earlier operations on this work order to be completed/)).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'You are checked into 12 operations' })).toBeInTheDocument();
  expect(mockedApi.startOperation).not.toHaveBeenCalled();
  expect(mockedApi.clockIn).not.toHaveBeenCalled();
});
