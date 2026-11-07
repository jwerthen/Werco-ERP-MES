/**
 * ShopFloorSimple — a job the WORK ORDER refuses is listed, labelled, and never started.
 *
 * Owner ruling 2026-11-05: the floor cannot start work on a job a supervisor has
 * not released (DRAFT) or has put on hold (ON_HOLD). The server refuses every
 * start 409 and says why on the read payload as `start_blocked_reason` (plus the
 * work order's `work_order_status`, which only picks the button label).
 *
 * The server also sets `can_check_in: false` on those rows, which is exactly the
 * trap pinned here: read that flag first and the card says "Waiting for earlier
 * operations" — blaming the routing for what is really a supervisor's release or
 * hold. So the reason is checked FIRST, everywhere:
 *  - the grid card renders a disabled "Not released" / "WO on hold" button whose
 *    title is the server's text, plus the text itself as a reason line;
 *  - the mobile "Next Job" strip skips a blocked row (its button would be a start)
 *    but keeps one the operator is checked in to (its button is Check Out);
 *  - the queue-fallback path (kiosk queue rows carry no `can_check_in`) maps the
 *    reason too, so a blocked job cannot render an ENABLED Check In there;
 *  - a 409 from a stale payload is shown verbatim;
 *  - neither start write is ever sent for a blocked row.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    startOperation: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    resumeOperation: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: 'operator' } }),
}));

jest.mock('../context/CompanyContext', () => ({
  useCompany: () => ({ currentCompany: { id: 1 } }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// Verbatim from backend app/services/operation_action_gates.py.
const NOT_RELEASED =
  'This work order has not been released — release first. Work cannot start until a supervisor releases it.';
const WO_ON_HOLD =
  "This work order is on hold — work cannot start until a supervisor resumes it (Resume Work Order on the work order). Resuming or clearing an operation's hold does not lift it.";

const baseOp = {
  id: 1,
  work_order_id: 101,
  work_order_number: 'WO-BASE',
  part_number: 'PN-1',
  part_name: 'Bracket',
  operation_number: '10',
  operation_name: 'Laser Cut',
  description: null,
  work_center_id: 1,
  work_center_name: 'Laser 1',
  status: 'ready',
  quantity_ordered: 10,
  quantity_complete: 0,
  quantity_scrapped: 0,
  priority: 3,
  due_date: null,
  customer_name: null,
  customer_po: null,
  actual_start: null,
  setup_instructions: null,
  run_instructions: null,
  requires_inspection: false,
  can_check_in: true,
  blocked_by_previous_operations: false,
  start_blocked_reason: null as string | null,
  work_order_status: 'released',
};

/** What the server sends for a READY operation of a DRAFT work order. */
const draftOp = {
  ...baseOp,
  id: 11,
  work_order_id: 111,
  work_order_number: 'WO-DRAFT',
  can_check_in: false,
  start_blocked_reason: NOT_RELEASED,
  work_order_status: 'draft',
};

/** …and of an ON_HOLD work order. */
const heldWoOp = {
  ...baseOp,
  id: 12,
  work_order_id: 112,
  work_order_number: 'WO-HELD',
  can_check_in: false,
  start_blocked_reason: WO_ON_HOLD,
  work_order_status: 'on_hold',
};

const startableOp = { ...baseOp, id: 13, work_order_id: 113, work_order_number: 'WO-GO' };

function renderPage(path = '/shop-floor/operations') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ShopFloorSimple />
    </MemoryRouter>
  );
}

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.resetAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, code: 'LASER1', name: 'Laser 1' }] as never);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
});

describe('ShopFloorSimple — work-order start refusals on the grid card', () => {
  it('renders a disabled "Not released" button with the server reason, never the predecessor copy', async () => {
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [draftOp] });
    renderPage();

    const card = await screen.findByTestId('shop-floor-op-11');
    const button = within(card).getByRole('button', { name: 'Not released' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', NOT_RELEASED);
    expect(within(card).getByTestId('shop-floor-start-blocked-11')).toHaveTextContent(NOT_RELEASED);
    // `can_check_in` is false on this row too — reading it first would blame the routing.
    expect(within(card).queryByText(/Waiting for earlier operations/)).not.toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: 'Waiting' })).not.toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /check in/i })).not.toBeInTheDocument();
  });

  it('renders "WO on hold" for a held work order', async () => {
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [heldWoOp] });
    renderPage();

    const card = await screen.findByTestId('shop-floor-op-12');
    const button = within(card).getByRole('button', { name: 'WO on hold' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', WO_ON_HOLD);
    expect(within(card).getByTestId('shop-floor-start-blocked-12')).toHaveTextContent(WO_ON_HOLD);
  });

  it('keeps the predecessor copy for a released job that is really waiting on its routing', async () => {
    // The control: the new branch must not swallow the old one.
    mockedApi.getShopFloorOperations.mockResolvedValue({
      operations: [{ ...startableOp, status: 'pending', can_check_in: false, blocked_by_previous_operations: true }],
    });
    renderPage();

    const card = await screen.findByTestId('shop-floor-op-13');
    expect(within(card).getByRole('button', { name: 'Waiting' })).toBeDisabled();
    expect(within(card).getByText(/Waiting for earlier operations/)).toBeInTheDocument();
    expect(within(card).queryByTestId('shop-floor-start-blocked-13')).not.toBeInTheDocument();
  });

  it('never sends a start write for a blocked row, even from the Next Job strip', async () => {
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [draftOp, heldWoOp] });
    renderPage();

    const card = await screen.findByTestId('shop-floor-op-11');
    fireEvent.click(within(card).getByRole('button', { name: 'Not released' }));
    fireEvent.click(within(screen.getByTestId('shop-floor-op-12')).getByRole('button', { name: 'WO on hold' }));

    // Both blocked rows are filtered out of the strip, so nothing offers a start.
    expect(screen.queryByTestId('shop-floor-next-job')).not.toBeInTheDocument();
    expect(mockedApi.startOperation).not.toHaveBeenCalled();
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });
});

