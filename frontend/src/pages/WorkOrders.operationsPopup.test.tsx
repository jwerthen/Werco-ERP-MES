import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../services/api';
import { workOrderBrowseFixture } from '../testUtils/workOrderBrowseFixture';
import WorkOrders from './WorkOrders';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    browseWorkOrders: jest.fn(),
    getWorkOrder: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    completeWOOperation: jest.fn(),
    completeWorkOrder: jest.fn(),
  },
}));
let mockRole = 'admin';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: mockRole, is_superuser: false } }),
}));
jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({ getAccessToken: () => null, buildWsUrl: () => 'ws://localhost/test' }));

const mockedApi = api as jest.Mocked<typeof api>;
const summary = {
  id: 42,
  work_order_number: 'WO-0042',
  version: 1,
  part_id: 10,
  part_number: 'BRACKET-01',
  part_name: 'Mounting bracket',
  part_type: 'manufactured',
  work_order_type: 'production',
  status: 'in_progress',
  priority: 2,
  quantity_ordered: 10,
  quantity_complete: 0,
  quantity_scrapped: 0,
  customer_name: 'Example customer',
  estimated_hours: 2,
  actual_hours: 0,
  created_at: '2026-09-17T12:00:00Z',
  updated_at: '2026-09-17T12:00:00Z',
};
const firstOp = {
  id: 71,
  version: 1,
  work_order_id: 42,
  work_center_id: 5,
  work_center_name: 'Laser',
  sequence: 10,
  operation_number: 'OP10',
  name: 'Cut',
  status: 'ready',
  quantity_complete: 0,
  quantity_scrapped: 0,
  component_quantity: 10,
};
const secondOp = {
  ...firstOp,
  id: 72,
  sequence: 20,
  operation_number: 'OP20',
  name: 'Bend',
  work_center_name: 'Press brake',
};
let job: any;
let rows: any[];

function Location() {
  const location = useLocation();
  return (
    <output aria-label="Current location">
      {location.pathname}
      {location.search}
    </output>
  );
}
function renderList(query = '?status=in_progress') {
  return render(
    <MemoryRouter initialEntries={[`/work-orders${query}`]}>
      <WorkOrders />
      <Location />
    </MemoryRouter>
  );
}
async function openPopup() {
  fireEvent.click(await screen.findByRole('link', { name: 'WO-0042' }));
  const dialog = await screen.findByRole('dialog', { name: 'Operations for WO-0042' });
  await within(dialog).findByText('Op 10 · Cut');
  return dialog;
}
async function openCompletion() {
  fireEvent.click(screen.getByRole('button', { name: 'Complete operation 10: Cut' }));
  const form = await screen.findByRole('dialog', { name: 'Complete operation "Cut"' });
  return within(form);
}

beforeEach(() => {
  jest.resetAllMocks();
  mockRole = 'admin';
  window.innerWidth = 1440;
  rows = [{ ...summary }];
  job = { ...summary, sequential_operations: true, operations: [{ ...firstOp }, { ...secondOp }] };
  mockedApi.browseWorkOrders.mockImplementation(async params => workOrderBrowseFixture(rows, params));
  mockedApi.getWorkOrder.mockImplementation(async () => JSON.parse(JSON.stringify(job)));
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
  mockedApi.completeWOOperation.mockResolvedValue({ message: 'Operation completed' });
  mockedApi.completeWorkOrder.mockResolvedValue({});
});

