/**
 * FEPERF-4 — WorkOrderDetail "Complete" double-submit guard, plus the
 * scrap-reason capture the backend now requires (HTTP 422 when scrap > 0).
 *
 * The completion flow no longer uses stacked prompt() dialogs: the header
 * "Complete" button opens a CompleteWorkModal that collects qty complete + qty
 * scrapped + a conditionally-required scrap reason, and the modal's own submit
 * button fires api.completeWorkOrder (server-gated → non-optimistic). This test
 * holds api.completeWorkOrder on a pending promise, submits the modal, and
 * asserts the submit button disables and a second click does NOT fire a second
 * mutation until the promise resolves. A second test confirms the entered
 * quantities reach the API.
 *
 * The page is heavy (websocket, many secondary fetches) so all side-channels are
 * mocked. The component fixture has zero operations, which keeps exactly ONE
 * header "Complete" button on screen (the work-order-level button that opens the
 * modal) without the per-row operation Complete buttons.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react';
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
    // MaterialTiesPanel loads on mount; an unmocked method is `undefined` and
    // surfaces as a silent <ErrorState role="alert"> instead of a red test.
    getMaterialAllocations: jest.fn(),
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
  });

  it('keeps secondary fetch failures distinct from verified empty engineering data', async () => {
    mockedApi.getMaterialRequirements.mockRejectedValueOnce(new Error('Offline'));
    mockedApi.getWorkOrderBlockers.mockRejectedValueOnce(new Error('Offline'));
    mockedApi.getDocuments.mockRejectedValue(new Error('Offline'));
    renderDetail();
    expect(await screen.findByText(/Could not verify material requirements/)).toBeInTheDocument();
    expect(screen.getByText(/Could not verify attached drawings/)).toBeInTheDocument();
    expect(screen.getByText(/Could not verify blockers/)).toBeInTheDocument();
  });

  it('renders the primary work order and settled support while drawings are still loading', async () => {
    let resolveDrawings!: (value: unknown[]) => void;
    mockedApi.getDocuments.mockImplementation((params: any) => params?.work_order_id
      ? new Promise(resolve => { resolveDrawings = resolve; })
      : Promise.resolve([]));
    renderDetail();
    expect(await screen.findByRole('button', { name: /^Complete$/ })).toBeEnabled();
    expect(await screen.findByText('No blockers reported')).toBeInTheDocument();
    expect(screen.getByText('Loading attached drawings…')).toBeInTheDocument();
    expect(screen.queryByText('No drawing PDF attached')).not.toBeInTheDocument();
    await act(async () => { resolveDrawings([]); });
    expect(await screen.findByText('No drawing PDF attached')).toBeInTheDocument();
  });

  it('keeps last-good drawings and blockers visible when a background refresh fails', async () => {
    mockedApi.getDocuments.mockImplementation((params: any) => Promise.resolve(params?.work_order_id ? [{
      id: 11, title: 'Fixture drawing', document_number: 'DOC-11', revision: 'A',
      file_name: 'fixture.pdf', mime_type: 'application/pdf', file_size: 100, work_order_id: 42,
    }] as never : []));
    mockedApi.getWorkOrderBlockers.mockResolvedValue([{
      id: 12, title: 'Fixture material missing', status: 'open', severity: 'high',
      category: 'material_missing', description: 'Waiting for plate', work_order_id: 42,
      created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
    }] as never);
    renderDetail();
    expect(await screen.findByRole('button', { name: 'Preview Fixture drawing' })).toBeInTheDocument();
    expect(await screen.findByText('Fixture material missing')).toBeInTheDocument();
    mockedApi.getDocuments.mockRejectedValue(new Error('Offline'));
    mockedApi.getWorkOrderBlockers.mockRejectedValue(new Error('Offline'));
    fireEvent(window, new Event('focus'));
    expect(await screen.findByText(/Could not verify attached drawings/)).toBeInTheDocument();
    expect(await screen.findByText(/Could not verify blockers/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Preview Fixture drawing' })).toBeInTheDocument();
    expect(screen.getByText('Fixture material missing')).toBeInTheDocument();
    expect(screen.getByText('Unknown open issues')).toBeInTheDocument();
    expect(screen.getByText('Attachments unverified')).toBeInTheDocument();
  });

  // Opens the CompleteWorkModal from the header button and returns the dialog.
  async function openCompleteModal() {
    const headerButton = await screen.findByRole('button', { name: /^Complete$/ });
    expect(headerButton).toBeEnabled();
    fireEvent.click(headerButton);
    return screen.findByRole('dialog');
  }

  it('disables the modal submit while the mutation is in flight and ignores a second click', async () => {
    // Hold the mutation open so the in-flight state is observable.
    let resolveComplete!: (value: unknown) => void;
    mockedApi.completeWorkOrder.mockReturnValue(
      new Promise((resolve) => {
        resolveComplete = resolve;
      })
    );

    renderDetail();

    const dialog = await openCompleteModal();

    // The modal's own submit fires the mutation (default qty = ordered, scrap 0).
    const submit = within(dialog).getByRole('button', { name: /^Complete$/ });
    fireEvent.click(submit);

    // Submit reflects the in-flight state and is disabled.
    const inFlightButton = await within(dialog).findByRole('button', { name: /Completing/i });
    expect(inFlightButton).toBeDisabled();
    expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);

    // A second click while in-flight must NOT fire another mutation.
    fireEvent.click(inFlightButton);
    expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);

    resolveComplete({ id: 42, status: 'complete' });
    await waitFor(() => {
      expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);
    });
  });

  it('passes the entered quantities to api.completeWorkOrder (no scrap → no reason)', async () => {
    mockedApi.completeWorkOrder.mockResolvedValue({ id: 42, status: 'complete' });

    renderDetail();

    const dialog = await openCompleteModal();
    fireEvent.click(within(dialog).getByRole('button', { name: /^Complete$/ }));

    await waitFor(() => {
      // qty complete defaults to ordered (10), scrap 0, scrapReason and
      // scrapReasonCodeId (Lean Phase 1) both undefined.
      expect(mockedApi.completeWorkOrder).toHaveBeenCalledWith(42, 10, 0, undefined, undefined);
    });
  });

  it('requires a scrap reason when scrap > 0 before the API can fire', async () => {
    mockedApi.completeWorkOrder.mockResolvedValue({ id: 42, status: 'complete' });

    renderDetail();

    const dialog = await openCompleteModal();

    // Enter a positive scrap quantity — the reason field appears and submit blocks.
    const scrapInput = within(dialog).getByLabelText(/Quantity scrapped/i);
    fireEvent.change(scrapInput, { target: { value: '2' } });

    const submit = within(dialog).getByRole('button', { name: /^Complete$/ });
    expect(submit).toBeDisabled(); // no reason chosen yet
    fireEvent.click(submit);
    expect(mockedApi.completeWorkOrder).not.toHaveBeenCalled();

    // Choose a scrap reason from the SelectField, then submit succeeds. The
    // SelectField commits a choice on mousedown (it preventDefaults to keep
    // focus), so drive it with mouseDown rather than click.
    fireEvent.click(within(dialog).getByRole('combobox', { name: /scrap reason/i }));
    fireEvent.mouseDown(await screen.findByRole('option', { name: /Out of tolerance/i }));

    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => {
      // Legacy fallback (no company scrap codes): text reason, no code id.
      expect(mockedApi.completeWorkOrder).toHaveBeenCalledWith(42, 10, 2, 'Out of tolerance', undefined);
    });
  });
});
