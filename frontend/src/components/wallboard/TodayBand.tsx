/**
 * Z4 TODAY BAND (9%h) — live today-so-far pulse: six hairline-divided cells,
 * Central-midnight reset, stretched across the full band width. (The PLANT
 * 30d KPI cluster that used to share this band was removed on owner feedback
 * 2026-07-15.) A missing `today` block (old backend) renders em-dash values;
 * the band itself never disappears.
 */

import React from 'react';
import type { WallboardToday } from '../../types/wallboard';

function TodayCell({ label, value, first }: { label: string; value: string; first?: boolean }) {
  return (
    <div
      className={`flex min-w-0 flex-1 flex-col items-center justify-center gap-[0.125rem] overflow-hidden px-[0.5rem] ${
        first ? '' : 'border-l-[0.0625rem] border-[#243042]'
      }`}
    >
      <span className="whitespace-nowrap text-[1.25rem] uppercase leading-none tracking-widest text-[#8b98a9]">
        {label}
      </span>
      <span className="whitespace-nowrap text-[3.5rem] font-bold leading-none tabular-nums text-[#f0f4f9]">
        {value}
      </span>
    </div>
  );
}

export default function TodayBand({ today }: { today: WallboardToday | null }) {
  const cell = (value: number | null | undefined, digits = 0): string =>
    value === null || value === undefined ? '—' : value.toFixed(digits);

  return (
    <div
      data-testid="today-band"
      className="flex h-full items-stretch overflow-hidden border-t-[0.0625rem] border-[#243042] bg-[#10151d]"
    >
      {/* TODAY — live, Central-midnight reset. */}
      <div className="flex min-w-0 flex-1 items-stretch overflow-hidden py-[0.5rem]">
        <div className="flex shrink-0 items-center px-[0.75rem]">
          <span className="text-[1.25rem] font-bold uppercase leading-none tracking-widest text-[#8b98a9]">Today</span>
        </div>
        <TodayCell first label="Ops Done" value={cell(today?.ops_completed)} />
        <TodayCell label="Pieces" value={cell(today?.pieces_completed)} />
        <TodayCell label="On Clock" value={cell(today?.operators_on_clock)} />
        <TodayCell label="Hrs" value={cell(today?.hours_logged, 1)} />
        <TodayCell label="Receipts" value={cell(today?.receipts)} />
        <TodayCell label="Scrap Evt" value={cell(today?.scrap_events)} />
      </div>
    </div>
  );
}
