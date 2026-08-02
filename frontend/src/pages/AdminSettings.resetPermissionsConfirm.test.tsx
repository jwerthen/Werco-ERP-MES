/**
 * AdminSettings — role-permission reset confirm (the WARNING-variant
 * ConfirmDialog shape).
 *
 * "Reset to Default" no longer fires a native window.confirm: it opens the
 * shared ConfirmDialog (variant="warning" — a consequential but non-delete
 * action that discards a customized permission set), and
 * api.resetRolePermissions fires only from the dialog's Reset button, riding
 * the existing `saving` in-flight state as `pending`. Cancel closes without
 * any API call. This file pins that pattern once for the warning-confirm shape.
 *
 * Mounted via the ?tab=roles deep link so only the roles tab data loads.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import AdminSettings from './AdminSettings';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getRolePermissions: jest.fn(),
    resetRolePermissions: jest.fn(),
    updateRolePermissions: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const rolePermissionsData = {
  role_permissions: { admin: ['work_orders:view'] },
  all_permissions: ['work_orders:view', 'work_orders:edit'],
  permission_categories: { 'Work Orders': ['work_orders:view', 'work_orders:edit'] },
  roles: [{ value: 'admin', label: 'Admin' }],
};

function renderRolesTab() {
  return render(
    <MemoryRouter initialEntries={['/admin/settings?tab=roles']}>
      <ToastProvider>
        <AdminSettings />
      </ToastProvider>
    </MemoryRouter>
  );
}

describe('AdminSettings reset-role-permissions confirm (warning variant)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getRolePermissions.mockResolvedValue(rolePermissionsData as any);
  });

  it('opens the warning confirm dialog and resets only on confirm', async () => {
    mockedApi.resetRolePermissions.mockResolvedValue({} as any);
    renderRolesTab();

    fireEvent.click(await screen.findByRole('button', { name: /Reset to Default/i }));

    // Opening the dialog writes nothing.
    const dialog = await screen.findByRole('dialog');
    expect(mockedApi.resetRolePermissions).not.toHaveBeenCalled();
    expect(within(dialog).getByText('Reset admin permissions to defaults?')).toBeInTheDocument();

    // Warning variant: the confirm button carries the amber override, not the
    // red danger chrome.
    const confirmButton = within(dialog).getByRole('button', { name: 'Reset' });
    expect(confirmButton.className).toContain('bg-amber-500');

    fireEvent.click(confirmButton);
    await waitFor(() => {
      expect(mockedApi.resetRolePermissions).toHaveBeenCalledWith('admin');
    });

    // onUpdate re-loads the roles tab (initial load + post-reset refresh) and
    // the dialog closes on settle.
    await waitFor(() => expect(mockedApi.getRolePermissions).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('cancel closes the dialog without calling the API', async () => {
    renderRolesTab();

    fireEvent.click(await screen.findByRole('button', { name: /Reset to Default/i }));
    const dialog = await screen.findByRole('dialog');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.resetRolePermissions).not.toHaveBeenCalled();
  });
});
