/**
 * TourMenu — the help/tours dropdown in the top bar.
 *
 * Batch 1 fix: the "Keyboard Shortcuts" footer button used to navigate('/')
 * (dumping the operator on the dashboard). It now opens the keyboard-shortcuts
 * help overlay via the shortcuts context's showHelp() and must NOT navigate.
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import TourMenu from './TourMenu';

// Spy on navigation so we can assert the button does NOT route to '/'.
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  __esModule: true,
  useNavigate: () => mockNavigate,
}));

// showHelp from the keyboard-shortcuts context is what the button must call.
const mockShowHelp = jest.fn();
jest.mock('../../context/KeyboardShortcutsContext', () => ({
  __esModule: true,
  useKeyboardShortcutsContext: () => ({ showHelp: mockShowHelp }),
}));

// Minimal context stubs so the menu renders without real providers.
jest.mock('../../context/TourContext', () => ({
  __esModule: true,
  useTour: () => ({
    startTour: jest.fn(),
    isTourComplete: () => false,
    resetAllTours: jest.fn(),
    isActive: false,
  }),
}));

jest.mock('../../context/AuthContext', () => ({
  __esModule: true,
  useAuth: () => ({ user: { role: 'operator', first_name: 'Rosa' } }),
}));

jest.mock('../../hooks/usePermissions', () => ({
  __esModule: true,
  usePermissions: () => ({ role: 'operator', isSuperuser: false }),
}));

// Keep the role-filtered tour/tip lists empty so the menu body is trivial; the
// footer (with the Keyboard Shortcuts button) renders regardless.
jest.mock('../../data/tours', () => ({
  __esModule: true,
  getToursForRole: () => [],
  getHelpTipsForRole: () => [],
  getTour: () => undefined,
}));

describe('TourMenu — Keyboard Shortcuts button', () => {
  beforeEach(() => jest.clearAllMocks());

  function openMenu() {
    render(<TourMenu />);
    // Open the dropdown via its trigger.
    fireEvent.click(screen.getByRole('button', { name: /help & tours/i }));
  }

  it('calls showHelp() and does not navigate when clicked', () => {
    openMenu();

    const shortcutsBtn = screen.getByRole('button', { name: /keyboard shortcuts/i });
    fireEvent.click(shortcutsBtn);

    expect(mockShowHelp).toHaveBeenCalledTimes(1);
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
