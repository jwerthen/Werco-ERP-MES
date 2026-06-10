/**
 * Admin > AI Usage & Cost tab.
 *
 * Read-only cost / latency observability over the AI usage ledger
 * (GET /ai-usage/summary) for the ACTIVE company:
 *   - a 7 / 30 / 90-day window selector;
 *   - a totals row (calls, tokens, estimated USD spend, error rate, latency);
 *   - per-task and per-model breakdown tables.
 *
 * Rendered inside AdminSettings, which is route-gated by AdminRoute
 * (admin role / superuser only). The backend endpoint additionally allows
 * MANAGER, but no manager-facing surface consumes it yet.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { ArrowPathIcon, CpuChipIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import type { AIUsageAggregate, AIUsageSummaryResponse } from '../../types/aiUsage';

const WINDOW_OPTIONS = [7, 30, 90] as const;

// ---------------------------------------------------------------------------
// Formatting helpers (exported for unit tests).
// ---------------------------------------------------------------------------

const usdFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

/** USD with 2-4 decimals; em dash when the bucket has no priced calls. */
export const formatUsd = (value: number | null | undefined): string =>
  value == null ? '—' : usdFormatter.format(value);

const compactNumber = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

/** Human-compact token counts: 987, 45.6K, 1.2M. */
export const formatTokens = (value: number): string =>
  value < 1000 ? String(value) : compactNumber.format(value);

/** 0.0-1.0 ratio rendered as a percentage with one decimal. */
export const formatErrorRate = (rate: number): string => `${(rate * 100).toFixed(1)}%`;

export const formatLatency = (ms: number | null | undefined): string => {
  if (ms == null) return '—';
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
};

const totalTokens = (agg: AIUsageAggregate): number =>
  agg.input_tokens + agg.output_tokens + agg.cache_creation_tokens + agg.cache_read_tokens;

const errorRateClass = (rate: number): string => {
  if (rate >= 0.05) return 'text-red-600 font-semibold';
  if (rate > 0) return 'text-amber-600';
  return 'text-surface-600';
};

// ---------------------------------------------------------------------------
// Tab component.
// ---------------------------------------------------------------------------

