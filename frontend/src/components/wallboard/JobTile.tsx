/**
 * One work-order tile on the job wall. The header is a SOLID FILLED color
 * band — the andon signal (state is never a hairline border): red DOWN,
 * orange BLOCKED, amber LATE, green RUNNING, slate WAITING. Text on filled
 * state fields is black. A tile newly entering DOWN/BLOCKED flashes its fill
 * for ~10s (steps(1) @1.2s ×8) then settles steady.
 *
 * Body (THE ASK — owner feedback 2026-07-15): the part number leads, then
 * "Op {n}/{total} · {op name} · {work center}" so a glance answers "what op
 * is it on?"; crew suffix when someone is clocked in; WO-level qty + progress
 * bar. A down job's body leads with "MACHINE DOWN · {wc}" in red.
 */

import React from 'react';
import type { WallboardJob } from '../../types/wallboard';
import type { JobStateClass, WallboardDensityTier } from '../../utils/wallboardLayout';
import { classifyJob, formatDownDuration } from '../../utils/wallboardLayout';
import { ProgressBar } from './JobRow';
import { WB } from './wallboardTokens';

const HEADER_FILL: Record<JobStateClass, string> = {
  down: WB.red,
  blocked: WB.orange,
  late: WB.amber,
  running: WB.green,
  waiting: WB.slate,
};

const STATE_WORD: Record<JobStateClass, string> = {
  down: 'DOWN',
  blocked: 'BLOCKED',
  late: 'LATE',
  running: 'RUNNING',
  waiting: 'WAITING',
};

const HEADER_HEIGHT: Record<WallboardDensityTier, string> = {
  roomy: 'h-[4.5rem]',
  standard: 'h-[4rem]',
  dense: 'h-[3.25rem]',
};

export default function JobTile({
  job,
  tier,
  flash,
  extraMinutes,
  widthCap,
}: {
  job: WallboardJob;
  tier: WallboardDensityTier;
  /** Newly entered DOWN/BLOCKED this poll → flash the header fill (~10s). */
  flash: boolean;
  /** Whole minutes since the payload landed — elapsed counters tick between polls. */
  extraMinutes: number;
  /** N=1 grid: cap the lone tile's width and center it. */
  widthCap: boolean;
}) {
  const stateClass = classifyJob(job);
  const op = job.current_op ?? null;
  const isLate = job.is_late ?? false;
  const daysLate = job.days_late ?? 0;
  const qtyComplete = job.qty_complete ?? 0;
  const qtyOrdered = job.qty_ordered ?? 0;

  // "Op 3/5 · Deburr · Mill 1" — the op position is 1-based off ops_completed.
  const opsTotal = job.ops_total ?? 0;
  const opIndex = Math.min((job.ops_completed ?? 0) + 1, Math.max(opsTotal, 1));
  const crew = op?.crew ?? [];
  const crewCount = op?.crew_count ?? crew.length;
  const crewLabel = crew.length > 0 ? `${crew[0]}${crewCount > 1 ? ` +${crewCount - 1}` : ''}` : null;
  const opLine = op
    ? [
        opsTotal > 0 ? `Op ${opIndex}/${opsTotal}` : null,
        op.name ?? null,
        op.work_center_name ?? op.work_center_code ?? null,
        crewLabel,
      ]
        .filter(Boolean)
        .join(' · ')
    : null;

  return (
    <section
      data-testid={`job-tile-${job.wo_number}`}
      className={`flex min-h-0 min-w-0 flex-col overflow-hidden bg-[#141b26] ${
        widthCap ? 'w-full max-w-[44rem] justify-self-center' : ''
      }`}
    >
      {/* Filled state band — the andon signal. */}
      <div
        data-testid={`job-tile-header-${job.wo_number}`}
        className={`flex shrink-0 items-center justify-between gap-[1rem] px-[1rem] ${
          HEADER_HEIGHT[tier]
        } ${flash ? 'wb-flash-new' : ''}`}
        style={{ backgroundColor: HEADER_FILL[stateClass] }}
      >
        {/* Smaller than the machine wall's names: a 15-char WO number is the
            tile's identity and must survive UNtruncated at a 3-column grid
            width — its suffix digits are the distinguishing part. The band
            color carries the state at distance; the word is the caption. */}
        <h2
          className={`min-w-0 truncate font-extrabold uppercase leading-none tracking-normal tabular-nums text-black ${
            tier === 'dense' ? 'text-[1.375rem]' : 'text-[1.75rem]'
          }`}
        >
          {job.wo_number}
        </h2>
        <span className="shrink-0 whitespace-nowrap text-[1.25rem] font-bold uppercase leading-none text-black">
          {STATE_WORD[stateClass]}
        </span>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-[0.5rem] overflow-hidden px-[1rem] py-[0.75rem]">
        {/* DOWN reason context leads the body; BLOCKED keeps the normal body
            (the band carries it — the rail names the blocker). */}
        {stateClass === 'down' && (
          <p className="truncate text-[1.75rem] font-bold uppercase leading-tight tracking-wide text-[#f04438]">
            Machine Down{op?.work_center_name ? ` · ${op.work_center_name}` : ''}
          </p>
        )}

        {/* Line 1: part number leads; WO-level qty right. */}
        <div className="flex items-baseline justify-between gap-[1rem]">
          <p className="min-w-0 truncate text-[1.875rem] font-bold leading-tight">
            <span style={{ color: isLate ? WB.amber : WB.text }}>{job.part_number ?? '—'}</span>
            {isLate && (
              <span className="ml-[0.75rem] inline-block bg-[#d29922] px-[0.5rem] align-middle text-[1.25rem] font-bold uppercase leading-snug text-black">
                Late{daysLate > 0 ? ` ${daysLate}d` : ''}
              </span>
            )}
          </p>
          <p className="shrink-0 text-[1.375rem] tabular-nums leading-tight text-[#f0f4f9]">
            {qtyComplete}/{qtyOrdered}
          </p>
        </div>

        {/* Line 2 (THE ASK): current op position, name, work center, crew;
            live elapsed right (only meaningful while labor is open). */}
        <div className="flex items-baseline justify-between gap-[1rem]">
          <p className="min-w-0 truncate text-[1.375rem] leading-tight text-[#8b98a9]">{opLine ?? '—'}</p>
          {job.running && op && (
            <p className="shrink-0 text-[1.375rem] tabular-nums leading-tight text-[#8b98a9]">
              {formatDownDuration((op.elapsed_minutes ?? 0) + extraMinutes)}
            </p>
          )}
        </div>

        {/* Thin WO-level progress bar: green, amber when late. */}
        <ProgressBar done={qtyComplete} target={qtyOrdered} late={isLate} />
      </div>
    </section>
  );
}
