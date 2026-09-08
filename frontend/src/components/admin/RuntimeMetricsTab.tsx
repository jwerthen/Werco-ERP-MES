import React, { useCallback, useEffect, useRef, useState } from 'react';
import api from '../../services/api';
import routes from '../../data/runtimeMetricRoutes.json';
import type { RuntimeMetricSummary, RuntimeMetricSummaryRow } from '../../types/runtimeMetrics';
import { ConfirmDialog, EmptyState, ErrorState } from '../ui';

const thresholds = { LCP: [2500, 4000], INP: [200, 500], CLS: [0.1, 0.25] };
const labels = { LCP: 'Loading (LCP)', INP: 'Input response (INP)', CLS: 'Layout shift (CLS)' };

function rating(row: RuntimeMetricSummaryRow) {
  return row.p75 <= thresholds[row.name][0]
    ? 'Good'
    : row.p75 <= thresholds[row.name][1]
      ? 'Needs improvement'
      : 'Poor';
}

export default function RuntimeMetricsTab() {
  const [data, setData] = useState<RuntimeMetricSummary | null>(null);
  const [days, setDays] = useState('7');
  const [device, setDevice] = useState('');
  const [route, setRoute] = useState('');
  const [page, setPage] = useState(1);
  const [contextEpoch, setContextEpoch] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [clearOpen, setClearOpen] = useState(false);
  const requestVersion = useRef(0);
  const contextVersion = useRef(0);

  useEffect(() => {
    const reset = () => {
      contextVersion.current += 1;
      requestVersion.current += 1;
      setData(null);
      setLoading(true);
      setBusy(false);
      setClearOpen(false);
      setPage(1);
      setContextEpoch(value => value + 1);
    };
    window.addEventListener('werco:auth-token-changed', reset);
    return () => {
      contextVersion.current += 1;
      window.removeEventListener('werco:auth-token-changed', reset);
    };
  }, []);

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setLoading(true);
    setError('');
    try {
      const result = await api.getRuntimeMetricSummary({
        days: Number(days),
        page,
        device: device || undefined,
        route: route || undefined,
      });
      if (version === requestVersion.current) setData(result);
    } catch {
      if (version === requestVersion.current) setError('Performance measurements could not be loaded.');
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  }, [days, device, route, page, contextEpoch]);

  useEffect(() => {
    void load();
    return () => {
      requestVersion.current += 1;
    };
  }, [load]);

  const toggle = async () => {
    if (!data) return;
    const context = contextVersion.current;
    setBusy(true);
    try {
      const result = await api.setRuntimeMetricsEnabled(!data.enabled);
      if (context === contextVersion.current) {
        setData(current => (current ? { ...current, enabled: result.enabled } : current));
      }
    } catch {
      if (context === contextVersion.current) setError('The collection setting could not be saved.');
    } finally {
      if (context === contextVersion.current) setBusy(false);
    }
  };

  const clear = async () => {
    const context = contextVersion.current;
    setBusy(true);
    try {
      await api.clearRuntimeMetrics();
      if (context !== contextVersion.current) return;
      setClearOpen(false);
      if (page === 1) await load();
      else setPage(1);
    } catch {
      if (context === contextVersion.current) setError('Measurements could not be cleared.');
    } finally {
      if (context === contextVersion.current) setBusy(false);
    }
  };

  return (
    <section aria-labelledby="runtime-metrics-title" className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-3xl">
          <h2 id="runtime-metrics-title" className="text-lg font-semibold text-fd-ink">
            App performance
          </h2>
          <p className="mt-1 text-sm text-fd-mute">
            Measurements from real signed-in work sessions, grouped by screen, device size and release. Use these
            results to find the slowest parts of daily work.
          </p>
        </div>
        <button className="btn btn-secondary" disabled={loading || busy} onClick={() => void load()}>
          Refresh
        </button>
      </div>
      <div className="rounded-lg border border-fd-line bg-fd-sunken p-4 text-sm">
        <p>
          No names, record IDs, form contents, URLs or screen recordings are collected. Data stays in this ERP and
          expires after 30 days. Browser privacy signals are respected.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <span className="font-medium">Collection: {data ? (data.enabled ? 'On' : 'Paused') : 'Loading…'}</span>
          <button
            className="btn btn-secondary btn-sm"
            disabled={!data || loading || busy}
            onClick={() => void toggle()}
          >
            {data?.enabled ? 'Pause collection' : 'Enable collection'}
          </button>
          <button
            className="btn btn-secondary btn-sm"
            disabled={!data || loading || busy}
            onClick={() => setClearOpen(true)}
          >
            Clear measurements
          </button>
        </div>
      </div>
      <div className="flex flex-wrap gap-4">
        <label className="text-sm font-medium">
          Period
          <select
            className="input mt-1 block"
            value={days}
            disabled={busy}
            onChange={event => {
              setDays(event.target.value);
              setPage(1);
            }}
          >
            <option value="1">Last 24 hours</option>
            <option value="7">Last 7 days</option>
            <option value="30">Last 30 days</option>
          </select>
        </label>
        <label className="text-sm font-medium">
          Device size
          <select
            className="input mt-1 block"
            value={device}
            disabled={busy}
            onChange={event => {
              setDevice(event.target.value);
              setPage(1);
            }}
          >
            <option value="">All sizes</option>
            <option value="mobile">Mobile</option>
            <option value="tablet">Tablet</option>
            <option value="desktop">Desktop</option>
          </select>
        </label>
        <label className="min-w-0 text-sm font-medium">
          Screen
          <select
            className="input mt-1 block max-w-full"
            value={route}
            disabled={busy}
            onChange={event => {
              setRoute(event.target.value);
              setPage(1);
            }}
          >
            <option value="">All screens</option>
            {routes.map(value => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
      </div>
      {error && <ErrorState title="Unable to update performance" message={error} onRetry={() => void load()} />}
      {loading ? (
        <p role="status" className="py-8 text-sm text-fd-mute">
          Loading measurements…
        </p>
      ) : data && data.rows.length === 0 ? (
        <EmptyState
          title="No measurements in this view"
          description="Measurements appear as signed-in users load pages, interact and leave screens. Try a longer period or another device size."
        />
      ) : (
        data && (
          <>
            <p className="text-sm text-fd-mute">
              The 75th percentile means 75% of samples were at least this fast or stable. Fewer than 20 samples is a
              small sample. Browser support varies; missing measurements are not zero.
            </p>
            {/* Horizontal overflow must be focusable for keyboard scrolling. */}
            {/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- Scroll region needs keyboard access. */}
            <div
              className="overflow-x-auto rounded-lg border border-fd-line"
              role="region"
              aria-label="Performance measurements; scroll horizontally for more columns"
              tabIndex={0}
            >
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Real user performance by screen and release</caption>
                <thead className="bg-fd-sunken">
                  <tr>
                    {[
                      'Screen',
                      'Device',
                      'Metric',
                      '75th percentile',
                      'Rating',
                      'Good samples',
                      'Samples',
                      'Navigation',
                      'Release',
                    ].map(label => (
                      <th key={label} scope="col" className="whitespace-nowrap px-3 py-3 font-semibold">
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-fd-line">
                  {data.rows.map(row => (
                    <tr key={[row.route, row.device, row.name, row.navigation, row.release].join(':')}>
                      <th scope="row" className="px-3 py-3 font-medium">
                        {row.route}
                      </th>
                      <td className="px-3 py-3 capitalize">{row.device}</td>
                      <td className="whitespace-nowrap px-3 py-3">{labels[row.name]}</td>
                      <td className="whitespace-nowrap px-3 py-3 tabular-nums">
                        {row.name === 'CLS' ? row.p75.toFixed(3) : `${Math.round(row.p75).toLocaleString()} ms`}
                      </td>
                      <td className="px-3 py-3">{rating(row)}</td>
                      <td className="px-3 py-3 tabular-nums">{row.good_percent}%</td>
                      <td className="px-3 py-3 tabular-nums">
                        {row.samples.toLocaleString()}
                        {row.samples < 20 && (
                          <span className="block whitespace-nowrap text-xs text-fd-mute">Small sample</span>
                        )}
                      </td>
                      <td className="px-3 py-3">{row.navigation === 'soft' ? 'Within app' : 'Page load'}</td>
                      <td className="px-3 py-3 font-mono" title={row.release}>
                        {row.release.slice(0, 8)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* eslint-enable jsx-a11y/no-noninteractive-tabindex */}
            <p className="text-xs text-fd-mute">
              Within-app navigation measurements require browser support. Other browsers attribute document-lifetime
              metrics to the screen first loaded. Device size is the viewport at page load.
            </p>
          </>
        )
      )}
      {data && (page > 1 || data.has_more) && (
        <nav className="flex items-center gap-3" aria-label="Performance pages">
          <button
            className="btn btn-secondary"
            disabled={loading || busy || page === 1}
            onClick={() => setPage(value => value - 1)}
          >
            Previous page
          </button>
          <span className="text-sm">Page {page}</span>
          <button
            className="btn btn-secondary"
            disabled={loading || busy || !data.has_more}
            onClick={() => setPage(value => value + 1)}
          >
            Next page
          </button>
        </nav>
      )}
      <ConfirmDialog
        open={clearOpen}
        onCancel={() => setClearOpen(false)}
        onConfirm={() => void clear()}
        title="Clear performance measurements?"
        message="This removes this company's anonymous performance history. Collection keeps its current setting."
        confirmLabel="Clear measurements"
        variant="danger"
        pending={busy}
      />
    </section>
  );
}
