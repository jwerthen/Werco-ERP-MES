/**
 * DisplayTokensTab — Admin > Wallboard Displays (A0.5).
 *
 * Covers: the token list (active / revoked / expired states), the create
 * flow surfacing the one-time JWT + ready-made /wallboard URL, and revoke.
 * The api service is mocked at the module boundary (AIUsageTab pattern).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import DisplayTokensTab from './DisplayTokensTab';
import api from '../../services/api';
import type { DisplayToken, DisplayTokenIssued } from '../../types/wallboard';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    listDisplayTokens: jest.fn(),
    createDisplayToken: jest.fn(),
    revokeDisplayToken: jest.fn(),
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
};

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.listDisplayTokens.mockResolvedValue([activeToken, revokedToken]);
});

describe('DisplayTokensTab', () => {
  it('lists tokens with their status and hides revoke for revoked ones', async () => {
    render(<DisplayTokensTab />);

    expect(await screen.findByText('North wall TV')).toBeInTheDocument();
    expect(screen.getByText('Old TV')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Revoked')).toBeInTheDocument();
    expect(screen.getByTestId('revoke-1')).toBeInTheDocument();
    expect(screen.queryByTestId('revoke-2')).not.toBeInTheDocument();
  });

  it('creates a token and shows the one-time JWT + wallboard URL', async () => {
    mockApi.createDisplayToken.mockResolvedValue(issued);
    render(<DisplayTokensTab />);
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
    expect((screen.getByTestId('issued-token') as HTMLInputElement).value).toBe('eyJ.display.jwt');
    expect((screen.getByTestId('issued-url') as HTMLInputElement).value).toContain(
      '/wallboard?token=eyJ.display.jwt',
    );
  });

  it('revokes a token after confirmation', async () => {
    mockApi.revokeDisplayToken.mockResolvedValue({ ...activeToken, revoked: true });
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      render(<DisplayTokensTab />);
      fireEvent.click(await screen.findByTestId('revoke-1'));

      await waitFor(() => expect(mockApi.revokeDisplayToken).toHaveBeenCalledWith(1));
      expect(mockApi.listDisplayTokens).toHaveBeenCalledTimes(2); // initial + after revoke
    } finally {
      confirmSpy.mockRestore();
    }
  });
});
