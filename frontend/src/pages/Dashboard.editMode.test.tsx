/**
 * Dashboard edit mode, page level, with the typed fake drag layer (dnd-kit is never loaded):
 * entering, the one stable widget tree (no remount, no refetch), keyboard / menu / remove /
 * Undo, harness-driven drags, the gesture-only polling pause with its one catch-up, the
 * saved ∪ draft fetch union and its 400 ms settle, Done / Cancel / 409, read-only, the
 * error-path re-read, a session change mid-edit, a chunk that fails to load, Escape, the
 * empty and banners-only entries, and the gallery's focus hand-off. Also: the entry re-read
 * and the post-409 Cancel re-read (CAS version), entry refused over the skeleton / ErrorState,
 * the measured scroll padding, FLIP wiring and its order against the editor's focus, the
 * widget-initiated refresh held during a drag, and no widget re-render on a live reorder.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import type { DashboardData } from '../types';
import type { DashboardLayoutResponse, DashboardLayoutValues } from '../types/dashboardLayout';
import { setCustomPermissions } from '../utils/permissions';
import { clearDashboardSourceCache } from '../components/dashboard/useDashboardSources';
import { ToastProvider } from '../components/ui';
import { useWebSocket } from '../hooks/useWebSocket';
import { invalidateNotificationList } from '../components/dashboard/widgets/extra/notificationListCache';
import { loadDashboardDragLayer, prefetchDashboardDragLayer } from '../components/dashboard/editor/loadDragLayer';
import {
  CANCELLED_ANNOUNCEMENT,
  DONE_UNCHANGED_ANNOUNCEMENT,
  EDIT_IDS,
  EDITOR_LOAD_FAILED,
  ENTER_ANNOUNCEMENT,
  READ_ONLY_CUSTOMIZE_DESCRIPTION,
  READ_ONLY_LAYOUT_NOTICE,
  SAVED_TOAST,
  SESSION_CHANGED_TOAST,
  ZONE_PLACEHOLDERS,
  moveButtonId,
  removeButtonId,
} from '../components/dashboard/editor/editorCopy';
import { FakeDragLayer, dragHarness } from '../test-utils/fakeDragLayer';
import { installFakeAnimate, toDomRect } from '../test-utils/dndDom';
import Dashboard from './Dashboard';

jest.mock('../components/dashboard/editor/loadDragLayer', () =>
  jest.requireActual('../test-utils/fakeDragLayer').createFakeLoadDragLayerModule()
);

jest.mock('../components/dashboard/widgets/extra/notificationListCache', () => {
  const actual = jest.requireActual<typeof import('../components/dashboard/widgets/extra/notificationListCache')>(
    '../components/dashboard/widgets/extra/notificationListCache'
  );
  return { __esModule: true, ...actual, invalidateNotificationList: jest.fn(actual.invalidateNotificationList) };
});

/** Renders of the Work Center Status panel (a widget-context consumer): see the reorder test. */
let mockWorkCenterRenders = 0;
jest.mock('../components/dashboard/widgets/core/WorkCenterStatusPanel', () => {
  const actual = jest.requireActual<typeof import('../components/dashboard/widgets/core/WorkCenterStatusPanel')>(
    '../components/dashboard/widgets/core/WorkCenterStatusPanel'
  );
  return {
    __esModule: true,
    // Called as a function, so the panel's context reads are this wrapper's: it re-renders
    // exactly when the panel would.
    WorkCenterStatusPanel: function CountedWorkCenterStatusPanel() {
      mockWorkCenterRenders += 1;
      return actual.WorkCenterStatusPanel();
    },
  };
});

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getDashboardWithCache: jest.fn(),
    getQualitySummary: jest.fn(),
    getEquipmentDueSoon: jest.fn(),
    getLowStockAlerts: jest.fn(),
    getCapacityHeatmap: jest.fn(),
    getDashboardLayout: jest.fn(),
    saveDashboardLayout: jest.fn(),
    resetDashboardLayout: jest.fn(),
    getKPIDashboard: jest.fn(),
    getNotifications: jest.fn(),
    getUnreadCount: jest.fn(),
    getSetupHealth: jest.fn(),
    getAIRecommendations: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = { id: 1, role: 'admin', is_superuser: false };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser, isAuthenticated: true, isLoading: false }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedLoad = loadDashboardDragLayer as jest.MockedFunction<typeof loadDashboardDragLayer>;
const mockedPrefetch = prefetchDashboardDragLayer as jest.MockedFunction<typeof prefetchDashboardDragLayer>;
const mockedInvalidate = invalidateNotificationList as jest.MockedFunction<typeof invalidateNotificationList>;
const mockedUseWebSocket = useWebSocket as jest.MockedFunction<typeof useWebSocket>;

const dashboardData: DashboardData = {
  summary: {
    active_work_orders: 42,
    due_today: 5,
    overdue: 3,
    signed_in_users: 2,
    checked_in_users: 1,
    idle_signed_in_users: 1,
    completed_today: 7,
  },
  work_centers: [],
  signed_in_users: [],
  active_assignments: [],
  recent_completions: [],
};

const layoutResponse = (
  layout: DashboardLayoutValues | null,
  overrides: Partial<DashboardLayoutResponse> = {}
): DashboardLayoutResponse => ({
  company_id: 1,
  version: layout ? 2 : 0,
  layout,
  updated_at: null,
  can_edit: true,
  ...overrides,
});

const layoutOf = (...widgets: { id: string; size?: string }[]): DashboardLayoutValues => ({
  format_version: 1,
  widgets,
});

const httpError = (status: number, detail = 'Request failed') =>
  Object.assign(new Error(detail), { response: { status, data: { detail } } });

const tokenFor = (claims: Record<string, unknown>) => `x.${btoa(JSON.stringify(claims))}.y`;

const renderDashboard = () =>
  render(
    <ToastProvider>
      <MemoryRouter>
        <main id="main-content" tabIndex={-1}>
          <Dashboard />
        </main>
      </MemoryRouter>
    </ToastProvider>
  );

const waitForLoaded = () => waitFor(() => expect(screen.queryByText('Loading dashboard…')).not.toBeInTheDocument());
const loaded = () => screen.findByRole('button', { name: 'Refresh dashboard' }).then(() => waitForLoaded());
/** Flush promises and zero-delay timers under fake timers. */
const settle = () =>
  act(async () => {
    await jest.advanceTimersByTimeAsync(0);
  });
const advance = (ms: number) =>
  act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });

const customize = () => document.getElementById('dashboard-customize') as HTMLButtonElement;
const heading = () => screen.getByRole('heading', { name: 'Customizing dashboard' });
const bar = () => screen.getByRole('region', { name: 'Customizing dashboard' });
const queryBar = () => screen.queryByRole('region', { name: 'Customizing dashboard' });
const announcer = () => screen.getByTestId('dashboard-announcer');
const kpiList = () => screen.getByRole('list', { name: 'Key metrics on your dashboard' });
const panelList = () => screen.getByRole('list', { name: 'Panels on your dashboard' });
const cellIds = (zone: HTMLElement) =>
  Array.from(zone.querySelectorAll(':scope > [data-widget-cell]')).map(cell => cell.getAttribute('data-widget-cell'));
const kpiStrip = () => document.querySelector('.grid.grid-cols-2') as HTMLElement;
const panelGrid = () => document.querySelector('.grid.xl\\:grid-cols-12') as HTMLElement;
const cellOf = (id: string) => document.querySelector(`[data-widget-cell="${id}"]`) as HTMLElement;
const byId = (id: string) => document.getElementById(id) as HTMLElement;

async function enterEditMode(button: HTMLElement = customize()) {
  fireEvent.click(button);
  await waitFor(() => expect(heading()).toHaveFocus());
}

/** Under fake timers: click Customize and let the (fake) chunk load resolve. */
async function enterEditModeWithFakeTimers(button: HTMLElement = customize()) {
  fireEvent.click(button);
  await settle();
  expect(heading()).toHaveFocus();
}

async function openGallery() {
  fireEvent.click(within(bar()).getByRole('button', { name: 'Add widgets' }));
  return screen.findByRole('dialog', { name: 'Add widgets' });
}

