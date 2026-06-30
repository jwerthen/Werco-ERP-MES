/**
 * MobileDataCard Component Tests
 *
 * Focus: the card's keyboard-activation contract. A clickable card is a
 * role="button" with tabIndex=0 that activates on Enter/Space, but keystrokes
 * bubbling up from focusable children (action buttons) must NOT activate it —
 * the onKeyDown has an `e.target === e.currentTarget` descendant guard.
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MobileDataCard } from './MobileDataCard';

const fields = [
  { label: 'Status', value: 'Open' },
  { label: 'Qty', value: 42 },
];

describe('MobileDataCard', () => {
  describe('clickable card (onClick provided)', () => {
    it('renders with role="button" and tabIndex=0', () => {
      render(<MobileDataCard title="WO-1001" fields={fields} onClick={jest.fn()} />);
      const card = screen.getByRole('button', { name: /WO-1001/i });
      expect(card).toHaveAttribute('tabindex', '0');
    });

    it('calls onClick on a mouse click', () => {
      const onClick = jest.fn();
      render(<MobileDataCard title="WO-1001" fields={fields} onClick={onClick} />);
      fireEvent.click(screen.getByRole('button', { name: /WO-1001/i }));
      expect(onClick).toHaveBeenCalledTimes(1);
    });

    it('activates once on Enter pressed on the card itself', () => {
      const onClick = jest.fn();
      render(<MobileDataCard title="WO-1001" fields={fields} onClick={onClick} />);
      fireEvent.keyDown(screen.getByRole('button', { name: /WO-1001/i }), { key: 'Enter' });
      expect(onClick).toHaveBeenCalledTimes(1);
    });

    it('activates once on Space pressed on the card itself', () => {
      const onClick = jest.fn();
      render(<MobileDataCard title="WO-1001" fields={fields} onClick={onClick} />);
      fireEvent.keyDown(screen.getByRole('button', { name: /WO-1001/i }), { key: ' ' });
      expect(onClick).toHaveBeenCalledTimes(1);
    });

    it('calls preventDefault for Space (avoids page scroll)', () => {
      const onClick = jest.fn();
      render(<MobileDataCard title="WO-1001" fields={fields} onClick={onClick} />);
      const card = screen.getByRole('button', { name: /WO-1001/i });
      const event = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
      const preventDefault = jest.spyOn(event, 'preventDefault');
      fireEvent(card, event);
      expect(preventDefault).toHaveBeenCalled();
      expect(onClick).toHaveBeenCalledTimes(1);
    });

    it('ignores other keys (e.g. Tab does not activate)', () => {
      const onClick = jest.fn();
      render(<MobileDataCard title="WO-1001" fields={fields} onClick={onClick} />);
      fireEvent.keyDown(screen.getByRole('button', { name: /WO-1001/i }), { key: 'Tab' });
      expect(onClick).not.toHaveBeenCalled();
    });
  });

  describe('non-clickable card (no onClick)', () => {
    it('renders WITHOUT role="button"', () => {
      render(<MobileDataCard title="WO-2002" fields={fields} />);
      expect(screen.queryByRole('button')).not.toBeInTheDocument();
    });

    it('is not focusable (no tabIndex)', () => {
      const { container } = render(<MobileDataCard title="WO-2002" fields={fields} />);
      // The outer wrapper is the first div; it must carry no tabindex.
      const wrapper = container.firstElementChild as HTMLElement;
      expect(wrapper).not.toHaveAttribute('tabindex');
    });
  });

  describe('REGRESSION: keystrokes from action buttons must not activate the card', () => {
    it('does not call card onClick when Enter fires on a child action button', () => {
      const cardOnClick = jest.fn();
      const actionOnClick = jest.fn();
      render(
        <MobileDataCard
          title="WO-3003"
          fields={fields}
          onClick={cardOnClick}
          actions={<button onClick={actionOnClick}>Delete</button>}
        />,
      );
      const actionButton = screen.getByRole('button', { name: 'Delete' });
      // target is the action button, not the card — the descendant guard must bail.
      fireEvent.keyDown(actionButton, { key: 'Enter' });
      expect(cardOnClick).not.toHaveBeenCalled();
    });

    it('does not call card onClick when Space fires on a child action button', () => {
      const cardOnClick = jest.fn();
      render(
        <MobileDataCard
          title="WO-3003"
          fields={fields}
          onClick={cardOnClick}
          actions={<button>Delete</button>}
        />,
      );
      const actionButton = screen.getByRole('button', { name: 'Delete' });
      fireEvent.keyDown(actionButton, { key: ' ' });
      expect(cardOnClick).not.toHaveBeenCalled();
    });
  });
});
