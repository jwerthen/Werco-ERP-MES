/**
 * Wallboard — the full-screen shop-floor TV board ("ANDON WALL").
 *
 * Covers: the JOB WALL (owner feedback 2026-07-15 — work-order tiles with
 * their current operation, server priority order preserved, "+N more" from
 * the uncapped jobs_total, DOWN > BLOCKED > LATE > RUNNING > WAITING bands)
 * + exception rail + today band (PLANT 30d cluster removed) rendered from
 * mock payload data; the shop-state hero; the steady amber→red offline chip
 * that keeps the last good data on failed polls; the revoked/no-token states;
 * the ?dept= pass-through with a title-cased chip and PLANT tags; fallback to
 * the ORIGINAL machine-tile FloorGrid when `jobs` is absent (old-backend
 * grace) incl. the OLD payload shape (no new blocks, no crew/is_late); the
 * new-event flash (suppressed on first paint, fired for a newly-blocked job
 * and a newly-down center); and the no-scroll / no-ticker invariants.
 *
 * services/wallboardClient is mocked at the module boundary — the page must
 * never touch the global axios client (a display token cannot enter it).
 */

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
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

/** Job wall block in SERVER priority order (alarms first) — never re-sorted. */
const jobs: WallboardJob[] = [
  {
    // down+blocked+late — DOWN wins the band.
    wo_number: 'WO-3001',
    part_number: 'PN-D1',
    status: 'in_progress',
    qty_complete: 5,
    qty_ordered: 20,
    promise_date: '2026-06-08',
    is_late: true,
    days_late: 2,
    blocked: true,
    down: true,
    running: false,
    ops_completed: 1,
    ops_total: 4,
    current_op: {
      sequence: 20,
      name: 'Weld',
      work_center_code: 'WELD-2',
      work_center_name: 'Weld 2',
      status: 'ready',
      qty_done: 0,
      qty_target: 20,
      crew: [],
      crew_count: 0,
      elapsed_minutes: 0,
    },
  },
  {
    // blocked+late — BLOCKED wins.
    wo_number: 'WO-3002',
    part_number: 'PN-B1',
    status: 'released',
    qty_complete: 0,
    qty_ordered: 10,
    promise_date: '2026-06-09',
    is_late: true,
    days_late: 1,
    blocked: true,
    down: false,
    running: false,
    ops_completed: 0,
    ops_total: 3,
    current_op: {
      sequence: 10,
      name: 'Saw',
      work_center_code: 'SAW-1',
      work_center_name: 'Saw 1',
      status: 'pending',
      qty_done: 0,
      qty_target: 10,
      crew: [],
      crew_count: 0,
      elapsed_minutes: 0,
    },
  },
  {
    // late+running — LATE wins; "Late 3d" chip on the part line.
    wo_number: 'WO-3003',
    part_number: 'PN-L1',
    status: 'in_progress',
    qty_complete: 8,
    qty_ordered: 40,
    promise_date: '2026-06-07',
    is_late: true,
    days_late: 3,
    blocked: false,
    down: false,
    running: true,
    ops_completed: 3,
    ops_total: 6,
    current_op: {
      sequence: 40,
      name: 'Deburr',
      work_center_code: 'DEB-1',
      work_center_name: 'Deburr 1',
      status: 'in_progress',
      qty_done: 8,
      qty_target: 40,
      crew: ['Ana P.'],
      crew_count: 1,
      elapsed_minutes: 30,
    },
  },
  {
    // RUNNING — crew suffix + live elapsed.
    wo_number: 'WO-3004',
    part_number: 'PN-77',
    status: 'in_progress',
    qty_complete: 12,
    qty_ordered: 50,
    promise_date: '2026-06-15',
    is_late: false,
    days_late: 0,
    blocked: false,
    down: false,
    running: true,
    ops_completed: 2,
    ops_total: 5,
    current_op: {
      sequence: 30,
      name: 'Mill',
      work_center_code: 'MILL-1',
      work_center_name: 'Mill 1',
      status: 'in_progress',
      qty_done: 12,
      qty_target: 50,
      crew: ['Jon W.', 'Sam K.'],
      crew_count: 2,
      elapsed_minutes: 75,
    },
  },
  {
    // Nothing on it yet — WAITING (slate).
    wo_number: 'WO-3005',
    part_number: 'PN-W1',
    status: 'released',
    qty_complete: 0,
    qty_ordered: 5,
    promise_date: null,
    is_late: false,
    days_late: 0,
    blocked: false,
    down: false,
    running: false,
    ops_completed: 0,
    ops_total: 2,
    current_op: {
      sequence: 10,
      name: 'Laser Cut',
      work_center_code: 'LASER-1',
      work_center_name: 'Laser 1',
      status: 'ready',
      qty_done: 0,
      qty_target: 5,
      crew: [],
      crew_count: 0,
      elapsed_minutes: 0,
    },
  },
];

