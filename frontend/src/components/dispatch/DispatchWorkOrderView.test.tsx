/**
 * Dispatch Board v2 — the WORK ORDERS view (the page's default).
 *
 * Rendered through the real page shell (`pages/DispatchBoard`) at `/dispatch`, so
 * the URL params the shell owns (view, q, chips, machine) are exercised for real.
 * services/api and usePermissions are mocked at the module boundary; every
 * fixture is typed against the real wire contract (`DispatchLane`, …), so a
 * shape the server never sends is a compile error, not a passing test.
 *
 * Pins:
 *  - server order is rendered as-is: the plan (true positions), then "Not in
 *    plan", with the "N released, not in plan" chip counting released lanes only;
 *  - the plan write is OPTIMISTIC and sends the full desired plan plus the plan
 *    exactly as rendered (CAS); a 409 rolls back, shows the server's detail
 *    verbatim and re-reads;
 *  - the keyboard paths: Move up, Move to top, Set position… (InputDialog, with
 *    range validation), Remove from plan, Add to plan, Add all by promise date
 *    (client sort by promise date, confirmed first);
 *  - a lane drag goes through the same PUT;
 *  - routing lanes are read-only (no operation-order control at all);
 *  - a pool lane's operation reorder sends setDispatchOperationOrder with the
 *    lane's queued subset as CAS; held / unplanned lanes disable it with the
 *    server's own reason and never call it;
 *  - a machine move is NON-optimistic (`updateOperation` with `version`), never
 *    offers a deactivated machine, pins running work, and a partial batch is a
 *    `warning` toast naming what did not move;
 *  - filters narrow lanes without renumbering them, and a move made while
 *    filtered lands next to the visible neighbour;
 *  - a 404 from an older API (deploy window) is a retryable ErrorState;
 *  - a background poll replaces the board WITHOUT announcing anything;
 *  - nothing read before a write lands after it: a poll in flight when a plan or
 *    operation write starts is DEFERRED (and runs once the write settles), a
 *    manual Refresh in flight is dropped, and neither undoes the move;
 *  - "Set / Add at position…" keeps what the planner typed: its default is
 *    frozen at open, and polls wait while a plan dialog is open.
 */

import React from 'react';
import { act, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import DispatchBoard from '../../pages/DispatchBoard';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type {
  DispatchLane,
  DispatchLaneOp,
  DispatchWorkCenterOption,
  DispatchWorkOrderBoardResponse,
} from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getDispatchBoard: jest.fn(),
    setWorkCenterRunOrder: jest.fn(),
    updateOperation: jest.fn(),
    getDispatchWorkOrderBoard: jest.fn(),
    setDispatchWorkOrderOrder: jest.fn(),
    setDispatchOperationOrder: jest.fn(),
  },
}));

jest.mock('../../hooks/usePermissions', () => ({
  __esModule: true,
  usePermissions: () => ({ can: () => true, canAny: () => true, canAll: () => true, isAdmin: true }),
}));

const mockApi = api as jest.Mocked<typeof api>;

const CONFLICT = 'The dispatch board changed while you were editing. It has been refreshed — please try again.';

const op = (overrides: Partial<DispatchLaneOp> & { operation_id: number }): DispatchLaneOp => ({
  version: 1,
  sequence: 10,
  operation_number: '10',
  operation_name: 'Operation',
  status: 'ready',
  work_center_id: 12,
  work_center_code: 'BRAKE-1',
  run_position: null,
  blocked_by_predecessor: false,
  on_hold: false,
  crew: [],
  quantity_complete: 0,
  quantity_target: 10,
  setup_time_hours: 0.5,
  run_time_hours: 1.25,
  component_part_number: null,
  laser_nest: null,
  material_tie: null,
  ...overrides,
});

const lane = (
  overrides: Partial<DispatchLane> & { work_order_id: number; work_order_number: string }
): DispatchLane => ({
  version: 1,
  unit_number: null,
  status: 'released',
  work_order_type: 'production',
  sequential_operations: false,
  is_laser: false,
  mode: 'pool',
  dispatch_position: null,
  priority: 5,
  due_date: null,
  must_ship_by: null,
  promise_date: null,
  customer_name: null,
  part_number: 'PN-1',
  part_name: 'Part',
  quantity_ordered: 10,
  quantity_complete: 0,
  ops_total: 1,
  ops_complete: 0,
  open_blocker_count: 0,
  any_material_short: false,
  operations: [],
  ...overrides,
});

const WORK_CENTERS: DispatchWorkCenterOption[] = [
  {
    id: 12,
    code: 'BRAKE-1',
    name: 'Press Brake 1',
    work_center_type: 'press_brake',
    is_active: true,
    current_status: 'available',
  },
  {
    id: 9,
    code: 'LASER-1',
    name: 'Ermaksan Fiber',
    work_center_type: 'laser',
    is_active: true,
    current_status: 'in_use',
  },
  {
    id: 10,
    code: 'LASER-2',
    name: 'Bystronic',
    work_center_type: 'laser',
    is_active: true,
    current_status: 'available',
  },
  {
    id: 14,
    code: 'WELD-2',
    name: 'Weld Bay 2',
    work_center_type: 'welding',
    is_active: true,
    current_status: 'available',
  },
  { id: 99, code: 'OLD-1', name: 'Retired Saw', work_center_type: 'sawing', is_active: false, current_status: null },
];

/** Plan position 1: a pool lane — two queued ops at BRAKE-1, one running at WELD-2, one pending there. */
const LANE_A = (): DispatchLane =>
  lane({
    work_order_id: 412,
    work_order_number: 'WO-20261103-004',
    version: 6,
    unit_number: '7',
    dispatch_position: 1,
    due_date: '2099-11-12',
    promise_date: '2099-11-12',
    customer_name: 'Acme Trailer',
    part_number: 'BRKT-1180',
    part_name: 'Hitch bracket',
    ops_total: 4,
    operations: [
      op({
        operation_id: 3101,
        version: 2,
        sequence: 10,
        operation_number: '10',
        operation_name: 'Brake form flange',
        run_position: 1,
      }),
      op({
        operation_id: 3102,
        version: 2,
        sequence: 20,
        operation_number: '20',
        operation_name: 'Brake form gusset',
        run_position: 2,
      }),
      op({
        operation_id: 3104,
        version: 3,
        sequence: 25,
        operation_number: '25',
        operation_name: 'Tack weld',
        status: 'in_progress',
        work_center_id: 14,
        work_center_code: 'WELD-2',
        run_position: 1,
        crew: [{ user_id: 61, display_name: 'Jon W.' }],
      }),
      op({
        operation_id: 3103,
        sequence: 30,
        operation_number: '30',
        operation_name: 'Weld assembly',
        status: 'pending',
        work_center_id: 14,
        work_center_code: 'WELD-2',
        blocked_by_predecessor: true,
      }),
    ],
  });

