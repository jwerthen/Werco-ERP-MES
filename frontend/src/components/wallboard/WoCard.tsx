/**
 * One work-order card on the Foundry TV board's 4×3 grid (design handoff
 * 2026-07-22, zone 2).
 *
 * Five fixed rows — header (WO + status chip) / part + qty / op + time /
 * machine + stop reason / progress — all keyed off classifyJob's strict
 * DOWN > BLOCKED > LATE > RUNNING > WAITING precedence. Stoppage detail is
 * JOINED client-side by the caller (downtime from work_centers by
 * work-center code, blocked age from blocked_wos by WO number) and degrades
 * to blank cells when a join misses — the design itself has blank cells.
 * Only DOWN chip dots pulse; WAITING cards carry no glow anywhere.
 */

import React from 'react';
import type { WallboardBlockedWorkOrder, WallboardDowntime, WallboardJob } from '../../types/wallboard';
import { blockerLabel, classifyJob, formatAgeHours, formatDownDuration, JobStateClass } from '../../utils/wallboardLayout';
import { FD } from './wallboardTokens';

interface StateSpec {
  edge: string;
  chipColor: string;
  chipBg: string;
  chipEdge: string;
  dotGlow: string | null;
  barFill: string;
  barGlow: string | null;
  pulse: boolean;
}

/** Exact per-state tints from the handoff (chip bg ~12% / edge ~40%). */
const STATE_SPECS: Record<JobStateClass, StateSpec> = {
  down: {
    edge: FD.red,
    chipColor: FD.red,
    chipBg: 'rgba(240,68,56,0.14)',
    chipEdge: 'rgba(240,68,56,0.45)',
    dotGlow: '0 0 0.5rem rgba(240,68,56,0.9)',
    barFill: FD.red,
    barGlow: '0 0 0.5rem rgba(240,68,56,0.5)',
    pulse: true,
  },
  blocked: {
    edge: FD.blockedOrange,
    chipColor: FD.blockedOrange,
    chipBg: 'rgba(234,125,44,0.12)',
    chipEdge: 'rgba(234,125,44,0.4)',
    dotGlow: '0 0 0.4375rem rgba(234,125,44,0.8)',
    barFill: FD.blockedOrange,
    barGlow: '0 0 0.5rem rgba(234,125,44,0.4)',
    pulse: false,
  },
  late: {
    edge: FD.amber,
    chipColor: FD.amber,
    chipBg: 'rgba(210,153,34,0.12)',
    chipEdge: 'rgba(210,153,34,0.4)',
    dotGlow: '0 0 0.4375rem rgba(210,153,34,0.8)',
    barFill: FD.amber,
    barGlow: '0 0 0.5rem rgba(210,153,34,0.4)',
    pulse: false,
  },
  running: {
    edge: FD.green,
    chipColor: FD.green,
    chipBg: 'rgba(63,185,80,0.10)',
    chipEdge: 'rgba(63,185,80,0.38)',
    dotGlow: '0 0 0.4375rem rgba(63,185,80,0.8)',
    barFill: FD.green,
    barGlow: '0 0 0.5rem rgba(63,185,80,0.5)',
    pulse: false,
  },
  waiting: {
    edge: FD.faint,
    chipColor: FD.waiting,
    chipBg: 'rgba(139,152,165,0.08)',
    chipEdge: 'rgba(139,152,165,0.28)',
    dotGlow: null,
    barFill: FD.waiting,
    barGlow: null,
    pulse: false,
  },
};

function chipWord(state: JobStateClass, job: WallboardJob): string {
  switch (state) {
    case 'down':
      return 'DOWN';
    case 'blocked':
      return 'BLOCKED';
    case 'late':
      return `LATE ${job.days_late ?? 0}D`;
    case 'running':
      return 'RUNNING';
    default:
      return 'WAITING';
  }
}

