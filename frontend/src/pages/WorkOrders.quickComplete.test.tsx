import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ToastProvider } from '../components/ui/Toast';
import api from '../services/api';
import { workOrderBrowseFixture } from '../testUtils/workOrderBrowseFixture';
import WorkOrders from './WorkOrders';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    browseWorkOrders: jest.fn(),
    getWorkOrder: jest.fn(),
    completeWorkOrder: jest.fn(),
  },
}));

let mockRole = 'manager';
let mockSuperuser = false;
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: mockRole, is_superuser: mockSuperuser } }),
}));
jest.mock('../hooks/useScrapReasonCodes', () => ({ useScrapReasonCodes: () => ({ codes: [] }) }));
jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({ getAccessToken: () => null, buildWsUrl: () => 'ws://localhost/test' }));

const mockedApi = api as jest.Mocked<typeof api>;
const summary = {
  id: 42,
  work_order_number: 'WO-0042',
  version: 7,
  part_id: 10,
  part_number: 'BRACKET-01',
  part_name: 'Mounting bracket',
  part_type: 'manufactured',
  work_order_type: 'production',
  status: 'in_progress',
  priority: 2,
  quantity_ordered: 10,
  quantity_complete: 3,
  quantity_scrapped: 0,
  customer_name: 'Example customer',
};
let rows: typeof summary[];

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

function renderList(query = '?status=in_progress') {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[`/work-orders${query}`]}>
        <WorkOrders />
        <Location />
      </MemoryRouter>
    </ToastProvider>
  );
}

async function openCompletion() {
  const trigger = await screen.findByRole('button', { name: 'Quick complete WO-0042' });
  if (trigger.closest('table')) expect(trigger).toHaveAttribute('title', 'Quick complete');
  fireEvent.click(trigger);
  return screen.findByRole('dialog', { name: 'Complete work order WO-0042' });
}

beforeEach(() => {
  jest.resetAllMocks();
  mockRole = 'manager';
  mockSuperuser = false;
  window.innerWidth = 1440;
  rows = [{ ...summary }];
  mockedApi.browseWorkOrders.mockImplementation(async params => workOrderBrowseFixture(rows, params));
  mockedApi.completeWorkOrder.mockResolvedValue({});
});

it.each(['', '&group=customer', '&group=part', '&group=status'])(
  'quick completes from the list%s without loading or navigating to the work order',
  async group => {
    renderList(`?status=in_progress${group}`);
    const dialog = await openCompletion();
    expect(within(dialog).getByLabelText(/Quantity completed/)).toHaveValue(10);
    expect(within(dialog).getByLabelText('Quantity scrapped')).toHaveValue(0);
    expect(within(dialog).getByText(/completes all remaining operations/i)).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Quantity scrapped')).toHaveAccessibleDescription(
      'Enter total scrap to update it; leave 0 to keep recorded scrap.'
    );
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(mockedApi.getWorkOrder).not.toHaveBeenCalled();
    expect(mockedApi.completeWorkOrder).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Current location')).toHaveTextContent(`/work-orders?status=in_progress${group}`);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
    expect(await screen.findByText('WO-0042 completed')).toBeInTheDocument();
    expect(mockedApi.completeWorkOrder).toHaveBeenCalledWith(42, 10, null, undefined, undefined);
    expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.browseWorkOrders).toHaveBeenCalledTimes(2);
    expect(mockedApi.getWorkOrder).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Current location')).toHaveTextContent(`/work-orders?status=in_progress${group}`);
  }
);

it('offers the same quick-complete flow from a mobile card', async () => {
  window.innerWidth = 390;
  renderList();
  const dialog = await openCompletion();
  expect(within(dialog).getByLabelText(/Quantity completed/)).toHaveValue(10);
  expect(mockedApi.getWorkOrder).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
  expect(await screen.findByText('WO-0042 completed')).toBeInTheDocument();
  expect(mockedApi.completeWorkOrder).toHaveBeenCalledWith(42, 10, null, undefined, undefined);
  expect(screen.getByLabelText('Current location')).toHaveTextContent('/work-orders?status=in_progress');
});

it.each(['admin', 'manager', 'supervisor', 'quality', 'platform_admin'])(
  'offers quick completion to %s',
  async role => {
    mockRole = role;
    renderList();
    expect(await screen.findByRole('button', { name: 'Quick complete WO-0042' })).toBeEnabled();
  }
);

it('offers quick completion to a superuser with an otherwise read-only role', async () => {
  mockRole = 'viewer';
  mockSuperuser = true;
  renderList();
  expect(await screen.findByRole('button', { name: 'Quick complete WO-0042' })).toBeEnabled();
});

it.each(['operator', 'shipping', 'viewer'])('hides quick completion from %s', async role => {
  mockRole = role;
  renderList();
  await screen.findByRole('link', { name: 'WO-0042' });
  expect(screen.queryByRole('button', { name: 'Quick complete WO-0042' })).not.toBeInTheDocument();
});

it('offers quick completion only for in-progress orders', async () => {
  const inactive = ['draft', 'released', 'on_hold', 'complete', 'closed', 'cancelled'];
  rows.push(...inactive.map((status, index) => ({
    ...summary,
    id: 100 + index,
    work_order_number: `WO-${status}`,
    status,
  })));
  renderList('');
  expect(await screen.findByRole('button', { name: 'Quick complete WO-0042' })).toBeEnabled();
  for (const status of inactive) {
    expect(screen.getByRole('link', { name: `WO-${status}` })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: `Quick complete WO-${status}` })).not.toBeInTheDocument();
  }
});

