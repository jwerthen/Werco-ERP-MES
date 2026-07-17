import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import { KioskApiError } from '../services/kioskStationClient';

// Keep KioskApiError REAL (instanceof checks in the page) but mock every call.
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

const ROSTER = [
  { time_entry_id: 501, user_id: 11, operator_name: 'Bob T', employee_id: 'E011', entry_type: 'run', clock_in: '2026-07-02T15:00:00Z' },
  { time_entry_id: 502, user_id: 12, operator_name: 'Charlie M', employee_id: 'E012', entry_type: 'run', clock_in: '2026-07-02T16:22:00Z' },
];

const ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Weldment, frame',
  operation_number: '20',
  operation_name: 'Weld',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 37,
  quantity_scrapped: 2,
  priority: 5,
  due_date: null,
  roster: ROSTER,
};

const QUEUE_RES = {
  queue: [ITEM],
  server_time: new Date().toISOString(),
  station: STATION,
};

const ALICE = { id: 13, full_name: 'Alice W', employee_id: 'E013' };
const ALICE_MINT = { access_token: 'op-token-alice', user: ALICE };
const BOB_MINT = { access_token: 'op-token-bob', user: { id: 11, full_name: 'Bob T', employee_id: 'E011' } };

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

function unlockedStation() {
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
}

/** Type a badge on the window (wedge scanner) and hit Enter. */
function scanBadge(id: string) {
  id.split('').forEach((key) => fireEvent.keyDown(window, { key }));
  fireEvent.keyDown(window, { key: 'Enter' });
}

async function openJobDetail() {
  fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
  await screen.findByRole('region', { name: /job detail/i });
}

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getStationToken.mockReturnValue(null);
  mocked.getStoredStation.mockReturnValue(null);
  mocked.getQueue.mockResolvedValue(QUEUE_RES);
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
});

