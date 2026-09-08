/**
 * Scheduling (/scheduling planners' "Dispatch Queue") — server run-order rendering.
 *
 * Owner decision: this is a CROSS-machine list, so the per-machine dispatch
 * rank is display context only — never a sort key. GET /scheduling/work-orders
 * arrives in the server's canonical planner order (priority -> due date -> WO
 * number) and this page renders it VERBATIM. The old client dispatch-score
 * re-sort (utils/dispatchScore.ts, now deleted) is gone, along with its blue
 * score badge: the Run column shows the shared KioskRunOrderChip instead —
 * the current op's gap-free position on its work center's live queue.
 *
 * The payload is constructed so the OLD score would have re-ordered it: the
 * LAST row is an overdue priority-5 job (~444 points, the score sort's #1
 * pick), while the server leads with the priority-1 job (~195 points). If a
 * client re-sort ever creeps back in, these tests fail.
 */

import React from 'react';
import { render, screen, within, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../services/api';
import Scheduling from './Scheduling';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getSchedulableWorkOrders: jest.fn(),
    getCapacityHeatmap: jest.fn(),
    updateWorkOrderPriority: jest.fn(),
    updateOperationWorkCenter: jest.fn(),
    scheduleWorkOrder: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'manager', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workCenters = [{ id: 7, code: 'LAS-1', name: 'Laser 1', capacity_hours_per_day: 8 }];

const emptyHeatmap = {
  start_date: '2026-01-01',
  end_date: '2026-01-07',
  overload_cells: 0,
  overloaded_work_centers: [],
  work_centers: [],
};

function job(overrides: Record<string, unknown>) {
  return {
    current_operation_name: 'Mill',
    current_operation_number: '10',
    current_operation_sequence: 10,
    part_number: 'PN-XXX',
    part_name: 'Fixture Plate',
    work_center_id: 7,
    status: 'released',
    operation_status: 'ready',
    quantity: 10,
    quantity_complete: 0,
    total_operations: 2,
    operations_complete: 0,
    remaining_hours: 45,
    setup_hours: 1,
    run_hours: 4,
    ...overrides,
  };
}

/**
 * SERVER order: priority -> due date -> WO number. All rows unscheduled, so the
 * Gantt renders no bars and the WO numbers appear only in the Dispatch Queue
 * table. The LAST row (WO-7004, overdue at P5) is the old score sort's top
 * pick — it must stay last. WO-7002 carries a payload work_center_code that
 * DISAGREES with the id-7 lookup ('SAW-2' vs 'LAS-1') to prove the payload
 * code wins; WO-7003 omits it to prove the lookup fallback still works.
 */
const serverOrderedJobs = [
  job({ id: 1, work_order_id: 1, current_operation_id: 101, work_order_number: 'WO-7001', priority: 1, due_date: '2099-06-01', run_order: 1, work_center_code: 'LAS-1' }),
  job({ id: 2, work_order_id: 2, current_operation_id: 102, work_order_number: 'WO-7002', priority: 3, due_date: '2099-06-02', run_order: 2, work_center_code: 'SAW-2' }),
  job({ id: 3, work_order_id: 3, current_operation_id: 103, work_order_number: 'WO-7003', priority: 5, due_date: '2099-07-01', run_order: null, work_center_code: null }),
  // Overdue P5, tiny remaining hours — the old dispatch score's #1. Must stay LAST.
  job({ id: 4, work_order_id: 4, current_operation_id: 104, work_order_number: 'WO-7004', priority: 5, due_date: '2020-01-01', run_order: null, work_center_code: 'LAS-1', remaining_hours: 2 }),
];

/** Exposes the live URL search string so param round-trips are assertable. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

function renderScheduling(url = '/scheduling') {
  return render(
    // MemoryRouter: the work-center filter now lives in a URL search param.
    <MemoryRouter initialEntries={[url]}>
      <ToastProvider>
        <Scheduling />
      </ToastProvider>
      <LocationProbe />
    </MemoryRouter>
  );
}

async function getQueueTable(): Promise<HTMLElement> {
  await screen.findByText('WO-7001');
  const runHeader = screen.getByRole('columnheader', { name: 'Run' });
  const table = runHeader.closest('table');
  if (!table) throw new Error('expected the Dispatch Queue <table> to render');
  return table as HTMLElement;
}

function getWoRow(table: HTMLElement, woNumber: string): HTMLElement {
  const row = within(table).getByText(woNumber).closest('tr');
  if (!row) throw new Error(`expected a row for ${woNumber}`);
  return row as HTMLElement;
}

/** Cell index in the queue table: 0 checkbox, 1 WO #, 2 Run, ... 6 Work Center. */
const RUN_CELL = 2;
const WORK_CENTER_CELL = 6;

describe('Scheduling renders the server run order verbatim', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue(workCenters as never);
    mockedApi.getSchedulableWorkOrders.mockResolvedValue(serverOrderedJobs as never);
    mockedApi.getCapacityHeatmap.mockResolvedValue(emptyHeatmap as never);
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('guards a row-header machine move until saved state is reconciled', async () => {
    jest.useFakeTimers().setSystemTime(new Date('2026-09-07T17:00:00Z'));
    const today = '2026-09-07'; // A fixed Monday must be visible in every browser timezone.
    const scheduledJob = { ...serverOrderedJobs[0], scheduled_start: today, remaining_hours: 1 };
    mockedApi.getWorkCenters.mockResolvedValue([...workCenters, { id: 8, code: 'MILL-2', name: 'Mill 2', capacity_hours_per_day: 8 }] as never);
    mockedApi.getSchedulableWorkOrders.mockResolvedValue([scheduledJob] as never);
    let resolveMove!: () => void;
    mockedApi.updateOperationWorkCenter.mockImplementation(() => new Promise(resolve => { resolveMove = () => resolve({} as never); }));
    renderScheduling();
    const card = await screen.findByRole('button', { name: /WO-7001/ });
    const scheduledCell = card.closest('td') as HTMLTableCellElement;
    expect(scheduledCell).toHaveAttribute('data-capacity-cell', `7-${today}`);
    const dateHeader = within(card.closest('table')!).getAllByRole('columnheader')[scheduledCell.cellIndex];
    expect(within(dateHeader).getByText('Mon')).toBeInTheDocument();
    expect(within(dateHeader).getByText('7')).toBeInTheDocument();
    const target = screen.getByText('Mill 2').closest('td')!;
    const dataTransfer = { setData: jest.fn(), effectAllowed: '', dropEffect: '' };
    fireEvent.dragStart(card, { dataTransfer });
    fireEvent.drop(target, { dataTransfer });
    expect(screen.getByText('Saving changes for WO-7001…')).toBeInTheDocument();
    fireEvent.drop(target, { dataTransfer });
    expect(mockedApi.updateOperationWorkCenter).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateOperationWorkCenter).toHaveBeenCalledWith(101, 8);
    await act(async () => { resolveMove(); });
    await waitFor(() => expect(mockedApi.getSchedulableWorkOrders).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('Saving changes for WO-7001…')).not.toBeInTheDocument();
  });

  it('keeps a named partial scheduling failure available after the machine assignment changes', async () => {
    jest.useFakeTimers().setSystemTime(new Date('2026-09-07T17:00:00Z'));
    const today = '2026-09-07';
    const scheduledJob = { ...serverOrderedJobs[0], scheduled_start: today, remaining_hours: 1 };
    mockedApi.getWorkCenters.mockResolvedValue([...workCenters, { id: 8, code: 'MILL-2', name: 'Mill 2', capacity_hours_per_day: 8 }] as never);
    mockedApi.getSchedulableWorkOrders.mockResolvedValueOnce([scheduledJob] as never)
      .mockResolvedValue([{ ...scheduledJob, work_center_id: 8, work_center_code: 'MILL-2' }] as never);
    mockedApi.updateOperationWorkCenter.mockResolvedValue({} as never);
    mockedApi.scheduleWorkOrder.mockRejectedValue({ response: { data: { detail: 'Date is unavailable' } } });
    const { container } = renderScheduling();
    const card = await screen.findByRole('button', { name: /WO-7001/ });
    expect(card.closest('td')).toHaveAttribute('data-capacity-cell', `7-${today}`);
    const target = container.querySelector(`[data-capacity-cell="8-${today}"]`)!;
    const dataTransfer = { setData: jest.fn(), effectAllowed: '', dropEffect: '' };
    fireEvent.dragStart(card, { dataTransfer });
    fireEvent.drop(target, { dataTransfer });
    const warning = await screen.findByText(/WO-7001: Date is unavailable.*machine move may already have succeeded/);
    expect(warning).toBeInTheDocument();
    await waitFor(() => expect(mockedApi.getSchedulableWorkOrders).toHaveBeenCalledTimes(2));
    const queueTable = screen.getByRole('columnheader', { name: 'Run' }).closest('table')!;
    expect(within(getWoRow(queueTable, 'WO-7001')).getByText('MILL-2')).toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(5000); });
    expect(warning).toBeInTheDocument();
  });

  it('renders the queue rows in payload order — no client dispatch-score re-sort', async () => {
    renderScheduling();
    const table = await getQueueTable();

    const renderedOrder = within(table)
      .getAllByRole('row')
      .map((row) => within(row).queryByText(/^WO-70\d\d$/)?.textContent)
      .filter(Boolean);

    // Verbatim server order. The old score sort led with WO-7004 (overdue P5,
    // ~444 points vs ~195 for WO-7001) — it must stay last.
    expect(renderedOrder).toEqual(['WO-7001', 'WO-7002', 'WO-7003', 'WO-7004']);
  });

  it('shows the shared RUN chip on ranked rows only — the score badge is gone', async () => {
    renderScheduling();
    const table = await getQueueTable();

    const chips = within(table).getAllByTestId('kiosk-run-order-chip');
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveAttribute('aria-label', 'Run order 1');
    expect(chips[1]).toHaveAttribute('aria-label', 'Run order 2');
    expect(within(chips[0].closest('tr') as HTMLElement).getByText('WO-7001')).toBeInTheDocument();
    expect(within(chips[1].closest('tr') as HTMLElement).getByText('WO-7002')).toBeInTheDocument();

    // Unranked rows render NOTHING in the Run column — the old blue dispatch
    // score badge painted a number in every row, so an empty cell here proves
    // no score fallback remains.
    for (const woNumber of ['WO-7003', 'WO-7004']) {
      const runCell = within(getWoRow(table, woNumber)).getAllByRole('cell')[RUN_CELL];
      expect(within(runCell).queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
      expect(runCell).toHaveTextContent(/^$/);
    }

    // The column header flipped Dispatch -> Run with the chip.
    expect(screen.queryByRole('columnheader', { name: 'Dispatch' })).not.toBeInTheDocument();
  });

  it('prefers the payload work_center_code, falling back to the work-center lookup', async () => {
    renderScheduling();
    const table = await getQueueTable();

    // Payload code wins even when the id lookup disagrees...
    const codeCell = within(getWoRow(table, 'WO-7002')).getAllByRole('cell')[WORK_CENTER_CELL];
    expect(codeCell).toHaveTextContent('SAW-2');
    // ...and a null payload code still resolves through the lookup.
    const fallbackCell = within(getWoRow(table, 'WO-7003')).getAllByRole('cell')[WORK_CENTER_CELL];
    expect(fallbackCell).toHaveTextContent('LAS-1');
  });
});

