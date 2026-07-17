/**
 * ShopFloorSimple — over-count correction (reduce-production).
 *
 * The "Add Completed Quantity" modal gains a mode toggle: the additive "Add
 * completed" path and a self-service "Correct over-count" path that walks back
 * good quantity the operator over-reported on their own open clock-in. The
 * correction is server-gated ⇒ NON-optimistic:
 *  - a REQUIRED free-text reason (audit trail) gates the submit button;
 *  - api.reduceOperationProduction carries { quantity_delta, reason, source };
 *  - the on-screen count is NOT touched locally — a refetch reflects the server;
 *  - a server refusal surfaces `detail` verbatim and leaves the modal open.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
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
    reportOperationProduction: jest.fn(),
    reduceOperationProduction: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const IN_PROGRESS_OP = {
  id: 101,
  work_order_id: 42,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_number: 'OP10',
  operation_name: 'Laser Cut',
  description: null,
  work_center_id: 1,
  work_center_name: 'Laser 1',
  status: 'in_progress',
  quantity_ordered: 25,
  quantity_complete: 5,
  quantity_scrapped: 0,
  priority: 3,
  due_date: null,
  customer_name: null,
  customer_po: null,
  actual_start: null,
  setup_instructions: null,
  run_instructions: null,
  requires_inspection: false,
};

const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 42,
  operation_id: 101,
  work_center_id: 1,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_name: 'Laser Cut',
  operation_number: 'OP10',
  quantity_ordered: 25,
  quantity_complete: 5,
};

function renderShopFloor() {
  return render(
    <MemoryRouter initialEntries={['/shop-floor/operations']}>
      <Routes>
        <Route path="/shop-floor/operations" element={<ShopFloorSimple />} />
      </Routes>
    </MemoryRouter>
  );
}

async function openCorrectMode() {
  renderShopFloor();
  await screen.findByTestId('shop-floor-op-101');
  const row = screen.getByTestId('shop-floor-op-101');
  fireEvent.click(within(row).getByRole('button', { name: /^more$/i }));
  await screen.findByRole('heading', { name: /add completed quantity/i });
  fireEvent.click(screen.getByRole('button', { name: /correct over-count/i }));
  return screen.getByRole('heading', { name: /correct over-count/i });
}

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, name: 'Laser 1', code: 'LASER1' }]);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [IN_PROGRESS_OP] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  mockedApi.reportOperationProduction.mockResolvedValue({});
  mockedApi.reduceOperationProduction.mockResolvedValue({});
});

describe('ShopFloorSimple over-count correction', () => {
  it('requires a reason and blocks submit until one is entered', async () => {
    await openCorrectMode();

    // The remove quantity defaults to 1, but the reason is blank → submit blocked.
    const submit = screen.getByRole('button', { name: /remove from completed/i });
    expect(submit).toBeDisabled();
    expect(mockedApi.reduceOperationProduction).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/reason for correction/i), {
      target: { value: 'double-scanned the tray' },
    });
    expect(screen.getByRole('button', { name: /remove from completed/i })).toBeEnabled();
  });

  it('sends { quantity_delta, reason, source } to reduceOperationProduction', async () => {
    await openCorrectMode();

    fireEvent.change(screen.getByLabelText(/parts to remove/i), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText(/reason for correction/i), {
      target: { value: 'double-scanned the tray' },
    });
    fireEvent.click(screen.getByRole('button', { name: /remove from completed/i }));

    await waitFor(() => expect(mockedApi.reduceOperationProduction).toHaveBeenCalledTimes(1));
    expect(mockedApi.reduceOperationProduction).toHaveBeenCalledWith(101, {
      quantity_delta: 3,
      reason: 'double-scanned the tray',
      notes: undefined,
      source: 'desktop',
    });
    // Additive report is never called on the correction path.
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();
  });

  it('surfaces the server refusal verbatim and does not optimistically change the count', async () => {
    mockedApi.reduceOperationProduction.mockRejectedValue({
      response: {
        data: { detail: 'You can only remove up to the 2 piece(s) you recorded on this clock-in; ask a supervisor to correct more.' },
      },
    });
    await openCorrectMode();

    fireEvent.change(screen.getByLabelText(/parts to remove/i), { target: { value: '9' } });
    fireEvent.change(screen.getByLabelText(/reason for correction/i), { target: { value: 'oops' } });
    fireEvent.click(screen.getByRole('button', { name: /remove from completed/i }));

    // Verbatim server detail is shown; the modal stays open with the count
    // unchanged ("Completed now: 5 / 25") because the UI never moved it optimistically.
    expect(
      await screen.findByText(/you can only remove up to the 2 piece\(s\)/i)
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /correct over-count/i })).toBeInTheDocument();
    expect(screen.getByText(/completed now: 5 \/ 25/i)).toBeInTheDocument();
  });
});
