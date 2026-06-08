/**
 * FEPERF-4 — WorkOrderDetail "Complete" button double-submit guard.
 *
 * handleComplete now flips a `completing` state around the api.completeWorkOrder
 * call and early-returns if already in-flight; the header "Complete" button is
 * `disabled={completing}`. This test renders an in_progress work order, holds
 * api.completeWorkOrder on a pending promise, clicks Complete, and asserts the
 * button disables and a second click does NOT fire a second mutation until the
 * promise resolves.
 *
 * The page is heavy (websocket, many secondary fetches) so all side-channels are
 * mocked. The component fixture has zero operations, which keeps exactly ONE
 * "Complete" button on screen (the work-order-level header button) — the unit
 * under test — without the per-row operation Complete buttons.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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
    completeWorkOrder: jest.fn(),
    completeWOOperation: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
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

const inProgressWorkOrder = {
  id: 42,
  version: 1,
  work_order_number: 'WO-0042',
  part_id: 100,
  work_order_type: 'production',
  quantity_ordered: 10,
  quantity_complete: 4,
  quantity_scrapped: 0,
  status: 'in_progress',
  priority: 3,
  estimated_hours: 8,
  actual_hours: 2,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
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

describe('FEPERF-4: WorkOrderDetail Complete double-submit guard', () => {
  let promptSpy: jest.SpyInstance;
  let alertSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();

    mockedApi.getWorkOrder.mockResolvedValue({ ...inProgressWorkOrder });
    // Secondary loads — page tolerates rejections, but resolve them to keep noise down.
    mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
    mockedApi.getMaterialRequirements.mockResolvedValue(null);
    mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
    mockedApi.getActiveUsers.mockResolvedValue([]);
    mockedApi.getUsers.mockResolvedValue([]);
    mockedApi.getDocuments.mockResolvedValue([]);

    // handleComplete reads two quantities via prompt(); supply valid answers.
    promptSpy = jest.spyOn(window, 'prompt').mockImplementation((message?: string) => {
      // First prompt: quantity completed. Second: quantity scrapped.
      if (message && message.includes('scrapped')) return '0';
      return '10';
    });
    alertSpy = jest.spyOn(window, 'alert').mockImplementation(() => undefined);
  });

  afterEach(() => {
    promptSpy.mockRestore();
    alertSpy.mockRestore();
  });

  it('disables the Complete button while the mutation is in flight and ignores a second click', async () => {
    // Hold the mutation open so the in-flight state is observable.
    let resolveComplete!: (value: unknown) => void;
    mockedApi.completeWorkOrder.mockReturnValue(
      new Promise((resolve) => {
        resolveComplete = resolve;
      })
    );

    renderDetail();

    // Wait for the in_progress header button to appear after the WO loads.
    const completeButton = await screen.findByRole('button', { name: /^Complete$/ });
    expect(completeButton).toBeEnabled();

    // First click kicks off the mutation.
    fireEvent.click(completeButton);

    // Button reflects the in-flight state and is disabled.
    const inFlightButton = await screen.findByRole('button', { name: /Completing/i });
    expect(inFlightButton).toBeDisabled();
    expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);

    // A second click while in-flight must NOT fire another mutation (button is
    // disabled AND handleComplete early-returns on the `completing` guard).
    fireEvent.click(inFlightButton);
    expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);

    // Resolving the promise releases the guard and re-enables the button.
    resolveComplete({ id: 42, status: 'complete' });
    await waitFor(() => {
      expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);
    });
  });

  it('passes the prompted quantities to api.completeWorkOrder', async () => {
    mockedApi.completeWorkOrder.mockResolvedValue({ id: 42, status: 'complete' });

    renderDetail();

    const completeButton = await screen.findByRole('button', { name: /^Complete$/ });
    fireEvent.click(completeButton);

    await waitFor(() => {
      expect(mockedApi.completeWorkOrder).toHaveBeenCalledWith(42, 10, 0);
    });
  });
});