/** Plan position 2: a sequenced routing, running, already LATE (promise in the past). */
const LANE_B = (): DispatchLane =>
  lane({
    work_order_id: 398,
    work_order_number: 'WO-20261030-002',
    version: 4,
    status: 'in_progress',
    sequential_operations: true,
    mode: 'routing',
    dispatch_position: 2,
    priority: 3,
    due_date: '2020-01-03',
    must_ship_by: '2020-01-01',
    promise_date: '2020-01-01',
    customer_name: 'Miratech',
    part_number: 'HSG-220',
    part_name: 'Housing',
    ops_total: 2,
    open_blocker_count: 1,
    operations: [
      op({
        operation_id: 3050,
        version: 5,
        operation_name: 'Brake form',
        status: 'in_progress',
        run_position: 3,
        crew: [{ user_id: 57, display_name: 'Maria L.' }],
        quantity_complete: 4,
      }),
      op({
        operation_id: 3051,
        sequence: 20,
        operation_number: '20',
        operation_name: 'Weld',
        status: 'pending',
        work_center_id: 14,
        work_center_code: 'WELD-2',
        blocked_by_predecessor: true,
      }),
    ],
  });

/** Plan position 3: a pool lane ON HOLD (still in the plan). */
const LANE_C = (): DispatchLane =>
  lane({
    work_order_id: 405,
    work_order_number: 'WO-20261101-007',
    status: 'on_hold',
    dispatch_position: 3,
    part_number: 'TAB-014',
    ops_total: 2,
    operations: [
      op({ operation_id: 3201, operation_name: 'Punch tab', run_position: null }),
      op({ operation_id: 3202, sequence: 20, operation_number: '20', operation_name: 'Form tab', run_position: null }),
    ],
  });

/** Not in plan: a laser package (two nests at LASER-1), released, promise Dec 20. */
const LANE_D = (): DispatchLane =>
  lane({
    work_order_id: 431,
    work_order_number: 'WO-20261104-001',
    version: 2,
    work_order_type: 'laser_cutting',
    is_laser: true,
    mode: 'laser',
    priority: 2,
    due_date: '2099-12-20',
    promise_date: '2099-12-20',
    part_number: null,
    part_name: null,
    quantity_ordered: 4,
    ops_total: 2,
    operations: [
      op({
        operation_id: 3140,
        version: 2,
        operation_number: 'Nest 1',
        operation_name: 'Laser Cut - CNC0412',
        work_center_id: 9,
        work_center_code: 'LASER-1',
        quantity_target: 4,
        laser_nest: {
          cnc_number: 'CNC0412',
          material: 'A36',
          thickness: '0.250',
          sheet_size: '60x120',
          planned_runs: 4,
          completed_runs: 0,
          remaining_runs: 4,
        },
      }),
      op({
        operation_id: 3141,
        version: 4,
        sequence: 20,
        operation_number: 'Nest 2',
        operation_name: 'Laser Cut - CNC0413',
        work_center_id: 9,
        work_center_code: 'LASER-1',
        quantity_target: 2,
        laser_nest: {
          cnc_number: 'CNC0413',
          material: '304SS',
          thickness: '0.250',
          sheet_size: '60x120',
          planned_runs: 2,
          completed_runs: 0,
          remaining_runs: 2,
        },
      }),
    ],
  });

/** Not in plan: a DRAFT routing, promise Dec 1. */
const LANE_E = (): DispatchLane =>
  lane({
    work_order_id: 425,
    work_order_number: 'WO-20261104-003',
    status: 'draft',
    sequential_operations: true,
    mode: 'routing',
    due_date: '2099-12-01',
    promise_date: '2099-12-01',
    part_number: 'PLT-055',
    part_name: 'Base plate',
    operations: [
      op({
        operation_id: 3120,
        operation_name: 'Weld',
        status: 'pending',
        work_center_id: 14,
        work_center_code: 'WELD-2',
      }),
    ],
  });

/** Not in plan: a released pool lane with no promise date. */
const LANE_F = (): DispatchLane =>
  lane({
    work_order_id: 440,
    work_order_number: 'WO-20261104-009',
    priority: 7,
    part_number: 'GUS-301',
    ops_total: 2,
    operations: [
      op({ operation_id: 3301, operation_name: 'Shear gusset' }),
      op({ operation_id: 3302, sequence: 20, operation_number: '20', operation_name: 'Bend gusset' }),
    ],
  });

const makeBoard = (work_orders: DispatchLane[]): DispatchWorkOrderBoardResponse => ({
  generated_at: '2026-11-04T16:34:12.939849Z',
  work_orders,
  ranked_count: work_orders.filter(l => l.dispatch_position != null).length,
  unranked_count: work_orders.filter(l => l.dispatch_position == null).length,
  work_centers: WORK_CENTERS,
});

const board = () => makeBoard([LANE_A(), LANE_B(), LANE_C(), LANE_D(), LANE_E(), LANE_F()]);

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

const renderPage = (path = '/dispatch') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <DispatchBoard />
        <LocationProbe />
      </ToastProvider>
    </MemoryRouter>
  );

const planOrder = (): number[] =>
  Array.from(screen.getByTestId('dispatch-plan-list').querySelectorAll('[data-lane-row]')).map(el =>
    Number(el.getAttribute('data-lane-row'))
  );

const unplannedOrder = (): number[] =>
  Array.from(screen.getByTestId('dispatch-unplanned-list').querySelectorAll('[data-lane-row]')).map(el =>
    Number(el.getAttribute('data-lane-row'))
  );

const waitForBoard = () => screen.findByTestId('dispatch-plan-list');

const expand = async (user: ReturnType<typeof userEvent.setup>, wo: string) =>
  user.click(screen.getByRole('button', { name: `Show details for ${wo}` }));

/** jsdom has no DataTransfer and no layout; pin clientY on the native event. */
const fireDrag = (kind: 'dragStart' | 'dragOver' | 'drop', element: Element, clientY = 0) => {
  const event = createEvent[kind](element, {
    dataTransfer: { effectAllowed: '', dropEffect: '', setData: jest.fn(), getData: jest.fn() },
  });
  Object.defineProperty(event, 'clientY', { value: clientY });
  fireEvent(element, event);
};

/**
 * The TOAST (not the aria-live status line) carrying `text`. Callers assert its
 * VARIANT — the colour class `<ToastProvider>` gives each type — because a
 * refactor that turns a `warning` into an `error` (or a refusal into a
 * `success`) keeps the same sentence and changes only that.
 */
const findToast = async (text: string): Promise<HTMLElement> => {
  let found: HTMLElement | undefined;
  await waitFor(() => {
    found = [...screen.queryAllByRole('alert'), ...screen.queryAllByRole('status')].find(
      element => /\bbg-(red|amber|green|blue)-600\b/.test(element.className) && !!element.textContent?.includes(text)
    );
    expect(found).toBeDefined();
  });
  return found as HTMLElement;
};

