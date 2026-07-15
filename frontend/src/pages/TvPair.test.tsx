/**
 * TvPair — the /tv setup-code pairing screen for the shop wallboard.
 *
 * Covers: the already-paired redirect (stored DISPLAY token only — a signed-in
 * user's session token must NOT count); the /tv/:code immediate claim with
 * normalization (lowercase/dashes/spaces → 8 uppercase chars); typed-input
 * normalization + Enter-to-submit (TV remotes act as keyboards); claim success
 * persisting the token + dept and navigating to /wallboard(?dept=); the
 * generic "code not recognized" retry state; and the distinct network-failure
 * message.
 *
 * services/wallboardClient is mocked at the module boundary — this page must
 * never touch the global axios client (the claim endpoint is public and the
 * claimed credential must never enter axios auth state).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import TvPair from './TvPair';
import {
  claimDisplayCode,
  getPersistedWallboardDept,
  getStoredDisplayToken,
  persistWallboardToken,
} from '../services/wallboardClient';

jest.mock('../services/wallboardClient', () => {
  const actual = jest.requireActual('../services/wallboardClient');
  return {
    __esModule: true,
    ...actual,
    claimDisplayCode: jest.fn(),
    persistWallboardToken: jest.fn(),
    getStoredDisplayToken: jest.fn(() => null),
    getPersistedWallboardDept: jest.fn(() => null),
  };
});

const mockClaim = claimDisplayCode as jest.MockedFunction<typeof claimDisplayCode>;
const mockPersist = persistWallboardToken as jest.MockedFunction<typeof persistWallboardToken>;
const mockGetStored = getStoredDisplayToken as jest.MockedFunction<typeof getStoredDisplayToken>;
const mockGetDept = getPersistedWallboardDept as jest.MockedFunction<typeof getPersistedWallboardDept>;

const claim = {
  token: 'claimed-jwt',
  dept: null as string | null,
  label: 'North TV',
  expires_at: '2027-01-01T00:00:00Z',
};

/** Marker route that records where the app navigated (path + query). */
function WallboardMarker() {
  const location = useLocation();
  return <div data-testid="wallboard-marker">{`${location.pathname}${location.search}`}</div>;
}

function renderTvPair(initialPath = '/tv') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/tv" element={<TvPair />} />
        <Route path="/tv/:code" element={<TvPair />} />
        <Route path="/wallboard" element={<WallboardMarker />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetStored.mockReturnValue(null);
  mockGetDept.mockReturnValue(null);
});

describe('TvPair', () => {
  it('redirects straight to /wallboard when a display token is already stored (safe as TV homepage)', async () => {
    mockGetStored.mockReturnValue('stored-display-jwt');
    mockGetDept.mockReturnValue('weld');

    renderTvPair('/tv');

    expect(await screen.findByTestId('wallboard-marker')).toHaveTextContent('/wallboard?dept=weld');
    expect(mockClaim).not.toHaveBeenCalled();
  });

  it('does NOT treat a signed-in user session as a paired display (helper returns null → form shows)', async () => {
    renderTvPair('/tv');

    expect(await screen.findByTestId('tv-pair-form')).toBeInTheDocument();
    expect(screen.queryByTestId('wallboard-marker')).not.toBeInTheDocument();
  });

  it('claims immediately from the /tv/:code path param, normalized, showing a Pairing state', async () => {
    let resolveClaim: (value: typeof claim) => void = () => {};
    mockClaim.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveClaim = resolve;
        }),
    );

    renderTvPair('/tv/ab-cd12ef');

    expect(await screen.findByTestId('tv-pair-pairing')).toBeInTheDocument();
    expect(mockClaim).toHaveBeenCalledWith('ABCD12EF');

    resolveClaim(claim);
    expect(await screen.findByTestId('wallboard-marker')).toHaveTextContent('/wallboard');
  });

  it('normalizes typed input (lowercase, dashes, spaces) and submits on Enter', async () => {
    mockClaim.mockResolvedValue(claim);
    renderTvPair('/tv');

    const input = await screen.findByLabelText(/setup code/i);
    fireEvent.change(input, { target: { value: 'ab-cd 12ef' } });
    // Auto-uppercased and grouped for TV legibility.
    expect((input as HTMLInputElement).value).toBe('ABCD-12EF');

    fireEvent.submit(screen.getByTestId('tv-pair-form'));

    await waitFor(() => expect(mockClaim).toHaveBeenCalledWith('ABCD12EF'));
  });

  it('keeps Connect disabled until 8 code characters are present', async () => {
    renderTvPair('/tv');

    const button = await screen.findByTestId('tv-pair-connect');
    expect(button).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/setup code/i), { target: { value: 'ABCD-12E' } });
    expect(button).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/setup code/i), { target: { value: 'ABCD-12EF' } });
    expect(button).toBeEnabled();
  });

  it('on success persists the token + dept and navigates to /wallboard?dept=', async () => {
    mockClaim.mockResolvedValue({ ...claim, dept: 'machining' });
    renderTvPair('/tv');

    fireEvent.change(await screen.findByLabelText(/setup code/i), { target: { value: 'ABCD12EF' } });
    fireEvent.click(screen.getByTestId('tv-pair-connect'));

    expect(await screen.findByTestId('wallboard-marker')).toHaveTextContent('/wallboard?dept=machining');
    expect(mockPersist).toHaveBeenCalledWith('claimed-jwt', 'machining');
  });

  it('shows the generic rejection message and clears the input for retry', async () => {
    mockClaim.mockRejectedValue(new Error('CLAIM_REJECTED'));
    renderTvPair('/tv');

    const input = await screen.findByLabelText(/setup code/i);
    fireEvent.change(input, { target: { value: 'ABCD12EF' } });
    fireEvent.click(screen.getByTestId('tv-pair-connect'));

    expect(await screen.findByTestId('tv-pair-error')).toHaveTextContent(
      /code not recognized — codes expire after 15 minutes and work once/i,
    );
    // The form remounts after the "Pairing…" interstitial — re-query the input.
    expect((screen.getByLabelText(/setup code/i) as HTMLInputElement).value).toBe('');
    expect(mockPersist).not.toHaveBeenCalled();
    expect(screen.queryByTestId('wallboard-marker')).not.toBeInTheDocument();
  });

  it('shows a distinct message when the server is unreachable', async () => {
    mockClaim.mockRejectedValue(new Error('NETWORK'));
    renderTvPair('/tv/ABCD12EF');

    expect(await screen.findByTestId('tv-pair-error')).toHaveTextContent(/can't reach the server/i);
    // Recovers to the input form so the code can be retried once the network is back.
    expect(screen.getByTestId('tv-pair-form')).toBeInTheDocument();
  });
});
