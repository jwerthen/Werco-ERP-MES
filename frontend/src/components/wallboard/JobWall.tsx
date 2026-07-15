/**
 * Z2 JOB WALL — deterministic grid of open WORK-ORDER tiles (owner feedback
 * 2026-07-15: the main wall shows work orders with their current operation,
 * not machines; work-center state stays visible via tile state + the rail).
 *
 * Order is the SERVER's priority sort (blocked/down first, then late by
 * days_late, then running, then promise date) — the client never re-sorts.
 * Grid shape always exactly fills (rows = round(sqrt(N/1.6)), cols =
 * ceil(N/rows)); trailing empty cells render as plain background,
 * bottom-right. Density tier (header height) is damped with 2-poll
 * hysteresis. The server caps the list at 24; jobs_total carries the true
 * count for the "+N more" line. No idle strip in job mode — idle machines
 * live in the exception rail.
 */

import React, { useRef } from 'react';
import type { WallboardJob } from '../../types/wallboard';
import type { TierHysteresisState } from '../../utils/wallboardLayout';
import { computeGridShape, nextTierState } from '../../utils/wallboardLayout';
import JobTile from './JobTile';

export default function JobWall({
  jobs,
  jobsTotal,
  pollKey,
  flashKeys,
  extraMinutes,
}: {
  jobs: WallboardJob[];
  /** Uncapped open-WO count (undefined/null on a sparse payload → no "+N more"). */
  jobsTotal: number | null | undefined;
  /** Changes once per successful poll (generated_at) — drives tier hysteresis. */
  pollKey: string;
  flashKeys: Set<string>;
  extraMinutes: number;
}) {
  // Density-tier hysteresis: advance the state machine exactly once per poll.
  // Render-time ref mutation is guarded by pollKey, so StrictMode double
  // renders and clock-tick re-renders never double-advance it.
  const hysteresisRef = useRef<{ key: string | null; state: TierHysteresisState | null }>({
    key: null,
    state: null,
  });
  if (hysteresisRef.current.key !== pollKey) {
    hysteresisRef.current = {
      key: pollKey,
      state: nextTierState(hysteresisRef.current.state, jobs.length),
    };
  }
  const tier = hysteresisRef.current.state?.tier ?? 'roomy';

  // Nothing released or in progress: a calm designed empty state, not an error.
  if (jobs.length === 0) {
    return (
      <div data-testid="job-wall" className="flex h-full items-center justify-center">
        <p className="text-[1.875rem] text-[#8b98a9]">No open work orders</p>
      </div>
    );
  }

  const { rows, cols } = computeGridShape(jobs.length);
  const hidden = Math.max(0, (jobsTotal ?? jobs.length) - jobs.length);

  return (
    <div data-testid="job-wall" className="flex h-full min-h-0 flex-col">
      <div
        data-testid="wallboard-grid"
        className={`grid min-h-0 flex-1 gap-[0.5rem] ${jobs.length === 1 ? 'justify-items-center' : ''}`}
        style={{
          gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
          gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        }}
      >
        {jobs.map(job => (
          <JobTile
            key={job.wo_number}
            job={job}
            tier={tier}
            flash={flashKeys.has(`job:${job.wo_number}:down`) || flashKeys.has(`job:${job.wo_number}:blocked`)}
            extraMinutes={extraMinutes}
            widthCap={jobs.length === 1}
          />
        ))}
      </div>
      {hidden > 0 && (
        <p className="mt-[0.5rem] shrink-0 text-center text-[1.25rem] leading-tight text-[#8b98a9]">
          +{hidden} more work orders
        </p>
      )}
    </div>
  );
}
