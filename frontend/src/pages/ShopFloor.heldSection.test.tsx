/**
 * ShopFloor (Time Clock) — the `held` list the page used to throw away.
 *
 * `loadQueue` did `setQueue(response.queue)` and discarded `response.held`,
 * which `GET /shop-floor/work-center-queue/{id}` returns beside it. The result:
 * this page could put an operation ON_HOLD with its own "No Material" button and
 * then had no way to show it again — the row simply left the queue, so a hold
 * looked like the job had disappeared, and the only Clear Hold controls in the
 * whole app were buried in the two kiosks.
 *
 * Covered here:
 *  - held rows render in their OWN section, never in the queue table (the LIST
 *    BOUNDARY is the safety property — heldOperations.ts, rule 1);
 *  - the hold REASON is on screen before the button, and a bare hold still names
 *    who pressed it;
 *  - Clear Hold issues exactly ONE write (resumeOperation) and re-reads;
 *  - the toast/notice is HONEST: `warning` when the job did not come back to the
 *    board or a blocker stayed open, `success` only for a clean lift.
 *
 * Harness mirrors ShopFloor.materialBlockerDialog.test.tsx.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ShopFloor from './ShopFloor';
import type { HeldQueueItem, QueueItem } from '../types';

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
    resumeOperation: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'supervisor', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workCenter = {
  id: 7,
  version: 1,
  code: 'LASER-1',
  name: 'Ermaksan Fiber',
  work_center_type: 'laser_cutting',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const queueItem: QueueItem = {
  operation_id: 101,
  work_order_id: 1001,
  work_order_number: 'WO-9001',
  part_number: 'PN-XXX',
  part_name: 'Fixture Plate',
  operation_number: '10',
  operation_name: 'Mill',
  status: 'ready' as QueueItem['status'],
  quantity_ordered: 10,
  quantity_complete: 0,
  setup_time_hours: 1,
  run_time_hours: 4,
  run_order: 1,
  priority: 5,
  due_date: '2099-01-05',
};

/**
 * A held row as the server builds it: the SAME job-card shape a queued row has
 * (one row builder feeds both), plus `startable: false` and the `hold` block.
 */
const heldItem: HeldQueueItem = {
  operation_id: 202,
  work_order_id: 1002,
  work_order_number: 'WO-9002',
  part_number: 'PN-YYY',
  part_name: 'Bracket',
  operation_number: '20',
  operation_name: 'Laser Cut',
  status: 'on_hold' as HeldQueueItem['status'],
  quantity_ordered: 4,
  quantity_complete: 1,
  setup_time_hours: 0.5,
  run_time_hours: 2,
  run_order: null,
  priority: 3,
  due_date: '2099-02-01',
  startable: false,
  hold: {
    held_at: '2026-08-11T19:14:00Z',
    held_by_user_id: 12,
    held_by_name: 'Dana R.',
    blocker: {
      id: 55,
      category: 'material_missing',
      severity: 'high',
      status: 'open',
      title: 'Material Missing: OP20 Laser Cut',
      note: '4140 plate not at the saw',
      has_note: true,
      free_text_withheld: false,
      reported_at: '2026-08-11T19:14:00Z',
      reported_by_user_id: 12,
      reported_by_name: 'Dana R.',
    },
  },
};

/** The accidental case: no blocker filed at all, but the event named the actor. */
const bareHeldItem: HeldQueueItem = {
  ...heldItem,
  operation_id: 303,
  work_order_id: 1003,
  work_order_number: 'WO-9003',
  hold: {
    held_at: '2026-08-11T19:14:00Z',
    held_by_user_id: 12,
    held_by_name: 'Dana R.',
    blocker: null,
  },
};

function renderShopFloor() {
  return render(
    <MemoryRouter>
      <ShopFloor />
    </MemoryRouter>
  );
}

function heldSection() {
  return screen.getByTestId('shop-floor-held-section');
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [queueItem], held: [heldItem] });
  mockedApi.resumeOperation.mockResolvedValue({ status: 'ready', open_blockers: [] });
});