const push = (type: string) => {
  const options = mockedUseWebSocket.mock.calls[mockedUseWebSocket.mock.calls.length - 1]?.[0];
  options?.onMessage?.({ type }, new MessageEvent('message'));
};

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
  dragHarness.reset();
  clearDashboardSourceCache();
  setCustomPermissions(null);
  mockUser = { id: 1, role: 'admin', is_superuser: false };
  mockedApi.getDashboardWithCache.mockResolvedValue({ data: dashboardData, fromCache: false, changed: true });
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 2, open_cars: 1, pending_fais: 0, ncr_dispositions: {} });
  mockedApi.getEquipmentDueSoon.mockResolvedValue([]);
  mockedApi.getLowStockAlerts.mockResolvedValue([]);
  mockedApi.getCapacityHeatmap.mockResolvedValue({
    start_date: '2026-11-11',
    end_date: '2026-11-17',
    overload_cells: 0,
    overloaded_work_centers: [],
    work_centers: [],
  });
  // A 403 renders in place ("Not available for your role") and is terminal: only the call count matters here.
  mockedApi.getKPIDashboard.mockRejectedValue(httpError(403, 'Forbidden'));
  mockedApi.getNotifications.mockResolvedValue({
    items: [],
    pagination: { page: 1, page_size: 5, total_count: 0, total_pages: 0, has_next: false, has_previous: false },
  });
  mockedApi.getUnreadCount.mockResolvedValue(0);
  mockedApi.getSetupHealth.mockResolvedValue({ progress: 40 });
  mockedApi.getAIRecommendations.mockResolvedValue([]);
  mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(null));
  mockedApi.saveDashboardLayout.mockImplementation(async body => layoutResponse(body.layout, { version: 3 }));
  mockedApi.resetDashboardLayout.mockImplementation(async () => layoutResponse(null, { version: 4 }));
});

afterEach(() => {
  setCustomPermissions(null);
  sessionStorage.clear();
  jest.restoreAllMocks();
});

describe('entering edit mode', () => {
  it('focuses the bar heading, announces, disables Customize and turns every cell into a named list item', async () => {
    renderDashboard();
    await loaded();
    const kpisBefore = cellIds(kpiStrip());
    const panelsBefore = cellIds(panelGrid());
    await enterEditMode();

    expect(announcer()).toHaveTextContent(ENTER_ANNOUNCEMENT);
    expect(customize()).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Refresh dashboard' })).toBeEnabled();
    expect(dragHarness.mounted).toBe(true);

    // Both zones are lists of named cells, in render order.
    expect(cellIds(kpiList())).toEqual(kpisBefore);
    expect(cellIds(panelList())).toEqual(panelsBefore);
    expect(within(kpiList()).getAllByRole('listitem')).toHaveLength(kpisBefore.length);
    expect(within(panelList()).getAllByRole('listitem')).toHaveLength(panelsBefore.length);
    expect(screen.getByRole('button', { name: 'Remove Overdue' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Move Overdue' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove Capacity Overview' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Move or resize Capacity Overview' })).toBeInTheDocument();

    // Widget content is live but inert; so is the banner zone. The chrome is not.
    document.querySelectorAll('[data-widget-body]').forEach(body => expect(body).toHaveAttribute('inert'));
    expect(screen.getByRole('button', { name: 'Remove Overdue' }).closest('[inert]')).toBeNull();
    expect(screen.getByRole('region', { name: 'First exception to review' }).closest('[inert]')).not.toBeNull();
    // The page root reserves room for the fixed bar.
    expect(customize().closest('.space-y-4.pb-28')).not.toBeNull();
  });

  it('leaves view mode untouched: no chrome, no roles, no inert, and no drag layer', async () => {
    renderDashboard();
    await loaded();
    expect(screen.queryByRole('button', { name: 'Move Overdue' })).not.toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Key metrics on your dashboard' })).not.toBeInTheDocument();
    expect(document.querySelector('[inert]')).toBeNull();
    expect(queryBar()).not.toBeInTheDocument();
    expect(dragHarness.mounted).toBe(false);
    expect(mockedLoad).not.toHaveBeenCalled();
  });

  it('renders the two grids test-utils/gridFlow models (the drag planner’s property tests rest on it)', async () => {
    renderDashboard();
    await loaded();
    await enterEditMode();
    const layoutTokens = (element: HTMLElement) =>
      Array.from(element.classList)
        .filter(token => /^((sm|lg|xl):)?(grid|grid-cols-\d+|gap-\d+|items-\w+)$/.test(token))
        .sort();
    // gridFlow: panel grid 1 / 2 / 12 columns, gap 16 px, items-start; KPI strip 2 / 3 / 5, gap 8 px, stretched.
    expect(layoutTokens(panelList())).toEqual(
      ['gap-4', 'grid', 'grid-cols-1', 'items-start', 'lg:grid-cols-2', 'xl:grid-cols-12'].sort()
    );
    expect(layoutTokens(kpiList())).toEqual(
      ['gap-2', 'grid', 'grid-cols-2', 'lg:grid-cols-5', 'sm:grid-cols-3'].sort()
    );
    // Every cell is a DIRECT child of its zone grid (gridFlow places items, not wrappers).
    [kpiList(), panelList()].forEach(zone =>
      Array.from(zone.children).forEach(child => expect(child).toHaveAttribute('data-widget-cell'))
    );
  });

  it('prefetches the drag layer when the pointer reaches or focus lands on Customize', async () => {
    renderDashboard();
    await loaded();
    fireEvent.pointerEnter(customize());
    expect(mockedPrefetch).toHaveBeenCalledTimes(1);
    customize().focus();
    expect(mockedPrefetch).toHaveBeenCalledTimes(2);
  });

  it('sets scroll padding while editing (7rem until the bar is measured) and restores it on exit', async () => {
    renderDashboard();
    await loaded();
    await enterEditMode();
    expect(document.documentElement.style.scrollPaddingTop).toBe('4rem');
    expect(document.documentElement.style.scrollPaddingBottom).toBe('7rem');
    // Scroll anchoring is off while editing: the live reorders move cells it could anchor to.
    expect(document.documentElement.style.overflowAnchor).toBe('none');
    fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(queryBar()).not.toBeInTheDocument());
    expect(document.documentElement.style.scrollPaddingTop).toBe('');
    expect(document.documentElement.style.scrollPaddingBottom).toBe('');
    expect(document.documentElement.style.overflowAnchor).toBe('');
  });

  it('re-measures when the bar itself grows (ResizeObserver) and puts the page’s own values back on exit', async () => {
    const html = document.documentElement.style;
    let barTop = 768 - 120;
    let notifyResize: (() => void) | null = null;
    const observed: Element[] = [];
    const disconnect = jest.fn();
    class FakeResizeObserver implements ResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        notifyResize = () => callback([], this);
      }
      observe(target: Element) {
        observed.push(target);
      }
      unobserve() {
        return undefined;
      }
      disconnect() {
        disconnect();
      }
    }
    const hadResizeObserver = 'ResizeObserver' in global;
    const realResizeObserver = global.ResizeObserver;
    global.ResizeObserver = FakeResizeObserver;
    const realRect = Element.prototype.getBoundingClientRect;
    jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      if (this.id === 'dashboard-edit-bar')
        return toDomRect({ left: 0, top: barTop, width: 1024, height: 768 - barTop });
      return realRect.call(this);
    });
    // Values the page had before editing (another feature's, or the browser's own).
    html.scrollPaddingTop = '12px';
    html.scrollPaddingBottom = '20px';
    html.overflowAnchor = 'auto';
    try {
      renderDashboard();
      await loaded();
      await enterEditMode();
      expect(observed).toEqual([bar()]);
      expect(html.scrollPaddingBottom).toBe('128px');

      // An alert appears in the bar: no window resize, only the bar's own box changes.
      barTop = 768 - 200;
      act(() => notifyResize?.());
      expect(html.scrollPaddingBottom).toBe('208px');

      fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
      await waitFor(() => expect(queryBar()).not.toBeInTheDocument());
      expect(disconnect).toHaveBeenCalledTimes(1);
      expect(html.scrollPaddingTop).toBe('12px');
      expect(html.scrollPaddingBottom).toBe('20px');
      expect(html.overflowAnchor).toBe('auto');
    } finally {
      html.scrollPaddingTop = '';
      html.scrollPaddingBottom = '';
      html.overflowAnchor = '';
      if (hadResizeObserver) global.ResizeObserver = realResizeObserver;
      else Reflect.deleteProperty(global, 'ResizeObserver');
    }
  });

  it('measures the bottom padding from the edit bar’s top edge (BottomNav, wrapped rows, alerts) and follows resizes', async () => {
    let barTop = 768 - 162; // below lg: BottomNav (64) + a two-row bar (98)
    const realRect = Element.prototype.getBoundingClientRect;
    jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      if (this.id === 'dashboard-edit-bar') return toDomRect({ left: 0, top: barTop, width: 390, height: 98 });
      return realRect.call(this);
    });
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 768 });
    renderDashboard();
    await loaded();
    await enterEditMode();
    expect(document.documentElement.style.scrollPaddingBottom).toBe('170px');
    const root = customize().closest('.space-y-4') as HTMLElement;
    expect(root.style.paddingBottom).toBe('170px');

    barTop = 768 - 212; // the bar grew (an alert)
    act(() => {
      window.dispatchEvent(new Event('resize'));
    });
    expect(document.documentElement.style.scrollPaddingBottom).toBe('220px');
    expect(root.style.paddingBottom).toBe('220px');

    // A container that already pads below the page (Layout's <main> reserves BottomNav below
    // lg) is not counted twice: the page adds only the rest, and never a negative padding.
    const container = root.parentElement as HTMLElement;
    try {
      container.style.paddingBottom = '80px';
      act(() => {
        window.dispatchEvent(new Event('resize'));
      });
      expect(document.documentElement.style.scrollPaddingBottom).toBe('220px');
      expect(root.style.paddingBottom).toBe('140px');
      container.style.paddingBottom = '240px';
      act(() => {
        window.dispatchEvent(new Event('resize'));
      });
      expect(root.style.paddingBottom).toBe('0px');
    } finally {
      container.style.paddingBottom = '';
    }

    fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(queryBar()).not.toBeInTheDocument());
    expect(document.documentElement.style.scrollPaddingBottom).toBe('');
    expect(root.style.paddingBottom).toBe('');

    // The window listener left with edit mode: a resize after Done writes nothing, even with
    // an element that still answers to the bar's id for a leaked measurement to find.
    const straggler = document.createElement('div');
    straggler.id = EDIT_IDS.bar;
    document.body.appendChild(straggler);
    try {
      act(() => {
        window.dispatchEvent(new Event('resize'));
      });
      expect(document.documentElement.style.scrollPaddingBottom).toBe('');
      expect(root.style.paddingBottom).toBe('');
    } finally {
      straggler.remove();
    }
  });

  it('is refused while the body is the loading skeleton', async () => {
    let finishHeartbeat!: (value: Awaited<ReturnType<typeof api.getDashboardWithCache>>) => void;
    mockedApi.getDashboardWithCache.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finishHeartbeat = resolve;
        })
    );
    renderDashboard();
    await waitFor(() => expect(mockedApi.getDashboardLayout).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText('Loading dashboard…')).toBeInTheDocument();
    expect(customize()).toBeDisabled();
    await act(async () => finishHeartbeat({ data: dashboardData, fromCache: false, changed: true }));
    await waitForLoaded();
    expect(customize()).toBeEnabled();
  });

  it('is refused over the shop-data ErrorState, then allowed once the grid is back', async () => {
    mockedApi.getDashboardWithCache.mockRejectedValueOnce(new Error('Network unavailable'));
    renderDashboard();
    expect(await screen.findByText('Error loading dashboard')).toBeInTheDocument();
    expect(customize()).toBeDisabled();
    fireEvent.click(customize());
    expect(queryBar()).not.toBeInTheDocument();
    expect(mockedLoad).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /Try again|Retry/ }));
    await waitFor(() => expect(screen.queryByText('Error loading dashboard')).not.toBeInTheDocument());
    await waitFor(() => expect(customize()).toBeEnabled());
  });

  it('re-reads the saved layout on entry, so a save from another tab seeds the draft and its version', async () => {
    mockedApi.getDashboardLayout.mockResolvedValueOnce(
      layoutResponse(layoutOf({ id: 'kpi.overdue' }, { id: 'kpi.due_today' }), { version: 2 })
    );
    renderDashboard();
    await loaded();
    expect(cellIds(kpiStrip())).toEqual(['kpi.overdue', 'kpi.due_today']);
    // Another tab saved since this page loaded.
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(layoutOf({ id: 'kpi.due_today' }, { id: 'kpi.overdue' }), { version: 3 })
    );
    await enterEditMode();
    expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(2);
    expect(cellIds(kpiList())).toEqual(['kpi.due_today', 'kpi.overdue']);
    fireEvent.keyDown(byId(moveButtonId('kpi.overdue')), { key: 'ArrowLeft' });
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    });
    expect(mockedApi.saveDashboardLayout).toHaveBeenCalledWith(expect.objectContaining({ expected_version: 3 }));
  });

  it('Cancel after a 409 re-reads the layout: the view and the next Done use the server’s version', async () => {
    jest.spyOn(window, 'confirm').mockReturnValue(true);
    const server = { version: 2, layout: layoutOf({ id: 'kpi.overdue' }, { id: 'kpi.due_today' }) };
    mockedApi.getDashboardLayout.mockImplementation(async () =>
      layoutResponse(server.layout, { version: server.version })
    );
    mockedApi.saveDashboardLayout.mockImplementation(async body => {
      if (body.expected_version !== server.version) throw httpError(409, 'The layout changed in another tab.');
      server.version += 1;
      server.layout = body.layout;
      return layoutResponse(server.layout, { version: server.version });
    });
    renderDashboard();
    await loaded();
    await enterEditMode();
    // Another tab saves while this one edits.
    server.version = 3;
    server.layout = layoutOf({ id: 'kpi.due_today' }, { id: 'kpi.overdue' });
    fireEvent.keyDown(byId(moveButtonId('kpi.overdue')), { key: 'ArrowRight' });
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    });
    expect(await within(bar()).findByRole('alert')).toHaveTextContent('The layout changed in another tab.');

    const reads = mockedApi.getDashboardLayout.mock.calls.length;
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Cancel' }));
    });
    expect(queryBar()).not.toBeInTheDocument();
    expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(reads + 1);
    await waitFor(() => expect(cellIds(kpiStrip())).toEqual(['kpi.due_today', 'kpi.overdue']));
  });
});