/** The operation ids rendered inside one machine group of an expanded lane, top to bottom. */
const groupOrder = (regionName: string): number[] =>
  Array.from(screen.getByRole('region', { name: regionName }).querySelectorAll('[data-lane-op]')).map(element =>
    Number(element.getAttribute('data-lane-op'))
  );

const stubRect = (element: HTMLElement, top: number, height = 40) => {
  element.getBoundingClientRect = () =>
    ({
      top,
      bottom: top + height,
      height,
      left: 0,
      right: 800,
      width: 800,
      x: 0,
      y: top,
      toJSON: () => ({}),
    }) as DOMRect;
};

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getDispatchWorkOrderBoard.mockResolvedValue(board());
});

describe('DispatchWorkOrderView — rendering', () => {
  it('renders the plan, then "Not in plan", in server order with the true plan positions', async () => {
    renderPage();
    await waitForBoard();

    expect(planOrder()).toEqual([412, 398, 405]);
    // The server's unplanned order (priority first) is kept — no client re-sort.
    expect(unplannedOrder()).toEqual([431, 425, 440]);
    expect(screen.getByTestId('dispatch-lane-position-412')).toHaveTextContent('1');
    expect(screen.getByTestId('dispatch-lane-position-405')).toHaveTextContent('3');
    expect(screen.getByTestId('dispatch-lane-position-431')).toHaveTextContent('–');

    // Released only: the DRAFT lane is not in plan but is not "released".
    expect(screen.getByTestId('dispatch-released-not-in-plan')).toHaveTextContent('2 released, not in plan');
    // The machine view is not touched on the default view.
    expect(mockApi.getDispatchBoard).not.toHaveBeenCalled();
  });

  it('draws a compact lane line: link, unit, part, customer, promise, mode, progress, blockers, crew', async () => {
    renderPage();
    await waitForBoard();

    const a = screen.getByTestId('dispatch-lane-412');
    expect(within(a).getByRole('link', { name: 'WO-20261103-004' })).toHaveAttribute('href', '/work-orders/412');
    expect(within(a).getByText('Unit')).toBeInTheDocument();
    expect(within(a).getByText('BRKT-1180')).toBeInTheDocument();
    expect(within(a).getByText('Acme Trailer')).toBeInTheDocument();
    expect(within(a).getByText('Pool')).toBeInTheDocument();
    expect(within(a).getByText('0/4 ops')).toBeInTheDocument();

    const b = screen.getByTestId('dispatch-lane-398');
    expect(within(b).getByText('Routing')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-lane-promise-398')).toHaveTextContent(/^Late/);
    expect(within(b).getByText('1 blocker')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-lane-running-398')).toHaveTextContent('Running · Maria L.');

    expect(within(screen.getByTestId('dispatch-lane-431')).getByText('Laser')).toBeInTheDocument();
  });

  it('renders a retryable ErrorState when an older API answers 404 (deploy window)', async () => {
    const user = userEvent.setup();
    mockApi.getDispatchWorkOrderBoard.mockRejectedValueOnce({
      response: { status: 404, data: { detail: 'Not Found' } },
    });
    renderPage();

    expect(await screen.findByText("Couldn't load the dispatch plan")).toBeInTheDocument();
    expect(screen.getByText(/not available from the server yet/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await waitForBoard()).toBeInTheDocument();
  });

  it('treats a body without lanes (older API shape) as a retryable error, not a crash', async () => {
    mockApi.getDispatchWorkOrderBoard.mockResolvedValueOnce({} as DispatchWorkOrderBoardResponse);
    renderPage();
    expect(await screen.findByTestId('error-state')).toBeInTheDocument();
  });
});

describe('DispatchWorkOrderView — reordering the plan (optimistic)', () => {
  it('Move up reorders at once and PUTs the full plan with the rendered plan as CAS', async () => {
    const user = userEvent.setup();
    let resolvePut: (value: DispatchWorkOrderBoardResponse) => void = () => undefined;
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(
      new Promise(resolve => {
        resolvePut = resolve;
      })
    );
    renderPage();
    await waitForBoard();

    const up = screen.getByRole('button', { name: 'Move WO-20261030-002 up' });
    await user.click(up);

    expect(planOrder()).toEqual([398, 412, 405]);
    expect(screen.getByTestId('dispatch-lane-position-398')).toHaveTextContent('1');
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([398, 412, 405], [412, 398, 405]);
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
      'WO-20261030-002 moved to plan position 1 of 3.'
    );
    // In flight: every reorder control is inert but still focusable.
    const down = screen.getByRole('button', { name: 'Move WO-20261103-004 down' });
    expect(down).toHaveAttribute('aria-disabled', 'true');
    await user.click(down);
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledTimes(1);

    const serverBoard = makeBoard([
      { ...LANE_B(), dispatch_position: 1 },
      { ...LANE_A(), dispatch_position: 2 },
      LANE_C(),
      LANE_D(),
      LANE_E(),
      LANE_F(),
    ]);
    await act(async () => {
      resolvePut(serverBoard);
    });
    expect(planOrder()).toEqual([398, 412, 405]);
    expect(screen.getByTestId('dispatch-lane-position-412')).toHaveTextContent('2');
    expect(down).not.toHaveAttribute('aria-disabled');
  });

  it('a 409 rolls the plan back, shows the server detail verbatim, and re-reads', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockRejectedValue({ response: { status: 409, data: { detail: CONFLICT } } });
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Move WO-20261030-002 up' }));

    expect(await screen.findByText(CONFLICT)).toBeInTheDocument();
    expect(planOrder()).toEqual([412, 398, 405]);
    await waitFor(() => expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent('Plan change not saved.');
  });

  it('the rollback stands on its own before any re-read lands, the refusal is an ERROR toast, and the stale body is never resent', async () => {
    const user = userEvent.setup();
    let rejectPut: (reason: unknown) => void = () => undefined;
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectPut = reject;
      })
    );
    // The first read renders the board; the re-read after the refusal never
    // answers, so whatever the plan shows afterwards is the ROLLBACK's doing.
    mockApi.getDispatchWorkOrderBoard.mockResolvedValueOnce(board()).mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Move WO-20261030-002 up' }));
    expect(planOrder()).toEqual([398, 412, 405]); // the optimistic guess

    await act(async () => {
      rejectPut({ response: { status: 409, data: { detail: CONFLICT } } });
    });

    expect(planOrder()).toEqual([412, 398, 405]);
    expect(screen.getByTestId('dispatch-lane-position-398')).toHaveTextContent('2');
    const toast = await findToast(CONFLICT);
    expect(toast).toHaveClass('bg-red-600');
    expect(toast).toHaveAttribute('role', 'alert');
    // Re-read issued; the same (stale-CAS) body is never auto-retried.
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledTimes(1);
    // The controls are live again — a refusal does not wedge the board.
    expect(screen.getByRole('button', { name: 'Move WO-20261030-002 up' })).not.toHaveAttribute('aria-disabled');
  });

  it('a 400 naming an unknown id is handled like a conflict: rolled back, verbatim, re-read', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockRejectedValue({
      response: { status: 400, data: { detail: 'Unknown work order id: 431' } },
    });
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Add WO-20261104-001 to plan' }));

    expect(await findToast('Unknown work order id: 431')).toHaveClass('bg-red-600');
    expect(planOrder()).toEqual([412, 398, 405]);
    expect(unplannedOrder()).toContain(431);
    await waitFor(() => expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2));
  });

  it("the plan PUT's response is the whole board and REPLACES the optimistic guess (the server wins)", async () => {
    const user = userEvent.setup();
    // Between render and save, WO-20261101-007 completed: the server drops a
    // no-longer-live id from both lists rather than refusing, and answers with
    // the board as it now is.
    mockApi.setDispatchWorkOrderOrder.mockResolvedValue(
      makeBoard([LANE_A(), LANE_B(), { ...LANE_D(), dispatch_position: 3 }, LANE_E(), LANE_F()])
    );
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Add WO-20261104-001 to plan' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([412, 398, 405, 431], [412, 398, 405]);

    await waitFor(() => expect(planOrder()).toEqual([412, 398, 431]));
    expect(screen.queryByTestId('dispatch-lane-405')).not.toBeInTheDocument();
    expect(screen.getByTestId('dispatch-lane-position-431')).toHaveTextContent('3');
    // Settling from the response needs no extra read.
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(1);
  });

  it('Move to top puts the lane at position 1', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    expect(screen.getByRole('button', { name: 'Move WO-20261103-004 to top' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Move WO-20261101-007 to top' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([405, 412, 398], [412, 398, 405]);
    expect(planOrder()).toEqual([405, 412, 398]);
    // Focus is kept inside the moved lane, not dropped on <body>.
    expect(screen.getByTestId('dispatch-lane-405')).toContainElement(document.activeElement as HTMLElement);
  });

  it('Set position… moves a lane to the typed position and refuses an out-of-range one', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Set plan position for WO-20261103-004' }));
    const field = screen.getByLabelText(/Plan position/);
    await user.clear(field);
    await user.type(field, '9');
    await user.click(screen.getByRole('button', { name: 'Move' }));
    expect(await screen.findByText('Enter a whole number from 1 to 3.')).toBeInTheDocument();
    expect(mockApi.setDispatchWorkOrderOrder).not.toHaveBeenCalled();

    await user.clear(field);
    await user.type(field, '3');
    await user.click(screen.getByRole('button', { name: 'Move' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([398, 405, 412], [412, 398, 405]);
    expect(planOrder()).toEqual([398, 405, 412]);
  });

  it('Remove from plan drops the lane into "Not in plan"', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Remove WO-20261030-002 from plan' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([412, 405], [412, 398, 405]);
    expect(planOrder()).toEqual([412, 405]);
    expect(unplannedOrder()).toContain(398);
    expect(screen.getByTestId('dispatch-lane-position-405')).toHaveTextContent('2');
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent('WO-20261030-002 removed from the plan.');
  });

  it('Add to plan appends a lane to the end of the plan', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Add WO-20261104-001 to plan' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([412, 398, 405, 431], [412, 398, 405]);
    expect(planOrder()).toEqual([412, 398, 405, 431]);
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
      'WO-20261104-001 added to the plan at position 4 of 4.'
    );
  });

  it('Add all by promise date confirms, then appends earliest promise first (no date last)', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Add all 3 by promise date' }));
    // Nothing is written until the planner confirms the bulk change.
    expect(mockApi.setDispatchWorkOrderOrder).not.toHaveBeenCalled();
    expect(screen.getByText(/1 still in draft — planning does not release them/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Add to plan' }));

    // Server order was D (P2), E, F; by promise date it is E (Dec 1), D (Dec 20), F (none).
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([412, 398, 405, 425, 431, 440], [412, 398, 405]);
  });

  it('Add all adds only the "Not in plan" lanes the filters show, and cancelling writes nothing', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    // LASER-1 work only: of the unplanned lanes, just the laser package.
    renderPage('/dispatch?machine=9');
    await screen.findByTestId('dispatch-unplanned-list');
    expect(unplannedOrder()).toEqual([431]);

    await user.click(screen.getByRole('button', { name: 'Add all 1 by promise date' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(mockApi.setDispatchWorkOrderOrder).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Add all 1 by promise date' }));
    await user.click(screen.getByRole('button', { name: 'Add to plan' }));
    // Hidden unplanned lanes (the draft, the gusset job) are NOT swept in.
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([412, 398, 405, 431], [412, 398, 405]);
  });

  it('Add at position… inserts a lane from "Not in plan" at the typed position', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Add WO-20261104-001 to plan at a position' }));
    const field = screen.getByLabelText(/Plan position/);
    // An unplanned lane may also go one past the end (4 of a 3-lane plan) — not 5.
    await user.clear(field);
    await user.type(field, '5');
    await user.click(screen.getByRole('button', { name: 'Add' }));
    expect(await screen.findByText('Enter a whole number from 1 to 4.')).toBeInTheDocument();
    expect(mockApi.setDispatchWorkOrderOrder).not.toHaveBeenCalled();

    await user.clear(field);
    await user.type(field, '2');
    await user.click(screen.getByRole('button', { name: 'Add' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([412, 431, 398, 405], [412, 398, 405]);
    expect(planOrder()).toEqual([412, 431, 398, 405]);
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
      'WO-20261104-001 added to the plan at position 2 of 4.'
    );
  });

  it('Add at position… keeps what the planner typed: a poll while the dialog is open waits until it closes', async () => {
    const user = userEvent.setup();
    let resolvePut: (value: DispatchWorkOrderBoardResponse) => void = () => undefined;
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(
      new Promise(resolve => {
        resolvePut = resolve;
      })
    );
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Add WO-20261104-001 to plan at a position' }));
    const field = screen.getByLabelText(/Plan position/);
    expect(field).toHaveValue('4');
    await user.clear(field);
    await user.type(field, '2');

    // WO-20261101-007 completes on the floor: the next poll would shrink the plan
    // (and with it the dialog's default) under the planner's typing.
    mockApi.getDispatchWorkOrderBoard.mockResolvedValue(makeBoard([LANE_A(), LANE_B(), LANE_D(), LANE_E(), LANE_F()]));
    fireEvent(window, new Event('focus'));
    await act(async () => {});

    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('dispatch-lanes-freshness')).toHaveTextContent('Updates deferred');
    expect(field).toHaveValue('2');

    await user.click(screen.getByRole('button', { name: 'Add' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([412, 431, 398, 405], [412, 398, 405]);

    // The deferred poll runs once the write it waited for has settled.
    await act(async () => {
      resolvePut(makeBoard([LANE_A(), { ...LANE_D(), dispatch_position: 2 }, { ...LANE_B(), dispatch_position: 3 }]));
    });
    await waitFor(() => expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2));
  });

  it('the add-all confirmation also holds polls back, and the deferred poll runs once it is cancelled', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitForBoard();

    await user.click(screen.getByRole('button', { name: 'Add all 3 by promise date' }));
    fireEvent(window, new Event('focus'));
    await act(async () => {});
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('dispatch-lanes-freshness')).toHaveTextContent('Updates deferred');

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByTestId('dispatch-lanes-freshness')).not.toHaveTextContent('Updates deferred')
    );
    expect(mockApi.setDispatchWorkOrderOrder).not.toHaveBeenCalled();
  });

  it('Add at position… keeps the typed position when a read already in flight lands under the open dialog', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    // A manual Refresh is not deferred: it is still in flight when the dialog opens.
    let resolveRefresh: (value: DispatchWorkOrderBoardResponse) => void = () => undefined;
    mockApi.getDispatchWorkOrderBoard.mockReturnValueOnce(
      new Promise(resolve => {
        resolveRefresh = resolve;
      })
    );
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await user.click(screen.getByRole('button', { name: 'Add WO-20261104-001 to plan at a position' }));
    const field = screen.getByLabelText(/Plan position/);
    expect(field).toHaveValue('4');
    await user.clear(field);
    await user.type(field, '2');

    // It answers without WO-20261101-007 (completed): the plan is now two long.
    await act(async () => {
      resolveRefresh(makeBoard([LANE_A(), LANE_B(), LANE_D(), LANE_E(), LANE_F()]));
    });

    expect(field).toHaveValue('2');
    // The range is still checked against the plan as it is now.
    expect(screen.getByText(/Enter a position from 1 to 3\./)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Add' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([412, 431, 398], [412, 398]);
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
      'WO-20261104-001 added to the plan at position 2 of 3.'
    );
  });

  it('a lane dragged onto the top half of another lands before it, through the same PUT', async () => {
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    const target = screen.getByTestId('dispatch-lane-drag-412');
    stubRect(target, 0);
    fireDrag('dragStart', screen.getByTestId('dispatch-lane-drag-405'));
    fireDrag('dragOver', target, 10);
    fireDrag('drop', target, 10);

    await waitFor(() =>
      expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([405, 412, 398], [412, 398, 405])
    );
  });
});

