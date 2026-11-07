/**
 * WorkOrderDetail — owner ruling 1 (2026-11-05) on the office page.
 *
 * A short count is reported as scrap with a reason, or the operation stays open. The
 * page shows that before anyone clicks (the "n unaccounted" chip, the modal's SHORT
 * readout), reflects only what the server returns (non-optimistic: a saved-but-open
 * result is a WARNING toast, never "completed"), keeps refusals INLINE, and offers the
 * one follow-up a refusal names — "Clock them out and close" for the still-clocked-in
 * 409, "Reopen Op N and correct" for the correction that would leave a completed
 * operation short.
 *
 * Harness mirrors WorkOrderDetail.stepsIncomplete.test.tsx (all side-channels mocked,
 * a real ToastProvider so toast text is assertable).
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui/Toast';

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
    reduceWOOperationProduction: jest.fn(),
    getOperationSteps: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useScrapReasonCodes', () => ({ useScrapReasonCodes: () => ({ codes: [] }) }));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

/** Op 10 is mid-run: 8 good of 10 recorded on the floor, 2 still unaccounted. */
const RUNNING_OP = {
  id: 71,
  work_order_id: 42,
  work_center_id: 5,
  sequence: 10,
  operation_number: '10',
  name: 'Deburr',
  status: 'in_progress',
  quantity_complete: 8,
  quantity_scrapped: 0,
  actual_start: '2026-11-05T14:00:00Z',
  started_by: 7,
  estimated_hours: 1,
  upstream_quantity_short: 0,
  quantity_good_ceiling: 10,
  quantity_unaccounted: 2,
};

/** Op 20 has not started: no progress, so no chip. */
const FRESH_OP = {
  ...RUNNING_OP,
  id: 72,
  sequence: 20,
  operation_number: '20',
  name: 'Inspect',
  status: 'ready',
  quantity_complete: 0,
  actual_start: undefined,
  started_by: undefined,
  quantity_unaccounted: 10,
};

function workOrder(overrides: Record<string, unknown> = {}) {
  return {
    id: 42,
    version: 1,
    work_order_number: 'WO-0042',
    part_id: 100,
    work_order_type: 'production',
    sequential_operations: false,
    quantity_ordered: 10,
    quantity_complete: 8,
    quantity_scrapped: 0,
    quantity_deliverable: 8,
    quantity_accounted_loss: 0,
    status: 'in_progress',
    priority: 3,
    estimated_hours: 8,
    actual_hours: 2,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    operations: [{ ...RUNNING_OP }, { ...FRESH_OP }],
    ...overrides,
  };
}

function renderDetail() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/42']}>
        <Routes>
          <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );
}

function operationRow(name: string): HTMLElement {
  const row = screen.getByText(name).closest('tr');
  if (!row) throw new Error(`no row for ${name}`);
  return row as HTMLElement;
}

async function openOperationModal(name: string) {
  await screen.findByText(name);
  fireEvent.click(within(operationRow(name)).getByTitle('Complete Operation'));
  return within(await screen.findByRole('dialog', { name: `Complete operation "${name}"` }));
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getWorkOrder.mockResolvedValue(workOrder() as never);
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.reduceWOOperationProduction.mockResolvedValue({});
});

describe('operation rows', () => {
  it('shows "n unaccounted" on an open operation with progress, read with its Complete button', async () => {
    renderDetail();
    await screen.findByText('Deburr');
    const chip = within(operationRow('Deburr')).getByTestId('op-unaccounted-71');
    expect(chip).toHaveTextContent('2 unaccounted');
    const complete = within(operationRow('Deburr')).getByTitle('Complete Operation');
    expect(complete).toBeEnabled();
    expect(complete).toHaveAccessibleDescription('2 unaccounted');
    // A fresh operation has nothing to flag.
    expect(within(operationRow('Inspect')).queryByTestId('op-unaccounted-72')).not.toBeInTheDocument();
  });

  it('keeps BOTH conditions on a held job: READY is disabled by the gate, a running row keeps its chip', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(workOrder({ status: 'on_hold' }) as never);
    renderDetail();
    await screen.findByText('Deburr');
    // READY on an ON_HOLD work order: completing would START it, which the server refuses.
    const ready = within(operationRow('Inspect')).getByRole('button', { name: /Complete/ });
    expect(ready).toBeDisabled();
    expect(ready).toHaveAttribute('title', 'Resume the work order first — completing would start this operation');
    // IN_PROGRESS stays closable on a held job, and still says what a close needs.
    expect(within(operationRow('Deburr')).getByTitle('Complete Operation')).toBeEnabled();
    expect(within(operationRow('Deburr')).getByTestId('op-unaccounted-71')).toHaveTextContent('2 unaccounted');
  });
});

