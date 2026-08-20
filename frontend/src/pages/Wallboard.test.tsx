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
 * Zone 2 anchor row + rotating field (2026-08-19): the 22s dwell advancing
 * only grid rows 2-3 while row 1 keeps its cards AND their DOM nodes; the
 * order-insensitive plan key (a tail reorder rebuilds nothing); the
 * edge-triggered alarm snap and its race guard against an out-of-order poll;
 * the dept reset; nightDim freezing the cycle; offline continuing it; a
 * vanished frozen wo_number leaving a hole in place; and the static band at
 * n <= 15, where the board stays byte-identical to its pre-cycle self.
 * All of those run on a PINNED clock — see the describe block's header.
 *
 * services/wallboardClient is mocked at the module boundary — the page must
 * never touch the global axios client (a display token cannot enter it).
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
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

/**
 * Zone 2 cycle fixtures + helpers.
 *
 * CYCLE_DWELL_MS mirrors Wallboard.tsx's own constant, which is deliberately not
 * exported there (cadence belongs with its caller) — so this copy is the test's
 * explicit statement of the contract it depends on.
 */
const CYCLE_DWELL_MS = 22_000;

/**
 * A clock pinned to a DWELL BOUNDARY. `slot` is floor(now / CYCLE_DWELL_MS),
 * derived from the board's existing 1s tick, so WHICH PAGE is on screen is a
 * function of wall-clock phase. Landing on a boundary makes every
 * advanceTimersByTime(22_000) cross exactly one boundary and every 30s poll land
 * in a slot the test can name — otherwise these tests would pass or fail
 * depending on what second the suite happened to start.
 */
const PINNED_NOW = new Date(Math.floor(new Date('2026-07-22T13:00:00Z').getTime() / CYCLE_DWELL_MS) * CYCLE_DWELL_MS);

/** WAITING job for the cycle fixtures — WO-C01, WO-C02, … in server order. */
function cycleJob(n: number, overrides: Partial<WallboardJob> = {}): WallboardJob {
  return {
    ...waitingJob(n),
    wo_number: `WO-C${String(n).padStart(2, '0')}`,
    part_number: `CYC-${String(n).padStart(2, '0')}`,
    ...overrides,
  };
}
const cycleJobs = (count: number) => Array.from({ length: count }, (_, i) => cycleJob(i + 1));
const woNum = (n: number) => `WO-C${String(n).padStart(2, '0')}`;
const cardId = (n: number) => `wo-card-${woNum(n)}`;
/** The twelve cells the board should be showing: anchor 1-4 + the field window. */
const boardOf = (...field: number[]) => [1, 2, 3, 4, ...field].map(cardId);

/** Rendered CARDS only — `wo-card-unit` / `wo-card-customer` share the prefix. */
const renderedCards = () => screen.getAllByTestId(/^wo-card-WO-/).map(el => el.getAttribute('data-testid'));
/** All twelve grid children in order: cards AND the plain cells between them. */
const gridCells = () => Array.from(screen.getByTestId('wo-grid').children);

/** The segmented page bar: how many segments, and which one is lit. */
function pageBar(): { segments: number; index: number } | null {
  const bar = screen.queryByTestId('wo-page-bar');
  if (!bar) return null;
  const segments = Array.from(bar.children).map(seg => seg.getAttribute('data-testid'));
  return { segments: segments.length, index: segments.indexOf('wo-page-seg-on') };
}

/**
 * Advance the board's clock. WHOLE SECONDS only: RTL auto-advances fake timers
 * inside findBy/waitFor in ~50ms steps, so a sub-second cadence would fire
 * mid-await and produce act() warnings.
 */
async function advance(ms: number) {
  await act(async () => {
    jest.advanceTimersByTime(ms);
  });
}

/** A promise the test resolves by hand — for proving the out-of-order race guard. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(r => {
    resolve = r;
  });
  return { promise, resolve };
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
  // 5 delivered against 12 open, deliberately inside the STATIC BAND. With 5
  // jobs the field holds one card, planFieldPages(1) returns a single page, and
  // pageIndex is 0 at every wall-clock phase — so every test in this file that
  // advances timers (59s / 61s / 90s / 120s) asserts against a board that cannot
  // flip underneath it, and none of them depend on when the suite happens to
  // run. Cycling gets its own >15-job fixture and its own pinned clock in
  // "anchor row + rotating field" below.
  jobs_total: 12,
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

/**
 * Lets a test change ?dept= on a MOUNTED board. `dept` comes from the router,
 * and MemoryRouter's initialEntries is initial-only, so the switch has to be a
 * real in-tree navigation — the same thing that happens when someone re-points a
 * TV at another department's URL.
 */
