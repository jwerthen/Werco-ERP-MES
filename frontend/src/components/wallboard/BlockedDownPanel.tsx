/**
 * BLOCKED / DOWN rail panel — blocked-orange top accent, fixed height
 * (design handoff 2026-07-22, zone 3, panel 3).
 *
 * Split header: BLOCKED count (blocked-orange) · hairline divider · DOWN
 * count (red). Counts dim to faint at zero but NEVER disappear (fixed
 * geography). Up to 4 rows, DOWN work centers first (payload order, live
 * minutes tick between polls) then BLOCKED WOs oldest-first (blocked_wos
 * server order); "+N MORE" against the true totals. All zero → a single
 * dim-green NOTHING BLOCKED OR DOWN line in the rows slot.
 */

import React from 'react';
import type { WallboardBlockedWorkOrder, WallboardWorkCenter } from '../../types/wallboard';
import { blockerLabel, formatAgeHours, formatDownDuration } from '../../utils/wallboardLayout';
import { FD } from './wallboardTokens';

const ROW_CAP = 4;

interface Row {
  key: string;
  duration: string;
  name: string;
  reason: string;
  durationColor: string;
  reasonColor: string;
}

export default function BlockedDownPanel({
  workCenters,
  blockedWos,
  blockedTotal,
  downTotal,
  extraMinutes,
}: {
  workCenters: WallboardWorkCenter[];
  blockedWos: WallboardBlockedWorkOrder[];
  blockedTotal: number;
  downTotal: number;
  extraMinutes: number;
}) {
  const downRows: Row[] = workCenters
    .filter(wc => wc.down !== null)
    .map(wc => ({
      key: `down:${wc.id}`,
      duration: formatDownDuration(wc.down!.minutes + extraMinutes).toUpperCase(),
      // Machine identity name-first, matching the card's machine row.
      name: wc.name || wc.code || '',
      reason: blockerLabel(wc.down!.category).toUpperCase(),
      durationColor: FD.red,
      reasonColor: FD.red,
    }));
  const blockedRows: Row[] = blockedWos.map(wo => ({
    key: `blocked:${wo.wo_number}`,
    duration: formatAgeHours(wo.age_hours).toUpperCase(),
    name: wo.wo_number,
    reason: blockerLabel(wo.category).toUpperCase(),
    durationColor: FD.blockedOrange,
    reasonColor: FD.body,
  }));

  const rows = [...downRows, ...blockedRows].slice(0, ROW_CAP);
  const hidden = Math.max(0, blockedTotal + downTotal - rows.length);
  const allClear = blockedTotal === 0 && downTotal === 0;

  return (
    <section
      data-testid="blocked-down-panel"
      className="flex flex-none flex-col gap-[0.75rem] rounded-[0.25rem] px-[1.25rem] py-[1rem]"
      style={{
        background: FD.panel,
        border: `0.0625rem solid ${FD.line}`,
        borderTop: `0.1875rem solid ${FD.blockedOrange}`,
      }}
    >
      <div className="flex items-center gap-[1.125rem]">
        <div className="flex flex-1 items-center justify-between gap-[0.625rem]">
          <span className="text-[0.9375rem] font-semibold tracking-[0.18em]" style={{ color: FD.mute }}>
            BLOCKED
          </span>
          <span
            data-testid="blocked-total"
            className="text-[2.25rem] font-extrabold leading-none"
            style={{ color: blockedTotal > 0 ? FD.blockedOrange : FD.faint }}
          >
            {blockedTotal}
          </span>
        </div>
        <div className="h-[2.125rem] w-[0.0625rem] shrink-0" style={{ background: FD.line }} />
        <div className="flex flex-1 items-center justify-between gap-[0.625rem]">
          <span className="text-[0.9375rem] font-semibold tracking-[0.18em]" style={{ color: FD.mute }}>
            DOWN
          </span>
          <span
            data-testid="down-total"
            className="text-[2.25rem] font-extrabold leading-none"
            style={{ color: downTotal > 0 ? FD.red : FD.faint }}
          >
            {downTotal}
          </span>
        </div>
      </div>

      {allClear ? (
        <div className="flex items-center justify-center py-[0.5rem]">
          <span className="text-[1rem] font-semibold tracking-[0.08em]" style={{ color: FD.green, opacity: 0.7 }}>
            NOTHING BLOCKED OR DOWN
          </span>
        </div>
      ) : (
        <div className="flex flex-col gap-[0.5rem] text-[1rem]">
          {rows.map(row => (
            <div key={row.key} className="flex items-baseline gap-[0.75rem]">
              <span className="w-[3.875rem] shrink-0 font-bold" style={{ color: row.durationColor }}>
                {row.duration}
              </span>
              <span className="min-w-0 truncate font-semibold" style={{ color: FD.ink }}>
                {row.name}
              </span>
              <span className="ml-auto shrink-0 whitespace-nowrap" style={{ color: row.reasonColor }}>
                {row.reason}
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
