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
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { getPermissionsForRole } from '../utils/permissions';
import { canAccessPath } from '../utils/routeAccess';

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
jest.mock('../hooks/useScrollRestoration', () => ({ useScrollRestoration: () => undefined }));
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
  return Array.from(sidebar().querySelectorAll('a, button')).some(el => (el.textContent || '').includes(label));
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
    const withEdit = roles.filter(r => getPermissionsForRole(r as any).includes('boms:edit'));
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

  it.each(['admin', 'manager', 'supervisor'])('shows the worklist to %s', role => {
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
      .map(p => (p.textContent || '').trim())
      .filter(t => SECTION_HEADERS.includes(t));
    expect(rendered.sort()).toEqual([...SECTION_HEADERS].sort());
  });
});

describe('Fabrication Quoting navigation', () => {
  afterEach(() => {
    mockUser.value = null;
  });

  it.each(['admin', 'manager', 'supervisor', 'viewer'])(
    'offers the fabrication quoting route to %s with purchasing:view',
    role => {
      renderLayout(role, '/fabrication-quotes');
      expect(within(sidebar()).getByRole('link', { name: 'Fabrication Quoting' })).toHaveAttribute(
        'href',
        '/fabrication-quotes'
      );
    }
  );

  it.each(['operator', 'quality', 'shipping'])('hides Fabrication Quoting from %s without purchasing:view', role => {
    renderLayout(role, '/fabrication-quotes');
    expect(within(sidebar()).queryByRole('link', { name: 'Fabrication Quoting' })).not.toBeInTheDocument();
  });
});

describe('Material Price History navigation', () => {
  afterEach(() => {
    mockUser.value = null;
  });

  it.each(['admin', 'manager', 'supervisor', 'viewer'])('offers price history to %s with purchasing:view', role => {
    renderLayout(role, '/purchasing/price-history?part=42');
    const historyLink = within(sidebar()).getByRole('link', { name: 'Material Price History' });
    expect(historyLink).toHaveAttribute('href', '/purchasing/price-history');
    expect(historyLink).toHaveClass('text-fd-ink');
    expect(within(sidebar()).getByRole('link', { name: 'Purchase Orders' })).not.toHaveClass('text-fd-ink');
    expect(
      canAccessPath('/purchasing/price-history?part=42', permission =>
        getPermissionsForRole(role as any).includes(permission)
      )
    ).toBe(true);
  });

  it.each(['operator', 'quality', 'shipping'])('hides price history from %s without purchasing:view', role => {
    renderLayout(role, '/purchasing/price-history');
    expect(within(sidebar()).queryByRole('link', { name: 'Material Price History' })).not.toBeInTheDocument();
    expect(
      canAccessPath('/purchasing/price-history', permission => getPermissionsForRole(role as any).includes(permission))
    ).toBe(false);
  });
});

describe('Supplier Scorecards navigation (the ASL owner is Purchasing / Quality)', () => {
  // Lead decision L2: the Approved Supplier List tab lives on /supplier-scorecards and the server
  // lets QUALITY write the ASL, so the quality role must reach the screen. Route and nav share one
  // requirement (`anyOf: purchasing:view | quality:approve`), so they cannot disagree; every GET the
  // page makes is open to any signed-in user server-side.
  afterEach(() => {
    mockUser.value = null;
  });

  const allows = (role: string, path = '/supplier-scorecards') =>
    canAccessPath(path, permission => getPermissionsForRole(role as any).includes(permission));

  it('quality:approve is exactly the ASL approval roles (ADMIN / MANAGER / QUALITY, + platform admin)', () => {
    const roles = ['platform_admin', 'admin', 'manager', 'supervisor', 'operator', 'quality', 'shipping', 'viewer'];
    expect(roles.filter(r => getPermissionsForRole(r as any).includes('quality:approve'))).toEqual([
      'platform_admin',
      'admin',
      'manager',
      'quality',
    ]);
    // The reason the gate is not plain purchasing:view: the quality set does not carry it.
    expect(getPermissionsForRole('quality' as any)).not.toContain('purchasing:view');
  });

  it.each(['admin', 'manager', 'supervisor', 'viewer', 'quality'])('offers Supplier Scorecards to %s', role => {
    renderLayout(role, '/supplier-scorecards');
    expect(within(sidebar()).getByRole('link', { name: 'Supplier Scorecards' })).toHaveAttribute(
      'href',
      '/supplier-scorecards'
    );
    expect(allows(role)).toBe(true);
    expect(allows(role, '/supplier-scorecards?tab=asl&asl=overdue')).toBe(true);
  });

  it.each(['operator', 'shipping'])('hides Supplier Scorecards from %s, and the route refuses it too', role => {
    renderLayout(role, '/supplier-scorecards');
    expect(within(sidebar()).queryByRole('link', { name: 'Supplier Scorecards' })).not.toBeInTheDocument();
    expect(allows(role)).toBe(false);
  });

  it('widening the screen for quality does not open the rest of purchasing to it', () => {
    expect(allows('quality', '/purchasing')).toBe(false);
    expect(allows('quality', '/purchasing/price-history')).toBe(false);
  });
});

