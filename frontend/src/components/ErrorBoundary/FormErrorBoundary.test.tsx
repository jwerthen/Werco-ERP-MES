/**
 * FormErrorBoundary — honest clipboard feedback, no alert().
 *
 * The Copy Data action used to alert('Form data copied to clipboard!') on BOTH
 * paths — including the execCommand fallback whose boolean result it ignored,
 * so a failed copy still claimed success. Now the outcome renders as inline
 * status text inside the existing role="alert" panel: 'copied' only when the
 * async clipboard API resolved or execCommand('copy') returned true, 'failed'
 * otherwise, and window.alert is never called.
 *
 * Harness mirrors ErrorBoundary.test.tsx (errorLogging stubbed, console.error
 * silenced around the thrown render).
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { FormErrorBoundary } from './FormErrorBoundary';

// logError posts via sendBeacon/fetch; stub it so the boundary test makes no network call.
jest.mock('../../services/errorLogging', () => ({ logError: jest.fn() }));

function Boom(): React.ReactElement {
  throw new Error('form render blew up');
}

const PRESERVED = { customer: 'Acme Aerospace', quantity: 12 };

function renderBoundary() {
  // The boundary recovers preserved data from localStorage in componentDidCatch;
  // seeding it makes the Copy Data button render.
  localStorage.setItem('form_backup_Quote', JSON.stringify(PRESERVED));
  return render(
    <FormErrorBoundary formName="Quote">
      <Boom />
    </FormErrorBoundary>
  );
}

describe('FormErrorBoundary clipboard status', () => {
  let consoleError: jest.SpyInstance;
  let alertSpy: jest.SpyInstance;
  let writeText: jest.Mock;

  beforeEach(() => {
    localStorage.clear();
    // React logs the caught render error to console.error; keep test output clean.
    consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    alertSpy = jest.spyOn(window, 'alert').mockImplementation(() => {});
    writeText = jest.fn();
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
  });

  afterEach(() => {
    consoleError.mockRestore();
    alertSpy.mockRestore();
  });

  it('renders the alert panel with recovery actions when the form throws', () => {
    renderBoundary();

    const panel = screen.getByRole('alert');
    expect(panel).toHaveTextContent('Error in Quote Form');
    expect(panel).toHaveTextContent('Form data preserved');
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /copy data/i })).toBeInTheDocument();
    // No status text until a copy is attempted.
    expect(screen.queryByText('Copied to clipboard')).not.toBeInTheDocument();
    expect(screen.queryByText('Copy failed')).not.toBeInTheDocument();
  });

  it('shows "Copied to clipboard" inside the alert panel on success — never an alert()', async () => {
    writeText.mockResolvedValue(undefined);
    renderBoundary();

    fireEvent.click(screen.getByRole('button', { name: /copy data/i }));

    expect(await screen.findByText('Copied to clipboard')).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith(JSON.stringify(PRESERVED, null, 2));
    // The status lives inside the announced panel, so it needs no alert().
    expect(screen.getByRole('alert')).toHaveTextContent('Copied to clipboard');
    expect(alertSpy).not.toHaveBeenCalled();
  });

  it('shows "Copy failed" when the clipboard API rejects and the execCommand fallback returns false', async () => {
    writeText.mockRejectedValue(new Error('clipboard denied'));
    // jsdom has no execCommand; the component captures its boolean result.
    const execCommand = jest.fn().mockReturnValue(false);
    document.execCommand = execCommand;
    renderBoundary();

    fireEvent.click(screen.getByRole('button', { name: /copy data/i }));

    expect(await screen.findByText('Copy failed')).toBeInTheDocument();
    expect(execCommand).toHaveBeenCalledWith('copy');
    // A failed copy must never claim success, in any form.
    expect(screen.queryByText('Copied to clipboard')).not.toBeInTheDocument();
    expect(alertSpy).not.toHaveBeenCalled();
    // The fallback textarea must not leak into the DOM (try/finally cleanup).
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('shows "Copied to clipboard" when the fallback execCommand actually succeeds', async () => {
    writeText.mockRejectedValue(new Error('no async clipboard'));
    const execCommand = jest.fn().mockReturnValue(true);
    document.execCommand = execCommand;
    renderBoundary();

    fireEvent.click(screen.getByRole('button', { name: /copy data/i }));

    // execCommand returning true IS a success — the fallback must not
    // hard-code a failure just because the async API was unavailable.
    expect(await screen.findByText('Copied to clipboard')).toBeInTheDocument();
    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(screen.queryByText('Copy failed')).not.toBeInTheDocument();
    expect(alertSpy).not.toHaveBeenCalled();
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('clears the copy status when Try Again re-arms the boundary', async () => {
    writeText.mockResolvedValue(undefined);
    renderBoundary();

    fireEvent.click(screen.getByRole('button', { name: /copy data/i }));
    expect(await screen.findByText('Copied to clipboard')).toBeInTheDocument();

    // Try Again with a still-throwing child re-catches the error — the
    // re-rendered panel must start with a FRESH (idle) copy status.
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Error in Quote Form');
    expect(screen.getByRole('button', { name: /copy data/i })).toBeInTheDocument();
    expect(screen.queryByText('Copied to clipboard')).not.toBeInTheDocument();
    expect(screen.queryByText('Copy failed')).not.toBeInTheDocument();
  });
});
