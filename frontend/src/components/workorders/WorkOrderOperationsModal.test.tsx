/**
 * WorkOrderOperationsModal — a WORK-ORDER hold names the door that lifts it.
 *
 * The modal used to say "Clear the work order hold", but "Clear Hold" is the
 * OPERATION-level action (on WorkOrderDetail's table and the floor screens). A
 * work-order hold is lifted with "Resume Work Order" on the work order's own
 * page, so the copy says that and links there. The modal's stricter client-side
 * blocking of every operation on a held job is unchanged.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import WorkOrderOperationsModal from './WorkOrderOperationsModal';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    completeWOOperation: jest.fn(),
    completeWorkOrder: jest.fn(),
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

function renderModal() {
  return render(
    <MemoryRouter>
      <WorkOrderOperationsModal workOrderId={42} workOrderNumber="WO-0042" onClose={jest.fn()} onUpdated={jest.fn()} />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('WorkOrderOperationsModal — held work order copy', () => {
  it('names Resume Work Order, links to the work order, and never says "Clear the work order hold"', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(heldWorkOrder as never);
    renderModal();

    const link = await screen.findByRole('link', { name: 'Resume Work Order on its page' });
    expect(link).toHaveAttribute('href', '/work-orders/42');
    expect(screen.queryByText(/Clear the work order hold/)).not.toBeInTheDocument();
  });

  it('keeps every operation blocked, with the per-row reason naming Resume Work Order', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(heldWorkOrder as never);
    renderModal();

    const list = await screen.findByRole('list', { name: 'Work order operations' });
    const complete = within(list).getByRole('button', { name: /Complete operation 10: Laser Cut/ });
    expect(complete).toBeDisabled();
    expect(complete).toHaveAttribute(
      'title',
      'Resume the work order (Resume Work Order on its page) before completing.'
    );
  });
});