describe('Scheduling — work-center filter round-trips through ?work_center=', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue(workCenters as never);
    mockedApi.getSchedulableWorkOrders.mockResolvedValue(serverOrderedJobs as never);
    mockedApi.getCapacityHeatmap.mockResolvedValue(emptyHeatmap as never);
  });

  it('applies an incoming numeric param, writes changes back, and defaults clean', async () => {
    renderScheduling('/scheduling?work_center=7');
    await getQueueTable();

    const select = screen.getByLabelText('Filter by work center') as HTMLSelectElement;
    expect(select.value).toBe('7');

    // Clearing the filter removes the param — default state keeps a clean URL.
    fireEvent.change(select, { target: { value: '' } });
    expect(screen.getByTestId('location-search').textContent).toBe('');

    // Re-selecting writes it back.
    fireEvent.change(select, { target: { value: '7' } });
    expect(screen.getByTestId('location-search').textContent).toBe('?work_center=7');
  });

  it('falls back to All Work Centers on a non-numeric param', async () => {
    renderScheduling('/scheduling?work_center=bogus');
    await getQueueTable();

    const select = screen.getByLabelText('Filter by work center') as HTMLSelectElement;
    expect(select.value).toBe('');
    // All four rows render — the junk param filtered nothing out.
    expect(screen.getByText('WO-7004')).toBeInTheDocument();
  });
});


