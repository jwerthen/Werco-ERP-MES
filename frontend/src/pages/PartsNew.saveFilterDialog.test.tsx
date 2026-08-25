/**
 * Parts (PartsNew) — naming a saved filter now goes through the shared
 * InputDialog instead of the native window.prompt().
 *
 * Covers: "Save Filter" opens the dialog, submit persists the filter under the
 * entered trimmed name (localStorage + the Saved chip row render it), and
 * cancel persists nothing.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
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
  fireEvent.click(await screen.findByRole('button', { name: /save filter/i }));
  return await screen.findByRole('dialog');
}

beforeEach(() => {
  jest.clearAllMocks();
  window.localStorage.clear();
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
