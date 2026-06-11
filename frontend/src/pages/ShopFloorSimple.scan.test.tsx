/**
 * A0.4 traveler scan UX — /shop-floor/operations
 *
 * Locks the resolve-first scan flow: the scan box and the ?scan= URL param
 * (what phone-scanned traveler op QRs open) both go through
 * /scanner/resolve-action first. An operation hit focuses the scanned op
 * (search + row highlight + details modal); employee/unknown results fall
 * back to the legacy /scanner/lookup path so supplier-part and part-number
 * scans keep working. The ?scan= param is handled exactly once and stripped
 * from the URL via history replace.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
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
    resolveScanAction: jest.fn(),
    scannerLookup: jest.fn(),
    getOperationDetails: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const OPERATION = {
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
  status: 'ready',
  quantity_ordered: 25,
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
};

const OPERATION_SCAN_RESULT = {
  kind: 'operation' as const,
  code: 'OP:101',
  operation: {
    id: 101,
    sequence: 10,
    operation_number: 'OP10',
    name: 'Laser Cut',
    status: 'ready',
    work_order_id: 42,
    work_order_number: 'WO-2026-0042',
    work_order_status: 'released',
    part_number: 'PN-0099',
    part_name: 'Mount Plate',
    work_center_id: 1,
    work_center_name: 'Laser 1',
    work_center_match: null,
    quantity_complete: 0,
    target_quantity: 25,
  },
  legal_actions: ['clock_in' as const],
  blockers: {},
  warning: null,
  routing_revision_check: null,
};

const OPERATION_DETAILS = {
  work_order: {
    id: 42,
    work_order_number: 'WO-2026-0042',
    part: { part_number: 'PN-0099', name: 'Mount Plate' },
  },
  operation: {
    id: 101,
    operation_number: 'OP10',
    name: 'Laser Cut',
    status: 'ready',
    quantity_complete: 0,
    quantity_ordered: 25,
    setup_instructions: null,
    run_instructions: null,
  },
  work_center: { name: 'Laser 1' },
  all_operations: [],
  history: [],
};

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

function renderShopFloor(initialEntry = '/shop-floor/operations') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/shop-floor/operations"
          element={
            <>
              <ShopFloorSimple />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>
  );
}

async function submitScan(code: string) {
  fireEvent.click(screen.getByRole('button', { name: /scan traveler/i }));
  fireEvent.change(screen.getByPlaceholderText('Scan or enter traveler code'), {
    target: { value: code },
  });
  fireEvent.click(screen.getByRole('button', { name: /^find$/i }));
}

describe('ShopFloorSimple scan resolution', () => {
  beforeAll(() => {
    window.HTMLElement.prototype.scrollIntoView = jest.fn();
  });

  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, name: 'Laser 1', code: 'LASER1' }]);
    mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [OPERATION] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
    mockedApi.getOperationDetails.mockResolvedValue(OPERATION_DETAILS);
  });

  it('resolves a scan-box operation hit: search, row highlight, and details modal', async () => {
    mockedApi.resolveScanAction.mockResolvedValue(OPERATION_SCAN_RESULT);
    renderShopFloor();

    await screen.findByTestId('shop-floor-op-101');
    await submitScan('OP:101');

    await waitFor(() => expect(mockedApi.resolveScanAction).toHaveBeenCalledWith('OP:101', undefined));
    // The operation is focused: search jumps to its WO, its row is
    // spotlighted, and the details modal opens with the action context.
    await waitFor(() =>
      expect(screen.getByPlaceholderText('Search WO# or Part#...')).toHaveValue('WO-2026-0042')
    );
    expect(screen.getByTestId('shop-floor-op-101').className).toContain('ring-werco-500/60');
    await waitFor(() => expect(mockedApi.getOperationDetails).toHaveBeenCalledWith(101));
    expect(await screen.findByText('Operation Details')).toBeInTheDocument();
    // Toast carries the op name plus the legal actions from the resolver.
    expect(screen.getByText(/Found Laser Cut on WO-2026-0042 — clock in available/)).toBeInTheDocument();
    // Typed resolver hit — the legacy lookup must NOT run.
    expect(mockedApi.scannerLookup).not.toHaveBeenCalled();
  });

  it('resolves a work-order hit: search jumps to the WO without opening a details modal', async () => {
    mockedApi.resolveScanAction.mockResolvedValue({
      kind: 'work_order',
      code: 'WO:WO-2026-0042',
      work_order: {
        id: 42,
        work_order_number: 'WO-2026-0042',
        status: 'released',
        quantity_ordered: 25,
        quantity_complete: 0,
        part_number: 'PN-0099',
        part_name: 'Mount Plate',
        current_operation_id: 101,
      },
      operations: [],
    });
    renderShopFloor();

    await screen.findByTestId('shop-floor-op-101');
    await submitScan('WO:WO-2026-0042');

    await waitFor(() =>
      expect(screen.getByPlaceholderText('Search WO# or Part#...')).toHaveValue('WO-2026-0042')
    );
    expect(mockedApi.getOperationDetails).not.toHaveBeenCalled();
    expect(mockedApi.scannerLookup).not.toHaveBeenCalled();
  });

  it('falls back to the legacy lookup on a structured unknown (supplier-part scans keep working)', async () => {
    mockedApi.resolveScanAction.mockResolvedValue({
      kind: 'unknown',
      code: 'SUP-XYZ-1',
      reason: 'No matching operation, work order, or employee',
    });
    mockedApi.scannerLookup.mockResolvedValue({ part: { part_number: 'PN-0555' } });
    renderShopFloor();

    await screen.findByTestId('shop-floor-op-101');
    await submitScan('SUP-XYZ-1');

    await waitFor(() => expect(mockedApi.scannerLookup).toHaveBeenCalledWith('SUP-XYZ-1'));
    await waitFor(() =>
      expect(screen.getByPlaceholderText('Search WO# or Part#...')).toHaveValue('PN-0555')
    );
    expect(mockedApi.getOperationDetails).not.toHaveBeenCalled();
  });

  it('falls back to the legacy lookup when the resolver itself errors', async () => {
    mockedApi.resolveScanAction.mockRejectedValue(new Error('resolver down'));
    mockedApi.scannerLookup.mockResolvedValue({ work_order: { work_order_number: 'WO-2026-0042' } });
    renderShopFloor();

    await screen.findByTestId('shop-floor-op-101');
    await submitScan('OP:101');

    await waitFor(() => expect(mockedApi.scannerLookup).toHaveBeenCalledWith('OP:101'));
    await waitFor(() =>
      expect(screen.getByPlaceholderText('Search WO# or Part#...')).toHaveValue('WO-2026-0042')
    );
  });

  it('handles a phone-scanned ?scan= URL param once and strips it via history replace', async () => {
    mockedApi.resolveScanAction.mockResolvedValue(OPERATION_SCAN_RESULT);
    renderShopFloor(`/shop-floor/operations?kiosk=1&scan=${encodeURIComponent('OP:101')}`);

    await waitFor(() => expect(mockedApi.resolveScanAction).toHaveBeenCalledWith('OP:101', undefined));
    // Same focus behavior as a scan-box hit.
    await waitFor(() =>
      expect(screen.getByPlaceholderText('Search WO# or Part#...')).toHaveValue('WO-2026-0042')
    );
    expect(await screen.findByText('Operation Details')).toBeInTheDocument();

    // The scan param is gone (other params survive) so a reload won't re-scan...
    await waitFor(() =>
      expect(screen.getByTestId('location-search').textContent).not.toContain('scan=')
    );
    expect(screen.getByTestId('location-search').textContent).toContain('kiosk=1');
    // ...and the resolve flow ran exactly once despite re-renders.
    expect(mockedApi.resolveScanAction).toHaveBeenCalledTimes(1);
  });
});
