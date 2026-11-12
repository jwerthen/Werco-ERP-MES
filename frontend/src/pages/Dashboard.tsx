import { PageHeader } from '../components/ui/PageHeader';
import React, { useEffect, useLayoutEffect, useState, useCallback, useRef, useMemo } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { SkeletonDashboard } from '../components/ui/Skeleton';
import { Button, EmptyState, ErrorState, useToast } from '../components/ui';
import DashboardOperationDetails from '../components/dashboard/DashboardOperationDetails';
import { DashboardWidgetProvider, type DashboardWidgetEnv } from '../components/dashboard/DashboardWidgetContext';
import { isEmptyLayout, resolveLayout, type EditableZone } from '../components/dashboard/dashboardLayout';
import { getSourceSpec } from '../components/dashboard/sources';
import { useDashboardLayout, useDashboardSessionScope } from '../components/dashboard/useDashboardLayout';
import { parseNeededKey, toNeededKey, useDashboardSources } from '../components/dashboard/useDashboardSources';
import { useWidgetEligibility } from '../components/dashboard/useWidgetEligibility';
import { WidgetCell } from '../components/dashboard/WidgetCell';
import { createDragBridge } from '../components/dashboard/editor/dragBridge';
import { EditBar } from '../components/dashboard/editor/EditBar';
import {
  EDITOR_LOAD_FAILED,
  EDIT_IDS,
  READ_ONLY_CUSTOMIZE_DESCRIPTION,
  READ_ONLY_LAYOUT_NOTICE,
  ZONE_LIST_LABELS,
  ZONE_PLACEHOLDERS,
} from '../components/dashboard/editor/editorCopy';
import {
  loadDashboardDragLayer,
  prefetchDashboardDragLayer,
  type DashboardDragLayerComponent,
} from '../components/dashboard/editor/loadDragLayer';
import { useFlipLayout } from '../components/dashboard/editor/useFlipLayout';
import { useLayoutEditor } from '../components/dashboard/editor/useLayoutEditor';
import { WidgetGallery } from '../components/dashboard/editor/WidgetGallery';
import {
  CORE_WIDGET_RENDERERS,
  CoreDashboardProvider,
  type CoreDashboardEnv,
} from '../components/dashboard/widgets/core';
import { EXTRA_WIDGET_RENDERERS } from '../components/dashboard/widgets/extra';
import type { DashboardWidgetDefinition } from '../components/dashboard/widgetTypes';
import { ActiveAssignment, DashboardData } from '../types';
import type { DashboardLayoutResponse } from '../types/dashboardLayout';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { usePrefersReducedMotion } from '../hooks/usePrefersReducedMotion';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { formatCentralTime } from '../utils/centralTime';
import { ArrowPathIcon, Squares2X2Icon, WrenchScrewdriverIcon } from '@heroicons/react/24/outline';

const isTabHidden = () => document.visibilityState !== 'visible';

/**
 * Edit mode fetches the sources of widgets the draft adds once its membership has been still
 * this long, so an add followed by Undo (or a remove and re-add) sends nothing.
 */
const EDIT_SOURCE_SETTLE_MS = 400;
const READ_ONLY_DESCRIPTION_ID = 'dashboard-customize-read-only';
const LAYOUT_LOAD_FAILED = "Your saved dashboard layout couldn't be loaded. Try again in a moment.";
/** Space kept between a focus-scrolled control and the fixed edit bar's top edge. */
const EDIT_BAR_CLEARANCE_PX = 8;

/** Core renderers first (a Map — widget ids are user data), then the opt-in catalog's. */
function rendererFor(definition: DashboardWidgetDefinition) {
  return CORE_WIDGET_RENDERERS.get(definition.id) ?? EXTRA_WIDGET_RENDERERS.get(definition.id);
}

/**
 * The draft's sources from a SETTLED edit-source tag, only when the tag was made under this
 * edit session: a debounced value left over from another session (Cancel, then Customize again
 * inside the settle window) is never fetched. The session id alone decides it — it grows on
 * every entry and never resets within a mount; the scope check is defense in depth.
 */