it('never remounts a widget across entering, Cancel, Done and a save', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(
      layoutOf(
        { id: 'setup_nudge' },
        { id: 'morning_brief' },
        { id: 'kpi.overdue' },
        { id: 'kpi.due_today' },
        { id: 'work_center_status', size: 'wide' },
        { id: 'notifications' }
      )
    )
  );
  renderDashboard();
  await loaded();
  await screen.findByText('Finish setup — 40% complete');
  const panel = document.querySelector('[data-cockpit-panel="Work Center Status"]');
  const notifications = document.querySelector('[data-cockpit-panel="Notifications"]');
  expect(panel).not.toBeNull();
  expect(notifications).not.toBeNull();
  const sameNodes = () => {
    expect(document.querySelector('[data-cockpit-panel="Work Center Status"]')).toBe(panel);
    expect(document.querySelector('[data-cockpit-panel="Notifications"]')).toBe(notifications);
  };

  await enterEditMode();
  sameNodes();
  fireEvent.click(within(bar()).getByRole('button', { name: 'Cancel' }));
  expect(queryBar()).not.toBeInTheDocument();
  sameNodes();

  await enterEditMode();
  fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
  expect(queryBar()).not.toBeInTheDocument();
  sameNodes();

  await enterEditMode();
  const move = screen.getByRole('button', { name: 'Move Overdue' });
  move.focus();
  fireEvent.keyDown(move, { key: 'ArrowRight' });
  await act(async () => {
    fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
  });
  await waitFor(() => expect(queryBar()).not.toBeInTheDocument());
  expect(mockedApi.saveDashboardLayout).toHaveBeenCalledTimes(1);
  sameNodes();

  expect(mockedInvalidate).not.toHaveBeenCalled();
  expect(mockedApi.getSetupHealth).toHaveBeenCalledTimes(1);
  expect(mockedApi.getAIRecommendations).toHaveBeenCalledTimes(1);
});