export default function AIUsageTab() {
  const [days, setDays] = useState<number>(30);
  const [summary, setSummary] = useState<AIUsageSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (windowDays: number) => {
    setLoading(true);
    setError(null);
    try {
      setSummary(await api.getAIUsageSummary(windowDays));
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to load AI usage');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(days);
  }, [days, load]);

  return (
    <div className="space-y-6">
      {/* Header + window selector */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h3 className="text-sm font-semibold uppercase tracking-wide text-surface-700 flex items-center gap-2">
            <CpuChipIcon className="h-5 w-5 text-werco-600" />
            AI Usage &amp; Cost
          </h3>
          <p className="text-xs text-surface-500 mt-0.5">
            Calls, tokens, and estimated spend across AI tasks for this company. Costs are estimates from
            published model pricing.
          </p>
        </div>
        <div className="flex border border-surface-200" role="group" aria-label="Aggregation window">
          {WINDOW_OPTIONS.map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={days === option}
              onClick={() => setDays(option)}
              className={`px-3 py-1.5 text-xs font-medium border-r border-surface-200 last:border-r-0 transition-colors ${
                days === option
                  ? 'bg-werco-600 text-white'
                  : 'text-surface-600 hover:text-surface-800 hover:bg-surface-100'
              }`}
            >
              {option}d
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12" data-testid="ai-usage-loading">
          <div className="spinner h-8 w-8" />
        </div>
      ) : error ? (
        <div className="flex items-start gap-3 border border-red-500/40 bg-red-500/5 px-4 py-3">
          <ExclamationTriangleIcon className="h-5 w-5 text-red-500 flex-shrink-0 mt-0.5" />
          <div className="text-sm">
            <p className="font-semibold text-red-600">Failed to load AI usage</p>
            <p className="text-surface-500 mt-0.5">{error}</p>
            <button type="button" onClick={() => load(days)} className="btn-secondary btn-sm mt-2">
              <ArrowPathIcon className="h-4 w-4 mr-1" />
              Retry
            </button>
          </div>
        </div>
      ) : !summary || summary.totals.calls === 0 ? (
        <div className="text-center py-12 border border-surface-200">
          <CpuChipIcon className="h-10 w-10 text-surface-300 mx-auto mb-2" />
          <p className="text-surface-500 text-sm">No AI usage recorded yet</p>
          <p className="text-surface-400 text-xs mt-1">
            Usage appears here once AI-powered features make calls within the selected window.
          </p>
        </div>
      ) : (
        <>
          <TotalsRow totals={summary.totals} />

          <BreakdownTable
            title="By Task"
            labelHeader="Task"
            rows={summary.by_task.map((row) => ({ label: row.task, agg: row }))}
          />
          <BreakdownTable
            title="By Model"
            labelHeader="Model"
            rows={summary.by_model.map((row) => ({ label: row.model, agg: row }))}
          />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Totals row — instrument-panel stat tiles.
// ---------------------------------------------------------------------------

function TotalsRow({ totals }: { totals: AIUsageAggregate }) {
  const tiles: { label: string; value: string; sub?: string; valueClass?: string }[] = [
    { label: 'Calls', value: totals.calls.toLocaleString('en-US') },
    {
      label: 'Tokens',
      value: formatTokens(totalTokens(totals)),
      sub: `${formatTokens(totals.input_tokens)} in · ${formatTokens(totals.output_tokens)} out · ${formatTokens(
        totals.cache_creation_tokens + totals.cache_read_tokens,
      )} cache`,
    },
    { label: 'Est. Cost', value: formatUsd(totals.estimated_cost_usd) },
    {
      label: 'Error Rate',
      value: formatErrorRate(totals.error_rate),
      valueClass: errorRateClass(totals.error_rate),
    },
    { label: 'Avg Latency', value: formatLatency(totals.avg_latency_ms) },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-5 border border-surface-200 divide-x divide-y md:divide-y-0 divide-surface-200">
      {tiles.map((tile) => (
        <div key={tile.label} className="px-4 py-3">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-surface-500">{tile.label}</p>
          <p className={`text-xl font-mono mt-1 ${tile.valueClass || 'text-surface-800'}`}>{tile.value}</p>
          {tile.sub && <p className="text-[11px] text-surface-400 font-mono mt-0.5">{tile.sub}</p>}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-task / per-model breakdown table.
// ---------------------------------------------------------------------------

function BreakdownTable({
  title,
  labelHeader,
  rows,
}: {
  title: string;
  labelHeader: string;
  rows: { label: string; agg: AIUsageAggregate }[];
}) {
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-surface-600 mb-2">{title}</h4>
      {rows.length === 0 ? (
        <p className="text-sm text-surface-400 border border-surface-200 px-4 py-3">
          No {labelHeader.toLowerCase()}-level usage in this window
        </p>
      ) : (
        <div className="table-container border border-surface-200">
          <table className="table">
            <thead>
              <tr>
                <th>{labelHeader}</th>
                <th className="text-right">Calls</th>
                <th className="text-right">Input</th>
                <th className="text-right">Output</th>
                <th className="text-right">Cache W/R</th>
                <th className="text-right">Est. Cost</th>
                <th className="text-right">Avg Latency</th>
                <th className="text-right">Error Rate</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ label, agg }) => (
                <tr key={label}>
                  <td className="font-mono text-sm">{label}</td>
                  <td className="text-right font-mono text-sm">{agg.calls.toLocaleString('en-US')}</td>
                  <td className="text-right font-mono text-sm">{formatTokens(agg.input_tokens)}</td>
                  <td className="text-right font-mono text-sm">{formatTokens(agg.output_tokens)}</td>
                  <td className="text-right font-mono text-sm">
                    {formatTokens(agg.cache_creation_tokens)} / {formatTokens(agg.cache_read_tokens)}
                  </td>
                  <td className="text-right font-mono text-sm">{formatUsd(agg.estimated_cost_usd)}</td>
                  <td className="text-right font-mono text-sm">{formatLatency(agg.avg_latency_ms)}</td>
                  <td className={`text-right font-mono text-sm ${errorRateClass(agg.error_rate)}`}>
                    {formatErrorRate(agg.error_rate)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
