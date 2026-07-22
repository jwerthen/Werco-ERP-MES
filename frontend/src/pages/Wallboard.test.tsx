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
 * Extended coverage (2026-07-22): display-settings hardening (JSON-null /
 * junk storage, URL override + re-persist); sparse payload degradation
 * (null ship/today/quality, absent totals, zero qty, null current_op);
 * blank cells on join misses; the 12-card grid cap in server order; the
 * SHIP fraction's Central-noon escalation; the fdPulse motion budget; and
 * the client-side minute tick between polls.
 *
 * services/wallboardClient is mocked at the module boundary — the page must
 * never touch the global axios client (a display token cannot enter it).
 */

import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import Wallboard from './Wallboard';
import { FD } from '../components/wallboard/wallboardTokens';
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

/** Minimal WAITING job for grid-cap tests — WO-A01, WO-A02, … */
function waitingJob(n: number): WallboardJob {
  return {
    wo_number: `WO-A${String(n).padStart(2, '0')}`,
    part_number: `PART-${n}`,
    status: 'released',
    qty_complete: 0,
    qty_ordered: 10,
    is_late: false,
    days_late: 0,
    blocked: false,
    down: false,
    running: false,
    ops_completed: 0,
    ops_total: 2,
    current_op: {
      sequence: 10,
      name: 'Saw',
      work_center_code: `SAW-${n}`,
      work_center_name: `Saw ${n}`,
      status: 'pending',
      elapsed_minutes: 0,
    },
  };
}

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

      // Recovery: one good poll resets to SYNC OK, clears the count, AND the
      // fresh payload swaps in (the board must not keep serving stale data).
      mockFetchWallboard.mockResolvedValue({ ...payload, late_total: 9 });
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '0');
      expect(screen.getByTestId('hud-chip-late')).toHaveTextContent('9');

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

  describe('display settings hardening', () => {
    it('renders with all-false defaults when storage holds JSON null (regression: "null" parses clean)', async () => {
      window.localStorage.setItem('wallboard_display_settings', 'null');
      mockFetchWallboard.mockResolvedValue(payload);
      renderWallboard();

      // The board comes up instead of crashing on the null field reads.
      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      expect(screen.queryByTestId('night-dim-overlay')).not.toBeInTheDocument();
      // 12h clock (meridiem shown), no seconds — the all-false defaults.
      expect(screen.getByText(/^(AM|PM)$/)).toBeInTheDocument();
      expect(screen.getByTestId('hud-clock')).toHaveTextContent(/^\d{1,2}:\d{2}$/);
      // A URL with no settings params never re-persists over the stored value.
      expect(window.localStorage.getItem('wallboard_display_settings')).toBe('null');
    });

    it('coerces non-boolean junk in stored settings to false', async () => {
      window.localStorage.setItem(
        'wallboard_display_settings',
        JSON.stringify({ clock24h: 'yes', clockSeconds: 1, nightDim: 'on' })
      );
      mockFetchWallboard.mockResolvedValue(payload);
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      // Truthy-but-not-true junk must NOT enable anything.
      expect(screen.queryByTestId('night-dim-overlay')).not.toBeInTheDocument();
      expect(screen.getByText(/^(AM|PM)$/)).toBeInTheDocument();
      expect(screen.getByTestId('hud-clock')).toHaveTextContent(/^\d{1,2}:\d{2}$/);
    });

    it('URL params override stored true values and persist the merged resolved set', async () => {
      window.localStorage.setItem(
        'wallboard_display_settings',
        JSON.stringify({ clock24h: true, clockSeconds: true, nightDim: true })
      );
      mockFetchWallboard.mockResolvedValue(payload);
      renderWallboard('/wallboard?clock24=0');

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      // Unmentioned settings load from storage (dim + seconds stay on)…
      expect(screen.getByTestId('night-dim-overlay')).toBeInTheDocument();
      expect(screen.getByTestId('hud-clock')).toHaveTextContent(/^\d{1,2}:\d{2}:\d{2}$/);
      // …while clock24=0 beats the stored true (12h → meridiem shown).
      expect(screen.getByText(/^(AM|PM)$/)).toBeInTheDocument();
      // The RESOLVED set re-persists, so the next unparameterized boot keeps it.
      expect(JSON.parse(window.localStorage.getItem('wallboard_display_settings') ?? '{}')).toEqual({
        clock24h: false,
        clockSeconds: true,
        nightDim: true,
      });
    });
  });

  describe('sparse / degraded payloads', () => {
    it('degrades ship, today, and quality to em-dashes when all three are null', async () => {
      mockFetchWallboard.mockResolvedValue({ ...payload, ship: null, today: null, quality: null });
      renderWallboard();

      // The grid still renders — a partial payload never blanks the board.
      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();

      // SHIP TODAY: em-dash fraction, em-dash body, em-dash week — same slots.
      expect(screen.getByTestId('ship-panel')).toHaveTextContent('—/—');
      expect(screen.getByTestId('ship-panel')).toHaveTextContent(/THIS WEEK—/);

      // NCRs / holds: both counts em-dash, no NEWEST sub-line.
      const quality = within(screen.getByTestId('quality-row'));
      expect(quality.getAllByText('—')).toHaveLength(2);
      expect(quality.queryByText(/NEWEST/)).not.toBeInTheDocument();

      // TODAY bar: all six KPI cells em-dash; the bar keeps its slot.
      expect(within(screen.getByTestId('today-kpis')).getAllByText('—')).toHaveLength(6);
    });

    it('falls back to derived counts when the true totals are absent (old backend)', async () => {
      mockFetchWallboard.mockResolvedValue({
        ...payload,
        late_total: undefined,
        blocked_total: undefined,
        down_total: undefined,
      });
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      // down ← work_centers with an open downtime (1), blocked ← blocked_wos
      // length (1), late ← late_wos length (2).
      expect(screen.getByTestId('hud-chip-down')).toHaveTextContent('1');
      expect(screen.getByTestId('hud-chip-blocked')).toHaveTextContent('1');
      expect(screen.getByTestId('hud-chip-late')).toHaveTextContent('2');
      expect(screen.getByTestId('late-total')).toHaveTextContent('2');
      expect(screen.getByTestId('blocked-total')).toHaveTextContent('1');
      expect(screen.getByTestId('down-total')).toHaveTextContent('1');
      // Derived totals equal the rendered rows — no phantom "+N MORE".
      expect(within(screen.getByTestId('late-panel')).queryByText(/\+\d+ MORE/)).not.toBeInTheDocument();
      expect(within(screen.getByTestId('blocked-down-panel')).queryByText(/\+\d+ MORE/)).not.toBeInTheDocument();
    });

    it('renders 0% (never NaN) for a job with qty_ordered 0', async () => {
      const zeroJob = { ...jobs[4], wo_number: 'WO-ZERO', qty_complete: 0, qty_ordered: 0 };
      mockFetchWallboard.mockResolvedValue({ ...payload, jobs: [zeroJob], jobs_total: 1 });
      renderWallboard();

      const card = await screen.findByTestId('wo-card-WO-ZERO');
      expect(within(card).getByText('0%')).toBeInTheDocument();
      expect(card.textContent).not.toMatch(/NaN/);
    });

    it('renders ALL OPS COMPLETE and a blank machine row when current_op is null', async () => {
      const doneJob = { ...jobs[4], wo_number: 'WO-DONE', current_op: null };
      mockFetchWallboard.mockResolvedValue({ ...payload, jobs: [doneJob], jobs_total: 1 });
      renderWallboard();

      const card = await screen.findByTestId('wo-card-WO-DONE');
      expect(within(card).getByText('ALL OPS COMPLETE')).toBeInTheDocument();
      // The waiting stop reason still renders; the machine cell is just blank.
      expect(within(card).getByText('IN QUEUE')).toBeInTheDocument();
      expect(card.textContent).not.toMatch(/undefined|NaN/i);
    });
  });

  describe('join misses degrade to blank cells', () => {
    it('a DOWN job whose work center code matches no work_centers entry gets blank stoppage cells', async () => {
      // work_centers still carries the MILL-1 downtime — the join is strictly
      // by the CURRENT OP's code, so GHOST-9 must not borrow another WC's data.
      const ghostJob = {
        ...jobs[0],
        wo_number: 'WO-GHOST',
        current_op: { ...jobs[0].current_op!, work_center_code: 'GHOST-9', work_center_name: 'Ghost Cell' },
      };
      mockFetchWallboard.mockResolvedValue({ ...payload, jobs: [ghostJob], jobs_total: 1 });
      renderWallboard();

      const card = await screen.findByTestId('wo-card-WO-GHOST');
      expect(within(card).getByText('DOWN')).toBeInTheDocument();
      // No duration, no reason, no "undefined" — blank cells are the design.
      expect(within(card).queryByText('2H14M')).not.toBeInTheDocument();
      expect(within(card).queryByText('MAINTENANCE')).not.toBeInTheDocument();
      expect(card.textContent).not.toMatch(/undefined|NaN/i);
    });

    it('a BLOCKED job absent from blocked_wos gets blank age/reason cells', async () => {
      const orphanJob = { ...jobs[1], wo_number: 'WO-7777' };
      mockFetchWallboard.mockResolvedValue({ ...payload, jobs: [orphanJob], jobs_total: 1 });
      renderWallboard();

      const card = await screen.findByTestId('wo-card-WO-7777');
      expect(within(card).getByText('BLOCKED')).toBeInTheDocument();
      expect(within(card).queryByText('22H')).not.toBeInTheDocument();
      expect(within(card).queryByText('WAITING INSPECT')).not.toBeInTheDocument();
      expect(card.textContent).not.toMatch(/undefined|NaN/i);
    });
  });

  describe('overflow strip arithmetic', () => {
    it('caps the grid at the first 12 jobs in server order and counts overflow from jobs_total', async () => {
      // Descending WO numbers: any client-side re-sort would flip the order.
      const manyJobs = Array.from({ length: 14 }, (_, i) => waitingJob(14 - i));
      mockFetchWallboard.mockResolvedValue({ ...payload, jobs: manyJobs, jobs_total: 17 });
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      const cardIds = screen.getAllByTestId(/^wo-card-/).map(el => el.getAttribute('data-testid'));
      expect(cardIds).toEqual(
        Array.from({ length: 12 }, (_, i) => `wo-card-WO-A${String(14 - i).padStart(2, '0')}`)
      );
      // 17 total − 12 rendered = +5 (from the uncapped total, not jobs.length).
      expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent('+5 MORE WORK ORDERS IN QUEUE');
    });
  });

  describe('SHIP TODAY fraction states', () => {
    const shipFraction = (text: string) =>
      within(screen.getByTestId('ship-panel')).getByText(
        (_, el) => !!el && el.tagName === 'SPAN' && el.textContent === text
      );

    it('shows NONE DUE when nothing is due today and there is no next promise date', async () => {
      mockFetchWallboard.mockResolvedValue({
        ...payload,
        ship: { due_today: 0, shipped_today: 0, due_this_week: 0, due_today_rows: [], next_due_date: null, next_due_count: 0 },
      });
      renderWallboard();

      expect(await screen.findByText('NONE DUE')).toBeInTheDocument();
    });

    it('shows the next promise date when nothing is due today', async () => {
      mockFetchWallboard.mockResolvedValue({
        ...payload,
        ship: {
          due_today: 0,
          shipped_today: 0,
          due_this_week: 4,
          due_today_rows: [],
          next_due_date: '2026-07-25',
          next_due_count: 3,
        },
      });
      renderWallboard();

      expect(await screen.findByText('NEXT DUE SAT JUL 25 (3 WOS)')).toBeInTheDocument();
    });

    it('colors a behind fraction amber before noon Central', async () => {
      // 15:00Z on 2026-07-22 = 10:00 CDT — behind (5/8) but morning.
      jest.useFakeTimers({ now: new Date('2026-07-22T15:00:00Z') });
      try {
        mockFetchWallboard.mockResolvedValue(payload);
        renderWallboard();

        expect(await screen.findByTestId('ship-panel')).toBeInTheDocument();
        expect(shipFraction('5/8')).toHaveStyle({ color: FD.amber });
      } finally {
        jest.useRealTimers();
      }
    });

    it('escalates a behind fraction to red at/after noon Central', async () => {
      // 18:30Z on 2026-07-22 = 13:30 CDT — still behind past the noon gate.
      jest.useFakeTimers({ now: new Date('2026-07-22T18:30:00Z') });
      try {
        mockFetchWallboard.mockResolvedValue(payload);
        renderWallboard();

        expect(await screen.findByTestId('ship-panel')).toBeInTheDocument();
        expect(shipFraction('5/8')).toHaveStyle({ color: FD.red });
      } finally {
        jest.useRealTimers();
      }
    });

    it('colors a complete fraction green regardless of the clock', async () => {
      mockFetchWallboard.mockResolvedValue({ ...payload, ship: { ...payload.ship!, shipped_today: 8 } });
      renderWallboard();

      expect(await screen.findByTestId('ship-panel')).toBeInTheDocument();
      expect(shipFraction('8/8')).toHaveStyle({ color: FD.green });
    });

    it('clamps +N MORE TODAY at zero when the rows already cover the remainder', async () => {
      // due 3 − shipped 2 − 2 rendered rows = −1 → clamps to 0 → no line.
      mockFetchWallboard.mockResolvedValue({
        ...payload,
        ship: { ...payload.ship!, due_today: 3, shipped_today: 2 },
      });
      renderWallboard();

      expect(await screen.findByTestId('ship-panel')).toBeInTheDocument();
      expect(within(screen.getByTestId('ship-panel')).queryByText(/MORE TODAY/)).not.toBeInTheDocument();
    });
  });

  describe('pulse discipline (the board motion budget)', () => {
    const pulsingElements = () => Array.from(document.querySelectorAll('[style*="fdPulse"]'));

    it('fdPulse animates exactly the DOWN dots: the header chip dot and the DOWN card chip dot', async () => {
      mockFetchWallboard.mockResolvedValue(payload);
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      const pulsing = pulsingElements();
      expect(pulsing).toHaveLength(2);
      expect(screen.getByTestId('hud-chip-down').contains(pulsing[0])).toBe(true);
      expect(screen.getByTestId('wo-card-WO-1042').contains(pulsing[1])).toBe(true);
    });

    it('nothing pulses when nothing is down — even with BLOCKED and LATE alarms active', async () => {
      mockFetchWallboard.mockResolvedValue({
        ...payload,
        down_total: 0,
        work_centers: payload.work_centers.map(wc => ({ ...wc, down: null })),
        jobs: jobs.map(job => (job.wo_number === 'WO-1042' ? { ...job, down: false } : job)),
      });
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      // BLOCKED (3) and LATE (7) chips are lit, but only DOWN may ever pulse.
      expect(screen.getByTestId('hud-chip-blocked')).toHaveTextContent('3');
      expect(pulsingElements()).toHaveLength(0);
    });
  });

  describe('minute counters between polls', () => {
    it('elapsed and downtime values tick by whole client-side minutes while polls fail', async () => {
      jest.useFakeTimers();
      try {
        mockFetchWallboard.mockResolvedValueOnce(payload);
        renderWallboard();

        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        // Baseline: downtime 134m on the DOWN card + the rail row; running 24m.
        expect(screen.getAllByText('2H14M')).toHaveLength(2);
        expect(screen.getByText('24M')).toBeInTheDocument();

        // Fail the next polls so lastUpdated (the tick baseline) stays put.
        mockFetchWallboard.mockRejectedValue(new Error('HTTP_500'));

        // 59s: still the same values — the counters move in WHOLE minutes.
        await act(async () => {
          jest.advanceTimersByTime(59_000);
        });
        expect(screen.getAllByText('2H14M')).toHaveLength(2);
        expect(screen.getByText('24M')).toBeInTheDocument();

        // Cross the minute: 134→135 (card + rail), 24→25, late elapsed 137→138.
        await act(async () => {
          jest.advanceTimersByTime(2_000);
        });
        expect(screen.getAllByText('2H15M')).toHaveLength(2);
        expect(screen.getByText('25M')).toBeInTheDocument();
        expect(screen.getByText('2H18M')).toBeInTheDocument();
      } finally {
        jest.useRealTimers();
      }
    });
  });
});
