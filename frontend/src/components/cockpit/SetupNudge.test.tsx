/**
 * SetupNudge — dismissible "Finish setup" prompt on the Dashboard.
 *
 * Behavior under test:
 *  - RBAC: rendered only for admins.
 *  - Shows live "Finish setup — N% complete" from the setup-health endpoint.
 *  - Hidden once setup is 100% complete.
 *  - Dismiss persists in localStorage and removes the nudge.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SetupNudge from './SetupNudge';

const mockGetSetupHealth = jest.fn();
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getSetupHealth: () => mockGetSetupHealth(),
  },
}));

let mockIsAdmin = true;
jest.mock('../../hooks/usePermissions', () => ({
  __esModule: true,
  usePermissions: () => ({ isAdmin: mockIsAdmin }),
}));

function renderNudge() {
  return render(
    <MemoryRouter>
      <SetupNudge />
    </MemoryRouter>
  );
}

describe('SetupNudge', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    mockIsAdmin = true;
    mockGetSetupHealth.mockResolvedValue({ progress: 40 });
  });

  it('renders the progress nudge for admins', async () => {
    renderNudge();
    expect(await screen.findByText(/Finish setup — 40% complete/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /finish setup/i })).toHaveAttribute('href', '/setup');
  });

  it('renders nothing for non-admins', async () => {
    mockIsAdmin = false;
    const { container } = renderNudge();
    await waitFor(() => expect(mockGetSetupHealth).not.toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('hides when setup is 100% complete', async () => {
    mockGetSetupHealth.mockResolvedValue({ progress: 100 });
    const { container } = renderNudge();
    await waitFor(() => expect(mockGetSetupHealth).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('dismiss removes the nudge and persists the choice', async () => {
    const { container, rerender } = renderNudge();
    await screen.findByText(/Finish setup/i);

    fireEvent.click(screen.getByRole('button', { name: /dismiss setup reminder/i }));
    expect(container).toBeEmptyDOMElement();
    expect(localStorage.getItem('werco-setup-nudge-dismissed')).toBe('1');

    // A fresh mount stays dismissed.
    rerender(
      <MemoryRouter>
        <SetupNudge />
      </MemoryRouter>
    );
    expect(screen.queryByText(/Finish setup/i)).not.toBeInTheDocument();
  });

  it('falls back to a generic prompt when setup-health fails', async () => {
    mockGetSetupHealth.mockRejectedValue(new Error('boom'));
    renderNudge();
    expect(await screen.findByText(/Complete your setup/i)).toBeInTheDocument();
  });
});
