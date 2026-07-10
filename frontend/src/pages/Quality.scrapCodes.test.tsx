/**
 * Quality "Scrap Codes" tab (Lean Phase 1 / issue #88) — the company scrap
 * vocabulary behind every scrap picker.
 *
 * Locks the happy paths:
 *  - opening the tab lazily loads codes WITH include_inactive (retired codes
 *    stay visible for reactivation) and renders them,
 *  - create: the modal posts the trimmed/uppercased payload and reloads,
 *  - deactivate: retirement is is_active=false via update (deactivate-not-
 *    delete — there is no delete action), non-optimistic (reflects the server).
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Quality from './Quality';

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => true, canAny: () => true }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    createScrapReasonCode: jest.fn(),
    updateScrapReasonCode: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const ACTIVE_CODE = {
  id: 7,
  code: 'OT',
  name: 'Out of tolerance',
  category: 'operator',
  description: 'Dimensional',
  is_active: true,
  display_order: 1,
};

const INACTIVE_CODE = {
  id: 9,
  code: 'OLD',
  name: 'Retired reason',
  category: 'other',
  description: null,
  is_active: false,
  display_order: 2,
};

function renderQuality() {
  return render(
    <MemoryRouter initialEntries={['/quality']}>
      <Quality />
    </MemoryRouter>
  );
}

async function openScrapCodesTab() {
  renderQuality();
  fireEvent.click(await screen.findByRole('button', { name: /scrap codes/i }));
  await waitFor(() => expect(mockedApi.getScrapReasonCodes).toHaveBeenCalled());
}

describe('Quality — Scrap Codes tab', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getNCRs.mockResolvedValue([]);
    mockedApi.getCARs.mockResolvedValue([]);
    mockedApi.getFAIs.mockResolvedValue([]);
    mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 0, open_cars: 0, pending_fais: 0 });
    mockedApi.getParts.mockResolvedValue([]);
    mockedApi.getScrapReasonCodes.mockResolvedValue([ACTIVE_CODE, INACTIVE_CODE] as any);
  });

  it('lazily loads codes (including inactive) when the tab opens and renders them', async () => {
    await openScrapCodesTab();

    expect(mockedApi.getScrapReasonCodes).toHaveBeenCalledWith({ include_inactive: true });
    expect(await screen.findAllByText('OT')).not.toHaveLength(0);
    expect(screen.getAllByText('Out of tolerance').length).toBeGreaterThan(0);
    // The retired code stays visible with its inactive badge (reactivation path).
    expect(screen.getAllByText('OLD').length).toBeGreaterThan(0);
    expect(screen.getAllByText('inactive').length).toBeGreaterThan(0);
    // Deactivate-not-delete: the action vocabulary has no "Delete".
    expect(screen.getAllByRole('button', { name: 'Deactivate' }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: 'Reactivate' }).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /^delete$/i })).not.toBeInTheDocument();
  });

  it('creates a code from the modal with the normalized payload, then reloads', async () => {
    mockedApi.createScrapReasonCode.mockResolvedValue({ ...ACTIVE_CODE, id: 11, code: 'MAT' } as any);
    await openScrapCodesTab();

    fireEvent.click(screen.getByRole('button', { name: /new scrap code/i }));
    const dialog = await screen.findByRole('dialog');

    // Code input uppercases as you type; name is stored as typed.
    fireEvent.change(within(dialog).getByLabelText(/code/i), { target: { value: 'mat' } });
    fireEvent.change(within(dialog).getByLabelText(/name/i), { target: { value: 'Material defect' } });
    fireEvent.change(within(dialog).getByLabelText(/category/i), { target: { value: 'material' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /create code/i }));

    await waitFor(() =>
      expect(mockedApi.createScrapReasonCode).toHaveBeenCalledWith({
        code: 'MAT',
        name: 'Material defect',
        category: 'material',
        description: null,
        display_order: 0,
      })
    );
    // The list is re-fetched after the save (tab open + post-create reload).
    await waitFor(() => expect(mockedApi.getScrapReasonCodes).toHaveBeenCalledTimes(2));
  });

  it('deactivates via is_active=false and reflects the server response', async () => {
    mockedApi.updateScrapReasonCode.mockResolvedValue({ ...ACTIVE_CODE, is_active: false } as any);
    await openScrapCodesTab();

    // After the toggle the reload returns the code as inactive.
    mockedApi.getScrapReasonCodes.mockResolvedValue([
      { ...ACTIVE_CODE, is_active: false },
      INACTIVE_CODE,
    ] as any);

    fireEvent.click(screen.getAllByRole('button', { name: 'Deactivate' })[0]);

    await waitFor(() => expect(mockedApi.updateScrapReasonCode).toHaveBeenCalledWith(7, { is_active: false }));
    await waitFor(() => expect(mockedApi.getScrapReasonCodes).toHaveBeenCalledTimes(2));
    // Non-optimistic: the row now renders the server's inactive state (both codes retired).
    await waitFor(() => expect(screen.queryAllByRole('button', { name: 'Deactivate' })).toHaveLength(0));
    expect(screen.getAllByRole('button', { name: 'Reactivate' }).length).toBeGreaterThan(0);
  });
});
