import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { OperationalQueue } from './ActionInbox';
import api from '../services/api';
import { OperationalInboxItem, OperationalInboxResponse } from '../types/operationsInbox';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getOperationalInbox: jest.fn(), updateOperationalInbox: jest.fn() },
}));
const mockApi = api as jest.Mocked<typeof api>;
const item = (override: Partial<OperationalInboxItem> = {}): OperationalInboxItem => ({
  key: 'blocker:5',
  source_kind: 'blocker',
  source_id: 5,
  occurrence: 'a'.repeat(64),
  title: 'Missing plate for WO-52',
  detail: 'Material not in rack.',
  severity: 'high',
  href: '/work-orders/52',
  suggested_action: 'Review material blocker',
  owner_id: null,
  owner_name: null,
  next_action: '',
  acknowledged: false,
  snoozed_until: null,
  version: 0,
  can_manage: true,
  ...override,
});
const result = (items: OperationalInboxItem[] = [item()]): OperationalInboxResponse => ({
  items,
  assignees: [{ id: 1, name: 'Alex Operator', sources: ['blocker'] }],
  checked_at: '2026-09-07T12:00:00Z',
  truncated_sources: [],
});
const mount = () =>
  render(
    <MemoryRouter>
      <OperationalQueue scope="1:1" />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.resetAllMocks();
  localStorage.clear();
  mockApi.getOperationalInbox.mockResolvedValue(result());
});

it('uses live issue identities independently of old category dismissals, with Mine and Unassigned filters', async () => {
  localStorage.setItem('actionInboxDismissed:1:1', JSON.stringify(['master-data:shortages', 'blocker:5']));
  mockApi.getOperationalInbox.mockResolvedValue(
    result([
      item(),
      item({
        key: 'quality_ncr:3',
        source_kind: 'quality_ncr',
        source_id: 3,
        title: 'NCR needs review',
        owner_id: 1,
        owner_name: 'Alex Operator',
        href: '/quality?tab=ncr&ncr=3',
      }),
    ])
  );
  mount();
  expect(await screen.findByRole('article', { name: 'Missing plate for WO-52' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Mine' }));
  expect(screen.queryByRole('article', { name: 'Missing plate for WO-52' })).not.toBeInTheDocument();
  expect(screen.getByRole('article', { name: 'NCR needs review' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Unassigned' }));
  expect(screen.getByRole('article', { name: 'Missing plate for WO-52' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument();
});

it('retains acknowledged issues and moves snoozed issues into a recoverable view only after success', async () => {
  mount();
  await screen.findByRole('article');
  mockApi.updateOperationalInbox.mockResolvedValueOnce(item({ acknowledged: true, version: 1 }));
  fireEvent.click(screen.getByRole('button', { name: 'Acknowledge' }));
  expect(await screen.findByText('Acknowledged · issue remains active')).toBeVisible();
  expect(screen.getByRole('article')).toBeVisible();
  mockApi.updateOperationalInbox.mockResolvedValueOnce(
    item({ acknowledged: true, version: 2, snoozed_until: '2026-09-08T12:00:00Z' })
  );
  fireEvent.click(screen.getByRole('button', { name: 'Snooze 24 hours' }));
  await waitFor(() => expect(screen.queryByRole('article')).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: 'Snoozed' }));
  expect(screen.getByRole('article')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Return to active' })).toBeVisible();
  expect(mockApi.updateOperationalInbox).toHaveBeenLastCalledWith(
    'blocker',
    5,
    expect.objectContaining({ expected_version: 1, snooze_hours: 24 })
  );
});

it('saves a chosen owner and next action and prevents duplicate submissions while pending', async () => {
  const user = userEvent.setup();
  let resolve!: (saved: OperationalInboxItem) => void;
  mockApi.updateOperationalInbox.mockImplementation(
    () =>
      new Promise(done => {
        resolve = done;
      })
  );
  mount();
  await screen.findByRole('article');
  await user.click(screen.getByRole('button', { name: 'Assign / next action' }));
  const dialog = screen.getByRole('dialog', { name: 'Assign next action' });
  await user.type(within(dialog).getByRole('combobox', { name: 'Owner' }), 'Alex');
  await user.click(screen.getByRole('option', { name: 'Alex Operator' }));
  await user.type(within(dialog).getByLabelText('Next action'), 'Check receiving rack');
  await user.click(within(dialog).getByRole('button', { name: 'Save action' }));
  expect(within(dialog).getByRole('button', { name: 'Saving…' })).toBeDisabled();
  expect(mockApi.updateOperationalInbox).toHaveBeenCalledTimes(1);
  expect(mockApi.updateOperationalInbox).toHaveBeenCalledWith(
    'blocker',
    5,
    expect.objectContaining({
      owner_id: 1,
      next_action: 'Check receiving rack',
      expected_version: 0,
      occurrence: 'a'.repeat(64),
    })
  );
  await act(async () =>
    resolve(item({ owner_id: 1, owner_name: 'Alex Operator', next_action: 'Check receiving rack', version: 1 }))
  );
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByText('Check receiving rack')).toBeVisible();
});

it('keeps modal input after a rejected stale update and keeps last verified rows after failed refresh', async () => {
  mockApi.updateOperationalInbox.mockRejectedValue({
    response: { data: { detail: 'Issue changed. Refresh before updating its action.' } },
  });
  mount();
  await screen.findByRole('article');
  fireEvent.click(screen.getByRole('button', { name: 'Assign / next action' }));
  fireEvent.change(screen.getByLabelText('Next action'), { target: { value: 'Retain this plan' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save action' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Issue changed');
  expect(screen.getByLabelText('Next action')).toHaveValue('Retain this plan');
  jest.spyOn(window, 'confirm').mockReturnValue(true);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  mockApi.getOperationalInbox.mockRejectedValueOnce(new Error('offline'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh operations' }));
  await screen.findByText(/Operational issues could not be refreshed/);
  expect(screen.getByRole('article')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Acknowledge' })).toBeDisabled();
  jest.restoreAllMocks();
});

it('exposes complete loaded pagination and provides no mutation controls to view-only roles', async () => {
  mockApi.getOperationalInbox.mockResolvedValue(
    result(
      Array.from({ length: 21 }, (_, index) =>
        item({ key: `blocker:${index}`, source_id: index, title: `Issue ${index}`, can_manage: false })
      )
    )
  );
  mount();
  await screen.findByText('Issue 0');
  expect(screen.getAllByRole('article')).toHaveLength(20);
  expect(screen.queryByRole('button', { name: 'Acknowledge' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Next issues' }));
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.getByText('Issue 20')).toBeVisible();
  fireEvent.change(screen.getByRole('textbox', { name: 'Search operational issues' }), {
    target: { value: 'Issue 0' },
  });
  expect(screen.getByText('Issue 0')).toBeVisible();
});

it('does not show a prior company response after the authenticated scope changes', async () => {
  let resolve!: (data: OperationalInboxResponse) => void;
  mockApi.getOperationalInbox.mockImplementationOnce(
    () =>
      new Promise(done => {
        resolve = done;
      })
  );
  const view = render(
    <MemoryRouter>
      <OperationalQueue scope="1:1" />
    </MemoryRouter>
  );
  mockApi.getOperationalInbox.mockResolvedValueOnce(result([]));
  view.rerender(
    <MemoryRouter>
      <OperationalQueue scope="2:2" />
    </MemoryRouter>
  );
  await screen.findByText('No operational issues match this view.');
  await act(async () => resolve(result([item({ title: 'Previous company private issue' })])));
  expect(screen.queryByText('Previous company private issue')).not.toBeInTheDocument();
});