describe('Scheduling bulk actions apply the reviewed visible selection', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue(workCenters as never);
    mockedApi.getSchedulableWorkOrders.mockResolvedValue(serverOrderedJobs as never);
    mockedApi.getCapacityHeatmap.mockResolvedValue(emptyHeatmap as never);
    mockedApi.updateWorkOrderPriority.mockResolvedValue(undefined);
  });
  it('Select visible excludes rows hidden by the text query', async () => {
    renderScheduling();
    await getQueueTable();
    fireEvent.change(screen.getByRole('textbox', { name: 'Search WO#, part' }), { target: { value: 'WO-7001' } });
    fireEvent.click(screen.getByRole('button', { name: /Bulk/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Select visible' }));
    expect(screen.getByText('Selected: 1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Apply Priority' }));
    await waitFor(() => expect(mockedApi.updateWorkOrderPriority).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateWorkOrderPriority).toHaveBeenCalledWith(1, 5, undefined);
    expect(await screen.findByText('WO-7001: Updated')).toBeInTheDocument();
  });
  it('retains failures and retries only those work orders after a mixed bulk result', async () => {
    mockedApi.updateWorkOrderPriority.mockRejectedValueOnce({ response: { data: { detail: 'Priority locked' } } });
    renderScheduling();
    await getQueueTable();
    fireEvent.click(screen.getByRole('button', { name: /Bulk/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Select visible' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply Priority' }));
    expect(await screen.findByText('WO-7001: Priority locked')).toBeInTheDocument();
    expect(screen.getByText('Selected: 1')).toBeInTheDocument();
    expect(mockedApi.updateWorkOrderPriority).toHaveBeenCalledTimes(4);
    fireEvent.click(screen.getByRole('button', { name: 'Retry failed jobs' }));
    await waitFor(() => expect(mockedApi.updateWorkOrderPriority).toHaveBeenCalledTimes(5));
    expect(mockedApi.updateWorkOrderPriority.mock.calls[4]).toEqual([1, 5, undefined]);
  });
});

test('backend midnight schedule dates remain the reviewed calendar date in the board, queue and date editor', async () => {
  jest.useFakeTimers().setSystemTime(new Date('2026-09-07T17:00:00Z'));
  jest.clearAllMocks();
  mockedApi.getWorkCenters.mockResolvedValue(workCenters as never);
  mockedApi.getCapacityHeatmap.mockResolvedValue(emptyHeatmap as never);
  mockedApi.getSchedulableWorkOrders.mockResolvedValue([{ ...serverOrderedJobs[0], scheduled_start: '2026-09-09T00:00:00', scheduled_end: '2026-09-11T00:00:00' }] as never);
  try {
    renderScheduling();
    const card = await screen.findByRole('button', { name: /WO-7001/ });
    expect(card.closest('td')).toHaveAttribute('data-capacity-cell', '7-2026-09-09');
    const table = screen.getByRole('columnheader', { name: 'Run' }).closest('table')!;
    const row = getWoRow(table, 'WO-7001');
    expect(within(row).getByText('Sep 9')).toBeInTheDocument();
    fireEvent.click(within(row).getByText('Sep 9'));
    expect(row.querySelector('input[type="date"]')).toHaveValue('2026-09-09');
  } finally { jest.useRealTimers(); }
});