function settledDraftSources(settled: string, scope: string | null, sessionId: number | null): string {
  if (!settled || sessionId === null) return '';
  try {
    const parsed: unknown = JSON.parse(settled);
    if (Array.isArray(parsed) && parsed[0] === scope && parsed[1] === sessionId && typeof parsed[2] === 'string') {
      return parsed[2];
    }
  } catch {
    // Not a tag: nothing to add.
  }
  return '';
}

/** An edit-mode zone with nothing in it: says so and opens the gallery. Sits outside the zone list. */
function ZonePlaceholder({ zone, disabled, onAdd }: { zone: EditableZone; disabled: boolean; onAdd: () => void }) {
  const copy = ZONE_PLACEHOLDERS[zone];
  return (
    <div
      data-zone-placeholder={zone}
      className="flex flex-wrap items-center justify-between gap-3 border border-dashed border-fd-line-bright p-3"
    >
      <p className="text-sm text-slate-300">{copy.message}</p>
      <Button variant="secondary" size="sm" disabled={disabled} onClick={onAdd}>
        {copy.action}
      </Button>
    </div>
  );
}

export default function Dashboard() {
  const { showToast } = useToast();
  const scope = useDashboardSessionScope();
  const layout = useDashboardLayout(scope);
  const { eligibility, canOpen } = useWidgetEligibility();
  const resolved = useMemo(() => resolveLayout(layout.saved, eligibility), [layout.saved, eligibility]);

  /** Written into a persistent polite region; the child is re-keyed so a repeated message is spoken again. */
  const [announcement, setAnnouncement] = useState<{ text: string; key: number }>({ text: '', key: 0 });
  const announce = useCallback((text: string) => setAnnouncement(current => ({ text, key: current.key + 1 })), []);

  // ── Edit mode ──────────────────────────────────────────────────────────────
  const reducedMotion = usePrefersReducedMotion();
  const rootRef = useRef<HTMLDivElement>(null);
  /** FLIP's `capture`, filled in once `useFlipLayout` has run (see the hook order below). */
  const flipCaptureRef = useRef<(() => void) | null>(null);
  const captureLayout = useCallback(() => flipCaptureRef.current?.(), []);
  const [bridge] = useState(createDragBridge);
  // useLayoutEditor BEFORE useFlipLayout: within one commit the editor's focus (and the
  // scroll that reveals a moved widget) then measures the committed layout, before FLIP's
  // play effect translates every moved cell back to where it was.
  const editor = useLayoutEditor({
    scope,
    layout,
    eligibility,
    onBeforeChange: captureLayout,
    showToast,
    announce,
    reducedMotion,
  });
  const flip = useFlipLayout(rootRef, { disabled: reducedMotion });
  flipCaptureRef.current = flip.capture;
  const editing = editor.active;
  const { enter: enterEditor, openGallery, dragStart, dragEnd, dragCancel } = editor;
  /** The lazily loaded drag layer (dnd-kit lives only in its chunk). */
  const [DragLayer, setDragLayer] = useState<DashboardDragLayerComponent | null>(null);
  const [editorLoading, setEditorLoading] = useState(false);
  const enteringRef = useRef(false);

  // What renders: the edit session's resolved draft while editing, else the saved layout.
  const view = editor.shown ?? resolved;

  // Fetched: the saved layout's sources, plus — while editing — the sources of the committed
  // draft once its membership has settled. Nothing is fetched until the layout settles (at
  // most LAYOUT_LOAD_TIMEOUT_MS), so a widget the saved layout hides is never fetched.
  const editSourceTag = editing
    ? JSON.stringify([scope, editor.sessionId, toNeededKey(editor.draftNeededSources)])
    : '';
  const settledEditSourceTag = useDebouncedValue(editSourceTag, EDIT_SOURCE_SETTLE_MS);
  const draftSourceKey = editing ? settledDraftSources(settledEditSourceTag, scope, editor.sessionId) : '';
  const neededKey =
    layout.status === 'loading' ? '' : toNeededKey([...resolved.neededSources, ...parseNeededKey(draftSourceKey)]);
  const { sources, refresh: refreshSources } = useDashboardSources(neededKey, { scope });

  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedAssignment, setSelectedAssignment] = useState<ActiveAssignment | null>(null);
  /**
   * The body is the grid (not the skeleton or the shop-data ErrorState). Edit mode arranges
   * the cells, so it opens only over them: `loading` only goes true → false and `data` is
   * never cleared, so the grid never goes away under an open editor.
   */
  const bodyReady = !loading && !(Boolean(error) && !data);
  const [layoutReloading, setLayoutReloading] = useState(false);
  /** A control to focus after the render that settles an action (a fresh object per request). */
  const [focusRequest, setFocusRequest] = useState<{ target: 'customize' | 'layoutReload' } | null>(null);
  const customizeButtonRef = useRef<HTMLButtonElement>(null);
  const layoutReloadButtonRef = useRef<HTMLButtonElement>(null);

  const requestRef = useRef(0);
  const dataRef = useRef(data);
  dataRef.current = data;
  const refreshSourcesRef = useRef(refreshSources);
  refreshSourcesRef.current = refreshSources;
  /** A poll or push skipped (hidden tab, or a drag in progress) that is owed one catch-up refresh. */
  const missedRefreshRef = useRef(false);
  /** A pointer / touch drag is in progress: background refreshes wait until it ends. */
  const gestureActiveRef = useRef(false);
  const mountedRef = useRef(true);
  /** The latest render's scope and layout status, for checks after an await. */
  const latestRef = useRef({ scope, status: layout.status });
  latestRef.current = { scope, status: layout.status };

  // Conditional request state
  const [isRefreshing, setIsRefreshing] = useState(false);
  const isRefreshingRef = useRef(isRefreshing);
  isRefreshingRef.current = isRefreshing;
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [dataChanged, setDataChanged] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const realtimeUrl = useMemo(() => {
    const token = getAccessToken();
    return buildWsUrl('/ws/updates', token ? { token } : undefined);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Clear "data changed" indicator after 2 seconds
  useEffect(() => {
    if (dataChanged) {
      const timeout = setTimeout(() => setDataChanged(false), 2000);
      return () => clearTimeout(timeout);
    }
  }, [dataChanged]);

  useEffect(() => {
    const interval = setInterval(() => setNowMs(Date.now()), 30000);
    return () => clearInterval(interval);
  }, []);

  /**
   * The shop-floor heartbeat (`GET /shop-floor/dashboard`, always fetched, guarded by its
   * own `requestRef`) plus a refresh of the widget sources (each guarded by its own key
   * sequence inside useDashboardSources). `forced` = a user action.
   */
  const loadDashboard = useCallback(async (isInitial = false, forced = false) => {
    const request = ++requestRef.current;
    setIsRefreshing(true);
    const current = () => request === requestRef.current;
    const main = async () => {
      try {
        const result = await api.getDashboardWithCache();
        if (!current()) return;
        if (result.changed || isInitial || !dataRef.current) setData(result.data);
        setDataChanged(!isInitial && result.changed);
        if (result.stale) setError('Connection interrupted. Showing last verified shop data.');
        else {
          setLastUpdated(new Date());
          setError('');
        }
      } catch {
        if (current())
          setError('Could not refresh shop data. Last verified information remains visible when available.');
      } finally {
        if (current()) setLoading(false); // Slow secondary widgets never gate the shop overview.
      }
    };
    await Promise.allSettled([main(), refreshSourcesRef.current(forced)]);
    if (current()) setIsRefreshing(false);
  }, []);

  /**
   * Poll / WebSocket refreshes skip a hidden tab — and a widget drag in progress (a refresh
   * that changes heights would move the grid under the pointer) — and run once afterwards.
   */
  const backgroundRefresh = useCallback(() => {
    if (isTabHidden() || gestureActiveRef.current) {
      missedRefreshRef.current = true;
      return;
    }
    void loadDashboard(false);
  }, [loadDashboard]);

  /** The one catch-up owed after a drag: through backgroundRefresh, so a hidden tab still defers it. */
  const catchUpAfterGesture = useCallback(() => {
    if (!missedRefreshRef.current) return;
    missedRefreshRef.current = false;
    backgroundRefresh();
  }, [backgroundRefresh]);

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      backgroundRefresh();
    }, 750);
  }, [backgroundRefresh]);

  useWebSocket({
    url: realtimeUrl,
    enabled: true,
    onMessage: message => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (
        ['dashboard_update', 'work_order_update', 'shop_floor_update', 'quality_alert', 'notification'].includes(
          message.type
        )
      ) {
        scheduleRealtimeRefresh();
      }
    },
  });

  useEffect(() => {
    loadDashboard(true);
    const interval = setInterval(backgroundRefresh, 30000);
    return () => {
      clearInterval(interval);
      ++requestRef.current;
    };
  }, [loadDashboard, backgroundRefresh]);

  useEffect(() => {
    const onVisibilityChange = () => {
      if (!isTabHidden() && !gestureActiveRef.current && missedRefreshRef.current) {
        missedRefreshRef.current = false;
        void loadDashboard(false);
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
  }, [loadDashboard]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

  // Leaving edit mode (Done, Cancel, a session change) mid-drag unmounts the drag layer
  // without an end or cancel callback: the gesture is over, and the catch-up is owed.
  useEffect(() => {
    if (editing || !gestureActiveRef.current) return;
    gestureActiveRef.current = false;
    catchUpAfterGesture();
  }, [editing, catchUpAfterGesture]);

  // While editing, focus-scrolling keeps controls clear of the sticky header (3.5rem) and
  // the fixed edit bar (WCAG 2.4.11); the previous values come back on exit. The bottom
  // padding is MEASURED from the bar's top edge, which covers BottomNav below lg, the safe
  // area, wrapped button rows and the bar's alert / notes (7rem is only the fallback before
  // the first measurement). The page root's bottom padding follows it, so the last row can
  // always scroll clear of the bar — less whatever its containers already pad below it
  // (Layout's <main> reserves BottomNav's height below lg; counting that twice left ~80 px
  // of dead space under the last row on a phone). Scroll anchoring is off: the live
  // reorders move cells the browser could pick as its anchor, and its compensating scroll
  // would jump the page.
  useEffect(() => {
    if (!editing) return;
    const style = document.documentElement.style;
    const pageRoot = rootRef.current;
    const previous = {
      top: style.scrollPaddingTop,
      bottom: style.scrollPaddingBottom,
      anchor: style.overflowAnchor,
    };
    style.scrollPaddingTop = '4rem';
    style.scrollPaddingBottom = '7rem';
    style.overflowAnchor = 'none';
    const reservedBelowRoot = () => {
      let reserved = 0;
      for (let node = pageRoot?.parentElement; node && node !== document.body; node = node.parentElement) {
        reserved += parseFloat(getComputedStyle(node).paddingBottom) || 0;
      }
      return reserved;
    };
    const sync = () => {
      const bar = document.getElementById(EDIT_IDS.bar);
      const box = bar?.getBoundingClientRect();
      if (!box || box.height <= 0) return; // Not laid out (yet): keep the fallback.
      const covered = Math.ceil(Math.max(0, window.innerHeight - box.top)) + EDIT_BAR_CLEARANCE_PX;
      style.scrollPaddingBottom = `${covered}px`;
      if (pageRoot) pageRoot.style.paddingBottom = `${Math.max(0, Math.ceil(covered - reservedBelowRoot()))}px`;
    };
    sync();
    const bar = document.getElementById(EDIT_IDS.bar);
    const observer = typeof ResizeObserver === 'function' && bar ? new ResizeObserver(sync) : null;
    if (observer && bar) observer.observe(bar);
    window.addEventListener('resize', sync);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', sync);
      style.scrollPaddingTop = previous.top;
      style.scrollPaddingBottom = previous.bottom;
      style.overflowAnchor = previous.anchor;
      if (pageRoot) pageRoot.style.paddingBottom = '';
    };
  }, [editing]);

  useLayoutEffect(() => {
    if (!focusRequest) return;
    const target = focusRequest.target === 'layoutReload' ? layoutReloadButtonRef.current : null;
    (target ?? customizeButtonRef.current)?.focus();
  }, [focusRequest]);

  // Manual refresh handler
  const handleManualRefresh = useCallback(() => {
    if (!isRefreshingRef.current) {
      void loadDashboard(false, true);
    }
  }, [loadDashboard]);

  const { reload: reloadLayout } = layout;
  const handleReloadLayout = useCallback(async () => {
    setLayoutReloading(true);
    try {
      return await reloadLayout();
    } finally {
      setLayoutReloading(false);
    }
  }, [reloadLayout]);

  /**
   * The layout notice's "Reload layout": the notice (and the focused button) disappears on
   * success, so focus moves to the header's Customize button and the change is announced;
   * a repeat failure says so and keeps focus on the button.
   */
  const handleNoticeReload = useCallback(async () => {
    const response = await handleReloadLayout();
    if (response) {
      announce('Your saved dashboard layout is now showing.');
      setFocusRequest({ target: 'customize' });
      return;
    }
    showToast('error', "Your saved dashboard layout still couldn't be loaded. Try again in a moment.");
    setFocusRequest({ target: 'layoutReload' });
  }, [handleReloadLayout, showToast, announce]);

  /**
   * Customize (the header button, the empty-state action and the banners-only action): only
   * over a rendered grid (not the skeleton or the shop-data ErrorState — edit mode arranges the
   * cells). It re-reads the saved layout first, every time — a copy cached since page load may
   * be behind a save from another tab or device, and a session seeded from it would end in a
   * 409 that discards its edits; a failed re-read of a layout already on screen keeps it, a
   * failed one after a load error refuses. Then it refuses a read-only session with a toast,
   * loads the drag layer's chunk, and enters edit mode — unless the session changed meanwhile.
   */
  const handleCustomize = useCallback(async () => {
    const status = layout.status;
    if (status === 'loading' || status === 'unsupported' || editing || enteringRef.current || !bodyReady) return;
    // Read-only is a property of the session scope (a change re-reads the layout anyway).
    if (status === 'ready' && !layout.canEdit) {
      showToast('info', READ_ONLY_LAYOUT_NOTICE);
      return;
    }
    const clickScope = scope;
    enteringRef.current = true;
    setEditorLoading(true);
    try {
      const response = await handleReloadLayout();
      if (!mountedRef.current) return;
      if (!response && status === 'error') {
        showToast('error', LAYOUT_LOAD_FAILED);
        // The button was disabled while it worked: give it focus back.
        setFocusRequest({ target: 'customize' });
        return;
      }
      const seed: DashboardLayoutResponse | undefined = response ?? undefined;
      if (!(seed ? seed.can_edit === true : layout.canEdit)) {
        showToast('info', READ_ONLY_LAYOUT_NOTICE);
        setFocusRequest({ target: 'customize' });
        return;
      }
      let Layer: DashboardDragLayerComponent;
      try {
        Layer = await loadDashboardDragLayer();
      } catch {
        // A chunk 404 after a deploy: stay in view mode, no automatic reload. The loader's
        // memo is already cleared, so the next click imports again.
        if (!mountedRef.current) return;
        showToast('error', EDITOR_LOAD_FAILED);
        setFocusRequest({ target: 'customize' });
        return;
      }
      if (!mountedRef.current) return;
      const latest = latestRef.current;
      if (latest.scope !== clickScope || (!seed && latest.status !== 'ready')) return;
      setDragLayer(() => Layer);
      enterEditor(seed);
    } finally {
      enteringRef.current = false;
      if (mountedRef.current) setEditorLoading(false);
    }
  }, [layout.status, layout.canEdit, editing, bodyReady, scope, handleReloadLayout, showToast, enterEditor]);

  // The drag layer's callbacks. Only an active pointer / touch gesture pauses background
  // refreshes; its end (or cancel) runs the one catch-up.
  const handleDragStart = useCallback(
    (zone: EditableZone, id: string) => {
      gestureActiveRef.current = true;
      dragStart(zone, id);
    },
    [dragStart]
  );
  const handleDragEnd = useCallback(
    (zone: EditableZone, id: string) => {
      gestureActiveRef.current = false;
      dragEnd(zone, id);
      catchUpAfterGesture();
    },
    [dragEnd, catchUpAfterGesture]
  );
  const handleDragCancel = useCallback(
    (zone: EditableZone, id: string) => {
      gestureActiveRef.current = false;
      dragCancel(zone, id);
      catchUpAfterGesture();
    },
    [dragCancel, catchUpAfterGesture]
  );

  // Keyed on visible MEMBERSHIP, not the Set's identity: a live drag reorder (or Undo of one)
  // rebuilds `visibleIds` with the same members, and a new `isVisible` would re-render every
  // widget that reads the widget context, on the drag's hot path.
  const visibleKey = JSON.stringify(Array.from(view.visibleIds).sort());
  const isVisible = useMemo(() => {
    const ids = new Set<string>(JSON.parse(visibleKey) as string[]);
    return (widgetId: string) => ids.has(widgetId);
  }, [visibleKey]);
  // A widget's Retry is the page's manual refresh. A widget's own non-forced request (e.g.
  // Notifications after the unread count moved) refreshes the sources only, under their
  // floors and the hidden-tab rule — never an extra shop-floor heartbeat (it reconciles on
  // read). The one exception is the drag pause: a refresh that changes heights mid-drag
  // would move the grid under the pointer, so the request is folded into the single
  // catch-up the drag's end already owes, which is a full refresh (heartbeat included).
  const refreshFromWidget = useCallback(
    (forced: boolean) => {
      if (forced) handleManualRefresh();
      else if (gestureActiveRef.current) missedRefreshRef.current = true;
      else void refreshSourcesRef.current(false);
    },
    [handleManualRefresh]
  );
  const widgetEnv = useMemo<DashboardWidgetEnv>(
    () => ({ sources, canOpen, isVisible, nowMs, refresh: refreshFromWidget }),
    [sources, canOpen, isVisible, nowMs, refreshFromWidget]
  );
  const coreEnv = useMemo<CoreDashboardEnv>(
    () => ({ data, shopError: error, openOperation: setSelectedAssignment }),
    [data, error]
  );

  const sourceErrorKeys = view.neededSources.filter(key => {
    const state = sources[key];
    return Boolean(state?.error) && !state?.forbidden;
  });

  const readOnly = layout.status === 'ready' && !layout.canEdit;
  const pageHeader = (
    <PageHeader
      title="Dashboard"
      description="Shop activity, staffing, and job progress · refreshes every 30 seconds"
      metadata={
        <>
          {dataChanged && <span className="rounded-full bg-fd-green/15 px-2 py-0.5 text-emerald-300">Updated</span>}
          {isRefreshing ? (
            <span role="status">Refreshing dashboard…</span>
          ) : (
            lastUpdated && <span>Checked {formatCentralTime(lastUpdated, { timeZoneName: 'short' })}</span>
          )}
        </>
      }
      actions={
        <>
          <button
            onClick={handleManualRefresh}
            disabled={loading || isRefreshing}
            className="btn-ghost btn-sm"
            title="Refresh dashboard"
            aria-label="Refresh dashboard"
          >
            <ArrowPathIcon aria-hidden="true" className={`h-5 w-5 ${isRefreshing ? 'animate-spin' : ''}`} />
          </button>
          {layout.status !== 'unsupported' && (
            <>
              <button
                ref={customizeButtonRef}
                id={EDIT_IDS.customize}
                type="button"
                onClick={() => void handleCustomize()}
                onPointerEnter={() => prefetchDashboardDragLayer()}
                onFocus={() => prefetchDashboardDragLayer()}
                disabled={layout.status === 'loading' || !bodyReady || layoutReloading || editorLoading || editing}
                aria-busy={editorLoading || undefined}
                aria-describedby={readOnly ? READ_ONLY_DESCRIPTION_ID : undefined}
                className="btn-ghost btn-sm"
                title="Customize dashboard"
                aria-label="Customize dashboard"
              >
                {editorLoading ? (
                  <ArrowPathIcon aria-hidden="true" className="h-5 w-5 animate-spin" />
                ) : (
                  <Squares2X2Icon aria-hidden="true" className="h-5 w-5" />
                )}
                <span className="ml-1.5 hidden sm:inline">Customize</span>
              </button>
              {readOnly && (
                <span id={READ_ONLY_DESCRIPTION_ID} className="sr-only">
                  {READ_ONLY_CUSTOMIZE_DESCRIPTION}
                </span>
              )}
            </>
          )}
          {canOpen('/shop-floor') && (
            <Link to="/shop-floor" className="btn-primary">
              <WrenchScrewdriverIcon aria-hidden="true" className="h-5 w-5 mr-2" />
              Shop Floor
            </Link>
          )}
        </>
      }
    />
  );
  const renderWidget = (definition: DashboardWidgetDefinition) => {
    const Widget = rendererFor(definition);
    return Widget ? <Widget key={definition.id} /> : null;
  };
  const hasKpis = view.kpis.length > 0;
  const hasPanels = view.panels.length > 0;
  // View mode only: edit mode always shows both zones (a grid or a placeholder) instead.
  const emptyView = !editing && isEmptyLayout(view);
  const bannersOnlyView = !editing && !emptyView && !hasKpis && !hasPanels;

  // ONE root for every state, with the header as its first child, so the header (and
  // focus inside it) survives the loading → loaded transition.
  let body: React.ReactNode;
  if (loading || layout.status === 'loading') {
    body = (
      <>
        <p role="status" className="sr-only">
          Loading dashboard…
        </p>
        <SkeletonDashboard />
      </>
    );
  } else if (error && !data) {
    body = <ErrorState title="Error loading dashboard" message={error} onRetry={() => loadDashboard(true)} />;
  } else {
    // KPI tiles and panels render through WidgetCell in BOTH modes, in the same slots, so
    // entering or leaving edit mode toggles attributes and never remounts a widget.
    const kpiZone = hasKpis ? (
      <div
        key="kpi-grid"
        ref={editing ? bridge.zoneRef('kpi') : undefined}
        role={editing ? 'list' : undefined}
        aria-label={editing ? ZONE_LIST_LABELS.kpi : undefined}
        className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2"
        data-tour="dashboard-stats"
      >
        {view.kpis.map((definition, index) => {
          const Widget = rendererFor(definition);
          if (!Widget) return null;
          return (
            <WidgetCell
              key={definition.id}
              definition={definition}
              component={Widget}
              zone="kpi"
              edit={editing ? { editor, bridge, index, count: view.kpis.length } : null}
            />
          );
        })}
      </div>
    ) : editing ? (
      <ZonePlaceholder
        key="kpi-placeholder"
        zone="kpi"
        disabled={editor.controlsDisabled}
        onAdd={() => openGallery('kpi')}
      />
    ) : null;

    const panelZone = hasPanels ? (
      <div
        key="panel-grid"
        ref={editing ? bridge.zoneRef('panel') : undefined}
        role={editing ? 'list' : undefined}
        aria-label={editing ? ZONE_LIST_LABELS.panel : undefined}
        className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start"
        data-tour={hasKpis ? undefined : 'dashboard-stats'}
      >
        {view.panels.map(({ definition, size }, index) => {
          const Widget = rendererFor(definition);
          if (!Widget) return null;
          return (
            <WidgetCell
              key={definition.id}
              definition={definition}
              component={Widget}
              zone="panel"
              size={size}
              edit={editing ? { editor, bridge, index, count: view.panels.length } : null}
            />
          );
        })}
      </div>
    ) : editing ? (
      <ZonePlaceholder
        key="panel-placeholder"
        zone="panel"
        disabled={editor.controlsDisabled}
        onAdd={() => openGallery('panel')}
      />
    ) : null;

    body = (
      <>
        {error && (
          <div role="alert" className="rounded border border-amber-500/40 bg-amber-500/10 p-3 text-amber-200">
            {error}{' '}
            <button className="underline" onClick={handleManualRefresh}>
              Retry
            </button>
          </div>
        )}
        {sourceErrorKeys.length > 0 && (
          <div role="status" className="rounded border border-amber-500/30 p-3 text-sm text-amber-200">
            {sourceErrorKeys.map(key => (
              <p key={key}>
                {getSourceSpec(key)?.label ?? key}: {sources[key]?.error}.{' '}
                {sources[key]?.data == null ? 'No verified data is available.' : 'Showing last verified data.'}
              </p>
            ))}
            <button className="underline mt-1" onClick={handleManualRefresh}>
              Retry unavailable sections
            </button>
          </div>
        )}
        {layout.status === 'error' && (
          <div role="status" className="rounded border border-fd-line p-3 text-sm text-slate-300">
            Your saved dashboard layout couldn&apos;t be loaded; showing the default.{' '}
            <button
              ref={layoutReloadButtonRef}
              type="button"
              className="underline disabled:opacity-60"
              disabled={layoutReloading}
              onClick={() => void handleNoticeReload()}
            >
              Reload layout
            </button>
          </div>
        )}

        {emptyView && (
          <EmptyState
            icon={Squares2X2Icon}
            title="Your dashboard is empty"
            description="Add key metrics and panels back from Customize dashboard."
            action={{ label: 'Customize dashboard', onClick: () => void handleCustomize() }}
          />
        )}

        {/* The banner zone: one stable wrapper (hidden while empty), inert while editing.
            With no KPI strip and no panel grid it carries the tour anchor and a standing
            note: every banner may be quiet, which would otherwise leave a blank page that
            reads as broken. */}
        <div
          className="space-y-4 empty:hidden"
          data-tour={bannersOnlyView ? 'dashboard-stats' : undefined}
          inert={editing}
        >
          {view.banners.map(renderWidget)}
          {bannersOnlyView && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-sm border border-dashed border-fd-line p-3">
              <p className="text-sm text-slate-300">
                Only alerts and banners are on your dashboard. They appear here when something needs attention.
              </p>
              <Button variant="secondary" size="sm" onClick={() => void handleCustomize()}>
                Customize dashboard
              </Button>
            </div>
          )}
        </div>

        {/* KPI strip — one dense row of compact tiles (the cells are its direct children) */}
        {kpiZone}

        {/* COCKPIT GRID — each panel in a sized cell */}
        {panelZone}
      </>
    );
  }

  return (
    <DashboardWidgetProvider value={widgetEnv}>
      <CoreDashboardProvider value={coreEnv}>
        <div ref={rootRef} className={editing ? 'space-y-4 pb-28' : 'space-y-4'}>
          {pageHeader}
          <p role="status" aria-live="polite" className="sr-only" data-testid="dashboard-announcer">
            <span key={announcement.key}>{announcement.text}</span>
          </p>
          {/* Fixed to the bottom of the viewport, but next after the header in the tab order. */}
          <EditBar editor={editor} />
          {body}

          {selectedAssignment?.operation.id != null && (
            <DashboardOperationDetails
              key={selectedAssignment.time_entry_id}
              assignment={selectedAssignment}
              onClose={() => setSelectedAssignment(null)}
            />
          )}

          <WidgetGallery editor={editor} eligibility={eligibility} />

          {/* The drag layer: a SIBLING of the widget tree (mounting it never remounts a widget),
              loaded on demand so dnd-kit stays out of the landing bundle. */}
          {editing && DragLayer && (
            <DragLayer
              bridge={bridge}
              enabled={!editor.controlsDisabled}
              orders={editor.orders}
              layoutKey={editor.layoutKey}
              reducedMotion={reducedMotion}
              describe={editor.describe}
              onDragStart={handleDragStart}
              onReorder={editor.dragReorder}
              onDragEnd={handleDragEnd}
              onDragCancel={handleDragCancel}
            />
          )}
        </div>
      </CoreDashboardProvider>
    </DashboardWidgetProvider>
  );
}
