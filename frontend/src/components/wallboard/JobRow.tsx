/**
 * One job row inside a work-center tile: part number leads (bold, amber +
 * "LATE Nd" chip when late), qty right; WO · op · crew secondary line with
 * elapsed right; a thin full-width progress bar underneath (green, amber when
 * late, width transitions 600ms at poll boundaries — spec motion item 4).
 */

import React from 'react';
import type { WallboardActiveJob } from '../../types/wallboard';
import { formatDownDuration } from '../../utils/wallboardLayout';
import { WB } from './wallboardTokens';

export function ProgressBar({ done, target, late }: { done: number; target: number; late: boolean }) {
  const pct = target > 0 ? Math.min(100, Math.max(0, (done / target) * 100)) : 0;
  return (
    <div className="h-[0.5rem] w-full bg-[#10151d]">
      <div
        className="h-full"
        style={{
          width: `${pct}%`,
          backgroundColor: late ? WB.amber : WB.green,
          transition: 'width 600ms ease',
        }}
      />
    </div>
  );
}

export default function JobRow({
  job,
  extraMinutes,
  lateDaysByWo,
}: {
  job: WallboardActiveJob;
  /** Whole minutes since the payload landed — elapsed counters tick between polls. */
  extraMinutes: number;
  /** days_late by WO number (from late_wos) for the "LATE 6d" suffix chip. */
  lateDaysByWo: Map<string, number>;
}) {
  // Back-compat: old payloads have no crew/is_late — fall back to operator_name.
  const crew = job.crew ?? (job.operator_name ? [job.operator_name] : []);
  const crewCount = job.crew_count ?? crew.length;
  const isLate = job.is_late ?? false;
  const lateDays = job.wo_number ? lateDaysByWo.get(job.wo_number) : undefined;

  const crewLabel = crew.length > 0 ? `${crew[0]}${crewCount > 1 ? ` +${crewCount - 1}` : ''}` : null;
  const secondary = [job.wo_number, job.op_name, crewLabel].filter(Boolean).join(' · ');

  return (
    <div className="flex min-w-0 flex-col gap-[0.25rem]">
      <div className="flex items-baseline justify-between gap-[1rem]">
        <p className="min-w-0 truncate text-[1.875rem] font-bold leading-tight">
          <span style={{ color: isLate ? WB.amber : WB.text }}>{job.part_number ?? job.wo_number ?? '—'}</span>
          {isLate && (
            <span className="ml-[0.75rem] inline-block bg-[#d29922] px-[0.5rem] align-middle text-[1.25rem] font-bold uppercase leading-snug text-black">
              Late{lateDays !== undefined ? ` ${lateDays}d` : ''}
            </span>
          )}
        </p>
        <p className="shrink-0 text-[1.375rem] tabular-nums leading-tight text-[#f0f4f9]">
          {job.qty_done}/{job.qty_target}
        </p>
      </div>
      <div className="flex items-baseline justify-between gap-[1rem]">
        <p className="min-w-0 truncate text-[1.375rem] leading-tight text-[#8b98a9]">{secondary || '—'}</p>
        <p className="shrink-0 text-[1.375rem] tabular-nums leading-tight text-[#8b98a9]">
          {formatDownDuration(job.elapsed_minutes + extraMinutes)}
        </p>
      </div>
      <ProgressBar done={job.qty_done} target={job.qty_target} late={isLate} />
    </div>
  );
}
