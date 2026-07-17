/**
 * Standalone laser nest WO detail — part_id NULL, work_order_type
 * 'laser_cutting', no parent.
 *
 * Guards the two behaviors the standalone-nest feature added to this page:
 *  1. A part-less laser WO renders cleanly (no crash, no part-flavored noise —
 *     the "No BOM defined for this part" nudge is suppressed when there is no
 *     part to hang a BOM on).
 *  2. The Laser Nest Package card now renders on laser_cutting WOs too (the
 *     backend {work_order_id} endpoints operate on a laser WO directly), so
 *     re-import and manual nest-add are available from this page — with the
 *     laser-specific subtitle and the wizard in PARENTED mode (workOrderId
 *     passed → no standalone copy).
 *
 * Heavy side-channels (websocket, secondary fetches) are mocked, mirroring
 * WorkOrderDetail.completeGuard.test.tsx.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { LaserNestPackagePreview } from '../types';

const mockNavigate = jest.fn();

jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

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
    completeWorkOrder: jest.fn(),
    completeWOOperation: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
    previewLaserNestPackage: jest.fn(),
    importLaserNestPackage: jest.fn(),
    previewLaserNestPackageStandalone: jest.fn(),
    importLaserNestPackageStandalone: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: true },
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

/** A standalone nest WO exactly as the standalone import creates it:
 *  released, laser_cutting, NO part, NO parent, quantity = total sheet runs. */
const standaloneNestWorkOrder = {
  id: 42,
  version: 1,
  work_order_number: 'WO-0042',
  part_id: null,
  parent_work_order_id: undefined,
  work_order_type: 'laser_cutting',
  quantity_ordered: 12,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'released',
  priority: 3,
  estimated_hours: 0,
  actual_hours: 0,
  created_at: '2026-07-01T00:00:00Z',
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

describe('WorkOrderDetail — standalone (part-less) laser nest WO', () => {
  beforeEach(() => {
    jest.clearAllMocks();

    mockedApi.getWorkOrder.mockResolvedValue({ ...standaloneNestWorkOrder });
    mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
    // Part-less WOs have no BOM; backend answers has_bom: false.
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
  });

  it('renders the WO without a part and suppresses the "No BOM" nudge', async () => {
    renderDetail();

    // The header renders off the WO itself — a NULL part must not crash it.
    expect(await screen.findByRole('heading', { name: 'WO-0042' })).toBeInTheDocument();

    // No part → no "No BOM defined for this part" empty-state.
    expect(screen.queryByText(/no bom defined for this part/i)).not.toBeInTheDocument();
    // And no literal "undefined"/"null" leaks into the DOM.
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
  });

  it('shows the Laser Nest Package card with manage actions on a laser_cutting WO', async () => {
    renderDetail();
    await screen.findByRole('heading', { name: 'WO-0042' });

    // The card renders for laser WOs now (re-import / manual-add surface),
    // with the laser-specific subtitle.
    expect(screen.getByText('Laser Nest Package')).toBeInTheDocument();
    expect(screen.getByText(/add nests to this laser work order/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /import nest package/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add nest manually/i })).toBeInTheDocument();
  });

  it('opens the import wizard in PARENTED mode (targets this WO, no standalone copy)', async () => {
    renderDetail();
    await screen.findByRole('heading', { name: 'WO-0042' });

    fireEvent.click(screen.getByRole('button', { name: /import nest package/i }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/import laser nest package/i)).toBeInTheDocument();
    // workOrderId was passed → the standalone "creates a new work order" copy
    // must NOT show; the import will land on THIS work order.
    expect(within(dialog).queryByText(/creates a new released laser cutting work order/i)).not.toBeInTheDocument();
  });

  it('refreshes in place (no navigate) when the import lands on THIS work order', async () => {
    // Backend generalization: a laser WO addressed by id is rebuilt directly and
    // the response returns ITS OWN id. Navigating to the same route would not
    // remount — the page must reload the WO in place instead.
    const preview: LaserNestPackagePreview = {
      package_name: 'nests.zip',
      nest_count: 1,
      total_planned_runs: 5,
      nests: [
        {
          source_file: 'sheet-1.pdf',
          nest_name: 'Sheet 1',
          cnc_number: '8001',
          cnc_file_name: null,
          planned_runs: 5,
          material: '304 SS',
          thickness: '0.125"',
          sheet_size: '48x96',
          confidence: 'high',
        },
      ],
    };
    mockedApi.previewLaserNestPackage.mockResolvedValue(preview);
    mockedApi.importLaserNestPackage.mockResolvedValue({
      package: preview,
      child_work_order: { id: 42, work_order_number: 'WO-0042' },
    });

    renderDetail();
    await screen.findByRole('heading', { name: 'WO-0042' });
    const loadsBeforeImport = mockedApi.getWorkOrder.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: /import nest package/i }));
    const dialog = await screen.findByRole('dialog');

    const zip = new File(['PK'], 'nests.zip', { type: 'application/zip' });
    fireEvent.change(within(dialog).getByLabelText(/zip package/i), { target: { files: [zip] } });
    fireEvent.click(within(dialog).getByRole('button', { name: /^preview$/i }));
    await waitFor(() => expect(mockedApi.previewLaserNestPackage).toHaveBeenCalledTimes(1));
    expect(mockedApi.previewLaserNestPackage.mock.calls[0][0]).toBe(42); // parented endpoint, this WO

    fireEvent.click(await within(dialog).findByRole('button', { name: /^import 1 nest$/i }));
    await waitFor(() => expect(mockedApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    expect(mockedApi.importLaserNestPackage.mock.calls[0][0]).toBe(42);

    // Returned id === current WO → wizard closes, WO reloads in place, NO navigation.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(loadsBeforeImport));
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
