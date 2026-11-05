/**
 * CrewStationKiosk — the floor dispatch notice (Dispatch Board v2, SPEC §7).
 *
 * The crew station reads its machine's newest notice off the station-token
 * queue poll. It is one shared terminal, so the acknowledgement is per DEVICE
 * per work center, and the strip has to be on the crew board the 90-second idle
 * reset returns to — it is not a view (a view carries an operationId the
 * ghost-guard acts on).
 */

import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import type { DispatchNotice } from '../types';
import type { KioskCrewQueueResponse } from '../types/kioskStation';

jest.mock('../services/kioskStationClient', () => {
  const actual = jest.requireActual('../services/kioskStationClient');
  return {
    __esModule: true,
    ...actual,
    getStationToken: jest.fn(),
    setStationToken: jest.fn(),
    clearStationToken: jest.fn(),
    getStoredStation: jest.fn(),
    stationLogin: jest.fn(),
    getQueue: jest.fn(),
    mintBadgeToken: jest.fn(),
    getMyActiveJob: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    reportProduction: jest.fn(),
    reduceProduction: jest.fn(),
    completeOperation: jest.fn(),
    holdOperation: jest.fn(),
  };
});

const mocked = kioskClient as jest.Mocked<typeof kioskClient>;

const STATION = {
  id: 3,
  label: 'Weld Bay Kiosk',
  work_center_id: 7,
  work_center_code: 'WELD1',
  work_center_name: 'Weld Bay 1',
};

const MOVED = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-RANK-0001',
  unit_number: '12',
  part_number: 'PN-1',
  part_name: 'Weldment, frame',
  operation_number: '20',
  operation_name: 'Weld',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  quantity_scrapped: 0,
  priority: 5,
  due_date: null,
  roster: [],
  run_order: 1,
};
const SECOND = {
  ...MOVED,
  operation_id: 32,
  work_order_id: 10,
  work_order_number: 'WO-RANK-0002',
  unit_number: null,
  run_order: 2,
};

const SERVER_TIME = '2026-11-04T16:41:07.957738Z';

function makeNotice(overrides: Partial<DispatchNotice> = {}): DispatchNotice {
  return {
    id: 90231,
    occurred_at: '2026-11-04T16:34:12.929689Z',
    work_center_id: 7,
    work_center_code: 'WELD1',
    change_kind: 'work_order_order',
    actor_display: 'Jon W.',
    next_up_before: {
      operation_id: 32,
      work_order_number: 'WO-RANK-0002',
      unit_number: null,
      operation_number: '20',
      operation_name: 'Weld',
      part_number: 'PN-1',
    },
    next_up_after: {
      operation_id: 31,
      work_order_number: 'WO-RANK-0001',
      unit_number: '12',
      operation_number: '20',
      operation_name: 'Weld',
      part_number: 'PN-1',
    },
    moved_up: [31],
    updates_in_window: 1,
    ...overrides,
  };
}

function queuePayload(notice?: DispatchNotice | null): KioskCrewQueueResponse {
  const payload: KioskCrewQueueResponse = { queue: [MOVED, SECOND], server_time: SERVER_TIME, station: STATION };
  if (notice !== undefined) payload.dispatch_notice = notice;
  return payload;
}

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

function movedCard(): HTMLElement {
  return screen.getByRole('button', { name: /WO-RANK-0001/ });
}

