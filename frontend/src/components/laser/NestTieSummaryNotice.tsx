/**
 * "N of M nests have no sheet tied" — the office-side untied-nest marker for one
 * job, read from `GET /laser-nests/work-orders/{id}/tie-summary` (its own nests
 * plus its laser child jobs').
 *
 * Three properties are deliberate:
 *
 * 1. **Office screens only.** The dispatch board, kiosk and crew station render
 *    nothing for an untied operation, by recorded contract. This component and
 *    its hook are mounted on Work Order Detail and nowhere on the floor.
 * 2. **A warning, never a gate.** An untied nest completes normally and deducts
 *    nothing; Release still goes ahead after the warning. Nothing here blocks.
 * 3. **Silent on failure.** The endpoint is ADMIN / MANAGER / SUPERVISOR; the
 *    hook is only enabled for `work_orders:edit`, and any failure (403, network,
 *    an older API without the route) renders nothing rather than an error box —
 *    the panel it rides on is not about this figure.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import type { WorkOrderNestTieSummary } from '../../types/untiedNests';

export interface UseWorkOrderNestTieSummary {
  summary: WorkOrderNestTieSummary | null;
  /** Re-read now; resolves to the fresh summary, or null when the read failed. */
  refresh: () => Promise<WorkOrderNestTieSummary | null>;
}

/**
 * Load the tie summary for `workOrderId` while `enabled`; re-read whenever
 * `refreshKey` changes (the page passes the work order's `updated_at` plus its
 * tie-refresh counter, since a tie write does not bump the work order).
 */
export function useWorkOrderNestTieSummary(
  workOrderId: number | null | undefined,
  enabled: boolean,
  refreshKey: string
): UseWorkOrderNestTieSummary {
  const [summary, setSummary] = useState<WorkOrderNestTieSummary | null>(null);
  const requestRef = useRef(0);

  const refresh = useCallback(async (): Promise<WorkOrderNestTieSummary | null> => {
    const requestId = ++requestRef.current;
    if (!enabled || !workOrderId) {
      setSummary(null);
      return null;
    }
    try {
      const value = await api.getWorkOrderNestTieSummary(workOrderId);
      if (requestId === requestRef.current) setSummary(value ?? null);
      return value ?? null;
    } catch {
      if (requestId === requestRef.current) setSummary(null);
      return null;
    }
  }, [enabled, workOrderId]);

  useEffect(() => {
    void refresh();
  }, [refresh, refreshKey]);

  // A route change to another work order must not show the previous job's figure
  // while the next read is in flight.
  useEffect(() => {
    setSummary(null);
  }, [workOrderId]);

  return { summary, refresh };
}

/** The Release-confirm text for a job with untied nests (a warning; release still proceeds). */
export function releaseUntiedNestWarning(summary: WorkOrderNestTieSummary): string {
  const lines = [
    `${summary.message ?? `${summary.untied_count} of ${summary.nest_count} nests have no sheet tied`}.`,
    'An untied nest completes normally but deducts no sheet from stock, so on-hand stays overstated by what it burns.',
    'You can still release now and tie the sheets afterwards from Untied Laser Nests or the Operations table.',
  ];
  if (summary.job_level_tie_count > 0) {
    lines.push(
      `This job already has ${summary.job_level_tie_count} whole-work-order material tie${
        summary.job_level_tie_count === 1 ? '' : 's'
      }; tying a nest as well could draw the same sheet twice.`
    );
  }
  return lines.join('\n\n');
}

export interface NestTieSummaryNoticeProps {
  summary: WorkOrderNestTieSummary | null;
}

/** The amber strip on the Material Ties panel. Renders nothing when every nest is tied. */
export function NestTieSummaryNotice({ summary }: NestTieSummaryNoticeProps) {
  if (!summary || summary.untied_count <= 0 || summary.nest_count <= 0) return null;
  const preview = summary.untied.slice(0, 6);
  const remaining = summary.untied.length - preview.length;
  return (
    <div
      role="status"
      data-testid="wo-nest-tie-summary"
      className="mb-3 rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-3"
    >
      <div className="flex items-start gap-2.5">
        <ExclamationTriangleIcon className="mt-0.5 h-5 w-5 flex-shrink-0 text-fd-amber" aria-hidden="true" />
        <div className="min-w-0 text-sm text-fd-body">
          <p className="font-semibold text-fd-amber">
            {summary.message ?? `${summary.untied_count} of ${summary.nest_count} nests have no sheet tied`}
          </p>
          <p className="mt-1 text-xs text-fd-mute">
            An untied nest deducts no sheet when it completes. Tie each one from the Operations table, or from the
            worklist where the sheet matcher proposes the part.
            {summary.job_level_tie_count > 0 &&
              ' This job also carries a whole-work-order tie — check it before tying nests, or the sheet may be drawn twice.'}
          </p>
          {preview.length > 0 && (
            <ul className="mt-2 flex flex-wrap gap-1.5" aria-label="Nests with no sheet tied">
              {preview.map(row => (
                <li
                  key={row.laser_nest_id}
                  className="rounded-none border border-fd-line px-1.5 py-0.5 font-mono text-[11px] text-fd-body"
                >
                  {row.cnc_number || row.nest_name}
                  {row.work_order_id !== summary.work_order_id && (
                    <span className="text-fd-mute"> · {row.work_order_number}</span>
                  )}
                </li>
              ))}
              {remaining > 0 && <li className="px-1 py-0.5 text-[11px] text-fd-mute">+{remaining} more</li>}
            </ul>
          )}
          <Link
            to={`/laser-nests/untied?work_order_id=${summary.work_order_id}`}
            className="mt-2 inline-block text-xs font-semibold text-fd-blue hover:underline"
          >
            Open in Untied Laser Nests
          </Link>
        </div>
      </div>
    </div>
  );
}

export default NestTieSummaryNotice;
