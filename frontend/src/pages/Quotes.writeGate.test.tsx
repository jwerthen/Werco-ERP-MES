let mockRole = 'viewer';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: mockRole, is_superuser: false } }),
}));
/**
 * Quotes — write controls follow the server's QUOTE_WRITE_ROLES gate.
 *
 * `PUT /quotes/{id}`, `POST /quotes/{id}/send` and `POST /quotes/{id}/convert` are
 * Admin / Manager / Supervisor on the server. The Quotes page is reachable by anyone
 * holding `purchasing:view` — which includes Viewer — so without this gate a Viewer
 * saw Send / Convert / Edit draft controls that the server then refused with 403.
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import Quotes from './Quotes';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQuotes: jest.fn(),
    getParts: jest.fn(),
    getQuote: jest.fn(),
    convertQuote: jest.fn(),
    sendQuote: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const draftQuote = {
  id: 7,
  quote_number: 'QUO-0007',
  revision: 'A',
  customer_name: 'Acme Aerospace',
  status: 'draft',
  quote_date: '2026-07-01',
  subtotal: 1000,
  total: 1000,
  lines: [],
};
const sentQuote = { ...draftQuote, id: 8, quote_number: 'QUO-0008', status: 'sent' };

const renderAt = (url: string) =>
  render(
    <MemoryRouter initialEntries={[url]}>
      <ToastProvider>
        <Routes>
          <Route path="/quotes" element={<Quotes />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getQuotes.mockResolvedValue([draftQuote, sentQuote] as any);
  mockedApi.getParts.mockResolvedValue([] as any);
});

test('a Viewer sees no Send, Convert or Edit draft control', async () => {
  mockRole = 'viewer';
  renderAt('/quotes?id=7');
  const panel = (await screen.findByText('Selected quote')).closest('div.mb-4') as HTMLElement;
  expect(within(panel).getByText(/QUO-0007/)).toBeInTheDocument();
  await waitFor(() => expect(mockedApi.getQuotes).toHaveBeenCalled());

  expect(screen.queryByRole('button', { name: 'Mark as sent' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Convert to Work Order' })).not.toBeInTheDocument();
  expect(within(panel).queryByRole('button', { name: 'Edit draft' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Email QUO-0007' })).not.toBeInTheDocument();
});

test('a Supervisor keeps every write control', async () => {
  mockRole = 'supervisor';
  renderAt('/quotes?id=7');
  const panel = (await screen.findByText('Selected quote')).closest('div.mb-4') as HTMLElement;

  expect(within(panel).getByRole('button', { name: 'Edit draft' })).toBeInTheDocument();
  expect((await screen.findAllByRole('button', { name: 'Mark as sent' })).length).toBeGreaterThan(0);
  expect((await screen.findAllByRole('button', { name: 'Convert to Work Order' })).length).toBeGreaterThan(0);
});

test('a platform admin gets Email as well as the write controls (same server role set)', async () => {
  // Quote email delivery admits platform_admin on the server; the Email control used to be an
  // inline role list that left it out, so a platform admin saw Send/Convert but not Email.
  mockRole = 'platform_admin';
  renderAt('/quotes?id=7');
  const panel = (await screen.findByText('Selected quote')).closest('div.mb-4') as HTMLElement;

  expect(within(panel).getByRole('button', { name: 'Edit draft' })).toBeInTheDocument();
  expect((await screen.findAllByRole('button', { name: 'Email QUO-0007' })).length).toBeGreaterThan(0);
  expect((await screen.findAllByRole('button', { name: 'Mark as sent' })).length).toBeGreaterThan(0);
});
