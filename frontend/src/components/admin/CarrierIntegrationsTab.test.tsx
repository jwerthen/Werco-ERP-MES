/**
 * CarrierIntegrationsTab — the Admin > Carriers / Integrations console.
 *
 * Covers the stage's first bullet: the credentials table renders ONLY the
 * masked ••••last4 (asserting a full key never reaches the DOM), the
 * create/edit modal submits to createCarrierAccount / updateCarrierAccount,
 * Test Connection raises a success/failure toast, and the egress kill switch
 * renders with its CUI warning and submits via updateShippingProfile.
 *
 * The api service is mocked at the module boundary (same pattern as the sibling
 * ScheduleShipmentModal / ShipmentTrackingPanel tests) — no real network.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import CarrierIntegrationsTab from './CarrierIntegrationsTab';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import type { CarrierAccount, CompanyShippingProfile } from '../../types/shipping';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getCarrierAccounts: jest.fn(),
    getShippingProfile: jest.fn(),
    createCarrierAccount: jest.fn(),
    updateCarrierAccount: jest.fn(),
    deleteCarrierAccount: jest.fn(),
    testCarrierConnection: jest.fn(),
    updateShippingProfile: jest.fn(),
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

const FULL_KEY = 'sk_live_supersecret_value_abcd';

const account: CarrierAccount = {
  id: 5,
  name: 'EasyPost (production)',
  provider: 'easypost',
  environment: 'production',
  is_active: true,
  is_default: true,
  carrier_refs: ['fedex', 'ups'],
  api_key_last4: 'abcd',
  has_webhook_secret: true,
  created_at: '2026-06-09T00:00:00Z',
};

const profile: CompanyShippingProfile = {
  id: 1,
  allow_carrier_egress: false,
  ship_from_city: 'Tulsa',
  ship_from_state: 'OK',
  ship_from_zip: '74101',
  ship_from_country: 'US',
};

const renderTab = () =>
  render(
    <ToastProvider>
      <CarrierIntegrationsTab />
    </ToastProvider>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  // Sensible defaults; individual tests override as needed.
  mockApi.getCarrierAccounts.mockResolvedValue([account]);
  mockApi.getShippingProfile.mockResolvedValue(profile);
});

describe('CarrierIntegrationsTab — credentials table', () => {
  it('renders accounts showing ONLY the masked ••••last4, never the full key', async () => {
    renderTab();

    await waitFor(() => expect(screen.getByText('EasyPost (production)')).toBeInTheDocument());

    // The masked tail is shown.
    expect(screen.getByText('••••abcd')).toBeInTheDocument();
    // The full plaintext key must never appear anywhere in the rendered output.
    expect(screen.queryByText((_, node) => !!node?.textContent?.includes(FULL_KEY))).toBeNull();
    expect(document.body.textContent).not.toContain(FULL_KEY);
    // Carrier refs render as the key list only.
    expect(screen.getByText('fedex, ups')).toBeInTheDocument();
    expect(screen.getByText('Default')).toBeInTheDocument();
  });

  it('shows an empty state when no accounts are configured', async () => {
    mockApi.getCarrierAccounts.mockResolvedValueOnce([]);
    renderTab();
    await waitFor(() => expect(screen.getByText(/no carrier accounts configured yet/i)).toBeInTheDocument());
  });
});

describe('CarrierIntegrationsTab — create / edit', () => {
  it('submits a new account to createCarrierAccount with the write-only api_key', async () => {
    mockApi.getCarrierAccounts.mockResolvedValueOnce([]); // start empty
    mockApi.createCarrierAccount.mockResolvedValueOnce({ ...account, id: 9 });
    renderTab();

    await waitFor(() => expect(screen.getByText(/no carrier accounts configured yet/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /add account/i }));

    fireEvent.change(screen.getByPlaceholderText(/e\.g\. EasyPost \(production\)/i), {
      target: { value: 'EasyPost Sandbox' },
    });
    // The API key field is type=password (write-only). Find it by its placeholder.
    fireEvent.change(screen.getByPlaceholderText(/write-only; encrypted at rest/i), {
      target: { value: FULL_KEY },
    });

    fireEvent.click(screen.getByRole('button', { name: /create account/i }));

    await waitFor(() =>
      expect(mockApi.createCarrierAccount).toHaveBeenCalledWith(
        expect.objectContaining({ name: 'EasyPost Sandbox', api_key: FULL_KEY, provider: 'easypost' }),
      ),
    );
    // Success toast surfaces.
    await waitFor(() => expect(screen.getByText(/carrier account created/i)).toBeInTheDocument());
  });

  it('blocks create when the API key is missing (no network call, error toast)', async () => {
    mockApi.getCarrierAccounts.mockResolvedValueOnce([]);
    renderTab();

    await waitFor(() => expect(screen.getByText(/no carrier accounts configured yet/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /add account/i }));
    fireEvent.change(screen.getByPlaceholderText(/e\.g\. EasyPost \(production\)/i), {
      target: { value: 'No Key Account' },
    });
    fireEvent.click(screen.getByRole('button', { name: /create account/i }));

    await waitFor(() => expect(screen.getByText(/api key is required/i)).toBeInTheDocument());
    expect(mockApi.createCarrierAccount).not.toHaveBeenCalled();
  });

  it('submits an edit to updateCarrierAccount without an api_key when left blank (secret not rotated)', async () => {
    mockApi.updateCarrierAccount.mockResolvedValueOnce({ ...account, name: 'EasyPost Renamed' });
    renderTab();

    await waitFor(() => expect(screen.getByText('EasyPost (production)')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Edit'));

    const nameInput = screen.getByDisplayValue('EasyPost (production)');
    fireEvent.change(nameInput, { target: { value: 'EasyPost Renamed' } });
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.updateCarrierAccount).toHaveBeenCalled());
    const [id, payload] = mockApi.updateCarrierAccount.mock.calls[0];
    expect(id).toBe(5);
    expect(payload.name).toBe('EasyPost Renamed');
    // Blank API key must NOT be sent on edit — that would wipe/rotate the stored secret.
    expect(payload).not.toHaveProperty('api_key');
    await waitFor(() => expect(screen.getByText(/carrier account updated/i)).toBeInTheDocument());
  });
});

describe('CarrierIntegrationsTab — test connection', () => {
  it('shows a success toast when the connection test passes', async () => {
    mockApi.testCarrierConnection.mockResolvedValueOnce({ ok: true, provider: 'easypost', message: 'Connected!' });
    renderTab();

    await waitFor(() => expect(screen.getByText('EasyPost (production)')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Test connection'));

    await waitFor(() => expect(mockApi.testCarrierConnection).toHaveBeenCalledWith(5));
    await waitFor(() => expect(screen.getByText('Connected!')).toBeInTheDocument());
  });

  it('shows an error toast when the connection test fails (ok:false)', async () => {
    mockApi.testCarrierConnection.mockResolvedValueOnce({ ok: false, provider: 'easypost', message: 'Invalid key' });
    renderTab();

    await waitFor(() => expect(screen.getByText('EasyPost (production)')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Test connection'));

    await waitFor(() => expect(screen.getByText('Invalid key')).toBeInTheDocument());
  });

  it('shows an error toast when the connection test request rejects', async () => {
    mockApi.testCarrierConnection.mockRejectedValueOnce(http(502, 'Aggregator unreachable'));
    renderTab();

    await waitFor(() => expect(screen.getByText('EasyPost (production)')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Test connection'));

    await waitFor(() => expect(screen.getByText('Aggregator unreachable')).toBeInTheDocument());
  });
});

describe('CarrierIntegrationsTab — egress kill switch', () => {
  it('renders the egress toggle with its CUI warning and the DISABLED banner by default', async () => {
    renderTab();

    await waitFor(() => expect(screen.getByText('EasyPost (production)')).toBeInTheDocument());

    // Banner reflects egress OFF.
    expect(screen.getByText(/carrier egress is disabled/i)).toBeInTheDocument();
    // The toggle warning calls out CUI / data-egress sign-off (present in both the
    // banner and the toggle's inline warning — assert at least one rendered).
    expect(screen.getByText(/allow carrier egress/i)).toBeInTheDocument();
    expect(screen.getAllByText(/CUI \/ data-egress sign-off/i).length).toBeGreaterThan(0);

    const egressToggle = screen.getByRole('checkbox', { name: /allow carrier egress/i });
    expect(egressToggle).not.toBeChecked();
  });

  it('flips the egress kill switch ON and submits via updateShippingProfile', async () => {
    mockApi.updateShippingProfile.mockResolvedValueOnce({ ...profile, allow_carrier_egress: true });
    renderTab();

    await waitFor(() => expect(screen.getByText('EasyPost (production)')).toBeInTheDocument());

    const egressToggle = screen.getByRole('checkbox', { name: /allow carrier egress/i });
    fireEvent.click(egressToggle);
    expect(egressToggle).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: /save shipping profile/i }));

    await waitFor(() => expect(mockApi.updateShippingProfile).toHaveBeenCalled());
    const payload = mockApi.updateShippingProfile.mock.calls[0][0];
    expect(payload.allow_carrier_egress).toBe(true);
    // Numeric dims are normalized to null when blank, never the empty string.
    expect(payload.default_package_weight_lbs).toBeNull();
    await waitFor(() => expect(screen.getByText(/shipping profile saved/i)).toBeInTheDocument());
  });

  it('shows the ENABLED banner when the profile already permits egress', async () => {
    mockApi.getShippingProfile.mockResolvedValueOnce({ ...profile, allow_carrier_egress: true });
    renderTab();

    await waitFor(() => expect(screen.getByText(/carrier egress is enabled/i)).toBeInTheDocument());
    const egressToggle = screen.getByRole('checkbox', { name: /allow carrier egress/i });
    expect(egressToggle).toBeChecked();
  });

  it('treats a 404 shipping profile as "not configured yet" (no error toast)', async () => {
    mockApi.getShippingProfile.mockRejectedValueOnce(http(404, 'No profile'));
    renderTab();

    await waitFor(() => expect(screen.getByText(/no shipping profile is configured yet/i)).toBeInTheDocument());
    // 404 is expected and must NOT raise an error toast.
    expect(screen.queryByText(/failed to load shipping profile/i)).toBeNull();
  });
});

describe('CarrierIntegrationsTab — delete', () => {
  it('soft-deletes via deleteCarrierAccount after confirmation', async () => {
    mockApi.deleteCarrierAccount.mockResolvedValueOnce({ status: 'ok' } as never);
    renderTab();

    await waitFor(() => expect(screen.getByText('EasyPost (production)')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Delete'));

    // Confirmation dialog: scope to the modal that contains the soft-delete copy.
    const modal = (await screen.findByText(/it is\s+soft-deleted/i)).closest('.modal') as HTMLElement;
    expect(modal).not.toBeNull();
    const confirmBtn = within(modal).getByRole('button', { name: /^delete$/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => expect(mockApi.deleteCarrierAccount).toHaveBeenCalledWith(5));
    await waitFor(() => expect(screen.getByText(/"EasyPost \(production\)" deleted/i)).toBeInTheDocument());
  });
});
