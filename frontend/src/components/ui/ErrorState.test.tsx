/**
 * ErrorState Component Tests
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { ErrorState } from './ErrorState';

describe('ErrorState', () => {
  it('renders the default title', () => {
    render(<ErrorState />);
    expect(screen.getByText("Couldn't load this")).toBeInTheDocument();
  });

  it('renders a custom title', () => {
    render(<ErrorState title="Failed to load orders" />);
    expect(screen.getByText('Failed to load orders')).toBeInTheDocument();
  });

  it('renders the message when provided', () => {
    render(<ErrorState message="Network request failed." />);
    expect(screen.getByText('Network request failed.')).toBeInTheDocument();
  });

  it('omits the message when not provided', () => {
    render(<ErrorState title="Boom" />);
    // Only the title paragraph should render, no message paragraph.
    expect(screen.getByRole('alert').querySelectorAll('p')).toHaveLength(1);
  });

  it('renders a Retry button and fires onRetry', () => {
    const onRetry = jest.fn();
    render(<ErrorState onRetry={onRetry} />);
    const button = screen.getByRole('button', { name: 'Retry' });
    fireEvent.click(button);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('uses a custom retry label', () => {
    const onRetry = jest.fn();
    render(<ErrorState onRetry={onRetry} retryLabel="Try again" />);
    expect(
      screen.getByRole('button', { name: 'Try again' })
    ).toBeInTheDocument();
  });

  it('does not render a Retry button when onRetry is absent', () => {
    render(<ErrorState message="No retry here" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('exposes an alert role for assistive tech', () => {
    render(<ErrorState />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('applies a custom className', () => {
    render(<ErrorState className="custom-class" />);
    expect(screen.getByTestId('error-state')).toHaveClass('custom-class');
  });
});
