/**
 * Parts (PartsNew) — naming a saved filter now goes through the shared
 * InputDialog instead of the native window.prompt().
 *
 * Covers: "Save Filter" opens the dialog, submit persists the filter under the
 * entered trimmed name (localStorage + the Saved chip row render it), and
 * cancel persists nothing.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { Part } from '../types';
import PartsNew from './PartsNew';
import { ToastProvider } from '../components/ui';

// PartsNew reads the signed-in role to decide whether to offer "New Part"
// (POST /parts is admin/manager/supervisor). This suite is about the saved-filter
// dialog, so it just needs a role that renders the page normally.
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    listWorkspaceRecords: jest.fn(), saveWorkspaceRecord: jest.fn(), listTeamWorkspaceRecords: jest.fn(),
    getParts: jest.fn(),
    getBOMs: jest.fn(),
    getCustomerNames: jest.fn(),
    createPart: jest.fn(),
    deletePart: jest.fn(),
    uploadDocument: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const SAVED_FILTERS_KEY = 'werco.parts.savedFilters.v1';

function renderParts() {
  return render(
    <ToastProvider>
      <MemoryRouter>
        <PartsNew />
      </MemoryRouter>
    </ToastProvider>
  );
}

async function openSaveFilterDialog() {
  renderParts();
  fireEvent.click(await screen.findByRole('button', { name: /save on device/i }));
  return await screen.findByRole('dialog');
}

beforeEach(() => {
  jest.clearAllMocks();
  window.localStorage.clear();
  sessionStorage.clear();
  mockedApi.listWorkspaceRecords.mockResolvedValue([]);
  mockedApi.listTeamWorkspaceRecords.mockResolvedValue({ items: [], can_manage: true });
  mockedApi.getParts.mockResolvedValue([]);
  mockedApi.getBOMs.mockResolvedValue([]);
  mockedApi.getCustomerNames.mockResolvedValue([]);
});

describe('PartsNew save-filter InputDialog', () => {
  it('opens the naming dialog without persisting anything', async () => {
    await openSaveFilterDialog();

    expect(screen.getByText('Save Parts Filter')).toBeInTheDocument();
    expect(screen.getByLabelText(/filter name/i)).toHaveValue('Parts filter');
    expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull();
  });

  it('submit persists the filter under the entered trimmed name and closes', async () => {
    await openSaveFilterDialog();

    fireEvent.change(screen.getByLabelText(/filter name/i), { target: { value: '  Critical turned parts  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    const stored = JSON.parse(window.localStorage.getItem(SAVED_FILTERS_KEY) ?? '[]');
    expect(stored).toHaveLength(1);
    expect(stored[0].name).toBe('Critical turned parts');

    // The Saved chip row renders the new filter (exact name — the chip's
    // delete button is named "Delete Critical turned parts").
    expect(await screen.findByRole('button', { name: 'Critical turned parts' })).toBeInTheDocument();
  });

  it('cancel closes the dialog and persists nothing', async () => {
    await openSaveFilterDialog();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull();
  });
});


test('legacy device filter imports privately with its own values and remains on the device', async () => {
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  const legacy = { id: 'old', name: 'Device fixture', search: 'legacy search', typeFilter: 'manufactured', statusFilter: 'active', showBOMComponents: true, viewMode: 'grid', createdAt: '2026-01-01' };
  localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify([legacy]));
  mockedApi.saveWorkspaceRecord.mockImplementation(async (namespace, key, value) => ({ namespace, key, ...value, version: 1, updated_at: '2026-09-07' }));
  renderParts();
  const button = await screen.findByRole('button', { name: 'Import Device fixture into my private views' });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() => expect(mockedApi.saveWorkspaceRecord).toHaveBeenCalledWith('parts', expect.any(String), expect.objectContaining({ kind: 'view', data: expect.objectContaining({ filters: { search: 'legacy search', typeFilter: 'manufactured', statusFilter: 'active', showBOMComponents: '1', viewMode: 'grid' } }) })));
  expect(JSON.parse(localStorage.getItem(SAVED_FILTERS_KEY)!)).toEqual([legacy]);
  sessionStorage.clear();
});


test('applying a different saved Parts view keeps the selector and discards a late old filter response', async () => {
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  const saved = (key: string, type: string) => ({ namespace: 'parts', key, kind: 'view' as const, name: key, version: 1, updated_at: '2026-09-07', data: { table: 'catalog', layout: {}, filters: { typeFilter: type, search: '', statusFilter: '', showBOMComponents: '1', viewMode: 'table' } } });
  mockedApi.listWorkspaceRecords.mockResolvedValue([saved('Purchased view', 'purchased'), saved('Made view', 'manufactured')]);
  let finishOld!: (rows: any[]) => void;
  const made = { id: 90, part_number: 'MADE-LATEST', name: 'Latest manufactured fixture', part_type: 'manufactured', status: 'active', revision: 'A', standard_cost: 0 } as Part;
  mockedApi.getParts.mockImplementation(async params => {
    if (params?.part_type === 'purchased') return new Promise(resolve => { finishOld = resolve; });
    if (params?.part_type === 'manufactured') return [made];
    return [];
  });
  renderParts();
  const selector = await screen.findByRole('combobox', { name: 'Saved views' });
  await waitFor(() => expect(selector).toBeEnabled());
  fireEvent.change(selector, { target: { value: 'Purchased view' } });
  fireEvent.click(screen.getByRole('button', { name: 'Apply view' }));
  await waitFor(() => expect(mockedApi.getParts).toHaveBeenCalledWith(expect.objectContaining({ part_type: 'purchased' })));
  expect(selector).toBeInTheDocument();
  expect(selector).toHaveValue('Purchased view');
  fireEvent.change(selector, { target: { value: 'Made view' } });
  fireEvent.click(screen.getByRole('button', { name: 'Apply view' }));
  await screen.findByText('MADE-LATEST');
  await act(async () => finishOld([{ ...made, id: 91, part_number: 'PURCHASED-STALE', part_type: 'purchased' }]));
  expect(screen.getByText('MADE-LATEST')).toBeInTheDocument();
  expect(screen.queryByText('PURCHASED-STALE')).not.toBeInTheDocument();
  expect(selector).toHaveValue('Made view');
  sessionStorage.clear();
});