/** Full NEW payload shape — every optional block present (jobs = job wall). */
const payload: WallboardResponse = {
  work_centers: [
    {
      id: 1,
      code: 'LASER-1',
      name: 'Laser 1',
      status: 'in_use',
      active_jobs: [
        {
          wo_number: 'WO-1001',
          part_number: 'PN-77',
          op_name: 'Laser Cut',
          operator_name: 'Jon W.',
          crew: ['Jon W.', 'Sam K.'],
          crew_count: 3,
          elapsed_minutes: 75,
          qty_done: 12,
          qty_target: 50,
          is_late: false,
        },
      ],
      queued_count: 3,
      blocked_count: 0,
      down: null,
    },
    {
      id: 2,
      code: 'WELD-2',
      name: 'Weld 2',
      status: 'available',
      active_jobs: [],
      queued_count: 0,
      blocked_count: 2,
      down: { category: 'mechanical', since: '2026-06-10T12:00:00Z', minutes: 18 },
    },
  ],
  late_wos: [
    {
      wo_number: 'WO-0999',
      part_number: 'PN-12',
      due_date: '2026-06-07',
      days_late: 3,
      status: 'in_progress',
    },
  ],
  blocked_wos: [{ wo_number: 'WO-0998', category: 'material_missing', age_hours: 5.5 }],
  // Deprecated block — even when an old backend still sends it, the strip
  // must NOT render (removed on owner feedback 2026-07-15).
  kpi_strip: {
    otd_ship_pct_30d: 96.2,
    fpy_pct_30d: 98.1,
    scrap_pct_30d: 1.2,
    open_wip_count: 47,
    avg_wip_age_days: 12.3,
  },
  late_total: 12,
  blocked_total: 4,
  down_total: 1,
  ship: {
    due_today: 5,
    shipped_today: 3,
    due_this_week: 11,
    due_today_rows: [{ wo_number: 'WO-1042', part_number: '4471-002', promise_date: '2026-06-10', qty_remaining: 4 }],
    next_due_date: null,
    next_due_count: 0,
  },
  today: {
    ops_completed: 27,
    pieces_completed: 342,
    wos_completed: 3,
    operators_on_clock: 9,
    hours_logged: 61.5,
    receipts: 4,
    scrap_events: 1,
  },
  quality: { open_ncr_count: 3, newest_ncr_age_days: 2, wos_on_hold: 1 },
  jobs,
  jobs_total: 7,
  generated_at: '2026-06-10T13:00:00Z',
};

/** New blocks present but NO `jobs` → the machine-tile FloorGrid renders. */
const machinePayload: WallboardResponse = { ...payload, jobs: undefined, jobs_total: undefined };

