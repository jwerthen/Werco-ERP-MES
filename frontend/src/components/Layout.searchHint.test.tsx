/**
 * Layout search-hint — the quick-search button's keyboard hint.
 *
 * Batch 1 fix: the search button's <kbd> used to show "/" (a stale shortcut).
 * It now shows the real platform-correct shortcut — "⌘K" on macOS, "Ctrl K"
 * elsewhere. This is a light assertion: render Layout with its heavy
 * collaborators stubbed and check the <kbd> text for each platform.
 *
 * Layout pulls in a large dependency tree (global search, copilot, websocket,
 * keyboard shortcuts, company switcher, API-driven effects). We mock those to
 * inert stand-ins so the real search-button JSX — including the isMac ternary
 * under test — renders without dragging in unrelated machinery.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

// --- Heavy child components reduced to inert stand-ins. -------------------
jest.mock('./CompanySwitcher', () => ({ __esModule: true, default: () => null }));
jest.mock('./ReadOnlyBanner', () => ({ __esModule: true, default: () => null }));
jest.mock('./SessionWarningModal', () => ({ __esModule: true, default: () => null }));
jest.mock('./SkipLink', () => ({ __esModule: true, default: () => null }));
jest.mock('./AdaptivePromptPanel', () => ({ __esModule: true, default: () => null }));
jest.mock('./Tour', () => ({ __esModule: true, TourMenu: () => null }));
jest.mock('./ui/BottomNav', () => ({ __esModule: true, default: () => null }));
jest.mock('./ai/CopilotPanel', () => ({ __esModule: true, CopilotPanel: () => null }));
jest.mock('./GlobalSearch', () => ({
  __esModule: true,
  default: () => null,
  useGlobalSearch: () => ({ isOpen: false, open: jest.fn(), close: jest.fn() }),
}));

// --- Hooks / services with side effects stubbed out. ---------------------
jest.mock('../hooks/useWebSocket', () => ({ __esModule: true, useWebSocket: () => ({}) }));
jest.mock('../hooks/useKeyboardShortcuts', () => ({
  __esModule: true,
  useKeyboardShortcuts: () => undefined,
  GLOBAL_SHORTCUTS: [],
}));
jest.mock('../context/KeyboardShortcutsContext', () => ({
  __esModule: true,
  useKeyboardShortcutsContext: () => ({ showHelp: jest.fn() }),
}));
jest.mock('../services/realtime', () => ({
  __esModule: true,
  buildWsUrl: () => 'ws://localhost/ws',
  getAccessToken: () => 'tok',
}));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getPendingUserApprovalSummary: jest.fn().mockResolvedValue({ count: 0 }) },
}));
jest.mock('../context/AuthContext', () => ({
  __esModule: true,
  useAuth: () => ({
    user: { id: 1, role: 'admin', first_name: 'Ada', last_name: 'L', email: 'a@x.y' },
  }),
}));

import Layout from './Layout';

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <Layout>
        <div>child</div>
      </Layout>
    </MemoryRouter>
  );
}

function setPlatform(value: string) {
  Object.defineProperty(window.navigator, 'platform', { value, configurable: true });
}

describe('Layout search hint', () => {
  const original = window.navigator.platform;
  afterEach(() => setPlatform(original));

  it('renders the ⌘K hint on macOS (and never the stale "/")', () => {
    setPlatform('MacIntel');
    renderLayout();
    const kbd = screen.getByText('⌘K');
    expect(kbd.tagName).toBe('KBD');
    expect(screen.queryByText('/', { selector: 'kbd' })).not.toBeInTheDocument();
  });

  it('renders the "Ctrl K" hint on non-macOS platforms', () => {
    setPlatform('Win32');
    renderLayout();
    const kbd = screen.getByText('Ctrl K');
    expect(kbd.tagName).toBe('KBD');
  });
});