describe('DispatchWorkOrderView — operations inside a lane', () => {
  it('a routing lane is read-only: a strip in sequence with no operation-order controls', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitForBoard();

    await expand(user, 'WO-20261030-002');
    expect(screen.getByTestId('lane-routing-note-398')).toHaveTextContent('Runs in routing sequence');
    const strip = screen.getByRole('list', { name: 'Routing for WO-20261030-002' });
    expect(strip).toHaveAttribute('title', 'Runs in routing sequence');
    expect(within(strip).getByTestId('lane-op-3050')).toHaveTextContent('Running · Maria L.');
    expect(within(strip).getByTestId('lane-op-3051')).toHaveTextContent('Waiting on Op 10');
    expect(screen.queryByRole('button', { name: /Move Op 10 Brake form (up|down)/ })).not.toBeInTheDocument();
    expect(mockApi.setDispatchOperationOrder).not.toHaveBeenCalled();
  });

  it('a pool lane groups operations by machine and shows RUN n at the machine', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitForBoard();

    await expand(user, 'WO-20261103-004');
    const brake = screen.getByRole('region', { name: 'BRAKE-1 operations of WO-20261103-004' });
    expect(within(brake).getByTestId('lane-op-3101')).toHaveTextContent(/Ready\s*Run\s*1\s*at BRAKE-1/);
    expect(screen.getByRole('region', { name: 'WELD-2 operations of WO-20261103-004' })).toBeInTheDocument();
  });

  it('reordering a pool lane operation sends setDispatchOperationOrder with the queued subset as CAS', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchOperationOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    await expand(user, 'WO-20261103-004');
    await user.click(screen.getByRole('button', { name: 'Move Op 20 Brake form gusset up' }));

    // Every ready / in-progress op, in the new order; CAS = the queued subset as rendered.
    expect(mockApi.setDispatchOperationOrder).toHaveBeenCalledWith(412, [3102, 3101, 3104], [3101, 3102, 3104]);
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
      'Op 20 Brake form gusset moved to position 1 of 2 for WO-20261103-004 at BRAKE-1.'
    );
    // A running operation can't be picked up (the board's own rule).
    expect(screen.getByRole('button', { name: 'Move Op 25 Tack weld up' })).toBeDisabled();
  });

  it('a refused operation reorder rolls the lane back, shows the detail as an ERROR toast, and re-reads', async () => {
    const user = userEvent.setup();
    let rejectPut: (reason: unknown) => void = () => undefined;
    mockApi.setDispatchOperationOrder.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectPut = reject;
      })
    );
    // The re-read never answers: the order shown afterwards is the rollback's.
    mockApi.getDispatchWorkOrderBoard.mockResolvedValueOnce(board()).mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261103-004');

    const brake = 'BRAKE-1 operations of WO-20261103-004';
    expect(groupOrder(brake)).toEqual([3101, 3102]);
    await user.click(screen.getByRole('button', { name: 'Move Op 20 Brake form gusset up' }));
    expect(groupOrder(brake)).toEqual([3102, 3101]); // optimistic

    await act(async () => {
      rejectPut({ response: { status: 409, data: { detail: CONFLICT } } });
    });

    expect(groupOrder(brake)).toEqual([3101, 3102]);
    expect(screen.getByTestId('lane-op-3101')).toHaveTextContent(/Run\s*1/);
    expect(await findToast(CONFLICT)).toHaveClass('bg-red-600');
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent('Operation order not saved.');
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);
    expect(mockApi.setDispatchOperationOrder).toHaveBeenCalledTimes(1);
  });

  it("the operation-order PUT's response replaces that one lane (the server's positions win)", async () => {
    const user = userEvent.setup();
    const serverLane = LANE_A();
    // The server answers with the lane re-sorted by queue position — and, since
    // a plan change landed meanwhile, RUN 3/4 rather than the guessed 1/2.
    const [flange, gusset, ...rest] = serverLane.operations;
    serverLane.operations = [{ ...gusset, run_position: 3 }, { ...flange, run_position: 4 }, ...rest];
    mockApi.setDispatchOperationOrder.mockResolvedValue(serverLane);
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261103-004');

    await user.click(screen.getByRole('button', { name: 'Move Op 20 Brake form gusset up' }));

    await waitFor(() => expect(screen.getByTestId('lane-op-3102')).toHaveTextContent(/Run\s*3/));
    expect(screen.getByTestId('lane-op-3101')).toHaveTextContent(/Run\s*4/);
    expect(groupOrder('BRAKE-1 operations of WO-20261103-004')).toEqual([3102, 3101]);
    // Other lanes untouched, and no extra read was needed to settle.
    expect(planOrder()).toEqual([412, 398, 405]);
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(1);
  });

  it('dragging an operation within its machine reorders it through the same PUT — never as a lane drag', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchOperationOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261103-004');

    const target = screen.getByTestId('lane-op-drag-3101');
    stubRect(target, 0);
    fireDrag('dragStart', screen.getByTestId('lane-op-drag-3102'));
    fireDrag('dragOver', target, 5);
    fireDrag('drop', target, 5);

    await waitFor(() =>
      expect(mockApi.setDispatchOperationOrder).toHaveBeenCalledWith(412, [3102, 3101, 3104], [3101, 3102, 3104])
    );
    expect(mockApi.setDispatchWorkOrderOrder).not.toHaveBeenCalled();
  });

  it('a held lane and an unplanned lane disable operation order with the server reason, and never call it', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitForBoard();

    await expand(user, 'WO-20261101-007');
    expect(screen.getByTestId('lane-op-order-blocked-405')).toHaveTextContent(
      'This work order is on hold. Clear the hold before ordering its operations.'
    );
    const heldDown = screen.getByRole('button', { name: 'Move Op 10 Punch tab down' });
    expect(heldDown).toBeDisabled();
    expect(heldDown).toHaveAttribute(
      'title',
      'This work order is on hold. Clear the hold before ordering its operations.'
    );
    // The pointer path is closed too: not draggable, and a forced drop sends nothing.
    const heldDrag = screen.getByTestId('lane-op-drag-3201');
    expect(heldDrag).toHaveAttribute('draggable', 'false');
    const heldTarget = screen.getByTestId('lane-op-drag-3202');
    stubRect(heldTarget, 0);
    fireDrag('dragStart', heldDrag);
    fireDrag('dragOver', heldTarget, 35);
    fireDrag('drop', heldTarget, 35);

    await expand(user, 'WO-20261104-009');
    expect(screen.getByTestId('lane-op-order-blocked-440')).toHaveTextContent('Add this work order to the plan first.');
    expect(screen.getByRole('button', { name: 'Move Op 10 Shear gusset down' })).toBeDisabled();
    expect(mockApi.setDispatchOperationOrder).not.toHaveBeenCalled();
  });

  it('moves an operation to another machine NON-optimistically, with its version, then re-reads', async () => {
    const user = userEvent.setup();
    let resolveMove: (value: unknown) => void = () => undefined;
    mockApi.updateOperation.mockReturnValue(
      new Promise(resolve => {
        resolveMove = resolve;
      })
    );
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261103-004');

    const select = screen.getByLabelText('Move Op 10 Brake form flange to another machine');
    const options = within(select)
      .getAllByRole('option')
      .map(option => option.textContent);
    // Active machines other than its own; a deactivated machine is never offered.
    expect(options).toEqual([
      'Move to machine…',
      'LASER-1 · Ermaksan Fiber',
      'LASER-2 · Bystronic',
      'WELD-2 · Weld Bay 2',
    ]);
    await user.selectOptions(select, '14');

    expect(mockApi.updateOperation).toHaveBeenCalledWith(3101, { version: 2, work_center_id: 14 });
    // Nothing moved on screen while the server decides.
    expect(
      within(screen.getByRole('region', { name: 'BRAKE-1 operations of WO-20261103-004' })).getByTestId('lane-op-3101')
    ).toBeInTheDocument();
    await act(async () => {
      resolveMove({});
    });
    await waitFor(() => expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2));
    // Reported only once the re-read landed (toast + the status line).
    await waitFor(() =>
      expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
        'Op 10 Brake form flange of WO-20261103-004 moved to WELD-2'
      )
    );
    expect(screen.getAllByText(/Op 10 Brake form flange of WO-20261103-004 moved to WELD-2/)).toHaveLength(2);
    // A PLANNED work order's moved operation stays inside its plan block there.
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
      "It runs within WO-20261103-004's plan block there, after that work order's ranked operations."
    );
    // A full success is a SUCCESS toast — not a warning, not an error.
    const toast = await findToast('Op 10 Brake form flange of WO-20261103-004 moved to WELD-2');
    expect(toast).toHaveClass('bg-green-600');
    expect(toast).toHaveAttribute('role', 'status');

    // Running work is pinned to its machine.
    expect(screen.getByLabelText('Move Op 25 Tack weld to another machine')).toBeDisabled();
  });

  it("an UNPLANNED work order's moved operation is announced as unranked work, never as a plan block", async () => {
    const user = userEvent.setup();
    mockApi.updateOperation.mockResolvedValue({});
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261104-001');

    await user.selectOptions(screen.getByLabelText(/CNC0412 to another machine/), 'LASER-2 · Bystronic');

    await waitFor(() =>
      expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
        "It joins that machine's unranked work, after the plan."
      )
    );
    expect(screen.getByTestId('dispatch-lanes-status')).not.toHaveTextContent('plan block');
  });

  it('a multi-select move that partly fails reports a WARNING naming what did not move', async () => {
    const user = userEvent.setup();
    mockApi.updateOperation.mockResolvedValueOnce({}).mockRejectedValueOnce({
      response: { status: 409, data: { detail: 'Clock out before moving the operation to another work center' } },
    });
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261104-001');

    await user.click(screen.getByRole('checkbox', { name: 'Select Nest 1 Laser Cut - CNC0412' }));
    await user.click(screen.getByRole('checkbox', { name: 'Select Nest 2 Laser Cut - CNC0413' }));
    await user.selectOptions(
      screen.getByLabelText('Move selected operations of WO-20261104-001 to another machine'),
      '10'
    );

    await waitFor(() => expect(mockApi.updateOperation).toHaveBeenCalledTimes(2));
    expect(mockApi.updateOperation).toHaveBeenNthCalledWith(1, 3140, { version: 2, work_center_id: 10 });
    expect(mockApi.updateOperation).toHaveBeenNthCalledWith(2, 3141, { version: 4, work_center_id: 10 });
    await waitFor(() =>
      expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent(
        'Moved 1 of 2 operations of WO-20261104-001 to LASER-2'
      )
    );
    // The toast is the `warning` variant: it interrupts like an error does
    // (role="alert"), because the batch only partly happened — but it is NOT an
    // error (one operation did move) and NOT a success (one did not). role=alert
    // alone cannot tell warning from error, so the variant is asserted directly.
    const toast = await findToast('Moved 1 of 2 operations of WO-20261104-001 to LASER-2');
    expect(toast).toHaveAttribute('role', 'alert');
    expect(toast).toHaveClass('bg-amber-600');
    expect(toast).not.toHaveClass('bg-red-600');
    expect(toast).toHaveTextContent(
      'Not moved: Nest 2 Laser Cut - CNC0413 — Clock out before moving the operation to another work center'
    );
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);
  });

  it('a multi-select move that moves nothing is an ERROR naming each refusal, never a success', async () => {
    const user = userEvent.setup();
    mockApi.updateOperation
      .mockRejectedValueOnce({
        response: { status: 409, data: { detail: 'Operation was modified by someone else. Refresh and try again.' } },
      })
      .mockRejectedValueOnce({ response: { status: 404, data: { detail: 'Work center not found' } } });
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261104-001');

    await user.click(screen.getByRole('checkbox', { name: 'Select Nest 1 Laser Cut - CNC0412' }));
    await user.click(screen.getByRole('checkbox', { name: 'Select Nest 2 Laser Cut - CNC0413' }));
    await user.selectOptions(
      screen.getByLabelText('Move selected operations of WO-20261104-001 to another machine'),
      '10'
    );

    const toast = await findToast('No operations of WO-20261104-001 moved to LASER-2');
    expect(toast).toHaveClass('bg-red-600');
    expect(toast).toHaveTextContent('Nest 1 Laser Cut - CNC0412 — Operation was modified by someone else.');
    expect(toast).toHaveTextContent('Nest 2 Laser Cut - CNC0413 — Work center not found');
    expect(document.querySelector('.bg-green-600')).toBeNull();
    expect(document.querySelector('.bg-amber-600')).toBeNull();
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent('Not moved.');
    // Still re-read: a refusal can mean the board moved under the planner.
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);
  });

  it('marks the material/thickness changeover between consecutive nests of a laser lane', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261104-001');

    expect(screen.getByTestId('lane-changeover-marker-3141')).toHaveTextContent('material change');
    expect(screen.getByTestId('lane-op-nest-3140')).toHaveTextContent('CNC# CNC0412 · A36 · 0.250 · 60x120');
  });
});

