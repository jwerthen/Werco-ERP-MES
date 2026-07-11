/**
 * Global error-boundary safety net.
 *
 * App.tsx wraps the ENTIRE tree (above every provider, including ToastProvider) in
 * `<ErrorBoundary level="global">`. Before that, a render error thrown above the
 * router's page-level boundary — e.g. a raw 422 detail array rendered inside a toast —
 * unmounted the whole SPA to a blank #root. This guards that a render error now yields
 * the friendly full-page fallback instead of nothing.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { ErrorBoundary } from './ErrorBoundary';

// logError posts via sendBeacon/fetch; stub it so the boundary test makes no network call.
jest.mock('../../services/errorLogging', () => ({ logError: jest.fn() }));

function Boom(): React.ReactElement {
  throw new Error('Objects are not valid as a React child');
}

describe('ErrorBoundary (global level)', () => {
  let consoleError: jest.SpyInstance;
  beforeEach(() => {
    // React logs the caught render error to console.error; keep test output clean.
    consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => consoleError.mockRestore());

  it('renders a full-page fallback with a reload affordance instead of unmounting', () => {
    render(
      <ErrorBoundary level="global" name="App">
        <Boom />
      </ErrorBoundary>,
    );

    // Something is on screen (the tree did NOT unmount to empty) and it offers recovery.
    expect(document.body.textContent).not.toBe('');
    expect(screen.getByRole('button', { name: /reload|refresh|try again/i })).toBeInTheDocument();
  });

  it('renders children normally when nothing throws', () => {
    render(
      <ErrorBoundary level="global" name="App">
        <div>All good</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText('All good')).toBeInTheDocument();
  });
});
