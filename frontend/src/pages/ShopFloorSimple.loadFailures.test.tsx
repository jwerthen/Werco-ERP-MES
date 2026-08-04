/**
 * ShopFloorSimple silent-load-failure surfacing.
 *
 * The page polls loadOperations / loadDashboardCounts / loadActiveJobs every
 * 30 seconds. These tests lock the failure contract:
 *  - a failed active-job poll KEEPS the last-known clocked-in job on screen
 *    (never "not clocked in" because a poll blipped — that invited a double
 *    clock-in), shows ONE error toast (ok→failed transition only, no toast
 *    per poll) and an inline ErrorState whose Retry re-runs the fetch;
 *  - a failed operations load swaps the grid for an inline ErrorState with a
 *    working Retry instead of silently showing a stale list;
 *  - an empty operations result renders the shared EmptyState with the
 *    conditional copy and the View All Operations action preserved.
 *
 * Harness mirrors ShopFloorSimple.scan.test.tsx (page-local toasts, no
 * ToastProvider needed).
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

// A full WorkCenter, exactly as `api.getWorkCenters()` resolves it — a
// short-shaped fixture is a mock of a payload the real API never sends.
const LASER_1: WorkCenter = {
  id: 1,
  version: 1,
  code: 'LASER1',
  name: 'Laser 1',
  work_center_type: 'laser_cutting',
  hourly_rate: 125,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const ACTIVE_JOB = {
  time_entry_id: 9001,
  clock_in: '2026-08-01T12:00:00Z',
  entry_type: 'run' as const,
  work_order_id: 42,
  operation_id: 101,
  work_center_id: 1,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_name: 'Laser Cut',
  operation_number: 'OP10',
  work_center_name: 'Laser 1',
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

describe('ShopFloorSimple load-failure surfacing', () => {
  let consoleError: jest.SpyInstance;

  beforeAll(() => {
    window.HTMLElement.prototype.scrollIntoView = jest.fn();
  });

  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    // Every failure path also console.error()s; keep test output clean.
    consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    mockedApi.getWorkCenters.mockResolvedValue([LASER_1]);
    mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [OPERATION] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it('keeps the last-known clocked-in job, toasts once, and retries inline when the poll fails', async () => {
    mockedApi.getMyActiveJob
      .mockResolvedValueOnce({ active_jobs: [ACTIVE_JOB], active_job: null })
      .mockRejectedValue(new Error('network down'));
    renderShopFloor();

    // Initial load succeeded — the operator sees their clocked-in job.
    expect(await screen.findByText(/you are checked into/i)).toBeInTheDocument();
    await screen.findByTestId('shop-floor-op-101');

    // A failing refresh must NOT clear the job — the strip stays up...
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }));
    const inline = await screen.findByTestId('error-state');
    expect(inline).toHaveTextContent("Couldn't refresh your clocked-in job");
    expect(screen.getByText(/you are checked into/i)).toBeInTheDocument();

    // ...with exactly ONE toast (toast + inline title = 2 matches)...
    expect(screen.getAllByText("Couldn't refresh your clocked-in job")).toHaveLength(2);

    // ...and a SECOND consecutive failure adds no new toast (transition guard).
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }));
    await waitFor(() => expect(mockedApi.getMyActiveJob).toHaveBeenCalledTimes(3));
    expect(screen.getAllByText("Couldn't refresh your clocked-in job")).toHaveLength(2);

    // Retry re-runs the fetch; success clears the stale flag, job still shown.
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: null });
    fireEvent.click(within(inline).getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument());
    expect(screen.getByText(/you are checked into/i)).toBeInTheDocument();
  });

  it('re-arms the failure toast after a successful recovery (guard resets on success)', async () => {
    mockedApi.getMyActiveJob
      .mockResolvedValueOnce({ active_jobs: [ACTIVE_JOB], active_job: null }) // initial load
      .mockRejectedValueOnce(new Error('outage 1')) // first failing refresh
      .mockResolvedValueOnce({ active_jobs: [ACTIVE_JOB], active_job: null }) // successful Retry
      .mockRejectedValue(new Error('outage 2')); // second, post-recovery failure
    renderShopFloor();
    await screen.findByText(/you are checked into/i);

    // First failure → one toast (toast + inline ErrorState title = 2 matches).
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }));
    const inline = await screen.findByTestId('error-state');
    expect(screen.getAllByText("Couldn't refresh your clocked-in job")).toHaveLength(2);

    // Successful Retry clears the inline state AND resets the transition guard.
    fireEvent.click(within(inline).getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument());

    // A NEW failure after recovery must toast AGAIN — if the guard did not
    // reset on success, this failure would render only the inline state and
    // the match count could never reach 3 (first toast + new toast + title).
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }));
    await screen.findByTestId('error-state');
    await waitFor(() =>
      expect(screen.getAllByText("Couldn't refresh your clocked-in job")).toHaveLength(3)
    );
  });

  it('surfaces work-center and dashboard-count failures in the same cycle as two independent toasts', async () => {
    // Both loaders fail inside the same mount-time Promise.all — the failures
    // land in the same event-loop turn, which is exactly the case the
    // monotonic toast ids exist for (same-tick Date.now() ids collided on
    // React keys and dismissed each other's toasts).
    mockedApi.getWorkCenters.mockRejectedValue(new Error('wc down'));
    mockedApi.getDashboard.mockRejectedValue(new Error('dash down'));
    renderShopFloor();

    // Per-loader guards are independent: BOTH failures toast.
    expect(await screen.findByText('Failed to load work centers')).toBeInTheDocument();
    expect(await screen.findByText('Failed to refresh work center counts')).toBeInTheDocument();

    // Distinct ids: two same-tick toasts must not collide on React keys
    // (a collision logs "Encountered two children with the same key").
    const duplicateKeyWarnings = consoleError.mock.calls.filter((call) =>
      String(call[0]).includes('same key')
    );
    expect(duplicateKeyWarnings).toHaveLength(0);
  });

  it('replaces the operations grid with an ErrorState whose Retry refetches', async () => {
    mockedApi.getShopFloorOperations.mockRejectedValueOnce(new Error('boom'));
    renderShopFloor();

    const inline = await screen.findByTestId('error-state');
    expect(inline).toHaveTextContent('Could not load operations');
    // No stale grid behind the error.
    expect(screen.queryByTestId('shop-floor-op-101')).not.toBeInTheDocument();

    // Retry refetches (next call resolves) and restores the grid.
    fireEvent.click(within(inline).getByRole('button', { name: /retry/i }));
    await screen.findByTestId('shop-floor-op-101');
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument();
  });

  it('renders the shared EmptyState with the View All Operations action when a station has no work', async () => {
    // Pre-select a station via the persisted work-center choice.
    localStorage.setItem('shop_floor_work_center_id', '1');
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
    renderShopFloor();

    const empty = await screen.findByTestId('empty-state');
    expect(empty).toHaveTextContent('No operations found for Laser 1');
    expect(empty).toHaveTextContent('Try adjusting your filters');

    // The action clears the station filter — copy flips to the unfiltered variant.
    fireEvent.click(within(empty).getByRole('button', { name: /view all operations/i }));
    await waitFor(() =>
      expect(screen.getByTestId('empty-state')).not.toHaveTextContent('Laser 1')
    );
    expect(screen.getByTestId('empty-state')).toHaveTextContent('No operations found');
    expect(screen.queryByRole('button', { name: /view all operations/i })).not.toBeInTheDocument();
  });
});
