/**
 * OperatorKiosk — the floor dispatch notice (Dispatch Board v2, SPEC §7).
 *
 * The notice reaches this kiosk on two polls it already makes: the station's
 * queue (`dispatch_notice`) and the operator's my-active-job (`dispatch_notices`).
 * What has to hold on a SHARED operator tablet:
 *  - the acknowledgement is per (operator, work center): operator B badging in
 *    after operator A tapped "Got it" still sees it;
 *  - the strip and the card's MOVED UP chip clear together on "Got it";
 *  - the notice never navigates and never resets the idle-logout timer — a poll
 *    that brings one must not keep an abandoned station signed in;
 *  - an older backend that sends no notice fields renders exactly as before.
 */

import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import type { DispatchNotice, MyActiveJobResponse } from '../types';
import type { KioskWorkCenterQueueResponse } from '../components/kiosk/kioskConstants';

jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: null }) }));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenters: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    completeOperation: jest.fn(),
    reportOperationProduction: jest.fn(),
    reduceOperationProduction: jest.fn(),
    holdOperation: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedUseAuth = useAuth as jest.Mock;

const SERVER_TIME = '2026-11-04T16:41:07.957738Z';

function makeNotice(overrides: Partial<DispatchNotice> = {}): DispatchNotice {
  return {
    id: 90231,
    occurred_at: '2026-11-04T16:34:12.929689Z',
    work_center_id: 7,
    work_center_code: 'DEBUR1',
    change_kind: 'machine_order',
    actor_display: 'Jon W.',
    next_up_before: {
      operation_id: 32,
      work_order_number: 'WO-RANK-0002',
      unit_number: null,
      operation_number: '20',
      operation_name: 'Deburr',
      part_number: 'PN-1',
    },
    next_up_after: {
      operation_id: 31,
      work_order_number: 'WO-RANK-0001',
      unit_number: '7',
      operation_number: '20',
      operation_name: 'Deburr',
      part_number: 'PN-1',
    },
    moved_up: [31],
    updates_in_window: 2,
    ...overrides,
  };
}

const MOVED = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-RANK-0001',
  part_number: 'PN-1',
  part_name: 'Bracket',
  operation_number: '20',
  operation_name: 'Deburr',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  priority: 5,
  due_date: null,
  run_order: 1,
};
const SECOND = { ...MOVED, operation_id: 32, work_order_id: 10, work_order_number: 'WO-RANK-0002', run_order: 2 };

function queuePayload(notice: DispatchNotice | null | undefined): KioskWorkCenterQueueResponse {
  const payload: KioskWorkCenterQueueResponse = { queue: [MOVED, SECOND], server_time: SERVER_TIME };
  if (notice !== undefined) payload.dispatch_notice = notice;
  return payload;
}

function activePayload(notices?: DispatchNotice[]): MyActiveJobResponse {
  const payload: MyActiveJobResponse = { active_jobs: [], active_job: null, server_time: SERVER_TIME };
  if (notices) payload.dispatch_notices = notices;
  return payload;
}

function signedInAs(id: number, firstName: string) {
  return {
    user: {
      id,
      first_name: firstName,
      last_name: 'Vega',
      employee_id: `EMP-${id}`,
      role: 'operator',
      email: `${id}@x.y`,
    },
    isAuthenticated: true,
    isLoading: false,
    loginWithEmployeeId: jest.fn(),
    logout: jest.fn(),
  };
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="kiosk-location">{`${location.pathname}${location.search}`}</span>;
}

const KIOSK_URL = '/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1';

function kioskTree(url = KIOSK_URL) {
  return (
    <MemoryRouter initialEntries={[url]}>
      <OperatorKiosk />
      <LocationProbe />
    </MemoryRouter>
  );
}

function movedCard(): HTMLElement {
  return screen.getByRole('button', { name: /WO-RANK-0001/ });
}

/** True when `later` comes after `earlier` in document order (the strip stacks BELOW it). */
function followsInDocument(earlier: Element, later: Element): boolean {
  return Boolean(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING);
}

