/**
 * Quality Pulse — reads the CORE `quality` source (`GET /quality/summary`), which the
 * classic dashboard already fetches for the Open NCRs tile, so the panel costs no request.
 *
 * `ncr_dispositions` is labelled for what it actually counts: NCRs OPENED since the 1st of
 * the server's calendar month (UTC), grouped by their CURRENT disposition — a new,
 * undispositioned NCR sits in `pending`, which is always shown. Voided NCRs are excluded.
 */
import React, { useId } from 'react';
import { ShieldCheckIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import { statusColor } from '../../../../utils/statusColors';
import type { QualitySummaryData } from '../../sources/coreSources';
import { useDashboardSource } from '../../DashboardWidgetContext';
import { PanelAction, PanelStat, SourceBody, humanize, panelSubtitle } from './shared';

/** Known dispositions, `pending` first; unknown keys from a newer backend are appended. */
const DISPOSITION_ORDER = ['pending', 'use_as_is', 'rework', 'repair', 'scrap', 'return_to_vendor'] as const;

const DISPOSITION_LABELS: Record<string, string> = {
  pending: 'Pending',
  use_as_is: 'Use as is',
  rework: 'Rework',
  repair: 'Repair',
  scrap: 'Scrap',
  return_to_vendor: 'Return to vendor',
};

export const NCR_MONTH_HEADING = 'New NCRs this month by current disposition';
export const NCR_MONTH_TOOLTIP =
  'NCRs opened since the 1st of this month — the server’s calendar month (UTC), not the Central day used elsewhere ' +
  'on this dashboard — grouped by each NCR’s current disposition. Undispositioned NCRs count as Pending; voided NCRs ' +
  'are excluded.';

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function dispositionRows(dispositions: Record<string, number> | null | undefined): { key: string; count: number }[] {
  const source = dispositions ?? {};
  const known = DISPOSITION_ORDER.map(key => ({ key, count: count(source[key]) }));
  const extra = Object.keys(source)
    .filter(key => !(DISPOSITION_ORDER as readonly string[]).includes(key))
    .sort()
    .map(key => ({ key, count: count(source[key]) }));
  return [...known, ...extra];
}

export function QualityPulseWidget() {
  const state = useDashboardSource<QualitySummaryData>('quality');
  const headingId = useId();

  return (
    <CockpitPanel
      title="Quality Pulse"
      subtitle={panelSubtitle([state], 'Open findings and this month’s new NCRs')}
      headerExtra={
        state.forbidden ? undefined : <PanelAction href="/quality" label="Quality" ariaLabel="Open quality" />
      }
    >
      <SourceBody state={state} loadingText="Loading quality summary…" errorTitle="Quality summary is unavailable">
        {summary => {
          const openNcrs = count(summary.open_ncrs);
          const openCars = count(summary.open_cars);
          const pendingFais = count(summary.pending_fais);
          const rows = dispositionRows(summary.ncr_dispositions);
          const monthTotal = rows.reduce((sum, row) => sum + row.count, 0);
          if (openNcrs === 0 && openCars === 0 && pendingFais === 0 && monthTotal === 0) {
            return (
              <EmptyState
                icon={ShieldCheckIcon}
                title="No open quality findings"
                description="No open NCRs or CARs, no pending FAIs, and no NCRs opened this month."
              />
            );
          }
          return (
            <div className="space-y-3">
              <div className="grid grid-cols-3 gap-1.5">
                <PanelStat
                  label="Open NCRs"
                  value={openNcrs}
                  valueClassName={openNcrs > 0 ? 'text-amber-400' : 'text-white'}
                  href="/quality?tab=ncr&filter=open"
                />
                <PanelStat
                  label="Open CARs"
                  value={openCars}
                  valueClassName={openCars > 0 ? 'text-amber-400' : 'text-white'}
                  href="/quality?tab=car"
                />
                <PanelStat label="Pending FAIs" value={pendingFais} href="/quality?tab=fai" />
              </div>
              <section aria-labelledby={headingId}>
                <div className="mb-1 flex items-baseline justify-between gap-2">
                  <h3
                    id={headingId}
                    title={NCR_MONTH_TOOLTIP}
                    className="text-[10px] font-semibold uppercase tracking-wide text-slate-500"
                  >
                    {NCR_MONTH_HEADING}
                  </h3>
                  <span className="flex-shrink-0 text-[10px] tabular-nums text-slate-400">{monthTotal} total</span>
                </div>
                <p className="sr-only">{NCR_MONTH_TOOLTIP}</p>
                <ul className="grid grid-cols-2 gap-1 sm:grid-cols-3">
                  {rows.map(row => (
                    <li
                      key={row.key}
                      className="flex items-center justify-between gap-2 rounded-sm border border-fd-line px-2 py-1 text-[11px]"
                    >
                      <span
                        className={`truncate rounded-sm px-1 py-0.5 text-[10px] font-medium ${statusColor(row.key)}`}
                      >
                        {DISPOSITION_LABELS[row.key] ?? humanize(row.key)}
                      </span>
                      <span className="font-semibold tabular-nums text-white">{row.count}</span>
                    </li>
                  ))}
                </ul>
              </section>
            </div>
          );
        }}
      </SourceBody>
    </CockpitPanel>
  );
}
