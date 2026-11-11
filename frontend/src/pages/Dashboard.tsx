import { PageHeader } from '../components/ui/PageHeader';
import React, { useEffect, useLayoutEffect, useState, useCallback, useRef, useMemo } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { SkeletonDashboard } from '../components/ui/Skeleton';
import { Button, EmptyState, ErrorState, useToast } from '../components/ui';
import { ErrorBoundary } from '../components/ErrorBoundary';
import DashboardOperationDetails from '../components/dashboard/DashboardOperationDetails';
import { CustomizeDashboardDialog } from '../components/dashboard/CustomizeDashboardDialog';
import { DashboardWidgetProvider, type DashboardWidgetEnv } from '../components/dashboard/DashboardWidgetContext';
import { isEmptyLayout, resolveLayout } from '../components/dashboard/dashboardLayout';
import { getSourceSpec } from '../components/dashboard/sources';
import { useDashboardLayout, useDashboardSessionScope } from '../components/dashboard/useDashboardLayout';
import { toNeededKey, useDashboardSources } from '../components/dashboard/useDashboardSources';
import { useWidgetEligibility } from '../components/dashboard/useWidgetEligibility';
import {
  CORE_WIDGET_RENDERERS,
  CoreDashboardProvider,
  type CoreDashboardEnv,
} from '../components/dashboard/widgets/core';
import { EXTRA_WIDGET_RENDERERS } from '../components/dashboard/widgets/extra';
import { PANEL_SIZE_CLASSES, type DashboardWidgetDefinition } from '../components/dashboard/widgetTypes';
import { ActiveAssignment, DashboardData } from '../types';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { formatCentralTime } from '../utils/centralTime';
import { ArrowPathIcon, Squares2X2Icon, WrenchScrewdriverIcon } from '@heroicons/react/24/outline';

const isTabHidden = () => document.visibilityState !== 'visible';

/** Core renderers first (a Map — widget ids are user data), then the opt-in catalog's. */
function rendererFor(definition: DashboardWidgetDefinition) {
  return CORE_WIDGET_RENDERERS.get(definition.id) ?? EXTRA_WIDGET_RENDERERS.get(definition.id);
}

