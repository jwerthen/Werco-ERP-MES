/**
 * One work-center tile on the floor wall. The header is a SOLID FILLED color
 * band — the andon signal (state is never a hairline border): green RUNNING,
 * red DOWN (with live minutes), orange BLOCKED, amber RUNNING · LATE. Text on
 * filled state fields is black. A tile newly entering DOWN/BLOCKED flashes its
 * fill for ~10s (steps(1) @1.2s ×8) then settles steady.
 */

import React from 'react';
import type { WallboardWorkCenter } from '../../types/wallboard';
import type { WallboardDensityTier } from '../../utils/wallboardLayout';
import { blockerLabel, classifyWorkCenter, formatDownDuration, TIER_JOB_ROWS } from '../../utils/wallboardLayout';
import JobRow from './JobRow';
import { WB } from './wallboardTokens';

const HEADER_FILL: Record<ReturnType<typeof classifyWorkCenter>, string> = {
  down: WB.red,
  blocked: WB.orange,
  late: WB.amber,
  running: WB.green,
};

const HEADER_HEIGHT: Record<WallboardDensityTier, string> = {
  roomy: 'h-[4.5rem]',
  standard: 'h-[4rem]',
  dense: 'h-[3.25rem]',
};

function QueueChip({ count }: { count: number }) {
  // Escalation: hidden at 0 (an empty queue isn't worth the header space the
  // machine name needs), default 1–4, amber-filled ≥5, red-outline ≥10.
  if (count === 0) return null;
  let className = 'text-black';
  if (count >= 10) className = 'border-[0.125rem] border-[#f04438] px-[0.5rem] text-black';
  else if (count >= 5) className = 'bg-[#d29922] px-[0.5rem] text-black';
  return (
    <span className={`shrink-0 whitespace-nowrap text-[1.375rem] font-bold leading-snug tabular-nums ${className}`}>
      Q {count}
    </span>
  );
}

export default function WorkCenterTile({
  wc,
  tier,
  flash,
  extraMinutes,
  lateDaysByWo,
  widthCap,
}: {
  wc: WallboardWorkCenter;
  tier: WallboardDensityTier;
  /** Newly entered DOWN/BLOCKED this poll → flash the header fill (~10s). */
  flash: boolean;
  extraMinutes: number;
  lateDaysByWo: Map<string, number>;
  /** N=1 grid: cap the lone tile's width and center it. */
  widthCap: boolean;
}) {
  const stateClass = classifyWorkCenter(wc);
  const code = wc.code ?? wc.id;

  // Keep the band short so the MACHINE NAME always survives — the down
  // category and live duration move into the (otherwise empty) body.
  let stateWord: string;
  if (stateClass === 'down') {
    stateWord = 'DOWN';
  } else if (stateClass === 'blocked') {
    stateWord = `${wc.blocked_count} BLOCKED`;
  } else if (stateClass === 'late') {
    stateWord = 'LATE';
  } else {
    stateWord = 'RUNNING';
  }

  const budget = TIER_JOB_ROWS[tier];
  const jobs = wc.active_jobs.slice(0, budget);
  const hidden = wc.active_jobs.length - jobs.length;

  return (
    <section
      data-testid={`wc-card-${code}`}
      className={`flex min-h-0 min-w-0 flex-col overflow-hidden bg-[#141b26] ${
        widthCap ? 'w-full max-w-[44rem] justify-self-center' : ''
      }`}
    >
      {/* Filled state band — the andon signal. */}
      <div
        data-testid={`wc-tile-header-${code}`}
        className={`flex shrink-0 items-center justify-between gap-[1rem] px-[1rem] ${
          HEADER_HEIGHT[tier]
        } ${flash ? 'wb-flash-new' : ''}`}
        style={{ backgroundColor: HEADER_FILL[stateClass] }}
      >
        <h2
          className={`min-w-0 truncate font-extrabold uppercase leading-none tracking-wide text-black ${
            tier === 'dense' ? 'text-[1.75rem]' : 'text-[2.125rem]'
          }`}
        >
          {wc.name}
        </h2>
        <div className="flex min-w-0 shrink-0 items-center gap-[1rem]">
          <span className="whitespace-nowrap text-[1.75rem] font-bold uppercase leading-none tabular-nums text-black">
            {stateWord}
          </span>
          <QueueChip count={wc.queued_count} />
        </div>
      </div>

      {/* Job rows (server-grouped: one row per operation). */}
      <div className="flex min-h-0 flex-1 flex-col gap-[0.75rem] overflow-hidden px-[1rem] py-[0.75rem]">
        {stateClass === 'down' && (
          <p className="text-[1.75rem] font-bold uppercase leading-tight tracking-wide text-[#f04438] tabular-nums">
            {blockerLabel(wc.down!.category)} · {formatDownDuration(wc.down!.minutes + extraMinutes)}
          </p>
        )}
        {jobs.map((job, idx) => (
          <JobRow
            // idx suffix: an OLD backend payload emits one row per time entry,
            // so two crew members on one op would otherwise collide on wo:op.
            key={`${job.wo_number ?? 'wo'}:${job.op_name ?? ''}:${idx}`}
            job={job}
            extraMinutes={extraMinutes}
            lateDaysByWo={lateDaysByWo}
          />
        ))}
        {hidden > 0 && <p className="text-[1.25rem] leading-tight text-[#8b98a9]">+{hidden} more</p>}
      </div>
    </section>
  );
}
