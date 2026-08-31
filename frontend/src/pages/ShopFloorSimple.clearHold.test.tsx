/**
 * ShopFloorSimple — Clear Hold is its own verb again.
 *
 * THE DEFECT. Commit 6cbdb95 ("Improve mobile shop floor flow") folded Resume
 * into Check In: an ON_HOLD card rendered a button labelled "Check In" (the
 * comment above it still said "Resume Button") wired to `handleCheckIn`, which
 * ran TWO writes inside one try — `api.resumeOperation` then `api.clockIn`. When
 * the clock-in leg was refused (most reachably by the already-clocked-in gate)
 * the resume had ALREADY COMMITTED: the operator saw a red error, the catch
 * skipped the refresh so the card still read "on hold", and the next tap came
 * back "Operation is not on hold" — because it no longer was.
 *
 * Covered here:
 *  - Clear Hold calls `resumeOperation` and NOTHING else, then refreshes;
 *  - a refused clock-in can no longer strand a committed resume, because the two
 *    verbs are no longer one tap;
 *  - the hold REASON is on the card before the button;
 *  - the toast is HONEST: `warning` when the job stayed off the board or a
 *    blocker is still open, `success` only for a clean lift.
 *
 * Note: ShopFloorSimple emits pre-existing act() warnings from its async polling
 * effects; assertions here pin concrete behavior (exact API calls, rendered
 * text), so a real regression fails rather than hiding behind a warning.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';
import { WorkCenter } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    resumeOperation: jest.fn(),
    clockIn: jest.fn(),
    startOperation: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

/** An ON_HOLD operation carrying the hold block the server now sends. */
const HELD_OP = {
  id: 202,
  work_order_id: 42,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_number: '20',
  operation_name: 'Laser Cut',
  description: null,
  work_center_id: 1,
  work_center_name: 'Laser 1',
  status: 'on_hold',
  quantity_ordered: 25,
  quantity_complete: 5,
  quantity_scrapped: 0,
  priority: 3,
  due_date: null,
  customer_name: null,
  customer_po: null,
  actual_start: null,
  setup_instructions: null,
  run_instructions: null,
  requires_inspection: false,
  hold: {
    held_at: '2026-08-11T19:14:00Z',
    held_by_user_id: 12,
    held_by_name: 'Dana R.',
    blocker: {
      id: 55,
      category: 'machine_down',
      severity: 'critical',
      status: 'open',
      title: 'Machine Down: OP20 Laser Cut',
      note: 'spindle bearing failed — do not run',
      has_note: true,
      free_text_withheld: false,
      reported_at: '2026-08-11T19:14:00Z',
      reported_by_user_id: 12,
      reported_by_name: 'Dana R.',
    },
  },
};

const LASER_1: WorkCenter = {
  id: 1,
  version: 1,
  code: 'LASER1',
  name: 'Laser 1',
  work_center_type: 'laser',
  hourly_rate: 95,
  capacity_hours_per_day: 8,
  efficiency_factor: 0.85,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function renderShopFloor() {
  return render(
    <MemoryRouter initialEntries={['/shop-floor/operations']}>
      <Routes>
        <Route path="/shop-floor/operations" element={<ShopFloorSimple />} />
      </Routes>
    </MemoryRouter>
  );
}

/** The grid card's Clear Hold (the mobile "Next Job" strip carries one too). */
function clearHoldButton() {
  return screen.getByTestId('shop-floor-clear-hold-202');
}

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  mockedApi.getWorkCenters.mockResolvedValue([LASER_1]);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [HELD_OP] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  mockedApi.resumeOperation.mockResolvedValue({ status: 'ready', open_blockers: [] });
});

