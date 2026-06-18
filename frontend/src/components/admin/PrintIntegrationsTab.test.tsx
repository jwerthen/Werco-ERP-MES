/**
 * PrintIntegrationsTab — the Admin > Label Printing console.
 *
 * Covers: the form renders the masked ••••last4 (never a full key), saving
 * submits to updatePrintProfile WITHOUT api_key when left blank (secret not
 * rotated) and WITH it when typed, the egress kill switch requires an explicit
 * confirmation before turning ON, and a 404 print profile is treated as "not
 * configured yet" with no error toast.
 *
 * The api service is mocked at the module boundary (same pattern as the sibling
 * CarrierIntegrationsTab test) — no real network.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PrintIntegrationsTab from './PrintIntegrationsTab';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import type { PrintProfile } from '../../types/print';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getPrintProfile: jest.fn(),
    updatePrintProfile: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const FULL_KEY = 'pbx_live_supersecret_value_wxyz';

const profile: PrintProfile = {
  id: 1,
  proxybox_base_url: 'https://pbx-1234.pbxz.cloud/api/v1',
  proxybox_target: 'whtp203e-01',
  api_key_last4: 'wxyz',
  has_api_key: true,
  default_paper_size: '4x6',
  default_copies: 2,
  auto_print_on_receipt: false,
  allow_print_egress: false,
  is_active: true,
  created_at: '2026-06-18T00:00:00Z',
};

const renderTab = () =>
  render(
    <ToastProvider>
      <PrintIntegrationsTab />
    </ToastProvider>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getPrintProfile.mockResolvedValue(profile);
});

describe('PrintIntegrationsTab — render', () => {
  it('renders the profile showing ONLY the masked ••••last4, never the full key', async () => {
    renderTab();

    await waitFor(() =>
      expect(screen.getByDisplayValue('https://pbx-1234.pbxz.cloud/api/v1')).toBeInTheDocument(),
    );

    // The masked tail is shown as the password placeholder.
    expect(screen.getByPlaceholderText('••••wxyz')).toBeInTheDocument();
    // The full plaintext key must never appear anywhere in the rendered output.
    expect(document.body.textContent).not.toContain(FULL_KEY);
    // Egress defaults OFF -> DISABLED banner.
    expect(screen.getByText(/print egress is disabled/i)).toBeInTheDocument();
  });

  it('treats a 404 print profile as "not configured yet" (no error toast)', async () => {
    mockApi.getPrintProfile.mockRejectedValueOnce(http(404, 'Print profile not configured'));
    renderTab();

    await waitFor(() => expect(screen.getByText(/no print profile is configured yet/i)).toBeInTheDocument());
    expect(screen.queryByText(/failed to load print profile/i)).toBeNull();
  });
});

describe('PrintIntegrationsTab — save', () => {
  it('submits WITHOUT api_key when the key field is left blank (secret not rotated)', async () => {
    mockApi.updatePrintProfile.mockResolvedValueOnce({ ...profile, default_copies: 3 });
    renderTab();

    await waitFor(() =>
      expect(screen.getByDisplayValue('https://pbx-1234.pbxz.cloud/api/v1')).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole('button', { name: /save print settings/i }));

    await waitFor(() => expect(mockApi.updatePrintProfile).toHaveBeenCalled());
    const payload = mockApi.updatePrintProfile.mock.calls[0][0];
    // Blank API key must NOT be sent — that would wipe/rotate the stored secret.
    expect(payload).not.toHaveProperty('api_key');
    expect(payload.proxybox_target).toBe('whtp203e-01');
    await waitFor(() => expect(screen.getByText(/print settings saved/i)).toBeInTheDocument());
  });

  it('sends a new api_key only when the admin types one', async () => {
    mockApi.updatePrintProfile.mockResolvedValueOnce(profile);
    renderTab();

    await waitFor(() => expect(screen.getByPlaceholderText('••••wxyz')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('••••wxyz'), { target: { value: FULL_KEY } });
    fireEvent.click(screen.getByRole('button', { name: /save print settings/i }));

    await waitFor(() => expect(mockApi.updatePrintProfile).toHaveBeenCalled());
    const payload = mockApi.updatePrintProfile.mock.calls[0][0];
    expect(payload.api_key).toBe(FULL_KEY);
  });
});

describe('PrintIntegrationsTab — egress kill switch', () => {
  it('requires explicit confirmation before turning egress ON', async () => {
    renderTab();

    await waitFor(() => expect(screen.getByText(/print egress is disabled/i)).toBeInTheDocument());

    const egressToggle = screen.getByRole('checkbox', { name: /allow print egress/i });
    expect(egressToggle).not.toBeChecked();

    // Clicking ON opens a confirmation dialog and does NOT immediately flip it.
    fireEvent.click(egressToggle);
    expect(await screen.findByText(/enable print egress\?/i)).toBeInTheDocument();
    expect(egressToggle).not.toBeChecked();

    // Confirming flips it on.
    fireEvent.click(screen.getByRole('button', { name: /enable egress/i }));
    await waitFor(() => expect(egressToggle).toBeChecked());
  });

  it('saves the enabled egress flag once confirmed', async () => {
    mockApi.updatePrintProfile.mockResolvedValueOnce({ ...profile, allow_print_egress: true });
    renderTab();

    await waitFor(() => expect(screen.getByText(/print egress is disabled/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: /allow print egress/i }));
    fireEvent.click(await screen.findByRole('button', { name: /enable egress/i }));
    fireEvent.click(screen.getByRole('button', { name: /save print settings/i }));

    await waitFor(() => expect(mockApi.updatePrintProfile).toHaveBeenCalled());
    const payload = mockApi.updatePrintProfile.mock.calls[0][0];
    expect(payload.allow_print_egress).toBe(true);
  });

  it('turns egress OFF without a confirmation when already enabled', async () => {
    mockApi.getPrintProfile.mockResolvedValueOnce({ ...profile, allow_print_egress: true });
    renderTab();

    await waitFor(() => expect(screen.getByText(/print egress is enabled/i)).toBeInTheDocument());
    const egressToggle = screen.getByRole('checkbox', { name: /allow print egress/i });
    expect(egressToggle).toBeChecked();

    fireEvent.click(egressToggle);
    // No confirmation dialog when disabling; flips immediately.
    expect(screen.queryByText(/enable print egress\?/i)).toBeNull();
    expect(egressToggle).not.toBeChecked();
  });
});