describe('CrewStationKiosk dispatch notice', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    window.localStorage.clear();
    mocked.getStationToken.mockReturnValue('station-token');
    mocked.getStoredStation.mockReturnValue(STATION);
    mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  });

  it('shows RUN NEXT on the crew board with a MOVED UP chip, and "Got it" clears both', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload(makeNotice()));
    renderKiosk();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('RUN NEXT: WO-RANK-0001 · Unit 12 · Op 20 Weld — moved up by Jon W. 10:34 AM');
    expect(within(movedCard()).getByTestId('kiosk-moved-up-chip')).toBeInTheDocument();
    expect(
      within(screen.getByRole('button', { name: /WO-RANK-0002/ })).queryByTestId('kiosk-moved-up-chip')
    ).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /got it/i }));
    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
    expect(within(movedCard()).queryByTestId('kiosk-moved-up-chip')).not.toBeInTheDocument();
    // Still on the crew board — acknowledging opened no job and locked nothing.
    expect(screen.queryByRole('region', { name: /job detail/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/enter station pin/i)).not.toBeInTheDocument();
  });

  it('acknowledges per DEVICE: the station stays acknowledged across a reload', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload(makeNotice()));
    const first = renderKiosk();
    fireEvent.click(await screen.findByRole('button', { name: /got it/i }));
    first.unmount();

    renderKiosk();
    await screen.findByRole('button', { name: /WO-RANK-0001/ });
    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
  });

  it('renders the calm strip when the next job did not change', async () => {
    mocked.getQueue.mockResolvedValue(
      queuePayload(makeNotice({ next_up_after: makeNotice().next_up_before, moved_up: [] }))
    );
    renderKiosk();

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Queue order updated by Jon W. 10:34 AM — your next job is unchanged'
    );
    expect(screen.queryByTestId('kiosk-moved-up-chip')).not.toBeInTheDocument();
  });

  it('renders exactly as before against an older backend that sends no notice field', async () => {
    mocked.getQueue.mockResolvedValue(queuePayload());
    renderKiosk();
    await screen.findByRole('button', { name: /WO-RANK-0001/ });
    expect(screen.queryByTestId('dispatch-notice-banner')).not.toBeInTheDocument();
  });

  describe('idle flow reset', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    });

    it('is still up on the board the 90s idle reset returns to (a poll mid-flow does not end the flow early)', async () => {
      mocked.getQueue.mockResolvedValueOnce(queuePayload(null));
      mocked.getQueue.mockResolvedValue(queuePayload(makeNotice()));
      renderKiosk();
      await act(async () => {});

      fireEvent.click(movedCard());
      expect(await screen.findByRole('region', { name: /job detail/i })).toBeInTheDocument();

      // The 10s poll brings the notice while the job screen is open. It shows,
      // and the flow stays where the operator left it.
      await act(async () => {
        jest.advanceTimersByTime(10_000);
      });
      expect(screen.getByTestId('dispatch-notice')).toHaveTextContent('RUN NEXT: WO-RANK-0001');
      expect(screen.getByRole('region', { name: /job detail/i })).toBeInTheDocument();

      // Nobody touches it: the idle reset (90s from the last real tap) lands on
      // the board — on time — and the notice is there.
      await act(async () => {
        jest.advanceTimersByTime(81_000);
      });
      expect(screen.queryByRole('region', { name: /job detail/i })).not.toBeInTheDocument();
      expect(screen.getByTestId('dispatch-notice')).toHaveTextContent('RUN NEXT: WO-RANK-0001');
      expect(within(movedCard()).getByTestId('kiosk-moved-up-chip')).toBeInTheDocument();
    });

    it('a failed poll raises OFFLINE above the notice and leaves the notice up', async () => {
      mocked.getQueue.mockResolvedValueOnce(queuePayload(makeNotice()));
      mocked.getQueue.mockRejectedValue(new Error('network down'));
      renderKiosk();
      await act(async () => {});
      expect(screen.getByTestId('dispatch-notice')).toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(10_000);
      });
      const offline = screen.getByText(/actions are disabled until the connection is restored/i);
      const banner = screen.getByTestId('dispatch-notice-banner');
      expect(within(banner).getByTestId('dispatch-notice')).toHaveTextContent('RUN NEXT: WO-RANK-0001');
      // Stacked BELOW the offline strip, never in its place.
      expect(offline.compareDocumentPosition(banner) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });
  });
});
