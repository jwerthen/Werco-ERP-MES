/**
 * EmptyState Component Tests
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { EmptyState } from './EmptyState';

describe('EmptyState', () => {
  it('renders the title', () => {
    render(<EmptyState title="No work orders" />);
    expect(screen.getByText('No work orders')).toBeInTheDocument();
  });

  it('renders the description when provided', () => {
    render(
      <EmptyState
        title="No work orders"
        description="Released work orders will appear here."
      />
    );
    expect(
      screen.getByText('Released work orders will appear here.')
    ).toBeInTheDocument();
  });

  it('omits the description when not provided', () => {
    const { container } = render(<EmptyState title="Empty" />);
    // Only the title <p> should be present (no description paragraph).
    expect(container.querySelectorAll('p')).toHaveLength(1);
  });

  it('renders an optional icon', () => {
    const Icon = (props: { className?: string }) => (
      <svg data-testid="empty-icon" {...props} />
    );
    render(<EmptyState icon={Icon} title="Empty" />);
    expect(screen.getByTestId('empty-icon')).toBeInTheDocument();
  });

  it('does not render an icon when none is provided', () => {
    const { container } = render(<EmptyState title="Empty" />);
    expect(container.querySelector('svg')).toBeNull();
  });

  it('renders a CTA button and fires onClick', () => {
    const onClick = jest.fn();
    render(
      <EmptyState
        title="No work orders"
        action={{ label: 'New Work Order', onClick }}
      />
    );
    const button = screen.getByRole('button', { name: 'New Work Order' });
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('renders a custom ReactNode action', () => {
    render(
      <EmptyState
        title="Empty"
        action={<a href="/create">Custom action</a>}
      />
    );
    expect(
      screen.getByRole('link', { name: 'Custom action' })
    ).toBeInTheDocument();
  });

  it('does not render an action region when no action is provided', () => {
    render(<EmptyState title="Empty" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('applies a custom className', () => {
    render(<EmptyState title="Empty" className="custom-class" />);
    expect(screen.getByTestId('empty-state')).toHaveClass('custom-class');
  });
});
