/**
 * Modal — the shared portaled dialog primitive.
 *
 * Covers the behavior phase-2 migrations depend on: it portals to document.body
 * and renders nothing when closed, the canonical chrome + size mapping, the
 * backdrop / Escape close semantics with their opt-out props, and the
 * open-modal stack that makes Escape close only the topmost of stacked modals.
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { Modal } from './Modal';

describe('Modal', () => {
  it('renders nothing when closed', () => {
    render(
      <Modal open={false} onClose={jest.fn()}>
        <p>Hidden content</p>
      </Modal>,
    );
    expect(screen.queryByText('Hidden content')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('portals to document.body with canonical chrome and the default lg width', () => {
    render(
      <Modal open onClose={jest.fn()}>
        <p>Body</p>
      </Modal>,
    );
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(document.body.contains(dialog)).toBe(true);
    expect(dialog).toHaveClass('bg-[#151b28]', 'rounded-xl', 'shadow-xl', 'animate-scale-in', 'max-w-lg', 'p-6', 'overflow-y-auto');
  });

  it('maps each size to its literal max-w class', () => {
    const { rerender } = render(
      <Modal open onClose={jest.fn()} size="sm">
        <p>x</p>
      </Modal>,
    );
    expect(screen.getByRole('dialog')).toHaveClass('max-w-sm');
    rerender(
      <Modal open onClose={jest.fn()} size="3xl">
        <p>x</p>
      </Modal>,
    );
    expect(screen.getByRole('dialog')).toHaveClass('max-w-3xl');
  });

  it('uses the flex-column / no-padding layout when scroll and padded are off', () => {
    render(
      <Modal open onClose={jest.fn()} scroll={false} padded={false}>
        <p>x</p>
      </Modal>,
    );
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveClass('flex', 'flex-col', 'overflow-hidden');
    expect(dialog).not.toHaveClass('overflow-y-auto', 'p-6');
  });

  it('appends extra className to the panel', () => {
    render(
      <Modal open onClose={jest.fn()} className="border border-slate-700">
        <p>x</p>
      </Modal>,
    );
    expect(screen.getByRole('dialog')).toHaveClass('border', 'border-slate-700');
  });

  it('closes on backdrop click by default but not when content is clicked', () => {
    const onClose = jest.fn();
    render(
      <Modal open onClose={onClose}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.click(screen.getByText('Body'));
    expect(onClose).not.toHaveBeenCalled();
    // The overlay is the dialog's parent.
    fireEvent.click(screen.getByRole('dialog').parentElement!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not close on backdrop click when closeOnBackdrop is false', () => {
    const onClose = jest.fn();
    render(
      <Modal open onClose={onClose} closeOnBackdrop={false}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.click(screen.getByRole('dialog').parentElement!);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on Escape by default', () => {
    const onClose = jest.fn();
    render(
      <Modal open onClose={onClose}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not close on Escape when closeOnEscape is false', () => {
    const onClose = jest.fn();
    render(
      <Modal open onClose={onClose} closeOnEscape={false}>
        <p>Body</p>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });

  it('Escape closes only the topmost modal when stacked', () => {
    const onCloseParent = jest.fn();
    const onCloseChild = jest.fn();
    render(
      <>
        <Modal open onClose={onCloseParent}>
          <p>Parent</p>
        </Modal>
        <Modal open onClose={onCloseChild}>
          <p>Child</p>
        </Modal>
      </>,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCloseChild).toHaveBeenCalledTimes(1);
    expect(onCloseParent).not.toHaveBeenCalled();
  });

  it('Escape closes only the child when opening it re-renders the parent (regression)', () => {
    // Reproduces the original stacked-modal bug: opening the child sets parent
    // state, so the PARENT re-renders while the child is mounted, and the parent
    // passes a fresh inline-arrow onClose each render (as real call sites do).
    // With the previous unstable-token logic that parent re-render re-captured
    // the child's stack slot, so one Escape closed BOTH modals. The stable-ref
    // token fix keeps the child topmost — only the child closes.
    const onCloseParent = jest.fn();
    const onCloseChild = jest.fn();

    function Harness() {
      const [childOpen, setChildOpen] = React.useState(false);
      return (
        // Inline arrow recreated every render: its identity changes on each
        // parent re-render, mimicking real call sites.
        <Modal open onClose={() => onCloseParent()}>
          <p>Parent</p>
          <button type="button" onClick={() => setChildOpen(true)}>
            Open child
          </button>
          <Modal open={childOpen} onClose={() => onCloseChild()}>
            <p>Child</p>
          </Modal>
        </Modal>
      );
    }

    render(<Harness />);

    // Opening the child sets parent state -> the parent Modal re-renders.
    fireEvent.click(screen.getByText('Open child'));
    expect(screen.getByText('Child')).toBeInTheDocument();

    // Topmost (child) should close; parent must stay open.
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCloseChild).toHaveBeenCalledTimes(1);
    expect(onCloseParent).not.toHaveBeenCalled();
  });

  it('Escape pops back to the parent after the child closes (regression)', () => {
    // After the topmost (child) closes, the next Escape should reach the parent,
    // proving the stack pops back correctly rather than leaving a stale top.
    const onCloseParent = jest.fn();

    function Harness() {
      const [childOpen, setChildOpen] = React.useState(false);
      return (
        <Modal open onClose={() => onCloseParent()}>
          <p>Parent</p>
          <button type="button" onClick={() => setChildOpen(true)}>
            Open child
          </button>
          {/* Child close is driven by state so the stack actually pops. */}
          <Modal open={childOpen} onClose={() => setChildOpen(false)}>
            <p>Child</p>
          </Modal>
        </Modal>
      );
    }

    render(<Harness />);

    fireEvent.click(screen.getByText('Open child'));
    expect(screen.getByText('Child')).toBeInTheDocument();

    // First Escape closes the child only.
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByText('Child')).not.toBeInTheDocument();
    expect(onCloseParent).not.toHaveBeenCalled();

    // Second Escape now reaches the parent.
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCloseParent).toHaveBeenCalledTimes(1);
  });
});