/** The OLD production payload shape — no new blocks, no crew, no is_late. */
const oldPayload: WallboardResponse = {
  work_centers: [
    {
      id: 1,
      code: 'LASER-1',
      name: 'Laser 1',
      status: 'in_use',
      active_jobs: [
        {
          wo_number: 'WO-1001',
          part_number: 'PN-77',
          op_name: 'Laser Cut',
          operator_name: 'Jon W.',
          elapsed_minutes: 75,
          qty_done: 12,
          qty_target: 50,
        },
      ],
      queued_count: 3,
      blocked_count: 0,
      down: null,
    },
    {
      id: 2,
      code: 'WELD-2',
      name: 'Weld 2',
      status: 'available',
      active_jobs: [],
      queued_count: 0,
      blocked_count: 2,
      down: { category: 'mechanical', since: '2026-06-10T12:00:00Z', minutes: 18 },
    },
  ],
  late_wos: [
    {
      wo_number: 'WO-0999',
      part_number: 'PN-12',
      due_date: '2026-06-07',
      days_late: 3,
      status: 'in_progress',
    },
  ],
  blocked_wos: [{ wo_number: 'WO-0998', category: 'material_missing', age_hours: 5.5 }],
  generated_at: '2026-06-10T13:00:00Z',
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
});

