/**
 * AIEgressTab — the Admin > AI Privacy console.
 *
 * Covers: the current ``allow_ai_egress`` state is read from GET /companies/me
 * and reflected in the banner + switch, enabling egress requires an explicit
 * confirmation (and only persists on confirm) while disabling is immediate,
 * the confirmed PUT hits updateCompanyAiEgress(true), a failed save rolls the
 * optimistic switch back, ADMIN (and superuser / platform_admin) can edit, and
 * any other role — including MANAGER — sees the control read-only (disabled),
 * defense in depth matching the ADMIN-only server contract.
 *
 * The api service and the auth context are mocked at the module boundary — no
 * real network and a controllable current-user role.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AIEgressTab from './AIEgressTab';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import type { Company, User } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getCurrentCompany: jest.fn(),
    updateCompanyAiEgress: jest.fn(),
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
      <AIEgressTab />
    </ToastProvider>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getCurrentCompany.mockResolvedValue(company());
  mockAuthRole('admin');
});

describe('AIEgressTab — render', () => {
  it('reads the current OFF state from GET /companies/me and shows the DISABLED banner', async () => {
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is disabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow ai egress/i })).not.toBeChecked();
    expect(mockApi.getCurrentCompany).toHaveBeenCalled();
  });

  it('reflects an ON state from the company read', async () => {
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ allow_ai_egress: true }));
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is enabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow ai egress/i })).toBeChecked();
  });
});

describe('AIEgressTab — egress kill switch', () => {
  it('requires explicit confirmation before turning egress ON and persists on confirm', async () => {
    mockApi.updateCompanyAiEgress.mockResolvedValueOnce(company({ allow_ai_egress: true }));
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is disabled/i)).toBeInTheDocument());
    const toggle = screen.getByRole('checkbox', { name: /allow ai egress/i });
    expect(toggle).not.toBeChecked();

    // Clicking ON opens a confirmation dialog and does NOT call the API yet.
    fireEvent.click(toggle);
    expect(await screen.findByText(/enable ai egress\?/i)).toBeInTheDocument();
    expect(mockApi.updateCompanyAiEgress).not.toHaveBeenCalled();

    // Confirming persists allow=true and flips the banner.
    fireEvent.click(screen.getByRole('button', { name: /enable egress/i }));
    await waitFor(() => expect(mockApi.updateCompanyAiEgress).toHaveBeenCalledWith(true));
    await waitFor(() => expect(screen.getByText(/ai egress is enabled/i)).toBeInTheDocument());
  });

  it('turns egress OFF immediately (no confirmation) when already enabled', async () => {
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ allow_ai_egress: true }));
    mockApi.updateCompanyAiEgress.mockResolvedValueOnce(company({ allow_ai_egress: false }));
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is enabled/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: /allow ai egress/i }));

    // No confirmation dialog when disabling; it persists allow=false directly.
    expect(screen.queryByText(/enable ai egress\?/i)).toBeNull();
    await waitFor(() => expect(mockApi.updateCompanyAiEgress).toHaveBeenCalledWith(false));
    await waitFor(() => expect(screen.getByText(/ai egress is disabled/i)).toBeInTheDocument());
  });

  it('rolls the switch back to the last-known state when the save fails', async () => {
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ allow_ai_egress: true }));
    mockApi.updateCompanyAiEgress.mockRejectedValueOnce(httpError(409, 'Conflict'));
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is enabled/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: /allow ai egress/i }));

    await waitFor(() => expect(mockApi.updateCompanyAiEgress).toHaveBeenCalledWith(false));
    // The optimistic flip is reverted: still ENABLED after the failure.
    await waitFor(() => expect(screen.getByText(/ai egress is enabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow ai egress/i })).toBeChecked();
  });
});

describe('AIEgressTab — RBAC', () => {
  it('allows an ADMIN to flip the control', async () => {
    mockAuthRole('admin');
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is disabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow ai egress/i })).not.toBeDisabled();
    expect(screen.queryByText(/only an administrator can change/i)).toBeNull();
  });

  it('allows a superuser (platform admin) to flip the control', async () => {
    mockAuthRole('platform_admin', true);
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is disabled/i)).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: /allow ai egress/i })).not.toBeDisabled();
    expect(screen.queryByText(/only an administrator can change/i)).toBeNull();
  });

  it('renders the control read-only for a MANAGER (ADMIN-only contract)', async () => {
    // The control gate now mirrors the backend ADMIN-only requirement on
    // PUT /companies/me/ai-egress (matching the sibling carrier / print egress
    // controls), so a MANAGER sees it read-only like any other non-admin role.
    mockAuthRole('manager');
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is disabled/i)).toBeInTheDocument());
    const toggle = screen.getByRole('checkbox', { name: /allow ai egress/i });
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/only an administrator can change/i)).toBeInTheDocument();

    // A blocked click never reaches the API.
    fireEvent.click(toggle);
    expect(mockApi.updateCompanyAiEgress).not.toHaveBeenCalled();
  });

  it('renders the control read-only for a non-admin role', async () => {
    mockAuthRole('operator');
    renderTab();

    await waitFor(() => expect(screen.getByText(/ai egress is disabled/i)).toBeInTheDocument());
    const toggle = screen.getByRole('checkbox', { name: /allow ai egress/i });
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/only an administrator can change/i)).toBeInTheDocument();

    // A blocked click never reaches the API.
    fireEvent.click(toggle);
    expect(mockApi.updateCompanyAiEgress).not.toHaveBeenCalled();
  });
});
