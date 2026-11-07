/**
 * OperatorKiosk — a job the WORK ORDER refuses (not released / on hold).
 *
 * Owner ruling 2026-11-05: the floor never starts work a supervisor has not
 * released, or has put on hold. Unlike an OPERATION hold (which the server moves
 * onto the separate `held` list), a READY operation of a draft or on-hold work
 * order stays on `queue` carrying `start_blocked_reason` — so the kiosk has to
 * handle it on the card, on the confirm screen and in the chained next-job start:
 *
 *  - the queue card carries a chip and stays tappable (the confirm explains why);
 *  - the confirm screen shows the server's reason (role="status") and a disabled
 *    Clock in labelled "WO on hold" / "Not released";
 *  - a 409 from a stale queue is toasted VERBATIM, then the kiosk lands on the
 *    queue and re-reads it, rather than leaving the operator on a confirm whose
 *    Clock in can never succeed;
 *  - the chained clock-in after a complete skips a blocked head and aims at the
 *    next startable job (or at nothing).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';

jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: null }) }));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenters: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    getOperationSteps: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    completeOperation: jest.fn(),
    reportOperationProduction: jest.fn(),
    reduceOperationProduction: jest.fn(),
    holdOperation: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedUseAuth = useAuth as jest.Mock;

// Verbatim from backend app/services/operation_action_gates.py.
const WO_ON_HOLD =
  "This work order is on hold — work cannot start until a supervisor resumes it (Resume Work Order on the work order). Resuming or clearing an operation's hold does not lift it.";
const NOT_RELEASED =
  'This work order has not been released — release first. Work cannot start until a supervisor releases it.';

const READY_ITEM = {
  operation_id: 32,
  work_order_id: 10,
  work_order_number: 'WO-2026-0143',
  part_number: 'PN-9001',
  part_name: 'Plate, cover',
  operation_number: '10',
  operation_name: 'Laser',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 20,
  quantity_complete: 0,
  priority: 5,
  due_date: null,
  steps_total: 0,
  steps_recorded: 0,
  work_order_status: 'released',
  start_blocked_reason: null,
};

const HELD_WO_ITEM = {
  ...READY_ITEM,
  operation_id: 33,
  work_order_id: 11,
  work_order_number: 'WO-2026-0150',
  work_order_status: 'on_hold',
  start_blocked_reason: WO_ON_HOLD,
};

const DRAFT_ITEM = {
  ...READY_ITEM,
  operation_id: 34,
  work_order_id: 12,
  work_order_number: 'WO-2026-0151',
  work_order_status: 'draft',
  start_blocked_reason: NOT_RELEASED,
};

const ACTIVE_ITEM = {
  ...READY_ITEM,
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  operation_number: '20',
  operation_name: 'Deburr',
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 5,
};

const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 9,
  operation_id: 31,
  work_center_id: 7,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-9001',
  part_name: 'Plate, cover',
  operation_name: 'Deburr',
  operation_number: '20',
  quantity_ordered: 50,
  quantity_complete: 5,
};

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=LASER1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedUseAuth.mockReturnValue({
    user: { id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' },
    isAuthenticated: true,
    isLoading: false,
    loginWithEmployeeId: jest.fn(),
    logout: jest.fn(),
  });
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
});

describe('OperatorKiosk — the queue card and the confirm screen', () => {
  it('chips a held job on the queue and keeps the card tappable', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [HELD_WO_ITEM] });
    renderKiosk();

    const card = await screen.findByRole('button', { name: /WO-2026-0150/ });
    expect(within(card).getByTestId('kiosk-start-blocked-chip')).toHaveTextContent('WO on hold');
    expect(card).not.toHaveAttribute('aria-disabled');
  });

  it('shows the reason on the confirm screen and disables Clock in', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [HELD_WO_ITEM] });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0150/ }));

    const confirm = await screen.findByRole('region', { name: 'Confirm clock in' });
    const panel = within(confirm).getByRole('status');
    expect(panel).toHaveTextContent(WO_ON_HOLD);
    const clockIn = within(confirm).getByRole('button', { name: 'WO on hold' });
    expect(clockIn).toBeDisabled();
    expect(within(confirm).queryByRole('button', { name: 'Clock in' })).not.toBeInTheDocument();

    fireEvent.click(clockIn);
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('labels an unreleased job "Not released"', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [DRAFT_ITEM] });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0151/ }));

    const confirm = await screen.findByRole('region', { name: 'Confirm clock in' });
    expect(within(confirm).getByRole('status')).toHaveTextContent(NOT_RELEASED);
    expect(within(confirm).getByRole('button', { name: 'Not released' })).toBeDisabled();
  });

  it('leaves a released job exactly as it was: no panel, an enabled Clock in', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [READY_ITEM] });
    renderKiosk();

    const card = await screen.findByRole('button', { name: /WO-2026-0143/ });
    expect(within(card).queryByTestId('kiosk-start-blocked-chip')).not.toBeInTheDocument();
    fireEvent.click(card);

    const confirm = await screen.findByRole('region', { name: 'Confirm clock in' });
    expect(within(confirm).queryByTestId('kiosk-confirm-start-blocked')).not.toBeInTheDocument();
    expect(within(confirm).getByRole('button', { name: 'Clock in' })).toBeEnabled();
  });
});

describe('OperatorKiosk — a 409 from a stale queue', () => {
  it('toasts the refusal verbatim, lands on the queue and re-reads it', async () => {
    // The row read as startable, but a supervisor held the job since.
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [READY_ITEM] });
    mockedApi.clockIn.mockRejectedValue({ response: { status: 409, data: { detail: WO_ON_HOLD } } });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0143/ }));
    const confirm = await screen.findByRole('region', { name: 'Confirm clock in' });
    const readsBefore = mockedApi.getWorkCenterQueue.mock.calls.length;
    fireEvent.click(within(confirm).getByRole('button', { name: 'Clock in' }));

    expect(await screen.findByText(WO_ON_HOLD)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Confirm clock in' })).not.toBeInTheDocument());
    expect(screen.getByRole('region', { name: /work queue/i })).toBeInTheDocument();
    await waitFor(() => expect(mockedApi.getWorkCenterQueue.mock.calls.length).toBeGreaterThan(readsBefore));
  });

  it('keeps the operator on the confirm screen for a non-409 refusal (unchanged)', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [READY_ITEM] });
    mockedApi.clockIn.mockRejectedValue({
      response: { status: 400, data: { detail: 'Previous operations must be completed first' } },
    });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0143/ }));
    const confirm = await screen.findByRole('region', { name: 'Confirm clock in' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Clock in' }));

    expect(await screen.findByText('Previous operations must be completed first')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Confirm clock in' })).toBeInTheDocument();
  });
});

describe('OperatorKiosk — the chained next-job start skips a blocked head', () => {
  beforeEach(() => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.clockOut.mockResolvedValue({});
    mockedApi.completeOperation.mockResolvedValue({});
    mockedApi.clockIn.mockResolvedValue({ id: 601 });
  });

  it('aims the chain at the next STARTABLE job, past a held one', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [ACTIVE_ITEM, HELD_WO_ITEM, READY_ITEM] });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    const cta = screen.getByTestId('kiosk-qty-confirm');
    expect(cta).toHaveTextContent(/start WO-2026-0143/i);
    expect(cta).not.toHaveTextContent(/WO-2026-0150/);
    fireEvent.click(cta);

    await waitFor(() => expect(mockedApi.clockIn).toHaveBeenCalledTimes(1));
    expect(mockedApi.clockIn).toHaveBeenCalledWith(expect.objectContaining({ operation_id: 32, work_order_id: 10 }));
  });

  it('chains to nothing when every other queued job is blocked', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [ACTIVE_ITEM, HELD_WO_ITEM, DRAFT_ITEM] });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    const cta = screen.getByTestId('kiosk-qty-confirm');
    expect(cta).not.toHaveTextContent(/start WO-/i);
    fireEvent.click(cta);

    await waitFor(() => expect(mockedApi.completeOperation).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Completed WO-2026-0142')).toBeInTheDocument();
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });
});
