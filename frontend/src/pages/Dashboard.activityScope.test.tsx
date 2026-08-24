/**
 * Live Shop Activity — the progress fraction must keep BOTH halves on ONE scope.
 *
 * A row in this panel IS one assignment to one operation, so the fraction beside its
 * bar has to be that operation's. It used to be an operation-scoped NUMERATOR over a
 * work-order-scoped DENOMINATOR: on production WO-20260821-001 (a laser job — 21
 * nests, `quantity_ordered` 102 = the sum of every nest's `planned_runs`,
 * `quantity_complete` 38) a nest sitting at 0 of 2 runs rendered as **0/102 (0%)** —
 * neither the nest's fraction (0/2, what Dispatch, the WO routing table and Shop Floor
 * Operations all showed) nor the job's (38/102).
 *
 * The denominator now comes from the server as `operation.quantity_ordered`
 * (`operation_target_quantity`), with `operationTargetQuantity` — the shared client
 * mirror of the same rule — covering the DEPLOY-SKEW window in which this frontend
 * (Vercel) is live against an API (Railway) that does not send the field yet. What is
 * pinned here:
 *
 *   - a nest row prints its OWN nest's fraction, and two nests on the SAME work order
 *     with different targets each print their own;
 *   - the ordinary non-nest row still prints 70/100 (no regression);
 *   - a stale payload with neither `quantity_ordered` nor `component_quantity`
 *     degrades to the work-order figure rather than dividing by zero;
 *   - a row with NO operation takes BOTH halves from the work order — never one of
 *     each, which is the defect itself;
 *   - the whole-job figure is still carried, but only in an explicitly-labelled
 *     tooltip segment where it cannot be misread as the operation's.
 *
 * Fixtures are LOCAL to this file on purpose: `Dashboard.dedup.test.tsx`'s base object
 * is spread into five of its own tests, so adding quantity fields there would
 * propagate into assertions that are about something else entirely.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Dashboard from './Dashboard';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getDashboardWithCache: jest.fn(),
    getQualitySummary: jest.fn(),
    getEquipmentDueSoon: jest.fn(),
    getLowStockAlerts: jest.fn(),
    getCapacityHeatmap: jest.fn(),
  },
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));

// SetupNudge pulls usePermissions -> useAuth and the setup-health endpoint, neither of
// which this panel-level test provides. Stub it inert (same as the de-dup suite).
jest.mock('../components/cockpit', () => {
  const actual = jest.requireActual('../components/cockpit');
  return { __esModule: true, ...actual, SetupNudge: () => null };
});
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workCenter = { id: 1, code: 'LASER-1', name: 'Laser cell 1', status: 'in_use', type: 'laser' };

/** The production laser job, reduced to its two load-bearing numbers. */
const laserWorkOrder = {
  id: 5501,
  work_order_number: 'WO-20260821-001',
  status: 'in_progress',
  part_number: 'PN-880',
  part_name: 'Nest package',
  customer_name: 'Miratech',
  priority: 2,
  due_date: '2026-09-30',
  quantity_ordered: 102,
  quantity_complete: 38,
};

/** An ordinary routed job: one operation, processing the whole order. */
const routedWorkOrder = {
  id: 3041,
  work_order_number: 'WO-3041',
  status: 'in_progress',
  part_number: 'PN-441',
  part_name: 'Mount bracket',
  customer_name: 'Acme Aero',
  priority: 2,
  due_date: '2026-09-30',
  quantity_ordered: 100,
  quantity_complete: 70,
};

const operator = (id: number, name: string) => ({
  id,
  employee_id: `E${String(id).padStart(3, '0')}`,
  name,
  role: 'operator',
  department: 'Production',
});

type AssignmentOverrides = {
  timeEntryId: number;
  userId?: number;
  userName?: string;
  clockIn?: string;
  workOrder?: typeof laserWorkOrder;
  operation: Record<string, unknown>;
};

const makeAssignment = ({
  timeEntryId,
  userId = 11,
  userName = 'Alex Reyes',
  clockIn = '2026-08-24T14:00:00Z',
  workOrder = laserWorkOrder,
  operation,
}: AssignmentOverrides) => ({
  time_entry_id: timeEntryId,
  clock_in: clockIn,
  entry_type: 'run',
  user: operator(userId, userName),
  work_order: workOrder,
  operation,
  work_center: workCenter,
});

const dashboardWith = (assignments: unknown[]) => ({
  summary: {
    active_work_orders: 1,
    due_today: 0,
    overdue: 0,
    signed_in_users: assignments.length,
    checked_in_users: assignments.length,
    idle_signed_in_users: 0,
    completed_today: 0,
  },
  work_centers: [
    {
      ...workCenter,
      active_operations: assignments.length,
      queued_operations: 0,
      active_people_count: assignments.length,
      active_people: [],
    },
  ],
  signed_in_users: [],
  active_assignments: assignments,
  recent_completions: [],
});