function DeptSwitch({ to }: { to: string }) {
  const navigate = useNavigate();
  return (
    <button type="button" data-testid="switch-dept" onClick={() => navigate(to)}>
      switch dept
    </button>
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

    // Overflow strip: 12 open − 5 on board = +7, in today's wording. The static
    // band emits none of the cycling copy and renders no page bar.
    expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent('+7 MORE WORK ORDERS IN QUEUE');
    expect(screen.getByTestId('wo-overflow-strip')).not.toHaveTextContent('PINNED');
    expect(screen.queryByTestId('wo-page-bar')).not.toBeInTheDocument();

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

  it('a SUPERSEDED failing poll cannot report STALE over data a newer poll already painted', async () => {
    jest.useFakeTimers();
    try {
      let rejectSlow!: (reason: unknown) => void;
      const slow = new Promise<WallboardResponse>((_resolve, reject) => {
        rejectSlow = reject;
      });
      const fast = deferred<WallboardResponse>();
      mockFetchWallboard
        .mockResolvedValueOnce(payload) // #1 — the initial board
        .mockReturnValueOnce(slow) // #2 — issued first, rejects LAST
        .mockReturnValueOnce(fast.promise) // #3 — issued second, resolves FIRST
        .mockResolvedValue(payload);

      renderWallboard();
      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(30_000); // poll #2 goes out (the slow one)
      });
      await act(async () => {
        jest.advanceTimersByTime(30_000); // poll #3 goes out
      });
      await act(async () => {
        fast.resolve({ ...payload, late_total: 9 });
      });
      expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '0');

      // The slow poll finally gives up on a flaky shop network. It is older than
      // the payload on screen, so it must report nothing: the connection is
      // demonstrably fine, and the chip claiming STALE over seconds-old data
      // (four such overlaps escalate it to LOST) is a lie about freshness.
      await act(async () => {
        rejectSlow(new Error('HTTP_500'));
        await Promise.resolve();
      });
      expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '0');
      expect(screen.getByTestId('sync-status')).toHaveTextContent('SYNC OK');
      expect(screen.getByTestId('hud-chip-late')).toHaveTextContent('9');
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
    it('fills the 12 cells with anchor + field page 0 — jobs[0..11] in server order — and counts overflow from jobs_total', async () => {
      // 12 delivered → F = 8 → the field FITS, so planFieldPages(8) is ONE page
      // and the board is byte-identical to its pre-cycle self. What used to be a
      // hard `slice(0, 12)` cap is now anchor (jobs[0..3], live) + field page 0
      // (jobs[4..11]) — the same twelve cards, in the same order, for a different
      // reason. Descending WO numbers: any client-side re-sort would flip them.
      const manyJobs = Array.from({ length: 12 }, (_, i) => waitingJob(12 - i));
      mockFetchWallboard.mockResolvedValue({ ...payload, jobs: manyJobs, jobs_total: 17 });
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      const cardIds = screen.getAllByTestId(/^wo-card-/).map(el => el.getAttribute('data-testid'));
      expect(cardIds).toEqual(Array.from({ length: 12 }, (_, i) => `wo-card-WO-A${String(12 - i).padStart(2, '0')}`));
      // 17 open − 12 delivered = +5 still hidden — but that residue is now the
      // SERVER's truncation alone. Every job the payload actually carried is on
      // the board, which is the difference between this and the pre-cycle rule.
      expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent('+5 MORE WORK ORDERS IN QUEUE');
      // Single page ⇒ no page bar, and none of the cycling copy.
      expect(screen.queryByTestId('wo-page-bar')).not.toBeInTheDocument();
      expect(screen.getByTestId('wo-overflow-strip')).not.toHaveTextContent('PINNED');
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
        ship: {
          due_today: 0,
          shipped_today: 0,
          due_this_week: 0,
          due_today_rows: [],
          next_due_date: null,
          next_due_count: 0,
        },
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

  /**
   * The board's motion budget is ONE animation: fdPulse on DOWN dots. Nothing
   * else animates, transitions or transforms — no heartbeat, no new-event flash,
   * no payload-swap fade, and (2026-08-19) no page-flip motion of any kind.
   *
   * This guard used to match only the literal string "fdPulse", which meant any
   * NEW keyframe — a flip fade, a countdown/progress bar, a highlight on a card
   * that just changed page — sailed straight past the one test that enforces the
   * budget. It now asserts the TOTAL set of moving elements (inline animation /
   * transition declarations AND Tailwind motion utilities) and the TOTAL set of
   * @keyframes the page injects, so anything that moves has to come here and say
   * so out loud.
   */
  describe('pulse discipline (the board motion budget)', () => {
    /**
     * Any inline animation-* / transition-* declaration that isn't explicitly
     * off. NOTE the value guard is a lookahead placed IMMEDIATELY after the
     * colon and consumes nothing: written as `:\s*(?!none)`, the `\s*` simply
     * backtracks to zero width, the lookahead is then evaluated against the
     * SPACE rather than the value, and every `animation: none` in the tree reads
     * as motion. That mistake makes this guard fail open, not closed.
     */
    const MOVING_INLINE_STYLE = /(?:^|;)\s*(?:animation|transition)[a-z-]*\s*:(?!\s*(?:none|initial|0s)\b)/i;
    /** Tailwind motion utilities: animate-*, transition / transition-*, duration-*, ease-*. */
    const MOVING_CLASS = /(?:^|\s)(?:animate-|transition(?:$|[\s-])|duration-|ease-)/;

    const movingElements = () =>
      Array.from(document.querySelectorAll('*')).filter(el => {
        const style = el.getAttribute('style') ?? '';
        const className = el.getAttribute('class') ?? '';
        return MOVING_INLINE_STYLE.test(style) || MOVING_CLASS.test(className);
      });

    /** Every @keyframes name the page injects, in document order. */
    const declaredKeyframes = () =>
      Array.from(document.querySelectorAll('style'))
        .flatMap(el => Array.from((el.textContent ?? '').matchAll(/@keyframes\s+([A-Za-z0-9_-]+)/g)))
        .map(match => match[1]);

    it('animates exactly two elements — the DOWN header chip dot and the DOWN card chip dot — and declares exactly one keyframe', async () => {
      mockFetchWallboard.mockResolvedValue(payload);
      renderWallboard();

      expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
      const moving = movingElements();
      expect(moving).toHaveLength(2);
      expect(screen.getByTestId('hud-chip-down').contains(moving[0])).toBe(true);
      expect(screen.getByTestId('wo-card-WO-1042').contains(moving[1])).toBe(true);
      // Both are fdPulse and nothing else declares a keyframe. A second name here
      // means someone spent the alarm channel — argue for it in review, don't
      // widen this expectation.
      moving.forEach(el => expect(el.getAttribute('style')).toContain('fdPulse'));
      expect(declaredKeyframes()).toEqual(['fdPulse']);
    });

    it('nothing moves when nothing is down — even with BLOCKED and LATE alarms active', async () => {
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
      expect(movingElements()).toHaveLength(0);
      // The keyframe is still DECLARED (it lives in a static <style>) — the point
      // is that nothing references it.
      expect(declaredKeyframes()).toEqual(['fdPulse']);
    });

    it('the zone 2 field flip spends nothing from the budget — a whole dwell later, still nothing moves', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // 16 delivered → 2 pages → the board is genuinely cycling. Every job is
        // WAITING, so a single moving element here would be the flip's own doing.
        mockFetchWallboard.mockResolvedValue({ ...payload, down_total: 0, jobs: cycleJobs(16), jobs_total: 16 });
        renderWallboard();

        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(screen.getByTestId('wo-page-bar')).toBeInTheDocument();
        expect(movingElements()).toHaveLength(0);

        await act(async () => {
          jest.advanceTimersByTime(22_000);
        });

        // The field HAS turned…
        expect(screen.getByTestId('wo-card-WO-C16')).toBeInTheDocument();
        // …with no fade, no slide, no transition, and no new keyframe. The flip
        // is a discrete React state derivation on purpose: the global
        // prefers-reduced-motion block in styles/accessibility.css forces
        // animation-duration: 0.01ms !important on *, so a CSS-animated carousel
        // would freeze on page 0 forever on any TV reporting reduced motion.
        expect(movingElements()).toHaveLength(0);
        expect(declaredKeyframes()).toEqual(['fdPulse']);
      } finally {
        jest.useRealTimers();
      }
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

  /**
   * Zone 2 — the anchor row + rotating field (2026-08-19).
   *
   * Grid row 1 is the ANCHOR (`jobs.slice(0, 4)`, re-derived LIVE every render,
   * never paged, never in the plan); grid rows 2-3 are the FIELD, an 8-wide
   * window over `jobs.slice(4)` that flips on a 22s dwell. The load-bearing
   * property is that anchor + field page 0 is exactly `jobs[0..11]` — today's
   * board, card for card — so a job shown twice or silently skipped is
   * unrepresentable rather than merely defended against.
   *
   * Every test here PINS the clock. Which page is on screen is a function of
   * wall-clock phase (`slot = floor(now / 22_000)`), so an unpinned test would
   * pass or fail depending on what second the suite started. The pin sits on a
   * dwell boundary, so from t = 0: +22s = one boundary, t = 30s is slot 1,
   * t = 60s is slot 2, t = 90s is slot 4.
   */
  describe('anchor row + rotating field', () => {
    /** A cycling payload: `list` delivered, `total` open (defaults to delivered). */
    const cyclePayload = (list: WallboardJob[], total?: number): WallboardResponse => ({
      ...payload,
      jobs: list,
      jobs_total: total ?? list.length,
    });

    it('flips only the field on the 22s dwell — the anchor row keeps its cards AND their DOM nodes', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // 16 delivered → F = 12 → 2 pages, starts [0, 4] (the spec's worked value).
        mockFetchWallboard.mockResolvedValue(cyclePayload(cycleJobs(16)));
        renderWallboard();

        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        // Page 0 IS today's board: jobs[0..11], in server order. This first
        // assertion is also the COLD-START guard: the very first render happens
        // before any payload exists, so a plan built from that empty list must
        // not be treated as a cycle in progress — rows 2-3 fill on the first
        // payload, not one dwell (22 blank seconds) into every boot.
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent(
          'TOP 4 PINNED · PAGE 1/2 · 16 OPEN WORK ORDERS'
        );
        expect(pageBar()).toEqual({ segments: 2, index: 0 });

        const anchorNodes = [1, 2, 3, 4].map(n => screen.getByTestId(cardId(n)));

        await advance(CYCLE_DWELL_MS);

        // starts[1] = 4 — the final window back-fills flush, so it is a FULL page
        // of eight rather than a row of holes.
        expect(renderedCards()).toEqual(boardOf(9, 10, 11, 12, 13, 14, 15, 16));
        expect(pageBar()).toEqual({ segments: 2, index: 1 });
        // The TRACK has to be visible too, or the bar reports "something
        // changed" without ever saying which of how many pages: at 1.48:1 the
        // FD.line hairline is not resolvable at TV distance, so unlit segments
        // carry the palette's de-emphasized-rail token instead.
        const segments = Array.from(screen.getByTestId('wo-page-bar').children);
        expect(segments[0]).toHaveStyle({ background: FD.faint });
        expect(segments[1]).toHaveStyle({ background: FD.ink });
        expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent('PAGE 2/2');
        // The four anchor cards are the SAME DOM nodes React had before the flip.
        // That identity is the whole reason this design spends nothing from the
        // alarm channel: a DOWN card's 1.6s fdPulse never resets phase.
        [1, 2, 3, 4].forEach((n, i) => expect(screen.getByTestId(cardId(n))).toBe(anchorNodes[i]));

        // …and it wraps rather than stalling on the last page.
        await advance(CYCLE_DWELL_MS);
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
      } finally {
        jest.useRealTimers();
      }
    });

    it('a poll that only REORDERS the tail rebuilds nothing — the frozen field keeps its order', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        const list = cycleJobs(16);
        // Same SET, tail reversed. This is the real stress case, not a contrived
        // one: `running` is position 3 of the server's sort key and flips on
        // every clock-in/out, so at lunch and every shift change nearly the whole
        // shop's flag flips within minutes — which is also when the most people
        // walk past. The plan key is order-INSENSITIVE precisely so that rebuilds
        // nothing: no card moves, cards only recolor in place.
        const reordered = [...list.slice(0, 4), ...list.slice(4).reverse()];
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(list));
        mockFetchWallboard.mockResolvedValue(cyclePayload(reordered));

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));

        // t = 30s: the reordered payload lands, and one dwell has turned.
        await advance(30_000);

        expect(renderedCards()).toEqual(boardOf(9, 10, 11, 12, 13, 14, 15, 16));
        // Had the reorder rebuilt the plan, page 1 of the REVERSED field would be
        // C12 … C05 — every card in the zone at a different coordinate.
        expect(renderedCards()).not.toEqual(boardOf(12, 11, 10, 9, 8, 7, 6, 5));
      } finally {
        jest.useRealTimers();
      }
    });

    it('a reorder that lifts a field job into the anchor row renders it ONCE, in row 1', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
      try {
        const list = cycleJobs(16);
        // Same SET again — C09 simply sorts to the front (it started running).
        // The anchor is LIVE while the field is FROZEN, and a reorder
        // deliberately rebuilds nothing, so without the anchor-overlap rule one
        // wo_number would render in BOTH halves under a single React key.
        const lifted = [list[8], ...list.filter((_, i) => i !== 8)];
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(list));
        mockFetchWallboard.mockResolvedValue(cyclePayload(lifted));

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();

        await advance(30_000);

        // Row 1 is live, so C09 is now the first anchor card…
        expect(screen.getAllByTestId(cardId(9))).toHaveLength(1);
        expect(renderedCards()).toEqual([9, 1, 2, 3, 10, 11, 12, 13, 14, 15, 16].map(cardId));
        // …and its frozen field slot is a PLAIN CELL, held in place. Not
        // "skipped" — the job is on screen, one row up.
        expect(gridCells()).toHaveLength(12);
        expect(gridCells()[4].getAttribute('data-testid')).toBeNull();
        expect(errorSpy.mock.calls.map(args => String(args[0])).join('\n')).not.toMatch(/same key/i);

        // C04 is the job C09's lift DISPLACED out of the anchor row, and for
        // this dwell it is on NO cell: the anchor is live (it holds C09, C01,
        // C02, C03) while the frozen field list was captured when C04 was still
        // rank 4. That is the mid-cycle cost of not moving cards…
        expect(screen.queryByTestId(cardId(4))).not.toBeInTheDocument();

        // …and it is BOUNDED BY ONE DWELL, which is the whole reason the plan
        // key is split `anchor|field` rather than being one order-insensitive
        // list. Under a whole-list key this reorder changes no set, rebuilds
        // nothing, and C04 stays invisible on EVERY page until some WO is
        // released or completed — minutes to hours, with the strip meanwhile
        // reading "16 OPEN WORK ORDERS" over eleven cards and a hole.
        await advance(CYCLE_DWELL_MS);
        expect(screen.getByTestId(cardId(4))).toBeInTheDocument();
        expect(renderedCards()).toEqual([9, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12].map(cardId));
        expect(gridCells().every(cell => (cell.getAttribute('data-testid') ?? '').startsWith('wo-card-WO-'))).toBe(
          true
        );
        // The rebuild PRESERVED THE PHASE — it is the deferred case (d), not a
        // snap back to the top of the queue.
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
      } finally {
        errorSpy.mockRestore();
        jest.useRealTimers();
      }
    });

    it('snaps to page 0 on a NEWLY down work order, and only on a new one (edge-triggered)', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        const list = cycleJobs(16);
        // Same SET both polls: C13 goes DOWN and the server sorts it first. The
        // set is unchanged, so nothing but the alarm can explain a rebuild.
        const downJob = { ...list[12], down: true };
        const alarmed = [downJob, ...list.filter((_, i) => i !== 12)];
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(list));
        mockFetchWallboard.mockResolvedValue(cyclePayload(alarmed));

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(pageBar()).toEqual({ segments: 2, index: 0 });

        // t = 22s: page 1.
        await advance(CYCLE_DWELL_MS);
        expect(pageBar()).toEqual({ segments: 2, index: 1 });

        // t = 30s: the poll lands with C13 newly DOWN. slot(30s) is still 1, so
        // WITHOUT the snap the board would sit on page 1 for another 14 seconds.
        await advance(8_000);
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
        // Anchor + field page 0 IS jobs[0..11] and the server sorts down first,
        // so the newly-alarmed job is on screen by construction.
        expect(screen.getByTestId(cardId(13))).toBeInTheDocument();
        expect(renderedCards()).toEqual([13, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11].map(cardId));

        // EDGE-triggered, not level-triggered: C13 stays down, and the board must
        // go back to cycling. A level-triggered snap would pin page 0 all
        // afternoon and silently restore the exact complaint this feature fixes.
        await advance(CYCLE_DWELL_MS);
        expect(pageBar()).toEqual({ segments: 2, index: 1 });
        // t = 60s — another poll, C13 still down, still no re-snap.
        await advance(8_000);
        expect(pageBar()).toEqual({ segments: 2, index: 1 });
      } finally {
        jest.useRealTimers();
      }
    });

    it('does NOT snap for a newly blocked HELD work order — it sorts last, so page 0 would show nothing new', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // 16 delivered, the last one ON_HOLD — the server sorts held work
        // strictly last (`_job_sort_key` prepends a held bucket), so C16 lives
        // on the final page whatever its blocked/down flags say.
        const list = [...cycleJobs(15), cycleJob(16, { status: 'on_hold' })];
        const heldBlocked = list.map(job => (job.wo_number === woNum(16) ? { ...job, blocked: true } : job));
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(list));
        mockFetchWallboard.mockResolvedValue(cyclePayload(heldBlocked));

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();

        // t = 22s: page 1.
        await advance(CYCLE_DWELL_MS);
        expect(pageBar()).toEqual({ segments: 2, index: 1 });

        // t = 30s: the poll lands with the HELD job newly blocked. The snap set
        // is the server's bucket-1 predicate EXACTLY, and since ON_HOLD joined
        // the wall that means "(blocked || down) AND NOT held". Snapping here
        // would yank every TV in the plant back to page 0 to show a job that is
        // on the page it just left — an unpredictable whole-zone lurch bought
        // for nothing.
        await advance(8_000);
        expect(pageBar()).toEqual({ segments: 2, index: 1 });
        expect(renderedCards()).toEqual(boardOf(9, 10, 11, 12, 13, 14, 15, 16));
      } finally {
        jest.useRealTimers();
      }
    });

    it('crossing 12 -> 14 delivered starts cycling on the SAME poll, never claiming the board is complete', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // A single-page plan has no cycle position to protect, so the deferred
        // rebuild has nothing to buy — and deferring costs a FALSE claim: the
        // strip takes its delivered count LIVE but its page count from the plan,
        // so for a whole dwell the board would read ALL OPEN WORK ORDERS ON
        // BOARD while ranks 13 and 14 were on no screen and no page bar existed.
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(cycleJobs(12), 12));
        mockFetchWallboard.mockResolvedValue(cyclePayload(cycleJobs(14), 14));

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent('ALL OPEN WORK ORDERS ON BOARD');
        expect(pageBar()).toBeNull();

        // t = 30s — mid-dwell (slot 1 turned at 22s), the two releases land.
        await advance(30_000);

        expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent(
          'TOP 4 PINNED · PAGE 1/2 · 14 OPEN WORK ORDERS'
        );
        expect(screen.getByTestId('wo-overflow-strip')).not.toHaveTextContent('ALL OPEN WORK ORDERS ON BOARD');
        expect(pageBar()).toEqual({ segments: 2, index: 0 });

        // …and the two new jobs really do reach the wall on the next flip.
        await advance(CYCLE_DWELL_MS);
        expect(screen.getByTestId(cardId(13))).toBeInTheDocument();
        expect(screen.getByTestId(cardId(14))).toBeInTheDocument();
      } finally {
        jest.useRealTimers();
      }
    });

    it('an out-of-order poll neither paints nor advances the alarm set (the race guard)', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // 24 delivered → F = 20 → 3 pages, starts [0, 8, 12].
        const list = cycleJobs(24);
        const alarmed = list.map(job => (job.wo_number === woNum(5) ? { ...job, down: true } : job));
        const slow = deferred<WallboardResponse>();
        const fast = deferred<WallboardResponse>();
        mockFetchWallboard
          .mockResolvedValueOnce(cyclePayload(list, 31)) // #1 — the initial board
          .mockReturnValueOnce(slow.promise) // #2 — issued first, lands LAST
          .mockReturnValueOnce(fast.promise) // #3 — issued second, lands FIRST
          .mockResolvedValue({ ...cyclePayload(alarmed, 31), late_total: 9 }); // #4 onward

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(pageBar()).toEqual({ segments: 3, index: 0 });

        await advance(30_000); // t = 30s, slot 1 → page 1; poll #2 goes out.
        await advance(30_000); // t = 60s, slot 2 → page 2; poll #3 goes out.
        expect(pageBar()).toEqual({ segments: 3, index: 2 });

        // #3 lands first: C05 is newly DOWN → snap to page 0, late chip 9.
        await act(async () => {
          fast.resolve({ ...cyclePayload(alarmed, 31), late_total: 9 });
        });
        expect(screen.getByTestId('hud-chip-late')).toHaveTextContent('9');
        expect(pageBar()).toEqual({ segments: 3, index: 0 });

        // #2 finally lands — older, superseded, and carrying NO alarm.
        await act(async () => {
          slow.resolve({ ...cyclePayload(list, 31), late_total: 3 });
        });
        // It painted nothing…
        expect(screen.getByTestId('hud-chip-late')).toHaveTextContent('9');
        expect(pageBar()).toEqual({ segments: 3, index: 0 });

        // …and, the part that is invisible without this assertion, it did not
        // rewind the alarm set. t = 90s is slot 4, two slots past the snap's
        // anchor, so the phase says page 2. Had the stale payload's empty alarm
        // set been applied, C05 would read as NEWLY down on this poll and snap
        // the board back to page 0 — the next genuine alarm silently swallowed.
        await advance(30_000);
        expect(pageBar()).toEqual({ segments: 3, index: 2 });
        expect(screen.getByTestId('hud-chip-late')).toHaveTextContent('9');
      } finally {
        jest.useRealTimers();
      }
    });

    it('a ?dept= change resets the plan to page 0 mid-dwell', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        mockFetchWallboard.mockResolvedValue(cyclePayload(cycleJobs(16)));
        // Same job SET on both departments, so nothing but the dept change can
        // explain the reset. On a real dept switch the frozen wo_numbers hold the
        // OLD department's jobs and would resolve to a page of blanks.
        render(
          <MemoryRouter initialEntries={['/wallboard?dept=machining']}>
            <Routes>
              <Route
                path="/wallboard"
                element={
                  <>
                    <Wallboard />
                    <DeptSwitch to="/wallboard?dept=finishing" />
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        );

        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        await advance(CYCLE_DWELL_MS);
        expect(pageBar()).toEqual({ segments: 2, index: 1 });

        await act(async () => {
          fireEvent.click(screen.getByTestId('switch-dept'));
        });

        await waitFor(() => expect(mockFetchWallboard).toHaveBeenCalledWith('finishing'));
        expect(screen.getByTestId('hud-scope')).toHaveTextContent('LIVE WALLBOARD // FINISHING');
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
      } finally {
        jest.useRealTimers();
      }
    });

    it('nightDim freezes the cycle at page 0', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        mockFetchWallboard.mockResolvedValue(cyclePayload(cycleJobs(16)));
        renderWallboard('/wallboard?dim=1');

        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(screen.getByTestId('night-dim-overlay')).toBeInTheDocument();
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));

        // Two full dwells later the board has not moved. The board is explicitly
        // declaring nobody is looking, so this is the one place where spending
        // the motion budget provably buys nothing — and page 0 is today's board.
        await advance(CYCLE_DWELL_MS);
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        await advance(CYCLE_DWELL_MS);
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        // The bar still shows two pages: a dimmed board stays honest about what
        // it is not showing.
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
      } finally {
        jest.useRealTimers();
      }
    });

    it('keeps cycling on last-known-good data while polls fail', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(cycleJobs(16)));
        renderWallboard();

        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));

        mockFetchWallboard.mockRejectedValue(new Error('HTTP_500'));

        // t = 30s: the poll fails (SYNC STALE) but the dwell has turned anyway.
        // Paging is not a freshness claim — the staged sync chip is already the
        // disclosure, and freezing would hide two-thirds of the population with
        // no visible cause, which is indistinguishable from a jammed cycle.
        await advance(30_000);
        expect(screen.getByTestId('sync-status')).toHaveTextContent('SYNC STALE');
        expect(renderedCards()).toEqual(boardOf(9, 10, 11, 12, 13, 14, 15, 16));

        await advance(CYCLE_DWELL_MS);
        expect(screen.getByTestId('sync-status')).toHaveAttribute('data-offline-level', '1');
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
      } finally {
        jest.useRealTimers();
      }
    });

    it('leaves a hole in place for a vanished wo_number, then heals at the next boundary WITHOUT resetting the phase', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        const list = cycleJobs(24); // 3 pages, starts [0, 8, 12]
        const withoutC15 = list.filter(job => job.wo_number !== woNum(15));
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(list, 24));
        mockFetchWallboard.mockResolvedValue(cyclePayload(withoutC15, 23));

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));

        // t = 30s: slot 1 → page 1 (field starts[1] = 8 → C13…C20), and the poll
        // that lands has C15 completed off the board. A set change with no new
        // alarm defers its rebuild to the next boundary, so this dwell renders
        // the frozen membership against the live payload.
        await advance(30_000);

        expect(screen.queryByTestId(cardId(15))).not.toBeInTheDocument();
        expect(renderedCards()).toEqual([1, 2, 3, 4, 13, 14, 16, 17, 18, 19, 20].map(cardId));
        // The hole stays a hole, in place: survivors deliberately do NOT reflow
        // up. Reflow is the coordinate scrambling this design exists to prevent.
        expect(gridCells()).toHaveLength(12);
        expect(gridCells()[6].getAttribute('data-testid')).toBeNull();
        expect(gridCells()[7].getAttribute('data-testid')).toBe(cardId(16));

        // Next boundary: the deferred rebuild fires and the zone is whole again.
        await advance(CYCLE_DWELL_MS);
        expect(gridCells()).toHaveLength(12);
        expect(gridCells().every(cell => (cell.getAttribute('data-testid') ?? '').startsWith('wo-card-WO-'))).toBe(
          true
        );
        expect(screen.queryByTestId(cardId(15))).not.toBeInTheDocument();
        // …and it rebuilds PRESERVING THE PHASE. 23 delivered → F = 19 → 3
        // pages, starts [0, 8, 11]; slot 2 against the original anchor slot 0 is
        // page 2, so the window is the flush final one. Had the rebuild reset
        // the phase to page 0 (C05…C12) the board would snap backwards on every
        // set change — and on this shop something is released or completed every
        // few minutes, so the later pages would never be reached at all and the
        // complaint this feature was filed to fix would be silently restored.
        expect(pageBar()).toEqual({ segments: 3, index: 2 });
        expect(renderedCards()).toEqual(boardOf(17, 18, 19, 20, 21, 22, 23, 24));
      } finally {
        jest.useRealTimers();
      }
    });

    it('rebuilds immediately when the WHOLE frozen field vanishes, rather than showing a dwell of blanks', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // A wholesale population replacement — every delivered wo_number is new.
        // The deferred rebuild exists to avoid disturbing A CYCLE IN PROGRESS, so
        // it has to be gated on there being one: with not a single frozen
        // wo_number still resolvable there is nothing to protect, and deferring
        // would blank grid rows 2-3 for a full 22 seconds.
        const replacement = cycleJobs(16).map(job => ({
          ...job,
          wo_number: job.wo_number.replace('WO-C', 'WO-D'),
        }));
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(cycleJobs(16)));
        mockFetchWallboard.mockResolvedValue(cyclePayload(replacement));

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));

        await advance(30_000);

        // All twelve cells, immediately, at page 0 — not four cards over eight
        // holes until the next boundary.
        expect(renderedCards()).toEqual(
          [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map(n => `wo-card-WO-D${String(n).padStart(2, '0')}`)
        );
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
      } finally {
        jest.useRealTimers();
      }
    });

    it('rebuilds immediately when the page ON SCREEN empties, not only when the whole field does', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // 24 delivered → 3 pages, starts [0, 8, 12]. A batch closes and the 8 WOs
        // that leave are exactly the ones in the window the board is SHOWING —
        // the least severe, which is where completions concentrate. Twelve
        // frozen wo_numbers survive, so "the whole frozen field vanished" is
        // false and a deferred rebuild would render eight blank cells for the
        // rest of the dwell while sixteen open work orders existed, under a strip
        // claiming three pages. A page of blanks reads as broken; there is
        // nothing on it to protect.
        const list = cycleJobs(24);
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(list, 24));
        mockFetchWallboard.mockResolvedValueOnce(cyclePayload(list, 24));
        mockFetchWallboard.mockResolvedValue(cyclePayload(cycleJobs(16), 16));

        renderWallboard();
        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();

        await advance(30_000); // slot 1 → page 1
        await advance(30_000); // slot 2 → page 2 (frozen field 12..19 = C17…C24), and the poll lands
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        expect(gridCells().every(cell => (cell.getAttribute('data-testid') ?? '').startsWith('wo-card-WO-'))).toBe(
          true
        );
      } finally {
        jest.useRealTimers();
      }
    });

    it('is byte-identical to the pre-cycle board at 12 delivered work orders (everything fits)', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // 12 delivered → F = 8 → the field fits in one window, so there is
        // nothing to cycle. The single-page band is exactly the band where no
        // delivered job would be hidden — a property of the formula, not a
        // hard-coded threshold.
        mockFetchWallboard.mockResolvedValue(cyclePayload(cycleJobs(12), 12));
        renderWallboard();

        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        expect(pageBar()).toBeNull();
        expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent('ALL OPEN WORK ORDERS ON BOARD');
        expect(screen.getByTestId('wo-overflow-strip')).not.toHaveTextContent('PINNED');

        // Two dwells later, nothing has moved. The feature is INERT here.
        await advance(CYCLE_DWELL_MS);
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        await advance(CYCLE_DWELL_MS);
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        expect(pageBar()).toBeNull();
      } finally {
        jest.useRealTimers();
      }
    });

    it('cycles at 13 delivered too, in two FULL pages that overlap rather than blank (owner decision)', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // 13 delivered → F = 9 → starts [0, 1]. This is the band that used to sit
        // static and hide the 13th job behind "+1 MORE". The flush clamp keeps
        // BOTH pages full, so the cost is that the field SHIFTS by one slot
        // rather than turning a clean page — accepted, because a short page would
        // blank seven cells for a whole dwell and disjoint full pages are
        // arithmetically impossible at F = 9.
        mockFetchWallboard.mockResolvedValue(cyclePayload(cycleJobs(13), 13));
        renderWallboard();

        expect(await screen.findByTestId('wo-grid')).toBeInTheDocument();
        // Page 0 is still today's board, card for card.
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
        expect(screen.getByTestId('wo-overflow-strip')).toHaveTextContent(
          'TOP 4 PINNED · PAGE 1/2 · 13 OPEN WORK ORDERS'
        );
        // The 13th job is not on page 0 — the whole point is that it arrives.
        expect(screen.queryByTestId(cardId(13))).not.toBeInTheDocument();

        await advance(CYCLE_DWELL_MS);
        // Field shifts by one: job 13 appears, job 5 leaves, the anchor holds.
        expect(renderedCards()).toEqual(boardOf(6, 7, 8, 9, 10, 11, 12, 13));
        expect(screen.getByTestId(cardId(13))).toBeInTheDocument();
        expect(pageBar()).toEqual({ segments: 2, index: 1 });
        // Both pages are FULL — twelve cards, never a row of holes.
        expect(renderedCards()).toHaveLength(12);

        // And it comes back round.
        await advance(CYCLE_DWELL_MS);
        expect(renderedCards()).toEqual(boardOf(5, 6, 7, 8, 9, 10, 11, 12));
        expect(pageBar()).toEqual({ segments: 2, index: 0 });
      } finally {
        jest.useRealTimers();
      }
    });

    it('reports the truncated residue in DIFFERENT wording while cycling (+N NOT ON BOARD)', async () => {
      jest.useFakeTimers({ now: PINNED_NOW });
      try {
        // 24 delivered (the payload cap) of 31 open.
        mockFetchWallboard.mockResolvedValue(cyclePayload(cycleJobs(24), 31));
        renderWallboard();

        const strip = await screen.findByTestId('wo-overflow-strip');
        expect(strip).toHaveTextContent('TOP 4 PINNED · PAGE 1/3 · 24 OF 31 OPEN WORK ORDERS · +7 NOT ON BOARD');
        // "+N MORE … IN QUEUE" is NEVER emitted while cycling: that phrase has to
        // keep exactly one meaning across the whole screen — permanently hidden
        // and strictly less severe — and it belongs to the Z3 rail. A viewer who
        // learned that zone 2's "+N" resolves itself on a cadence would stand in
        // front of the LATE panel waiting forever.
        expect(strip).not.toHaveTextContent('IN QUEUE');
        expect(pageBar()).toEqual({ segments: 3, index: 0 });
      } finally {
        jest.useRealTimers();
      }
    });
  });
});
