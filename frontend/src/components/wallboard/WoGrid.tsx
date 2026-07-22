/**
 * The 4-col × 3-row work-order grid + the "+N MORE" overflow strip (design
 * handoff 2026-07-22, zone 2).
 *
 * Shows the FIRST 12 of the server-sorted jobs — severity order is the
 * server's; the client NEVER re-sorts. Fewer than 12 → trailing cells stay
 * plain background (the grid template keeps the geometry). The strip counts
 * overflow from the uncapped jobs_total. Empty jobs → a full-zone
 * NO OPEN WORK ORDERS state and no strip; a payload with `jobs` missing
 * entirely (pre-job-wall backend) degrades to a BOARD DATA UNAVAILABLE
 * state instead of crashing. Nothing scrolls, ever.
 */

import React from 'react';
import type { WallboardBlockedWorkOrder, WallboardJob, WallboardWorkCenter } from '../../types/wallboard';
import WoCard from './WoCard';
import { FD } from './wallboardTokens';

const GRID_CAP = 12;

function EmptyZone({ text }: { text: string }) {
  return (
    <div
      data-testid="wo-grid-empty"
      className="flex min-h-0 min-w-0 flex-1 items-center justify-center rounded-[0.25rem] px-[2rem]"
      style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}` }}
    >
      <span className="text-center text-[1.5rem] font-semibold tracking-[0.14em]" style={{ color: FD.mute }}>
        {text}
      </span>
    </div>
  );
}

export default function WoGrid({
  jobs,
  jobsTotal,
  workCenters,
  blockedWos,
  extraMinutes,
}: {
  jobs: WallboardJob[] | null;
  jobsTotal: number | null;
  /** Downtime join source: current-op work center code → open downtime. */
  workCenters: WallboardWorkCenter[];
  /** Blocked-age join source: wo_number → category + age_hours. */
  blockedWos: WallboardBlockedWorkOrder[];
  extraMinutes: number;
}) {
  if (!Array.isArray(jobs)) {
    return <EmptyZone text="BOARD DATA UNAVAILABLE — BACKEND UPDATE REQUIRED" />;
  }
  if (jobs.length === 0) {
    return <EmptyZone text="NO OPEN WORK ORDERS" />;
  }

  const visible = jobs.slice(0, GRID_CAP);
  const remaining = Math.max(0, (jobsTotal ?? jobs.length) - visible.length);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-[0.625rem]">
      <div className="grid min-h-0 flex-1 grid-cols-4 grid-rows-3 gap-[0.8125rem]" data-testid="wo-grid">
        {visible.map(job => {
          const code = job.current_op?.work_center_code ?? null;
          const downtime = code !== null ? (workCenters.find(wc => wc.code === code)?.down ?? null) : null;
          const blockedInfo = blockedWos.find(b => b.wo_number === job.wo_number) ?? null;
          return (
            <WoCard
              key={job.wo_number}
              job={job}
              downtime={downtime}
              blockedInfo={blockedInfo}
              extraMinutes={extraMinutes}
            />
          );
        })}
      </div>
      <div
        data-testid="wo-overflow-strip"
        className="flex h-[2.375rem] flex-none items-center justify-center rounded-[0.25rem]"
        style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}` }}
      >
        <span className="text-[0.9375rem] font-semibold tracking-[0.14em]" style={{ color: FD.mute }}>
          {remaining > 0 ? `+${remaining} MORE WORK ORDERS IN QUEUE` : 'ALL OPEN WORK ORDERS ON BOARD'}
        </span>
      </div>
    </div>
  );
}