describe('office operation complete', () => {
  it('feeds the modal the server figures: recorded prefill and the SHORT readout', async () => {
    renderDetail();
    const dialog = await openOperationModal('Deburr');
    expect(dialog.getByLabelText(/Quantity completed/)).toHaveValue(8);
    expect(dialog.getByTestId('complete-work-short')).toHaveTextContent('2 of 10 not yet accounted for');
    expect(dialog.getByTestId('complete-work-short')).toHaveTextContent('Saving keeps Op 10 open');
    expect(dialog.getByRole('button', { name: /Save progress · leave open/ })).toBeDisabled();
  });

  it('a saved-but-open result is a WARNING toast and the modal closes', async () => {
    mockedApi.completeWOOperation.mockResolvedValue({
      message: 'Progress updated',
      is_fully_complete: false,
      operation: { id: 71, status: 'in_progress', quantity_complete: 9, quantity_scrapped: 0, quantity_unaccounted: 1 },
      closed_time_entries: [],
      quality_exceptions: [],
    });
    renderDetail();
    const dialog = await openOperationModal('Deburr');
    fireEvent.change(dialog.getByLabelText(/Quantity completed/), { target: { value: '9' } });
    // The floor recorded 8 and labor exists: booking 9 needs a reason.
    fireEvent.change(dialog.getByLabelText(/Why record more than the floor counted/), {
      target: { value: 'Inspector found one more good part' },
    });
    await act(async () => {
      fireEvent.click(dialog.getByRole('button', { name: /Save progress · leave open/ }));
    });
    const toast = await screen.findByText('Progress saved — Op 10 left open, 1 pc not yet accounted for');
    expect(toast.closest('[role="alert"]')).not.toBeNull();
    expect(mockedApi.completeWOOperation).toHaveBeenCalledWith(71, 9, {
      quantityScrapped: null,
      overrideReason: 'Inspector found one more good part',
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('keeps the still-clocked-in refusal inline and resubmits with the acknowledgement on confirm', async () => {
    mockedApi.completeWOOperation
      .mockRejectedValueOnce({
        response: {
          status: 409,
          data: {
            detail:
              'Dana R. is still clocked in to Op 10 — have them clock out with their count first, or leave the operation open. Resubmit with acknowledge_open_entries to clock them out; pieces they have not reported will not be counted.',
          },
        },
      })
      .mockResolvedValueOnce({
        message: 'Operation completed',
        is_fully_complete: true,
        operation: { id: 71, status: 'complete', quantity_complete: 8, quantity_scrapped: 2, quantity_unaccounted: 0 },
        closed_time_entries: [{ time_entry_id: 900, user_id: 7, operator_name: 'Dana R.' }],
        quality_exceptions: [],
      });
    renderDetail();
    const dialog = await openOperationModal('Deburr');
    fireEvent.click(dialog.getByRole('button', { name: 'Report 2 as scrap' }));
    fireEvent.click(dialog.getByRole('combobox', { name: /scrap reason/i }));
    fireEvent.mouseDown(screen.getByRole('option', { name: 'Out of tolerance' }));
    await act(async () => {
      fireEvent.click(dialog.getByRole('button', { name: /^Complete$/ }));
    });

    // Refused: the dialog stays, the text is verbatim and inline, nothing closed.
    const alert = await dialog.findByRole('alert');
    expect(alert).toHaveTextContent('Dana R. is still clocked in to Op 10');
    expect(dialog.getByLabelText(/Quantity scrapped/)).toHaveValue(2);
    expect(mockedApi.completeWOOperation).toHaveBeenNthCalledWith(1, 71, 8, {
      quantityScrapped: 2,
      scrapReason: 'Out of tolerance',
    });

    await act(async () => {
      fireEvent.click(within(alert).getByRole('button', { name: 'Clock them out and close' }));
    });
    expect(mockedApi.completeWOOperation).toHaveBeenNthCalledWith(2, 71, 8, {
      quantityScrapped: 2,
      scrapReason: 'Out of tolerance',
      acknowledgeOpenEntries: true,
    });
    expect(await screen.findByText('Op 10 completed — clocked out Dana R.')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});

describe('office work-order complete', () => {
  it('judges the force-complete with the server bounds from the read model', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      workOrder({ quantity_accounted_loss: 2, quantity_deliverable: 8, operations: [] }) as never
    );
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /^Complete$/ }));
    const dialog = within(await screen.findByRole('dialog', { name: 'Complete work order WO-0042' }));
    // 8 good + the 2 the floor already reported lost = the order: closable as-is.
    expect(dialog.getByLabelText(/Quantity completed/)).toHaveValue(8);
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeEnabled();
    fireEvent.change(dialog.getByLabelText(/Quantity completed/), { target: { value: '7' } });
    expect(dialog.getByTestId('complete-work-short')).toHaveTextContent('1 of 10 unaccounted for');
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeDisabled();
  });

  it('keeps a force-complete refusal inline beside the entered totals', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(workOrder({ quantity_accounted_loss: 2, operations: [] }) as never);
    mockedApi.completeWorkOrder.mockRejectedValue({
      response: {
        status: 403,
        data: { detail: 'Closing work short must be done by a person signed in to the app, not an API token.' },
      },
    });
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /^Complete$/ }));
    const dialog = within(await screen.findByRole('dialog', { name: 'Complete work order WO-0042' }));
    await act(async () => {
      fireEvent.click(dialog.getByRole('button', { name: /^Complete$/ }));
    });
    expect(await dialog.findByRole('alert')).toHaveTextContent('not an API token');
    expect(dialog.getByLabelText(/Quantity completed/)).toHaveValue(8);
    expect(mockedApi.completeWorkOrder).toHaveBeenCalledWith(42, 8, null, undefined, undefined, undefined);
  });
});

