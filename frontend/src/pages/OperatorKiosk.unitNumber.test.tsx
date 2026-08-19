/**
 * OperatorKiosk — the UNIT badge on the two full-screen surfaces an operator acts on.
 *
 * These are the highest-consequence renders of `unit_number` in the app, and both take
 * the `lg` badge because they are read at arm's length from a tablet on a machine:
 *
 *  - the **CLOCK IN?** confirm screen, which is the last thing between a badge scan and
 *    labour being booked against a work order. If two work orders in the queue build
 *    different units of the same part, this screen is where that distinction has to be
 *    visible — the WO number alone is what the operator was already failing to
 *    disambiguate.
 *  - the **running-job hero**, the screen an operator stares at for hours. It is fed by
 *    `GET /shop-floor/my-active-job`, a payload hand-built SEPARATELY from the queue
 *    rows on the server (only `_job_guidance_fields` is shared), so a server-side
 *    regression could leave this one screen unable to name the unit on the bench while
 *    every other surface still showed it.
 *
 * Both are asserted with a job that carries no unit as well, because most jobs do not,
 * and an empty chip on the kiosk hero would be a whole-shop visual regression.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenters: jest.fn(),
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

const UNIT = '2410048';

const QUEUE_ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Weldment, frame',
  operation_number: '20',
  operation_name: 'Weld out',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 1,
  quantity_complete: 0,
  priority: 5,
  due_date: null,
};

const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 9,
  operation_id: 31,
  work_center_id: 7,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Weldment, frame',
  operation_name: 'Weld out',
  operation_number: '20',
  quantity_ordered: 1,
  quantity_complete: 0,
};

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=WELD1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

function authAs(user: object | null) {
  mockedUseAuth.mockReturnValue({
    user,
    isAuthenticated: !!user,
    isLoading: false,
    loginWithEmployeeId: jest.fn(),
    logout: jest.fn(),
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  authAs({ id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' });
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM] });
});

describe('OperatorKiosk unit number', () => {
  it('names the unit on the CLOCK IN? confirm screen', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [{ ...QUEUE_ITEM, unit_number: UNIT }] });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));

    // The confirm screen is up…
    expect(screen.getByRole('button', { name: /^clock in$/i })).toBeInTheDocument();
    // …and it says which unit this badge scan is about to book labour against.
    const badges = screen.getAllByTestId('unit-badge');
    expect(badges.some((b) => b.textContent?.includes(UNIT))).toBe(true);
  });

  it('shows no badge on the confirm screen for a job that tracks no unit', async () => {
    renderKiosk(); // QUEUE_ITEM carries no unit_number

    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));

    expect(screen.getByRole('button', { name: /^clock in$/i })).toBeInTheDocument();
    expect(screen.queryByTestId('unit-badge')).not.toBeInTheDocument();
  });

  it('names the unit on the running-job hero', async () => {
    // Fed by my-active-job, a payload built separately from the queue rows on the
    // server — so this assertion cannot ride on the confirm-screen one above.
    mockedApi.getMyActiveJob.mockResolvedValue({
      active_jobs: [{ ...ACTIVE_JOB, unit_number: UNIT }],
      active_job: { ...ACTIVE_JOB, unit_number: UNIT },
    });
    renderKiosk();

    await waitFor(() => expect(screen.getAllByTestId('unit-badge').length).toBeGreaterThan(0));
    expect(screen.getAllByTestId('unit-badge').some((b) => b.textContent?.includes(UNIT))).toBe(true);
  });

  it('shows no badge on the running-job hero when the active job tracks no unit', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    renderKiosk();

    // Wait for the running panel to render (so the absence is a decision, not a
    // not-yet-loaded screen), then assert nothing was drawn.
    expect(await screen.findByRole('button', { name: /report production/i })).toBeInTheDocument();
    expect(screen.queryByTestId('unit-badge')).not.toBeInTheDocument();
  });
});
