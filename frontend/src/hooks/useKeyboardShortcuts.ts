/**
 * useKeyboardShortcuts Hook
 * 
 * Provides keyboard shortcut functionality for the application.
 * Supports global shortcuts and context-specific shortcuts.
 */

import { useEffect, useCallback, useRef } from 'react';

export interface KeyboardShortcut {
  key: string;
  ctrl?: boolean;
  shift?: boolean;
  alt?: boolean;
  meta?: boolean;
  description: string;
  action: () => void;
  /** If true, prevents default browser behavior */
  preventDefault?: boolean;
  /** If true, only triggers when no input is focused */
  ignoreWhenInputFocused?: boolean;
}

interface UseKeyboardShortcutsOptions {
  /** Whether shortcuts are enabled (default: true) */
  enabled?: boolean;
  /** Only trigger shortcuts when no input element is focused */
  ignoreInputFocus?: boolean;
}

const isInputElement = (element: Element | null): boolean => {
  if (!element) return false;
  const tagName = element.tagName.toLowerCase();
  return (
    tagName === 'input' ||
    tagName === 'textarea' ||
    tagName === 'select' ||
    (element as HTMLElement).isContentEditable
  );
};

const normalizeKey = (key: string): string => {
  return key.toLowerCase();
};

/**
 * Hook for handling keyboard shortcuts
 * 
 * @example
 * ```tsx
 * useKeyboardShortcuts([
 *   {
 *     key: 'n',
 *     ctrl: true,
 *     description: 'Create new item',
 *     action: () => setShowCreateModal(true),
 *     preventDefault: true,
 *   },
 *   {
 *     key: 'Escape',
 *     description: 'Close modal',
 *     action: () => setModalOpen(false),
 *   },
 * ]);
 * ```
 */
export function useKeyboardShortcuts(
  shortcuts: KeyboardShortcut[],
  options: UseKeyboardShortcutsOptions = {}
) {
  const { enabled = true, ignoreInputFocus = true } = options;
  const shortcutsRef = useRef(shortcuts);
  
  // Update ref when shortcuts change
  useEffect(() => {
    shortcutsRef.current = shortcuts;
  }, [shortcuts]);

  const handleKeyDown = useCallback((event: KeyboardEvent) => {
    if (!enabled) return;

    const activeElement = document.activeElement;
    const isInInput = isInputElement(activeElement);

    for (const shortcut of shortcutsRef.current) {
      const keyMatches = normalizeKey(event.key) === normalizeKey(shortcut.key);
      const ctrlMatches = shortcut.ctrl ? (event.ctrlKey || event.metaKey) : !event.ctrlKey && !event.metaKey;
      const shiftMatches = shortcut.shift ? event.shiftKey : !event.shiftKey;
      const altMatches = shortcut.alt ? event.altKey : !event.altKey;

      if (keyMatches && ctrlMatches && shiftMatches && altMatches) {
        // Check if we should ignore when input is focused
        const shouldIgnore = (ignoreInputFocus || shortcut.ignoreWhenInputFocused) && isInInput;
        
        // Exception: Escape and certain shortcuts should work even in inputs
        const isEscapeOrGlobal = shortcut.key.toLowerCase() === 'escape' || shortcut.ctrl;
        
        if (shouldIgnore && !isEscapeOrGlobal) {
          continue;
        }

        if (shortcut.preventDefault !== false) {
          event.preventDefault();
        }
        
        shortcut.action();
        return;
      }
    }
  }, [enabled, ignoreInputFocus]);

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [handleKeyDown]);
}

/**
 * Global keyboard shortcuts for the application
 */
export const GLOBAL_SHORTCUTS = {
  NEW: { key: 'n', ctrl: true, description: 'Create new item' },
  SAVE: { key: 's', ctrl: true, description: 'Save changes' },
  ESCAPE: { key: 'Escape', description: 'Close modal / Cancel' },
  HELP: { key: '/', ctrl: true, description: 'Show keyboard shortcuts' },
  SEARCH: { key: 'k', ctrl: true, description: 'Open global search' },
  REFRESH: { key: 'r', ctrl: true, shift: true, description: 'Refresh data' },
} as const;

/**
 * Format a shortcut for display
 */
export function formatShortcut(shortcut: Partial<KeyboardShortcut>): string {
  const parts: string[] = [];
  
  const isMac = typeof navigator !== 'undefined' && navigator.platform.toUpperCase().indexOf('MAC') >= 0;
  
  if (shortcut.ctrl) {
    parts.push(isMac ? '⌘' : 'Ctrl');
  }
  if (shortcut.alt) {
    parts.push(isMac ? '⌥' : 'Alt');
  }
  if (shortcut.shift) {
    parts.push('Shift');
  }
  
  // Format special keys
  let key = shortcut.key || '';
  switch (key.toLowerCase()) {
    case 'escape':
      key = 'Esc';
      break;
    case 'arrowup':
      key = '↑';
      break;
    case 'arrowdown':
      key = '↓';
      break;
    case 'arrowleft':
      key = '←';
      break;
    case 'arrowright':
      key = '→';
      break;
    case 'enter':
      key = '↵';
      break;
    case ' ':
      key = 'Space';
      break;
    default:
      key = key.toUpperCase();
  }
  
  parts.push(key);
  
  return parts.join(isMac ? '' : '+');
}

export default useKeyboardShortcuts;