describe('CrewStationKiosk', () => {
  it('PIN unlock: mints the station token then shows the crew board', async () => {
    mocked.stationLogin.mockResolvedValue({ access_token: 'station-token', station: STATION });
    renderKiosk();

    expect(screen.getByText(/enter station pin/i)).toBeInTheDocument();
    // Queue must not load before the PIN session exists.
    expect(mocked.getQueue).not.toHaveBeenCalled();

    ['1', '2', '3', '4'].forEach((d) => fireEvent.click(screen.getByTestId(`crew-pin-key-${d}`)));
    fireEvent.click(screen.getByRole('button', { name: /unlock/i }));

    await waitFor(() => expect(mocked.stationLogin).toHaveBeenCalledWith(3, '1234'));
    // Board renders with the roster-enriched queue.
    expect(await screen.findByRole('button', { name: /WO-2026-0142/i })).toBeInTheDocument();
    expect(mocked.getQueue).toHaveBeenCalledWith(7);
  });

  it('PIN unlock: shows the server rejection verbatim', async () => {
    mocked.stationLogin.mockRejectedValue(new KioskApiError(401, 'Invalid station or PIN', 'Invalid station or PIN'));
    renderKiosk();

    ['9', '9', '9', '9'].forEach((d) => fireEvent.click(screen.getByTestId(`crew-pin-key-${d}`)));
    fireEvent.click(screen.getByRole('button', { name: /unlock/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid station or PIN');
    expect(screen.getByText(/enter station pin/i)).toBeInTheDocument();
  });

  it('job-first JOIN: badge scan mints, checks elsewhere informationally, clocks in as that operator, then refetches', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.clockIn.mockResolvedValue({});
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
    expect(await screen.findByText(/scan badge to join or leave/i)).toBeInTheDocument();

    const queueCallsBefore = mocked.getQueue.mock.calls.length;
    scanBadge('E013');

    await waitFor(() =>
      expect(mocked.clockIn).toHaveBeenCalledWith('op-token-alice', {
        work_order_id: 9,
        operation_id: 31,
        work_center_id: 7,
        entry_type: 'run',
        source: 'kiosk',
      })
    );
    expect(mocked.mintBadgeToken).toHaveBeenCalledWith('E013');
    // Refetch-after-mutate: the queue is re-read immediately after the clock-in.
    await waitFor(() => expect(mocked.getQueue.mock.calls.length).toBeGreaterThan(queueCallsBefore));
  });

  it('JOIN honors the SETUP entry-type toggle', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.clockIn.mockResolvedValue({});
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
    fireEvent.click(await screen.findByRole('button', { name: /^setup$/i }));
    scanBadge('E013');

    await waitFor(() =>
      expect(mocked.clockIn).toHaveBeenCalledWith('op-token-alice', expect.objectContaining({ entry_type: 'setup' }))
    );
  });

  it('job-first LEAVE: a rostered badge routes to the clock-out quantity screen (0/0 allowed)', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
    mocked.clockOut.mockResolvedValue({});
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
    scanBadge('E011');

    // Bob is on the roster → LEAVE, not JOIN.
    expect(await screen.findByText(/clock out — bob t/i)).toBeInTheDocument();
    expect(mocked.clockIn).not.toHaveBeenCalled();

    // 0/0 is allowed on LEAVE (someone else may report the pieces).
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));
    await waitFor(() =>
      expect(mocked.clockOut).toHaveBeenCalledWith('op-token-bob', 501, {
        quantity_produced: 0,
        quantity_scrapped: 0,
        scrap_reason: undefined,
        source: 'kiosk',
      })
    );
  });

  it('400 "already clocked in" surfaces as an info toast and refreshes the roster', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.clockIn.mockRejectedValue(new KioskApiError(400, 'Already clocked in to this operation', 'Already clocked in to this operation'));
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
    const queueCallsBefore = mocked.getQueue.mock.calls.length;
    scanBadge('E013');

    expect(await screen.findByText('Already clocked in to this operation')).toBeInTheDocument();
    await waitFor(() => expect(mocked.getQueue.mock.calls.length).toBeGreaterThan(queueCallsBefore));
  });

  it('an invalid badge shows the rejection verbatim and stays on the scan screen', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockRejectedValue(new KioskApiError(401, 'Invalid badge', 'Invalid badge'));
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
    scanBadge('E099');

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid badge');
    expect(screen.getByText(/scan badge to join or leave/i)).toBeInTheDocument();
    expect(mocked.clockIn).not.toHaveBeenCalled();
  });

  it('REPORT PRODUCTION: tally banner guards double counting; badge signature saves; failure preserves quantities', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.reportProduction.mockRejectedValueOnce(new KioskApiError(400, 'Operation is on hold', 'Operation is on hold'));
    mocked.reportProduction.mockResolvedValueOnce({});
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /report production/i }));

    // The crew tally banner is the double-count guard.
    expect(await screen.findByTestId('kiosk-tally-banner')).toHaveTextContent(
      'CREW TOTAL SO FAR: 37 of 50 · 2 scrap — enter only NEW pieces'
    );

    fireEvent.click(screen.getByTestId('kiosk-key-3'));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    // Badge-signature screen shows what is about to be saved.
    expect(await screen.findByText(/scan badge to save/i)).toBeInTheDocument();
    expect(screen.getByText(/saving: 3 good/i)).toBeInTheDocument();

    // First attempt: server refuses — verbatim error, quantities preserved.
    scanBadge('E013');
    expect(await screen.findByRole('alert')).toHaveTextContent('Operation is on hold');
    expect(screen.getByText(/saving: 3 good/i)).toBeInTheDocument();

    // Second attempt succeeds and quotes the NEW tally in the toast.
    scanBadge('E013');
    await waitFor(() =>
      expect(mocked.reportProduction).toHaveBeenLastCalledWith('op-token-alice', 31, {
        quantity_complete_delta: 3,
        quantity_scrapped_delta: 0,
        scrap_reason: undefined,
        source: 'kiosk',
      })
    );
    expect(await screen.findByText(/saved by alice w — crew total now 40 of 50 · 2 scrap/i)).toBeInTheDocument();
  });

  it('CORRECT OVER-COUNT: quantity + reason then a badge signature walks back the count', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.reduceProduction.mockResolvedValue({});
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByTestId('crew-correct-verb'));

    // Correction screen: tally reminder + digits-only keypad + required reason.
    expect(await screen.findByTestId('kiosk-correct-tally-banner')).toHaveTextContent(
      'CREW TOTAL SO FAR: 37 of 50 · 2 scrap'
    );
    fireEvent.click(screen.getByTestId('kiosk-correct-key-2'));
    // No reason yet → confirm blocked.
    expect(screen.getByTestId('kiosk-correct-confirm')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Double-counted' }));
    fireEvent.click(screen.getByTestId('kiosk-correct-confirm'));

    // Badge-signature screen shows what is about to be removed.
    expect(await screen.findByRole('heading', { name: /scan badge to correct/i })).toBeInTheDocument();
    expect(screen.getByText(/removing: 2 good/i)).toBeInTheDocument();

    scanBadge('E013');
    await waitFor(() =>
      expect(mocked.reduceProduction).toHaveBeenCalledWith('op-token-alice', 31, {
        quantity_delta: 2,
        reason: 'Double-counted',
        source: 'kiosk',
      })
    );
    expect(mocked.mintBadgeToken).toHaveBeenCalledWith('E013');
    // The success toast quotes the NEW crew tally (37 − 2 = 35).
    expect(await screen.findByText(/alice w removed 2 — crew total now 35 of 50 · 2 scrap/i)).toBeInTheDocument();
    // The additive report is never called on the correction path.
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });

  it('CORRECT OVER-COUNT: a server refusal shows verbatim and preserves the entered correction', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
    const refusal = 'You can only remove up to the 1 piece(s) you recorded on this clock-in; ask a supervisor to correct more.';
    mocked.reduceProduction.mockRejectedValue(new KioskApiError(400, refusal, refusal));
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByTestId('crew-correct-verb'));
    fireEvent.click(screen.getByTestId('kiosk-correct-key-5'));
    fireEvent.click(screen.getByRole('button', { name: 'Wrong job' }));
    fireEvent.click(screen.getByTestId('kiosk-correct-confirm'));

    scanBadge('E011');
    expect(await screen.findByRole('alert')).toHaveTextContent('You can only remove up to the 1 piece(s)');
    // Still on the sign screen with the entered correction preserved for a re-scan.
    expect(screen.getByText(/removing: 5 good/i)).toBeInTheDocument();
  });

  it('COMPLETE: the confirm modal names everyone who will be clocked out, re-derived from queue state', async () => {
    unlockedStation();
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /^complete$/i }));
    // Final-pieces screen prefills the remaining quantity (50 - 37 = 13).
    expect(await screen.findByTestId('kiosk-qty-good')).toHaveTextContent('13');
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/everyone currently clocked in will be clocked out/i)).toBeInTheDocument();
    const list = within(dialog).getByRole('list', { name: /will be clocked out/i });
    expect(within(list).getByText('Bob T')).toBeInTheDocument();
    expect(within(list).getByText('Charlie M')).toBeInTheDocument();
    expect(within(dialog).getByText(/final pieces to record: 13 good/i)).toBeInTheDocument();

    // Cancel is a no-op: no mint, no mutations.
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mocked.mintBadgeToken).not.toHaveBeenCalled();
    expect(mocked.completeOperation).not.toHaveBeenCalled();
    expect(mocked.reportProduction).not.toHaveBeenCalled();
  });

  it('COMPLETE: badge scan inside the modal reports final pieces FIRST, then completes, and names who was clocked out', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.reportProduction.mockResolvedValue({});
    mocked.completeOperation.mockResolvedValue({
      closed_time_entries: [
        { time_entry_id: 501, user_id: 11, operator_name: 'Bob T' },
        { time_entry_id: 502, user_id: 12, operator_name: 'Charlie M' },
      ],
    });
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /^complete$/i }));
    fireEvent.click(await screen.findByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    scanBadge('E013');

    await waitFor(() =>
      expect(mocked.completeOperation).toHaveBeenCalledWith('op-token-alice', 31, {
        quantity_complete: 50,
        source: 'kiosk',
      })
    );
    expect(mocked.reportProduction).toHaveBeenCalledWith('op-token-alice', 31, {
      quantity_complete_delta: 13,
      quantity_scrapped_delta: 0,
      scrap_reason: undefined,
      source: 'kiosk',
    });
    // Production (the operator's final pieces) lands BEFORE the complete-all.
    expect(mocked.reportProduction.mock.invocationCallOrder[0]).toBeLessThan(
      mocked.completeOperation.mock.invocationCallOrder[0]
    );
    expect(
      await screen.findByText(/completed WO-2026-0142 — clocked out Bob T, Charlie M/i)
    ).toBeInTheDocument();
  });

  it('COMPLETE: a 409 (someone completed it first) surfaces verbatim and refreshes', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.completeOperation.mockRejectedValue(
      new KioskApiError(409, 'Operation already completed by Dana R', 'Operation already completed by Dana R')
    );
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /^complete$/i }));
    // Clear the prefilled GOOD so no production report happens (pure complete).
    fireEvent.click(await screen.findByTestId('kiosk-key-clear'));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    const queueCallsBefore = mocked.getQueue.mock.calls.length;
    scanBadge('E013');

    expect(await screen.findByText('Operation already completed by Dana R')).toBeInTheDocument();
    expect(mocked.reportProduction).not.toHaveBeenCalled();
    await waitFor(() => expect(mocked.getQueue.mock.calls.length).toBeGreaterThan(queueCallsBefore));
  });

  it('HOLD requires a reason, then a badge signature, and files the blocker category', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.holdOperation.mockResolvedValue({});
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /^hold$/i }));

    // No badge panel until a reason is chosen.
    expect(screen.queryByText(/scan badge to hold/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Machine down' }));
    expect(await screen.findByText(/scan badge to hold/i)).toBeInTheDocument();

    scanBadge('E013');
    await waitFor(() =>
      expect(mocked.holdOperation).toHaveBeenCalledWith('op-token-alice', 31, {
        category: 'machine_down',
        severity: 'medium',
        source: 'kiosk',
      })
    );
  });

  it('badge-first: scanning at the board opens the operator sheet with open entries and joinable jobs', async () => {
    unlockedStation();
    mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
    mocked.getMyActiveJob.mockResolvedValue({
      active_jobs: [
        {
          time_entry_id: 700,
          operation_id: 99,
          work_order_number: 'WO-2026-0007',
          operation_name: 'Deburr',
          work_center_name: 'Finish',
          clock_in: '2026-07-02T16:00:00Z',
        },
      ],
    });
    renderKiosk();

    await screen.findByRole('button', { name: /WO-2026-0142/i });
    scanBadge('E013');

    expect(await screen.findByRole('region', { name: /your jobs/i })).toBeInTheDocument();
    expect(screen.getByText('Alice W')).toBeInTheDocument();
    // Her open entry elsewhere is listed for clock-out…
    expect(screen.getByText('WO-2026-0007')).toBeInTheDocument();
    // …and this station's job is offered as joinable (she is not on its roster).
    expect(screen.getByRole('button', { name: /WO-2026-0142/i })).toBeInTheDocument();
  });

  describe('polling (10s), stale-poll discard, offline', () => {
    const POLL_MS = 10_000;

    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    });

    it('discards a stale in-flight poll that a mutation superseded', async () => {
      unlockedStation();
      renderKiosk();
      await screen.findByRole('button', { name: /WO-2026-0142/i });

      // Poll leaves at t=10s and hangs (slow network)…
      let resolveStalePoll: (value: typeof QUEUE_RES) => void = () => undefined;
      mocked.getQueue.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveStalePoll = resolve;
          })
      );
      await act(async () => {
        jest.advanceTimersByTime(POLL_MS);
      });

      // …meanwhile a JOIN lands and its refetch returns the NEW roster (Alice on).
      const freshQueue = {
        ...QUEUE_RES,
        queue: [
          {
            ...ITEM,
            roster: [
              ...ROSTER,
              { time_entry_id: 503, user_id: 13, operator_name: 'Alice W', employee_id: 'E013', entry_type: 'run', clock_in: '2026-07-02T17:00:00Z' },
            ],
          },
        ],
      };
      mocked.getQueue.mockResolvedValue(freshQueue);
      mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
      mocked.clockIn.mockResolvedValue({});

      fireEvent.click(screen.getByRole('button', { name: /WO-2026-0142/i }));
      fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
      scanBadge('E013');
      await act(async () => {});
      expect(await screen.findByText('Alice W')).toBeInTheDocument();

      // NOW the stale poll (pre-join snapshot, Alice missing) finally lands —
      // it must be DISCARDED, not allowed to erase her from the roster.
      await act(async () => {
        resolveStalePoll(QUEUE_RES);
      });
      expect(screen.getByText('Alice W')).toBeInTheDocument();
    });

    it('flips to OFFLINE on a failed poll, disables the verbs, and recovers on the next good poll', async () => {
      unlockedStation();
      renderKiosk();
      fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
      await screen.findByRole('region', { name: /job detail/i });
      expect(screen.getByRole('button', { name: /report production/i })).toBeEnabled();

      mocked.getQueue.mockRejectedValueOnce(new Error('network down'));
      await act(async () => {
        jest.advanceTimersByTime(POLL_MS);
      });

      expect(await screen.findByText(/actions are disabled until the connection is restored/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /report production/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /^complete$/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /^hold$/i })).toBeDisabled();
      // Last-known queue data is retained (job detail still on screen).
      expect(screen.getByRole('region', { name: /job detail/i })).toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(POLL_MS);
      });
      await waitFor(() => expect(screen.getByRole('button', { name: /report production/i })).toBeEnabled());
    });

    it('90s idle mid-flow abandons the half-entered flow back to the crew board WITHOUT locking the station', async () => {
      const IDLE_MS = 90_000;
      unlockedStation();
      renderKiosk();
      fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
      await screen.findByRole('region', { name: /job detail/i });
      fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
      expect(await screen.findByText(/scan badge to join or leave/i)).toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(IDLE_MS + 1_000);
      });

      // Idle = flow reset, not logout: back on the board, station stays unlocked.
      expect(screen.queryByText(/scan badge to join or leave/i)).not.toBeInTheDocument();
      expect(screen.queryByRole('region', { name: /job detail/i })).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: /WO-2026-0142/i })).toBeInTheDocument();
      expect(screen.queryByText(/enter station pin/i)).not.toBeInTheDocument();
      expect(mocked.clearStationToken).not.toHaveBeenCalled();
    });

    it('the idle reset never fires while a verb is in flight (busy guard)', async () => {
      const IDLE_MS = 90_000;
      unlockedStation();
      mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
      // The clock-in hangs — the operator's screen must NOT be yanked away
      // underneath the in-flight response, no matter how long it takes.
      mocked.clockIn.mockImplementation(() => new Promise(() => undefined));
      renderKiosk();

      fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
      await screen.findByRole('region', { name: /job detail/i });
      fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
      await screen.findByText(/scan badge to join or leave/i);
      scanBadge('E013');
      await waitFor(() => expect(mocked.clockIn).toHaveBeenCalled());

      await act(async () => {
        jest.advanceTimersByTime(IDLE_MS + 1_000);
      });

      // Still mid-flow: the scan screen survives, no reset to the board, no PIN screen.
      expect(screen.getByRole('region', { name: /join or leave/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /WO-2026-0142.*clocked in/i })).not.toBeInTheDocument();
      expect(screen.queryByText(/enter station pin/i)).not.toBeInTheDocument();
    });

    it('a 401 on the station poll locks back to the PIN screen (never a /login redirect)', async () => {
      unlockedStation();
      renderKiosk();
      await screen.findByRole('button', { name: /WO-2026-0142/i });

      mocked.getQueue.mockRejectedValueOnce(new KioskApiError(401, 'Station revoked', 'Station revoked'));
      await act(async () => {
        jest.advanceTimersByTime(POLL_MS);
      });

      expect(await screen.findByText(/enter station pin/i)).toBeInTheDocument();
      expect(screen.getByRole('alert')).toHaveTextContent(/station session expired or revoked/i);
      expect(mocked.clearStationToken).toHaveBeenCalled();
    });
  });
  describe('scrap reason codes (Lean Phase 1)', () => {
    // Active codes ride the station-authed queue payload (the kiosk's scoped
    // tokens cannot reach /quality/scrap-reason-codes), so the scrap picker is
    // codes-mode whenever the tenant has codes and the legacy SCRAP_REASONS
    // grid otherwise. Server accepts code OR text.
    const SCRAP_CODES = [
      { id: 7, code: 'OT', name: 'Out of tolerance', category: 'operator', display_order: 1 },
      { id: 9, code: 'MAT', name: 'Material defect', category: 'material', display_order: 2 },
    ];

    it('LEAVE with codes on the queue payload: CODE — Name tiles + optional detail, clock-out carries the code id', async () => {
      unlockedStation();
      mocked.getQueue.mockResolvedValue({ ...QUEUE_RES, scrap_reason_codes: SCRAP_CODES });
      mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
      mocked.clockOut.mockResolvedValue({});
      renderKiosk();

      await openJobDetail();
      fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
      scanBadge('E011');
      expect(await screen.findByText(/clock out — bob t/i)).toBeInTheDocument();

      // Enter 2 scrap — the reason grid is built from the company codes and the
      // optional detail line appears; confirm stays blocked until a tile is tapped.
      fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
      fireEvent.click(screen.getByTestId('kiosk-key-2'));
      expect(screen.getByRole('button', { name: 'OT — Out of tolerance' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'MAT — Material defect' })).toBeInTheDocument();
      expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();

      fireEvent.click(screen.getByRole('button', { name: 'OT — Out of tolerance' }));
      fireEvent.change(screen.getByTestId('kiosk-scrap-detail'), { target: { value: 'porosity at weld' } });
      fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

      await waitFor(() =>
        expect(mocked.clockOut).toHaveBeenCalledWith('op-token-bob', 501, {
          quantity_produced: 0,
          quantity_scrapped: 2,
          scrap_reason: 'porosity at weld',
          scrap_reason_code_id: 7,
          source: 'kiosk',
        })
      );
    });

    it('REPORT PRODUCTION threads the code id through the badge-signature step (code-only, no text)', async () => {
      unlockedStation();
      mocked.getQueue.mockResolvedValue({ ...QUEUE_RES, scrap_reason_codes: SCRAP_CODES });
      mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
      mocked.reportProduction.mockResolvedValue({});
      renderKiosk();

      await openJobDetail();
      fireEvent.click(screen.getByRole('button', { name: /report production/i }));
      await screen.findByTestId('kiosk-tally-banner');

      fireEvent.click(screen.getByTestId('kiosk-key-3'));
      fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
      fireEvent.click(screen.getByTestId('kiosk-key-1'));
      fireEvent.click(screen.getByRole('button', { name: 'MAT — Material defect' }));
      fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

      await screen.findByText(/scan badge to save/i);
      scanBadge('E013');

      // Code alone satisfies the scrap-requires-a-reason rule (no typed detail).
      await waitFor(() =>
        expect(mocked.reportProduction).toHaveBeenCalledWith('op-token-alice', 31, {
          quantity_complete_delta: 3,
          quantity_scrapped_delta: 1,
          scrap_reason: undefined,
          scrap_reason_code_id: 9,
          source: 'kiosk',
        })
      );
    });

    it('empty scrap_reason_codes falls back to the legacy grid: text reason, no detail input, no code id', async () => {
      unlockedStation();
      mocked.getQueue.mockResolvedValue({ ...QUEUE_RES, scrap_reason_codes: [] });
      mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
      mocked.clockOut.mockResolvedValue({});
      renderKiosk();

      await openJobDetail();
      fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
      scanBadge('E011');
      expect(await screen.findByText(/clock out — bob t/i)).toBeInTheDocument();

      fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
      fireEvent.click(screen.getByTestId('kiosk-key-2'));

      // Legacy SCRAP_REASONS vocabulary, no optional-detail line.
      expect(screen.getByRole('button', { name: 'Material defect' })).toBeInTheDocument();
      expect(screen.queryByTestId('kiosk-scrap-detail')).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
      fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

      await waitFor(() =>
        expect(mocked.clockOut).toHaveBeenCalledWith('op-token-bob', 501, {
          quantity_produced: 0,
          quantity_scrapped: 2,
          scrap_reason: 'Material defect',
          scrap_reason_code_id: undefined,
          source: 'kiosk',
        })
      );
    });
  });
});