describe('keyboard moves on the Move button', () => {
  beforeEach(() => {
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(layoutOf({ id: 'kpi.overdue' }, { id: 'kpi.due_today' }, { id: 'kpi.active_work_orders' }))
    );
  });

  it('reorders the DOM, keeps focus on the same Move button by id and announces each position', async () => {
    renderDashboard();
    await loaded();
    await enterEditMode();
    const moveId = moveButtonId('kpi.overdue');
    byId(moveId).focus();

    // A move LATER re-inserts the cell: React restores focus to the button, and the screen
    // reader reads it with its description, which ends with the new position.
    fireEvent.keyDown(byId(moveId), { key: 'ArrowRight' });
    expect(cellIds(kpiList())).toEqual(['kpi.due_today', 'kpi.overdue', 'kpi.active_work_orders']);
    expect(document.activeElement?.id).toBe(moveId);
    expect(byId(moveId)).toHaveAccessibleDescription(/Position 2 of 3\.$/);
    expect(announcer()).not.toHaveTextContent('Overdue, position 2 of 3.');

    fireEvent.keyDown(byId(moveId), { key: 'End' });
    expect(cellIds(kpiList())).toEqual(['kpi.due_today', 'kpi.active_work_orders', 'kpi.overdue']);
    expect(document.activeElement?.id).toBe(moveId);
    expect(byId(moveId)).toHaveAccessibleDescription(/Position 3 of 3\.$/);

    fireEvent.keyDown(byId(moveId), { key: 'ArrowDown' });
    expect(cellIds(kpiList())).toEqual(['kpi.due_today', 'kpi.active_work_orders', 'kpi.overdue']);
    expect(announcer()).toHaveTextContent('Overdue is already last.');

    fireEvent.keyDown(byId(moveId), { key: 'Home' });
    expect(cellIds(kpiList())).toEqual(['kpi.overdue', 'kpi.due_today', 'kpi.active_work_orders']);
    expect(announcer()).toHaveTextContent('Overdue, position 1 of 3.');

    fireEvent.keyDown(byId(moveId), { key: 'ArrowUp' });
    expect(announcer()).toHaveTextContent('Overdue is already first.');
    expect(document.activeElement?.id).toBe(moveId);
  });

  it('ignores modified arrows', async () => {
    renderDashboard();
    await loaded();
    await enterEditMode();
    const move = byId(moveButtonId('kpi.overdue'));
    move.focus();
    fireEvent.keyDown(move, { key: 'ArrowRight', shiftKey: true });
    fireEvent.keyDown(move, { key: 'ArrowRight', ctrlKey: true });
    fireEvent.keyDown(move, { key: 'ArrowRight', metaKey: true });
    fireEvent.keyDown(move, { key: 'ArrowRight', altKey: true });
    expect(cellIds(kpiList())).toEqual(['kpi.overdue', 'kpi.due_today', 'kpi.active_work_orders']);
  });
});

it('moves through the Move menu and resizes a panel in place', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(
      layoutOf(
        { id: 'capacity_overview', size: 'wide' },
        { id: 'work_center_status', size: 'wide' },
        { id: 'live_activity', size: 'narrow' }
      )
    )
  );
  renderDashboard();
  await loaded();
  await enterEditMode();
  fireEvent.click(screen.getByRole('button', { name: 'Move or resize Capacity Overview' }));
  const menu = screen.getByRole('group', { name: 'Move or resize Capacity Overview' });
  fireEvent.click(within(menu).getByRole('button', { name: 'Move to end' }));
  expect(cellIds(panelList())).toEqual(['work_center_status', 'live_activity', 'capacity_overview']);
  expect(screen.queryByRole('group', { name: 'Move or resize Capacity Overview' })).not.toBeInTheDocument();
  expect(document.activeElement?.id).toBe(moveButtonId('capacity_overview'));
  // Focus moved onto the button: its description (read on focus) ends with the position.
  expect(byId(moveButtonId('capacity_overview'))).toHaveAccessibleDescription(/Position 3 of 3\.$/);

  fireEvent.click(screen.getByRole('button', { name: 'Move or resize Capacity Overview' }));
  const reopened = screen.getByRole('group', { name: 'Move or resize Capacity Overview' });
  expect(cellOf('capacity_overview')).toHaveClass('xl:col-span-7');
  fireEvent.click(within(reopened).getByRole('radio', { name: /Third/ }));
  expect(cellOf('capacity_overview')).toHaveClass('xl:col-span-4');
  expect(cellOf('capacity_overview')).not.toHaveClass('xl:col-span-7');
  await waitFor(() => expect(announcer()).toHaveTextContent('Capacity Overview width: Third.'));
  // The width stays a one-tap choice: the menu stays open on the radio.
  expect(screen.getByRole('group', { name: 'Move or resize Capacity Overview' })).toBeInTheDocument();
});

it('removes along the zone, hands focus to a surviving control, and Undo restores', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(
      layoutOf(
        { id: 'kpi.overdue' },
        { id: 'kpi.due_today' },
        { id: 'kpi.active_work_orders' },
        { id: 'work_center_status', size: 'wide' }
      )
    )
  );
  renderDashboard();
  await loaded();
  await enterEditMode();

  fireEvent.click(screen.getByRole('button', { name: 'Remove Due Today' }));
  expect(cellIds(kpiList())).toEqual(['kpi.overdue', 'kpi.active_work_orders']);
  expect(document.activeElement?.id).toBe(removeButtonId('kpi.active_work_orders'));
  expect(announcer()).toHaveTextContent('Due Today removed from your dashboard. Undo is available.');

  fireEvent.click(screen.getByRole('button', { name: 'Remove Active Work Orders' }));
  expect(document.activeElement?.id).toBe(removeButtonId('kpi.overdue'));

  fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
  expect(within(bar()).getByRole('button', { name: 'Add widgets' })).toHaveFocus();
  expect(screen.queryByRole('list', { name: 'Key metrics on your dashboard' })).not.toBeInTheDocument();
  expect(screen.getByText(ZONE_PLACEHOLDERS.kpi.message)).toBeInTheDocument();

  const undo = within(bar()).getByRole('button', { name: 'Undo' });
  undo.focus();
  fireEvent.click(undo);
  expect(cellIds(kpiList())).toEqual(['kpi.overdue']);
  expect(announcer()).toHaveTextContent('Undone: removed Overdue.');
  expect(undo).toHaveFocus();
  fireEvent.click(undo);
  fireEvent.click(undo);
  expect(cellIds(kpiList())).toEqual(['kpi.overdue', 'kpi.due_today', 'kpi.active_work_orders']);
  expect(undo).toHaveAttribute('aria-disabled', 'true');
  expect(within(bar()).queryByText('Unsaved changes')).not.toBeInTheDocument();
});

describe('pointer drags (through the fake layer)', () => {
  beforeEach(() => {
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(
        layoutOf(
          { id: 'capacity_overview', size: 'wide' },
          { id: 'work_center_status', size: 'wide' },
          { id: 'live_activity', size: 'narrow' }
        )
      )
    );
  });

  it('renders the live order before the drop, commits it as one Undo step and focuses the Move button', async () => {
    renderDashboard();
    await loaded();
    await enterEditMode();
    const props = dragHarness.latest();
    expect(props.enabled).toBe(true);
    expect(props.orders.panel).toEqual(['capacity_overview', 'work_center_status', 'live_activity']);
    expect(props.describe('capacity_overview')).toEqual({ title: 'Capacity Overview', sizeLabel: 'Wide' });

    dragHarness.start('panel', 'capacity_overview');
    expect(cellOf('capacity_overview')).toHaveAttribute('data-dragging', 'true');
    expect(announcer()).toHaveTextContent('Picked up Capacity Overview, position 1 of 3.');

    dragHarness.reorder('panel', ['work_center_status', 'capacity_overview', 'live_activity']);
    // The real grid shows the landing slot before anything is committed.
    expect(cellIds(panelList())).toEqual(['work_center_status', 'capacity_overview', 'live_activity']);
    expect(dragHarness.latest().orders.panel).toEqual(['work_center_status', 'capacity_overview', 'live_activity']);
    expect(dragHarness.latest().layoutKey).not.toBe(props.layoutKey);

    dragHarness.end('panel', 'capacity_overview');
    expect(cellOf('capacity_overview')).not.toHaveAttribute('data-dragging');
    expect(cellIds(panelList())).toEqual(['work_center_status', 'capacity_overview', 'live_activity']);
    expect(document.activeElement?.id).toBe(moveButtonId('capacity_overview'));
    expect(announcer()).toHaveTextContent('Capacity Overview dropped at position 2 of 3.');

    const undo = within(bar()).getByRole('button', { name: 'Undo' });
    fireEvent.click(undo);
    expect(cellIds(panelList())).toEqual(['capacity_overview', 'work_center_status', 'live_activity']);
    expect(undo).toHaveAttribute('aria-disabled', 'true');
  });

  it('a cancelled drag glides back to the starting order and commits nothing', async () => {
    renderDashboard();
    await loaded();
    await enterEditMode();
    dragHarness.start('panel', 'live_activity');
    dragHarness.reorder('panel', ['live_activity', 'capacity_overview', 'work_center_status']);
    expect(cellIds(panelList())).toEqual(['live_activity', 'capacity_overview', 'work_center_status']);
    dragHarness.cancel('panel', 'live_activity');
    expect(cellIds(panelList())).toEqual(['capacity_overview', 'work_center_status', 'live_activity']);
    expect(announcer()).toHaveTextContent('Move cancelled. Live Shop Activity is back at position 3 of 3.');
    expect(document.activeElement?.id).toBe(moveButtonId('live_activity'));
    expect(within(bar()).getByRole('button', { name: 'Undo' })).toHaveAttribute('aria-disabled', 'true');
  });
});