it.each(['Cancel', 'Escape'])('%s dismisses without completing or reading the work order', async method => {
  renderList();
  const dialog = await openCompletion();
  fireEvent.change(within(dialog).getByLabelText(/Quantity completed/), { target: { value: '8' } });
  if (method === 'Cancel') fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  else fireEvent.keyDown(window, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(mockedApi.completeWorkOrder).not.toHaveBeenCalled();
  expect(mockedApi.getWorkOrder).not.toHaveBeenCalled();
});

it('requires a scrap reason and forwards the entered completion quantities', async () => {
  renderList();
  const dialog = await openCompletion();
  const form = within(dialog);
  fireEvent.change(form.getByLabelText(/Quantity completed/), { target: { value: '8' } });
  fireEvent.change(form.getByLabelText('Quantity scrapped'), { target: { value: '2' } });
  expect(form.getByRole('button', { name: 'Complete' })).toBeDisabled();
  expect(mockedApi.completeWorkOrder).not.toHaveBeenCalled();
  fireEvent.click(form.getByRole('combobox', { name: 'Scrap reason' }));
  fireEvent.mouseDown(screen.getByRole('option', { name: 'Out of tolerance' }));
  fireEvent.click(form.getByRole('button', { name: 'Complete' }));
  expect(await screen.findByText('WO-0042 completed')).toBeInTheDocument();
  expect(mockedApi.completeWorkOrder).toHaveBeenCalledWith(42, 8, 2, 'Out of tolerance', undefined);
});

it('prevents repeat submission and dismissal until completion and list refresh finish', async () => {
  let complete!: (value: unknown) => void;
  let refresh!: (value: ReturnType<typeof workOrderBrowseFixture>) => void;
  mockedApi.completeWorkOrder.mockReturnValueOnce(new Promise(resolve => { complete = resolve; }));
  renderList();
  const dialog = await openCompletion();
  mockedApi.browseWorkOrders.mockReturnValueOnce(new Promise(resolve => { refresh = resolve; }));

  fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
  const pendingButton = within(dialog).getByRole('button', { name: /Completing/ });
  expect(pendingButton).toBeDisabled();
  expect(within(dialog).getByLabelText(/Quantity completed/)).toBeDisabled();
  expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();
  fireEvent.click(pendingButton);
  fireEvent.submit(dialog.querySelector('form')!);
  fireEvent.keyDown(window, { key: 'Escape' });
  fireEvent.click(dialog.parentElement!);
  expect(screen.getByRole('dialog', { name: 'Complete work order WO-0042' })).toBeInTheDocument();
  expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);
  expect(mockedApi.browseWorkOrders).toHaveBeenCalledTimes(1);
  expect(screen.queryByText('WO-0042 completed')).not.toBeInTheDocument();

  await act(async () => complete({}));
  await waitFor(() => expect(mockedApi.browseWorkOrders).toHaveBeenCalledTimes(2));
  expect(screen.queryByText('WO-0042 completed')).not.toBeInTheDocument();
  await act(async () => refresh(workOrderBrowseFixture([])));
  expect(await screen.findByText('WO-0042 completed')).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(await screen.findByText('No work orders found')).toBeInTheDocument();
  expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);
});

it('keeps a server refusal and entered quantities in the dialog for a safe retry', async () => {
  mockedApi.completeWorkOrder.mockRejectedValueOnce({
    response: { status: 409, data: { detail: 'Clear the operation hold before completing.' } },
  });
  renderList();
  const dialog = await openCompletion();
  const form = within(dialog);
  fireEvent.change(form.getByLabelText(/Quantity completed/), { target: { value: '8' } });
  fireEvent.click(form.getByRole('button', { name: 'Complete' }));
  expect(await form.findByRole('alert')).toHaveTextContent('Clear the operation hold before completing.');
  expect(form.getByLabelText(/Quantity completed/)).toHaveValue(8);
  expect(form.getByRole('button', { name: 'Complete' })).toBeEnabled();
  expect(mockedApi.browseWorkOrders).toHaveBeenCalledTimes(1);
  expect(screen.queryByText('WO-0042 completed')).not.toBeInTheDocument();
  fireEvent.click(form.getByRole('button', { name: 'Complete' }));
  expect(await screen.findByText('WO-0042 completed')).toBeInTheDocument();
  expect(mockedApi.completeWorkOrder).toHaveBeenNthCalledWith(2, 42, 8, null, undefined, undefined);
  expect(mockedApi.getWorkOrder).not.toHaveBeenCalled();
});

it('retries only the list read when refreshing an accepted completion fails', async () => {
  renderList();
  const dialog = await openCompletion();
  mockedApi.browseWorkOrders.mockRejectedValueOnce(new Error('Offline'));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));

  expect(await screen.findByText('WO-0042 completed')).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  const warning = screen.getByRole('alert');
  expect(warning).toHaveTextContent('Displayed orders may be out of date.');
  rows = [];
  fireEvent.click(within(warning).getByRole('button', { name: 'Retry' }));
  expect(await screen.findByText('No work orders found')).toBeInTheDocument();
  expect(mockedApi.completeWorkOrder).toHaveBeenCalledTimes(1);
  expect(mockedApi.browseWorkOrders).toHaveBeenCalledTimes(3);
});

it('preserves the server notice when completion bypasses required step records', async () => {
  mockedApi.completeWorkOrder.mockResolvedValueOnce({
    steps_bypassed: { count: 1, steps: [{ operation: '10', step_id: 1, label: 'Measure slot', serials: [] }] },
  });
  renderList();
  const dialog = await openCompletion();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Complete' }));
  const notice = await screen.findByText('Completed with 1 step record bypassed: Measure slot');
  expect(notice.closest('[role="status"]')).not.toBeNull();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