const heatmap = {
  start_date: '2026-08-24',
  end_date: '2026-08-24',
  overload_cells: 0,
  overloaded_work_centers: [],
  work_centers: [],
};

const renderWith = (assignments: unknown[]) => {
  mockedApi.getDashboardWithCache.mockResolvedValue({
    data: dashboardWith(assignments) as any,
    fromCache: false,
    changed: true,
  });
  return render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>
  );
};

const rowFor = (timeEntryId: number) => document.getElementById(`assign-${timeEntryId}`) as HTMLElement;

/** The bar's inline width — clamped even when the printed percentage is not. */
const barWidth = (row: HTMLElement) =>
  (row.querySelector('.bg-werco-500') as HTMLElement | null)?.style.width ?? null;

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView; the panel cross-links call it.
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 0 } as any);
  mockedApi.getEquipmentDueSoon.mockResolvedValue([] as any);
  mockedApi.getLowStockAlerts.mockResolvedValue([] as any);
  mockedApi.getCapacityHeatmap.mockResolvedValue(heatmap as any);
});

describe('a laser nest row prints its OWN nest fraction, not the job total', () => {
  it('renders 0/2 (0%) for a nest at zero of two on a job ordered 102 — never 0/102', async () => {
    renderWith([
      makeAssignment({
        timeEntryId: 201,
        operation: {
          id: 91,
          operation_number: 'Nest 11',
          name: 'Laser Cut - N11',
          status: 'in_progress',
          sequence: 110,
          quantity_ordered: 2,
          component_quantity: 2,
          quantity_complete: 0,
          quantity_scrapped: 0,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    const row = rowFor(201);
    expect(row).not.toBeNull();
    expect(within(row).getByText('0/2 (0%)')).toBeInTheDocument();
    // The exact string the defect printed.
    expect(row.textContent).not.toContain('0/102');
    expect(row.textContent).not.toContain('102');
  });

  it('gives two nests on the SAME work order their own targets (0/2 and 4/16)', async () => {
    renderWith([
      makeAssignment({
        timeEntryId: 201,
        clockIn: '2026-08-24T14:00:00Z',
        operation: {
          id: 91,
          operation_number: 'Nest 11',
          name: 'Laser Cut - N11',
          status: 'in_progress',
          sequence: 110,
          quantity_ordered: 2,
          component_quantity: 2,
          quantity_complete: 0,
          quantity_scrapped: 0,
        },
      }),
      makeAssignment({
        timeEntryId: 202,
        userId: 12,
        userName: 'Dana Ruiz',
        clockIn: '2026-08-24T15:00:00Z',
        operation: {
          id: 92,
          operation_number: 'Nest 12',
          name: 'Laser Cut - N12',
          status: 'in_progress',
          sequence: 120,
          quantity_ordered: 16,
          component_quantity: 16,
          quantity_complete: 4,
          quantity_scrapped: 0,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    // Same work order on both rows — so a shared denominator would be invisible
    // unless the two targets differ, which is exactly why they do here.
    expect(within(rowFor(201)).getByText('0/2 (0%)')).toBeInTheDocument();
    expect(within(rowFor(202)).getByText('4/16 (25%)')).toBeInTheDocument();
    expect(rowFor(202).textContent).not.toContain('/102');
  });

  it('keeps the whole-job figure, but only as an explicitly labelled tooltip segment', async () => {
    renderWith([
      makeAssignment({
        timeEntryId: 201,
        operation: {
          id: 91,
          operation_number: 'Nest 11',
          name: 'Laser Cut - N11',
          status: 'in_progress',
          sequence: 110,
          quantity_ordered: 2,
          component_quantity: 2,
          quantity_complete: 0,
          quantity_scrapped: 0,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    const row = rowFor(201);
    // Nothing is lost — the job's own progress is still on the row…
    expect(row.title).toContain('Work order total 38/102');
    // …but it is NOT the number beside the bar.
    expect(within(row).getByText('0/2 (0%)')).toBeInTheDocument();
  });

  it('prints an over-completed nest unclamped (3/2 = 150%) while clamping the bar', async () => {
    // Reachable at nest scale, where the denominator is one sheet count rather than
    // the whole job's. A clamped "3/2 (100%)" would contradict the fraction beside it.
    renderWith([
      makeAssignment({
        timeEntryId: 203,
        operation: {
          id: 93,
          operation_number: 'Nest 3',
          name: 'Laser Cut - N3',
          status: 'in_progress',
          sequence: 30,
          quantity_ordered: 2,
          component_quantity: 2,
          quantity_complete: 3,
          quantity_scrapped: 0,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    const row = rowFor(203);
    expect(within(row).getByText('3/2 (150%)')).toBeInTheDocument();
    expect(barWidth(row)).toBe('100%');
  });
});

describe('non-nest and stale-payload rows still read the work-order figure', () => {
  it('no regression: an ordinary routed operation renders 70/100 (70%)', async () => {
    renderWith([
      makeAssignment({
        timeEntryId: 101,
        workOrder: routedWorkOrder,
        operation: {
          id: 1,
          operation_number: '10',
          name: 'Deburr',
          status: 'in_progress',
          sequence: 1,
          // The server resolves the rule's second tier for a plain routing op: no
          // component_quantity, so the target IS the work order's quantity_ordered.
          quantity_ordered: 100,
          component_quantity: null,
          quantity_complete: 70,
          quantity_scrapped: 0,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    expect(within(rowFor(101)).getByText('70/100 (70%)')).toBeInTheDocument();
  });

  it('deploy-skew payload (no quantity_ordered, no component_quantity) degrades to the work-order figure', async () => {
    // The SPA and the API deploy independently, so this build can be live against a
    // backend that does not send the field yet. It must degrade to the previous
    // behavior — NOT to a zero denominator, which would print "70/0" and NaN%.
    // (Deliberately not "an ETag-cached body": that cache is an in-memory Map which
    // starts empty every page load, so it can never hold a payload older than the
    // bundle reading it. Deploy skew is the real and only window.)
    renderWith([
      makeAssignment({
        timeEntryId: 102,
        workOrder: routedWorkOrder,
        operation: {
          id: 1,
          operation_number: '10',
          name: 'Deburr',
          status: 'in_progress',
          sequence: 1,
          quantity_complete: 70,
          quantity_scrapped: 0,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    const row = rowFor(102);
    expect(within(row).getByText('70/100 (70%)')).toBeInTheDocument();
    expect(row.textContent).not.toContain('70/0');
    expect(row.textContent).not.toContain('NaN');
    expect(barWidth(row)).toBe('70%');
  });

  it('a stale payload on a NEST still reaches the nest target via component_quantity', async () => {
    // The client mirror reads the same raw input the server rule reads first, so a
    // payload that carries component_quantity but not the resolved target still lands
    // on the nest's number rather than the job's.
    renderWith([
      makeAssignment({
        timeEntryId: 204,
        operation: {
          id: 94,
          operation_number: 'Nest 11',
          name: 'Laser Cut - N11',
          status: 'in_progress',
          sequence: 110,
          component_quantity: 2,
          quantity_complete: 1,
          quantity_scrapped: 0,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    const row = rowFor(204);
    expect(within(row).getByText('1/2 (50%)')).toBeInTheDocument();
    expect(row.textContent).not.toContain('/102');
  });
});

describe('a row with no operation takes BOTH halves from the work order', () => {
  it('renders the work order pair (38/102), never an operation numerator over it', async () => {
    // Indirect / setup labor: the server sends operation.id null and quantity_ordered
    // null. Falling back one half at a time is the defect this suite exists for, so
    // the numerator must be the work order's 38 — with the denominator its own 102.
    renderWith([
      makeAssignment({
        timeEntryId: 301,
        operation: {
          id: null,
          operation_number: null,
          name: null,
          status: null,
          sequence: null,
          quantity_ordered: null,
          component_quantity: null,
          quantity_complete: null,
          quantity_scrapped: null,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    const row = rowFor(301);
    expect(within(row).getByText('38/102 (37%)')).toBeInTheDocument();
    // No redundant "Work order total" segment: the inline fraction already IS it.
    expect(row.title).not.toContain('Work order total');
  });

  it('does not fall back to a zero denominator when the work order carries no quantity', async () => {
    renderWith([
      makeAssignment({
        timeEntryId: 302,
        workOrder: { ...laserWorkOrder, quantity_ordered: 0, quantity_complete: 0 },
        operation: {
          id: null,
          operation_number: null,
          name: null,
          status: null,
          sequence: null,
          quantity_ordered: null,
          component_quantity: null,
          quantity_complete: null,
          quantity_scrapped: null,
        },
      }),
    ]);
    await screen.findByText('Live Shop Activity');

    const row = rowFor(302);
    // 0/0 is printed as a plain 0% — not NaN%, not Infinity%.
    expect(within(row).getByText('0/0 (0%)')).toBeInTheDocument();
    expect(row.textContent).not.toContain('NaN');
    expect(row.textContent).not.toContain('Infinity');
  });
});