describe('background refreshes while editing', () => {
  let visibility: DocumentVisibilityState = 'visible';
  beforeEach(() => {
    jest.useFakeTimers();
    visibility = 'visible';
    jest.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
  });
  afterEach(() => {
    jest.useRealTimers();
  });

  it('pauses polls and pushes only during a drag, then catches up exactly once', async () => {
    renderDashboard();
    await settle();
    await settle();
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(1);
    await enterEditModeWithFakeTimers();

    // Keyboard editing never pauses the poll.
    await advance(30_000);
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(2);

    dragHarness.start('kpi', 'kpi.overdue');
    await advance(30_000);
    act(() => push('work_order_update'));
    await advance(1_000);
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(2);

    dragHarness.end('kpi', 'kpi.overdue');
    await settle();
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(3);
    // One catch-up, not one per missed tick.
    await advance(1_000);
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(3);
  });

  it('holds a widget-initiated refresh (the Notifications unread count moved) until the drag ends', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(
        layoutOf({ id: 'kpi.overdue' }, { id: 'capacity_overview', size: 'wide' }, { id: 'notifications' })
      )
    );
    renderDashboard();
    await settle();
    await settle();
    await enterEditModeWithFakeTimers();
    await advance(2_000);
    const count = () => ({
      shop: mockedApi.getDashboardWithCache.mock.calls.length,
      capacity: mockedApi.getCapacityHeatmap.mock.calls.length,
      notifications: mockedApi.getNotifications.mock.calls.length,
      unread: mockedApi.getUnreadCount.mock.calls.length,
    });
    const before = count();

    dragHarness.start('panel', 'capacity_overview');
    mockedApi.getUnreadCount.mockResolvedValue(3);
    await advance(60_000);
    const during = count();
    // The bell polled and saw the new count; nothing the widget asked for went out mid-drag.
    expect(during.unread).toBeGreaterThan(before.unread);
    expect({ ...during, unread: before.unread }).toEqual(before);

    dragHarness.end('panel', 'capacity_overview');
    await settle();
    const after = count();
    expect(after.shop).toBe(before.shop + 1);
    expect(after.capacity).toBe(before.capacity + 1);
    expect(after.notifications).toBe(before.notifications + 1);
  });

  it('defers the catch-up to the hidden-tab rule when the drag ends in a hidden tab', async () => {
    renderDashboard();
    await settle();
    await settle();
    await enterEditModeWithFakeTimers();
    const before = mockedApi.getDashboardWithCache.mock.calls.length;

    dragHarness.start('kpi', 'kpi.overdue');
    await advance(30_000);
    visibility = 'hidden';
    // A tab switch cancels the drag (dnd-kit's visibilitychange listener).
    dragHarness.cancel('kpi', 'kpi.overdue');
    await settle();
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(before);

    visibility = 'visible';
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(before + 1);
  });

  it('does not catch up on a visibility change while the drag is still in progress', async () => {
    renderDashboard();
    await settle();
    await settle();
    await enterEditModeWithFakeTimers();
    const before = mockedApi.getDashboardWithCache.mock.calls.length;

    dragHarness.start('panel', 'capacity_overview');
    visibility = 'hidden';
    await advance(30_000);
    visibility = 'visible';
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(before);
    dragHarness.end('panel', 'capacity_overview');
    await settle();
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(before + 1);
  });

  it('catches up when edit mode ends mid-drag without an end callback', async () => {
    renderDashboard();
    await settle();
    await settle();
    await enterEditModeWithFakeTimers();
    const before = mockedApi.getDashboardWithCache.mock.calls.length;
    dragHarness.start('kpi', 'kpi.overdue');
    await advance(30_000);
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(before);
    // A session change ends edit mode mid-drag: the layer unmounts without calling back.
    await act(async () => {
      sessionStorage.setItem('token', tokenFor({ sub: '9', cid: 9 }));
      window.dispatchEvent(new Event('werco:auth-token-changed'));
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(dragHarness.mounted).toBe(false);
    expect(mockedApi.getDashboardWithCache).toHaveBeenCalledTimes(before + 1);
  });
});

describe('what edit mode fetches', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(layoutOf({ id: 'kpi.overdue' }, { id: 'capacity_overview', size: 'wide' }))
    );
  });
  afterEach(() => {
    jest.useRealTimers();
  });

  const start = async () => {
    renderDashboard();
    await settle();
    await settle();
    await enterEditModeWithFakeTimers();
  };
  const addFromGallery = async (name: string) => {
    fireEvent.click(within(bar()).getByRole('button', { name: 'Add widgets' }));
    await settle();
    const gallery = screen.getByRole('dialog', { name: 'Add widgets' });
    fireEvent.click(within(gallery).getByRole('button', { name }));
    fireEvent.click(within(gallery).getByRole('button', { name: 'Done' }));
    await settle();
  };
  const manualRefresh = async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Refresh dashboard' }));
    await settle();
  };

  it('fetches an added widget’s source only once its membership has settled for 400 ms', async () => {
    await start();
    await addFromGallery('Add Delivery & Yield KPIs');
    expect(cellIds(panelList())).toEqual(['capacity_overview', 'delivery_kpis']);
    await advance(398);
    expect(mockedApi.getKPIDashboard).not.toHaveBeenCalled();
    await advance(2);
    expect(mockedApi.getKPIDashboard).toHaveBeenCalledTimes(1);
  });

  it('sends nothing for an add that is undone within 400 ms', async () => {
    await start();
    await addFromGallery('Add Delivery & Yield KPIs');
    await advance(200);
    fireEvent.click(within(bar()).getByRole('button', { name: 'Undo' }));
    await advance(1_000);
    expect(mockedApi.getKPIDashboard).not.toHaveBeenCalled();
  });

  it('keeps a removed widget’s source until a Done that drops it', async () => {
    await start();
    const baseline = mockedApi.getCapacityHeatmap.mock.calls.length;
    expect(baseline).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: 'Remove Capacity Overview' }));
    await advance(1_000);
    await manualRefresh();
    expect(mockedApi.getCapacityHeatmap).toHaveBeenCalledTimes(baseline + 1);

    fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    await settle();
    expect(queryBar()).not.toBeInTheDocument();
    await manualRefresh();
    expect(mockedApi.getCapacityHeatmap).toHaveBeenCalledTimes(baseline + 1);
  });

  it('re-entering inside the 400 ms window never fetches the cancelled draft’s sources', async () => {
    jest.spyOn(window, 'confirm').mockReturnValue(true);
    await start();
    await addFromGallery('Add Open NCRs');
    await advance(400);
    expect(mockedApi.getQualitySummary).toHaveBeenCalledTimes(1);
    fireEvent.click(within(bar()).getByRole('button', { name: 'Cancel' }));
    await settle();
    await advance(100);
    // The settled value still names session 1's draft; session 2 must not fetch it.
    await enterEditModeWithFakeTimers();
    await advance(1_000);
    expect(mockedApi.getQualitySummary).toHaveBeenCalledTimes(1);
  });

  it('drops draft-only sources the moment Cancel leaves edit mode', async () => {
    jest.spyOn(window, 'confirm').mockReturnValue(true);
    await start();
    await addFromGallery('Add Open NCRs');
    await advance(400);
    expect(mockedApi.getQualitySummary).toHaveBeenCalledTimes(1);
    // Still needed while editing: a manual refresh asks again.
    await manualRefresh();
    expect(mockedApi.getQualitySummary).toHaveBeenCalledTimes(2);

    fireEvent.click(within(bar()).getByRole('button', { name: 'Cancel' }));
    await settle();
    expect(queryBar()).not.toBeInTheDocument();
    await manualRefresh();
    expect(mockedApi.getQualitySummary).toHaveBeenCalledTimes(2);
  });
});

