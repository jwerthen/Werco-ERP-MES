/**
 * LATE — OLDEST FIRST rail panel — amber top accent, flex-1 (design handoff
 * 2026-07-22, zone 3, panel 2).
 *
 * Up to 6 rows from late_wos (server order = oldest first), each a 44px
 * amber days column + WO + part, space-evenly so the panel fills its slot
 * at any row count. "+N MORE" against the TRUE uncapped late total. Zero
 * late → a single dim-green ON TIME line; the panel keeps its slot (fixed
 * geography — panels never disappear).
 */

import React from 'react';
import type { WallboardLateWorkOrder } from '../../types/wallboard';
import { FD } from './wallboardTokens';

const LATE_ROW_CAP = 6;

export default function LatePanel({ lateWos, lateTotal }: { lateWos: WallboardLateWorkOrder[]; lateTotal: number }) {
  const rows = lateWos.slice(0, LATE_ROW_CAP);
  const hidden = Math.max(0, lateTotal - rows.length);

  return (
    <section
      data-testid="late-panel"
      className="flex min-h-0 flex-1 flex-col gap-[0.6875rem] rounded-[0.25rem] px-[1.25rem] py-[1rem]"
      style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}`, borderTop: `0.1875rem solid ${FD.amber}` }}
    >
      <div className="flex items-center justify-between gap-[0.625rem]">
        <span className="text-[0.9375rem] font-semibold tracking-[0.18em]" style={{ color: FD.mute }}>
          LATE — OLDEST FIRST
        </span>
        <span
          data-testid="late-total"
          className="text-[2.75rem] font-extrabold leading-none"
          style={{ color: lateTotal > 0 ? FD.amber : FD.mute }}
        >
          {lateTotal}
        </span>
      </div>

      {lateTotal === 0 ? (
        <div className="flex min-h-0 flex-1 items-center justify-center">
          <span
            className="text-[1.0625rem] font-semibold tracking-[0.08em]"
            style={{ color: FD.green, opacity: 0.7 }}
          >
            ON TIME — NOTHING LATE
          </span>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col justify-evenly text-[1.0625rem]">
          {rows.map(wo => (
            <div key={wo.wo_number} className="flex items-baseline gap-[0.875rem]">
              <span className="w-[2.75rem] shrink-0 font-bold" style={{ color: FD.amber }}>
                {wo.days_late}D
              </span>
              <span className="shrink-0 font-semibold" style={{ color: FD.ink }}>
                {wo.wo_number}
              </span>
              <span className="min-w-0 truncate" style={{ color: FD.mute }}>
                {wo.part_number ?? ''}
              </span>
            </div>
          ))}
          {hidden > 0 && (
            <div className="text-[0.9375rem] tracking-[0.08em]" style={{ color: FD.mute }}>
              +{hidden} MORE
            </div>
          )}
        </div>
      )}
    </section>
  );
}
