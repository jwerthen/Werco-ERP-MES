/**
 * SmsEgressTab — the Admin > SMS Privacy console.
 *
 * Covers: the current ``allow_sms_egress`` state is read from GET /companies/me
 * and reflected in the banner + switch, enabling egress requires an explicit
 * confirmation (and only persists on confirm) while disabling is immediate, the
 * confirmed PUT hits updateCompanySmsEgress(true), a failed save rolls the
 * optimistic switch back and surfaces the server's verbatim detail, ADMIN (and
 * superuser / platform_admin) can edit, and any other role — including MANAGER —
 * sees the control read-only, defense in depth matching the ADMIN-only server
 * contract. Also asserts the confirmation copy names the third-party carrier
 * boundary and that no Twilio credential field exists anywhere in the console.
 *
 * The api service and the auth context are mocked at the module boundary — no
 * real network and a controllable current-user role.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import SmsEgressTab from './SmsEgressTab';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import type { Company, User } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getCurrentCompany: jest.fn(),
    updateCompanySmsEgress: jest.fn(),
  },
}));

jest.mock('../../context/AuthContext', () => ({
  __esModule: true,
  useAuth: jest.fn(),
}));

const mockApi = api as jest.Mocked<typeof api>;
const mockUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;

const httpError = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const company = (overrides: Partial<Company> = {}): Company => ({
  id: 1,
  name: 'Acme Precision',
  slug: 'acme',
  is_active: true,
  allow_ai_egress: false,
  allow_sms_egress: false,
  ...overrides,
});

const asUser = (role: User['role'], isSuperuser = false): User =>
  ({
    id: 1,
    version: 1,
    employee_id: '0001',
    email: 'admin@acme.com',
    first_name: 'Ada',
    last_name: 'Admin',
    role,
    is_active: true,
    is_superuser: isSuperuser,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }) as User;

const mockAuthRole = (role: User['role'], isSuperuser = false) => {
  mockUseAuth.mockReturnValue({ user: asUser(role, isSuperuser) } as ReturnType<typeof useAuth>);
};

const renderTab = () =>
  render(
    <ToastProvider>
      <SmsEgressTab />
    </ToastProvider>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getCurrentCompany.mockResolvedValue(company());
  mockAuthRole('admin');
});

describe('SmsEgressTab — render', () => {
  it('reads the current OFF state from GET /companies/me and shows the DISABLED banner', async () => {
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow sms egress/i })).not.toBeChecked();
    expect(mockApi.getCurrentCompany).toHaveBeenCalled();
  });

  it('treats a company payload with no allow_sms_egress field as OFF (fail-closed)', async () => {
    const legacy = company();
    delete (legacy as Partial<Company>).allow_sms_egress;
    mockApi.getCurrentCompany.mockResolvedValueOnce(legacy);
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow sms egress/i })).not.toBeChecked();
  });

  it('reflects an ON state from the company read', async () => {
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ allow_sms_egress: true }));
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is enabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow sms egress/i })).toBeChecked();
  });

  it('exposes no provider-credential input (the console toggles one boolean)', async () => {
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    // The only control on the tab is the kill switch itself.
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByText(/account sid|auth token|api key/i)).toBeNull();
  });
});

describe('SmsEgressTab — egress kill switch', () => {
  it('requires explicit confirmation before turning egress ON and persists on confirm', async () => {
    mockApi.updateCompanySmsEgress.mockResolvedValueOnce(company({ allow_sms_egress: true }));
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    const toggle = screen.getByRole('checkbox', { name: /allow sms egress/i });
    expect(toggle).not.toBeChecked();

    // Clicking ON opens a confirmation dialog and does NOT call the API yet.
    fireEvent.click(toggle);
    expect(await screen.findByText(/enable sms egress\?/i)).toBeInTheDocument();
    expect(mockApi.updateCompanySmsEgress).not.toHaveBeenCalled();

    // Confirming persists allow=true and flips the banner.
    fireEvent.click(screen.getByRole('button', { name: /enable egress/i }));
    await waitFor(() => expect(mockApi.updateCompanySmsEgress).toHaveBeenCalledWith(true));
    await waitFor(() => expect(screen.getByText(/sms egress is enabled/i)).toBeInTheDocument());
  });

  it('states plainly in the confirmation that message bodies leave the boundary to a carrier', async () => {
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: /allow sms egress/i }));

    const dialogCopy = await screen.findByText(/will transmit notification message bodies/i);
    expect(dialogCopy).toHaveTextContent(/commercial sms carrier outside the system boundary/i);
    expect(dialogCopy).toHaveTextContent(/cui/i);
    expect(dialogCopy).toHaveTextContent(/audit trail/i);
  });

  it('cancelling the confirmation leaves egress OFF and never calls the API', async () => {
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: /allow sms egress/i }));
    expect(await screen.findByText(/enable sms egress\?/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    await waitFor(() => expect(screen.queryByText(/enable sms egress\?/i)).toBeNull());
    expect(mockApi.updateCompanySmsEgress).not.toHaveBeenCalled();
    expect(screen.getByRole('checkbox', { name: /allow sms egress/i })).not.toBeChecked();
  });

  it('turns egress OFF immediately (no confirmation) when already enabled', async () => {
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ allow_sms_egress: true }));
    mockApi.updateCompanySmsEgress.mockResolvedValueOnce(company({ allow_sms_egress: false }));
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is enabled/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: /allow sms egress/i }));

    // No confirmation dialog when disabling; it persists allow=false directly.
    expect(screen.queryByText(/enable sms egress\?/i)).toBeNull();
    await waitFor(() => expect(mockApi.updateCompanySmsEgress).toHaveBeenCalledWith(false));
    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
  });

  it('rolls the switch back to the last-known state and surfaces the server detail when the save fails', async () => {
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ allow_sms_egress: true }));
    mockApi.updateCompanySmsEgress.mockRejectedValueOnce(httpError(409, 'Egress is locked by policy'));
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is enabled/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: /allow sms egress/i }));

    await waitFor(() => expect(mockApi.updateCompanySmsEgress).toHaveBeenCalledWith(false));
    // The optimistic flip is reverted: still ENABLED after the failure.
    await waitFor(() => expect(screen.getByText(/sms egress is enabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow sms egress/i })).toBeChecked();
    expect(await screen.findByText('Egress is locked by policy')).toBeInTheDocument();
  });
});

describe('SmsEgressTab — RBAC', () => {
  it('allows an ADMIN to flip the control', async () => {
    mockAuthRole('admin');
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow sms egress/i })).not.toBeDisabled();
    expect(screen.queryByText(/only an administrator can change/i)).toBeNull();
  });

  it('allows a superuser (platform admin) to flip the control', async () => {
    mockAuthRole('platform_admin', true);
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow sms egress/i })).not.toBeDisabled();
    expect(screen.queryByText(/only an administrator can change/i)).toBeNull();
  });

  it('renders the control read-only for a MANAGER (ADMIN-only contract)', async () => {
    mockAuthRole('manager');
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    const toggle = screen.getByRole('checkbox', { name: /allow sms egress/i });
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/only an administrator can change/i)).toBeInTheDocument();

    // A blocked click never reaches the API.
    fireEvent.click(toggle);
    expect(mockApi.updateCompanySmsEgress).not.toHaveBeenCalled();
  });

  it('renders the control read-only for a non-admin role', async () => {
    mockAuthRole('operator');
    renderTab();

    await waitFor(() => expect(screen.getByText(/sms egress is disabled/i)).toBeInTheDocument());
    const toggle = screen.getByRole('checkbox', { name: /allow sms egress/i });
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/only an administrator can change/i)).toBeInTheDocument();

    // A blocked click never reaches the API.
    fireEvent.click(toggle);
    expect(mockApi.updateCompanySmsEgress).not.toHaveBeenCalled();
  });
});
