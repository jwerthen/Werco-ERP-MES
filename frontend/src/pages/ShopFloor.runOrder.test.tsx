/**
 * ShopFloor (/shop-floor "Time Clock") — server run-order rendering.
 *
 * Owner decision: the manager-dictated Dispatch Board run order is the order
 * operators see EVERYWHERE work is listed. The work-center-queue payload
 * arrives already in the canonical dispatch order (run_order NULLS-LAST, then
 * priority/due date/sequence) with the gap-free RUN position on each row, and
 * this page must render it VERBATIM — the old client dispatch-score re-sort
 * (which promoted overdue/high-priority jobs over the manager's ranks) is gone.
 *
 * The payload here is constructed so the OLD score sort would have inverted
 * it: the LAST row is an overdue priority-1 job (top dispatch score), while
 * the manager's ranked rows lead with priority 5 and far-future due dates. If
 * a client re-sort ever creeps back in, these tests fail.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ShopFloor from './ShopFloor';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkOrder: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    updateWorkOrderPriority: jest.fn(),
    createWorkOrderBlocker: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'operator', is_superuser: false },
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

const workCenter = {
  id: 7,
  version: 1,
  code: 'CNC-1',
  name: 'CNC Mill 1',
  work_center_type: 'milling',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function queueItem(overrides: Record<string, unknown>) {
  return {
    part_number: 'PN-XXX',
    part_name: 'Fixture Plate',
    operation_number: '10',
    operation_name: 'Mill',
    status: 'ready',
    quantity_ordered: 10,
    quantity_complete: 0,
    setup_time_hours: 1,
    run_time_hours: 4,
    ...overrides,
  };
}

/**
 * SERVER order (canonical dispatch sort): two ranked rows first, unranked tail
 * after. The tail's LAST row (WO-9006) is overdue at priority 1 — the old
 * dispatch-score sort would have put it FIRST.
 */
const serverOrderedQueue = [
  queueItem({ operation_id: 101, work_order_id: 1001, work_order_number: 'WO-9001', run_order: 1, priority: 5, due_date: '2099-01-05' }),
  queueItem({ operation_id: 102, work_order_id: 1002, work_order_number: 'WO-9002', run_order: 2, priority: 5, due_date: '2099-01-06' }),
  queueItem({ operation_id: 103, work_order_id: 1003, work_order_number: 'WO-9003', run_order: null, priority: 4, due_date: '2099-02-01' }),
  queueItem({ operation_id: 104, work_order_id: 1004, work_order_number: 'WO-9004', run_order: null, priority: 4, due_date: '2099-03-01' }),
  queueItem({ operation_id: 105, work_order_id: 1005, work_order_number: 'WO-9005', run_order: null, priority: 3, due_date: '2099-04-01' }),
  // Overdue P1 — the old score sort's #1 pick. Must stay LAST (server order).
  queueItem({ operation_id: 106, work_order_id: 1006, work_order_number: 'WO-9006', run_order: null, priority: 1, due_date: '2020-01-01' }),
];

function renderShopFloor() {
  return render(
    <MemoryRouter>
      <ShopFloor />
    </MemoryRouter>
  );
}

async function getQueueTable(): Promise<HTMLElement> {
  const woCells = await screen.findAllByText('WO-9001');
  const table = woCells.map((el) => el.closest('table')).find(Boolean);
  if (!table) throw new Error('expected the Job Queue <table> to render');
  return table as HTMLElement;
}

function getUpNextStrip(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'Up Next' });
  const card = heading.closest('.card');
  if (!card) throw new Error('expected the Up Next strip card to render');
  return card as HTMLElement;
}

describe('ShopFloor renders the server run order verbatim', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: serverOrderedQueue });
  });

  it('renders the job-queue rows in payload order — no client dispatch-score re-sort', async () => {
    renderShopFloor();
    const table = await getQueueTable();

    const renderedOrder = within(table)
      .getAllByRole('row')
      .map((row) => within(row).queryByText(/^WO-90\d\d$/)?.textContent)
      .filter(Boolean);

    // Verbatim server order. The old score sort would have led with WO-9006
    // (overdue, P1) — it must stay last.
    expect(renderedOrder).toEqual(['WO-9001', 'WO-9002', 'WO-9003', 'WO-9004', 'WO-9005', 'WO-9006']);
  });

  it('shows the shared RUN chip on ranked rows only, with the server-assigned rank', async () => {
    renderShopFloor();
    const table = await getQueueTable();

    const chips = within(table).getAllByTestId('kiosk-run-order-chip');
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveAttribute('aria-label', 'Run order 1');
    expect(chips[1]).toHaveAttribute('aria-label', 'Run order 2');

    // The chips sit on the ranked rows...
    expect(within(chips[0].closest('tr') as HTMLElement).getByText('WO-9001')).toBeInTheDocument();
    expect(within(chips[1].closest('tr') as HTMLElement).getByText('WO-9002')).toBeInTheDocument();
    // ...and the unranked hot job carries none.
    const hotRow = within(table).getByText('WO-9006').closest('tr') as HTMLElement;
    expect(within(hotRow).queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
  });

  it('the Up Next strip is the first five of the payload order, not a score ranking', async () => {
    renderShopFloor();
    await getQueueTable();

    const strip = getUpNextStrip();
    expect(within(strip).getByText('First 5 in queue order')).toBeInTheDocument();

    const stripOrder = within(strip)
      .getAllByRole('button')
      .map((button) => within(button).queryByText(/^WO-90\d\d$/)?.textContent)
      .filter(Boolean);
    expect(stripOrder).toEqual(['WO-9001', 'WO-9002', 'WO-9003', 'WO-9004', 'WO-9005']);

    // #1 is the manager's rank-1 job; the score sort's darling is not in the strip.
    const first = within(strip).getByText('#1').closest('button') as HTMLElement;
    expect(within(first).getByText('WO-9001')).toBeInTheDocument();
    expect(within(strip).queryByText('WO-9006')).not.toBeInTheDocument();
  });
});