describe('ShopFloor held section', () => {
  it('renders the served `held` rows instead of discarding them', async () => {
    renderShopFloor();

    const section = await screen.findByTestId('shop-floor-held-section');
    expect(within(section).getByText('WO-9002')).toBeInTheDocument();
    // Matched on the identity line specifically: the hold panel underneath now
    // renders the blocker's own title ("Material Missing: OP20 Laser Cut"), so a
    // bare /Laser Cut/ across the whole section matches two elements.
    expect(within(section).getByText(/^Op 20\s*·\s*Laser Cut$/)).toBeInTheDocument();
    // Count + copy are split across elements by the tabular-nums span.
    expect(section).toHaveTextContent(/1\s*job stopped at this work center/i);
  });

  it('keeps held work OUT of the queue table — the list boundary, not a flag', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    // The queue table offers Start; the held job must not be reachable through it.
    const startButtons = screen.getAllByRole('button', { name: /^start$/i });
    expect(startButtons).toHaveLength(1);

    const table = screen.getByRole('table');
    expect(within(table).queryByText('WO-9002')).not.toBeInTheDocument();
    // ...and the queue count still describes the queue alone (1, not 2).
    expect(screen.getByRole('heading', { name: 'Job Queue' }).parentElement).toHaveTextContent(
      /Ermaksan Fiber\s*•\s*1\s*job$/
    );
  });

  it('shows WHY it is held BEFORE the button — category, severity, note and who held it', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    const reason = screen.getByTestId('shop-floor-hold-reason-202');
    expect(within(reason).getByText('Material missing')).toBeInTheDocument();
    expect(within(reason).getByText('High')).toBeInTheDocument();
    expect(within(reason).getByText('4140 plate not at the saw')).toBeInTheDocument();
    expect(within(reason).getByText(/Held by Dana R\./)).toBeInTheDocument();
  });

  it('still names who held a BARE hold, which files no blocker at all', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [queueItem], held: [bareHeldItem] });
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    const reason = screen.getByTestId('shop-floor-hold-reason-303');
    // Reason and attribution are INDEPENDENT: the accidental case has the second
    // without the first, and rendering it anonymous would be the worst outcome.
    expect(within(reason).getByText('No reason given')).toBeInTheDocument();
    expect(within(reason).getByText(/Held by Dana R\./)).toBeInTheDocument();
  });

  it('says so when the server capped the held list', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({
      queue: [queueItem],
      held: [heldItem],
      held_truncated: true,
    });
    renderShopFloor();

    expect(await screen.findByTestId('shop-floor-held-truncated')).toHaveTextContent(/most recent holds only/i);
  });

  it('renders no section at all when nothing is held', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [queueItem], held: [] });
    renderShopFloor();
    await screen.findByRole('table');

    expect(screen.queryByTestId('shop-floor-held-section')).not.toBeInTheDocument();
  });
});

describe('ShopFloor Clear Hold', () => {
  it('issues exactly ONE write and re-reads the queue', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    fireEvent.click(within(heldSection()).getByRole('button', { name: /clear hold on WO-9002/i }));

    await waitFor(() => expect(mockedApi.resumeOperation).toHaveBeenCalledWith(202));
    expect(mockedApi.resumeOperation).toHaveBeenCalledTimes(1);
    // No clock-in rides along — that pairing is what stranded ShopFloorSimple.
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
    // Non-optimistic: initial load + post-write re-read.
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledTimes(2));
  });

  it('reports a clean lift as success', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    fireEvent.click(within(heldSection()).getByRole('button', { name: /clear hold on WO-9002/i }));

    const notice = await screen.findByRole('status');
    expect(notice).toHaveTextContent('WO-9002 hold cleared');
  });

  it('WARNS, not succeeds, when the resume left the job off the board', async () => {
    mockedApi.resumeOperation.mockResolvedValue({ status: 'pending', open_blockers: [] });
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    fireEvent.click(within(heldSection()).getByRole('button', { name: /clear hold on WO-9002/i }));

    // role="alert", like an error: the operator has to act on the shortfall.
    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent(/did not return to the queue/i);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('WARNS and names a blocker the resume did not resolve', async () => {
    mockedApi.resumeOperation.mockResolvedValue({
      status: 'ready',
      open_blockers: [
        {
          id: 55,
          title: 'Material Missing: OP20 Laser Cut',
          category: 'material_missing',
          severity: 'high',
          status: 'open',
        },
      ],
    });
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    fireEvent.click(within(heldSection()).getByRole('button', { name: /clear hold on WO-9002/i }));

    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent('1 blocker still open');
    expect(notice).toHaveTextContent('Material Missing: OP20 Laser Cut');
  });

  it('surfaces the server refusal verbatim and leaves the row held', async () => {
    mockedApi.resumeOperation.mockRejectedValue({
      response: { data: { detail: 'This nest was cancelled; its operation cannot be resumed.' } },
    });
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    fireEvent.click(within(heldSection()).getByRole('button', { name: /clear hold on WO-9002/i }));

    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent('This nest was cancelled; its operation cannot be resumed.');
    // The refusal did not move the card, and no re-read was claimed.
    expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledTimes(1);
    expect(within(heldSection()).getByText('WO-9002')).toBeInTheDocument();
  });

  it('discloses that the blocker stays open BEFORE the tap', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-held-section');

    expect(
      within(heldSection()).getByText(/Any blocker stays open for a supervisor to resolve/i)
    ).toBeInTheDocument();
  });
});
