/**
 * PlatformOverview cockpit — company-row navigation + modal-state-reset regression.
 *
 * The platform overview was reworked into the instrument-panel cockpit: a
 * MiniStat strip of platform totals over a CockpitPanel that lists every
 * company as a clickable row. Clicking a row switches the active company
 * (CompanyContext.switchCompany) then redirects.
 *
 * The shared <Modal> keeps CreateCompanyModal mounted while closed, so the
 * component resets its form state on close (PlatformOverview.tsx useEffect on
 * `open`). This test locks that fix: type into a field, close, reopen, assert
 * the field comes back empty — guarding against the previous-credentials leak.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import PlatformOverview from './PlatformOverview';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getPlatformOverview: jest.fn(),
    createCompany: jest.fn(),
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

test('Add Company modal resets its form state on close (no stale value on reopen)', async () => {
  renderPage();
  await screen.findByRole('button', { name: /Acme Aero/i });

  // Labels aren't htmlFor-associated, so grab the Company Name textbox by role.
  // It's the only text <input> in the dialog (the rest are email/password).
  const getNameInput = () =>
    within(screen.getByRole('dialog')).getAllByRole('textbox')[0] as HTMLInputElement;

  // Open the modal and type into the Company Name field.
  fireEvent.click(screen.getByRole('button', { name: 'Add Company' }));
  await screen.findByRole('dialog');
  const nameInput = getNameInput();
  fireEvent.change(nameInput, { target: { value: 'Typed Co' } });
  expect(getNameInput().value).toBe('Typed Co');

  // Close via Cancel — Modal unmounts its content while closed.
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

  // Reopen — the field must be empty (state reset on close).
  fireEvent.click(screen.getByRole('button', { name: 'Add Company' }));
  await screen.findByRole('dialog');
  expect(getNameInput().value).toBe('');
});
