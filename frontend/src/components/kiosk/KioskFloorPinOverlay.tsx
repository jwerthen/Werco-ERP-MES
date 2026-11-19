import React, { useEffect, useId, useLayoutEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import FloorPinPad from '../auth/FloorPinPad';
import type { FloorPinPromptState } from '../../hooks/useFloorPinPrompt';

interface KioskFloorPinOverlayProps {
  /** The open challenge from `useFloorPinPrompt`, or null for no overlay. */
  prompt: FloorPinPromptState | null;
  onSubmit: (pin: string) => void;
  onCancel: () => void;
  /** The scanned badge, shown on the pad. */
  badgeLabel?: string | null;
}

const FOCUSABLE_SELECTOR = 'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * The floor-PIN pad over a kiosk screen (solo kiosk sign-in and every crew-station
 * scan). Rendered for the one scan that drew the `floor_pin_required` challenge; it
 * closes when the sign-on ends. Scrim tap and Escape are Cancel — except while a
 * PIN is with the server, when nothing dismisses it underneath the answer.
 *
 * Deliberately NOT built on KioskModal: the crew station's COMPLETE signature is a
 * badge scan inside a KioskModal, so this pad can open ON TOP of one, and
 * KioskModal assumes overlays never nest (both would close on one Escape and fight
 * over Tab). So this layer sits one z-step above it (z-[70] over z-[60]) and owns
 * Escape and Tab in the window CAPTURE phase — stopped there, they never reach the
 * dialog underneath. Every other key (digits, Backspace, Enter) passes through to
 * the field and to the idle timer's activity listener as normal.
 */
export default function KioskFloorPinOverlay({ prompt, onSubmit, onCancel, badgeLabel }: KioskFloorPinOverlayProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const open = prompt !== null;
  const verifying = prompt?.verifying ?? false;
  const stateRef = useRef({ verifying, onCancel });
  stateRef.current = { verifying, onCancel };
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  // Record where focus was BEFORE the pad takes it. A layout effect runs before every
  // passive effect, including FloorPinPad's mount effect that focuses the PIN field, so
  // this sees the element the operator was on; a passive effect here would see the field.
  useLayoutEffect(() => {
    if (open) restoreFocusRef.current = document.activeElement as HTMLElement | null;
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const previouslyFocused = restoreFocusRef.current;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        event.preventDefault();
        if (!stateRef.current.verifying) stateRef.current.onCancel();
        return;
      }
      if (event.key !== 'Tab') return;
      event.stopPropagation();
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (event.shiftKey) {
        if (active === first || !panel.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || !panel.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener('keydown', handleKeyDown, true);
    return () => {
      window.removeEventListener('keydown', handleKeyDown, true);
      if (previouslyFocused && typeof previouslyFocused.focus === 'function' && document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    };
  }, [open]);

  if (!prompt || typeof document === 'undefined') return null;

  return createPortal(
    // Scrim tap = Cancel (Escape is the keyboard path, handled above). The portal
    // leaves the page's `.fd-scope-kiosk` wrapper, so the scope is re-applied here.
    <div
      className="fd-scope-kiosk fixed inset-0 z-[70] flex items-center justify-center bg-[rgba(4,6,10,0.72)] p-4"
      role="presentation"
      onClick={e => {
        if (e.target === e.currentTarget && !prompt.verifying) onCancel();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        data-testid="kiosk-floor-pin-dialog"
        className="max-h-[92vh] w-full max-w-[420px] overflow-y-auto rounded-[4px] border border-t-2 border-fd-line-bright border-t-fd-blue bg-fd-panel shadow-[0_24px_80px_rgba(0,0,0,0.6)]"
      >
        <FloorPinPad
          variant="kiosk"
          error={prompt.error}
          verifying={prompt.verifying}
          attempt={prompt.attempt}
          onSubmit={onSubmit}
          onCancel={onCancel}
          badgeLabel={badgeLabel}
          titleId={titleId}
        />
      </div>
    </div>,
    document.body
  );
}