it.each(['', '&group=customer', '&group=part', '&group=status'])(
  'opens operations from the list%s and preserves the filtered URL',
  async group => {
    renderList(`?status=in_progress${group}`);
    await screen.findByRole('link', { name: 'WO-0042' });
    expect(mockedApi.getWorkOrder).not.toHaveBeenCalled();
    const popup = await openPopup();
    expect(mockedApi.getWorkOrder).toHaveBeenCalledWith(42);
    expect(within(popup).getByText('Press brake')).toBeInTheDocument();
    expect(within(popup).getByRole('link', { name: 'Open full work order' })).toHaveAttribute(
      'href',
      '/work-orders/42'
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Current location')).toHaveTextContent(`/work-orders?status=in_progress${group}`);
  }
);

it('opens the same operations popup from a mobile card', async () => {
  window.innerWidth = 390;
  renderList();
  fireEvent.click(await screen.findByRole('button', { name: 'Operations' }));
  expect(await screen.findByText('Op 10 · Cut')).toBeInTheDocument();
  expect(screen.getByRole('dialog', { name: 'Operations for WO-0042' })).toBeInTheDocument();
});

it('uses progressive operation numbers for mobile labels and completion when sequence is shared', async () => {
  window.innerWidth = 390;
  job.operations[1].sequence = 10;
  renderList();
  fireEvent.click(await screen.findByRole('button', { name: 'Operations' }));
  const popup = await screen.findByRole('dialog', { name: 'Operations for WO-0042' });
  await within(popup).findByText('Op 20 · Bend');
  fireEvent.click(within(popup).getByRole('button', { name: 'Complete operation 20: Bend' }));
  const form = await screen.findByRole('dialog', { name: 'Complete operation "Bend"' });
  fireEvent.click(within(form).getByRole('button', { name: 'Complete' }));

  expect(await within(popup).findByText('Operation 20 (Bend) completed.')).toBeInTheDocument();
  expect(mockedApi.completeWOOperation).toHaveBeenCalledWith(72, 10, 0, undefined);
  expect(mockedApi.completeWOOperation).toHaveBeenCalledTimes(1);
  expect(mockedApi.completeWorkOrder).not.toHaveBeenCalled();
});

it('completes an operation once, keeps the popup open, and refreshes progress and the list', async () => {
  let resolve!: (value: unknown) => void;
  mockedApi.completeWOOperation.mockReturnValueOnce(
    new Promise(done => {
      resolve = done;
    })
  );
  renderList();
  const popup = await openPopup();
  expect(within(popup).getByRole('button', { name: 'Complete operation 20: Bend' })).toBeDisabled();
  const form = await openCompletion();
  fireEvent.click(form.getByRole('button', { name: 'Complete' }));
  const pendingButton = form.getByRole('button', { name: /Completing/ });
  expect(pendingButton).toBeDisabled();
  fireEvent.click(pendingButton);
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(screen.getAllByRole('dialog')).toHaveLength(2);
  expect(mockedApi.completeWOOperation).toHaveBeenCalledTimes(1);
  expect(mockedApi.completeWOOperation).toHaveBeenCalledWith(71, 10, 0, undefined);
  expect(within(popup).getByText('0 / 2 operations complete')).toBeInTheDocument();

  job.operations[0].status = 'complete';
  job.operations[0].quantity_complete = 10;
  const reads = mockedApi.browseWorkOrders.mock.calls.length;
  await act(async () => resolve({ message: 'Operation completed' }));
  expect(await within(popup).findByText('1 / 2 operations complete')).toBeInTheDocument();
  await waitFor(() => expect(within(popup).getByRole('button', { name: 'Complete operation 20: Bend' })).toBeEnabled());
  expect(mockedApi.browseWorkOrders.mock.calls.length).toBeGreaterThan(reads);
  expect(screen.getByLabelText('Current location')).toHaveTextContent('/work-orders?status=in_progress');
});

it('reports partial progress honestly and retains server refusals inside the quantity dialog', async () => {
  mockedApi.completeWOOperation.mockRejectedValueOnce({
    response: {
      data: {
        detail: { code: 'STEPS_INCOMPLETE', missing: [{ step_id: 1, label: 'Measure slot', serials: ['SN-01'] }] },
      },
    },
  });
  renderList();
  const popup = await openPopup();
  const form = await openCompletion();
  fireEvent.click(form.getByRole('button', { name: 'Complete' }));
  expect(await form.findByRole('alert')).toHaveTextContent('Measure slot (SN-01)');
  expect(mockedApi.getWorkOrder).toHaveBeenCalledTimes(1);
  fireEvent.change(form.getByLabelText(/Quantity completed/), { target: { value: '4' } });
  job.operations[0].quantity_complete = 4;
  job.operations[0].status = 'in_progress';
  mockedApi.completeWOOperation.mockResolvedValueOnce({ message: 'Progress updated' });
  fireEvent.click(form.getByRole('button', { name: 'Complete' }));
  expect(await within(popup).findByText('Progress saved for operation 10 (Cut).')).toBeInTheDocument();
  expect(await within(popup).findByText('4 / 10 completed')).toBeInTheDocument();
  expect(within(popup).getByText('0 / 2 operations complete')).toBeInTheDocument();
});

it('requires a scrap reason before posting completion', async () => {
  renderList();
  await openPopup();
  const form = await openCompletion();
  fireEvent.change(form.getByLabelText('Quantity scrapped'), { target: { value: '1' } });
  expect(form.getByRole('button', { name: 'Complete' })).toBeDisabled();
  expect(mockedApi.completeWOOperation).not.toHaveBeenCalled();
});

it.each(['viewer', 'operator', 'shipping'])('allows %s to inspect but does not offer completion', async role => {
  mockRole = role;
  renderList();
  const popup = await openPopup();
  expect(within(popup).queryByRole('button', { name: /Complete/ })).not.toBeInTheDocument();
});

it('allows quality users to complete operations', async () => {
  mockRole = 'quality';
  renderList();
  await openPopup();
  expect(screen.getByRole('button', { name: 'Complete operation 10: Cut' })).toBeEnabled();
});

it.each(['draft', 'complete', 'closed', 'cancelled'])(
  'shows %s work orders without completion controls',
  async status => {
    job.status = status;
    renderList();
    const popup = await openPopup();
    expect(within(popup).queryByRole('button', { name: /Complete/ })).not.toBeInTheDocument();
  }
);

it('blocks held and pending operations, while laser nests ignore sequential routing', async () => {
  job.work_order_type = 'laser_cutting';
  job.operations[0].status = 'on_hold';
  renderList();
  const popup = await openPopup();
  expect(within(popup).getByRole('button', { name: 'Complete operation 10: Cut' })).toBeDisabled();
  expect(within(popup).getByRole('button', { name: 'Complete work order' })).toBeDisabled();
  expect(within(popup).getByRole('button', { name: 'Complete operation 20: Bend' })).toBeEnabled();
  job.operations[1].status = 'pending';
  fireEvent.click(within(popup).getByRole('button', { name: 'Refresh' }));
  await within(popup).findByText('This operation must be ready or in progress before completing.');
  expect(within(popup).getByRole('button', { name: 'Complete operation 20: Bend' })).toBeDisabled();
});

it('keeps a successful whole-order completion visible when the active list removes its row', async () => {
  mockedApi.completeWorkOrder.mockImplementation(async () => {
    job.status = 'complete';
    job.operations.forEach((operation: any) => {
      operation.status = 'complete';
    });
    rows = [];
    return {
      steps_bypassed: { count: 1, steps: [{ operation: '10', step_id: 1, label: 'Measure slot', serials: [] }] },
    };
  });
  renderList();
  const popup = await openPopup();
  fireEvent.click(within(popup).getByRole('button', { name: 'Complete work order' }));
  const form = within(await screen.findByRole('dialog', { name: 'Complete work order WO-0042' }));
  expect(form.getByText(/This completes all remaining operations/)).toBeInTheDocument();
  fireEvent.click(form.getByRole('button', { name: 'Complete' }));
  expect(await within(popup).findByText('2 / 2 operations complete')).toBeInTheDocument();
  expect(within(popup).getByText(/Completed with 1 step record bypassed: Measure slot/)).toBeInTheDocument();
  expect(await screen.findByText('No work orders found')).toBeInTheDocument();
  expect(screen.getByRole('dialog', { name: 'Operations for WO-0042' })).toBeInTheDocument();
  expect(mockedApi.completeWorkOrder).toHaveBeenCalledWith(42, 10, 0, undefined, undefined);
});

it('disables stale actions after a post-save refresh fails and recovers without repeating the write', async () => {
  renderList();
  const popup = await openPopup();
  const form = await openCompletion();
  mockedApi.getWorkOrder.mockRejectedValueOnce(new Error('Offline'));
  fireEvent.click(form.getByRole('button', { name: 'Complete' }));
  expect(await within(popup).findByRole('alert')).toHaveTextContent('Displayed operations may be out of date');
  expect(within(popup).getByRole('button', { name: 'Complete operation 10: Cut' })).toBeDisabled();
  expect(screen.getAllByRole('dialog')).toHaveLength(1);
  job.operations[0].status = 'complete';
  fireEvent.click(within(popup).getByRole('button', { name: 'Retry' }));
  expect(await within(popup).findByText('1 / 2 operations complete')).toBeInTheDocument();
  expect(mockedApi.completeWOOperation).toHaveBeenCalledTimes(1);
});

it('closes only the quantity form on Escape and leaves operations open', async () => {
  renderList();
  await openPopup();
  await openCompletion();
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(screen.getAllByRole('dialog')).toHaveLength(1);
  expect(screen.getByRole('dialog', { name: 'Operations for WO-0042' })).toBeInTheDocument();
});
