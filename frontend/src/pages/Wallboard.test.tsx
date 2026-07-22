/**
 * Wallboard — the full-screen Foundry TV board (design handoff 2026-07-22).
 *
 * Baseline coverage: HUD chip counts from the true totals; the DOWN card's
 * client-side work_centers join (downtime duration + reason) and the BLOCKED
 * card's blocked_wos join; WAITING "IN QUEUE"; the overflow-strip arithmetic
 * against the uncapped jobs_total; SHIP fraction + rows; LATE rows + "+N
 * MORE"; the TODAY KPI values; the steady SYNC OK → STALE → LOST escalation
 * that keeps the last good data; the ?dept= scope line; the degraded states
 * (jobs empty / jobs missing); and the no-token / revoked screens.
 *
 * services/wallboardClient is mocked at the module boundary — the page must
 * never touch the global axios client (a display token cannot enter it).
 */

import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import Wallboard from './Wallboard';
import {
  captureWallboardTokenFromUrl,
  clearWallboardToken,
  fetchWallboard,
  getWallboardToken,
} from '../services/wallboardClient';
import type { WallboardJob, WallboardResponse } from '../types/wallboard';

jest.mock('../services/wallboardClient', () => ({
  __esModule: true,
  captureWallboardTokenFromUrl: jest.fn(),
  clearWallboardToken: jest.fn(),
  getWallboardToken: jest.fn(() => 'display-jwt'),
  fetchWallboard: jest.fn(),
}));

const mockFetchWallboard = fetchWallboard as jest.MockedFunction<typeof fetchWallboard>;
const mockGetToken = getWallboardToken as jest.MockedFunction<typeof getWallboardToken>;
const mockClearToken = clearWallboardToken as jest.MockedFunction<typeof clearWallboardToken>;
const mockCapture = captureWallboardTokenFromUrl as jest.MockedFunction<typeof captureWallboardTokenFromUrl>;

/** Grid block in SERVER severity order — the client never re-sorts. */
const jobs: WallboardJob[] = [
  {
    // DOWN — stoppage detail joins to work_centers[0] by wc code.
    wo_number: 'WO-1042',
    part_number: '88231-REV-C',
    status: 'in_progress',
    qty_complete: 120,
    qty_ordered: 400,
    is_late: false,
    days_late: 0,
    blocked: false,
    down: true,
    running: false,
    ops_completed: 2,
    ops_total: 6,
    current_op: {
      sequence: 30,
      name: 'CNC Mill',
      work_center_code: 'MILL-1',
      work_center_name: 'HAAS VF-4',
      status: 'ready',
      elapsed_minutes: 0,
    },
  },
  {
    // BLOCKED — age + reason join to blocked_wos by WO number.
    wo_number: 'WO-0991',
    part_number: '4471-002',
    status: 'in_progress',
    qty_complete: 340,
    qty_ordered: 500,
    is_late: false,
    days_late: 0,
    blocked: true,
    down: false,
    running: false,
    ops_completed: 3,
    ops_total: 7,
    current_op: {
      sequence: 40,
      name: 'Deburr',
      work_center_code: 'DEB-1',
      work_center_name: 'Deburr Bench 1',
      status: 'ready',
      elapsed_minutes: 0,
    },
  },
  {
    // LATE + running — chip carries the days, elapsed renders muted.
    wo_number: 'WO-0885',
    part_number: 'PLT-2093',
    status: 'in_progress',
    qty_complete: 80,
    qty_ordered: 600,
    is_late: true,
    days_late: 14,
    blocked: false,
    down: false,
    running: true,
    ops_completed: 0,
    ops_total: 4,
    current_op: {
      sequence: 10,
      name: 'Laser Cut',
      work_center_code: 'LASER-1',
      work_center_name: 'Trumpf 3030',
      status: 'in_progress',
      elapsed_minutes: 137,
    },
  },
  {
    // RUNNING — green elapsed.
    wo_number: 'WO-1131',
    part_number: 'SHFT-9902',
    status: 'in_progress',
    qty_complete: 90,
    qty_ordered: 250,
    is_late: false,
    days_late: 0,
    blocked: false,
    down: false,
    running: true,
    ops_completed: 1,
    ops_total: 4,
    current_op: {
      sequence: 20,
      name: 'CNC Turn',
      work_center_code: 'TURN-1',
      work_center_name: 'Mazak QT-250',
      status: 'in_progress',
      elapsed_minutes: 24,
    },
  },
  {
    // WAITING — de-emphasized, IN QUEUE stop reason.
    wo_number: 'WO-1155',
    part_number: 'BUSH-1120',
    status: 'released',
    qty_complete: 0,
    qty_ordered: 500,
    is_late: false,
    days_late: 0,
    blocked: false,
    down: false,
    running: false,
    ops_completed: 1,
    ops_total: 4,
    current_op: {
      sequence: 20,
      name: 'Deburr',
      work_center_code: 'DEB-2',
      work_center_name: 'Deburr Bench 2',
      status: 'pending',
      elapsed_minutes: 0,
    },
  },
];

