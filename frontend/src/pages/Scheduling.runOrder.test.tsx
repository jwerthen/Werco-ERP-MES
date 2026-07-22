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
import { render, screen, within } from '@testing-library/react';
import api from '../services/api';
import Scheduling from './Scheduling';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getSchedulableWorkOrders: jest.fn(),
    getCapacityHeatmap: jest.fn(),
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

function renderScheduling() {
  return render(
    <ToastProvider>
      <Scheduling />
    </ToastProvider>
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