describe('API Tokens navigation', () => {
  // Every /api-tokens verb is require_role([ADMIN]) server-side (platform admins and
  // superusers pass), and the /admin/api-tokens route is wrapped in AdminRoute with the
  // same set. The sidebar must agree, or it advertises a guaranteed 403.
  afterEach(() => {
    mockUser.value = null;
  });

  it.each(['admin', 'platform_admin'])('offers API Tokens to %s', role => {
    renderLayout(role, '/admin/api-tokens');
    expect(within(sidebar()).getByRole('link', { name: 'API Tokens' })).toHaveAttribute('href', '/admin/api-tokens');
  });

  it('a superuser flag alone grants it', () => {
    mockUser.value = { id: 9, role: 'manager', is_superuser: true, first_name: 'S', last_name: 'U', email: 's@x.y' };
    render(
      <MemoryRouter initialEntries={['/work-centers']}>
        <Layout>
          <div>child</div>
        </Layout>
      </MemoryRouter>
    );
    expect(within(sidebar()).getByRole('link', { name: 'API Tokens' })).toBeInTheDocument();
  });

  it.each(['manager', 'supervisor'])('hides API Tokens from %s while the Administration group itself is open', role => {
    renderLayout(role, '/supplier-scorecards');
    // A sibling in the same group renders, so the absence below is the gate, not a closed group.
    expect(within(sidebar()).getByRole('link', { name: 'Supplier Scorecards' })).toBeInTheDocument();
    expect(within(sidebar()).queryByRole('link', { name: 'API Tokens' })).not.toBeInTheDocument();
  });
});

describe('Labor Review navigation (GET /shop-floor/time-entries role set)', () => {
  // The list endpoint is require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY]) (+ platform admin and
  // superuser via require_role). Route and nav share `quality:inspect`, which is exactly that set.
  afterEach(() => {
    mockUser.value = null;
  });

  const allows = (role: string) =>
    canAccessPath('/labor-review?status=open', permission => getPermissionsForRole(role as any).includes(permission));

  it('quality:inspect is exactly the Labor Review roles', () => {
    const roles = ['platform_admin', 'admin', 'manager', 'supervisor', 'operator', 'quality', 'shipping', 'viewer'];
    expect(roles.filter(r => getPermissionsForRole(r as any).includes('quality:inspect'))).toEqual([
      'platform_admin',
      'admin',
      'manager',
      'supervisor',
      'quality',
    ]);
  });

  it.each(['admin', 'manager', 'supervisor', 'quality'])('offers Labor Review to %s', role => {
    renderLayout(role, '/labor-review');
    expect(within(sidebar()).getByRole('link', { name: 'Labor Review' })).toHaveAttribute('href', '/labor-review');
    expect(allows(role)).toBe(true);
  });

  it.each(['operator', 'shipping', 'viewer'])('hides Labor Review from %s and refuses the deep link', role => {
    renderLayout(role, '/shop-floor');
    expect(within(sidebar()).queryByRole('link', { name: 'Labor Review' })).not.toBeInTheDocument();
    expect(allows(role)).toBe(false);
  });
});
