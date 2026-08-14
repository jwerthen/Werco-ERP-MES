/**
 * Cross-page operation-number label consistency.
 *
 * The defect: `WorkOrderOperation.operation_number` is FREE TEXT the office
 * types — and `WorkOrderNew` itself mints it as `Op ${seq}` — so on
 * WO-20260807-006 the column literally holds the string "Op 10". Screens that
 * hard-coded a literal `Op ` prefix around it rendered "Op Op 10".
 *
 * The kiosk was fixed first (`formatOperationLabel`), but the defect was never
 * kiosk-specific: ShopFloor reads the SAME endpoint the kiosk queue does
 * (GET /shop-floor/work-center-queue/{id}) and doubled the prefix on the same
 * row the kiosk had just stopped doubling. This suite locks BOTH office screens
 * to the one shared helper, and locks the helper to ONE definition.
 *
 * The pages are rendered for real (not the helper called directly) because the
 * bug lived in JSX interpolation, not in the helper — a unit test of
 * `formatOperationLabel` passed the whole time these screens were broken.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import fs from 'fs';
import path from 'path';
import api from '../services/api';
import ShopFloor from './ShopFloor';
import DispatchBoard, { jobLabel } from './DispatchBoard';
import { ToastProvider } from '../components/ui';
import { formatOperationLabel as kioskFormatOperationLabel } from '../components/kiosk/kioskConstants';
import { formatOperationLabel, hasOperationNumber } from '../utils/operationLabel';
import type { DispatchBoardColumn, DispatchBoardRow } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    // ShopFloor
    getWorkCenters: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkOrder: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    updateWorkOrderPriority: jest.fn(),
    createWorkOrderBlocker: jest.fn(),
    // DispatchBoard
    getDispatchBoard: jest.fn(),
    setWorkCenterRunOrder: jest.fn(),
    updateOperation: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'operator', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/usePermissions', () => ({
  __esModule: true,
  usePermissions: () => ({ can: () => true, canAny: () => true, canAll: () => true, isAdmin: true }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockApi = api as jest.Mocked<typeof api>;

/** The exact stored value on WO-20260807-006 — the row that read "Op Op 10". */
const STORED = 'Op 10';

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

/**
 * A work-center-queue row. Shaped to `QueueItem` (types/index.ts) — the same
 * contract `api.getWorkCenterQueue` really resolves — so a mock that drifts from
 * the client is a compile error, not a green test.
 */
const queueItem = (overrides: Record<string, unknown> = {}) => ({
  operation_id: 101,
  work_order_id: 1001,
  work_order_number: 'WO-20260807-006',
  part_number: 'PN-441',
  part_name: 'Skid Fit',
  operation_number: STORED,
  operation_name: 'Skid Fit',
  status: 'ready',
  quantity_ordered: 10,
  quantity_complete: 0,
  priority: 5,
  due_date: '2099-01-05',
  setup_time_hours: 1,
  run_time_hours: 4,
  run_order: null,
  ...overrides,
});

const makeRow = (overrides: Partial<DispatchBoardRow> & { operation_id: number }): DispatchBoardRow => ({
  run_order: 1,
  version: 0,
  work_order_id: 1001,
  work_order_number: 'WO-20260807-006',
  operation_number: STORED,
  operation_name: 'Skid Fit',
  part_number: null,
  part_name: null,
  status: 'ready',
  priority: 5,
  due_date: null,
  quantity_ordered: 10,
  quantity_complete: 0,
  setup_time_hours: 0.5,
  run_time_hours: 1.25,
  laser_nest: null,
  ...overrides,
});

const board = (queue: DispatchBoardRow[]): { work_centers: DispatchBoardColumn[] } => ({
  work_centers: [
    { id: 7, name: 'CNC Mill 1', code: 'CNC-1', work_center_type: 'milling', is_active: true, queue },
  ],
});

describe('ShopFloor renders the operation number through the shared label helper', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.getWorkCenters.mockResolvedValue([workCenter]);
    mockApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  });

  it('renders a stored "Op 10" as "Op 10" — never "Op Op 10"', async () => {
    mockApi.getWorkCenterQueue.mockResolvedValue({ queue: [queueItem()] });
    render(
      <MemoryRouter>
        <ShopFloor />
      </MemoryRouter>
    );

    const cell = await screen.findByLabelText('Operation');
    expect(within(cell).getByText('Op 10')).toBeInTheDocument();
    expect(cell.textContent).not.toMatch(/Op\s+Op/i);
  });

  it('normalizes every stored spelling of operation 10 to the same "Op 10"', async () => {
    mockApi.getWorkCenterQueue.mockResolvedValue({
      queue: [
        queueItem({ operation_id: 101, operation_number: '10' }),
        queueItem({ operation_id: 102, operation_number: 'OP10' }),
        queueItem({ operation_id: 103, operation_number: 'op-10' }),
        queueItem({ operation_id: 104, operation_number: 'Operation 10' }),
        queueItem({ operation_id: 105, operation_number: STORED }),
      ],
    });
    render(
      <MemoryRouter>
        <ShopFloor />
      </MemoryRouter>
    );

    const cells = await screen.findAllByLabelText('Operation');
    expect(cells).toHaveLength(5);
    cells.forEach((cell) => {
      expect(within(cell).getByText('Op 10')).toBeInTheDocument();
      expect(cell.textContent).not.toMatch(/Op\s+Op/i);
    });
  });

  it('shows the em-dash, not a dangling "Op ", when the office left the number blank', async () => {
    mockApi.getWorkCenterQueue.mockResolvedValue({ queue: [queueItem({ operation_number: '' })] });
    render(
      <MemoryRouter>
        <ShopFloor />
      </MemoryRouter>
    );

    const cell = await screen.findByLabelText('Operation');
    expect(within(cell).getByText('Op —')).toBeInTheDocument();
  });
});