describe('ShopFloorSimple Clear Hold', () => {
  it('labels the held card Clear Hold, not Check In', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    expect(clearHoldButton()).toHaveTextContent(/clear hold/i);
    const card = screen.getByTestId('shop-floor-op-202');
    expect(within(card).queryByRole('button', { name: /check in/i })).not.toBeInTheDocument();
  });

  it('issues exactly ONE write — resume, never a clock-in riding along', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    fireEvent.click(clearHoldButton());

    await waitFor(() => expect(mockedApi.resumeOperation).toHaveBeenCalledWith(202));
    expect(mockedApi.resumeOperation).toHaveBeenCalledTimes(1);
    // The pairing that stranded the operator: a refused clock-in behind an
    // already-committed resume. It cannot happen if the clock-in is not sent.
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
    expect(mockedApi.startOperation).not.toHaveBeenCalled();
  });

  it('refreshes the card after the write (non-optimistic)', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');
    const loadsBefore = mockedApi.getShopFloorOperations.mock.calls.length;

    fireEvent.click(clearHoldButton());

    await waitFor(() =>
      expect(mockedApi.getShopFloorOperations.mock.calls.length).toBeGreaterThan(loadsBefore)
    );
  });

  it('shows WHY it is held on the card, before the button', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    const reason = screen.getByTestId('shop-floor-hold-reason-202');
    expect(within(reason).getByText('Machine down')).toBeInTheDocument();
    expect(within(reason).getByText('Critical')).toBeInTheDocument();
    expect(within(reason).getByText('spindle bearing failed — do not run')).toBeInTheDocument();
    expect(within(reason).getByText(/Held by Dana R\./)).toBeInTheDocument();
  });

  it('reports a clean lift as success', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    fireEvent.click(clearHoldButton());

    expect(await screen.findByText('WO-2026-0042 hold cleared')).toBeInTheDocument();
  });

  it('WARNS, not succeeds, when the resume left the job off the board', async () => {
    mockedApi.resumeOperation.mockResolvedValue({ status: 'pending', open_blockers: [] });
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    fireEvent.click(clearHoldButton());

    const toast = await screen.findByRole('alert');
    expect(toast).toHaveTextContent(/did not return to the queue/i);
    expect(toast).toHaveTextContent(/hold cleared/i);
  });

  it('WARNS and names a blocker the resume did not resolve', async () => {
    mockedApi.resumeOperation.mockResolvedValue({
      status: 'ready',
      open_blockers: [
        {
          id: 55,
          title: 'Machine Down: OP20 Laser Cut',
          category: 'machine_down',
          severity: 'critical',
          status: 'open',
        },
      ],
    });
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    fireEvent.click(clearHoldButton());

    const toast = await screen.findByRole('alert');
    expect(toast).toHaveTextContent('1 blocker still open');
    expect(toast).toHaveTextContent('Machine Down: OP20 Laser Cut');
  });

  it('surfaces a server refusal verbatim and leaves the card held', async () => {
    mockedApi.resumeOperation.mockRejectedValue({
      response: { data: { detail: 'This nest was cancelled; its operation cannot be resumed.' } },
    });
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    fireEvent.click(clearHoldButton());

    expect(
      await screen.findByText('This nest was cancelled; its operation cannot be resumed.')
    ).toBeInTheDocument();
    expect(clearHoldButton()).toBeInTheDocument();
  });

  it('gives the mobile "Next Job" strip the same one-write verb', async () => {
    // priorityFocusQueue includes ON_HOLD work, so the phone-sized strip could
    // otherwise still put the old resume+clock-in double write behind one tap.
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    const stripButton = screen.getByTestId('shop-floor-next-clear-hold-202');
    expect(stripButton).toHaveTextContent(/clear hold/i);
    expect(screen.getByTestId('shop-floor-next-hold-reason-202')).toBeInTheDocument();

    fireEvent.click(stripButton);
    await waitFor(() => expect(mockedApi.resumeOperation).toHaveBeenCalledWith(202));
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('renders no reason panel when the backend predates the hold block', async () => {
    // The SPA and the API deploy independently; the card must degrade to what it
    // always showed rather than rendering an empty amber box.
    const withoutHold: Record<string, unknown> = { ...HELD_OP };
    delete withoutHold.hold;
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [withoutHold] });
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-202');

    expect(screen.queryByTestId('shop-floor-hold-reason-202')).not.toBeInTheDocument();
    expect(clearHoldButton()).toBeInTheDocument();
  });
});
