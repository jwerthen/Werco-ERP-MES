import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import BackgroundEmailActivity, { BackgroundEmailLog, backgroundEmailStatus } from './BackgroundEmailActivity';

let mockRole = 'manager';
let mockCompanyId = 1;
jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: mockCompanyId } }) }));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, role: mockRole } }) }));
jest.mock('../services/api', () => ({ __esModule: true, default: { getNotificationLogs: jest.fn() } }));
const list = api.getNotificationLogs as jest.Mock;
const row: BackgroundEmailLog = {
  id: 10,
  user_id: 1,
  subject: 'Work order released',
  sent: false,
  error: 'SMTP is not configured. No email was sent.',
  provider_status: 'skipped',
  sent_at: '2026-09-07T12:00:00Z',
};
beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'manager';
  mockCompanyId = 1;
  list.mockResolvedValue([row]);
});
function mount(url = '/notifications') {
  render(
    <MemoryRouter initialEntries={[url]}>
      <BackgroundEmailActivity />
    </MemoryRouter>
  );
}

test('shows durable failures with a self-scoped default and no resend action', async () => {
  mount();
  await screen.findByText('SMTP is not configured. No email was sent.');
  expect(screen.getByText('Skipped — not sent')).toBeVisible();
  expect(list).toHaveBeenCalledWith(expect.objectContaining({ mine_only: true, status: 'failed', channel: 'email' }));
  expect(screen.queryByRole('button', { name: /resend|retry/i })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Email activity scope'), { target: { value: 'company' } });
  await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ mine_only: false })));
});

test('operator cannot choose company scope and exact notification link selects the requested record', async () => {
  mockRole = 'operator';
  mount('/notifications?delivery=10');
  await screen.findByText('Skipped — not sent');
  expect(list).toHaveBeenCalledWith(expect.objectContaining({ mine_only: true, delivery_id: 10 }));
  expect(screen.queryByLabelText('Email activity scope')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Email status')).toBeDisabled();
});

test('failed refresh stays explicit and a stale request cannot replace current scope', async () => {
  let resolve!: (rows: BackgroundEmailLog[]) => void;
  list.mockImplementationOnce(
    () =>
      new Promise(done => {
        resolve = done;
      })
  );
  mount();
  fireEvent.change(screen.getByLabelText('Email activity scope'), { target: { value: 'company' } });
  await screen.findByText('Skipped — not sent');
  await act(async () => resolve([{ ...row, subject: 'Stale previous scope' }]));
  expect(screen.queryByText('Stale previous scope')).not.toBeInTheDocument();
  list.mockRejectedValueOnce(new Error('offline'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh email activity' }));
  await screen.findByRole('alert');
  expect(screen.queryByText('No recorded email failures in this scope.')).not.toBeInTheDocument();
});

test('labels legacy enqueue and stale sending truthfully instead of claiming delivery', () => {
  expect(backgroundEmailStatus({ ...row, sent: true, provider_status: null })).toBe(
    'Legacy queue record — delivery unverified'
  );
  expect(backgroundEmailStatus({ ...row, provider_status: 'sending', sent_at: '2020-01-01T00:00:00' })).toBe(
    'Outcome unknown — worker did not finish'
  );
  expect(backgroundEmailStatus({ ...row, provider_status: 'accepted' })).toBe('Accepted by mail server');
});

test('switching company refreshes the email scope and clears the previous company result', async () => {
  const view = () => (
    <MemoryRouter>
      <BackgroundEmailActivity />
    </MemoryRouter>
  );
  const { rerender } = render(view());
  await screen.findByText('Work order released');
  list.mockResolvedValueOnce([{ ...row, id: 20, subject: 'Current company notification' }]);
  mockCompanyId = 2;
  rerender(view());
  await screen.findByText('Current company notification');
  expect(screen.queryByText('Work order released')).not.toBeInTheDocument();
  expect(list).toHaveBeenCalledTimes(2);
});