export default function WoCard({
  job,
  downtime,
  blockedInfo,
  extraMinutes,
}: {
  job: WallboardJob;
  /** Open downtime on the current op's work center (join by wc code), if any. */
  downtime: WallboardDowntime | null;
  /** The WO's row in blocked_wos (join by wo_number), if any. */
  blockedInfo: WallboardBlockedWorkOrder | null;
  /** Whole minutes since the last good poll — counters tick between polls. */
  extraMinutes: number;
}) {
  const state = classifyJob(job);
  const spec = STATE_SPECS[state];
  const waiting = state === 'waiting';

  const qtyOrdered = job.qty_ordered ?? 0;
  const qtyComplete = job.qty_complete ?? 0;
  const pct = qtyOrdered > 0 ? Math.min(100, Math.max(0, Math.round((100 * qtyComplete) / qtyOrdered))) : 0;

  const elapsed = job.current_op
    ? formatDownDuration((job.current_op.elapsed_minutes ?? 0) + extraMinutes).toUpperCase()
    : null;

  // Op-row right: the state's time value (blank when the state has none or a
  // join missed — a blank cell is part of the design).
  let timeValue: { text: string; color: string; bold: boolean } | null = null;
  if (state === 'down' && downtime) {
    timeValue = { text: formatDownDuration(downtime.minutes + extraMinutes).toUpperCase(), color: FD.red, bold: true };
  } else if (state === 'blocked' && blockedInfo) {
    timeValue = { text: formatAgeHours(blockedInfo.age_hours).toUpperCase(), color: FD.blockedOrange, bold: true };
  } else if (state === 'running' && elapsed) {
    timeValue = { text: elapsed, color: FD.green, bold: true };
  } else if (state === 'late' && job.running && elapsed) {
    timeValue = { text: elapsed, color: FD.body, bold: false };
  }

  // Machine-row right: the stop reason.
  let reason: { text: string; color: string; bold: boolean } | null = null;
  if (state === 'down' && downtime) {
    reason = { text: blockerLabel(downtime.category).toUpperCase(), color: FD.red, bold: true };
  } else if (state === 'blocked' && blockedInfo) {
    reason = { text: blockerLabel(blockedInfo.category).toUpperCase(), color: FD.blockedOrange, bold: true };
  } else if (state === 'waiting') {
    reason = { text: 'IN QUEUE', color: FD.mute, bold: false };
  }

  return (
    <div
      data-testid={`wo-card-${job.wo_number}`}
      className="flex min-w-0 flex-col justify-between rounded-[0.25rem] px-[1.125rem] py-[1rem]"
      style={{
        background:
          state === 'down'
            ? `linear-gradient(165deg, rgba(240,68,56,0.10), rgba(240,68,56,0.02) 60%), ${FD.panel}`
            : FD.panel,
        border: `0.0625rem solid ${state === 'down' ? 'rgba(240,68,56,0.45)' : FD.line}`,
        borderLeft: `0.25rem solid ${spec.edge}`,
      }}
    >
      {/* Row 1 — WO number + status chip */}
      <div className="flex items-center justify-between gap-[0.5rem]">
        <span className="min-w-0 truncate text-[1.3125rem] font-semibold tracking-[0.03em]" style={{ color: FD.body }}>
          {job.wo_number}
        </span>
        <span
          className="flex shrink-0 items-center gap-[0.5rem] rounded-[0.1875rem] px-[0.625rem] py-[0.3125rem] text-[0.875rem] font-bold tracking-[0.13em]"
          style={{ color: spec.chipColor, background: spec.chipBg, border: `0.0625rem solid ${spec.chipEdge}` }}
        >
          <span
            className="h-[0.5rem] w-[0.5rem] rounded-full"
            style={{
              background: spec.chipColor,
              boxShadow: spec.dotGlow ?? 'none',
              animation: spec.pulse ? 'fdPulse 1.6s ease-in-out infinite' : 'none',
            }}
          />
          {chipWord(state, job)}
        </span>
      </div>

      {/* Row 2 — part number + qty done/total */}
      <div className="flex items-baseline justify-between gap-[0.625rem]">
        <span
          className="min-w-0 truncate text-[1.9375rem] font-extrabold tracking-[-0.01em]"
          style={{ color: waiting ? FD.body : FD.ink }}
        >
          {job.part_number ?? ''}
        </span>
        <span className="shrink-0 text-[1.1875rem] font-medium" style={{ color: FD.mute }}>
          <span className="font-bold" style={{ color: waiting ? FD.body : FD.ink }}>
            {qtyComplete}
          </span>
          /{qtyOrdered}
        </span>
      </div>

      {/* Row 3 — op position + the state's time value */}
      <div className="flex items-center justify-between gap-[0.5rem] text-[1rem]">
        <span
          className="min-w-0 truncate tracking-[0.05em]"
          style={{ color: job.current_op ? (waiting ? FD.mute : FD.body) : FD.mute }}
        >
          {job.current_op
            ? `OP ${(job.ops_completed ?? 0) + 1}/${job.ops_total ?? 0} · ${(job.current_op.name ?? '').toUpperCase()}`
            : 'ALL OPS COMPLETE'}
        </span>
        {timeValue ? (
          <span
            className={`shrink-0 ${timeValue.bold ? 'font-bold' : 'font-semibold'}`}
            style={{ color: timeValue.color }}
          >
            {timeValue.text}
          </span>
        ) : (
          <span />
        )}
      </div>

      {/* Row 4 — machine + stop reason */}
      <div className="flex items-center justify-between gap-[0.5rem] text-[1rem]">
        <span className="min-w-0 truncate tracking-[0.05em]" style={{ color: FD.mute }}>
          {(job.current_op?.work_center_name ?? job.current_op?.work_center_code ?? '').toUpperCase()}
        </span>
        {reason ? (
          <span
            className={`shrink-0 whitespace-nowrap tracking-[0.06em] ${reason.bold ? 'font-bold' : 'font-semibold'}`}
            style={{ color: reason.color }}
          >
            {reason.text}
          </span>
        ) : (
          <span />
        )}
      </div>

      {/* Row 5 — progress bar + percent */}
      <div className="flex items-center gap-[0.625rem]">
        <div className="h-[0.375rem] min-w-0 flex-1 overflow-hidden rounded-[0.125rem]" style={{ background: FD.sunken }}>
          <div
            className="h-full"
            style={{ width: `${pct}%`, background: spec.barFill, boxShadow: spec.barGlow ?? 'none' }}
          />
        </div>
        <span className="shrink-0 text-[0.875rem] font-semibold" style={{ color: FD.mute }}>
          {pct}%
        </span>
      </div>
    </div>
  );
}
