/**
 * Dispatch Board — manager-controlled run order.
 *
 * Covers:
 *  - the board renders one column per work center from the mocked response,
 *    with ranks, an empty (idle) column, and server order preserved;
 *  - the keyboard path (Move up / Move down buttons) reorders optimistically and
 *    PUTs the full new `operation_ids` order for that work center, announcing the
 *    resulting position on the aria-live status line;
 *  - a rejected reorder rolls the board back and surfaces the server's `detail`;
 *  - the machine select performs a cross-machine move via `updateOperation` with
 *    `work_center_id` + `version`, and a 409 is surfaced VERBATIM with the board
 *    left unchanged (non-optimistic);
 *  - an in-progress card is held in place: its controls are disabled and it isn't
 *    draggable, and the tooltips claim only what is actually enforced;
 *  - the HTML5 drag path drops BEFORE the target card within a column, and onto
 *    another column routes through the same server-gated cross-machine move;
 *  - drop geometry: the gap between two cards inserts BETWEEN them (it does not
 *    silently append), the tail is only chosen below the last card, and the slot
 *    is shown as a drop line before release;
 *  - the reorder seq guard is PER COLUMN — reordering one column must not discard
 *    another column's authoritative reconcile;
 *  - a refused reorder re-reads the board instead of trusting a client snapshot,
 *    and a second reorder can't stack on an unresolved one;
 *  - a cross-machine move whose re-read fails reports the failure, not success;
 *  - a keyboard reorder that disables the pressed button keeps focus in the card;
 *  - SCALE (24 work centers): machines with work render BEFORE idle ones, the idle
 *    ones are collapsed behind a disclosure by default (aria-expanded / aria-controls),
 *    an all-idle board still renders with no disclosure at all, the header reports
 *    the busy/idle split, hidden idle machines stay in every card's move list, and
 *    the Scroll left/right buttons go inert at each end of the board;
 *  - laser nests: the card carries the material/thickness/sheet/sheets-left line,
 *    a material or thickness change is marked between the two cards that cause it
 *    (never above the first card or against a non-nest job), the column header
 *    summarises "N nests · M changeovers", and that count follows the optimistic
 *    reorder so batching shows its payoff immediately.
 *
 * services/api and usePermissions are mocked at the module boundary.
 */

import React from 'react';
import { act, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import DispatchBoard, { insertionIndexFromPointer } from './DispatchBoard';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import type { DispatchBoardColumn, DispatchBoardRow } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getDispatchBoard: jest.fn(),
    setWorkCenterRunOrder: jest.fn(),
    updateOperation: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  __esModule: true,
  usePermissions: () => ({ can: () => true, canAny: () => true, canAll: () => true, isAdmin: true }),
}));

const mockApi = api as jest.Mocked<typeof api>;

