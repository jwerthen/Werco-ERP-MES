/**
 * Button Component Tests
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { Button } from './Button';

describe('Button', () => {
  it('renders children', () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it('defaults to type="button" so it does not submit forms unexpectedly', () => {
    render(<Button>Action</Button>);
    expect(screen.getByRole('button')).toHaveAttribute('type', 'button');
  });

  it('honors an explicit type', () => {
    render(<Button type="submit">Go</Button>);
    expect(screen.getByRole('button')).toHaveAttribute('type', 'submit');
  });

  describe('variants', () => {
    it('renders primary by default', () => {
      render(<Button>Primary</Button>);
      expect(screen.getByRole('button')).toHaveClass('btn-primary');
    });

    it('renders secondary', () => {
      render(<Button variant="secondary">Secondary</Button>);
      expect(screen.getByRole('button')).toHaveClass('btn-secondary');
    });

    it('renders danger', () => {
      render(<Button variant="danger">Danger</Button>);
      expect(screen.getByRole('button')).toHaveClass('btn-danger');
    });

    it('renders ghost', () => {
      render(<Button variant="ghost">Ghost</Button>);
      expect(screen.getByRole('button')).toHaveClass('btn-ghost');
    });
  });

  describe('sizes', () => {
    it('renders md by default (no btn-sm)', () => {
      render(<Button>Md</Button>);
      expect(screen.getByRole('button')).not.toHaveClass('btn-sm');
    });

    it('renders sm', () => {
      render(<Button size="sm">Sm</Button>);
      expect(screen.getByRole('button')).toHaveClass('btn-sm');
    });
  });

  it('fires onClick', () => {
    const onClick = jest.fn();
    render(<Button onClick={onClick}>Click</Button>);
    fireEvent.click(screen.getByRole('button'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('respects disabled (no click, disabled attribute set)', () => {
    const onClick = jest.fn();
    render(
      <Button disabled onClick={onClick}>
        Click
      </Button>,
    );
    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('applies custom className alongside variant', () => {
    render(<Button className="w-full">Wide</Button>);
    const button = screen.getByRole('button');
    expect(button).toHaveClass('btn-primary', 'w-full');
  });

  it('passes through native button props', () => {
    render(
      <Button data-testid="x" aria-label="do-thing">
        X
      </Button>,
    );
    const button = screen.getByTestId('x');
    expect(button).toHaveAttribute('aria-label', 'do-thing');
  });

  it('forwards a ref to the button element', () => {
    const ref = React.createRef<HTMLButtonElement>();
    render(<Button ref={ref}>Ref</Button>);
    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
  });
});
