/**
 * WorkOrderDetail — a save through the LaserNestManualModal refreshes
 * MaterialTiesPanel.
 *
 * The nest modal's edit path can create/update/cancel the tie on the nest's
 * operation, and a tie write does NOT bump `work_orders.updated_at` — the
 * panel's only other load dependency. So `handleNestSaved` must bump
 * `tieRefreshToken` (the same seam OperationMaterialTieModal uses); refetching
 * the work order alone leaves a stale tie list sitting directly beneath the
 * nest card. The regression this pins: nest-modal saves refreshed the WO but
 * never the panel.
 *
 * Harness mirrors WorkOrderDetail.standaloneNest.test.tsx (side-channels
 * mocked); driven through the CREATE path because it is the shortest route
 * through `onSaved` → `handleNestSaved`, which is the seam under test.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getOperationDetails: jest.fn(),
    getMaterialRequirements: jest.fn(),
    getWorkOrderBlockers: jest.fn(),
    getActiveUsers: jest.fn(),
    getUsers: jest.fn(),
    getDocuments: jest.fn(),
    getMaterialAllocations: jest.fn(),
    completeWorkOrder: jest.fn(),
    completeWOOperation: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
    // Mounted dialogs/panels (tie editor, backflush preview) — none fetch on
    // the closed path, but the mock has to carry them.
    getMaterials: jest.fn(),
    createMaterialAllocation: jest.fn(),
    updateMaterialAllocation: jest.fn(),
    getWorkOrderBackflushPreview: jest.fn(),
    // The nest modal itself.
    createManualLaserNest: jest.fn(),
    updateLaserNest: jest.fn(),
    uploadDocument: jest.fn(),
    attachLaserNestDocument: jest.fn(),
    extractLaserNestFromPdf: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const laserWorkOrder = {
  id: 42,
  version: 1,
  work_order_number: 'WO-0042',
  part_id: null,
  work_order_type: 'laser_cutting',
  quantity_ordered: 12,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'released',
  priority: 3,
  estimated_hours: 0,
  actual_hours: 0,
  created_at: '2026-07-01T00:00:00Z',
  // Deliberately NEVER changes across refetches: the panel must re-read on the
  // token bump alone, exactly the case a tie write puts the page in.
  updated_at: '2026-07-01T00:00:00Z',
  operations: [],
};

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/work-orders/42']}>
      <Routes>
        <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('WorkOrderDetail — nest modal save refreshes MaterialTiesPanel', () => {
  beforeEach(() => {
    jest.clearAllMocks();

    mockedApi.getWorkOrder.mockResolvedValue({ ...laserWorkOrder });
    mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
    mockedApi.getMaterialRequirements.mockResolvedValue({
      work_order_id: 42,
      work_order_number: 'WO-0042',
      quantity_ordered: 12,
      has_bom: false,
      materials: [],
    });
    mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
    mockedApi.getActiveUsers.mockResolvedValue([]);
    mockedApi.getUsers.mockResolvedValue([]);
    mockedApi.getDocuments.mockResolvedValue([]);
    mockedApi.getMaterialAllocations.mockResolvedValue([]);
    mockedApi.getMaterials.mockResolvedValue([]);
    mockedApi.createManualLaserNest.mockResolvedValue({
      id: 7,
      nest_name: '8001',
      cnc_number: '8001',
      planned_runs: 5,
      completed_runs: 0,
      remaining_runs: 5,
    });
  });

  it('re-reads the material ties after a save through the nest modal', async () => {
    renderDetail();
    await screen.findByRole('heading', { name: 'WO-0042' });

    // MaterialTiesPanel's mount read has landed; everything after this is the
    // save's doing.
    await waitFor(() => expect(mockedApi.getMaterialAllocations).toHaveBeenCalled());
    const tieReadsBefore = mockedApi.getMaterialAllocations.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: /add nest manually/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/cnc number/i), { target: { value: '8001' } });
    fireEvent.change(within(dialog).getByLabelText(/qty to cut/i), { target: { value: '5' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /^add nest$/i }));

    await waitFor(() => expect(mockedApi.createManualLaserNest).toHaveBeenCalledTimes(1));
    // The WO refetch alone cannot refresh the panel (updated_at is unchanged
    // above) — only the tieRefreshToken bump in handleNestSaved can drive this.
    await waitFor(() =>
      expect(mockedApi.getMaterialAllocations.mock.calls.length).toBeGreaterThan(tieReadsBefore)
    );
  });
});