export default function Dashboard() {
  const { showToast } = useToast();
  const scope = useDashboardSessionScope();
  const layout = useDashboardLayout(scope);
  const { eligibility, canOpen } = useWidgetEligibility();
  const resolved = useMemo(() => resolveLayout(layout.saved, eligibility), [layout.saved, eligibility]);
  // Nothing is fetched for widgets until the layout settles (at most LAYOUT_LOAD_TIMEOUT_MS),
  // so a widget the saved layout hides is never fetched.
  const neededKey = layout.status === 'loading' ? '' : toNeededKey(resolved.neededSources);
  const { sources, refresh: refreshSources } = useDashboardSources(neededKey, { scope });

  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedAssignment, setSelectedAssignment] = useState<ActiveAssignment | null>(null);
  const [customizeOpen, setCustomizeOpen] = useState(false);
  const [layoutReloading, setLayoutReloading] = useState(false);
  /** Written into a persistent polite region, so layout changes are announced. */
  const [layoutAnnouncement, setLayoutAnnouncement] = useState('');
  /** A control to focus after the render that settles an action (a fresh object per request). */
  const [focusRequest, setFocusRequest] = useState<{ target: 'customize' | 'layoutReload' } | null>(null);
  const customizeButtonRef = useRef<HTMLButtonElement>(null);
  const layoutReloadButtonRef = useRef<HTMLButtonElement>(null);

  const requestRef = useRef(0);
  const dataRef = useRef(data);
  dataRef.current = data;
  const refreshSourcesRef = useRef(refreshSources);
  refreshSourcesRef.current = refreshSources;
  const missedWhileHiddenRef = useRef(false);

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

  /** Poll / WebSocket refreshes skip a hidden tab and run once when it becomes visible again. */
  const backgroundRefresh = useCallback(() => {
    if (isTabHidden()) {
      missedWhileHiddenRef.current = true;
      return;
    }
    void loadDashboard(false);
  }, [loadDashboard]);

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
      if (!isTabHidden() && missedWhileHiddenRef.current) {
        missedWhileHiddenRef.current = false;
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

  // A different session (user, active company, read-only flag) never keeps the dialog open.
  useEffect(() => {
    setCustomizeOpen(false);
  }, [scope]);

  useLayoutEffect(() => {
    if (!focusRequest) return;
    const target = focusRequest.target === 'layoutReload' ? layoutReloadButtonRef.current : null;
    (target ?? customizeButtonRef.current)?.focus();
  }, [focusRequest]);

  // The dialog's Modal returns focus to whatever opened it — unless that control is gone:
  // the empty-state (or banners-only) Customize action unmounts as soon as a save or reset
  // puts widgets back. Then focus would drop to <body>; send it to the header's Customize
  // button instead. This runs after the Modal's own restore (its cleanup runs first).
  const dialogOpen = customizeOpen && layout.response !== null;
  const dialogWasOpenRef = useRef(false);
  useEffect(() => {
    const closed = dialogWasOpenRef.current && !dialogOpen;
    dialogWasOpenRef.current = dialogOpen;
    if (!closed) return;
    const active = document.activeElement;
    if (!active || active === document.body) customizeButtonRef.current?.focus();
  }, [dialogOpen]);

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
    setLayoutAnnouncement('');
    const response = await handleReloadLayout();
    if (response) {
      setLayoutAnnouncement('Your saved dashboard layout is now showing.');
      setFocusRequest({ target: 'customize' });
      return;
    }
    showToast('error', "Your saved dashboard layout still couldn't be loaded. Try again in a moment.");
    setFocusRequest({ target: 'layoutReload' });
  }, [handleReloadLayout, showToast]);

  const handleCustomize = useCallback(async () => {
    if (layout.status === 'loading' || layout.status === 'unsupported') return;
    if (layout.status === 'error') {
      const response = await handleReloadLayout();
      if (!response) {
        showToast('error', "Your saved dashboard layout couldn't be loaded. Try again in a moment.");
        return;
      }
    }
    setCustomizeOpen(true);
  }, [layout.status, handleReloadLayout, showToast]);

  const isVisible = useCallback((widgetId: string) => resolved.visibleIds.has(widgetId), [resolved.visibleIds]);
  // A widget's Retry is the page's manual refresh. A widget's own non-forced request (e.g.
  // Notifications after the unread count moved) refreshes the sources only, under their
  // floors and the hidden-tab rule — never an extra shop-floor heartbeat (it reconciles on read).
  const refreshFromWidget = useCallback(
    (forced: boolean) => {
      if (forced) handleManualRefresh();
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

  const sourceErrorKeys = resolved.neededSources.filter(key => {
    const state = sources[key];
    return Boolean(state?.error) && !state?.forbidden;
  });

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
            <button
              ref={customizeButtonRef}
              type="button"
              onClick={() => void handleCustomize()}
              disabled={layout.status === 'loading' || layoutReloading}
              className="btn-ghost btn-sm"
              title="Customize dashboard"
              aria-label="Customize dashboard"
            >
              <Squares2X2Icon aria-hidden="true" className="h-5 w-5" />
              <span className="ml-1.5 hidden sm:inline">Customize</span>
            </button>
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
  const hasKpis = resolved.kpis.length > 0;
  const hasPanels = resolved.panels.length > 0;
  const banners = resolved.banners.map(renderWidget);

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

        {isEmptyLayout(resolved) ? (
          <EmptyState
            icon={Squares2X2Icon}
            title="Your dashboard is empty"
            description="Add key metrics and panels back from Customize dashboard."
            action={{ label: 'Customize dashboard', onClick: () => void handleCustomize() }}
          />
        ) : (
          <>
            {/* With no KPI strip and no panel grid, the tour anchor moves to the banner zone,
                with a standing note: every banner may be quiet, which would otherwise leave
                a blank page that reads as broken. */}
            {!hasKpis && !hasPanels ? (
              <div className="space-y-4" data-tour="dashboard-stats">
                {banners}
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-sm border border-dashed border-fd-line p-3">
                  <p className="text-sm text-slate-300">
                    Only alerts and banners are on your dashboard. They appear here when something needs attention.
                  </p>
                  <Button variant="secondary" size="sm" onClick={() => void handleCustomize()}>
                    Customize dashboard
                  </Button>
                </div>
              </div>
            ) : (
              banners
            )}

            {/* KPI strip — one dense row of compact tiles (the tiles are its direct children) */}
            {hasKpis && (
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2" data-tour="dashboard-stats">
                {resolved.kpis.map(renderWidget)}
              </div>
            )}

            {/* COCKPIT GRID — each panel in a sized cell */}
            {hasPanels && (
              <div
                className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start"
                data-tour={hasKpis ? undefined : 'dashboard-stats'}
              >
                {resolved.panels.map(({ definition, size }) => {
                  const Widget = rendererFor(definition);
                  if (!Widget) return null;
                  return (
                    <div key={definition.id} className={`min-w-0 ${PANEL_SIZE_CLASSES[size]}`}>
                      <ErrorBoundary level="widget" name={definition.title}>
                        <Widget />
                      </ErrorBoundary>
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}
      </>
    );
  }

  return (
    <DashboardWidgetProvider value={widgetEnv}>
      <CoreDashboardProvider value={coreEnv}>
        <div className="space-y-4">
          {pageHeader}
          <p role="status" aria-live="polite" className="sr-only">
            {layoutAnnouncement}
          </p>
          {body}

          {selectedAssignment?.operation.id != null && (
            <DashboardOperationDetails
              key={selectedAssignment.time_entry_id}
              assignment={selectedAssignment}
              onClose={() => setSelectedAssignment(null)}
            />
          )}

          {customizeOpen && layout.response && (
            <CustomizeDashboardDialog
              saved={layout.saved}
              canEdit={layout.canEdit}
              eligibility={eligibility}
              onSave={layout.save}
              onReset={layout.reset}
              onReload={async () => {
                const response = await handleReloadLayout();
                if (!response) throw new Error("Your saved dashboard layout couldn't be reloaded. Try again.");
                return response.layout;
              }}
              onClose={() => setCustomizeOpen(false)}
            />
          )}
        </div>
      </CoreDashboardProvider>
    </DashboardWidgetProvider>
  );
}
