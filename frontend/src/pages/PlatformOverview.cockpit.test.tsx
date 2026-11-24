/**
 * PlatformOverview cockpit — company-row navigation, and no company-creation door.
 *
 * The platform overview was reworked into the instrument-panel cockpit: a
 * MiniStat strip of platform totals over a CockpitPanel that lists every
 * company as a clickable row. Clicking a row switches the active company
 * (CompanyContext.switchCompany) then redirects.
 *
 * Werco is the only company on this system (owner decision 2026-11-23), so the
 * page no longer offers an Add Company button or modal: the platform-admin
 * `POST /platform/companies` route it called was deleted.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import PlatformOverview from './PlatformOverview';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getPlatformOverview: jest.fn(),
  },
}));

const mockSwitchCompany = jest.fn();
jest.mock('../context/CompanyContext', () => ({
  useCompany: () => ({ switchCompany: mockSwitchCompany }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const overview = {
  total_companies: 2,
  total_active_users: 9,
  total_active_work_orders: 14,
  companies: [
    { id: 1, name: 'Acme Aero', slug: 'acme-aero', active_users: 5, active_work_orders: 8 },
    { id: 2, name: 'Beta Machining', slug: 'beta-machining', active_users: 4, active_work_orders: 6 },
  ],
};

const renderPage = () => render(<MemoryRouter><PlatformOverview /></MemoryRouter>);

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getPlatformOverview.mockResolvedValue(overview as any);
  mockSwitchCompany.mockResolvedValue(undefined);
});

test('renders a clickable row per company and switches company on click', async () => {
  renderPage();

  // Rows render after the initial load.
  const acmeRow = await screen.findByRole('button', { name: /Acme Aero/i });
  expect(screen.getByRole('button', { name: /Beta Machining/i })).toBeInTheDocument();

  fireEvent.click(acmeRow);

  await waitFor(() => expect(mockSwitchCompany).toHaveBeenCalledWith(1));
});

test('offers no way to create a company, even when the list is empty', async () => {
  renderPage();
  await screen.findByRole('button', { name: /Acme Aero/i });
  expect(screen.queryByRole('button', { name: /add company|create company/i })).toBeNull();
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('empty company list renders the empty state with no create action', async () => {
  mockedApi.getPlatformOverview.mockResolvedValue({
    total_companies: 0,
    total_active_users: 0,
    total_active_work_orders: 0,
    companies: [],
  } as any);
  renderPage();

  expect(await screen.findByText('No companies yet')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /add company|create company/i })).toBeNull();
});