const payload: WallboardResponse = {
  work_centers: [
    {
      id: 1,
      code: 'MILL-1',
      name: 'Haas VF-4',
      status: 'in_use',
      active_jobs: [],
      queued_count: 0,
      blocked_count: 0,
      // 134 min → "2H14M" on the DOWN card and the BLOCKED/DOWN rail.
      down: { category: 'maintenance', since: '2026-07-22T12:00:00Z', minutes: 134 },
    },
    {
      id: 2,
      code: 'LASER-1',
      name: 'Trumpf 3030',
      status: 'in_use',
      active_jobs: [],
      queued_count: 2,
      blocked_count: 0,
      down: null,
    },
  ],
  late_wos: [
    { wo_number: 'WO-0885', part_number: 'PLT-2093', due_date: '2026-07-08', days_late: 14, status: 'in_progress' },
    { wo_number: 'WO-0850', part_number: 'HSG-2201', due_date: '2026-07-11', days_late: 11, status: 'in_progress' },
  ],
  blocked_wos: [{ wo_number: 'WO-0991', category: 'waiting_inspect', age_hours: 22 }],
  late_total: 7,
  blocked_total: 3,
  down_total: 1,
  ship: {
    due_today: 8,
    shipped_today: 5,
    due_this_week: 18,
    due_today_rows: [
      { wo_number: 'WO-1141', part_number: 'SHFT-9902', promise_date: '2026-07-22', qty_remaining: 4 },
      { wo_number: 'WO-1149', part_number: 'CVR-5567', promise_date: '2026-07-22', qty_remaining: 2 },
    ],
    next_due_date: null,
    next_due_count: 0,
  },
  today: {
    ops_completed: 47,
    pieces_completed: 1284,
    wos_completed: 6,
    operators_on_clock: 12,
    hours_logged: 86.5,
    receipts: 9,
    scrap_events: 3,
  },
  quality: { open_ncr_count: 4, newest_ncr_age_days: 2, wos_on_hold: 3 },
  jobs,
  jobs_total: 17,
  generated_at: '2026-07-22T13:00:00Z',
};

function renderWallboard(initialEntry = '/wallboard') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/wallboard" element={<Wallboard />} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetToken.mockReturnValue('display-jwt');
  window.localStorage.clear();
});

