import React from 'react';
import type { OperationHoursSource as HoursSource, WorkOrderOperation } from '../../types';

type HoursFields = Pick<
  WorkOrderOperation,
  | 'actual_setup_hours'
  | 'actual_run_hours'
  | 'hours_source'
  | 'recorded_setup_hours'
  | 'recorded_run_hours'
  | 'hours_include_backfill'
>;

const SOURCE_LABEL: Record<Exclude<HoursSource, 'none'>, string> = {
  time_entries: 'time entries',
  manual: 'manual',
  standard: 'standard',
};

const SOURCE_EXPLANATION: Record<Exclude<HoursSource, 'none'>, string> = {
  time_entries: 'Measured by closed time entries (clock-in to clock-out).',
  manual: 'Stored hours with no time entries behind them (legacy or imported data).',
  standard: 'Routing standard hours.',
};

const hours = (value: number | null | undefined) => Number(value || 0);

/**
 * The quiet "where do these actual hours come from" line under an operation's
 * estimated hours on the work-order detail page. Reads the server-derived
 * `hours_source` (P1.4) -- nothing is inferred client-side -- and renders nothing
 * when the field is absent (an older API, another route) or `none`.
 */
export function OperationHoursSource({ operation }: { operation: HoursFields }) {
  const source = operation.hours_source;
  if (!source || source === 'none') return null;
  const actual = hours(operation.actual_setup_hours) + hours(operation.actual_run_hours);
  const recordedSetup = hours(operation.recorded_setup_hours);
  const recordedRun = hours(operation.recorded_run_hours);
  const titleParts = [SOURCE_EXPLANATION[source]];
  if (source === 'time_entries') {
    titleParts.push(`Closed entries: setup ${recordedSetup.toFixed(2)} h, run ${recordedRun.toFixed(2)} h.`);
    if (Math.abs(recordedSetup + recordedRun - actual) > 0.005) {
      titleParts.push('Some entries were closed without crediting this operation (a hold or an automatic clock-out).');
    }
  }
  if (operation.hours_include_backfill) {
    titleParts.push('Includes paper back-entries or imports, timed by when they were keyed, not the labor.');
  }
  return (
    <div
      className="mt-0.5 text-[11px] text-fd-mute tabular-nums"
      title={titleParts.join(' ')}
      data-testid="op-hours-source"
    >
      Act {actual.toFixed(2)} · {SOURCE_LABEL[source]}
      {operation.hours_include_backfill && <span className="text-fd-amber"> · incl. back-entry</span>}
    </div>
  );
}

export default OperationHoursSource;
