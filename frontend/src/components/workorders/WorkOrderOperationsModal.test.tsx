/**
 * WorkOrderOperationsModal — a WORK-ORDER hold names the door that lifts it,
 * and refuses only what the server refuses.
 *
 * The modal used to say "Clear the work order hold", but "Clear Hold" is the
 * OPERATION-level action (on WorkOrderDetail's table and the floor screens). A
 * work-order hold is lifted with "Resume Work Order" on the work order's own
 * page, so the copy says that and links there.
 *
 * What a held job blocks is the server's rule, mirrored as WorkOrderDetail
 * mirrors it: completing a READY operation STARTS it first, and a start on a
 * held job is refused 409, so that row is blocked. An IN_PROGRESS operation is
 * not — labor already running when the job was held must always be closable —
 * so its Complete stays live. This modal used to block every row of a held
 * job, which sent a supervisor to put a quality-held job back on the floor just
 * to close work the server would have closed as-is.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import WorkOrderOperationsModal from './WorkOrderOperationsModal';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    completeWOOperation: jest.fn(),
    completeWorkOrder: jest.fn(),
    getScrapReasonCodes: jest.fn(),
  },
}));

jest.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'supervisor', is_superuser: false } }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const READY_OP = {
  id: 71,
  work_order_id: 42,
  sequence: 10,
  operation_number: '10',
  name: 'Laser Cut',
  status: 'ready',
  quantity_complete: 0,
  quantity_scrapped: 0,
  work_center_name: 'Laser 1',
};

const RUNNING_OP = {
  id: 72,
  work_order_id: 42,
  sequence: 20,
  operation_number: '20',
  name: 'Deburr',
  status: 'in_progress',
  quantity_complete: 8,
  quantity_scrapped: 0,
  work_center_name: 'Deburr 1',
};

const heldWorkOrder = {
  id: 42,
  version: 1,
  work_order_number: 'WO-0042',
  work_order_type: 'production',
  quantity_ordered: 10,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'on_hold',
  sequential_operations: false,
  operations: [READY_OP],
};

const RESUME_COPY = 'Resume the work order (Resume Work Order on its page) before completing.';

function renderModal() {
  return render(
    <MemoryRouter>
      <WorkOrderOperationsModal workOrderId={42} workOrderNumber="WO-0042" onClose={jest.fn()} onUpdated={jest.fn()} />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
});

describe('WorkOrderOperationsModal — held work order copy', () => {
  it('names Resume Work Order, links to the work order, and never says "Clear the work order hold"', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(heldWorkOrder as never);
    renderModal();

    const link = await screen.findByRole('link', { name: 'Resume Work Order on its page' });
    expect(link).toHaveAttribute('href', '/work-orders/42');
    expect(screen.queryByText(/Clear the work order hold/)).not.toBeInTheDocument();
    // The banner says what the hold does and does not block, so it never claims
    // running work is stuck behind it.
    expect(link.closest('p')).toHaveTextContent(/Operations already in progress can still be completed/);
  });

  it('blocks a READY operation, with the per-row reason naming Resume Work Order', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(heldWorkOrder as never);
    renderModal();

    const list = await screen.findByRole('list', { name: 'Work order operations' });
    const complete = within(list).getByRole('button', { name: /Complete operation 10: Laser Cut/ });
    expect(complete).toBeDisabled();
    expect(complete).toHaveAttribute('title', RESUME_COPY);
  });

  it('leaves an IN_PROGRESS operation completable on a held job, exactly as the server does', async () => {
    mockedApi.getWorkOrder.mockResolvedValue({ ...heldWorkOrder, operations: [READY_OP, RUNNING_OP] } as never);
    mockedApi.completeWOOperation.mockResolvedValue({ message: 'Operation completed' } as never);
    renderModal();

    const list = await screen.findByRole('list', { name: 'Work order operations' });
    // The READY sibling is still refused — the hold blocks starts, nothing else.
    expect(within(list).getByRole('button', { name: /Complete operation 10: Laser Cut/ })).toBeDisabled();

    const running = within(list).getByRole('button', { name: /Complete operation 20: Deburr/ });
    expect(running).toBeEnabled();
    expect(running).not.toHaveAttribute('title');
    const runningRow = running.closest('li') as HTMLElement;
    expect(runningRow).not.toHaveTextContent(RESUME_COPY);

    await userEvent.click(running);
    await userEvent.click(await screen.findByRole('button', { name: /^Complete$/ }));

    expect(mockedApi.completeWOOperation).toHaveBeenCalledWith(72, 10, 0, undefined);
    expect(await screen.findByText('Operation 20 (Deburr) completed.')).toBeInTheDocument();
  });
});