describe('Done', () => {
  it('saves the expected placements under the last read’s version, anchoring widgets the viewer cannot see', async () => {
    mockUser = { id: 6, role: 'shipping', is_superuser: false };
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(
        layoutOf(
          { id: 'kpi.open_cars' },
          { id: 'kpi.overdue' },
          { id: 'kpi.due_today' },
          { id: 'quality_pulse', size: 'third' },
          { id: 'work_center_status', size: 'full' }
        ),
        { version: 7 }
      )
    );
    renderDashboard();
    await loaded();
    expect(cellIds(kpiStrip())).toEqual(['kpi.overdue', 'kpi.due_today']);
    await enterEditMode();
    const move = byId(moveButtonId('kpi.overdue'));
    move.focus();
    fireEvent.keyDown(move, { key: 'ArrowRight' });
    expect(within(bar()).getByText('Unsaved changes')).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    });

    expect(mockedApi.saveDashboardLayout).toHaveBeenCalledTimes(1);
    expect(mockedApi.saveDashboardLayout).toHaveBeenCalledWith({
      expected_company_id: 1,
      expected_version: 7,
      layout: {
        format_version: 1,
        widgets: [
          { id: 'kpi.open_cars' },
          { id: 'kpi.due_today' },
          { id: 'kpi.overdue' },
          { id: 'quality_pulse', size: 'third' },
          { id: 'work_center_status', size: 'full' },
        ],
      },
    });
    await waitFor(() => expect(queryBar()).not.toBeInTheDocument());
    expect(await screen.findByText(SAVED_TOAST)).toBeInTheDocument();
    expect(customize()).toHaveFocus();
    expect(cellIds(kpiStrip())).toEqual(['kpi.due_today', 'kpi.overdue']);
  });

  it('saves a width chosen in the Move menu and a banner switched off in the gallery, in one PUT', async () => {
    renderDashboard();
    await loaded();
    await enterEditMode();
    fireEvent.click(screen.getByRole('button', { name: 'Move or resize Capacity Overview' }));
    fireEvent.click(
      within(screen.getByRole('group', { name: 'Move or resize Capacity Overview' })).getByRole('radio', {
        name: /Full width/,
      })
    );
    const gallery = await openGallery();
    fireEvent.click(within(gallery).getByRole('checkbox', { name: /Morning brief/ }));
    fireEvent.click(within(gallery).getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add widgets' })).not.toBeInTheDocument());
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    });
    expect(mockedApi.saveDashboardLayout).toHaveBeenCalledTimes(1);
    const body = mockedApi.saveDashboardLayout.mock.calls[0][0];
    expect(body.expected_version).toBe(0);
    expect(body.layout.widgets).toContainEqual({ id: 'capacity_overview', size: 'full' });
    expect(body.layout.widgets.map(widget => widget.id)).not.toContain('morning_brief');
    await waitFor(() => expect(queryBar()).not.toBeInTheDocument());
    expect(cellOf('capacity_overview')).toHaveClass('xl:col-span-12');
  });

  it('leaves without a request when nothing changed', async () => {
    renderDashboard();
    await loaded();
    await enterEditMode();
    fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    expect(queryBar()).not.toBeInTheDocument();
    expect(mockedApi.saveDashboardLayout).not.toHaveBeenCalled();
    expect(announcer()).toHaveTextContent(DONE_UNCHANGED_ANNOUNCEMENT);
    expect(customize()).toHaveFocus();
    expect(dragHarness.mounted).toBe(false);
  });

  it('locks dragging after a 409 until the saved layout is reloaded', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(layoutOf({ id: 'kpi.overdue' }, { id: 'kpi.due_today' }))
    );
    mockedApi.saveDashboardLayout.mockRejectedValue(httpError(409, 'The layout changed in another tab.'));
    renderDashboard();
    await loaded();
    await enterEditMode();
    fireEvent.keyDown(byId(moveButtonId('kpi.overdue')), { key: 'ArrowRight' });
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    });

    const alert = await within(bar()).findByRole('alert');
    expect(alert).toHaveTextContent('The layout changed in another tab.');
    const reload = within(alert).getByRole('button', { name: 'Reload saved layout' });
    expect(reload).toHaveFocus();
    expect(dragHarness.latest().enabled).toBe(false);
    expect(screen.getByRole('button', { name: 'Move Overdue' })).toBeDisabled();
    // A pickup attempt starts nothing.
    dragHarness.start('kpi', 'kpi.overdue');
    expect(cellOf('kpi.overdue')).not.toHaveAttribute('data-dragging');

    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(layoutOf({ id: 'kpi.due_today' }, { id: 'kpi.overdue' }), { version: 5 })
    );
    await act(async () => {
      fireEvent.click(reload);
    });
    await waitFor(() => expect(heading()).toHaveFocus());
    expect(dragHarness.latest().enabled).toBe(true);
    expect(cellIds(kpiList())).toEqual(['kpi.due_today', 'kpi.overdue']);
    expect(
      within(bar()).getByText('Reloaded your saved layout. Your unsaved changes were discarded.')
    ).toBeInTheDocument();
  });
});

describe('who may customize', () => {
  it('a read-only session gets the info toast and never enters edit mode', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(layoutOf({ id: 'kpi.overdue' }), { can_edit: false })
    );
    renderDashboard();
    await loaded();
    expect(customize()).toBeEnabled();
    expect(customize()).toHaveAccessibleDescription(READ_ONLY_CUSTOMIZE_DESCRIPTION);
    fireEvent.click(customize());
    expect(await screen.findByText(READ_ONLY_LAYOUT_NOTICE)).toBeInTheDocument();
    expect(queryBar()).not.toBeInTheDocument();
    expect(mockedLoad).not.toHaveBeenCalled();
    // Read-only belongs to the session scope: refused at once, no re-read, Customize keeps focus.
    expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(1);
    expect(customize()).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Move Overdue' })).not.toBeInTheDocument();
    expect(mockedApi.saveDashboardLayout).not.toHaveBeenCalled();
  });

  it('re-reads a layout that failed to load before entering', async () => {
    mockedApi.getDashboardLayout
      .mockRejectedValueOnce(httpError(500))
      .mockResolvedValue(layoutResponse(layoutOf({ id: 'kpi.overdue' })));
    renderDashboard();
    await loaded();
    await enterEditMode();
    expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(2);
    expect(cellIds(kpiList())).toEqual(['kpi.overdue']);
  });

  it('refuses after the re-read when it answers read-only, and says so when it fails again', async () => {
    mockedApi.getDashboardLayout
      .mockRejectedValueOnce(httpError(500))
      .mockResolvedValueOnce(layoutResponse(null, { can_edit: false }));
    renderDashboard();
    await loaded();
    fireEvent.click(customize());
    expect(await screen.findByText(READ_ONLY_LAYOUT_NOTICE)).toBeInTheDocument();
    expect(queryBar()).not.toBeInTheDocument();
    expect(mockedLoad).not.toHaveBeenCalled();
  });

  it('says so, and stays in view mode, when the re-read fails', async () => {
    mockedApi.getDashboardLayout.mockRejectedValue(httpError(500));
    renderDashboard();
    await loaded();
    fireEvent.click(customize());
    expect(
      await screen.findByText("Your saved dashboard layout couldn't be loaded. Try again in a moment.")
    ).toBeInTheDocument();
    expect(queryBar()).not.toBeInTheDocument();
    expect(mockedLoad).not.toHaveBeenCalled();
    // The button was disabled while it worked; focus comes back to it.
    await waitFor(() => expect(customize()).toHaveFocus());
  });

  it('enters with the layout already on screen when the entry re-read of a loaded layout fails', async () => {
    mockedApi.getDashboardLayout
      .mockResolvedValueOnce(layoutResponse(layoutOf({ id: 'kpi.due_today' }, { id: 'kpi.overdue' }), { version: 5 }))
      .mockRejectedValue(httpError(503, 'Service unavailable'));
    renderDashboard();
    await loaded();
    await enterEditMode();
    expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(2);
    // The on-screen layout seeds the draft: no refusal, no fallback to the default.
    expect(cellIds(kpiList())).toEqual(['kpi.due_today', 'kpi.overdue']);
    expect(
      screen.queryByText("Your saved dashboard layout couldn't be loaded. Try again in a moment.")
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/showing the default/)).not.toBeInTheDocument();
    // And it saves under the version that layout was read at.
    fireEvent.keyDown(byId(moveButtonId('kpi.overdue')), { key: 'ArrowLeft' });
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    });
    expect(mockedApi.saveDashboardLayout).toHaveBeenCalledWith(expect.objectContaining({ expected_version: 5 }));
  });
});

it('a session change mid-edit discards the dirty draft at once, warns, saves nothing and fetches nothing for it', async () => {
  jest.useFakeTimers();
  try {
    sessionStorage.setItem('token', tokenFor({ sub: '1', cid: 1 }));
    let resolveNext!: (value: DashboardLayoutResponse) => void;
    mockedApi.getDashboardLayout
      .mockResolvedValueOnce(layoutResponse(layoutOf({ id: 'kpi.overdue' })))
      // Customize re-reads the saved layout before entering.
      .mockResolvedValueOnce(layoutResponse(layoutOf({ id: 'kpi.overdue' })))
      .mockImplementationOnce(
        () =>
          new Promise<DashboardLayoutResponse>(resolve => {
            resolveNext = resolve;
          })
      );
    renderDashboard();
    await settle();
    await settle();
    await enterEditModeWithFakeTimers();
    fireEvent.click(within(bar()).getByRole('button', { name: 'Add widgets' }));
    await settle();
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Add widgets' })).getByRole('button', { name: 'Add Open NCRs' })
    );
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Add widgets' })).getByRole('button', { name: 'Done' }));
    await settle();
    expect(byId(moveButtonId('kpi.open_ncrs'))).toHaveFocus();
    expect(mockedApi.getQualitySummary).not.toHaveBeenCalled();

    await act(async () => {
      sessionStorage.setItem('token', tokenFor({ sub: '1', cid: 2 }));
      window.dispatchEvent(new Event('werco:auth-token-changed'));
    });
    // Inactive in the very render the scope changed: no bar, no chrome, no layer.
    expect(queryBar()).not.toBeInTheDocument();
    expect(dragHarness.mounted).toBe(false);
    expect(customize()).toBeDisabled();
    expect(await screen.findByText(SESSION_CHANGED_TOAST)).toBeInTheDocument();
    expect(document.getElementById('main-content')).toHaveFocus();

    // The new session's layout resolves inside the old draft's 400 ms settle window.
    await act(async () => {
      resolveNext(layoutResponse(layoutOf({ id: 'kpi.overdue' }), { company_id: 2 }));
      await jest.advanceTimersByTimeAsync(0);
    });
    await advance(1_000);
    expect(cellIds(kpiStrip())).toEqual(['kpi.overdue']);
    expect(mockedApi.getQualitySummary).not.toHaveBeenCalled();
    expect(mockedApi.saveDashboardLayout).not.toHaveBeenCalled();
    expect(customize()).toBeEnabled();
  } finally {
    jest.useRealTimers();
  }
});

