/**
 * Shop-floor completion confirm — /shop-floor/operations
 *
 * Batch 1 shop-floor safety: "Complete Operation" on an accounted-for op does
 * not fire the irreversible completeOperation call on the first click. It opens
 * a ConfirmDialog ("Complete this operation?…") first, which states the counts
 * the close will book. Cancel must NOT call the API; only Confirm does.
 *
 * Owner ruling 1: the close sends NO quantity — the server books the good the
 * floor recorded — and an op whose short is covered by recorded scrap surfaces
 * the same Complete Operation path.
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
import { WorkCenter } from '../types';

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

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: 'operator' } }),
}));

jest.mock('../context/CompanyContext', () => ({
  useCompany: () => ({ currentCompany: { id: 1 } }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// The work center the op runs on. Typed as the real `WorkCenter` so the fixture
// carries every field the endpoint actually serves.
const LASER_1: WorkCenter = {
  id: 1,
  version: 1,
  code: 'LASER1',
  name: 'Laser 1',
  work_center_type: 'laser_cutting',
  hourly_rate: 85,
  capacity_hours_per_day: 16,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

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
    sessionStorage.clear();
    mockedApi.getWorkCenters.mockResolvedValue([LASER_1]);
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
    expect(await screen.findByText('Complete this operation?')).toBeInTheDocument();
    expect(screen.getByText('This closes Op 10 at 25 good and cannot be undone.')).toBeInTheDocument();
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
  });

  it('Cancel dismisses the dialog without calling completeOperation', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    fireEvent.click(await screen.findByRole('button', { name: /complete operation/i }));
    await screen.findByText('Complete this operation?');

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await waitFor(() =>
      expect(screen.queryByText('Complete this operation?')).not.toBeInTheDocument()
    );
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
  });

  it('Confirm closes the operation with NO quantity — the server books what the floor recorded', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    fireEvent.click(await screen.findByRole('button', { name: /complete operation/i }));
    await screen.findByText('Complete this operation?');

    // The dialog's confirm button carries the "Complete Operation" label; the
    // queue card's button is now behind the modal, so scope to the dialog.
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /complete operation/i }));

    await waitFor(() =>
      expect(mockedApi.completeOperation).toHaveBeenCalledWith(101, { source: 'desktop' })
    );
    expect(mockedApi.completeOperation).toHaveBeenCalledTimes(1);
  });

  it('a short covered by recorded scrap is "all pieces accounted for" and closes the same way', async () => {
    const covered = { ...TARGET_REACHED_OP, quantity_complete: 23, quantity_scrapped: 2 };
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [covered] });
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    expect(await screen.findByText('All pieces accounted for')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /complete operation/i }));
    expect(await screen.findByText('This closes Op 10 at 23 good · 2 scrapped and cannot be undone.')).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /complete operation/i }));

    await waitFor(() => expect(mockedApi.completeOperation).toHaveBeenCalledWith(101, { source: 'desktop' }));
  });

  it('a short with nothing covering it offers no Complete — the operation stays open', async () => {
    const short = { ...TARGET_REACHED_OP, quantity_complete: 23, quantity_scrapped: 0 };
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [short] });
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    expect(screen.queryByText('Target quantity reached')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /complete operation/i })).not.toBeInTheDocument();
  });

  it('works from the RECEIVED count: units closed short upstream never count against this operation', async () => {
    // Target 25, 2 never reached this operation (an upstream close declared them short).
    const downstream = { ...TARGET_REACHED_OP, quantity_complete: 23, upstream_quantity_short: 2 };
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [downstream] });
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    expect(await screen.findByText('Target quantity reached')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /complete operation/i })).toBeInTheDocument();
  });

  it('surfaces a close refusal verbatim (e.g. a crewmate still clocked in)', async () => {
    mockedApi.completeOperation.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'Bob T is still clocked in to Op OP10 — have them clock out with their count first, or leave the operation open.' },
      },
    });
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');

    fireEvent.click(await screen.findByRole('button', { name: /complete operation/i }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /complete operation/i }));

    expect(
      await screen.findByText(
        'Bob T is still clocked in to Op OP10 — have them clock out with their count first, or leave the operation open.'
      )
    ).toBeInTheDocument();
  });
});