describe('Wallboard', () => {
  it('renders the job wall, tiles, exception rail, hero, and today band', async () => {
    mockFetchWallboard.mockResolvedValue(payload);
    renderWallboard();

    expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();
    expect(mockCapture).toHaveBeenCalled();

    // The wall is the JOB wall — machine tiles do not render alongside it.
    expect(screen.getByTestId('job-wall')).toBeInTheDocument();
    expect(screen.queryByTestId('wc-card-LASER-1')).not.toBeInTheDocument();

    // Hero: computed shop-state sentence from the true totals — red class of
    // problem leads, zero segments omitted.
    expect(screen.getByTestId('shop-state-headline')).toHaveTextContent('1 DOWN · 4 BLOCKED · 12 LATE');

    // SERVER priority order preserved — the client never re-sorts.
    const tileIds = screen.getAllByTestId(/^job-tile-WO/).map(el => el.getAttribute('data-testid'));
    expect(tileIds).toEqual([
      'job-tile-WO-3001',
      'job-tile-WO-3002',
      'job-tile-WO-3003',
      'job-tile-WO-3004',
      'job-tile-WO-3005',
    ]);

    // State bands, strict precedence: down > blocked > late > running > waiting.
    expect(screen.getByTestId('job-tile-header-WO-3001')).toHaveTextContent('DOWN');
    expect(screen.getByTestId('job-tile-header-WO-3002')).toHaveTextContent('BLOCKED');
    expect(screen.getByTestId('job-tile-header-WO-3003')).toHaveTextContent('LATE');
    expect(screen.getByTestId('job-tile-header-WO-3004')).toHaveTextContent('RUNNING');
    expect(screen.getByTestId('job-tile-header-WO-3005')).toHaveTextContent('WAITING');

    // RUNNING tile anatomy: WO number in the band; part + WO qty; THE ASK —
    // "Op n/total · op name · work center" with the crew suffix; live elapsed.
    const running = screen.getByTestId('job-tile-WO-3004');
    expect(screen.getByTestId('job-tile-header-WO-3004')).toHaveTextContent('WO-3004');
    expect(running).toHaveTextContent('PN-77');
    expect(running).toHaveTextContent('12/50');
    expect(running).toHaveTextContent('Op 3/5 · Mill · Mill 1 · Jon W. +1');
    expect(running).toHaveTextContent('1h15m');

    // LATE tile: "Late 3d" chip on the part line.
    expect(screen.getByTestId('job-tile-WO-3003')).toHaveTextContent('Late 3d');
    expect(screen.getByTestId('job-tile-WO-3003')).toHaveTextContent('Op 4/6 · Deburr · Deburr 1 · Ana P.');

    // DOWN tile body leads with the machine-down context in red.
    expect(screen.getByTestId('job-tile-WO-3001')).toHaveTextContent('Machine Down · Weld 2');

    // "+N more" from the uncapped jobs_total (7 total, 5 tiles).
    expect(screen.getByTestId('job-wall')).toHaveTextContent('+2 more work orders');

    // Rail P1 SHIP: shipped/due fraction, behind-state, week count, rows.
    const ship = screen.getByTestId('ship-panel');
    expect(ship).toHaveTextContent('3 / 5');
    expect(ship).toHaveTextContent('2 TO GO');
    expect(ship).toHaveTextContent('This Week 11');
    expect(ship).toHaveTextContent('WO-1042 · 4471-002');
    expect(ship).toHaveTextContent('+1 more today');

    // Rail P2 LATE: TRUE total (late_total=12), pinned rows, +N more.
    expect(screen.getByTestId('late-total')).toHaveTextContent('12');
    const late = screen.getByTestId('attention-late');
    expect(late).toHaveTextContent('3d');
    expect(late).toHaveTextContent('WO-0999');
    expect(late).toHaveTextContent('PN-12');
    expect(late).toHaveTextContent('+11 more');

    // Rail P3 BLOCKED · DOWN: twin true totals, down row first, +N more.
    expect(screen.getByTestId('blocked-total')).toHaveTextContent('4');
    expect(screen.getByTestId('down-total')).toHaveTextContent('1');
    const blockedDown = screen.getByTestId('attention-blocked-down');
    expect(blockedDown).toHaveTextContent('WELD-2 · mechanical');
    expect(blockedDown).toHaveTextContent('WO-0998 · material missing');
    expect(blockedDown).toHaveTextContent('+3 more');

    // Rail P4 QUALITY: counts + newest age.
    const quality = screen.getByTestId('quality-panel');
    expect(quality).toHaveTextContent('Open NCRs');
    expect(quality).toHaveTextContent('3');
    expect(quality).toHaveTextContent('newest 2d');
    expect(quality).toHaveTextContent('On Hold');

    // Z4: six TODAY cells; the PLANT 30d cluster is GONE (owner feedback) —
    // even though this payload still carries a kpi_strip block.
    const band = screen.getByTestId('today-band');
    expect(band).toHaveTextContent('27');
    expect(band).toHaveTextContent('342');
    expect(band).toHaveTextContent('61.5');
    expect(screen.queryByTestId('wallboard-kpi-strip')).not.toBeInTheDocument();
    expect(band).not.toHaveTextContent('Plant 30d');
    expect(band).not.toHaveTextContent('96.2%');

    // The ticker is gone, nothing scrolls, no offline chip when healthy.
    expect(screen.queryByTestId('ticker')).not.toBeInTheDocument();
    expect(document.querySelector('[class*="overflow-y-auto"]')).toBeNull();
    expect(document.querySelector('[class*="overflow-x-auto"]')).toBeNull();
    expect(screen.queryByTestId('offline-banner')).not.toBeInTheDocument();

    // First paint never flashes (new-event diff suppressed).
    expect(document.getElementsByClassName('wb-flash-new').length).toBe(0);
  });

  it('renders the job-wall empty state when there are no open work orders', async () => {
    mockFetchWallboard.mockResolvedValue({ ...payload, jobs: [], jobs_total: 0 });
    renderWallboard();

    expect(await screen.findByText('No open work orders')).toBeInTheDocument();
    expect(screen.getByTestId('job-wall')).toBeInTheDocument();
    // No grid, no machine-wall fallback — jobs IS present, just empty.
    expect(screen.queryByTestId('wallboard-grid')).not.toBeInTheDocument();
    expect(screen.queryByTestId('wc-card-LASER-1')).not.toBeInTheDocument();
  });

  it('renders the all-clear machine board: green hero, zero-line panels, idle strip', async () => {
    // Machine-wall (no jobs) fallback keeps its designed all-clear state.
    const clean: WallboardResponse = {
      ...machinePayload,
      work_centers: [
        machinePayload.work_centers[0],
        { ...machinePayload.work_centers[1], blocked_count: 0, down: null }, // now idle
      ],
      late_wos: [],
      blocked_wos: [],
      late_total: 0,
      blocked_total: 0,
      down_total: 0,
    };
    mockFetchWallboard.mockResolvedValue(clean);
    renderWallboard();

    expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();
    expect(screen.getByTestId('shop-state-headline')).toHaveTextContent('ALL SYSTEMS NORMAL');

    // P2 + P3 keep their slots and render green zero-lines (large clean-day mode).
    const zeroLines = screen.getAllByTestId('all-clear-line');
    expect(zeroLines).toHaveLength(2);
    expect(zeroLines[0]).toHaveTextContent('LATE 0 — ON TIME');
    expect(zeroLines[1]).toHaveTextContent('NOTHING BLOCKED OR DOWN');

    // The idle center leaves the grid for the idle strip, queue count kept.
    expect(screen.getByTestId('idle-strip')).toHaveTextContent('Idle 1');
    expect(screen.getByTestId('idle-strip')).toHaveTextContent('WELD-2');
    expect(screen.queryByTestId('wc-card-WELD-2')).not.toBeInTheDocument();
  });

  it('escalates the STEADY offline chip amber → red, keeps the last good data, and resets on recovery', async () => {
    jest.useFakeTimers();
    try {
      mockFetchWallboard.mockResolvedValueOnce(payload);
      renderWallboard();

      expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();

      // 1st failed poll → amber chip (level 1), last good data stays on screen.
      mockFetchWallboard.mockRejectedValue(new Error('HTTP_500'));
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      const chip = await screen.findByTestId('offline-banner');
      expect(chip).toHaveAttribute('data-offline-level', '1');
      // The chip carries the as-of time of the LAST GOOD poll: "Offline · h:mm AM/PM".
      expect(chip).toHaveTextContent(/Offline · \d{1,2}:\d{2}/);
      expect(screen.getByTestId('job-tile-WO-3001')).toBeInTheDocument();
      // Steady, never flashing — the flash class is reserved for new events.
      expect(chip.className).not.toContain('wb-flash');

      // 4th consecutive failure (~2 min) → red fill chip (level 2).
      await act(async () => {
        jest.advanceTimersByTime(90_000);
      });
      expect(screen.getByTestId('offline-banner')).toHaveAttribute('data-offline-level', '2');
      expect(screen.getByTestId('job-tile-WO-3001')).toBeInTheDocument();

      // Recovery: one good poll clears the chip AND resets the failure count…
      mockFetchWallboard.mockResolvedValue(payload);
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(screen.queryByTestId('offline-banner')).not.toBeInTheDocument();

      // …so the next single failure starts over at amber level 1, not red.
      mockFetchWallboard.mockRejectedValue(new Error('HTTP_500'));
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(screen.getByTestId('offline-banner')).toHaveAttribute('data-offline-level', '1');
    } finally {
      jest.useRealTimers();
    }
  });

  it('shows the revoked screen, clears the token, and stops polling on UNAUTHORIZED', async () => {
    jest.useFakeTimers();
    try {
      mockFetchWallboard.mockResolvedValueOnce(payload);
      renderWallboard();

      expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();

      // Next poll: the server rejects the (revoked/expired) display token.
      mockFetchWallboard.mockRejectedValue(new Error('UNAUTHORIZED'));
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });

      // Distinct full-screen state — NOT the generic offline chip over stale data.
      expect(await screen.findByTestId('revoked-screen')).toBeInTheDocument();
      expect(screen.getByText(/Display access revoked or expired/i)).toBeInTheDocument();
      // Copy points at the TV pairing flow — no more typing a #token= URL on a remote.
      expect(screen.getByText(/new display link or setup code in Admin Settings/i)).toBeInTheDocument();
      expect(screen.getByText(/open \/tv on this screen and enter the code/i)).toBeInTheDocument();
      expect(screen.queryByTestId('wallboard-grid')).not.toBeInTheDocument();
      expect(screen.queryByTestId('offline-banner')).not.toBeInTheDocument();

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

  it('passes ?dept= to the fetch helper, title-cases the chip, and tags plant-wide panels', async () => {
    mockFetchWallboard.mockResolvedValue({ ...payload, work_centers: [payload.work_centers[0]] });
    renderWallboard('/wallboard?dept=machining');

    await waitFor(() => expect(mockFetchWallboard).toHaveBeenCalledWith('machining'));
    // Title-cased, never the raw query param.
    expect(screen.getByTestId('dept-label')).toHaveTextContent('Machining');
    // SHIP + QUALITY stay plant-wide and say so.
    expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();
    expect(screen.getAllByText('Plant').length).toBeGreaterThanOrEqual(2);
  });

  it('shows guidance when no token is available', async () => {
    mockGetToken.mockReturnValue(null);
    mockFetchWallboard.mockRejectedValue(new Error('NO_TOKEN'));
    renderWallboard();

    expect(await screen.findByText('No display token')).toBeInTheDocument();
    // Leads with the /tv setup-code pairing flow; link + sign-in are fallbacks.
    expect(screen.getByText(/setup code from Admin Settings .* enter it at \/tv/i)).toBeInTheDocument();
  });

  it('falls back to the machine wall when `jobs` is absent (new blocks, no job wall)', async () => {
    mockFetchWallboard.mockResolvedValue(machinePayload);
    renderWallboard();

    expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();

    // The ORIGINAL machine-tile FloorGrid renders, unchanged.
    expect(screen.queryByTestId('job-wall')).not.toBeInTheDocument();
    const laser = screen.getByTestId('wc-card-LASER-1');
    expect(laser).toHaveTextContent('Laser 1');
    expect(screen.getByTestId('wc-tile-header-LASER-1')).toHaveTextContent('RUNNING');
    expect(screen.getByTestId('wc-tile-header-LASER-1')).toHaveTextContent('Q 3');
    expect(laser).toHaveTextContent('PN-77');
    expect(laser).toHaveTextContent('WO-1001 · Laser Cut · Jon W. +2');
    expect(laser).toHaveTextContent('12/50');
    expect(laser).toHaveTextContent('1h15m');

    // Down wins over blocked — red band; category + live minutes in the body.
    expect(screen.getByTestId('wc-tile-header-WELD-2')).toHaveTextContent('DOWN');
    expect(screen.getByTestId('wc-card-WELD-2')).toHaveTextContent('mechanical · 18m');
  });

  it('renders correctly against the OLD payload shape (back-compat, degraded machine wall)', async () => {
    mockFetchWallboard.mockResolvedValue(oldPayload);
    renderWallboard();

    expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();

    // No `jobs` block → the machine wall renders (old-backend grace).
    expect(screen.queryByTestId('job-wall')).not.toBeInTheDocument();
    expect(screen.getByTestId('wc-card-LASER-1')).toBeInTheDocument();
    expect(screen.getByTestId('wc-tile-header-WELD-2')).toHaveTextContent('DOWN');

    // Hero derives from work_centers + list lengths when totals are absent.
    expect(screen.getByTestId('shop-state-headline')).toHaveTextContent('1 DOWN · 1 BLOCKED · 1 LATE');

    // Rail totals fall back to list lengths / derived down count.
    expect(screen.getByTestId('late-total')).toHaveTextContent('1');
    expect(screen.getByTestId('blocked-total')).toHaveTextContent('1');
    expect(screen.getByTestId('down-total')).toHaveTextContent('1');

    // SHIP has no block → em-dash fraction; the panel keeps its slot.
    expect(screen.getByTestId('ship-panel')).toHaveTextContent('— / —');

    // QUALITY has no block → em-dash values.
    expect(screen.getByTestId('quality-panel')).toHaveTextContent('—');

    // TODAY band renders with em-dash values (no `today` block), and the
    // removed 30d cluster never comes back.
    expect(screen.getByTestId('today-band')).toHaveTextContent('—');
    expect(screen.queryByTestId('wallboard-kpi-strip')).not.toBeInTheDocument();

    // Job row falls back to operator_name when there is no crew array.
    expect(screen.getByTestId('wc-card-LASER-1')).toHaveTextContent('WO-1001 · Laser Cut · Jon W.');

    // No crash, no ticker, no flash on first paint.
    expect(screen.queryByTestId('ticker')).not.toBeInTheDocument();
    expect(document.getElementsByClassName('wb-flash-new').length).toBe(0);
  });

  it('flashes a newly-blocked job once, and never re-flashes unchanged jobs', async () => {
    jest.useFakeTimers();
    try {
      mockFetchWallboard.mockResolvedValueOnce(payload);
      renderWallboard();

      expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();
      // First paint: suppressed.
      expect(document.getElementsByClassName('wb-flash-new').length).toBe(0);

      // Next poll: the WAITING job newly enters BLOCKED → its band flashes.
      const withNewBlock: WallboardResponse = {
        ...payload,
        jobs: payload.jobs!.map(job => (job.wo_number === 'WO-3005' ? { ...job, blocked: true } : job)),
        generated_at: '2026-06-10T13:00:30Z',
      };
      mockFetchWallboard.mockResolvedValueOnce(withNewBlock);
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(screen.getByTestId('job-tile-header-WO-3005')).toHaveTextContent('BLOCKED');
      expect(screen.getByTestId('job-tile-header-WO-3005').className).toContain('wb-flash-new');
      // Already-blocked/down jobs do NOT re-flash — diff is by stable WO number.
      expect(screen.getByTestId('job-tile-header-WO-3001').className).not.toContain('wb-flash-new');
      expect(screen.getByTestId('job-tile-header-WO-3002').className).not.toContain('wb-flash-new');

      // Next poll with the same states → steady, no flash classes anywhere.
      mockFetchWallboard.mockResolvedValueOnce({
        ...withNewBlock,
        generated_at: '2026-06-10T13:01:00Z',
      });
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(document.getElementsByClassName('wb-flash-new').length).toBe(0);
    } finally {
      jest.useRealTimers();
    }
  });

  it('flashes a newly-down work center once on the fallback machine wall', async () => {
    jest.useFakeTimers();
    try {
      mockFetchWallboard.mockResolvedValueOnce(machinePayload);
      renderWallboard();

      expect(await screen.findByTestId('wallboard-grid')).toBeInTheDocument();
      // First paint: suppressed.
      expect(document.getElementsByClassName('wb-flash-new').length).toBe(0);

      // Next poll: LASER-1 newly enters DOWN → its header band flashes.
      const withNewDown: WallboardResponse = {
        ...machinePayload,
        work_centers: [
          {
            ...machinePayload.work_centers[0],
            down: { category: 'tooling', since: null, minutes: 1 },
          },
          machinePayload.work_centers[1],
        ],
        generated_at: '2026-06-10T13:00:30Z',
      };
      mockFetchWallboard.mockResolvedValueOnce(withNewDown);
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(screen.getByTestId('wc-tile-header-LASER-1').className).toContain('wb-flash-new');
      // The already-down center does NOT flash — diff is by stable ids.
      expect(screen.getByTestId('wc-tile-header-WELD-2').className).not.toContain('wb-flash-new');

      // Next poll with the same ids → steady, no flash classes anywhere.
      mockFetchWallboard.mockResolvedValueOnce({
        ...withNewDown,
        generated_at: '2026-06-10T13:01:00Z',
      });
      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });
      expect(document.getElementsByClassName('wb-flash-new').length).toBe(0);
    } finally {
      jest.useRealTimers();
    }
  });

  it('keeps the empty-state copy for a dept with zero work centers (machine wall)', async () => {
    mockFetchWallboard.mockResolvedValue({ ...machinePayload, work_centers: [] });
    renderWallboard('/wallboard?dept=bogus');

    // Same convention as the dept chip: title-cased, never the raw query param.
    expect(await screen.findByText('No active work centers for "Bogus"')).toBeInTheDocument();
  });
});