it('stays in view mode with an error toast when the editor chunk fails to load, and retries on the next click', async () => {
  mockedLoad.mockRejectedValueOnce(new Error('Failed to fetch dynamically imported module'));
  renderDashboard();
  await loaded();
  fireEvent.click(customize());
  expect(await screen.findByText(EDITOR_LOAD_FAILED)).toBeInTheDocument();
  expect(queryBar()).not.toBeInTheDocument();
  await waitFor(() => expect(customize()).toHaveFocus());
  expect(customize()).toBeEnabled();
  expect(customize()).not.toHaveAttribute('aria-busy');

  await enterEditMode();
  expect(mockedLoad).toHaveBeenCalledTimes(2);
});

it('does not enter edit mode when the session changed while the editor chunk was loading', async () => {
  sessionStorage.setItem('token', tokenFor({ sub: '1', cid: 1 }));
  let resolveLoad!: (layer: typeof FakeDragLayer) => void;
  mockedLoad.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        resolveLoad = resolve;
      })
  );
  renderDashboard();
  await loaded();
  fireEvent.click(customize());
  expect(customize()).toBeDisabled();
  expect(customize()).toHaveAttribute('aria-busy', 'true');

  await act(async () => {
    sessionStorage.setItem('token', tokenFor({ sub: '1', cid: 2 }));
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  await waitForLoaded();
  await act(async () => resolveLoad(FakeDragLayer));
  expect(queryBar()).not.toBeInTheDocument();
  expect(dragHarness.mounted).toBe(false);
  expect(customize()).toBeEnabled();
});

it('asks before Cancel discards a dirty draft, and keeps editing when declined', async () => {
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(layoutOf({ id: 'kpi.overdue' }, { id: 'kpi.due_today' }))
  );
  renderDashboard();
  await loaded();

  // Clean: no question.
  await enterEditMode();
  fireEvent.click(within(bar()).getByRole('button', { name: 'Cancel' }));
  expect(confirm).not.toHaveBeenCalled();
  expect(queryBar()).not.toBeInTheDocument();

  await enterEditMode();
  fireEvent.keyDown(byId(moveButtonId('kpi.overdue')), { key: 'ArrowRight' });
  fireEvent.click(within(bar()).getByRole('button', { name: 'Cancel' }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(bar()).toBeInTheDocument();
  expect(cellIds(kpiList())).toEqual(['kpi.due_today', 'kpi.overdue']);

  confirm.mockReturnValue(true);
  fireEvent.click(within(bar()).getByRole('button', { name: 'Cancel' }));
  expect(queryBar()).not.toBeInTheDocument();
  expect(cellIds(kpiStrip())).toEqual(['kpi.overdue', 'kpi.due_today']);
  expect(announcer()).toHaveTextContent(CANCELLED_ANNOUNCEMENT);
  expect(customize()).toHaveFocus();
  expect(mockedApi.saveDashboardLayout).not.toHaveBeenCalled();
});

it('never leaves edit mode on Escape', async () => {
  renderDashboard();
  await loaded();
  await enterEditMode();
  fireEvent.keyDown(heading(), { key: 'Escape' });
  fireEvent.keyDown(document.body, { key: 'Escape' });
  const move = screen.getByRole('button', { name: 'Move Overdue' });
  move.focus();
  fireEvent.keyDown(move, { key: 'Escape' });
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(bar()).toBeInTheDocument();
  expect(dragHarness.mounted).toBe(true);
});

describe('entering from an empty or banners-only dashboard', () => {
  it('shows both zone placeholders from the empty state, and they open the gallery', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(layoutOf()));
    renderDashboard();
    await loaded();
    const [, emptyStateButton] = screen.getAllByRole('button', { name: 'Customize dashboard' });
    await enterEditMode(emptyStateButton);
    expect(screen.queryByText('Your dashboard is empty')).not.toBeInTheDocument();
    expect(screen.getByText(ZONE_PLACEHOLDERS.kpi.message)).toBeInTheDocument();
    expect(screen.getByText(ZONE_PLACEHOLDERS.panel.message)).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Key metrics on your dashboard' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: ZONE_PLACEHOLDERS.panel.action }));
    const gallery = await screen.findByRole('dialog', { name: 'Add widgets' });
    // "Add panels" lands on the Panels section, past the banners and key metrics above it.
    await waitFor(() => expect(within(gallery).getByRole('heading', { name: 'Panels' })).toHaveFocus());
    expect(mockedApi.getQualitySummary).not.toHaveBeenCalled();
    expect(mockedApi.getCapacityHeatmap).not.toHaveBeenCalled();

    fireEvent.click(within(gallery).getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add widgets' })).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: ZONE_PLACEHOLDERS.kpi.action }));
    const again = await screen.findByRole('dialog', { name: 'Add widgets' });
    await waitFor(() => expect(within(again).getByRole('heading', { name: 'Key metrics' })).toHaveFocus());
  });

  it('"Add widgets" in the bar still opens the gallery at the top', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(layoutOf()));
    renderDashboard();
    await loaded();
    const [, emptyStateButton] = screen.getAllByRole('button', { name: 'Customize dashboard' });
    await enterEditMode(emptyStateButton);
    const gallery = await openGallery();
    await waitFor(() => expect(gallery.contains(document.activeElement)).toBe(true));
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 10));
    });
    expect(document.activeElement?.tagName).not.toBe('H3');
  });

  it('suppresses the banners-only note while editing and shows the placeholders', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(layoutOf({ id: 'morning_brief' }, { id: 'alert_chips' }))
    );
    renderDashboard();
    await loaded();
    const anchor = document.querySelector('[data-tour="dashboard-stats"]') as HTMLElement;
    await enterEditMode(within(anchor).getByRole('button', { name: 'Customize dashboard' }));
    expect(screen.queryByText(/Only alerts and banners are on your dashboard/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: ZONE_PLACEHOLDERS.kpi.action })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: ZONE_PLACEHOLDERS.panel.action })).toBeInTheDocument();
  });

  /** The empty state's or the banners-only note's Customize (whichever this layout shows). */
  const secondaryCustomize = () => {
    const others = screen
      .getAllByRole('button', { name: 'Customize dashboard' })
      .filter(button => button !== customize());
    expect(others).toHaveLength(1);
    return others[0];
  };

  it.each<[string, DashboardLayoutValues, 'header' | 'secondary']>([
    ['a double click on the empty state’s Customize', layoutOf(), 'secondary'],
    [
      'a double click on the banners-only note’s Customize',
      layoutOf({ id: 'morning_brief' }, { id: 'alert_chips' }),
      'secondary',
    ],
    ['the header Customize, then the empty state’s while that entry works,', layoutOf(), 'header'],
  ])('%s re-reads, loads the drag layer and enters only once', async (_case, saved, firstClick) => {
    mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(saved));
    renderDashboard();
    await loaded();
    const reads = mockedApi.getDashboardLayout.mock.calls.length;
    let finishRead!: (response: DashboardLayoutResponse) => void;
    mockedApi.getDashboardLayout.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finishRead = resolve;
        })
    );
    // Every announcement is a fresh keyed span in the announcer: collect what it spoke.
    const spoken: string[] = [];
    const observer = new MutationObserver(records =>
      records.forEach(record => record.addedNodes.forEach(node => spoken.push(node.textContent ?? '')))
    );
    observer.observe(announcer(), { childList: true });
    try {
      const secondary = secondaryCustomize();
      fireEvent.click(firstClick === 'header' ? customize() : secondary);
      // Only the header button disables while the entry re-reads; this one stays clickable,
      // so the second click reaches handleCustomize and only its in-flight guard stops it.
      expect(customize()).toBeDisabled();
      expect(secondary).toBeEnabled();
      fireEvent.click(secondary);
      await act(async () => finishRead(layoutResponse(saved)));
      await waitFor(() => expect(heading()).toHaveFocus());
      // Room for a second entry, had one started, to land.
      await act(async () => {
        await new Promise(resolve => setTimeout(resolve, 20));
      });
      expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(reads + 1);
      expect(mockedLoad).toHaveBeenCalledTimes(1);
      expect(spoken.filter(text => text === ENTER_ANNOUNCEMENT)).toEqual([ENTER_ANNOUNCEMENT]);
    } finally {
      observer.disconnect();
    }
  });

  it('saving an empty layout shows the empty state and focuses the header Customize button', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(layoutOf({ id: 'kpi.overdue' })));
    renderDashboard();
    await loaded();
    await enterEditMode();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    await act(async () => {
      fireEvent.click(within(bar()).getByRole('button', { name: 'Done' }));
    });
    expect(mockedApi.saveDashboardLayout).toHaveBeenCalledWith({
      expected_company_id: 1,
      expected_version: 2,
      layout: { format_version: 1, widgets: [] },
    });
    await waitFor(() => expect(queryBar()).not.toBeInTheDocument());
    expect(screen.getByText('Your dashboard is empty')).toBeInTheDocument();
    expect(customize()).toHaveFocus();
  });
});

