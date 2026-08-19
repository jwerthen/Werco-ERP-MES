/**
 * The 4-col × 3-row work-order grid + the strip beneath it (design handoff
 * 2026-07-22, zone 2; anchor-row + rotating-field 2026-08-19).
 *
 * ROW 1 is the ANCHOR — `jobs.slice(0, 4)`, live from the freshest payload,
 * never paged. ROWS 2-3 are the FIELD — an 8-wide window over `jobs.slice(4)`
 * that flips on a 22s dwell. Anchor + field page 0 is exactly `jobs[0..11]`,
 * i.e. today's board card for card, so a job can never be shown twice or
 * silently skipped. Severity order is the server's; the client NEVER re-sorts.
 * Every one of the 12 cells is always rendered — a slot with no job is a plain
 * background cell, which is what holds the grid geometry fixed.
 *
 * All cycle STATE lives in `Wallboard.tsx` / `useWoCycle`, never here: the two
 * early returns below sit ABOVE where any hook could run, and a hook after them
 * fails `react-hooks/rules-of-hooks` under CI's `--max-warnings=0`.
 *
 * The strip is unchanged in height, chrome and slot. It carries the walk-up
 * copy on the left and — ONLY while the board is actually cycling — a segmented
 * page bar on the right. The bar is NON-TEXT on purpose: at 5m a viewer reads
 * how many segments exist and which is lit without resolving any glyphs, and it
 * changes state discretely once per dwell so it costs the motion budget
 * nothing. Empty jobs → a full-zone NO OPEN WORK ORDERS state and no strip; a
 * payload with `jobs` missing entirely (pre-job-wall backend) degrades to a
 * BOARD DATA UNAVAILABLE state instead of crashing. Nothing scrolls, ever.
 */

import React from 'react';
import type { WallboardBlockedWorkOrder, WallboardJob, WallboardWorkCenter } from '../../types/wallboard';
import WoCard from './WoCard';
import { FD } from './wallboardTokens';
import { ANCHOR_SLOTS, stripCopy } from '../../utils/wallboardLayout';

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
  anchorJobs,
  fieldJobs,
  pageIndex,
  pages,
  workCenters,
  blockedWos,
  extraMinutes,
}: {
  /** The full delivered list — used ONLY for the two early returns and the
   *  strip's delivered count. The rendered cells come from anchor + field. */
  jobs: WallboardJob[] | null;
  jobsTotal: number | null;
  /** Grid row 1, pinned and live (`useWoCycle`). */
  anchorJobs: WallboardJob[];
  /** Grid rows 2-3, exactly 8 entries; `null` = plain cell (`useWoCycle`). */
  fieldJobs: (WallboardJob | null)[];
  pageIndex: number;
  pages: number;
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

  // The anchor is padded to a full row so the field always starts at row 2 —
  // the pinned/rotating boundary is a CONSTANT geometry edge the viewer learns
  // by watching, never a data-dependent divider. (No divider line is drawn.)
  const anchorRow: (WallboardJob | null)[] = [
    ...anchorJobs.slice(0, ANCHOR_SLOTS),
    ...Array.from({ length: Math.max(0, ANCHOR_SLOTS - anchorJobs.length) }, () => null),
  ];
  const cells: (WallboardJob | null)[] = [...anchorRow, ...fieldJobs];

  const { text, showPageBar } = stripCopy({
    pageIndex,
    pages,
    delivered: jobs.length,
    total: jobsTotal ?? jobs.length,
  });

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-[0.625rem]">
      <div className="grid min-h-0 flex-1 grid-cols-4 grid-rows-3 gap-[0.8125rem]" data-testid="wo-grid">
        {cells.map((job, index) => {
          // Cards stay keyed by wo_number so React REUSES the four anchor DOM
          // nodes across a field flip — that is what keeps a DOWN card's 1.6s
          // fdPulse from resetting phase, and it is the whole reason this design
          // spends none of the alarm channel.
          if (!job) return <div key={`wo-cell-${index}`} />;
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
      {text === null ? null : (
        <div
          data-testid="wo-overflow-strip"
          className={`flex h-[2.375rem] flex-none items-center rounded-[0.25rem] ${
            showPageBar ? 'justify-between px-[1rem]' : 'justify-center'
          }`}
          style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}` }}
        >
          <span className="text-[0.9375rem] font-semibold tracking-[0.14em]" style={{ color: FD.mute }}>
            {text}
          </span>
          {showPageBar ? (
            <div className="flex shrink-0 items-center gap-[0.25rem]" data-testid="wo-page-bar">
              {Array.from({ length: pages }, (_, i) => (
                <div
                  key={`page-seg-${i}`}
                  data-testid={i === pageIndex ? 'wo-page-seg-on' : 'wo-page-seg-off'}
                  className="h-[0.5rem] w-[2.5rem] rounded-[0.0625rem]"
                  style={{ background: i === pageIndex ? FD.ink : FD.line }}
                />
              ))}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