describe('DispatchWorkOrderView — filters, freshness', () => {
  it('filters lanes by URL params without renumbering them', async () => {
    renderPage('/dispatch?held=1');
    await waitForBoard();

    expect(planOrder()).toEqual([405]);
    expect(screen.getByTestId('dispatch-lane-position-405')).toHaveTextContent('3');
    expect(screen.getByText('No work orders outside the plan match these filters.')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-lanes-filter-summary')).toHaveTextContent('Showing 1 of 6 work orders.');
  });

  it('a move made while filtered lands next to the visible neighbour, inside the full plan', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    // BRAKE-1 work: A (1) and C (3) are visible; B (2) is hidden by the filter.
    renderPage('/dispatch?machine=12&q=WO-2026110');
    await waitForBoard();
    expect(planOrder()).toEqual([412, 405]);

    await user.click(screen.getByRole('button', { name: 'Move WO-20261101-007 up' }));
    expect(mockApi.setDispatchWorkOrderOrder).toHaveBeenCalledWith([405, 412, 398], [412, 398, 405]);
  });

  it('a background poll replaces the board silently — nothing is announced', async () => {
    renderPage();
    await waitForBoard();
    expect(screen.getByTestId('dispatch-lanes-freshness')).not.toHaveAttribute('aria-live');
    expect(screen.getByTestId('dispatch-lanes-freshness')).toHaveTextContent(
      'refreshes every 10 seconds while visible'
    );

    mockApi.getDispatchWorkOrderBoard.mockResolvedValue(
      makeBoard([
        { ...LANE_C(), dispatch_position: 1 },
        { ...LANE_A(), dispatch_position: 2 },
        { ...LANE_B(), dispatch_position: 3 },
        LANE_D(),
        LANE_E(),
        LANE_F(),
      ])
    );
    fireEvent(window, new Event('focus'));
    await waitFor(() => expect(planOrder()).toEqual([405, 412, 398]));
    expect(screen.getByTestId('dispatch-lanes-status')).toHaveTextContent('');
  });

  it('polls every 10 seconds while visible, never while hidden — and never the Machines view', async () => {
    jest.useFakeTimers();
    try {
      renderPage();
      await act(async () => {});
      expect(screen.getByTestId('dispatch-plan-list')).toBeInTheDocument();
      expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(1);

      await act(async () => {
        jest.advanceTimersByTime(9_999);
      });
      expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(1);
      await act(async () => {
        jest.advanceTimersByTime(1);
      });
      expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);

      // A background tab does not poll.
      const visibility = jest.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
      try {
        await act(async () => {
          jest.advanceTimersByTime(30_000);
        });
        expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);
      } finally {
        visibility.mockRestore();
      }

      // Only the ACTIVE view polls.
      expect(mockApi.getDispatchBoard).not.toHaveBeenCalled();
    } finally {
      act(() => {
        jest.runOnlyPendingTimers();
      });
      jest.useRealTimers();
    }
  });

  it('a poll that fails once the plan is on screen keeps the plan and raises a retryable stale notice', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitForBoard();

    // The deploy window: the next read hits the previous API, which has no lanes route.
    mockApi.getDispatchWorkOrderBoard.mockRejectedValueOnce({
      response: { status: 404, data: { detail: 'Not Found' } },
    });
    fireEvent(window, new Event('focus'));

    const stale = await screen.findByTestId('dispatch-lanes-stale');
    expect(stale).toHaveTextContent('This plan may be out of date');
    expect(stale).toHaveTextContent(/not available from the server yet/);
    // The plan the planner was looking at stays — no blank page, no ErrorState.
    expect(planOrder()).toEqual([412, 398, 405]);
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument();

    await user.click(within(stale).getByRole('button', { name: 'Retry refresh' }));
    await waitFor(() => expect(screen.queryByTestId('dispatch-lanes-stale')).not.toBeInTheDocument());
  });

  it('defers a poll while a lane is dragged and runs it once the drag ends', async () => {
    renderPage();
    await waitForBoard();
    const handle = screen.getByTestId('dispatch-lane-drag-405');
    fireDrag('dragStart', handle);
    fireEvent(window, new Event('focus'));
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('dispatch-lanes-freshness')).toHaveTextContent('Updates deferred');
    fireEvent.dragEnd(handle);
    await waitFor(() => expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2));
  });

  /** Hold the NEXT board read open; returns its resolver. */
  const holdNextRead = () => {
    let resolveRead: (value: DispatchWorkOrderBoardResponse) => void = () => undefined;
    mockApi.getDispatchWorkOrderBoard.mockReturnValueOnce(
      new Promise(resolve => {
        resolveRead = resolve;
      })
    );
    return (value: DispatchWorkOrderBoardResponse) => resolveRead(value);
  };

  /** LANE_A as the server answers after the gusset moved up at BRAKE-1. */
  const laneAGussetFirst = (): DispatchLane => {
    const serverLane = LANE_A();
    const [flange, gusset, ...rest] = serverLane.operations;
    serverLane.operations = [{ ...gusset, run_position: 1 }, { ...flange, run_position: 2 }, ...rest];
    return serverLane;
  };

  it('a poll already in flight when a plan move starts is deferred: the move stays, and a fresh read follows the PUT', async () => {
    const user = userEvent.setup();
    let resolvePut: (value: DispatchWorkOrderBoardResponse) => void = () => undefined;
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(
      new Promise(resolve => {
        resolvePut = resolve;
      })
    );
    renderPage();
    await waitForBoard();

    const resolvePoll = holdNextRead();
    fireEvent(window, new Event('focus'));
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);

    await user.click(screen.getByRole('button', { name: 'Move WO-20261030-002 up' }));
    expect(planOrder()).toEqual([398, 412, 405]);

    // The server answered the poll before the PUT committed: the pre-write plan.
    await act(async () => {
      resolvePoll(board());
    });
    expect(planOrder()).toEqual([398, 412, 405]);
    expect(screen.getByTestId('dispatch-lanes-freshness')).toHaveTextContent('Updates deferred');
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);

    const afterWrite = makeBoard([
      { ...LANE_B(), dispatch_position: 1 },
      { ...LANE_A(), dispatch_position: 2 },
      LANE_C(),
      LANE_D(),
      LANE_E(),
      LANE_F(),
    ]);
    mockApi.getDispatchWorkOrderBoard.mockResolvedValue(afterWrite);
    await act(async () => {
      resolvePut(afterWrite);
    });

    await waitFor(() => expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.getByTestId('dispatch-lanes-freshness')).not.toHaveTextContent('Updates deferred')
    );
    expect(planOrder()).toEqual([398, 412, 405]);
  });

  it('a poll already in flight when an operation reorder starts is deferred: the order stays, and a fresh read follows the PUT', async () => {
    const user = userEvent.setup();
    let resolvePut: (value: DispatchLane) => void = () => undefined;
    mockApi.setDispatchOperationOrder.mockReturnValue(
      new Promise(resolve => {
        resolvePut = resolve;
      })
    );
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261103-004');
    const brake = 'BRAKE-1 operations of WO-20261103-004';

    const resolvePoll = holdNextRead();
    fireEvent(window, new Event('focus'));
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);

    await user.click(screen.getByRole('button', { name: 'Move Op 20 Brake form gusset up' }));
    expect(groupOrder(brake)).toEqual([3102, 3101]);

    await act(async () => {
      resolvePoll(board());
    });
    expect(groupOrder(brake)).toEqual([3102, 3101]);
    expect(screen.getByTestId('dispatch-lanes-freshness')).toHaveTextContent('Updates deferred');
    expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(2);

    // The PUT answers one lane; the poll it displaced is still owed for the rest.
    const serverLane = laneAGussetFirst();
    mockApi.getDispatchWorkOrderBoard.mockResolvedValue(
      makeBoard([serverLane, LANE_B(), LANE_C(), LANE_D(), LANE_E(), LANE_F()])
    );
    await act(async () => {
      resolvePut(serverLane);
    });

    await waitFor(() => expect(mockApi.getDispatchWorkOrderBoard).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.getByTestId('dispatch-lanes-freshness')).not.toHaveTextContent('Updates deferred')
    );
    expect(groupOrder(brake)).toEqual([3102, 3101]);
  });

  it('a plan move drops a Refresh already in flight: it cannot undo the move while the PUT is out', async () => {
    const user = userEvent.setup();
    mockApi.setDispatchWorkOrderOrder.mockReturnValue(new Promise(() => undefined));
    renderPage();
    await waitForBoard();

    const resolveRefresh = holdNextRead();
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await user.click(screen.getByRole('button', { name: 'Move WO-20261030-002 up' }));
    expect(planOrder()).toEqual([398, 412, 405]);

    await act(async () => {
      resolveRefresh(board());
    });

    expect(planOrder()).toEqual([398, 412, 405]);
    expect(screen.getByRole('button', { name: 'Refresh' })).not.toBeDisabled();
  });

  it('an operation reorder drops a Refresh already in flight: the stale read never puts the old order back', async () => {
    const user = userEvent.setup();
    let resolvePut: (value: DispatchLane) => void = () => undefined;
    mockApi.setDispatchOperationOrder.mockReturnValue(
      new Promise(resolve => {
        resolvePut = resolve;
      })
    );
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261103-004');
    const brake = 'BRAKE-1 operations of WO-20261103-004';

    const resolveRefresh = holdNextRead();
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(screen.getByRole('button', { name: 'Refreshing…' })).toBeDisabled();

    await user.click(screen.getByRole('button', { name: 'Move Op 20 Brake form gusset up' }));
    expect(groupOrder(brake)).toEqual([3102, 3101]);

    await act(async () => {
      resolvePut(laneAGussetFirst());
    });
    expect(groupOrder(brake)).toEqual([3102, 3101]);

    // The Refresh was answered before the PUT committed — it carries the old order.
    await act(async () => {
      resolveRefresh(board());
    });

    expect(groupOrder(brake)).toEqual([3102, 3101]);
    expect(screen.getByTestId('lane-op-3102')).toHaveTextContent(/Run\s*1/);
    // The dropped read does not leave the Refresh button spinning.
    expect(screen.getByRole('button', { name: 'Refresh' })).not.toBeDisabled();
  });

  it('a Refresh clicked WHILE an operation reorder is out cannot put the old order back when it lands after the PUT', async () => {
    const user = userEvent.setup();
    let resolvePut: (value: DispatchLane) => void = () => undefined;
    mockApi.setDispatchOperationOrder.mockReturnValue(
      new Promise(resolve => {
        resolvePut = resolve;
      })
    );
    renderPage();
    await waitForBoard();
    await expand(user, 'WO-20261103-004');
    const brake = 'BRAKE-1 operations of WO-20261103-004';

    await user.click(screen.getByRole('button', { name: 'Move Op 20 Brake form gusset up' }));
    expect(groupOrder(brake)).toEqual([3102, 3101]);

    // Refresh stays enabled while the PUT is out; this read is answered by the
    // server BEFORE the write commits, but its response arrives AFTER the PUT's.
    const resolveRefresh = holdNextRead();
    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    await act(async () => {
      resolvePut(laneAGussetFirst());
    });
    expect(groupOrder(brake)).toEqual([3102, 3101]);

    await act(async () => {
      resolveRefresh(board());
    });

    expect(groupOrder(brake)).toEqual([3102, 3101]);
    expect(screen.getByTestId('lane-op-3102')).toHaveTextContent(/Run\s*1/);
    expect(screen.getByRole('button', { name: 'Refresh' })).not.toBeDisabled();
  });
});