it('hands focus to the last-added widget once the gallery closes', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(layoutResponse(layoutOf({ id: 'kpi.overdue' })));
  renderDashboard();
  await loaded();
  await enterEditMode();
  const gallery = await openGallery();
  fireEvent.click(within(gallery).getByRole('button', { name: 'Add Due Today' }));
  expect(within(gallery).getByTestId('gallery-announcement')).toHaveTextContent('Due Today added at position 2 of 2.');
  fireEvent.click(within(gallery).getByRole('button', { name: 'Add Open NCRs' }));
  fireEvent.click(within(gallery).getByRole('button', { name: 'Done' }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add widgets' })).not.toBeInTheDocument());
  await waitFor(() => expect(document.activeElement?.id).toBe(moveButtonId('kpi.open_ncrs')));
  expect(cellIds(kpiList())).toEqual(['kpi.overdue', 'kpi.due_today', 'kpi.open_ncrs']);
  expect(cellOf('kpi.open_ncrs')).toHaveClass('ring-2');
});

describe('layout animation (FLIP) and the editor’s focus', () => {
  /** Every cell's box follows its DOM position (100 px a row), so a reorder moves boxes. */
  function stubCellBoxes() {
    const realRect = Element.prototype.getBoundingClientRect;
    jest.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      if (this.hasAttribute('data-widget-cell') && this.parentElement) {
        const index = Array.from(this.parentElement.children).indexOf(this);
        return toDomRect({ left: 0, top: 200 + index * 100, width: 300, height: 90 });
      }
      return realRect.call(this);
    });
  }

  beforeEach(() => {
    mockedApi.getDashboardLayout.mockResolvedValue(
      layoutResponse(
        layoutOf(
          { id: 'kpi.overdue' },
          { id: 'kpi.due_today' },
          { id: 'capacity_overview', size: 'full' },
          { id: 'work_center_status', size: 'full' },
          { id: 'live_activity', size: 'full' }
        )
      )
    );
  });

  it('glides the cells a keyboard move displaces, and glides a cancelled drag back', async () => {
    const fake = installFakeAnimate();
    try {
      stubCellBoxes();
      renderDashboard();
      await loaded();
      await enterEditMode();
      fake.clear();
      fireEvent.keyDown(byId(moveButtonId('kpi.overdue')), { key: 'ArrowRight' });
      const moved = fake.calls.map(call => call.element.getAttribute('data-widget-cell'));
      expect(moved).toEqual(expect.arrayContaining(['kpi.overdue', 'kpi.due_today']));
      expect(fake.calls[0].keyframes).toEqual([
        { transform: expect.stringMatching(/^translate\(/) },
        { transform: 'none' },
      ]);

      dragHarness.start('panel', 'live_activity');
      dragHarness.reorder('panel', ['live_activity', 'capacity_overview', 'work_center_status']);
      fake.clear();
      dragHarness.cancel('panel', 'live_activity');
      expect(fake.calls.map(call => call.element.getAttribute('data-widget-cell'))).toEqual(
        expect.arrayContaining(['live_activity', 'capacity_overview'])
      );
    } finally {
      fake.restore();
    }
  });

  it('reveals a moved widget BEFORE FLIP translates it back to where it was (focus measures the real layout)', async () => {
    const fake = installFakeAnimate();
    const scrollIntoView = window.HTMLElement.prototype.scrollIntoView as jest.Mock;
    try {
      stubCellBoxes();
      renderDashboard();
      await loaded();
      await enterEditMode();
      fake.clear();
      scrollIntoView.mockClear();
      const move = byId(moveButtonId('capacity_overview'));
      move.focus();
      fireEvent.keyDown(move, { key: 'End' });
      const revealed = scrollIntoView.mock.contexts.findIndex(
        element => (element as HTMLElement).getAttribute('data-widget-cell') === 'capacity_overview'
      );
      expect(revealed).toBeGreaterThanOrEqual(0);
      expect(fake.animate).toHaveBeenCalled();
      expect(scrollIntoView.mock.invocationCallOrder[revealed]).toBeLessThan(fake.animate.mock.invocationCallOrder[0]);
    } finally {
      fake.restore();
    }
  });

  it('follows prefers-reduced-motion: no glide, a jump instead of a smooth reveal, and no drop animation', async () => {
    const REDUCE = '(prefers-reduced-motion: reduce)';
    const realMatchMedia = window.matchMedia;
    let reduced = false;
    const listeners = new Set<() => void>();
    // setupTests defines matchMedia writable (not configurable): assign, never redefine.
    window.matchMedia = (query: string): MediaQueryList => ({
      get matches() {
        return query === REDUCE && reduced;
      },
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
        if (query === REDUCE && typeof listener === 'function') listeners.add(listener as () => void);
      },
      removeEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
        if (typeof listener === 'function') listeners.delete(listener as () => void);
      },
      dispatchEvent: () => true,
    });
    const fake = installFakeAnimate();
    const scrollIntoView = window.HTMLElement.prototype.scrollIntoView as jest.Mock;
    try {
      stubCellBoxes();
      renderDashboard();
      await loaded();
      await enterEditMode();
      expect(dragHarness.latest().reducedMotion).toBe(false);

      // The viewer turns reduced motion on mid-session: everything follows at once.
      act(() => {
        reduced = true;
        Array.from(listeners).forEach(listener => listener());
      });
      expect(dragHarness.latest().reducedMotion).toBe(true);

      fake.clear();
      scrollIntoView.mockClear();
      const move = byId(moveButtonId('capacity_overview'));
      move.focus();
      fireEvent.keyDown(move, { key: 'End' });
      expect(cellIds(panelList()).at(-1)).toBe('capacity_overview');
      expect(fake.calls).toHaveLength(0);
      const revealed = scrollIntoView.mock.contexts.findIndex(
        element => (element as HTMLElement).getAttribute('data-widget-cell') === 'capacity_overview'
      );
      expect(revealed).toBeGreaterThanOrEqual(0);
      expect(scrollIntoView.mock.calls[revealed][0]).toEqual({ block: 'nearest', behavior: 'auto' });
    } finally {
      fake.restore();
      window.matchMedia = realMatchMedia;
    }
  });
});

it('a live drag reorder re-renders no widget: its visible membership did not change', async () => {
  mockedApi.getDashboardLayout.mockResolvedValue(
    layoutResponse(
      layoutOf(
        { id: 'capacity_overview', size: 'wide' },
        { id: 'work_center_status', size: 'wide' },
        { id: 'live_activity', size: 'narrow' }
      )
    )
  );
  renderDashboard();
  await loaded();
  await enterEditMode();
  const before = mockWorkCenterRenders;
  dragHarness.start('panel', 'capacity_overview');
  dragHarness.reorder('panel', ['work_center_status', 'capacity_overview', 'live_activity']);
  dragHarness.reorder('panel', ['work_center_status', 'live_activity', 'capacity_overview']);
  dragHarness.end('panel', 'capacity_overview');
  expect(mockWorkCenterRenders).toBe(before);
  // A membership change still reaches it.
  fireEvent.click(screen.getByRole('button', { name: 'Remove Live Shop Activity' }));
  expect(mockWorkCenterRenders).toBeGreaterThan(before);
});
