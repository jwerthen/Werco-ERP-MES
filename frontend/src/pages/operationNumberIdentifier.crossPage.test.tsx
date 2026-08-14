/**
 * Legacy and new operation numbers render IDENTICALLY.
 *
 * `WorkOrderNew` used to mint a display label (`Op 10`) into the identifier
 * column `operation_number`; it now stores the bare `10`. There is deliberately
 * NO backfill, so the column is permanently mixed — a work order released last
 * month holds `Op 10`, one released tomorrow holds `10`, and the two can sit in
 * the same table.
 *
 * That makes the display fix the load-bearing half. Without it, fixing the write
 * path alone would have turned an ugly-but-uniform "Op Op 10" into something
 * worse: two different strings for the same operation number, in one column,
 * with no rule a reader could infer. So every assertion here is an EQUIVALENCE —
 * both stored spellings, rendered by the real page, must produce the same text.
 * Asserting only the new shape would pass on a page that still prints the legacy
 * value raw.
 *
 * The pages are rendered for real rather than calling the helper directly: the
 * defect lives in JSX interpolation, and a unit test of the helper passed the
 * whole time these screens were broken.
 */

import React from 'react';
import { render, screen, fireEvent, within, cleanup } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import RoutingPage from './Routing';
import ShopFloor from './ShopFloor';
import PrintTraveler from './PrintTraveler';
import { PartRoutingTab } from '../components/parts/PartRoutingTab';
import { ToastProvider } from '../components/ui';
import type { Part } from '../types';
import type { Routing, RoutingOperation, WorkCenter as EngWorkCenter } from '../types/engineering';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    // Routing page
    getRoutings: jest.fn(),
    getRouting: jest.fn(),
    getParts: jest.fn(),
    getPart: jest.fn(),
    getWorkCenters: jest.fn(),
    getProcessSheets: jest.fn(),
    getProcessSheet: jest.fn(),
    updateRoutingOperation: jest.fn(),
    addRoutingOperation: jest.fn(),
    deleteRoutingOperation: jest.fn(),
    releaseRouting: jest.fn(),
    deleteRouting: jest.fn(),
    createRouting: jest.fn(),
    // ShopFloor
    getMyActiveJob: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkOrder: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    updateWorkOrderPriority: jest.fn(),
    createWorkOrderBlocker: jest.fn(),
    // PrintTraveler
    getMaterialRequirements: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false, first_name: 'Quinn', last_name: 'Printer' },
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

jest.mock('qrcode', () => ({
  __esModule: true,
  default: { toDataURL: jest.fn(async () => 'data:image/png;base64,stub') },
}));

const mockedApi = api as jest.Mocked<typeof api>;

/**
 * The two stored spellings of the SAME operation number: what the create form
 * wrote before the identifier fix, and what it writes now.
 */
const LEGACY = 'Op 10';
const CURRENT = '10';
const SPELLINGS: Array<[string, string]> = [
  ['a legacy row (stored "Op 10")', LEGACY],
  ['a new row (stored "10")', CURRENT],
];

/**
 * The text of the first body cell under a given column header.
 *
 * Resolved by the header's own `cellIndex` rather than a hard-coded column
 * position, because two of these tables carry a leading QR / expander column.
 */
function firstBodyCellUnder(headerName: string, root: HTMLElement = document.body): string {
  const header = within(root).getByRole('columnheader', { name: headerName }) as HTMLTableCellElement;
  const table = header.closest('table') as HTMLTableElement;
  // The first row carrying a DATA cell in this column -- deliberately not
  // `table.querySelector('tbody tr')`. ShopFloor's drilldown table is nested
  // inside the queue table's own `<tbody>`, and a descendant combinator on a
  // scoped `querySelector` still resolves ancestors outside the scope element:
  // the drilldown's HEADER row matches `tbody tr` via the OUTER table's tbody,
  // so that selector silently returns the header and every assertion reads
  // "Op #". Walking `table.rows` and taking the first `<td>` cannot be fooled.
  const dataRow = Array.from(table.rows).find(
    (row) => row.cells[header.cellIndex]?.tagName === 'TD'
  );
  if (!dataRow) throw new Error(`no body row under the "${headerName}" column`);
  return (dataRow.cells[header.cellIndex].textContent || '').trim();
}

