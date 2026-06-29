/**
 * Shop-floor full-quantity completion confirm — /shop-floor/operations
 *
 * Batch 1 shop-floor safety: "Complete Operation" on a target-reached op no
 * longer fires the irreversible completeOperation call on the first click. It
 * opens a ConfirmDialog ("Complete operation at full quantity?…") first.
 * Cancel must NOT call the API; only Confirm does.
 *
 * Note: ShopFloorSimple emits pre-existing act() warnings around its async
 * polling effects. Those warnings are noise here — every assertion below is on
 * concrete behavior (dialog visibility, API call counts), so a real regression
 * still fails the test rather than hiding behind a warning.
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
    completeOperation: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// An op that has hit its target (complete >= ordered) and is in_progress, which
// is the only state that surfaces the "Complete Operation" button.
const TARGET_REACHED_OP = {
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
  quantity_complete: 25,
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

// A matching active job so getActiveJobForOperation() resolves (op.id === operation_id).
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
  quantity_complete: 25,
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

describe('ShopFloorSimple full-quantity complete confirm', () => {
  beforeAll(() => {
    window.HTMLElement.prototype.scrollIntoView = jest.fn();
  });

  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, name: 'Laser 1', code: 'LASER1' }]);
    mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [TARGET_REACHED_OP] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
    mockedApi.completeOperation.mockResolvedValue({});
  });

  it('opens a confirm dialog instead of completing immediately on the first click', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    // The target-reached card surfaces the Complete Operation button.
    const completeBtn = await screen.findByRole('button', { name: /complete operation/i });
    fireEvent.click(completeBtn);

    // A confirm dialog appears; the API has NOT been called yet.
    expect(await screen.findByText('Complete operation at full quantity?')).toBeInTheDocument();
    expect(screen.getByText(/closes the operation and cannot be undone/i)).toBeInTheDocument();
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
  });

  it('Cancel dismisses the dialog without calling completeOperation', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    fireEvent.click(await screen.findByRole('button', { name: /complete operation/i }));
    await screen.findByText('Complete operation at full quantity?');

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await waitFor(() =>
      expect(screen.queryByText('Complete operation at full quantity?')).not.toBeInTheDocument()
    );
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
  });

  it('Confirm completes the operation at the full ordered quantity', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    fireEvent.click(await screen.findByRole('button', { name: /complete operation/i }));
    await screen.findByText('Complete operation at full quantity?');

    // The dialog's confirm button carries the "Complete Operation" label; the
    // queue card's button is now behind the modal, so scope to the dialog.
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /complete operation/i }));

    await waitFor(() =>
      expect(mockedApi.completeOperation).toHaveBeenCalledWith(101, { quantity_complete: 25 })
    );
    expect(mockedApi.completeOperation).toHaveBeenCalledTimes(1);
  });
});