describe('correct count on a completed operation (reopen)', () => {
  const COMPLETE_OP = { ...RUNNING_OP, status: 'complete', quantity_complete: 10, quantity_unaccounted: 0 };
  const SHORT_REFUSAL =
    'Lowering Op 10 by 3 would leave it 3 short, and a completed operation cannot be short. Resubmit with reopen_operation to reopen it (it returns to in progress).';

  async function openCorrect() {
    mockedApi.getWorkOrder.mockResolvedValue(workOrder({ operations: [{ ...COMPLETE_OP }] }) as never);
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /correct count/i }));
    await screen.findByRole('heading', { name: /correct count — op 10 deburr/i });
    fireEvent.change(screen.getByLabelText(/quantity to remove/i), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText(/reason for correction/i), { target: { value: 'double-scanned' } });
  }

  it('offers an explicit "Reopen Op 10 and correct" only after the server says the count would go short', async () => {
    mockedApi.reduceWOOperationProduction.mockRejectedValueOnce({
      response: { status: 409, data: { detail: SHORT_REFUSAL } },
    });
    await openCorrect();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /remove from completed/i }));
    });
    // First attempt: never a reopen.
    expect(mockedApi.reduceWOOperationProduction).toHaveBeenNthCalledWith(1, 71, {
      quantity_delta: 3,
      reason: 'double-scanned',
      source: 'desktop',
    });
    expect(await screen.findByTestId('wo-correct-error')).toHaveTextContent(SHORT_REFUSAL);
    expect(screen.getByTestId('wo-correct-reopen-note')).toHaveTextContent('Reopening returns Op 10 to in progress');
    const reopen = screen.getByRole('button', { name: 'Reopen Op 10 and correct' });

    await act(async () => {
      fireEvent.click(reopen);
    });
    expect(mockedApi.reduceWOOperationProduction).toHaveBeenNthCalledWith(2, 71, {
      quantity_delta: 3,
      reason: 'double-scanned',
      source: 'desktop',
      reopen_operation: true,
    });
    expect(await screen.findByText(/Removed 3 from operation 10 Deburr and reopened it/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /correct count — op 10 deburr/i })).not.toBeInTheDocument()
    );
  });

  it('withdraws the offer when the quantity changes, and when the reopen itself is refused', async () => {
    mockedApi.reduceWOOperationProduction
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: SHORT_REFUSAL } } })
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: SHORT_REFUSAL } } })
      .mockRejectedValueOnce({
        response: { status: 409, data: { detail: 'Op 20 is already complete with these units; correct Op 20 first.' } },
      });
    await openCorrect();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /remove from completed/i }));
    });
    expect(await screen.findByRole('button', { name: 'Reopen Op 10 and correct' })).toBeInTheDocument();

    // A different quantity has to be asked about again.
    fireEvent.change(screen.getByLabelText(/quantity to remove/i), { target: { value: '2' } });
    expect(screen.queryByRole('button', { name: 'Reopen Op 10 and correct' })).not.toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /remove from completed/i }));
    });
    expect(mockedApi.reduceWOOperationProduction).toHaveBeenLastCalledWith(71, {
      quantity_delta: 2,
      reason: 'double-scanned',
      source: 'desktop',
    });

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Reopen Op 10 and correct' }));
    });
    expect(await screen.findByTestId('wo-correct-error')).toHaveTextContent('correct Op 20 first');
    expect(screen.queryByRole('button', { name: 'Reopen Op 10 and correct' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /remove from completed/i })).toBeInTheDocument();
  });
});
