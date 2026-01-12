/**
 * LoadingButton Component Tests
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { LoadingButton } from './LoadingButton';

describe('LoadingButton', () => {
  it('renders children when not loading', () => {
    render(<LoadingButton>Click Me</LoadingButton>);
    expect(screen.getByText('Click Me')).toBeInTheDocument();
  });

  it('shows spinner when loading', () => {
    const { container } = render(<LoadingButton loading>Click Me</LoadingButton>);
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('shows loading text when provided', () => {
    render(<LoadingButton loading loadingText="Saving...">Save</LoadingButton>);
    expect(screen.getByText('Saving...')).toBeInTheDocument();
    expect(screen.queryByText('Save')).not.toBeInTheDocument();
  });

  it('is disabled when loading', () => {
    render(<LoadingButton loading>Click Me</LoadingButton>);
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('is disabled when disabled prop is true', () => {
    render(<LoadingButton disabled>Click Me</LoadingButton>);
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('calls onClick when not loading', () => {
    const handleClick = jest.fn();
    render(<LoadingButton onClick={handleClick}>Click Me</LoadingButton>);
    fireEvent.click(screen.getByRole('button'));
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it('does not call onClick when loading', () => {
    const handleClick = jest.fn();
    render(<LoadingButton loading onClick={handleClick}>Click Me</LoadingButton>);
    fireEvent.click(screen.getByRole('button'));
    expect(handleClick).not.toHaveBeenCalled();
  });

  describe('variants', () => {
    it('renders primary variant by default', () => {
      render(<LoadingButton>Primary</LoadingButton>);
      expect(screen.getByRole('button')).toHaveClass('btn-primary');
    });

    it('renders secondary variant', () => {
      render(<LoadingButton variant="secondary">Secondary</LoadingButton>);
      expect(screen.getByRole('button')).toHaveClass('bg-gray-100');
    });

    it('renders danger variant', () => {
      render(<LoadingButton variant="danger">Danger</LoadingButton>);
      expect(screen.getByRole('button')).toHaveClass('bg-red-600');
    });

    it('renders ghost variant', () => {
      render(<LoadingButton variant="ghost">Ghost</LoadingButton>);
      expect(screen.getByRole('button')).toHaveClass('bg-transparent');
    });
  });

  describe('sizes', () => {
    it('renders medium size by default', () => {
      render(<LoadingButton>Medium</LoadingButton>);
      expect(screen.getByRole('button')).toHaveClass('px-4', 'py-2');
    });

    it('renders small size', () => {
      render(<LoadingButton size="sm">Small</LoadingButton>);
      expect(screen.getByRole('button')).toHaveClass('px-3', 'py-1.5', 'text-sm');
    });

    it('renders large size', () => {
      render(<LoadingButton size="lg">Large</LoadingButton>);
      expect(screen.getByRole('button')).toHaveClass('px-6', 'py-3', 'text-lg');
    });
  });

  it('applies custom className', () => {
    render(<LoadingButton className="custom-class">Custom</LoadingButton>);
    expect(screen.getByRole('button')).toHaveClass('custom-class');
  });

  it('passes through other button props', () => {
    render(<LoadingButton type="submit" data-testid="submit-btn">Submit</LoadingButton>);
    const button = screen.getByTestId('submit-btn');
    expect(button).toHaveAttribute('type', 'submit');
  });

  it('has reduced opacity when loading', () => {
    render(<LoadingButton loading>Loading</LoadingButton>);
    expect(screen.getByRole('button')).toHaveClass('opacity-75');
  });

  it('has cursor-not-allowed when loading', () => {
    render(<LoadingButton loading>Loading</LoadingButton>);
    expect(screen.getByRole('button')).toHaveClass('cursor-not-allowed');
  });
});
