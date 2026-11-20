/**
 * FloorPinPolicyTab — Admin > Badge Sign-in: the company's "Require a PIN for badge sign-in"
 * policy (`require_floor_pin`, AS9100D 8.5.2).
 *
 * Pinned: the state is read from GET /companies/me; turning it ON asks first (it can stop
 * people signing in) and persists only on confirm, while turning it OFF is immediate; the
 * page is NON-optimistic -- the switch shows only what the server returned, and a refusal
 * leaves it where it was with the server's verbatim detail; the `users_without_pin` the write
 * returns drives the warning (plural / singular / none); only an Admin (or a superuser /
 * platform admin) can flip it, everyone else sees it read-only; a failed load offers Retry.
 *
 * The api service and the auth context are mocked at the module boundary (the
 * SmsEgressTab.test.tsx pattern).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import FloorPinPolicyTab from './FloorPinPolicyTab';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import type { Company, FloorPinPolicyResponse, User } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getCurrentCompany: jest.fn(),
    updateCompanyFloorPinPolicy: jest.fn(),
  },
}));

jest.mock('../../context/AuthContext', () => ({
  __esModule: true,
  useAuth: jest.fn(),
}));

const mockApi = api as jest.Mocked<typeof api>;
const mockUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;

const TOGGLE = 'Require a PIN for badge sign-in';

const company = (overrides: Partial<Company> = {}): Company => ({
  id: 1,
  name: 'Acme Precision',
  slug: 'acme',
  is_active: true,
  allow_ai_egress: false,
  allow_sms_egress: false,
  require_floor_pin: false,
  ...overrides,
});

const policy = (requireFloorPin: boolean, usersWithoutPin: number | null): FloorPinPolicyResponse => ({
  ...company({ require_floor_pin: requireFloorPin }),
  require_floor_pin: requireFloorPin,
  users_without_pin: usersWithoutPin,
});

const httpError = (status: number, detail: string) => {
  const err = new Error(detail) as Error & { response: { status: number; data: { detail: string } } };
  err.response = { status, data: { detail } };
  return err;
};

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
    <MemoryRouter>
      <ToastProvider>
        <FloorPinPolicyTab />
      </ToastProvider>
    </MemoryRouter>
  );

const toggle = () => screen.getByRole('checkbox', { name: TOGGLE });

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getCurrentCompany.mockResolvedValue(company());
  mockAuthRole('admin');
});

describe('FloorPinPolicyTab', () => {
  it('reads the policy from the company record', async () => {
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ require_floor_pin: true }));
    renderTab();

    expect(await screen.findByRole('checkbox', { name: TOGGLE })).toBeChecked();
    expect(toggle()).toBeEnabled();
    expect(toggle()).toHaveAccessibleDescription(/a badge with no floor PIN is refused/i);
  });

  it('treats a company record without the field as off', async () => {
    const legacy = company();
    delete legacy.require_floor_pin;
    mockApi.getCurrentCompany.mockResolvedValueOnce(legacy);
    renderTab();

    expect(await screen.findByRole('checkbox', { name: TOGGLE })).not.toBeChecked();
  });

  it('turning it ON asks first and persists only on confirm, then warns with the count', async () => {
    mockApi.updateCompanyFloorPinPolicy.mockResolvedValueOnce(policy(true, 3));
    renderTab();

    fireEvent.click(await screen.findByRole('checkbox', { name: TOGGLE }));
    const confirm = await screen.findByRole('dialog', { name: 'Require a PIN for badge sign-in?' });
    expect(mockApi.updateCompanyFloorPinPolicy).not.toHaveBeenCalled();
    expect(toggle()).not.toBeChecked();

    fireEvent.click(within(confirm).getByRole('button', { name: 'Require PIN' }));

    await waitFor(() => expect(toggle()).toBeChecked());
    expect(mockApi.updateCompanyFloorPinPolicy).toHaveBeenCalledWith(true);
    const warning = screen.getByText(/^3 active (badge )?users have no floor PIN$/);
    expect(warning.closest('[role="alert"]')).not.toBeNull();
    expect(screen.getByRole('link', { name: 'Users' })).toHaveAttribute('href', '/users');
    expect(await screen.findByText('A PIN is now required for badge sign-in')).toBeInTheDocument();
  });

  it('cancelling the confirmation changes nothing', async () => {
    renderTab();

    fireEvent.click(await screen.findByRole('checkbox', { name: TOGGLE }));
    const confirm = await screen.findByRole('dialog', { name: 'Require a PIN for badge sign-in?' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockApi.updateCompanyFloorPinPolicy).not.toHaveBeenCalled();
    expect(toggle()).not.toBeChecked();
  });

  it('a single user without a PIN is worded in the singular', async () => {
    mockApi.updateCompanyFloorPinPolicy.mockResolvedValueOnce(policy(true, 1));
    renderTab();

    fireEvent.click(await screen.findByRole('checkbox', { name: TOGGLE }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Require PIN' }));

    expect(await screen.findByText(/^1 active (badge )?user has no floor PIN$/)).toBeInTheDocument();
    expect(screen.getByText(/That person is/)).toBeInTheDocument();
  });

  it('nobody without a PIN says so instead of warning', async () => {
    mockApi.updateCompanyFloorPinPolicy.mockResolvedValueOnce(policy(true, 0));
    renderTab();

    fireEvent.click(await screen.findByRole('checkbox', { name: TOGGLE }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Require PIN' }));

    expect(await screen.findByText(/^Every active (badge )?user has a floor PIN\.$/)).toBeInTheDocument();
    expect(screen.queryByText(/no floor PIN$/)).not.toBeInTheDocument();
  });

  it('turning it OFF is immediate, with no confirmation, and the warning goes', async () => {
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ require_floor_pin: true }));
    mockApi.updateCompanyFloorPinPolicy.mockResolvedValueOnce(policy(false, 4));
    renderTab();

    fireEvent.click(await screen.findByRole('checkbox', { name: TOGGLE }));

    await waitFor(() => expect(toggle()).not.toBeChecked());
    expect(mockApi.updateCompanyFloorPinPolicy).toHaveBeenCalledWith(false);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByText(/no floor PIN$/)).not.toBeInTheDocument();
    expect(await screen.findByText('A PIN is no longer required for badge sign-in')).toBeInTheDocument();
  });

  it('shows only what the server returned (non-optimistic)', async () => {
    // The server says it is still off (e.g. a racing flip): the switch follows the server.
    mockApi.updateCompanyFloorPinPolicy.mockResolvedValueOnce(policy(false, 2));
    renderTab();

    fireEvent.click(await screen.findByRole('checkbox', { name: TOGGLE }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Require PIN' }));

    await waitFor(() => expect(mockApi.updateCompanyFloorPinPolicy).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(toggle()).not.toBeChecked();
  });

  it('a refused write leaves the switch where it was and shows the verbatim detail', async () => {
    mockApi.updateCompanyFloorPinPolicy.mockRejectedValueOnce(
      httpError(
        503,
        'The audit record for this badge sign-in PIN policy change could not be saved; nothing was changed. Retry.'
      )
    );
    renderTab();

    fireEvent.click(await screen.findByRole('checkbox', { name: TOGGLE }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Require PIN' }));

    expect(
      await screen.findByText(
        'The audit record for this badge sign-in PIN policy change could not be saved; nothing was changed. Retry.'
      )
    ).toBeInTheDocument();
    expect(toggle()).not.toBeChecked();
    expect(screen.queryByText(/no floor PIN$/)).not.toBeInTheDocument();
  });

  it.each([
    ['superuser', 'operator' as User['role'], true],
    ['platform admin', 'platform_admin' as User['role'], false],
  ])('a %s can edit', async (_label, role, superuser) => {
    mockAuthRole(role, superuser);
    renderTab();
    expect(await screen.findByRole('checkbox', { name: TOGGLE })).toBeEnabled();
  });

  it.each(['manager', 'supervisor', 'quality', 'operator', 'viewer'] as User['role'][])(
    'a %s sees it read-only and cannot flip it',
    async role => {
      mockAuthRole(role);
      renderTab();

      const box = await screen.findByRole('checkbox', { name: TOGGLE });
      expect(box).toBeDisabled();
      expect(screen.getByText(/Only an Administrator can change this policy/)).toBeInTheDocument();
      fireEvent.click(box);
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(mockApi.updateCompanyFloorPinPolicy).not.toHaveBeenCalled();
    }
  );

  it('a failed load offers Retry, which reloads', async () => {
    mockApi.getCurrentCompany.mockRejectedValueOnce(new Error('offline'));
    mockApi.getCurrentCompany.mockResolvedValueOnce(company({ require_floor_pin: true }));
    renderTab();

    fireEvent.click(await screen.findByRole('button', { name: /retry/i }));

    expect(await screen.findByRole('checkbox', { name: TOGGLE })).toBeChecked();
    expect(mockApi.getCurrentCompany).toHaveBeenCalledTimes(2);
  });
});
