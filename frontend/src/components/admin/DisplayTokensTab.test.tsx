/**
 * DisplayTokensTab — Admin > Wallboard Displays (A0.5).
 *
 * Covers: the token list (active / revoked / expired states), the create
 * flow surfacing the one-time setup code (primary) + JWT/URL fallback, the
 * optional department pass-through, the per-row "New setup code" re-issue
 * (disabled for revoked/expired rows, errors via toast), and revoke.
 * The api service is mocked at the module boundary (AIUsageTab pattern).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import DisplayTokensTab from './DisplayTokensTab';
import api from '../../services/api';
import { ToastProvider } from '../ui/Toast';
import type { DisplayToken, DisplayTokenIssued, SetupCodeResponse } from '../../types/wallboard';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    listDisplayTokens: jest.fn(),
    createDisplayToken: jest.fn(),
    revokeDisplayToken: jest.fn(),
    issueDisplaySetupCode: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const activeToken: DisplayToken = {
  id: 1,
  label: 'North wall TV',
  expires_at: '2099-01-01T00:00:00Z',
  revoked: false,
  revoked_at: null,
  created_by: 7,
  created_at: '2026-06-01T00:00:00Z',
  dept: null,
};

const revokedToken: DisplayToken = {
  ...activeToken,
  id: 2,
  label: 'Old TV',
  revoked: true,
  revoked_at: '2026-06-05T00:00:00Z',
};

const issued: DisplayTokenIssued = {
  ...activeToken,
  id: 3,
  label: 'Weld bay monitor',
  token: 'eyJ.display.jwt',
  setup_code: 'ABCD12EF',
  setup_code_expires_at: '2026-06-01T00:15:00Z',
  dept: null,
};

const reissuedCode: SetupCodeResponse = {
  id: 1,
  label: 'North wall TV',
  dept: 'weld',
  setup_code: 'WXYZ7890',
  setup_code_expires_at: '2026-06-01T00:15:00Z',
};

function renderTab() {
  return render(
    <ToastProvider>
      <DisplayTokensTab />
    </ToastProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.listDisplayTokens.mockResolvedValue([activeToken, revokedToken]);
});

describe('DisplayTokensTab', () => {
  it('lists tokens with their status and hides revoke for revoked ones', async () => {
    renderTab();

    expect(await screen.findByText('North wall TV')).toBeInTheDocument();
    expect(screen.getByText('Old TV')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Revoked')).toBeInTheDocument();
    expect(screen.getByTestId('revoke-1')).toBeInTheDocument();
    expect(screen.queryByTestId('revoke-2')).not.toBeInTheDocument();
  });

  it('creates a token and shows the one-time setup code (primary) + JWT/URL fallback', async () => {
    mockApi.createDisplayToken.mockResolvedValue(issued);
    renderTab();
    await screen.findByText('North wall TV');

    fireEvent.change(screen.getByTestId('display-token-label'), {
      target: { value: 'Weld bay monitor' },
    });
    fireEvent.change(screen.getByTestId('display-token-days'), { target: { value: '30' } });
    fireEvent.click(screen.getByRole('button', { name: /create token/i }));

    await waitFor(() =>
      expect(mockApi.createDisplayToken).toHaveBeenCalledWith({
        label: 'Weld bay monitor',
        expires_days: 30,
      }),
    );

    const panel = await screen.findByTestId('issued-panel');
    expect(panel).toHaveTextContent(/will not be shown again/i);
    // The TV pairing path is the headline: <host>/tv + the grouped code.
    expect(panel).toHaveTextContent(/on the tv/i);
    expect(panel.textContent).toContain(`${window.location.origin}/tv`);
    expect(screen.getByTestId('issued-setup-code')).toHaveTextContent('ABCD-12EF');
    expect(panel).toHaveTextContent(/valid 15 minutes, single use/i);
    // Fallback: the one-time JWT + fragment URL, still shown on create.
    expect((screen.getByTestId('issued-token') as HTMLInputElement).value).toBe('eyJ.display.jwt');
    // Token rides in the URL FRAGMENT — query strings land in server access
    // logs, fragments never leave the browser.
    const issuedUrl = (screen.getByTestId('issued-url') as HTMLInputElement).value;
    expect(issuedUrl).toContain('/wallboard#token=eyJ.display.jwt');
    expect(issuedUrl).not.toContain('?token=');
  });

  it('passes the optional department through on create', async () => {
    mockApi.createDisplayToken.mockResolvedValue({ ...issued, dept: 'weld' });
    renderTab();
    await screen.findByText('North wall TV');

    fireEvent.change(screen.getByTestId('display-token-label'), {
      target: { value: 'Weld bay monitor' },
    });
    fireEvent.change(screen.getByTestId('display-token-dept'), { target: { value: 'weld' } });
    fireEvent.click(screen.getByRole('button', { name: /create token/i }));

    await waitFor(() =>
      expect(mockApi.createDisplayToken).toHaveBeenCalledWith({
        label: 'Weld bay monitor',
        expires_days: 90,
        dept: 'weld',
      }),
    );
    const panel = await screen.findByTestId('issued-panel');
    expect(panel).toHaveTextContent(/pinned to department/i);
  });

  it('re-issues a setup code for a row and shows it in the one-time panel (no JWT/URL)', async () => {
    mockApi.issueDisplaySetupCode.mockResolvedValue(reissuedCode);
    renderTab();
    await screen.findByText('North wall TV');

    fireEvent.click(screen.getByTestId('new-code-1'));

    await waitFor(() => expect(mockApi.issueDisplaySetupCode).toHaveBeenCalledWith(1));
    const panel = await screen.findByTestId('issued-panel');
    expect(panel).toHaveTextContent(/will not be shown again/i);
    expect(screen.getByTestId('issued-setup-code')).toHaveTextContent('WXYZ-7890');
    // A re-issue mints a pairing code only — the JWT is never re-shown.
    expect(screen.queryByTestId('issued-token')).not.toBeInTheDocument();
    expect(screen.queryByTestId('issued-url')).not.toBeInTheDocument();
  });

  it('disables New setup code for revoked and expired rows', async () => {
    const naiveUtc = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, '');
    const expiredToken: DisplayToken = {
      ...activeToken,
      id: 3,
      label: 'Expired TV',
      expires_at: naiveUtc(new Date(Date.now() - 60 * 60 * 1000)),
    };
    mockApi.listDisplayTokens.mockResolvedValue([activeToken, revokedToken, expiredToken]);
    renderTab();
    await screen.findByText('North wall TV');

    expect(screen.getByTestId('new-code-1')).toBeEnabled();
    expect(screen.getByTestId('new-code-2')).toBeDisabled();
    expect(screen.getByTestId('new-code-3')).toBeDisabled();
  });

  it('surfaces a re-issue failure as an error toast', async () => {
    mockApi.issueDisplaySetupCode.mockRejectedValue(new Error('boom'));
    renderTab();
    await screen.findByText('North wall TV');

    fireEvent.click(screen.getByTestId('new-code-1'));

    expect(await screen.findByText('boom')).toBeInTheDocument();
    expect(screen.queryByTestId('issued-panel')).not.toBeInTheDocument();
  });

  it('marks a token Expired using UTC parsing of the zone-less expires_at', async () => {
    // The backend serializes naive-UTC datetimes with NO zone suffix. Parsing
    // them as local time would disagree with the server by the UTC offset —
    // a token that expired 1 hour ago must show Expired in every timezone.
    const naiveUtc = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, '');
    const oneHourAgo = naiveUtc(new Date(Date.now() - 60 * 60 * 1000));
    const oneHourAhead = naiveUtc(new Date(Date.now() + 60 * 60 * 1000));
    mockApi.listDisplayTokens.mockResolvedValue([
      { ...activeToken, id: 10, label: 'Just expired TV', expires_at: oneHourAgo },
      { ...activeToken, id: 11, label: 'Still active TV', expires_at: oneHourAhead },
    ]);

    renderTab();

    expect(await screen.findByText('Just expired TV')).toBeInTheDocument();
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
  });

  it('revokes a token after confirmation', async () => {
    mockApi.revokeDisplayToken.mockResolvedValue({ ...activeToken, revoked: true });
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      renderTab();
      fireEvent.click(await screen.findByTestId('revoke-1'));

      await waitFor(() => expect(mockApi.revokeDisplayToken).toHaveBeenCalledWith(1));
      expect(mockApi.listDisplayTokens).toHaveBeenCalledTimes(2); // initial + after revoke
    } finally {
      confirmSpy.mockRestore();
    }
  });
});
