import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';

export type ModalSize = 'sm' | 'md' | 'lg' | 'xl' | '2xl' | '3xl' | '4xl' | '5xl' | '6xl';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  size?: ModalSize;
  closeOnBackdrop?: boolean;
  closeOnEscape?: boolean;
  padded?: boolean;
  scroll?: boolean;
  className?: string;
  // Forwarded to the dialog panel as `aria-labelledby`. Named distinctly from
  // the DOM `aria-labelledby` attribute so it stays explicit at call sites.
  ariaLabelledBy?: string;
  children: React.ReactNode;
}

// Tailwind can only see fully-literal class strings, so the max-width classes
// must be spelled out here rather than built from the `size` prop.
const MAX_WIDTH: Record<ModalSize, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
  '2xl': 'max-w-2xl',
  '3xl': 'max-w-3xl',
  '4xl': 'max-w-4xl',
  '5xl': 'max-w-5xl',
  '6xl': 'max-w-6xl',
};

// Module-level stack of currently-open modals. Only the top entry handles
// Escape and focus-trapping, so stacked/nested modals (e.g. "New Part" opened
// from "Add Item") close one layer at a time and only the frontmost layer keeps
// focus rather than all at once.
const modalStack: symbol[] = [];

// Selector for tabbable elements inside the panel. Used both to seed initial
// focus and to wrap Tab/Shift+Tab at the panel edges.
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function getFocusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => el.offsetParent !== null || el === document.activeElement
  );
}

export function Modal({
  open,
  onClose,
  size = 'lg',
  closeOnBackdrop = true,
  closeOnEscape = true,
  padded = true,
  scroll = true,
  className,
  ariaLabelledBy,
  children,
}: ModalProps) {
  // One stable identity per Modal instance, shared by the stack push/pop effect
  // and the Escape handler. Created lazily so it never changes across the
  // instance's life — crucial for nested modals, where a parent re-render must
  // not let it re-capture a child's token and close both layers on one Escape.
  const tokenRef = useRef<symbol | null>(null);
  if (tokenRef.current === null) tokenRef.current = Symbol('modal');

  // The dialog panel element, used to seed and trap focus.
  const panelRef = useRef<HTMLDivElement | null>(null);

  // Register this instance on the open-modal stack while it is open. Push on
  // open, pop on close/unmount via the effect cleanup. Identity comes from the
  // stable ref, so re-renders never push a new token.
  useEffect(() => {
    if (!open) return;
    const token = tokenRef.current!;
    modalStack.push(token);
    return () => {
      const idx = modalStack.lastIndexOf(token);
      if (idx !== -1) modalStack.splice(idx, 1);
    };
  }, [open]);

  // Escape closes only the topmost open modal. Compares the stable per-instance
  // token against the stack top, so a parent that re-renders under a child does
  // not steal the top slot.
  useEffect(() => {
    if (!open || !closeOnEscape) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (modalStack[modalStack.length - 1] !== tokenRef.current) return; // not topmost
      onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [open, closeOnEscape, onClose]);

  // Focus management: on open, capture the previously-focused element and move
  // focus into the panel. While open, trap Tab/Shift+Tab inside the panel — but
  // only for the topmost modal, so nested modals don't fight over focus. On
  // close/unmount, restore focus to the element that was focused before opening.
  useEffect(() => {
    if (!open) return;
    const token = tokenRef.current;
    const previouslyFocused = document.activeElement as HTMLElement | null;

    // Move initial focus into the panel: first focusable child, else the panel
    // itself (it carries tabIndex={-1}). Deferred a tick so the portal content
    // is mounted before we query it.
    const focusTimer = window.setTimeout(() => {
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = getFocusable(panel);
      (focusable[0] ?? panel).focus();
    }, 0);

    const handleFocusTrap = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      // Only the topmost modal traps focus, so nested modals keep working.
      if (modalStack[modalStack.length - 1] !== token) return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = getFocusable(panel);
      if (focusable.length === 0) {
        // Nothing tabbable — keep focus pinned to the panel.
        e.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (activeEl === first || !panel.contains(activeEl)) {
          e.preventDefault();
          last.focus();
        }
      } else if (activeEl === last || !panel.contains(activeEl)) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleFocusTrap);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener('keydown', handleFocusTrap);
      // Restore focus to the trigger if it is still in the document.
      if (previouslyFocused && typeof previouslyFocused.focus === 'function' && document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    };
  }, [open]);

  if (!open || typeof document === 'undefined') return null;

  const panelClasses = [
    'bg-[#151b28] rounded-xl shadow-xl animate-scale-in w-full',
    MAX_WIDTH[size],
    'max-h-[90vh]',
    scroll ? 'overflow-y-auto' : 'flex flex-col overflow-hidden',
    padded ? 'p-6' : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ');

  const overlay = (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4"
      onClick={() => {
        if (closeOnBackdrop) onClose();
      }}
    >
      <div
        ref={panelRef}
        className={panelClasses}
        role="dialog"
        aria-modal="true"
        aria-labelledby={ariaLabelledBy}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );

  return createPortal(overlay, document.body);
}
