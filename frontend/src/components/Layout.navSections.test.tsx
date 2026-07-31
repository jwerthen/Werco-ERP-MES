/**
 * Layout sectioned sidebar — Batch 7 navigation & wayfinding.
 *
 * The sidebar moved from a flat nav list to `navSections` (8 sections), each
 * with a mono/uppercase header. RBAC filtering now happens *within* sections,
 * and any section that ends up empty is dropped so no orphan header renders.
 *
 * This guards three things:
 *   1. Section headers render for a full-access (admin) user.
 *   2. Every prior nav link is still present (no nav item lost in the refactor).
 *   3. For a streamlined role (operator), sections that filter down to zero
 *      items are dropped — no header without links underneath it.
 *
 * Layout pulls a large dependency tree (global search, copilot, websocket,
 * keyboard shortcuts, company switcher, API-driven effects). We mock those to
 * inert stand-ins so the real sidebar JSX renders without unrelated machinery.
 * `useAuth` is mocked per-test so we can flip the active role.
 *
 * Note on text collisions: a few section-header labels (Engineering, Quality,
 * Sales & Quoting) are *also* the label of a collapsible nav group within that
 * section, so a bare getByText would match two nodes. Section headers render as
 * mono/uppercase <p> elements; nav entries render inside <a>/<button>. The
 * helpers below target each kind precisely.
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

// useAuth is the seam we vary per-test (role drives RBAC nav filtering).
const mockUser: { value: any } = { value: { id: 1, role: 'admin', first_name: 'Ada', last_name: 'L', email: 'a@x.y' } };
jest.mock('../context/AuthContext', () => ({
  __esModule: true,
  useAuth: () => ({ user: mockUser.value }),
}));

// Tour auto-start (added with the onboarding triggers) pulls TourContext into
// Layout. Stub it so the sidebar renders without a TourProvider wrapper.
jest.mock('../context/TourContext', () => ({
  __esModule: true,
  useTour: () => ({ startTour: jest.fn(), isTourComplete: () => true }),
}));

import Layout from './Layout';

function renderLayout(initialPath = '/dashboard') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Layout>
        <div>child</div>
      </Layout>
    </MemoryRouter>
  );
}

/** The sidebar <nav> — the seam under test, distinct from the top-bar breadcrumb. */
function sidebar(): HTMLElement {
  return screen.getByRole('navigation', { name: /main navigation/i });
}

// The eight section headers introduced by Batch 7. They render as mono/uppercase
// <p> labels; the visible text is title-cased per `navSections[].label`.
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

/** All <p> section-header elements (mono/uppercase headers), by text. */
function sectionHeaderEls(): HTMLElement[] {
  return Array.from(sidebar().querySelectorAll('p')).filter(p =>
    SECTION_HEADERS.includes((p.textContent || '').trim())
  ) as HTMLElement[];
}

/** True if a given section header (the <p>) is rendered. */
function hasSectionHeader(label: string): boolean {
  return sectionHeaderEls().some(p => (p.textContent || '').trim() === label);
}

/** True if a nav entry (link or collapsible-group button) with this label renders. */
function hasNavEntry(label: string): boolean {
  const nav = sidebar();
  const interactive = Array.from(nav.querySelectorAll('a, button'));
  return interactive.some(el => (el.textContent || '').includes(label));
}

// Nav entries that are ALWAYS rendered for an admin regardless of which group
// is expanded: the top-level (childless) links plus every collapsible-group
// button label. Collapsible-group *children* only render when their group is
// open (collapse hides them from the DOM), so they're covered separately below.
const ALWAYS_RENDERED_NAV_LABELS = [
  // Overview (top-level links)
  'Dashboard',
  'Action Inbox',
  // Production: Shop Floor is a group; the rest are top-level links.
  'Shop Floor',
  'Scheduling',
  'Work Orders',
  'Maintenance',
  'Tool Management',
  'OEE',
  // Group buttons (their children are gated behind the collapse).
  'Engineering',
  'Warehouse',
  'Purchasing',
  'Sales & Quoting',
  'Quality',
  'Administration',
  // Insights (top-level links)
  'Documents',
  'Job Costing',
  'Analytics',
  'Reports',
];

