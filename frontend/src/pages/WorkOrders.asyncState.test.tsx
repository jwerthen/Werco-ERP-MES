/**
 * Batch 3 — async-state standardization (WorkOrders).
 *
 * Locks the new load-failure / empty-result pattern on the WorkOrders list:
 *
 *   1. When the work-order fetch rejects, the list section renders the shared
 *      <ErrorState> (role="alert") instead of a blank list, and clicking Retry
 *      re-invokes api.getWorkOrders; on the retry's success the rows render and
 *      the error block clears.
 *   2. When the fetch resolves to an empty array, the page renders the shared
 *      <EmptyState> with its real title ("No work orders found"), not a bare
 *      "No X" string.
 *   3. When a row mutation (delete) rejects, the page surfaces the failure as an
 *      error toast (showToast('error', ...)) rather than the old window.alert.
 *      Asserted by rendering inside a real ToastProvider and reading the toast
 *      text out of the live aria-live region.
 *
 * WorkOrders mounts a websocket + visibility/focus/interval refresh loop. Those
 * hooks are mocked out so the only data source is the explicit api mock, keeping
 * the retry call-count assertions deterministic. Pre-existing act() noise from
 * the polling timers is irrelevant here — every assertion targets concrete
 * output, never timing.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkOrders from './WorkOrders';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrders: jest.fn(),
    deleteWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: true },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

// Silence the realtime refresh loop so api.getWorkOrders is only called by the
// explicit mount load + the Retry click — nothing else perturbs the counts.
jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const sampleWorkOrder = {
  id: 1,
  work_order_number: 'WO-1001',
  part_id: 10,
  work_order_type: 'production',
  part_number: 'PN-AAA',
  part_name: 'Bracket Assembly',
  part_type: 'manufactured',
  status: 'draft' as const,
  priority: 2,
  quantity_ordered: 50,
  quantity_complete: 0,
  customer_name: 'Acme Aero',
};

function renderWorkOrders() {
  return render(
    <MemoryRouter>
      <WorkOrders />
    </MemoryRouter>
  );
}

// Same, but wrapped in a real ToastProvider so showToast renders an actual
// toast we can read (the default context value is a no-op).
function renderWorkOrdersWithToasts() {
  return render(
    <ToastProvider>
      <MemoryRouter>
        <WorkOrders />
      </MemoryRouter>
    </ToastProvider>
  );
}

describe('WorkOrders async-state (Batch 3)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.releaseWorkOrder.mockResolvedValue({});
    mockedApi.deleteWorkOrder.mockResolvedValue({});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders ErrorState on load failure and recovers content on Retry', async () => {
    // First load rejects → ErrorState; second (Retry) load resolves → rows.
    mockedApi.getWorkOrders
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce([sampleWorkOrder]);

    renderWorkOrders();

    // The section renders the shared error block (not a blank list).
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Could not load work orders.')).toBeInTheDocument();
    // No work-order content yet.
    expect(screen.queryByRole('link', { name: 'WO-1001' })).not.toBeInTheDocument();
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(1);

    // Click Retry → re-invokes the fetch.
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));

    // On the retry's success, rows render and the error block clears.
    await waitFor(() => {
      expect(screen.getAllByRole('link', { name: 'WO-1001' }).length).toBeGreaterThan(0);
    });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(mockedApi.getWorkOrders).toHaveBeenCalledTimes(2);
  });

  it('renders EmptyState with its title when the fetch resolves to an empty list', async () => {
    mockedApi.getWorkOrders.mockResolvedValue([]);

    renderWorkOrders();

    // The shared EmptyState, identified by its testid + real title.
    const empty = await screen.findByTestId('empty-state');
    expect(within(empty).getByText('No work orders found')).toBeInTheDocument();
    // The CTA to create a work order is offered.
    expect(within(empty).getByRole('link', { name: /New Work Order/i })).toHaveAttribute(
      'href',
      '/work-orders/new'
    );
    // No error block on an empty (successful) load.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows an error toast when a row delete mutation fails', async () => {
    mockedApi.getWorkOrders.mockResolvedValue([sampleWorkOrder]);
    // Reject with the axios-shaped error the handler reads (err.response.data.detail).
    mockedApi.deleteWorkOrder.mockRejectedValueOnce({
      response: { data: { detail: 'Cannot delete a released work order' } },
    });
    jest.spyOn(window, 'confirm').mockReturnValue(true);

    renderWorkOrdersWithToasts();

    // Wait for the row, then click its desktop-table Delete control.
    const woLinks = await screen.findAllByRole('link', { name: 'WO-1001' });
    const table = woLinks.find((el) => el.closest('table'))?.closest('table') as HTMLElement;
    const deleteButton = within(table).getByTitle('Delete');
    fireEvent.click(deleteButton);

    await waitFor(() => {
      expect(mockedApi.deleteWorkOrder).toHaveBeenCalledWith(1);
    });

    // The server detail surfaces as toast text (not a window.alert, not silent).
    expect(
      await screen.findByText('Cannot delete a released work order')
    ).toBeInTheDocument();
  });
});
