/**
 * WorkCenters — per-row Deactivate / Reactivate controls.
 *
 * Deactivation is server-GATED (PR #143: the API refuses with a 409 carrying
 * the live-work counts + remedy while queued/running work exists), so the flow
 * is deliberately NON-optimistic: the row action opens the shared warning
 * ConfirmDialog, the confirm awaits api.updateWorkCenter(id, { is_active:
 * false }), success reloads the list, and a refusal surfaces the server's
 * verbatim `detail` in an error toast with the row left untouched (no reload,
 * no local flip). Reactivation is allowed unconditionally server-side, so it
 * is a direct action (no confirm) with an in-flight guard + reload.
 *
 * The desktop table and the responsive mobile cards both mount in JSDOM, so
 * row-action lookups use the desktop icons' unique aria-labels
 * (`Deactivate <code>` / `Reactivate <code>`).
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkCenters from './WorkCenters';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getWorkCenterTypes: jest.fn(),
    updateWorkCenterStatus: jest.fn(),
    createWorkCenter: jest.fn(),
    updateWorkCenter: jest.fn(),
  },
}));

// Deactivate/reactivate are Admin-tier actions; mount as an admin. (The inline status
// select is separately role-gated — see WorkCenters.rbac.test.tsx.)
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const baseWc = {
  version: 0,
  description: '',
  availability_rate: 1,
  building: '',
  area: '',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  current_status: 'available',
};

const activeWc = { ...baseWc, id: 1, code: 'LASER-01', name: 'Trumpf Fiber', work_center_type: 'laser', is_active: true };
const inactiveWc = { ...baseWc, id: 2, code: 'WELD-09', name: 'Retired TIG Cell', work_center_type: 'welding', is_active: false };

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <WorkCenters />
      </ToastProvider>
    </MemoryRouter>
  );
}

describe('WorkCenters deactivate / reactivate controls', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue([activeWc, inactiveWc] as any);
    mockedApi.getWorkCenterTypes.mockResolvedValue({ types: ['laser', 'welding'] } as any);
  });

  it('deactivates through the warning confirm dialog and reloads on success', async () => {
    mockedApi.updateWorkCenter.mockResolvedValue({ ...activeWc, is_active: false } as any);
    renderPage();
    await screen.findAllByTestId('group-header');
    expect(mockedApi.getWorkCenters).toHaveBeenCalledTimes(1); // mount load

    // The active row offers Deactivate (not Reactivate); the inactive row the reverse.
    fireEvent.click(screen.getByLabelText('Deactivate LASER-01'));
    expect(screen.queryByLabelText('Reactivate LASER-01')).not.toBeInTheDocument();

    // The row control only OPENS the dialog — nothing is written yet.
    const dialog = await screen.findByRole('dialog');
    expect(mockedApi.updateWorkCenter).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/Deactivate LASER-01 — Trumpf Fiber\?/)).toBeInTheDocument();

    // Warning variant: the confirm button carries the amber override, not the
    // red danger chrome.
    const confirmButton = within(dialog).getByRole('button', { name: 'Deactivate' });
    expect(confirmButton.className).toContain('bg-amber-500');

    fireEvent.click(confirmButton);
    await waitFor(() => {
      expect(mockedApi.updateWorkCenter).toHaveBeenCalledWith(1, { is_active: false });
    });

    // Non-optimistic: success reflects the SERVER by reloading the list.
    await waitFor(() => expect(mockedApi.getWorkCenters).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByText('Deactivated LASER-01')).toBeInTheDocument();
  });

  it('surfaces a 409 refusal verbatim in an error toast and leaves the row active (no reload, no local flip)', async () => {
    // The exact production template from work_centers.py::_live_work_refusal,
    // so the verbatim pass-through this test asserts reads true.
    const refusal =
      'Cannot deactivate LASER-01: 4 operations still have live work here (3 ready, 1 in progress). ' +
      'Move them to another machine (Dispatch Board -> Move to machine) or complete them first.';
    mockedApi.updateWorkCenter.mockRejectedValue({
      response: { status: 409, data: { detail: refusal } },
    });
    renderPage();
    await screen.findAllByTestId('group-header');

    fireEvent.click(screen.getByLabelText('Deactivate LASER-01'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Deactivate' }));

    // The server's composed refusal lands VERBATIM in an error toast.
    const toastText = await screen.findByText(refusal);
    expect(toastText.closest('[role="alert"]')).toBeInTheDocument();

    // The row stays active: no reload happened (mount load only) and the
    // Deactivate affordance is still offered for the row.
    expect(mockedApi.getWorkCenters).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Deactivate LASER-01')).toBeInTheDocument();
    expect(screen.queryByLabelText('Reactivate LASER-01')).not.toBeInTheDocument();
  });

  it('reactivates an inactive work center directly (no confirm) and reloads', async () => {
    mockedApi.updateWorkCenter.mockResolvedValue({ ...inactiveWc, is_active: true } as any);
    renderPage();
    await screen.findAllByTestId('group-header');

    fireEvent.click(screen.getByLabelText('Reactivate WELD-09'));

    // No dialog — the server allows reactivation unconditionally.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => {
      expect(mockedApi.updateWorkCenter).toHaveBeenCalledWith(2, { is_active: true });
    });
    await waitFor(() => expect(mockedApi.getWorkCenters).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Reactivated WELD-09')).toBeInTheDocument();
  });
});
