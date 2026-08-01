/**
 * NotFound — smoke test.
 *
 * The one behavior worth pinning beyond content: the way home is a
 * react-router <Link> (SPA navigation), not a raw anchor — a bare
 * `<a href="/">` would force a full app reload.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NotFound from './NotFound';

function renderAtUnknownRoute() {
  return render(
    <MemoryRouter initialEntries={['/definitely-not-a-route']}>
      <Routes>
        <Route path="/" element={<div>DASHBOARD_HOME</div>} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('NotFound', () => {
  it('renders the 404 content', () => {
    renderAtUnknownRoute();
    expect(screen.getByText(/error 404/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /page not found/i })).toBeInTheDocument();
  });

  it('links home via an SPA <Link>, not a full-reload anchor', () => {
    renderAtUnknownRoute();
    const link = screen.getByRole('link', { name: /back to dashboard/i });
    expect(link).toHaveAttribute('href', '/');

    // Clicking must route within the SPA. A raw <a href="/"> would not update
    // the MemoryRouter (it would attempt a full document navigation instead).
    fireEvent.click(link);
    expect(screen.getByText('DASHBOARD_HOME')).toBeInTheDocument();
  });
});