// Children of the Engineering group — present in the DOM only once that group
// is expanded. Rendering at /parts auto-opens it (Parts is an active child).
const ENGINEERING_GROUP_CHILDREN = ['Parts', 'Bill of Materials', 'Routing', 'Engineering Changes'];

describe('Layout sectioned sidebar (admin / full access)', () => {
  beforeEach(() => {
    mockUser.value = { id: 1, role: 'admin', is_superuser: true, first_name: 'Ada', last_name: 'L', email: 'a@x.y' };
  });

  it('renders all eight section headers', () => {
    renderLayout();
    for (const header of SECTION_HEADERS) {
      expect(hasSectionHeader(header)).toBe(true);
    }
    // Exactly the eight expected headers, no more.
    expect(sectionHeaderEls()).toHaveLength(SECTION_HEADERS.length);
  });

  it('keeps every always-rendered nav entry present after the flat -> sectioned refactor', () => {
    renderLayout();
    // Top-level links + collapsible-group buttons are always in the DOM.
    const missing = ALWAYS_RENDERED_NAV_LABELS.filter(label => !hasNavEntry(label));
    expect(missing).toEqual([]);
  });

  it('still renders a collapsible group\'s children once its group is open', () => {
    // Navigating into the Engineering group (/parts) auto-opens it, exposing
    // the children that the collapse otherwise hides. This proves the refactor
    // didn't drop child links — they're behind the (unchanged) collapse, not gone.
    renderLayout('/parts');
    const missing = ENGINEERING_GROUP_CHILDREN.filter(label => !hasNavEntry(label));
    expect(missing).toEqual([]);
  });
});

describe('Layout sectioned sidebar (operator / streamlined RBAC)', () => {
  beforeEach(() => {
    mockUser.value = { id: 2, role: 'operator', first_name: 'Op', last_name: 'R', email: 'op@x.y' };
  });

  it('drops sections that filter to zero items — no orphan headers', () => {
    renderLayout();

    // Operators see only Dashboard, Shop Floor, Quality, Maintenance items,
    // which live in Overview / Production / Quality — so those headers stay.
    expect(hasSectionHeader('Overview')).toBe(true);
    expect(hasSectionHeader('Production')).toBe(true);
    expect(hasSectionHeader('Quality')).toBe(true);

    // Engineering, Inventory & Purchasing, Sales & Quoting, Insights, and Admin
    // have no operator-visible items -> the header must be gone (no orphan).
    expect(hasSectionHeader('Engineering')).toBe(false);
    expect(hasSectionHeader('Inventory & Purchasing')).toBe(false);
    expect(hasSectionHeader('Sales & Quoting')).toBe(false);
    expect(hasSectionHeader('Insights')).toBe(false);
    expect(hasSectionHeader('Admin')).toBe(false);
  });

  it('shows the streamlined operator items and hides the rest', () => {
    renderLayout();

    // Visible to operators.
    expect(hasNavEntry('Dashboard')).toBe(true);
    expect(hasNavEntry('Shop Floor')).toBe(true);
    expect(hasNavEntry('Maintenance')).toBe(true);
    // Quality group label collides with the Quality section header; assert the
    // interactive group entry exists.
    expect(hasNavEntry('Quality')).toBe(true);

    // Filtered out for operators (top-level items / groups in dropped sections).
    expect(hasNavEntry('Work Orders')).toBe(false);
    expect(hasNavEntry('Action Inbox')).toBe(false);
    expect(hasNavEntry('Administration')).toBe(false);
    expect(hasNavEntry('Engineering Changes')).toBe(false);
    expect(hasNavEntry('Parts')).toBe(false);
  });

  it('renders no empty section header (every header has at least one nav link below it)', () => {
    renderLayout();
    const headerEls = sectionHeaderEls();
    expect(headerEls.length).toBeGreaterThan(0);
    for (const header of headerEls) {
      // The section is the header's parent <div>; its links live in the sibling
      // container after the header <p>. There must be at least one.
      const section = header.parentElement as HTMLElement;
      const links = section.querySelectorAll('a, button');
      expect(links.length).toBeGreaterThan(0);
    }
  });
});
