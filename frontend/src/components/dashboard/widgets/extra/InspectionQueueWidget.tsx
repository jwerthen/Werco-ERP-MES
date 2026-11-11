/**
 * Inspection Queue — two sources read together: `inspectionStats`
 * (`GET /receiving/stats?days=30`) for the counts and `inspectionQueue`
 * (`GET /receiving/inspection-queue`, oldest first, every receipt still pending) for the five
 * oldest receipts. The queue has no per-receipt deep link, so rows are plain text and the
 * header action opens Receiving & Inspection.
 */
import React from 'react';
import { ClipboardDocumentCheckIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import type { InspectionQueueRow, ReceivingStatsResponse } from '../../../../types/dashboardWidgetData';
import { useDashboardSource } from '../../DashboardWidgetContext';
import { CountChip, PanelAction, PanelStat, SourceBody, combineSources, panelSubtitle } from './shared';

const OLDEST_LIMIT = 5;

function formatRate(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${value.toFixed(1)}%`;
}

export function InspectionQueueWidget() {
  const statsState = useDashboardSource<ReceivingStatsResponse>('inspectionStats');
  const queueState = useDashboardSource<InspectionQueueRow[]>('inspectionQueue');
  const state = combineSources(statsState, queueState);
  const pending = !state.forbidden && statsState.data ? statsState.data.pending_inspection : null;

  return (
    <CockpitPanel
      title="Inspection Queue"
      subtitle={panelSubtitle([statsState, queueState], 'Receipts awaiting incoming inspection · oldest first')}
      headerExtra={
        state.forbidden ? undefined : (
          <div className="flex items-center gap-2">
            {pending != null && <CountChip variant={pending > 0 ? 'amber' : 'slate'}>{pending} pending</CountChip>}
            <PanelAction href="/warehouse?tab=receiving" label="Receiving" ariaLabel="Open receiving and inspection" />
          </div>
        )
      }
    >
      <SourceBody state={state} loadingText="Loading inspection queue…" errorTitle="Inspection queue is unavailable">
        {([stats, queue]) => {
          if (stats.pending_inspection === 0 && queue.length === 0) {
            return (
              <EmptyState
                icon={ClipboardDocumentCheckIcon}
                title="Nothing waiting for inspection"
                description="Receipts that need incoming inspection appear here, oldest first."
              />
            );
          }
          return (
            <div className="space-y-2.5">
              <div className="grid grid-cols-3 gap-1.5">
                <PanelStat
                  label="Pending"
                  value={stats.pending_inspection}
                  valueClassName={stats.pending_inspection > 0 ? 'text-amber-400' : 'text-white'}
                />
                <PanelStat
                  label="Rejections"
                  value={stats.rejections_in_period}
                  valueClassName={stats.rejections_in_period > 0 ? 'text-red-400' : 'text-white'}
                  hint="Last 30 days"
                />
                <PanelStat label="Accepted" value={formatRate(stats.acceptance_rate)} hint="Qty, last 30 days" />
              </div>
              {queue.length > 0 && (
                <div>
                  <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                    Oldest waiting
                  </p>
                  <ul className="divide-y divide-fd-line">
                    {queue.slice(0, OLDEST_LIMIT).map(row => (
                      <li key={row.receipt_id} className="flex items-center gap-2 py-1.5 first:pt-0">
                        <span className="flex-shrink-0 text-xs font-semibold text-slate-200">{row.receipt_number}</span>
                        <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400">
                          {row.part_number || row.part_name || '—'}
                          {row.vendor_name ? ` · ${row.vendor_name}` : ''}
                        </span>
                        <span
                          className={`flex-shrink-0 text-[11px] font-semibold tabular-nums ${
                            row.days_pending > 3
                              ? 'text-red-400'
                              : row.days_pending > 1
                                ? 'text-amber-400'
                                : 'text-slate-300'
                          }`}
                          title="Days waiting for inspection"
                        >
                          {row.days_pending}d
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          );
        }}
      </SourceBody>
    </CockpitPanel>
  );
}