/**
 * Run one render per stored spelling and hand back both results.
 *
 * `cleanup()` between them because both renders happen inside a single `it` —
 * the equivalence is the assertion, so splitting it across tests would lose it.
 */
async function bothSpellings(renderOnce: (stored: string) => Promise<string>): Promise<string[]> {
  const results: string[] = [];
  for (const [, stored] of SPELLINGS) {
    jest.clearAllMocks();
    results.push(await renderOnce(stored));
    cleanup();
  }
  return results;
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const ENG_WORK_CENTER: EngWorkCenter = {
  id: 5,
  code: 'WC-LASER',
  name: 'Laser Cell',
  work_center_type: 'fabrication',
  hourly_rate: 95,
};

const routingOperation = (operationNumber: string): RoutingOperation => ({
  id: 100,
  routing_id: 1,
  sequence: 10,
  operation_number: operationNumber,
  name: 'Laser Cut',
  description: 'Cut blank to size',
  work_center_id: 5,
  work_center: ENG_WORK_CENTER,
  setup_hours: 0.5,
  run_hours_per_unit: 0.1,
  move_hours: 0,
  queue_hours: 0,
  is_inspection_point: false,
  is_outside_operation: false,
  is_active: true,
});

const routingFixture = (operationNumber: string, status: Routing['status'] = 'draft'): Routing => ({
  id: 1,
  part_id: 10,
  part: { id: 10, part_number: 'PN-REL', name: 'Released Bracket', part_type: 'manufactured' },
  revision: 'A',
  status,
  is_active: true,
  total_setup_hours: 0.5,
  total_run_hours_per_unit: 0.1,
  total_labor_cost: 57,
  total_overhead_cost: 0,
  operations: [routingOperation(operationNumber)],
  created_at: '2026-01-01T00:00:00Z',
});

const PART: Part = {
  id: 10,
  version: 1,
  part_number: 'PN-REL',
  revision: 'A',
  name: 'Released Bracket',
  part_type: 'manufactured',
  unit_of_measure: 'EA',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

// ---------------------------------------------------------------------------
// 1 + 2. Routing page — the "Op #" column and the released-edit heading
// ---------------------------------------------------------------------------

describe('Routing page', () => {
  const setup = (stored: string, status: Routing['status']) => {
    const routing = routingFixture(stored, status);
    mockedApi.getRoutings.mockResolvedValue([routing]);
    mockedApi.getRouting.mockResolvedValue(routing);
    mockedApi.getParts.mockResolvedValue([PART]);
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getProcessSheets.mockResolvedValue([]);
    render(
      <MemoryRouter>
        <ToastProvider>
          <RoutingPage />
        </ToastProvider>
      </MemoryRouter>
    );
  };

  it('prints the BARE identifier under its own "Op #" header — same text for both spellings', async () => {
    const [legacy, current] = await bothSpellings(async (stored) => {
      setup(stored, 'draft');
      fireEvent.click(await screen.findByText('PN-REL'));
      await screen.findByRole('columnheader', { name: 'Op #' });
      return firstBodyCellUnder('Op #');
    });

    // The header already says "Op"; the cell must not say it again.
    expect(legacy).toBe('10');
    expect(current).toBe('10');
    expect(legacy).toBe(current);
  });

  it('uses the FULL label in the released-edit heading, where no header supplies the word', async () => {
    const [legacy, current] = await bothSpellings(async (stored) => {
      setup(stored, 'released');
      fireEvent.click(await screen.findByText('PN-REL'));
      fireEvent.click(await screen.findByRole('button', { name: /edit times/i }));
      const heading = await screen.findByText(/— Laser Cut$/);
      return (heading.textContent || '').trim();
    });

    // This value stands alone rather than under an "Op #" column, so it keeps the
    // prefix — and a blank number would otherwise print a dangling "— Laser Cut".
    expect(legacy).toBe('Op 10 — Laser Cut');
    expect(current).toBe('Op 10 — Laser Cut');
    expect(legacy).toBe(current);
    expect(legacy).not.toMatch(/Op\s+Op/i);
  });
});

// ---------------------------------------------------------------------------
// 3. PartRoutingTab — the "Op #" column on a part's Routing tab
// ---------------------------------------------------------------------------

describe('PartRoutingTab', () => {
  it('prints the BARE identifier under its own "Op #" header — same text for both spellings', async () => {
    const [legacy, current] = await bothSpellings(async (stored) => {
      mockedApi.getWorkCenters.mockResolvedValue([]);
      render(
        <ToastProvider>
          <PartRoutingTab part={PART} routing={routingFixture(stored)} onRoutingChanged={jest.fn()} />
        </ToastProvider>
      );
      await screen.findByRole('columnheader', { name: 'Op #' });
      return firstBodyCellUnder('Op #');
    });

    expect(legacy).toBe('10');
    expect(current).toBe('10');
    expect(legacy).toBe(current);
  });
});

// ---------------------------------------------------------------------------
// 4. ShopFloor — the "Op #" column in the expanded work-order drilldown
// ---------------------------------------------------------------------------

describe('ShopFloor work-order drilldown', () => {
  const queueItem = (stored: string) => ({
    operation_id: 101,
    work_order_id: 1001,
    work_order_number: 'WO-20260807-006',
    part_number: 'PN-441',
    part_name: 'Skid Fit',
    operation_number: stored,
    operation_name: 'Skid Fit',
    status: 'ready',
    quantity_ordered: 10,
    quantity_complete: 0,
    priority: 5,
    due_date: '2099-01-05',
    setup_time_hours: 1,
    run_time_hours: 4,
    run_order: null,
  });

  it('prints the BARE identifier under its own "Op #" header — same text for both spellings', async () => {
    const [legacy, current] = await bothSpellings(async (stored) => {
      mockedApi.getWorkCenters.mockResolvedValue([
        {
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
        },
      ]);
      mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
      mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [queueItem(stored)] });
      mockedApi.getWorkOrder.mockResolvedValue({
        id: 1001,
        work_order_number: 'WO-20260807-006',
        quantity_ordered: 10,
        quantity_complete: 0,
        quantity_scrapped: 0,
        operations: [
          {
            id: 101,
            // The column is FREE TEXT even though ShopFloor's local interface
            // declares it a number — see the note in the report.
            operation_number: stored,
            name: 'Skid Fit',
            work_center_name: 'CNC Mill 1',
            status: 'ready',
            estimated_hours: 5,
            actual_hours: 0,
          },
        ],
      });

      render(
        <MemoryRouter>
          <ShopFloor />
        </MemoryRouter>
      );

      // Expanding the queue row is what loads and renders the drilldown table.
      // Located via the QUEUE table's own "Work Order" header -- the work-order
      // number also appears in the compact "up next" strip above it.
      const queueTable = (await screen.findByRole('columnheader', { name: 'Work Order' }))
        .closest('table') as HTMLTableElement;
      fireEvent.click(queueTable.querySelector('tbody tr') as HTMLElement);
      await screen.findByRole('columnheader', { name: 'Op #' });
      return firstBodyCellUnder('Op #');
    });

    expect(legacy).toBe('10');
    expect(current).toBe('10');
    expect(legacy).toBe(current);
  });
});

