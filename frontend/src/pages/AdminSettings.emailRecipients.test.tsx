import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import AdminSettings from './AdminSettings';
import { ToastProvider } from '../components/ui/Toast';
import type { EmailRecipientsSettings } from '../types/emailRecipients';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getEmailRecipients: jest.fn(), updateEmailRecipients: jest.fn() },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const data: EmailRecipientsSettings = {
  users: [
    { id: 1, name: 'Ashley Werthen', email: 'awerthen@wercomfg.com', is_active: true, email_deliverable: true },
    { id: 2, name: 'Jon Werthen', email: 'jwerthen@wercomfg.com', is_active: true, email_deliverable: true },
    { id: 3, name: 'Jon Werthen Jr.', email: 'jmw@wercomfg.com', is_active: true, email_deliverable: true },
    { id: 4, name: 'Another User', email: 'another@example.test', is_active: true, email_deliverable: true },
    { id: 5, name: 'Badge Only', email: 'badge@users.werco.com', is_active: true, email_deliverable: false },
  ],
  events: [
    {
      event_key: 'wo.completed',
      label: 'Work order completed',
      description: 'A work order reached completion.',
      category: 'Production',
      user_ids: [1, 2, 3],
      is_custom: false,
      missing_default_emails: [],
    },
    {
      event_key: 'receipt.created',
      label: 'Material received',
      description: 'Material received against a purchase order.',
      category: 'Purchasing & Inventory',
      user_ids: [1, 2, 3],
      is_custom: false,
      missing_default_emails: [],
    },
    {
      event_key: 'ncr.created',
      label: 'NCR created',
      description: 'A non-conformance report was created.',
      category: 'Quality',
      user_ids: null,
      is_custom: false,
      missing_default_emails: [],
    },
  ],
};

function renderTab() {
  return render(
    <MemoryRouter initialEntries={['/admin/settings?tab=emails']}>
      <ToastProvider>
        <AdminSettings />
      </ToastProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getEmailRecipients.mockResolvedValue(JSON.parse(JSON.stringify(data)));
  mockedApi.updateEmailRecipients.mockImplementation(async (key, ids) => ({
    ...data,
    events: data.events.map(event => (event.event_key === key ? { ...event, user_ids: ids, is_custom: true } : event)),
  }));
});

it('opens from Admin Settings and checks exactly the three requested recipients', async () => {
  renderTab();
  expect(await screen.findByLabelText('Email type')).toHaveValue('wo.completed');
  expect(screen.getAllByRole('checkbox', { checked: true })).toHaveLength(3);
  expect(screen.getByRole('checkbox', { name: /Ashley Werthen/ })).toBeChecked();
  expect(screen.getByRole('checkbox', { name: /Jon Werthen \(/ })).toBeChecked();
  expect(screen.getByRole('checkbox', { name: /Jon Werthen Jr/ })).toBeChecked();
  expect(screen.getByRole('checkbox', { name: /Another User/ })).not.toBeChecked();
  expect(screen.getByRole('checkbox', { name: /Badge Only/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save recipients' })).toBeDisabled();
});

it('keeps unsaved selections separate for each email and saves only the current list', async () => {
  renderTab();
  await screen.findByLabelText('Email type');
  fireEvent.click(screen.getByRole('checkbox', { name: /Another User/ }));
  fireEvent.change(screen.getByLabelText('Email type'), { target: { value: 'receipt.created' } });
  expect(screen.getByRole('checkbox', { name: /Another User/ })).not.toBeChecked();
  fireEvent.click(screen.getByRole('checkbox', { name: /Jon Werthen Jr/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Save recipients' }));
  await waitFor(() => expect(mockedApi.updateEmailRecipients).toHaveBeenCalledWith('receipt.created', [1, 2]));
  await waitFor(() => expect(screen.getByLabelText('Email type')).toBeEnabled());
  fireEvent.change(screen.getByLabelText('Email type'), { target: { value: 'wo.completed' } });
  expect(screen.getByRole('checkbox', { name: /Another User/ })).toBeChecked();
});

it('supports disabling one email with an empty list', async () => {
  renderTab();
  await screen.findByLabelText('Email type');
  screen.getAllByRole('checkbox', { checked: true }).forEach(checkbox => fireEvent.click(checkbox));
  expect(screen.getByText(/Nobody selected/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Save recipients' }));
  await waitFor(() => expect(mockedApi.updateEmailRecipients).toHaveBeenCalledWith('wo.completed', []));
});

it('retains selections on a save failure and allows retry', async () => {
  mockedApi.updateEmailRecipients.mockRejectedValueOnce(new Error('offline'));
  renderTab();
  await screen.findByLabelText('Email type');
  fireEvent.click(screen.getByRole('checkbox', { name: /Another User/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Save recipients' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not save email recipients');
  expect(screen.getByRole('checkbox', { name: /Another User/ })).toBeChecked();
  fireEvent.click(screen.getByRole('button', { name: 'Save recipients' }));
  await waitFor(() => expect(mockedApi.updateEmailRecipients).toHaveBeenCalledTimes(2));
});

it('lets an admin replace an automatic audience with specific recipients', async () => {
  renderTab();
  await screen.findByLabelText('Email type');
  fireEvent.change(screen.getByLabelText('Email type'), { target: { value: 'ncr.created' } });
  fireEvent.click(screen.getByRole('button', { name: 'Choose specific recipients' }));
  fireEvent.click(screen.getByRole('checkbox', { name: /Ashley Werthen/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Save recipients' }));
  await waitFor(() => expect(mockedApi.updateEmailRecipients).toHaveBeenCalledWith('ncr.created', [1]));
});
