/**
 * KeyboardShortcutsContext
 * 
 * Provides global keyboard shortcuts management and the help modal.
 */

import React, { createContext, useContext, useState, useCallback, ReactNode } from 'react';
import { useKeyboardShortcuts, KeyboardShortcut, GLOBAL_SHORTCUTS } from '../hooks/useKeyboardShortcuts';
import KeyboardShortcutsModal from '../components/KeyboardShortcutsModal';

interface KeyboardShortcutsContextValue {
  /** Show the keyboard shortcuts help modal */
  showHelp: () => void;
  /** Hide the keyboard shortcuts help modal */
  hideHelp: () => void;
  /** Whether the help modal is open */
  isHelpOpen: boolean;
  /** Register page-specific shortcuts (will be shown in help modal) */
  registerPageShortcuts: (shortcuts: Omit<KeyboardShortcut, 'action'>[]) => void;
  /** Clear page-specific shortcuts */
  clearPageShortcuts: () => void;
}

const KeyboardShortcutsContext = createContext<KeyboardShortcutsContextValue | null>(null);

interface KeyboardShortcutsProviderProps {
  children: ReactNode;
  /** Callback when Ctrl+N (new) is pressed */
  onNew?: () => void;
  /** Callback when Ctrl+S (save) is pressed */
  onSave?: () => void;
  /** Callback when Ctrl+K (search) is pressed */
  onSearch?: () => void;
  /** Callback when Ctrl+Shift+R (refresh) is pressed */
  onRefresh?: () => void;
}

export function KeyboardShortcutsProvider({
  children,
  onNew,
  onSave,
  onSearch,
  onRefresh,
}: KeyboardShortcutsProviderProps) {
  const [isHelpOpen, setIsHelpOpen] = useState(false);
  const [pageShortcuts, setPageShortcuts] = useState<Omit<KeyboardShortcut, 'action'>[]>([]);

  const showHelp = useCallback(() => setIsHelpOpen(true), []);
  const hideHelp = useCallback(() => setIsHelpOpen(false), []);

  const registerPageShortcuts = useCallback((shortcuts: Omit<KeyboardShortcut, 'action'>[]) => {
    setPageShortcuts(shortcuts);
  }, []);

  const clearPageShortcuts = useCallback(() => {
    setPageShortcuts([]);
  }, []);

  // Global shortcuts
  const shortcuts: KeyboardShortcut[] = [
    {
      ...GLOBAL_SHORTCUTS.HELP,
      action: showHelp,
      preventDefault: true,
    },
    {
      ...GLOBAL_SHORTCUTS.ESCAPE,
      action: () => {
        if (isHelpOpen) {
          hideHelp();
        }
      },
    },
  ];

  // Add optional global shortcuts
  if (onNew) {
    shortcuts.push({
      ...GLOBAL_SHORTCUTS.NEW,
      action: onNew,
      preventDefault: true,
    });
  }

  if (onSave) {
    shortcuts.push({
      ...GLOBAL_SHORTCUTS.SAVE,
      action: onSave,
      preventDefault: true,
    });
  }

  if (onSearch) {
    shortcuts.push({
      ...GLOBAL_SHORTCUTS.SEARCH,
      action: onSearch,
      preventDefault: true,
    });
  }

  if (onRefresh) {
    shortcuts.push({
      ...GLOBAL_SHORTCUTS.REFRESH,
      action: onRefresh,
      preventDefault: true,
    });
  }

  useKeyboardShortcuts(shortcuts);

  const value: KeyboardShortcutsContextValue = {
    showHelp,
    hideHelp,
    isHelpOpen,
    registerPageShortcuts,
    clearPageShortcuts,
  };

  return (
    <KeyboardShortcutsContext.Provider value={value}>
      {children}
      <KeyboardShortcutsModal
        isOpen={isHelpOpen}
        onClose={hideHelp}
        contextShortcuts={pageShortcuts}
      />
    </KeyboardShortcutsContext.Provider>
  );
}

export function useKeyboardShortcutsContext() {
  const context = useContext(KeyboardShortcutsContext);
  if (!context) {
    throw new Error('useKeyboardShortcutsContext must be used within KeyboardShortcutsProvider');
  }
  return context;
}

export default KeyboardShortcutsContext;