describe('DispatchBoard renders the operation number through the shared label helper', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  const renderBoard = () =>
    render(
      <MemoryRouter>
        <ToastProvider>
          <DispatchBoard />
        </ToastProvider>
      </MemoryRouter>
    );

  it('renders a stored "Op 10" on the card as "Op 10" — never "Op Op 10"', async () => {
    mockApi.getDispatchBoard.mockResolvedValue(board([makeRow({ operation_id: 11 })]));
    renderBoard();

    const card = await screen.findByTestId('dispatch-rank-11');
    const row = card.parentElement as HTMLElement;
    expect(row.textContent).toContain('Op 10 · Skid Fit');
    expect(row.textContent).not.toMatch(/Op\s+Op/i);
  });

  it('omits the operation segment entirely — separator included — when the number is blank', async () => {
    mockApi.getDispatchBoard.mockResolvedValue(
      board([makeRow({ operation_id: 12, operation_number: '' })])
    );
    renderBoard();

    const card = await screen.findByTestId('dispatch-rank-12');
    const row = card.parentElement as HTMLElement;
    // The board's empty-state is "no segment", NOT the kiosk's em-dash — a
    // dangling "Op  · " is exactly what the old `!= null` guard emitted.
    expect(row.textContent).not.toContain('Op —');
    expect(row.textContent).not.toMatch(/Op\s+·/);
    expect(row.textContent).toContain('Skid Fit');
  });

  it('jobLabel() — the drag announcement and aria-label — never doubles the prefix', () => {
    // operation_name is null so the label falls through to the number, which is
    // the branch that used to hard-code its own `Op ` prefix.
    const row = makeRow({ operation_id: 13, operation_name: null });
    expect(jobLabel(row)).toBe('WO-20260807-006 Op 10');
    expect(jobLabel(row)).not.toMatch(/Op\s+Op/i);
  });

  it('jobLabel() keeps the word "Operation" — not "Op —" — when nothing names the operation', () => {
    // This string is read aloud in the aria-live reorder announcements, so the
    // kiosk em-dash would be a regression here.
    expect(jobLabel(makeRow({ operation_id: 14, operation_name: null, operation_number: null }))).toBe(
      'WO-20260807-006 Operation'
    );
    // An empty string used to slip past the old `!= null` guard and announce "Op ".
    expect(jobLabel(makeRow({ operation_id: 15, operation_name: null, operation_number: '' }))).toBe(
      'WO-20260807-006 Operation'
    );
  });
});

describe('the operation-label helper has exactly one definition', () => {
  it('the kiosk barrel re-exports the util — it is the SAME function, not a copy', () => {
    expect(kioskFormatOperationLabel).toBe(formatOperationLabel);
  });

  it('only src/utils/operationLabel.ts implements the prefix normalization', () => {
    const srcDir = path.join(__dirname, '..');
    const walk = (dir: string): string[] =>
      fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) return walk(full);
        return /\.tsx?$/.test(entry.name) ? [full] : [];
      });

    // The `function formatOperationLabel` DECLARATION, not the re-export or the
    // call sites. A second copy of the parsing is how the office and floor
    // spellings drifted apart in the first place.
    const definers = walk(srcDir).filter((file) =>
      /(export\s+)?function\s+formatOperationLabel\s*\(/.test(fs.readFileSync(file, 'utf8'))
    );

    expect(definers.map((f) => path.relative(srcDir, f))).toEqual(['utils/operationLabel.ts']);
  });

  it('hasOperationNumber agrees with formatOperationLabel on what counts as blank', () => {
    // It delegates rather than re-testing the regexes, so this can never drift —
    // the test states the contract the call-site guards rely on.
    [null, undefined, '', '   ', 'Op', 'OPERATION', 'OP-'].forEach((blank) => {
      expect(hasOperationNumber(blank)).toBe(false);
      expect(formatOperationLabel(blank)).toBe('Op —');
    });
    // `0` is a real operation number that a bare truthiness check would drop.
    [0, '0', '10', STORED, 'A10', 'FINAL'].forEach((named) => {
      expect(hasOperationNumber(named)).toBe(true);
      expect(formatOperationLabel(named)).not.toBe('Op —');
    });
  });
});
