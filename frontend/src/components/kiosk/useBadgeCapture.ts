import { useEffect } from 'react';

/** Longest badge/employee id a wedge scanner can emit (e.g. "EMP-0042"). */
export const MAX_BADGE_LENGTH = 32;
// Printable characters a badge/wedge scanner can emit (employee IDs like "EMP-0042").
const BADGE_CHAR = /^[0-9A-Za-z\-_.]$/;

interface UseBadgeCaptureOptions {
  /**
   * Master switch. EXACTLY ONE enabled consumer may exist at a time — the
   * topmost visible view owns the scanner. Flip this off while a submit is in
   * flight (mirrors the old `if (submitting) return` guard) and whenever a view
   * higher in the stack (e.g. a modal) is capturing badges instead.
   */
  enabled: boolean;
  /** The current badge buffer (owned by the consumer so keypads can share it). */
  value: string;
  /** Buffer updates (scanner keystrokes, Backspace). */
  onValueChange: (next: string) => void;
  /** Fired with the buffer when the scanner sends Enter. */
  onSubmit: (value: string) => void;
  maxLength?: number;
}

/**
 * Window-level badge-scanner capture (extracted from KioskBadgeLogin).
 *
 * A keyboard-wedge scanner "types" the employee id and sends Enter — captured
 * at the window level so it works without any focused input (gloved operators
 * never have to tap a field first). Keyboard shortcuts (Ctrl/Cmd/Alt chords)
 * and in-progress IME composition are not badge input and never pollute the
 * buffer; a modified Enter is a shortcut too and never submits.
 */
export function useBadgeCapture({
  enabled,
  value,
  onValueChange,
  onSubmit,
  maxLength = MAX_BADGE_LENGTH,
}: UseBadgeCaptureOptions): void {
  useEffect(() => {
    if (!enabled) return undefined;

    const handleKeyDown = (event: KeyboardEvent) => {
      // Keyboard shortcuts (Ctrl/Cmd/Alt chords) and in-progress IME composition
      // are not badge input — don't let them pollute the buffer.
      if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
      // Neither is typing into a real field. No consumer feeds this buffer from
      // an <input> today — every badge screen keys through the on-screen
      // KioskKeypad — so this changes nothing for them; it is here so that
      // arming a capture on a screen that also carries a text field (a
      // scrap-detail line, a note) cannot silently turn the operator's typing
      // into a badge buffer and their Enter into a token mint.
      const target = event.target as HTMLElement | null;
      if (target) {
        const tag = target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable) return;
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        onSubmit(value);
        return;
      }
      if (event.key === 'Backspace') {
        onValueChange(value.slice(0, -1));
        return;
      }
      if (BADGE_CHAR.test(event.key)) {
        if (value.length < maxLength) onValueChange(value + event.key);
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [enabled, value, onValueChange, onSubmit, maxLength]);
}
