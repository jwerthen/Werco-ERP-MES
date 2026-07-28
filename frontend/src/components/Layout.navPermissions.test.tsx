/**
 * Layout sidebar — per-item RBAC gating (`NavItem.permission`).
 *
 * The BOM Unit Mismatches worklist is the first nav entry to carry a
 * `permission`. Its endpoint (`GET /bom/uom-mismatches`) is gated
 * `require_role([ADMIN, MANAGER, SUPERVISOR])` server-side, so a role holding
 * only `boms:view` (viewer, operator, quality) would get a 403 if the sidebar
 * offered it the link. `Layout` therefore filters nav items — and collapsible
 * group children — by `permission`, dropping a group left with no children.
 *
 * What this locks:
 *   1. The client gate matches the SERVER gate. `boms:edit` is exactly
 *      {platform_admin, admin, manager, supervisor}; if either side drifts, the
 *      sidebar starts advertising a guaranteed 403 (or hides a page from
 *      someone entitled to it).
 *   2. Entitled roles see the entry; a `boms:view`-only role does not — while
 *      still seeing the unpermissioned siblings (Parts, Bill of Materials), so
 *      the filter removes ONE item rather than collapsing the group.
 *   3. The new mechanism is inert for every existing item: nothing without a
 *      `permission` changed visibility, and no section lost its header.
 *
 * Layout pulls a large dependency tree (global search, copilot, websocket,
 * keyboard shortcuts, company switcher, API-driven effects); the stand-ins below
 * mirror Layout.navSections.test.tsx so the real sidebar JSX renders alone.
 * `useAuth` is the seam we vary — it is also the module `usePermissions` reads,
 * so mocking it drives both the streamlined-role filter and `can()`.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { getPermissionsForRole } from '../utils/permissions';

// --- Heavy child components reduced to inert stand-ins. -------------------
jest.mock('./CompanySwitcher', () => ({ __esModule: true, default: () => null }));
jest.mock('./ReadOnlyBanner', () => ({ __esModule: true, default: () => null }));
jest.mock('./SessionWarningModal', () => ({ __esModule: true, default: () => null }));
jest.mock('./SkipLink', () => ({ __esModule: true, default: () => null }));
jest.mock('./AdaptivePromptPanel', () => ({ __esModule: true, default: () => null }));
jest.mock('./Tour', () => ({ __esModule: true, TourMenu: () => null }));
jest.mock('./ui/BottomNav', () => ({ __esModule: true, default: () => null }));
jest.mock('./ai/CopilotPanel', () => ({ __esModule: true, CopilotPanel: () => null }));
jest.mock('./NotificationBell', () => ({ __esModule: true, default: () => null }));
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

const mockUser: { value: any } = { value: null };
jest.mock('../context/AuthContext', () => ({
  __esModule: true,
  useAuth: () => ({ user: mockUser.value }),
}));

jest.mock('../context/TourContext', () => ({
  __esModule: true,
  useTour: () => ({ startTour: jest.fn(), isTourComplete: () => true }),
}));

import Layout from './Layout';

/** Render at /parts so the Engineering group is auto-opened by its active child. */
function renderLayout(role: string, initialPath = '/parts') {
  mockUser.value = { id: 1, role, is_superuser: false, first_name: 'T', last_name: 'U', email: 't@x.y' };
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Layout>
        <div>child</div>
      </Layout>
    </MemoryRouter>
  );
}

function sidebar(): HTMLElement {
  return screen.getByRole('navigation', { name: /main navigation/i });
}

function hasNavEntry(label: string): boolean {
  return Array.from(sidebar().querySelectorAll('a, button')).some((el) =>
    (el.textContent || '').includes(label)
  );
}

const MISMATCHES = 'BOM Unit Mismatches';

describe('nav permission gate matches the server role gate', () => {
  it('boms:edit is exactly the roles the endpoint accepts (ADMIN / MANAGER / SUPERVISOR, + platform admin)', () => {
    // `list_bom_uom_mismatches` is require_role([ADMIN, MANAGER, SUPERVISOR]).
    const roles = [
      'platform_admin',
      'admin',
      'manager',
      'supervisor',
      'operator',
      'quality',
      'shipping',
      'viewer',
    ] as const;
    const withEdit = roles.filter((r) => getPermissionsForRole(r as any).includes('boms:edit'));
    expect(withEdit).toEqual(['platform_admin', 'admin', 'manager', 'supervisor']);

    // The excluded roles still hold boms:view — which is precisely why the item
    // needs its own gate rather than riding on the /bom entry.
    expect(getPermissionsForRole('viewer' as any)).toContain('boms:view');
    expect(getPermissionsForRole('quality' as any)).toContain('boms:view');
  });
});

describe('Layout sidebar — permission-gated nav item', () => {
  afterEach(() => {
    mockUser.value = null;
  });

  it.each(['admin', 'manager', 'supervisor'])('shows the worklist to %s', (role) => {
    renderLayout(role);
    expect(hasNavEntry(MISMATCHES)).toBe(true);
    // It sits inside the Engineering group, next to Bill of Materials.
    expect(hasNavEntry('Bill of Materials')).toBe(true);
  });

  it('hides the worklist from a boms:view-only role (quality) without collapsing the group', () => {
    renderLayout('quality');

    expect(hasNavEntry(MISMATCHES)).toBe(false);
    // The unpermissioned siblings are untouched — the filter removed one item,
    // not the group.
    expect(hasNavEntry('Bill of Materials')).toBe(true);
    expect(hasNavEntry('Parts')).toBe(true);
    expect(hasNavEntry('Routing')).toBe(true);
  });

  it('hides the worklist from viewer', () => {
    renderLayout('viewer');
    expect(hasNavEntry(MISMATCHES)).toBe(false);
    expect(hasNavEntry('Bill of Materials')).toBe(true);
  });

  it('hides the worklist from operator (already streamlined, and not entitled)', () => {
    renderLayout('operator');
    expect(hasNavEntry(MISMATCHES)).toBe(false);
  });

  it('a superuser flag alone grants it (usePermissions short-circuits on is_superuser)', () => {
    mockUser.value = { id: 9, role: 'viewer', is_superuser: true, first_name: 'S', last_name: 'U', email: 's@x.y' };
    render(
      <MemoryRouter initialEntries={['/parts']}>
        <Layout>
          <div>child</div>
        </Layout>
      </MemoryRouter>
    );
    expect(hasNavEntry(MISMATCHES)).toBe(true);
  });

  it('leaves every section header intact for an entitled role (no group dropped by the new filter)', () => {
    renderLayout('admin');
    const SECTION_HEADERS = [
      'Overview',
      'Production',
      'Engineering',
      'Inventory & Purchasing',
      'Sales & Quoting',
      'Quality',
      'Insights',
      'Admin',
    ];
    const rendered = Array.from(sidebar().querySelectorAll('p'))
      .map((p) => (p.textContent || '').trim())
      .filter((t) => SECTION_HEADERS.includes(t));
    expect(rendered.sort()).toEqual([...SECTION_HEADERS].sort());
  });
});
