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
    render(<LoadingButton loading>Click Me</LoadingButton>);
    expect(screen.getByRole('status', { name: /loading/i })).toBeInTheDocument();
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
      expect(screen.getByRole('button')).toHaveClass('bg-slate-700');
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

  describe('focusableWhileLoading', () => {
    describe('off (the default every existing caller gets)', () => {
      it('disables natively while loading and adds no aria-disabled of its own', () => {
        const handleClick = jest.fn();
        render(
          <LoadingButton loading onClick={handleClick}>
            Save
          </LoadingButton>
        );
        const button = screen.getByRole('button');
        expect(button).toBeDisabled();
        expect(button).not.toHaveAttribute('aria-disabled');
        fireEvent.click(button);
        expect(handleClick).not.toHaveBeenCalled();
      });

      it("passes a caller's own aria-disabled and onClick straight through", () => {
        const handleClick = jest.fn();
        render(
          <LoadingButton aria-disabled="true" onClick={handleClick}>
            Save
          </LoadingButton>
        );
        const button = screen.getByRole('button');
        expect(button).toHaveAttribute('aria-disabled', 'true');
        expect(button).toBeEnabled();
        fireEvent.click(button);
        expect(handleClick).toHaveBeenCalledTimes(1);
      });
    });

    describe('on', () => {
      it('changes nothing while not loading', () => {
        const handleClick = jest.fn();
        render(
          <LoadingButton focusableWhileLoading onClick={handleClick}>
            Done
          </LoadingButton>
        );
        const button = screen.getByRole('button', { name: 'Done' });
        expect(button).toBeEnabled();
        expect(button).not.toHaveAttribute('aria-disabled');
        fireEvent.click(button);
        expect(handleClick).toHaveBeenCalledTimes(1);
      });

      it('keeps the focused button focusable while loading: aria-disabled, never natively disabled', () => {
        const handleClick = jest.fn();
        const { rerender } = render(
          <LoadingButton focusableWhileLoading onClick={handleClick}>
            Done
          </LoadingButton>
        );
        const button = screen.getByRole('button', { name: 'Done' });
        button.focus();
        rerender(
          <LoadingButton focusableWhileLoading loading loadingText="Saving…" onClick={handleClick}>
            Done
          </LoadingButton>
        );
        expect(button).not.toBeDisabled();
        expect(button).toHaveAttribute('aria-disabled', 'true');
        expect(button).toHaveFocus();
        // Still the loading button: spinner, loading text and the loading classes.
        expect(screen.getByRole('status', { name: /loading/i })).toBeInTheDocument();
        expect(button).toHaveTextContent('Saving…');
        expect(button).toHaveClass('opacity-75', 'cursor-not-allowed');
        // Settled: an ordinary button again.
        rerender(
          <LoadingButton focusableWhileLoading onClick={handleClick}>
            Done
          </LoadingButton>
        );
        expect(button).toHaveFocus();
        expect(button).not.toHaveAttribute('aria-disabled');
      });

      it('ignores a click while loading, and a submit button does not submit its form', () => {
        const handleClick = jest.fn();
        const handleSubmit = jest.fn((event: React.FormEvent) => event.preventDefault());
        render(
          <form onSubmit={handleSubmit}>
            <LoadingButton type="submit" focusableWhileLoading loading onClick={handleClick}>
              Save
            </LoadingButton>
          </form>
        );
        const button = screen.getByRole('button');
        // fireEvent returns false when a handler prevented the click's default action.
        expect(fireEvent.click(button)).toBe(false);
        expect(handleClick).not.toHaveBeenCalled();
        expect(handleSubmit).not.toHaveBeenCalled();
      });

      it("overrides a caller's aria-disabled=false while loading", () => {
        render(
          <LoadingButton focusableWhileLoading loading aria-disabled={false}>
            Done
          </LoadingButton>
        );
        expect(screen.getByRole('button')).toHaveAttribute('aria-disabled', 'true');
      });

      it('still disables natively when `disabled` is set, loading or not', () => {
        const { rerender } = render(
          <LoadingButton focusableWhileLoading loading disabled>
            Done
          </LoadingButton>
        );
        expect(screen.getByRole('button')).toBeDisabled();
        rerender(
          <LoadingButton focusableWhileLoading disabled>
            Done
          </LoadingButton>
        );
        expect(screen.getByRole('button')).toBeDisabled();
      });
    });
  });
});