// ---------------------------------------------------------------------------
// 5. PrintTraveler — the "Op #" column on the printed shop document
// ---------------------------------------------------------------------------

describe('PrintTraveler routing table', () => {
  const workOrder = (stored: string) => ({
    id: 42,
    work_order_number: 'WO-20260812-003',
    part_id: 10,
    part_number: 'PN-REL',
    part_name: 'Released Bracket',
    status: 'in_progress',
    priority: 3,
    quantity_ordered: 8,
    quantity_complete: 0,
    customer_name: 'Acme Aero',
    customer_po: 'PO-1',
    due_date: '2026-08-20',
    operations: [
      {
        id: 101,
        sequence: 10,
        operation_number: stored,
        name: 'Laser Cut',
        work_center_id: 5,
        work_center_name: 'Laser Cell',
        status: 'pending',
        setup_time_hours: 0.5,
        run_time_hours: 2,
      },
    ],
  });

  const renderTraveler = () =>
    render(
      <MemoryRouter initialEntries={['/work-orders/42/traveler']}>
        <Routes>
          <Route path="/work-orders/:id/traveler" element={<PrintTraveler />} />
        </Routes>
      </MemoryRouter>
    );

  it('prints the BARE identifier under the "Op #" header — same text for both spellings', async () => {
    const [legacy, current] = await bothSpellings(async (stored) => {
      mockedApi.getWorkOrder.mockResolvedValue(workOrder(stored));
      mockedApi.getPart.mockResolvedValue(PART);
      mockedApi.getMaterialRequirements.mockResolvedValue({
        work_order_id: 42,
        work_order_number: 'WO-20260812-003',
        quantity_ordered: 8,
        has_bom: false,
        materials: [],
      });
      renderTraveler();
      await screen.findByText('WORK ORDER TRAVELER');
      return firstBodyCellUnder('Op #');
    });

    // On paper the header reads "Op #" and the cell reads "10" — one number under
    // a name that matches what the cell holds. The header is deliberately NOT
    // "Seq": the cell prints `operation_number`, which the create form seeds from
    // the sequence but never re-derives when the planner edits Seq afterwards, so
    // the two can legitimately differ on the same row.
    expect(legacy).toBe('10');
    expect(current).toBe('10');
    expect(legacy).toBe(current);
  });

  it('prints a customer-mandated identifier WHOLE — "OP-10A" prints "10A", not "10"', async () => {
    // The reason the column is sourced from `operation_number` and not `sequence`:
    // when a customer print mandates op "10A", that string is what the shop, the
    // traveler and the customer's own paperwork all reference. Printing the
    // sequence beside it would drop the "A" from a controlled document.
    mockedApi.getWorkOrder.mockResolvedValue(workOrder('OP-10A'));
    mockedApi.getPart.mockResolvedValue(PART);
    mockedApi.getMaterialRequirements.mockResolvedValue({
      work_order_id: 42,
      work_order_number: 'WO-20260812-003',
      quantity_ordered: 8,
      has_bom: false,
      materials: [],
    });
    renderTraveler();
    await screen.findByText('WORK ORDER TRAVELER');

    // `10A`, not `10` (the sequence) and not `OP-10A` (the header carries the noun).
    expect(firstBodyCellUnder('Op #')).toBe('10A');
  });

  it('still falls back to the numeric sequence when no number is stored', async () => {
    mockedApi.getWorkOrder.mockResolvedValue({
      ...workOrder(''),
      operations: [{ ...workOrder('').operations[0], operation_number: '', sequence: 30 }],
    });
    mockedApi.getPart.mockResolvedValue(PART);
    mockedApi.getMaterialRequirements.mockResolvedValue({
      work_order_id: 42,
      work_order_number: 'WO-20260812-003',
      quantity_ordered: 8,
      has_bom: false,
      materials: [],
    });
    renderTraveler();
    await screen.findByText('WORK ORDER TRAVELER');

    // The `|| op.sequence` chain is preserved: `operationNumberText` returns ''
    // for a blank, so a traveler never prints an empty "Op #" cell.
    expect(firstBodyCellUnder('Op #')).toBe('30');
  });
});