const makeRow = (overrides: Partial<DispatchBoardRow> & { operation_id: number }): DispatchBoardRow => ({
  run_order: null,
  version: 0,
  work_order_id: 7,
  work_order_number: 'WO-20260720-001',
  operation_number: '10',
  operation_name: 'Operation',
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

const LASER_ROWS: DispatchBoardRow[] = [
  makeRow({
    operation_id: 11,
    run_order: 1,
    work_order_number: 'WO-20260720-001',
    operation_number: 'Nest 1',
    operation_name: 'Laser Cut - nest-p001',
    due_date: '2026-07-24',
    laser_nest: {
      cnc_number: 'nest-p001',
      material: 'A36',
      thickness: '0.25in',
      sheet_size: '48x96',
      planned_runs: 5,
      completed_runs: 2,
      remaining_runs: 3,
    },
  }),
  makeRow({
    operation_id: 9,
    run_order: 2,
    version: 3,
    work_order_id: 5,
    work_order_number: 'WO-20260719-004',
    operation_number: 'Nest 2',
    operation_name: 'Laser Cut - nest-p002',
    part_number: 'PN-2231',
    part_name: 'Bracket',
    // Same thickness, different material -> a material-only changeover from Nest 1.
    laser_nest: {
      cnc_number: 'nest-p002',
      material: '304SS',
      thickness: '0.25in',
      sheet_size: '48x96',
      planned_runs: 2,
      completed_runs: 0,
      remaining_runs: 2,
    },
  }),
  makeRow({
    operation_id: 14,
    work_order_id: 8,
    work_order_number: 'WO-20260720-003',
    operation_number: '20',
    operation_name: 'Deburr',
  }),
];

const MILL_ROWS: DispatchBoardRow[] = [
  makeRow({
    operation_id: 21,
    run_order: 1,
    status: 'in_progress',
    work_order_id: 4,
    work_order_number: 'WO-20260718-002',
    operation_number: '30',
    operation_name: 'Mill Face',
  }),
];

/** Two reorderable ready rows on the mill, for the per-column seq-guard test. */
const MILL_QUEUE_ROWS: DispatchBoardRow[] = [
  makeRow({
    operation_id: 31,
    run_order: 1,
    work_order_id: 12,
    work_order_number: 'WO-20260718-005',
    operation_number: '10',
    operation_name: 'Drill',
  }),
  makeRow({
    operation_id: 32,
    run_order: 2,
    work_order_id: 13,
    work_order_number: 'WO-20260718-006',
    operation_number: '20',
    operation_name: 'Tap',
  }),
];

const board = (): { work_centers: DispatchBoardColumn[] } => ({
  work_centers: [
    { id: 2, name: 'Ermaksan Fiber Laser', code: 'ERM-FL', work_center_type: 'laser', queue: LASER_ROWS.map((r) => ({ ...r })) },
    { id: 5, name: 'Haas VF-2', code: 'HAAS-2', work_center_type: 'milling', queue: MILL_ROWS.map((r) => ({ ...r })) },
    { id: 7, name: 'Press Brake 1', code: 'PB-1', work_center_type: 'forming', queue: [] },
  ],
});

/**
 * Four nests on one laser, ordered so the current sequence costs THREE
 * changeovers and moving one card removes one — the feedback loop under test.
 */
const NEST_ROWS: DispatchBoardRow[] = [
  makeRow({
    operation_id: 101,
    run_order: 1,
    work_order_number: 'WO-N-101',
    operation_name: 'Laser Cut - n101',
    laser_nest: { material: 'A36', thickness: '0.25in', planned_runs: 1, completed_runs: 0, remaining_runs: 1 },
  }),
  makeRow({
    operation_id: 102,
    run_order: 2,
    work_order_number: 'WO-N-102',
    operation_name: 'Laser Cut - n102',
    laser_nest: { material: '304SS', thickness: '0.25in', planned_runs: 1, completed_runs: 0, remaining_runs: 1 },
  }),
  makeRow({
    operation_id: 103,
    run_order: 3,
    work_order_number: 'WO-N-103',
    operation_name: 'Laser Cut - n103',
    // Whitespace + casing noise: still the SAME material as 101.
    laser_nest: { material: ' a36 ', thickness: '0.25in', planned_runs: 1, completed_runs: 0, remaining_runs: 1 },
  }),
  makeRow({
    operation_id: 104,
    run_order: 4,
    work_order_number: 'WO-N-104',
    operation_name: 'Laser Cut - n104',
    laser_nest: { material: 'A36', thickness: '0.5in', planned_runs: 1, completed_runs: 0, remaining_runs: 1 },
  }),
];

const nestBoard = (): { work_centers: DispatchBoardColumn[] } => ({
  work_centers: [
    {
      id: 2,
      name: 'Ermaksan Fiber Laser',
      code: 'ERM-FL',
      work_center_type: 'laser',
      queue: NEST_ROWS.map((r) => ({ ...r })),
    },
  ],
});

/** Same board, but the mill column is reorderable too (two ready rows). */
const twoQueueBoard = (): { work_centers: DispatchBoardColumn[] } => ({
  work_centers: [
    {
      id: 2,
      name: 'Ermaksan Fiber Laser',
      code: 'ERM-FL',
      work_center_type: 'laser',
      queue: LASER_ROWS.map((r) => ({ ...r })),
    },
    {
      id: 5,
      name: 'Haas VF-2',
      code: 'HAAS-2',
      work_center_type: 'milling',
      queue: MILL_QUEUE_ROWS.map((r) => ({ ...r })),
    },
  ],
});

/**
 * The real shop's shape in miniature: the idle machines sort FIRST in code order
 * (ASM-01, BND-01…), which is exactly what pushed every actual job off the right
 * edge of the board.
 */
const scaleBoard = (): { work_centers: DispatchBoardColumn[] } => ({
  work_centers: [
    { id: 1, name: 'Assembly 1', code: 'ASM-01', work_center_type: 'assembly', queue: [] },
    { id: 3, name: 'Bandsaw 1', code: 'BND-01', work_center_type: 'sawing', queue: [] },
    {
      id: 2,
      name: 'Ermaksan Fiber Laser',
      code: 'ERM-FL',
      work_center_type: 'laser',
      queue: LASER_ROWS.map((r) => ({ ...r })),
    },
    { id: 6, name: 'Grinder 1', code: 'GRD-01', work_center_type: 'grinding', queue: [] },
    { id: 5, name: 'Haas VF-2', code: 'HAAS-2', work_center_type: 'milling', queue: MILL_ROWS.map((r) => ({ ...r })) },
  ],
});

/** Every machine quiet — the board must still render them, with no disclosure. */
const allIdleBoard = (): { work_centers: DispatchBoardColumn[] } => ({
  work_centers: [
    { id: 1, name: 'Assembly 1', code: 'ASM-01', work_center_type: 'assembly', queue: [] },
    { id: 3, name: 'Bandsaw 1', code: 'BND-01', work_center_type: 'sawing', queue: [] },
  ],
});

/** Column regions in DOM order — the partition is an ORDER claim. */
const columnOrder = (): string[] =>
  screen
    .getAllByRole('region')
    .map((el) => el.getAttribute('aria-label') || '')
    .filter((label) => label.endsWith('run order'));

/**
 * jsdom lays nothing out, so the scroll geometry the buttons key off has to be
 * stubbed. `scrollBy` is stubbed too — jsdom does not implement it.
 */
const stubScrollGeometry = (scrollLeft: number, scrollWidth = 3000, clientWidth = 1000) => {
  const el = screen.getByTestId('dispatch-board-scroll');
  Object.defineProperty(el, 'scrollLeft', { value: scrollLeft, configurable: true, writable: true });
  Object.defineProperty(el, 'scrollWidth', { value: scrollWidth, configurable: true });
  Object.defineProperty(el, 'clientWidth', { value: clientWidth, configurable: true });
  const scrollBy = jest.fn();
  (el as HTMLElement & { scrollBy: jest.Mock }).scrollBy = scrollBy;
  return { el, scrollBy };
};

const cardOrder = (workCenterId: number): number[] =>
  Array.from(
    screen.getByTestId(`dispatch-column-${workCenterId}`).querySelectorAll('[data-testid^="dispatch-card-"]')
  ).map((el) => Number(el.getAttribute('data-testid')!.replace('dispatch-card-', '')));

/** The columns' work-center names also appear inside every machine <select>, so
 *  address columns by their labelled region, not by bare text. */
const findColumn = (name: string) => screen.findByRole('region', { name: `${name} run order` });

/** jsdom has no DataTransfer — the handlers only touch effectAllowed/dropEffect/setData. */
const makeDataTransfer = () => ({ effectAllowed: '', dropEffect: '', setData: jest.fn(), getData: jest.fn() });

/**
 * jsdom implements neither `DragEvent` nor pointer coordinates on the `Event`
 * fallback testing-library uses in its place, so `clientY` has to be pinned onto
 * the native event by hand. Without this every drag reads as y=undefined and the
 * geometry under test never runs.
 */
const fireDrag = (kind: 'dragStart' | 'dragOver' | 'drop', element: Element, clientY = 0) => {
  const event = createEvent[kind](element, { dataTransfer: makeDataTransfer() });
  Object.defineProperty(event, 'clientY', { value: clientY });
  fireEvent(element, event);
};

const CARD_HEIGHT = 90;
const CARD_GAP = 10;

/**
 * jsdom lays nothing out — every rect is 0×0 — so the drop geometry can only be
 * exercised by stubbing the cards' rects. Card i occupies [i*100, i*100+90], so
 * the 10px gap under card i is y ∈ (i*100+90, i*100+100).
 */
const stubCardGeometry = (workCenterId: number) => {
  const container = screen.getByTestId(`dispatch-column-${workCenterId}`);
  container.querySelectorAll<HTMLElement>('[data-dispatch-card]').forEach((card, index) => {
    const top = index * (CARD_HEIGHT + CARD_GAP);
    card.getBoundingClientRect = () =>
      ({
        top,
        bottom: top + CARD_HEIGHT,
        height: CARD_HEIGHT,
        left: 0,
        right: 320,
        width: 320,
        x: 0,
        y: top,
        toJSON: () => ({}),
      }) as DOMRect;
  });
};

const renderBoard = () =>
  render(
    <MemoryRouter>
      <ToastProvider>
        <DispatchBoard />
      </ToastProvider>
    </MemoryRouter>
  );

describe('DispatchBoard', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.getDispatchBoard.mockResolvedValue(board());
  });

  it('renders a column per machine WITH WORK, with ranks and cards in server order', async () => {
    renderBoard();

    expect(await findColumn('Ermaksan Fiber Laser')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Haas VF-2 run order' })).toBeInTheDocument();
    // Press Brake 1 is idle, so it starts behind the disclosure rather than
    // taking a 320px slice of the first screen.
    expect(screen.queryByRole('region', { name: 'Press Brake 1 run order' })).not.toBeInTheDocument();

    // Server order is preserved (ranked first, unranked after) — no client sort.
    expect(cardOrder(2)).toEqual([11, 9, 14]);

    expect(screen.getByTestId('dispatch-rank-11')).toHaveTextContent('1');
    expect(screen.getByTestId('dispatch-rank-9')).toHaveTextContent('2');
    // Unranked rows show a dash, not a fabricated rank.
    expect(screen.getByTestId('dispatch-rank-14')).toHaveTextContent('–');

    expect(screen.getByText(/Laser Cut - nest-p001/)).toBeInTheDocument();
    expect(screen.getByText('PN-2231')).toBeInTheDocument();
  });

  it('Move down reorders optimistically, PUTs the full operation_ids order, and announces the new position', async () => {
    const user = userEvent.setup();
    // Hold the PUT open so the pre-server (optimistic) DOM can be asserted.
    let resolvePut: (value: DispatchBoardRow[]) => void = () => undefined;
    mockApi.setWorkCenterRunOrder.mockReturnValue(
      new Promise<DispatchBoardRow[]>((resolve) => {
        resolvePut = resolve;
      })
    );
    renderBoard();

    await findColumn('Ermaksan Fiber Laser');
    await user.click(screen.getByLabelText('Move WO-20260720-001 Laser Cut - nest-p001 down'));

    // Optimistic: the DOM shows the new order (and re-ranks) before the server answers.
    expect(cardOrder(2)).toEqual([9, 11, 14]);
    expect(screen.getByTestId('dispatch-rank-11')).toHaveTextContent('2');
    expect(screen.getByTestId('dispatch-rank-9')).toHaveTextContent('1');
    expect(screen.getByTestId('dispatch-status')).toHaveTextContent(
      'WO-20260720-001 Laser Cut - nest-p001 moved to position 2 of 3 on Ermaksan Fiber Laser'
    );

    // The PUT carries the FULL new id order for that work center.
    expect(mockApi.setWorkCenterRunOrder).toHaveBeenCalledTimes(1);
    expect(mockApi.setWorkCenterRunOrder).toHaveBeenCalledWith(2, [9, 11, 14]);

    // The server's refreshed queue reconciles the optimistic guess.
    const refreshed = [
      { ...LASER_ROWS[1], run_order: 1 },
      { ...LASER_ROWS[0], run_order: 2 },
      { ...LASER_ROWS[2], run_order: 3 },
    ];
    await act(async () => {
      resolvePut(refreshed);
    });
    expect(cardOrder(2)).toEqual([9, 11, 14]);
    expect(screen.getByTestId('dispatch-rank-14')).toHaveTextContent('3');
  });

  it('Move up is disabled for the first card and Move down for the last', async () => {
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    expect(screen.getByLabelText('Move WO-20260720-001 Laser Cut - nest-p001 up')).toBeDisabled();
    expect(screen.getByLabelText('Move WO-20260720-003 Deburr down')).toBeDisabled();
    expect(screen.getByLabelText('Move WO-20260720-003 Deburr up')).toBeEnabled();
  });

  it('rolls the board back, shows the server detail verbatim, and RE-READS the board when a reorder is refused', async () => {
    const user = userEvent.setup();
    mockApi.setWorkCenterRunOrder.mockRejectedValue({
      response: { status: 400, data: { detail: 'Operation 11 is not queued at work center 2' } },
    });
    renderBoard();

    await findColumn('Ermaksan Fiber Laser');
    await user.click(screen.getByLabelText('Move WO-20260720-001 Laser Cut - nest-p001 down'));

    expect(await screen.findByText('Operation 11 is not queued at work center 2')).toBeInTheDocument();
    await waitFor(() => expect(cardOrder(2)).toEqual([11, 9, 14]));
    expect(screen.getByTestId('dispatch-rank-11')).toHaveTextContent('1');
    // The restored snapshot is a client guess — the column is re-read from the
    // server rather than left sitting on it indefinitely.
    await waitFor(() => expect(mockApi.getDispatchBoard).toHaveBeenCalledTimes(2));
  });

  it('raises a retryable stale banner when the re-read after a refused reorder also fails', async () => {
    const user = userEvent.setup();
    mockApi.setWorkCenterRunOrder.mockRejectedValue({ response: { data: { detail: 'Run order rejected' } } });
    mockApi.getDispatchBoard.mockResolvedValueOnce(board()).mockRejectedValueOnce({
      response: { data: { detail: 'Board read failed' } },
    });
    renderBoard();

    await findColumn('Ermaksan Fiber Laser');
    await user.click(screen.getByLabelText('Move WO-20260720-001 Laser Cut - nest-p001 down'));

    const notice = await screen.findByTestId('dispatch-stale-notice');
    expect(notice).toHaveTextContent('This board may be out of date');
    expect(notice).toHaveTextContent('Board read failed');

    // The banner's Retry re-reads, and a successful read clears it.
    mockApi.getDispatchBoard.mockResolvedValue(board());
    await user.click(within(notice).getByRole('button', { name: 'Retry refresh' }));
    await waitFor(() => expect(screen.queryByTestId('dispatch-stale-notice')).not.toBeInTheDocument());
  });

  it('does not let a second reorder of the same column stack on an unresolved one', async () => {
    const user = userEvent.setup();
    mockApi.setWorkCenterRunOrder.mockReturnValue(new Promise(() => undefined)); // never settles
    renderBoard();

    await findColumn('Ermaksan Fiber Laser');
    const down = screen.getByLabelText('Move WO-20260720-001 Laser Cut - nest-p001 down');
    await user.click(down);
    expect(mockApi.setWorkCenterRunOrder).toHaveBeenCalledTimes(1);

    // Still focusable (so focus is never yanked mid-action) but inert while the
    // first reorder is unresolved.
    expect(down).toHaveAttribute('aria-disabled', 'true');
    expect(down).toBeEnabled();
    await user.click(down);
    expect(mockApi.setWorkCenterRunOrder).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('dispatch-card-11')).toHaveAttribute('draggable', 'false');
  });

  it("reordering one column does not discard another column's authoritative reconcile", async () => {
    const user = userEvent.setup();
    mockApi.getDispatchBoard.mockResolvedValue(twoQueueBoard());
    const resolvers = new Map<number, (queue: DispatchBoardRow[]) => void>();
    mockApi.setWorkCenterRunOrder.mockImplementation(
      (workCenterId: number) =>
        new Promise<DispatchBoardRow[]>((resolve) => {
          resolvers.set(workCenterId, resolve);
        })
    );
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    // Column A (laser) reorder goes in flight...
    await user.click(screen.getByLabelText('Move WO-20260720-001 Laser Cut - nest-p001 down'));
    // ...then an unrelated column B (mill) reorder is started before A answers.
    await user.click(screen.getByLabelText('Move WO-20260718-005 Drill down'));
    expect(resolvers.has(2)).toBe(true);
    expect(resolvers.has(5)).toBe(true);

    // A's server answer is authoritative and must still be applied: with a
    // board-global seq counter, B's reorder made this reconcile a no-op and the
    // column kept client-side ranks and stale versions.
    const serverQueue = [
      { ...LASER_ROWS[2], run_order: 1 },
      { ...LASER_ROWS[1], run_order: 2 },
      { ...LASER_ROWS[0], run_order: 3 },
    ];
    await act(async () => {
      resolvers.get(2)!(serverQueue);
    });
    expect(cardOrder(2)).toEqual([14, 9, 11]);
    expect(screen.getByTestId('dispatch-rank-14')).toHaveTextContent('1');
  });

  it('moves a card across machines with work_center_id + version and refetches the board', async () => {
    const user = userEvent.setup();
    mockApi.updateOperation.mockResolvedValue({});
    renderBoard();

    await findColumn('Ermaksan Fiber Laser');
    await user.selectOptions(
      screen.getByLabelText('Move WO-20260719-004 Laser Cut - nest-p002 to another machine'),
      '5'
    );

    await waitFor(() => expect(mockApi.updateOperation).toHaveBeenCalledWith(9, { work_center_id: 5, version: 3 }));
    // Non-optimistic: the board only changes after a successful re-read.
    await waitFor(() => expect(mockApi.getDispatchBoard).toHaveBeenCalledTimes(2));
    expect(mockApi.setWorkCenterRunOrder).not.toHaveBeenCalled();
  });

  it('surfaces a refused cross-machine move verbatim and leaves the board unchanged', async () => {
    const user = userEvent.setup();
    mockApi.updateOperation.mockRejectedValue({
      response: { status: 409, data: { detail: 'Cannot move an operation that is in progress' } },
    });
    renderBoard();

    await findColumn('Ermaksan Fiber Laser');
    await user.selectOptions(
      screen.getByLabelText('Move WO-20260719-004 Laser Cut - nest-p002 to another machine'),
      '5'
    );

    expect(await screen.findByText('Cannot move an operation that is in progress')).toBeInTheDocument();
    expect(cardOrder(2)).toEqual([11, 9, 14]);
    expect(mockApi.getDispatchBoard).toHaveBeenCalledTimes(1);
  });

  it('holds an in-progress card in place, and its tooltips claim only what is enforced', async () => {
    renderBoard();
    await findColumn('Haas VF-2');

    const runningCard = screen.getByTestId('dispatch-card-21');
    expect(within(runningCard).getByText('Running')).toBeInTheDocument();
    expect(runningCard).toHaveAttribute('draggable', 'false');

    const up = screen.getByLabelText('Move WO-20260718-002 Mill Face up');
    const down = screen.getByLabelText('Move WO-20260718-002 Mill Face down');
    const machineSelect = screen.getByLabelText('Move WO-20260718-002 Mill Face to another machine');
    expect(up).toBeDisabled();
    expect(down).toBeDisabled();
    expect(machineSelect).toBeDisabled();

    // The reorder controls describe a BOARD rule (the server does not refuse a
    // re-rank of a running op, and neighbours really do shift its position)...
    expect(up).toHaveAttribute(
      'title',
      "This job is running, so the board won't pick it up. Its position still shifts as the jobs around it are reordered."
    );
    // ...while only the cross-machine control claims a server refusal, which is
    // the one thing the server actually enforces (409 "Clock out before moving").
    expect(machineSelect).toHaveAttribute(
      'title',
      'This job is running. The server refuses to move an in-progress operation — clock out first.'
    );
    expect(within(runningCard).getByText('Held in place while running')).toBeInTheDocument();
  });

  it('drag within a column reorders and PUTs the new order (drop lands BEFORE the target card)', async () => {
    mockApi.setWorkCenterRunOrder.mockResolvedValue([]);
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');
    stubCardGeometry(2);

    const dragged = screen.getByTestId('dispatch-card-14'); // last, unranked
    const target = screen.getByTestId('dispatch-card-11'); // first, y 0..90
    fireDrag('dragStart', dragged);
    // Released in the TOP half of card 11 -> insert before it.
    fireDrag('dragOver', target, 20);
    fireDrag('drop', target, 20);

    await waitFor(() => expect(mockApi.setWorkCenterRunOrder).toHaveBeenCalledWith(2, [14, 11, 9]));
  });

  it('dropping in the GAP between two cards inserts between them, not at the end of the column', async () => {
    mockApi.setWorkCenterRunOrder.mockReturnValue(new Promise(() => undefined));
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');
    stubCardGeometry(2);

    const dragged = screen.getByTestId('dispatch-card-14'); // last card
    const surface = screen.getByTestId('dispatch-column-2');
    fireDrag('dragStart', dragged);
    // y=95 is the 8px gap between card 11 (0..90) and card 9 (100..190) — the
    // column's own drop surface, which used to mean "append to the tail".
    fireDrag('dragOver', surface, 95);
    // The slot is shown before release, tail slots included.
    expect(screen.getByTestId('dispatch-drop-line-2-1')).toBeInTheDocument();

    fireDrag('drop', surface, 95);
    await waitFor(() => expect(mockApi.setWorkCenterRunOrder).toHaveBeenCalledWith(2, [11, 14, 9]));
  });

  it('dropping below the last card is what appends to the end of the column', async () => {
    mockApi.setWorkCenterRunOrder.mockReturnValue(new Promise(() => undefined));
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');
    stubCardGeometry(2);

    const dragged = screen.getByTestId('dispatch-card-11'); // first card
    const surface = screen.getByTestId('dispatch-column-2');
    fireDrag('dragStart', dragged);
    fireDrag('dragOver', surface, 400);
    expect(screen.getByTestId('dispatch-drop-line-2-3')).toBeInTheDocument(); // the tail slot

    fireDrag('drop', surface, 400);
    await waitFor(() => expect(mockApi.setWorkCenterRunOrder).toHaveBeenCalledWith(2, [9, 14, 11]));
  });

  it('drag onto another column performs the server-gated cross-machine move', async () => {
    const user = userEvent.setup();
    mockApi.updateOperation.mockResolvedValue({});
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');
    // A revealed idle machine is a full drop target, not a summary.
    await user.click(screen.getByTestId('dispatch-idle-toggle'));

    const dragged = screen.getByTestId('dispatch-card-9');
    const idleColumn = screen.getByTestId('dispatch-column-7');
    fireDrag('dragStart', dragged);
    fireDrag('dragOver', idleColumn, 40);
    fireDrag('drop', idleColumn, 40);

    await waitFor(() => expect(mockApi.updateOperation).toHaveBeenCalledWith(9, { work_center_id: 7, version: 3 }));
    expect(mockApi.setWorkCenterRunOrder).not.toHaveBeenCalled();
  });

  it('does NOT report a successful cross-machine move when the board re-read fails', async () => {
    const user = userEvent.setup();
    mockApi.updateOperation.mockResolvedValue({});
    mockApi.getDispatchBoard
      .mockResolvedValueOnce(board())
      .mockRejectedValueOnce({ response: { data: { detail: 'Board read failed' } } });
    renderBoard();

    await findColumn('Ermaksan Fiber Laser');
    await user.selectOptions(
      screen.getByLabelText('Move WO-20260719-004 Laser Cut - nest-p002 to another machine'),
      '5'
    );

    // The card is still drawn in its old column, so the UI must not say it moved.
    expect(
      await screen.findByText(
        'WO-20260719-004 Laser Cut - nest-p002 moved to Haas VF-2 on the server, but the board could not be re-read. Refresh to see where it is.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByText('WO-20260719-004 Laser Cut - nest-p002 moved to Haas VF-2.')).not.toBeInTheDocument();
    expect(cardOrder(2)).toEqual([11, 9, 14]);
    expect(screen.getByTestId('dispatch-stale-notice')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-status')).toHaveTextContent('may be out of date');
  });

  it('keeps focus inside the moved card when a keyboard reorder disables the pressed button', async () => {
    const user = userEvent.setup();
    mockApi.setWorkCenterRunOrder.mockReturnValue(new Promise(() => undefined));
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    // Card 9 is second; Move up puts it first, which DISABLES the very button
    // that was pressed — the browser blurs it and focus used to land on <body>.
    await user.click(screen.getByLabelText('Move WO-20260719-004 Laser Cut - nest-p002 up'));

    expect(cardOrder(2)).toEqual([9, 11, 14]);
    const movedCard = screen.getByTestId('dispatch-card-9');
    expect(movedCard).toContainElement(document.activeElement as HTMLElement);
    expect(document.activeElement).toBe(
      screen.getByLabelText('Move WO-20260719-004 Laser Cut - nest-p002 down')
    );
    expect(document.activeElement).not.toBe(document.body);
  });

  it('keeps focus on the pressed control when a keyboard reorder does not hit a boundary', async () => {
    const user = userEvent.setup();
    mockApi.setWorkCenterRunOrder.mockReturnValue(new Promise(() => undefined));
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    // Card 14 is last; Move up lands it in the middle, where Move up survives.
    const up = screen.getByLabelText('Move WO-20260720-003 Deburr up');
    await user.click(up);

    expect(cardOrder(2)).toEqual([11, 14, 9]);
    expect(document.activeElement).toBe(up);
  });

  it('carries the nest detail line on a nest card and nothing extra on a plain operation', async () => {
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    // Material and thickness first — they are what an order is chosen by.
    expect(screen.getByTestId('dispatch-nest-11')).toHaveTextContent('A36 · 0.25in · 48x96 · 3 of 5 sheets left');
    expect(screen.getByTestId('dispatch-nest-9')).toHaveTextContent('304SS · 0.25in · 48x96 · 2 of 2 sheets left');
    // The deburr op has no nest, so the card gains no line at all.
    expect(screen.queryByTestId('dispatch-nest-14')).not.toBeInTheDocument();
  });

  it('marks the boundary where the material changes, and never against a non-nest job', async () => {
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    // Nest 1 (A36) -> Nest 2 (304SS): the boundary is drawn ON the lower card.
    const marker = screen.getByTestId('dispatch-changeover-marker-9');
    expect(marker).toHaveTextContent('material change');
    // Presentational only: nothing to activate, nothing to focus.
    expect(marker).not.toHaveAttribute('role');
    expect(marker).not.toHaveAttribute('tabindex');

    // No marker above the first card, and none against the non-nest deburr job
    // even though its neighbour is a nest.
    expect(screen.queryByTestId('dispatch-changeover-marker-11')).not.toBeInTheDocument();
    expect(screen.queryByTestId('dispatch-changeover-marker-14')).not.toBeInTheDocument();
  });

  it("summarises the column's nests and the changeovers its current order costs", async () => {
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    expect(screen.getByTestId('dispatch-changeovers-2')).toHaveTextContent('2 nests · 1 changeover');
    // A column with no nest work says nothing rather than "0 nests".
    expect(screen.queryByTestId('dispatch-changeovers-5')).not.toBeInTheDocument();
  });

  it('names material-only, thickness-only and combined changeovers distinctly', async () => {
    mockApi.getDispatchBoard.mockResolvedValue(nestBoard());
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    expect(screen.getByTestId('dispatch-changeover-marker-102')).toHaveTextContent('material change');
    // 102 (304SS) -> 103 (' a36 ') is a material change despite the case/whitespace...
    expect(screen.getByTestId('dispatch-changeover-marker-103')).toHaveTextContent('material change');
    // ...and 103 -> 104 is the same material at a new thickness.
    expect(screen.getByTestId('dispatch-changeover-marker-104')).toHaveTextContent('thickness change');
    expect(screen.getByTestId('dispatch-changeovers-2')).toHaveTextContent('4 nests · 3 changeovers');
  });

  it('re-counts changeovers as the order changes, so batching shows its own payoff', async () => {
    const user = userEvent.setup();
    mockApi.getDispatchBoard.mockResolvedValue(nestBoard());
    mockApi.setWorkCenterRunOrder.mockReturnValue(new Promise(() => undefined));
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    expect(screen.getByTestId('dispatch-changeovers-2')).toHaveTextContent('4 nests · 3 changeovers');

    // Batch the two A36/0.25in nests together: 101,103,102,104 costs one less.
    await user.click(screen.getByLabelText('Move WO-N-103 Laser Cut - n103 up'));

    expect(cardOrder(2)).toEqual([101, 103, 102, 104]);
    // The count follows the OPTIMISTIC order — the payoff is visible immediately,
    // before the server answers.
    expect(screen.getByTestId('dispatch-changeovers-2')).toHaveTextContent('4 nests · 2 changeovers');
    expect(screen.queryByTestId('dispatch-changeover-marker-103')).not.toBeInTheDocument();
    expect(screen.getByTestId('dispatch-changeover-marker-104')).toHaveTextContent('material + thickness change');
  });

  // --- Scale: 24 machines, 14 jobs ------------------------------------------

  it('renders machines with work before idle ones, which stay collapsed by default', async () => {
    mockApi.getDispatchBoard.mockResolvedValue(scaleBoard());
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    // Code order put ASM-01 and BND-01 first; only the machines with work show.
    expect(columnOrder()).toEqual(['Ermaksan Fiber Laser run order', 'Haas VF-2 run order']);
    expect(screen.queryByRole('region', { name: 'Assembly 1 run order' })).not.toBeInTheDocument();

    const toggle = screen.getByTestId('dispatch-idle-toggle');
    expect(toggle).toHaveTextContent('Show 3 idle machines');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    // The disclosure names the region it controls, so it is announced as one.
    expect(toggle).toHaveAttribute('aria-controls', 'dispatch-idle-machines');
    expect(document.getElementById('dispatch-idle-machines')).toBeInTheDocument();
  });

  it('reveals the idle machines after the busy ones, in code order, and flips aria-expanded', async () => {
    const user = userEvent.setup();
    mockApi.getDispatchBoard.mockResolvedValue(scaleBoard());
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    await user.click(screen.getByTestId('dispatch-idle-toggle'));

    expect(columnOrder()).toEqual([
      'Ermaksan Fiber Laser run order',
      'Haas VF-2 run order',
      // Idle machines follow the busy ones, still in the server's code order.
      'Assembly 1 run order',
      'Bandsaw 1 run order',
      'Grinder 1 run order',
    ]);
    const toggle = screen.getByTestId('dispatch-idle-toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveTextContent('Hide idle machines');
    expect(within(screen.getByTestId('dispatch-column-1')).getByText(/Idle — no queued work/)).toBeInTheDocument();

    // ...and collapsing puts them away again.
    await user.click(toggle);
    expect(columnOrder()).toEqual(['Ermaksan Fiber Laser run order', 'Haas VF-2 run order']);
    expect(screen.getByTestId('dispatch-idle-toggle')).toHaveAttribute('aria-expanded', 'false');
  });

  it('keeps a hidden idle machine reachable from every card’s move list', async () => {
    const user = userEvent.setup();
    mockApi.getDispatchBoard.mockResolvedValue(scaleBoard());
    mockApi.updateOperation.mockResolvedValue({});
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    // Grinder 1 has no column on screen, but the job can still be sent to it —
    // which is what makes collapsing the idle machines cost-free.
    const select = screen.getByLabelText('Move WO-20260719-004 Laser Cut - nest-p002 to another machine');
    expect(within(select).getByRole('option', { name: 'Grinder 1' })).toBeInTheDocument();
    await user.selectOptions(select, '6');
    await waitFor(() => expect(mockApi.updateOperation).toHaveBeenCalledWith(9, { work_center_id: 6, version: 3 }));
  });

  it('renders an all-idle board in full, with no disclosure to hide it behind', async () => {
    mockApi.getDispatchBoard.mockResolvedValue(allIdleBoard());
    renderBoard();

    expect(await findColumn('Assembly 1')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Bandsaw 1 run order' })).toBeInTheDocument();
    expect(screen.queryByTestId('dispatch-idle-toggle')).not.toBeInTheDocument();
    // Still a board, not the "no machines" empty state.
    expect(screen.queryByText('No machines to dispatch')).not.toBeInTheDocument();
  });

  it('reports the busy/idle split alongside the shop-wide totals', async () => {
    mockApi.getDispatchBoard.mockResolvedValue(scaleBoard());
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    const totals = screen.getByTestId('dispatch-totals');
    // The totals still describe the SHOP (4 jobs across all 5 machines)...
    expect(totals).toHaveTextContent('4 queued jobs across 5 machines');
    expect(totals).toHaveTextContent('3 ranked');
    // ...and the split says why only two columns are on screen.
    expect(totals).toHaveTextContent('2 machines with work, 3 idle');
  });

  it('scrolls the board by two columns and goes inert at each end', async () => {
    const user = userEvent.setup();
    mockApi.getDispatchBoard.mockResolvedValue(scaleBoard());
    renderBoard();
    await findColumn('Ermaksan Fiber Laser');

    const left = screen.getByTestId('dispatch-scroll-left');
    const right = screen.getByTestId('dispatch-scroll-right');

    // At the left end: only "scroll right" can do anything.
    const { el, scrollBy } = stubScrollGeometry(0);
    fireEvent.scroll(el);
    await waitFor(() => expect(right).not.toHaveAttribute('aria-disabled'));
    expect(left).toHaveAttribute('aria-disabled', 'true');
    // Inert, NOT `disabled` — a keyboard user panning the board must not be
    // blurred onto <body> the moment they reach an end.
    expect(left).toBeEnabled();
    expect(right).toBeEnabled();
    // ...and the edge fade says the board continues.
    expect(screen.getByTestId('dispatch-board-more')).toBeInTheDocument();

    await user.click(left);
    expect(scrollBy).not.toHaveBeenCalled();
    await user.click(right);
    expect(scrollBy).toHaveBeenCalledWith({ left: 664, behavior: 'smooth' });

    // At the right end the mirror image holds, and the fade is gone.
    stubScrollGeometry(2000);
    fireEvent.scroll(el);
    await waitFor(() => expect(right).toHaveAttribute('aria-disabled', 'true'));
    expect(left).not.toHaveAttribute('aria-disabled');
    expect(screen.queryByTestId('dispatch-board-more')).not.toBeInTheDocument();
  });

  it('renders a retryable error state when the board fails to load', async () => {
    const user = userEvent.setup();
    mockApi.getDispatchBoard.mockRejectedValueOnce({ response: { data: { detail: 'Dispatch board unavailable' } } });
    renderBoard();

    expect(await screen.findByText('Dispatch board unavailable')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await findColumn('Ermaksan Fiber Laser')).toBeInTheDocument();
  });
});

describe('insertionIndexFromPointer', () => {
  // Card i occupies [i*100, i*100+90]; the gaps are the 10px between them.
  const rects = [0, 1, 2].map((i) => ({ top: i * 100, bottom: i * 100 + 90 }));

  it('inserts before a card while the pointer is in its top half', () => {
    expect(insertionIndexFromPointer(rects, 0)).toBe(0);
    expect(insertionIndexFromPointer(rects, 44)).toBe(0);
    expect(insertionIndexFromPointer(rects, 120)).toBe(1);
  });

  it('inserts after a card once the pointer passes its midpoint', () => {
    expect(insertionIndexFromPointer(rects, 46)).toBe(1);
    expect(insertionIndexFromPointer(rects, 160)).toBe(2);
  });

  it('treats the gap between two cards as the slot between them, never the tail', () => {
    expect(insertionIndexFromPointer(rects, 95)).toBe(1); // gap under card 0
    expect(insertionIndexFromPointer(rects, 195)).toBe(2); // gap under card 1
  });

  it('only returns the tail below the last card, and handles an empty column', () => {
    expect(insertionIndexFromPointer(rects, 246)).toBe(3);
    expect(insertionIndexFromPointer(rects, 5000)).toBe(3);
    expect(insertionIndexFromPointer([], 0)).toBe(0);
  });
});