describe('OperatorKiosk dispatch notice', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    window.localStorage.clear();
    mockedUseAuth.mockReturnValue(signedInAs(3, 'Rosa'));
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue(activePayload([]));
  });

  it('shows RUN NEXT from the queue payload, flags the moved card, and clears both on "Got it" without navigating', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(makeNotice()));
    render(kioskTree());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('RUN NEXT: WO-RANK-0001 · Unit 7 · Op 20 Deburr — moved up by Jon W. 10:34 AM');
    expect(within(movedCard()).getByTestId('kiosk-moved-up-chip')).toBeInTheDocument();
    // Only the moved card carries the chip.
    expect(
      within(screen.getByRole('button', { name: /WO-RANK-0002/ })).queryByTestId('kiosk-moved-up-chip')
    ).toBeNull();

    const before = screen.getByTestId('kiosk-location').textContent;
    fireEvent.click(screen.getByRole('button', { name: /got it/i }));

    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
    expect(within(movedCard()).queryByTestId('kiosk-moved-up-chip')).not.toBeInTheDocument();
    // Focus lands on the banner's hidden anchor where the strip was, not on <body>.
    expect(document.activeElement).toBe(screen.getByTestId('dispatch-notice-focus-anchor'));
    // Information only: still on the queue, same URL, no clock-in confirm opened.
    expect(screen.getByTestId('kiosk-location').textContent).toBe(before);
    expect(screen.queryByRole('region', { name: /confirm clock in/i })).not.toBeInTheDocument();
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('a second operator on the same shared kiosk still sees the notice the first one acknowledged', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(makeNotice()));
    const view = render(kioskTree());

    fireEvent.click(await screen.findByRole('button', { name: /got it/i }));
    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();

    // Rosa walks away; Ben badges in on the same tablet.
    mockedUseAuth.mockReturnValue(signedInAs(4, 'Ben'));
    view.rerender(kioskTree());

    expect(await screen.findByTestId('dispatch-notice')).toHaveTextContent('RUN NEXT: WO-RANK-0001');
    expect(within(movedCard()).getByTestId('kiosk-moved-up-chip')).toBeInTheDocument();

    // Rosa coming back finds it still acknowledged for her.
    mockedUseAuth.mockReturnValue(signedInAs(3, 'Rosa'));
    view.rerender(kioskTree());
    await screen.findByRole('button', { name: /WO-RANK-0001/ });
    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
  });

  it('dedupes the same notice arriving on both the queue and my-active-job, and adds other clocked-in machines', async () => {
    const here = makeNotice();
    const elsewhere = makeNotice({
      id: 90240,
      work_center_id: 9,
      work_center_code: 'LASER-1',
      next_up_after: makeNotice().next_up_before,
      moved_up: [],
    });
    mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(here));
    mockedApi.getMyActiveJob.mockResolvedValue(activePayload([elsewhere, here]));
    render(kioskTree());

    await screen.findByRole('alert');
    const strips = screen.getAllByTestId('dispatch-notice');
    expect(strips).toHaveLength(2);
    expect(strips[0]).toHaveTextContent('LASER-1');
    expect(strips[0]).toHaveTextContent('Queue order updated by Jon W. 10:34 AM — your next job is unchanged');
    expect(strips[1]).toHaveTextContent('DEBUR1');
    expect(strips[1]).toHaveTextContent('RUN NEXT: WO-RANK-0001');
  });

  it('names the machine on a lone strip for ANOTHER machine the operator is clocked in at', async () => {
    // Open labor on a LASER-1 nest while badged in at DEBUR1; DEBUR1 has nothing
    // new. An untagged "RUN NEXT" would read as DEBUR1's next job.
    const laser = makeNotice({
      id: 90240,
      work_center_id: 9,
      work_center_code: 'LASER-1',
      next_up_after: {
        operation_id: 3142,
        work_order_number: 'WO-20261104-001',
        unit_number: null,
        operation_number: 'Nest 3',
        operation_name: 'Laser Cut',
        part_number: null,
      },
      moved_up: [3142],
    });
    mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(null));
    mockedApi.getMyActiveJob.mockResolvedValue(activePayload([laser]));
    render(kioskTree());

    const strip = await screen.findByTestId('dispatch-notice');
    expect(screen.getAllByTestId('dispatch-notice')).toHaveLength(1);
    expect(strip).toHaveTextContent(/^LASER-1\s*RUN NEXT: WO-20261104-001 · Nest 3 Laser Cut/);
    expect(screen.getByRole('button', { name: /got it.*LASER-1/i })).toBeInTheDocument();
  });

  it("leaves a lone strip for this station's own machine untagged", async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(makeNotice()));
    render(kioskTree());

    const strip = await screen.findByTestId('dispatch-notice');
    expect(strip).toHaveTextContent(/^RUN NEXT: WO-RANK-0001/);
    expect(strip).not.toHaveTextContent('DEBUR1');
  });

  it('renders exactly as before against an older backend that sends no notice fields', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(undefined));
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
    render(kioskTree());

    await screen.findByRole('button', { name: /WO-RANK-0001/ });
    expect(screen.queryByTestId('dispatch-notice-banner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-moved-up-chip')).not.toBeInTheDocument();
  });

  it('treats a notice older than 15 minutes by server_time as already seen on first sighting', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({
      ...queuePayload(makeNotice()),
      server_time: '2026-11-04T17:10:00Z',
    });
    render(kioskTree());

    await screen.findByRole('button', { name: /WO-RANK-0001/ });
    expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-moved-up-chip')).not.toBeInTheDocument();
  });

  it('keeps working when localStorage is blocked', async () => {
    jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    try {
      mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(makeNotice()));
      render(kioskTree());

      fireEvent.click(await screen.findByRole('button', { name: /got it/i }));
      expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: /WO-RANK-0001/ })).toBeInTheDocument();
    } finally {
      jest.restoreAllMocks();
    }
  });

  describe('idle logout', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    });

    it('a poll that brings a notice does NOT reset the idle timer — only a real tap does', async () => {
      // 45s idle timeout → the 30s countdown is up from t=15s, logout at t=45s.
      const auth = signedInAs(3, 'Rosa');
      mockedUseAuth.mockReturnValue(auth);
      mockedApi.getWorkCenterQueue.mockResolvedValueOnce(queuePayload(null));
      mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(makeNotice()));
      render(kioskTree(`${KIOSK_URL}&idle_logout_s=45`));
      await act(async () => {});
      expect(screen.queryByTestId('dispatch-notice')).not.toBeInTheDocument();

      // t=16s: the 15s poll brought the notice; the countdown is already running.
      await act(async () => {
        jest.advanceTimersByTime(16_000);
      });
      expect(screen.getByTestId('dispatch-notice')).toHaveTextContent('RUN NEXT: WO-RANK-0001');
      const countdown = screen.getByTestId('kiosk-idle-countdown');
      expect(countdown).toHaveTextContent(/Logging out in (29|30)s/);
      // Stacked BELOW the idle countdown, never in its place.
      expect(followsInDocument(countdown, screen.getByTestId('dispatch-notice-banner'))).toBe(true);

      // t=31s: another poll (same notice) came and went — the countdown kept falling.
      await act(async () => {
        jest.advanceTimersByTime(15_000);
      });
      expect(screen.getByTestId('kiosk-idle-countdown')).toHaveTextContent(/Logging out in (14|15)s/);
      expect(screen.getByTestId('dispatch-notice')).toBeInTheDocument();

      // t=46s: past the deadline with nobody touching the screen → signed out on time.
      await act(async () => {
        jest.advanceTimersByTime(15_000);
      });
      await act(async () => {});
      expect(auth.logout).toHaveBeenCalled();
    });
  });

  describe('across polls', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    });

    async function nextPoll() {
      await act(async () => {
        jest.advanceTimersByTime(15_000);
      });
    }

    it('"Changed N times" counts the distinct notices THIS tablet saw since "Got it" — never updates_in_window', async () => {
      // The server's 12-hour total says 6 from the very first poll; the tablet has
      // seen exactly one change, so it says nothing about a count yet.
      mockedApi.getWorkCenterQueue.mockResolvedValueOnce(queuePayload(makeNotice({ updates_in_window: 6 })));
      mockedApi.getWorkCenterQueue.mockResolvedValueOnce(queuePayload(makeNotice({ updates_in_window: 6 })));
      mockedApi.getWorkCenterQueue.mockResolvedValue(queuePayload(makeNotice({ id: 90299, updates_in_window: 7 })));
      render(kioskTree());
      await act(async () => {});

      const strip = () => screen.getByTestId('dispatch-notice');
      expect(strip()).toHaveTextContent('RUN NEXT: WO-RANK-0001');
      expect(screen.queryByTestId('dispatch-notice-change-count')).not.toBeInTheDocument();
      expect(strip()).not.toHaveTextContent(/\b6 (times|updates)\b/i);

      // Same notice again: still one change.
      await nextPoll();
      expect(screen.queryByTestId('dispatch-notice-change-count')).not.toBeInTheDocument();

      // A second, newer change lands: two seen here — not the server's 7.
      await nextPoll();
      expect(strip()).toHaveAttribute('data-notice-id', '90299');
      expect(screen.getByTestId('dispatch-notice-change-count')).toHaveTextContent(
        'Changed 2 times since you last checked'
      );
      expect(strip()).not.toHaveTextContent(/\b7 (times|updates)\b/i);
      expect(strip()).not.toHaveTextContent(/updates since you last checked/i);
    });

    it('a failed poll raises OFFLINE above the notice and leaves the notice up (never hidden or replaced)', async () => {
      mockedApi.getWorkCenterQueue.mockResolvedValueOnce(queuePayload(makeNotice()));
      mockedApi.getWorkCenterQueue.mockRejectedValue(new Error('network down'));
      render(kioskTree());
      await act(async () => {});
      expect(screen.getByTestId('dispatch-notice')).toBeInTheDocument();
      expect(screen.queryByText(/actions are disabled until the connection is restored/i)).not.toBeInTheDocument();

      await nextPoll();
      const offline = screen.getByText(/actions are disabled until the connection is restored/i);
      const banner = screen.getByTestId('dispatch-notice-banner');
      expect(within(banner).getByTestId('dispatch-notice')).toHaveTextContent('RUN NEXT: WO-RANK-0001');
      expect(followsInDocument(offline, banner)).toBe(true);
      expect(within(movedCard()).getByTestId('kiosk-moved-up-chip')).toBeInTheDocument();
    });
  });
});
