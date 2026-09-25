import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';
import { ActiveJob, WorkCenter } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    getOperationDetails: jest.fn(),
    startOperation: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    reportOperationProduction: jest.fn(),
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

const LASER: WorkCenter = {
  id: 1,
  version: 1,
  code: 'LASER1',
  name: 'Laser 1',
  work_center_type: 'laser_cutting',
  hourly_rate: 125,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const BRAKE: WorkCenter = { ...LASER, id: 2, code: 'BRAKE1', name: 'Brake 1' };

const CUT = {
  id: 101,
  work_order_id: 42,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_number: '10',
  operation_name: 'Laser Cut',
  description: null,
  work_center_id: LASER.id,
  work_center_name: LASER.name,
  status: 'in_progress',
  quantity_ordered: 25,
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
};

const BEND = {
  ...CUT,
  id: 202,
  operation_number: '20',
  operation_name: 'Bend',
  work_center_id: BRAKE.id,
  work_center_name: BRAKE.name,
};

function activeJob(operation: typeof CUT, overrides: Partial<ActiveJob> = {}): ActiveJob {
  return {
    ...operation,
    operation_id: operation.id,
    time_entry_id: operation.id + 1000,
    clock_in: '2026-09-24T12:00:00Z',
    entry_type: 'run',
    ...overrides,
  };
}

const MISSING_OPERATION_MESSAGE =
  'This operation is no longer in the queue. Refresh your checked-in operations and try again.';

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/shop-floor/operations?kiosk=1']}>
      <ShopFloorSimple />
    </MemoryRouter>
  );
}

function goToBend() {
  return screen.getByRole('button', { name: 'Go to WO-2026-0042, Op 20 - Bend' });
}

