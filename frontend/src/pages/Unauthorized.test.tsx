/**
 * Unauthorized — smoke test.
 *
 * Pins the restyle's contract: the refusal + role explanation render, and
 * navigation is SPA-only (buttons + navigate(), no raw anchors that would
 * force a full reload).
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import Unauthorized from './Unauthorized';

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: 'operator' }),
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/unauthorized']}>
      <Routes>
        <Route path="/" element={<div>DASHBOARD_HOME</div>} />
        <Route path="/unauthorized" element={<Unauthorized />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Unauthorized', () => {
  it('renders the refusal and the current-role explanation', () => {
    renderPage();
    expect(screen.getByRole('heading', { name: /access denied/i })).toBeInTheDocument();
    expect(screen.getByText(/current role/i)).toBeInTheDocument();

    // Navigation is button + navigate() — no raw anchors on this page.
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('SPA-navigates to the dashboard from the primary action', () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: /go to dashboard/i }));
    expect(screen.getByText('DASHBOARD_HOME')).toBeInTheDocument();
  });
});