describe('ShopFloorSimple — the Next Job strip', () => {
  it('skips a blocked row and recommends the next startable one', async () => {
    // Server order puts the blocked job first: dispatch order is not keyed on
    // work-order status, so the client has to drop it.
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [draftOp, startableOp] });
    renderPage();

    const strip = await screen.findByTestId('shop-floor-next-job');
    expect(within(strip).getByText('WO-GO')).toBeInTheDocument();
    expect(within(strip).queryByText('WO-DRAFT')).not.toBeInTheDocument();
    expect(within(strip).getByRole('button', { name: 'Check In' })).toBeEnabled();
  });

  it('keeps a blocked job the operator is checked in to, with Check Out (labor stays closable)', async () => {
    const runningOnHeldJob = { ...heldWoOp, status: 'in_progress' };
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [runningOnHeldJob, startableOp] });
    mockedApi.getMyActiveJob.mockResolvedValue({
      active_jobs: [
        {
          ...runningOnHeldJob,
          operation_id: runningOnHeldJob.id,
          time_entry_id: 5012,
          clock_in: '2026-11-05T14:00:00Z',
          entry_type: 'run',
        },
      ],
    });
    renderPage();

    const strip = await screen.findByTestId('shop-floor-next-job');
    await waitFor(() => expect(within(strip).getByText('WO-HELD')).toBeInTheDocument());
    expect(within(strip).getByRole('button', { name: 'Check Out' })).toBeEnabled();
  });

  it('keeps an op-level hold under a held work order, with Clear Hold', async () => {
    mockedApi.getShopFloorOperations.mockResolvedValue({
      operations: [{ ...heldWoOp, status: 'on_hold', hold: null }, startableOp],
    });
    renderPage();

    const strip = await screen.findByTestId('shop-floor-next-job');
    expect(within(strip).getByText('WO-HELD')).toBeInTheDocument();
    expect(within(strip).getByRole('button', { name: 'Clear Hold' })).toBeInTheDocument();
  });
});

describe('ShopFloorSimple — the queue-fallback path', () => {
  it('maps the reason from a kiosk queue row, so a blocked job renders disabled', async () => {
    // GET /shop-floor/operations comes back empty for the station, so the page
    // falls back to the work-center queue — whose rows carry no can_check_in.
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({
      queue: [
        {
          operation_id: 21,
          work_order_id: 121,
          work_order_number: 'WO-QDRAFT',
          part_number: 'PN-Q',
          part_name: 'Queue part',
          operation_number: '10',
          operation_name: 'Bend',
          status: 'ready',
          quantity_ordered: 4,
          quantity_complete: 0,
          priority: 3,
          due_date: null,
          setup_time_hours: 0,
          run_time_hours: 1,
          work_order_status: 'draft',
          start_blocked_reason: NOT_RELEASED,
        },
      ],
    });
    renderPage('/shop-floor/operations?work_center_id=1');

    const card = await screen.findByTestId('shop-floor-op-21');
    expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(1);
    const button = within(card).getByRole('button', { name: 'Not released' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', NOT_RELEASED);
    expect(mockedApi.startOperation).not.toHaveBeenCalled();
  });
});

describe('ShopFloorSimple — a stale payload meets the server gate', () => {
  it('shows a 409 string detail verbatim', async () => {
    // The row read as startable, but a supervisor held the job since.
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [startableOp] });
    mockedApi.startOperation.mockRejectedValue({ response: { status: 409, data: { detail: WO_ON_HOLD } } });
    renderPage();

    const card = await screen.findByTestId('shop-floor-op-13');
    fireEvent.click(within(card).getByRole('button', { name: 'Check In' }));

    await waitFor(() => expect(mockedApi.startOperation).toHaveBeenCalledWith(13));
    expect(await screen.findByText(WO_ON_HOLD)).toBeInTheDocument();
  });
});