describe('checked-in operation shortcuts', () => {
  let scrollIntoView: jest.Mock;

  beforeEach(() => {
    jest.resetAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    scrollIntoView = jest.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView;
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [CUT, BEND] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
    mockedApi.getWorkCenters.mockResolvedValue([LASER, BRAKE]);
    mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [activeJob(CUT), activeJob(BEND)] });
    mockedApi.getScrapReasonCodes.mockResolvedValue([]);
  });

  it('focuses the selected operation when two check-ins share a work order', async () => {
    const earlierJobs = Array.from({ length: 12 }, (_, index) => ({
      ...CUT,
      id: 300 + index,
      work_order_id: 100 + index,
      work_order_number: `WO-EARLIER-${index}`,
    }));
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [CUT, ...earlierJobs, BEND] });
    renderPage();
    const bendCard = await screen.findByTestId('shop-floor-op-202');
    const cutCard = screen.getByTestId('shop-floor-op-101');
    const queryCount = mockedApi.getShopFloorOperations.mock.calls.length;

    expect(within(goToBend()).getByText(/Go to operation/)).toBeInTheDocument();
    fireEvent.click(goToBend());

    await waitFor(() => expect(bendCard).toHaveFocus());
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(bendCard);
    expect(bendCard).toHaveClass('ring-werco-500/60');
    expect(cutCard).not.toHaveClass('ring-werco-500/60');
    expect(mockedApi.getShopFloorOperations).toHaveBeenCalledTimes(queryCount);

    fireEvent.click(screen.getByRole('button', { name: 'Go to WO-2026-0042, Op 10 - Laser Cut' }));
    await waitFor(() => expect(cutCard).toHaveFocus());
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(cutCard);
    expect(cutCard).toHaveClass('ring-werco-500/60');
    expect(bendCard).not.toHaveClass('ring-werco-500/60');
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();
    expect(mockedApi.getOperationDetails).not.toHaveBeenCalled();
  });

  it('clears conflicting filters and waits for an operation outside the initial queue to load', async () => {
    localStorage.setItem('shop_floor_work_center_id', String(LASER.id));
    const firstPage = Array.from({ length: 50 }, (_, index) => ({
      ...CUT,
      id: 300 + index,
      work_order_id: 100 + index,
      work_order_number: `WO-EARLIER-${index}`,
    }));
    let resolveTarget!: (value: { operations: typeof CUT[] }) => void;
    const delayedTarget = new Promise<{ operations: typeof CUT[] }>((resolve) => {
      resolveTarget = resolve;
    });
    mockedApi.getShopFloorOperations.mockImplementation(async (params) => {
      if (params?.work_center_id === BRAKE.id && params.search === BEND.work_order_number) {
        return delayedTarget;
      }
      return { operations: firstPage };
    });
    renderPage();
    await screen.findByTestId('shop-floor-op-300');
    fireEvent.change(screen.getByDisplayValue('All Status'), { target: { value: 'ready' } });
    fireEvent.click(screen.getByRole('button', { name: 'Filter jobs' }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Due Today' })[0]);
    fireEvent.change(screen.getByPlaceholderText('Search WO or part...'), { target: { value: 'OTHER-WO' } });
    await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenCalledWith({
      work_center_id: LASER.id,
      status: 'ready',
      due_today: true,
      search: 'OTHER-WO',
    }));

    fireEvent.click(goToBend());
    await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenCalledWith({
      work_center_id: BRAKE.id,
      search: BEND.work_order_number,
    }));
    expect(screen.queryByTestId('shop-floor-op-202')).not.toBeInTheDocument();
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(screen.queryByText(MISSING_OPERATION_MESSAGE)).not.toBeInTheDocument();

    await act(async () => resolveTarget({ operations: [BEND] }));
    const bendCard = await screen.findByTestId('shop-floor-op-202');
    await waitFor(() => expect(bendCard).toHaveFocus());
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(bendCard);
    expect(bendCard).toHaveClass('ring-werco-500/60');
    expect(screen.getByPlaceholderText('Search WO or part...')).toHaveValue(BEND.work_order_number);
    expect(screen.queryByText(MISSING_OPERATION_MESSAGE)).not.toBeInTheDocument();
  });

  it('keeps Check Out separate from the operation shortcut', async () => {
    renderPage();
    const list = await screen.findByRole('list', { name: 'Checked-in operations' });
    const bendRow = within(list).getAllByRole('listitem')[1];
    fireEvent.click(within(bendRow).getByRole('button', { name: 'Check Out' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Op 20 - Bend/)).toBeInTheDocument();
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
    expect(mockedApi.getOperationDetails).not.toHaveBeenCalled();
  });

  it('honors a later tap on a visible job while the first shortcut is still loading', async () => {
    let resolveBend!: (value: { operations: typeof CUT[] }) => void;
    const delayedBend = new Promise<{ operations: typeof CUT[] }>((resolve) => {
      resolveBend = resolve;
    });
    mockedApi.getShopFloorOperations.mockImplementation(async (params) => {
      if (params?.work_center_id === BRAKE.id) return delayedBend;
      return { operations: [CUT] };
    });
    renderPage();
    await screen.findByTestId('shop-floor-op-101');

    fireEvent.click(goToBend());
    await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenCalledWith({
      work_center_id: BRAKE.id,
      search: BEND.work_order_number,
    }));
    // The old queue still shows Laser Cut while Bend's new queue is loading.
    fireEvent.click(screen.getByRole('button', { name: 'Go to WO-2026-0042, Op 10 - Laser Cut' }));
    await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenCalledWith({
      work_center_id: LASER.id,
      search: CUT.work_order_number,
    }));
    const cutCard = screen.getByTestId('shop-floor-op-101');
    await waitFor(() => expect(cutCard).toHaveFocus());

    // Resolving the older response last must not replace the operator's choice.
    await act(async () => resolveBend({ operations: [BEND] }));
    expect(screen.getByTestId('shop-floor-op-101')).toBe(cutCard);
    expect(screen.queryByTestId('shop-floor-op-202')).not.toBeInTheDocument();
    expect(screen.getByDisplayValue(LASER.name)).toBeInTheDocument();
    expect(cutCard).toHaveFocus();
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(cutCard);
    expect(screen.queryByText(MISSING_OPERATION_MESSAGE)).not.toBeInTheDocument();
  });

  it('explains when a checked-in operation is no longer returned by the queue', async () => {
    mockedApi.getShopFloorOperations.mockImplementation(async (params) => ({
      operations: params?.search === BEND.work_order_number ? [] : [CUT],
    }));
    renderPage();
    await screen.findByTestId('shop-floor-op-101');
    fireEvent.click(goToBend());

    expect(await screen.findByText(MISSING_OPERATION_MESSAGE)).toBeInTheDocument();
    expect(goToBend()).toBeInTheDocument();
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
    expect(mockedApi.getOperationDetails).not.toHaveBeenCalled();
  });

  it('matches a legacy active job by work order and operation number, not its time entry id', async () => {
    const legacyJob = activeJob(BEND, { operation_id: undefined, time_entry_id: CUT.id });
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [legacyJob] });
    renderPage();
    const bendCard = await screen.findByTestId('shop-floor-op-202');
    fireEvent.click(goToBend());

    await waitFor(() => expect(bendCard).toHaveFocus());
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(bendCard);
    expect(screen.getByTestId('shop-floor-op-101')).not.toHaveFocus();
    expect(mockedApi.getOperationDetails).not.toHaveBeenCalled();
  });

  it('does not confuse a different operation with the same work order and operation number', async () => {
    const unrelatedOperation = { ...BEND, id: 999 };
    mockedApi.getShopFloorOperations.mockImplementation(async (params) => ({
      operations: params?.search === BEND.work_order_number ? [BEND] : [unrelatedOperation],
    }));
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [activeJob(BEND)] });
    renderPage();
    const unrelatedCard = await screen.findByTestId('shop-floor-op-999');
    fireEvent.click(goToBend());

    const bendCard = await screen.findByTestId('shop-floor-op-202');
    await waitFor(() => expect(bendCard).toHaveFocus());
    expect(scrollIntoView.mock.contexts).not.toContain(unrelatedCard);
  });

  it('can retry a failed queue load without losing the requested operation', async () => {
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    try {
      mockedApi.getShopFloorOperations.mockImplementation(async (params) => {
        if (params?.search === BEND.work_order_number) throw new Error('Network unavailable');
        return { operations: [CUT] };
      });
      renderPage();
      await screen.findByTestId('shop-floor-op-101');
      fireEvent.click(goToBend());
      const error = await screen.findByTestId('error-state');
      expect(error).toHaveTextContent('Could not load operations');
      expect(screen.queryByText(MISSING_OPERATION_MESSAGE)).not.toBeInTheDocument();

      mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [BEND] });
      fireEvent.click(within(error).getByRole('button', { name: /retry/i }));
      const bendCard = await screen.findByTestId('shop-floor-op-202');
      await waitFor(() => expect(bendCard).toHaveFocus());
      expect(scrollIntoView.mock.contexts.at(-1)).toBe(bendCard);
    } finally {
      consoleError.mockRestore();
    }
  });
});
