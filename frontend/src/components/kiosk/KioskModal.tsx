import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { XMarkIcon } from '@heroicons/react/24/outline';

/**
 * Kiosk overlay primitive (Foundry chrome — 1c/1d/1f/1g).
 *
 * Deliberately LOCAL rather than the shared `components/ui/Modal`: the shared
 * primitive hard-codes `rounded-xl` panels, a `bg-black/50` scrim and generic
 * padding — the kiosk overlays need the exact handoff chrome (4px radius,
 * emphasis border, `rgba(4,6,10,.72)` scrim, `0 24px 80px` shadow, optional
 * 2px accent top edge) with fixed pixel widths. The a11y behaviors mirror the
 * shared Modal: focus moves into the panel on open, Tab/Shift+Tab wrap inside
 * it, Escape closes, a tap on the scrim closes (cancel semantics), and focus
 * restores to the trigger on close. Kiosk overlays never nest, so no stack
 * bookkeeping is needed.
 *
 * The panel portals to document.body — OUTSIDE the page's `.fd-scope-kiosk`
 * wrapper — so the scope class is re-applied on the overlay root; without it
 * the fd-* utilities would resolve to the global (lighter) palette.
 */

interface KioskModalProps {
  onClose: () => void;
  /** Panel width, e.g. 'w-[620px]' (max-w keeps it usable in portrait). */
  widthClassName: string;
  /** Optional 2px accent top edge, e.g. 'border-t-2 border-t-fd-amber'. */
  topEdgeClassName?: string;
  ariaLabelledBy: string;
  children: React.ReactNode;
}

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

export default function KioskModal({
  onClose,
  widthClassName,
  topEdgeClassName = '',
  ariaLabelledBy,
  children,
}: KioskModalProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Escape closes (cancel semantics).
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // Focus into the panel on open; trap Tab inside; restore focus on close.
  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const focusTimer = window.setTimeout(() => {
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = getFocusable(panel);
      (focusable[0] ?? panel).focus();
    }, 0);

    const handleFocusTrap = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = getFocusable(panel);
      if (focusable.length === 0) {
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
      if (previouslyFocused && typeof previouslyFocused.focus === 'function' && document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    };
  }, []);

  if (typeof document === 'undefined') return null;

  const overlay = (
    // Scrim tap = cancel; keyboard users dismiss via Escape + the close button.
    <div
      className="fd-scope-kiosk fixed inset-0 z-[60] flex items-center justify-center bg-[rgba(4,6,10,0.72)] p-4"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={ariaLabelledBy}
        tabIndex={-1}
        className={`max-h-[92vh] w-full overflow-y-auto rounded-[4px] border border-fd-line-bright bg-fd-panel shadow-[0_24px_80px_rgba(0,0,0,0.6)] ${topEdgeClassName} ${widthClassName}`}
      >
        {children}
      </div>
    </div>
  );

  return createPortal(overlay, document.body);
}

/** Shared 40px close button for the kiosk modal headers. */
export function KioskModalClose({ onClose, disabled }: { onClose: () => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      aria-label="Close"
      disabled={disabled}
      onClick={onClose}
      className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[3px] border border-fd-line text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
    >
      <XMarkIcon className="h-[18px] w-[18px]" aria-hidden="true" />
    </button>
  );
}
