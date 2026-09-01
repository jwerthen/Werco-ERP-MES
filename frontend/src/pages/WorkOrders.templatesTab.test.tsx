/**
 * WorkOrders — the Templates tab.
 *
 * Templates is a TAB on `/work-orders`, not a route, and that is a decision with
 * a verifiable cause: `/work-orders/templates` is matched by App.tsx's
 * `/work-orders/:id` route AND by routeMeta's WO-detail pattern
 * `^\/work-orders\/(?!new$)[^/]+$`, so a real route there would resolve as a work
 * order whose id is the word "templates".
 *
 * Three properties are locked here:
 *
 * 1. **A fresh deep link to `?tab=templates` paints the catalog.** The page
 *    early-returns a full-page skeleton while `loading` is true, and `loading` is
 *    cleared ONLY by the work-order fetch. So the templates branch has to return
 *    ABOVE that gate — otherwise the deep link lands on a work-order skeleton
 *    with the tab it asked for unreachable, and stays there until an unrelated
 *    fetch finishes. The test holds the work-order fetch open forever to prove it.
 *
 * 2. **Switching tabs preserves the other filters.** The tab rides on `?tab=`
 *    through the page's own copy-and-set `setFilterParam`, which COPIES the
 *    existing params. Building a fresh `URLSearchParams` (as a neighbouring page
 *    does) would silently drop the status / customer / COTS / grouping filters
 *    the planner had set.
 *
 * 3. **Every template control is gated on `work_orders:edit`.** That permission
 *    maps to exactly the backend's ADMIN/MANAGER/SUPERVISOR trio, which gates
 *    every `/work-order-templates` verb — READS included. So a user without it
 *    sees no tab, and a deep link falls back to the list rather than rendering a
 *    panel that can only 403.
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';

import api from '../services/api';
import WorkOrders from './WorkOrders';
import type { WorkOrderTemplate } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrders: jest.fn(),
    deleteWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    listWorkOrderTemplates: jest.fn(),
    createWorkOrderTemplate: jest.fn(),
    updateWorkOrderTemplate: jest.fn(),
    deleteWorkOrderTemplate: jest.fn(),
    useWorkOrderTemplate: jest.fn(),
  },
}));

const mockRole = { current: 'admin' as string };

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: mockRole.current, is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const workOrders = [
  {
    id: 1,
    work_order_number: 'WO-1001',
    part_id: 10,
    work_order_type: 'production',
    part_number: 'PN-AAA',
    part_name: 'Bracket Assembly',
    part_type: 'manufactured',
    status: 'draft' as const,
    priority: 2,
    quantity_ordered: 50,
    quantity_complete: 0,
    customer_name: 'Acme Aero',
  },
];

const template: WorkOrderTemplate = {
  id: 7,
  name: 'Bracket brake set',
  notes: null,
  source_work_order_id: 42,
  default_quantity: 12,
  created_at: '2026-08-20T12:00:00Z',
  updated_at: '2026-08-20T12:00:00Z',
  created_by: 3,
  plan: {
    available: true,
    unavailable_reason: null,
    source_work_order_number: 'WO-20260501-004',
    source_status: 'complete',
    work_order_type: 'production',
    sequential_operations: true,
    priority: 3,
    operation_count: 4,
    nest_count: 0,
    planned_runs_total: 0,
    open_material_tie_count: 0,
    work_centers: ['BRAKE-2'],
    source_quantity_ordered: 50,
  },
};

/** Exposes the live URL so param round-trips AND navigations are assertable. */
function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <div data-testid="location-search">{location.search}</div>
      <div data-testid="location-pathname">{location.pathname}</div>
    </>
  );
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <WorkOrders />
      <LocationProbe />
    </MemoryRouter>
  );
}

const locationSearch = () => screen.getByTestId('location-search').textContent;
const locationPathname = () => screen.getByTestId('location-pathname').textContent;

/**
 * The work-order row, as ALL its renderings. DataTable mounts the desktop table
 * AND the mobile cards into the DOM (CSS hides one per breakpoint; jsdom applies
 * none), so a single-match query throws on a row that is legitimately there twice.
 */