describe('Wallboard', () => {
  it('renders the HUD chips, the card joins, the rail, and the TODAY bar from one payload', async () => {
    mockFetchWallboard.mockResolvedValue(payload);
    renderWallboard();

    expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
    expect(mockCapture).toHaveBeenCalled();

    // HUD alert chips carry the TRUE totals.
    expect(screen.getByTestId('hud-chip-down')).toHaveTextContent('1');
    expect(screen.getByTestId('hud-chip-down')).toHaveTextContent('DOWN');
    expect(screen.getByTestId('hud-chip-blocked')).toHaveTextContent('3');
    expect(screen.getByTestId('hud-chip-blocked')).toHaveTextContent('BLOCKED');
    expect(screen.getByTestId('hud-chip-late')).toHaveTextContent('7');
    expect(screen.getByTestId('hud-chip-late')).toHaveTextContent('LATE');
    expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '0');
    expect(screen.getByTestId('sync-status')).toHaveTextContent('SYNC OK');

    // Server order preserved — the client never re-sorts.
    const cardIds = screen.getAllByTestId(/^wo-card-/).map(el => el.getAttribute('data-testid'));
    expect(cardIds).toEqual([
      'wo-card-WO-1042',
      'wo-card-WO-0991',
      'wo-card-WO-0885',
      'wo-card-WO-1131',
      'wo-card-WO-1155',
    ]);

    // DOWN card: duration + reason come from the work_centers join.
    const downCard = within(screen.getByTestId('wo-card-WO-1042'));
    expect(downCard.getByText('DOWN')).toBeInTheDocument();
    expect(downCard.getByText('2H14M')).toBeInTheDocument();
    expect(downCard.getByText('MAINTENANCE')).toBeInTheDocument();
    expect(downCard.getByText('88231-REV-C')).toBeInTheDocument();
    expect(downCard.getByText('OP 3/6 · CNC MILL')).toBeInTheDocument();
    expect(downCard.getByText('HAAS VF-4')).toBeInTheDocument();
    expect(downCard.getByText('30%')).toBeInTheDocument();

    // BLOCKED card: age + reason come from the blocked_wos join.
    const blockedCard = within(screen.getByTestId('wo-card-WO-0991'));
    expect(blockedCard.getByText('BLOCKED')).toBeInTheDocument();
    expect(blockedCard.getByText('22H')).toBeInTheDocument();
    expect(blockedCard.getByText('WAITING INSPECT')).toBeInTheDocument();

    // LATE chip carries days late; the running op's elapsed still renders.
    const lateCard = within(screen.getByTestId('wo-card-WO-0885'));
    expect(lateCard.getByText('LATE 14D')).toBeInTheDocument();
    expect(lateCard.getByText('2H17M')).toBeInTheDocument();

    // RUNNING elapsed; WAITING is IN QUEUE with no time value.
    expect(within(screen.getByTestId('wo-card-WO-1131')).getByText('24M')).toBeInTheDocument();
    const waitingCard = within(screen.getByTestId('wo-card-WO-1155'));
    expect(waitingCard.getByText('WAITING')).toBeInTheDocument();
    expect(waitingCard.getByText('IN QUEUE')).toBeInTheDocument();

    // Overflow strip: 17 total − 5 rendered = +12.
    expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent('+12 MORE WORK ORDERS IN QUEUE');

    // SHIP TODAY: fraction, rows with qty remaining, +N MORE (8−5−2=1), week.
    const ship = within(screen.getByTestId('ship-panel'));
    expect(screen.getByTestId('ship-panel')).toHaveTextContent('5/8');
    expect(ship.getByText('4 LEFT')).toBeInTheDocument();
    expect(ship.getByText('2 LEFT')).toBeInTheDocument();
    expect(ship.getByText('+1 MORE TODAY')).toBeInTheDocument();
    expect(ship.getByText('THIS WEEK')).toBeInTheDocument();
    expect(ship.getByText('18')).toBeInTheDocument();

    // LATE — OLDEST FIRST: total, day columns, +N MORE (7−2=5).
    expect(screen.getByTestId('late-total')).toHaveTextContent('7');
    const late = within(screen.getByTestId('late-panel'));
    expect(late.getByText('14D')).toBeInTheDocument();
    expect(late.getByText('WO-0885')).toBeInTheDocument();
    expect(late.getByText('PLT-2093')).toBeInTheDocument();
    expect(late.getByText('+5 MORE')).toBeInTheDocument();

    // BLOCKED / DOWN: split totals, down row first, +N MORE (3+1−2=2).
    expect(screen.getByTestId('blocked-total')).toHaveTextContent('3');
    expect(screen.getByTestId('down-total')).toHaveTextContent('1');
    const blockedDown = within(screen.getByTestId('blocked-down-panel'));
    expect(blockedDown.getByText('2H14M')).toBeInTheDocument();
    // Machine identity is name-first (matching the card's machine row).
    expect(blockedDown.getByText('Haas VF-4')).toBeInTheDocument();
    expect(blockedDown.getByText('MAINTENANCE')).toBeInTheDocument();
    expect(blockedDown.getByText('22H')).toBeInTheDocument();
    expect(blockedDown.getByText('WAITING INSPECT')).toBeInTheDocument();
    expect(blockedDown.getByText('+2 MORE')).toBeInTheDocument();

    // NCRs / holds split row.
    const quality = within(screen.getByTestId('quality-row'));
    expect(quality.getByText('OPEN NCRS')).toBeInTheDocument();
    expect(quality.getByText('NEWEST 2D AGO')).toBeInTheDocument();
    expect(quality.getByText('4')).toBeInTheDocument();
    expect(quality.getByText('ON HOLD')).toBeInTheDocument();

    // TODAY KPI bar values.
    const band = within(screen.getByTestId('today-kpis'));
    expect(band.getByText('47')).toBeInTheDocument();
    expect(band.getByText('1284')).toBeInTheDocument();
    expect(band.getByText('12')).toBeInTheDocument();
    expect(band.getByText('86.5')).toBeInTheDocument();
    expect(band.getByText('9')).toBeInTheDocument();
    expect(band.getByText('3')).toBeInTheDocument();

    // Nothing scrolls, ever.
    expect(document.querySelector('[class*="overflow-y-auto"]')).toBeNull();
    expect(document.querySelector('[class*="overflow-x-auto"]')).toBeNull();
  });

  it('says ALL OPEN WORK ORDERS ON BOARD when nothing overflows', async () => {
    mockFetchWallboard.mockResolvedValue({ ...payload, jobs_total: 5 });
    renderWallboard();

    expect(await screen.findByTestId('wo-overflow-strip')).toHaveTextContent('ALL OPEN WORK ORDERS ON BOARD');
  });

  it('renders the empty state and no strip when there are no open work orders', async () => {
    mockFetchWallboard.mockResolvedValue({ ...payload, jobs: [], jobs_total: 0 });
    renderWallboard();

    expect(await screen.findByText('NO OPEN WORK ORDERS')).toBeInTheDocument();
    expect(screen.queryByTestId('wo-grid')).not.toBeInTheDocument();
    expect(screen.queryByTestId('wo-overflow-strip')).not.toBeInTheDocument();
    // The rail + TODAY bar keep rendering around the empty grid zone.
    expect(screen.getByTestId('ship-panel')).toBeInTheDocument();
    expect(screen.getByTestId('today-kpis')).toBeInTheDocument();
  });

  it('degrades to BOARD DATA UNAVAILABLE when the payload has no jobs block', async () => {
    mockFetchWallboard.mockResolvedValue({ ...payload, jobs: undefined, jobs_total: undefined });
    renderWallboard();

    expect(await screen.findByText('BOARD DATA UNAVAILABLE — BACKEND UPDATE REQUIRED')).toBeInTheDocument();
    expect(screen.queryByTestId('wo-grid')).not.toBeInTheDocument();
    expect(screen.queryByTestId('wo-overflow-strip')).not.toBeInTheDocument();
  });

  it('escalates the steady sync chip STALE → LOST, keeps the last good data, and resets on recovery', async () => {
    jest.useFakeTimers();
    try {
      mockFetchWallboard.mockResolvedValueOnce(payload);
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '0');

      // 1st failed poll → SYNC STALE; the last good board stays on screen.
      mockFetchWallboard.mockRejectedValue(new Error('HTTP_500'));
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '1');
      expect(screen.getByTestId('sync-status')).toHaveTextContent('SYNC STALE');
      expect(screen.getByTestId('wo-card-WO-1042')).toBeInTheDocument();

      // 4th consecutive failure (~2 min) → SYNC LOST.
      await act(async () => {
        jest.advanceTimersByTime(90_000);
      });
      expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '2');
      expect(screen.getByTestId('sync-status')).toHaveTextContent('SYNC LOST');
      expect(screen.getByTestId('wo-card-WO-1042')).toBeInTheDocument();

      // Recovery: one good poll resets to SYNC OK and clears the count.
      mockFetchWallboard.mockResolvedValue(payload);
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '0');

      // …so the next single failure starts over at STALE, not LOST.
      mockFetchWallboard.mockRejectedValue(new Error('HTTP_500'));
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '1');
    } finally {
      jest.useRealTimers();
    }
  });

  it('shows the revoked screen, clears the token, and stops polling on UNAUTHORIZED', async () => {
    jest.useFakeTimers();
    try {
      mockFetchWallboard.mockResolvedValueOnce(payload);
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();

      // Next poll: the server rejects the (revoked/expired) display token.
      mockFetchWallboard.mockRejectedValue(new Error('UNAUTHORIZED'));
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });

      // Distinct full-screen state — NOT the sync chip over stale data.
      expect(await screen.findByTestId('revoked-screen')).toBeInTheDocument();
      expect(screen.getByText(/Display access revoked or expired/i)).toBeInTheDocument();
      expect(screen.getByText(/new display link or setup code in Admin Settings/i)).toBeInTheDocument();
      expect(screen.getByText(/open \/tv on this screen and enter the code/i)).toBeInTheDocument();
      expect(screen.queryByTestId('wo-grid')).not.toBeInTheDocument();

      // The dead credential is dropped from storage.
      expect(mockClearToken).toHaveBeenCalled();

      // Polling stops — no further fetches against a known-dead token.
      const callsAfterRevoke = mockFetchWallboard.mock.calls.length;
      await act(async () => {
        jest.advanceTimersByTime(120_000);
      });
      expect(mockFetchWallboard.mock.calls.length).toBe(callsAfterRevoke);
    } finally {
      jest.useRealTimers();
    }
  });

  it('shows guidance when no token is available', async () => {
    mockGetToken.mockReturnValue(null);
    mockFetchWallboard.mockRejectedValue(new Error('NO_TOKEN'));
    renderWallboard();

    expect(await screen.findByText('No display token')).toBeInTheDocument();
    // Leads with the /tv setup-code pairing flow; link + sign-in are fallbacks.
    expect(screen.getByText(/setup code from Admin Settings .* enter it at \/tv/i)).toBeInTheDocument();
  });

  it('passes ?dept= to the fetch helper and renders it in the HUD scope line', async () => {
    mockFetchWallboard.mockResolvedValue(payload);
    renderWallboard('/wallboard?dept=machining');

    await waitFor(() => expect(mockFetchWallboard).toHaveBeenCalledWith('machining'));
    // Title-cased then uppercased — never the raw query param casing rules.
    expect(await screen.findByTestId('hud-scope')).toHaveTextContent('LIVE WALLBOARD // MACHINING');
  });

  it('applies and persists display settings from URL params', async () => {
    mockFetchWallboard.mockResolvedValue(payload);
    renderWallboard('/wallboard?dim=1&clock24=1');

    expect(await screen.findByTestId('night-dim-overlay')).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem('wallboard_display_settings') ?? '{}')).toEqual({
      clock24h: true,
      clockSeconds: false,
      nightDim: true,
    });
  });
});
