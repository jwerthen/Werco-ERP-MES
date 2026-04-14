/**
 * KeyboardShortcutsModal Component
 * 
 * Displays a modal showing all available keyboard shortcuts.
 * Triggered by Ctrl+? or clicking the help button.
 */

import React from 'react';
import { formatShortcut, GLOBAL_SHORTCUTS } from '../hooks/useKeyboardShortcuts';

interface KeyboardShortcutsModalProps {
  isOpen: boolean;
  onClose: () => void;
  contextShortcuts?: Array<{
    key: string;
    ctrl?: boolean;
    shift?: boolean;
    alt?: boolean;
    description: string;
  }>;
}

interface ShortcutCategory {
  title: string;
  shortcuts: Array<{
    key: string;
    ctrl?: boolean;
    shift?: boolean;
    alt?: boolean;
    description: string;
  }>;
}

export function KeyboardShortcutsModal({ 
  isOpen, 
  onClose, 
  contextShortcuts = [] 
}: KeyboardShortcutsModalProps) {
  if (!isOpen) return null;

  const categories: ShortcutCategory[] = [
    {
      title: 'Global Shortcuts',
      shortcuts: [
        GLOBAL_SHORTCUTS.NEW,
        GLOBAL_SHORTCUTS.SAVE,
        GLOBAL_SHORTCUTS.ESCAPE,
        GLOBAL_SHORTCUTS.HELP,
        GLOBAL_SHORTCUTS.SEARCH,
        GLOBAL_SHORTCUTS.REFRESH,
      ],
    },
    {
      title: 'Navigation',
      shortcuts: [
        { key: 'ArrowUp', description: 'Move up in list/table' },
        { key: 'ArrowDown', description: 'Move down in list/table' },
        { key: 'Enter', description: 'Select/open item' },
        { key: 'Tab', description: 'Move to next field' },
        { key: 'Tab', shift: true, description: 'Move to previous field' },
      ],
    },
  ];

  if (contextShortcuts.length > 0) {
    categories.push({
      title: 'Page-Specific Shortcuts',
      shortcuts: contextShortcuts,
    });
  }

  return (
    <div 
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="keyboard-shortcuts-title"
    >
      <div 
        className="bg-[#151b28] rounded-lg shadow-xl max-w-lg w-full mx-4 max-h-[80vh] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700">
          <h2 
            id="keyboard-shortcuts-title"
            className="text-lg font-semibold text-white"
          >
            Keyboard Shortcuts
          </h2>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-200 transition-colors"
            aria-label="Close keyboard shortcuts"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="p-6 overflow-y-auto max-h-[60vh]">
          {categories.map((category, categoryIndex) => (
            <div key={categoryIndex} className={categoryIndex > 0 ? 'mt-6' : ''}>
              <h3 className="text-sm font-medium text-slate-400 uppercase tracking-wider mb-3">
                {category.title}
              </h3>
              <div className="space-y-2">
                {category.shortcuts.map((shortcut, shortcutIndex) => (
                  <div 
                    key={shortcutIndex}
                    className="flex items-center justify-between py-2"
                  >
                    <span className="text-slate-200">
                      {shortcut.description}
                    </span>
                    <kbd className="px-2 py-1 text-sm font-mono bg-slate-700 text-slate-200 rounded border border-slate-600">
                      {formatShortcut(shortcut)}
                    </kbd>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-700 bg-slate-800/50">
          <p className="text-sm text-slate-400 text-center">
            Press <kbd className="px-1 py-0.5 text-xs bg-slate-700 rounded text-slate-300">Esc</kbd> to close
          </p>
        </div>
      </div>
    </div>
  );
}

export default KeyboardShortcutsModal;