const findWorkOrderRow = () => screen.findAllByText('WO-1001');

beforeEach(() => {
  jest.clearAllMocks();
  mockRole.current = 'admin';
  mockedApi.getWorkOrders.mockResolvedValue(workOrders);
  mockedApi.listWorkOrderTemplates.mockResolvedValue({ templates: [template], total: 1 });
});

describe('WorkOrders: ?tab=templates renders the catalog', () => {
  it('renders the templates panel on a deep link', async () => {
    renderAt('/work-orders?tab=templates');

    expect(await screen.findByText('Bracket brake set')).toBeInTheDocument();
    expect(mockedApi.listWorkOrderTemplates).toHaveBeenCalled();
    // The work-order list is not rendered underneath it.
    expect(screen.queryAllByText('WO-1001')).toHaveLength(0);
  });

  it('paints even while the WORK ORDER fetch is still outstanding', async () => {
    // THE trap. The page early-returns a full-page skeleton while `loading` is
    // true, and only the work-order fetch clears it — so a templates branch below
    // that gate would leave this deep link on a skeleton forever.
    mockedApi.getWorkOrders.mockReturnValue(new Promise(() => {}));

    renderAt('/work-orders?tab=templates');

    expect(await screen.findByText('Bracket brake set')).toBeInTheDocument();
    // The header and the tab strip render above the gate too, so the tab the
    // planner asked for is reachable rather than hidden behind the skeleton.
    expect(screen.getByRole('heading', { name: 'Work Orders' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Templates' })).toBeInTheDocument();
  });

  it('defaults to the work-order list with no tab param, and keeps the URL clean', async () => {
    renderAt('/work-orders');

    expect(await findWorkOrderRow()).not.toHaveLength(0);
    expect(mockedApi.listWorkOrderTemplates).not.toHaveBeenCalled();
    expect(locationSearch()).toBe('');
  });
});

describe('WorkOrders: switching tabs preserves the other filters', () => {
  it('adds ?tab=templates WITHOUT dropping an existing status filter', async () => {
    // The page's own copy-and-set setter COPIES the current params. A fresh
    // URLSearchParams here would wipe status / customer / COTS / grouping.
    renderAt('/work-orders?status=in_progress');
    await findWorkOrderRow();

    await userEvent.click(screen.getByRole('button', { name: 'Templates' }));

    await waitFor(() => expect(screen.getByText('Bracket brake set')).toBeInTheDocument());
    const search = new URLSearchParams(locationSearch() ?? '');
    expect(search.get('tab')).toBe('templates');
    expect(search.get('status')).toBe('in_progress');
  });

  it('drops the tab param entirely on the way back, keeping the default URL clean', async () => {
    renderAt('/work-orders?status=in_progress&tab=templates');
    await screen.findByText('Bracket brake set');

    await userEvent.click(screen.getByRole('button', { name: 'Work Orders' }));

    await waitFor(() => expect(screen.getAllByText('WO-1001').length).toBeGreaterThan(0));
    const search = new URLSearchParams(locationSearch() ?? '');
    expect(search.has('tab')).toBe(false);
    expect(search.get('status')).toBe('in_progress');
  });

  it('"New from template" in the header switches to the tab', async () => {
    renderAt('/work-orders');
    await findWorkOrderRow();

    await userEvent.click(screen.getByRole('button', { name: /New from template/i }));

    expect(await screen.findByText('Bracket brake set')).toBeInTheDocument();
  });
});

describe('WorkOrders: the row action that fills the catalog', () => {
  it('opens the save-as-template dialog from a list row', async () => {
    renderAt('/work-orders');
    await findWorkOrderRow();

    // Desktop table and mobile cards BOTH mount in jsdom, so the control is
    // legitimately present more than once — click the first.
    const [saveButton] = screen.getAllByRole('button', { name: /Save WO-1001 as a template/i });
    await userEvent.click(saveButton);

    expect(await screen.findByText('Save as template — WO-1001')).toBeInTheDocument();
    // It writes ONE row pointing at the work order — nothing is copied now.
    expect(screen.getByRole('dialog')).toHaveTextContent(/nothing on that work order changes/i);
  });
});

describe('WorkOrders: the tab is gated on work_orders:edit', () => {
  it('hides the tab strip and the header button from a role without it', async () => {
    // `work_orders:edit` maps to exactly the trio the backend requires on EVERY
    // template verb, reads included — an operator would only ever get a 403.
    mockRole.current = 'operator';
    renderAt('/work-orders');

    await findWorkOrderRow();
    expect(screen.queryByRole('button', { name: 'Templates' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /New from template/i })).not.toBeInTheDocument();
  });

  it('falls back to the work-order list on a deep link it may not read', async () => {
    mockRole.current = 'operator';
    renderAt('/work-orders?tab=templates');

    expect(await findWorkOrderRow()).not.toHaveLength(0);
    expect(mockedApi.listWorkOrderTemplates).not.toHaveBeenCalled();
  });
});

/**
 * Where a template's output goes, which is TWO answers rather than one.
 *
 * ONE copy has a destination and keeps the Duplicate dialog's hand-off: land on
 * the new draft, because nobody has reviewed it yet and reviewing it is the whole
 * point of this door.
 *
 * SEVERAL have no single destination. Five drafts have five numbers, and picking
 * one to navigate to would silently make the other four the ones the planner did
 * not see — so the page refreshes the list instead and lets the dialog, which is
 * the only surface that names them all, stay up.
 */
describe('WorkOrders: where a template use hands off to', () => {
  const draft = {
    id: 501,
    version: 1,
    work_order_number: 'WO-20260901-001',
    part_id: 10,
    work_order_type: 'production',
    quantity_ordered: 1,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'draft' as const,
    priority: 3,
    estimated_hours: 0,
    actual_hours: 0,
    created_at: '2026-09-01T12:00:00Z',
    updated_at: '2026-09-01T12:00:00Z',
    operations: [],
  };
  const second = { ...draft, id: 502, work_order_number: 'WO-20260901-002' };

  async function openTheUseDialog() {
    renderAt('/work-orders?tab=templates');
    await screen.findByText('Bracket brake set');
    await userEvent.click(screen.getByRole('button', { name: 'Use template Bracket brake set' }));
    await screen.findByText('Use template — Bracket brake set');
  }

  it('navigates to the new draft when ONE was created', async () => {
    mockedApi.useWorkOrderTemplate.mockResolvedValue({
      work_order: draft,
      created_count: 1,
      work_orders: [draft],
      skipped_operations: [],
      skipped_material_allocations: [],
    });
    await openTheUseDialog();

    await userEvent.click(screen.getByRole('button', { name: /Create draft work order/i }));

    await waitFor(() => expect(locationPathname()).toBe('/work-orders/501'));
  });

  it('stays put and REFRESHES the list when several were created', async () => {
    mockedApi.useWorkOrderTemplate.mockResolvedValue({
      work_order: draft,
      created_count: 2,
      work_orders: [draft, second],
      skipped_operations: [],
      skipped_material_allocations: [],
    });
    await openTheUseDialog();
    const listReadsBefore = mockedApi.getWorkOrders.mock.calls.length;

    await userEvent.clear(screen.getByLabelText('Work orders to create'));
    await userEvent.type(screen.getByLabelText('Work orders to create'), '2');
    await userEvent.click(screen.getByRole('button', { name: /Create 2 draft work orders/i }));

    // The dialog holds the number -> Unit # table the planner is about to write on
    // paper, so the hand-off is the "Done" click rather than the submit.
    await screen.findByTestId('use-template-batch-table');
    await userEvent.click(screen.getByRole('button', { name: 'Done' }));

    // Never onto one of the two: the other would be the one nobody saw.
    await waitFor(() => expect(mockedApi.getWorkOrders.mock.calls.length).toBeGreaterThan(listReadsBefore));
    expect(locationPathname()).toBe('/work-orders');
    expect(locationSearch()).toBe('?tab=templates');
  });
});
