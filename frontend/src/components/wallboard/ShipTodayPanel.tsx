/**
 * SHIP TODAY rail panel — blue top accent, fixed height (design handoff
 * 2026-07-22, zone 3, panel 1).
 *
 * Fraction color escalates on the shop's Central wall clock: mute when
 * nothing is due, green once shipped >= due, amber when behind before
 * 12:00 Central, red when still behind at/after noon. Up to 2 due-today
 * rows with "+N MORE TODAY"; a zero-due day shows the next promise date
 * instead; a null ship block renders em-dash values in the same geometry.
 */

import React from 'react';
import type { WallboardShip } from '../../types/wallboard';
import { formatInCentralTime } from '../../utils/centralTime';
import { FD } from './wallboardTokens';

const SHIP_ROW_CAP = 2;
const NOON_CENTRAL_MINUTES = 12 * 60;

/** "2026-07-25" → "SAT JUL 25" (Central-safe date-only formatting). */
function formatDayShort(value: string): string {
  return formatInCentralTime(value, { weekday: 'short', month: 'short', day: 'numeric' })
    .replace(/,/g, '')
    .toUpperCase();
}

export default function ShipTodayPanel({
  ship,
  centralMinutes,
}: {
  ship: WallboardShip | null;
  /** Minutes since Central midnight — drives the past-noon escalation. */
  centralMinutes: number;
}) {
  const due = ship?.due_today ?? null;
  const shipped = ship?.shipped_today ?? null;

  let fractionColor: string = FD.mute;
  if (ship !== null && due !== null && shipped !== null && due > 0) {
    if (shipped >= due) fractionColor = FD.green;
    else fractionColor = centralMinutes >= NOON_CENTRAL_MINUTES ? FD.red : FD.amber;
  }

  const rows = ship?.due_today_rows.slice(0, SHIP_ROW_CAP) ?? [];
  const moreToday = ship !== null && due !== null && shipped !== null ? Math.max(0, due - shipped - rows.length) : 0;

  return (
    <section
      data-testid="ship-panel"
      className="flex flex-none flex-col gap-[0.75rem] rounded-[0.25rem] px-[1.25rem] py-[1rem]"
      style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}`, borderTop: `0.1875rem solid ${FD.blue}` }}
    >
      <div className="flex items-center justify-between gap-[0.625rem]">
        <span className="text-[0.9375rem] font-semibold tracking-[0.18em]" style={{ color: FD.mute }}>
          SHIP TODAY
        </span>
        <span className="text-[2.75rem] font-extrabold leading-none" style={{ color: fractionColor }}>
          {shipped ?? '—'}
          <span className="font-semibold" style={{ color: FD.faint }}>
            /{due ?? '—'}
          </span>
        </span>
      </div>

      <div className="flex flex-col gap-[0.4375rem] text-[1.0625rem]">
        {ship === null ? (
          <div style={{ color: FD.mute }}>—</div>
        ) : due === 0 ? (
          <div className="truncate" style={{ color: FD.mute }}>
            {ship.next_due_date
              ? `NEXT DUE ${formatDayShort(ship.next_due_date)} (${ship.next_due_count} WOS)`
              : 'NONE DUE'}
          </div>
        ) : (
          <>
            {rows.map(row => (
              <div key={row.wo_number} className="flex justify-between gap-[0.625rem]">
                <span className="min-w-0 truncate font-semibold" style={{ color: FD.ink }}>
                  {row.wo_number}{' '}
                  <span className="font-medium" style={{ color: FD.mute }}>
                    · {row.part_number ?? ''}
                  </span>
                </span>
                <span className="shrink-0" style={{ color: FD.body }}>
                  {row.qty_remaining} LEFT
                </span>
              </div>
            ))}
            {moreToday > 0 && (
              <div className="text-[0.9375rem] tracking-[0.08em]" style={{ color: FD.mute }}>
                +{moreToday} MORE TODAY
              </div>
            )}
          </>
        )}
      </div>

      <div
        className="flex items-center justify-between pt-[0.625rem]"
        style={{ borderTop: `0.0625rem solid ${FD.line}` }}
      >
        <span className="text-[0.875rem] font-semibold tracking-[0.16em]" style={{ color: FD.mute }}>
          THIS WEEK
        </span>
        <span className="text-[1.375rem] font-bold" style={{ color: FD.ink }}>
          {ship?.due_this_week ?? '—'}
        </span>
      </div>
    </section>
  );
}
