/**
 * Z1 HEADER (9%h) — wordmark + dept chip | computed shop-state hero | updated/clock.
 *
 * The hero is the board's single headline truth: one computed sentence at
 * 4.5rem with a heartbeat status dot. The dot's 2s ease pulse is the liveness
 * cue — it FREEZES while offline (a stopped heartbeat = stale data). The
 * offline chip itself is STEADY (amber after 1 failed poll, red fill after 4);
 * flashing is reserved for newly-raised events only, never for steady state.
 */

import React from 'react';
import { formatCentralTime } from '../../utils/centralTime';
import { titleCaseDept } from '../../utils/wallboardLayout';
import { WB } from './wallboardTokens';

export interface ShopStateTotals {
  down: number;
  blocked: number;
  late: number;
}

export function shopStateSentence(totals: ShopStateTotals, offShift: boolean): { text: string; color: string } {
  if (totals.down === 0 && totals.blocked === 0 && totals.late === 0) {
    if (offShift) return { text: 'OFF SHIFT', color: WB.slate };
    return { text: 'ALL SYSTEMS NORMAL', color: WB.green };
  }
  const segments: string[] = [];
  if (totals.down > 0) segments.push(`${totals.down} DOWN`);
  if (totals.blocked > 0) segments.push(`${totals.blocked} BLOCKED`);
  if (totals.late > 0) segments.push(`${totals.late} LATE`);
  const color = totals.down > 0 ? WB.red : totals.blocked > 0 ? WB.orange : WB.amber;
  return { text: segments.join(' · '), color };
}

interface WallboardHeaderProps {
  dept: string | null;
  totals: ShopStateTotals;
  offShift: boolean;
  hasData: boolean;
  offline: boolean;
  /** 0 = online · 1 = ≥1 failed poll (steady amber) · 2 = ≥4 failed polls (steady red fill). */
  offlineLevel: 0 | 1 | 2;
  lastUpdated: Date | null;
  now: Date;
}

export default function WallboardHeader({
  dept,
  totals,
  offShift,
  hasData,
  offline,
  offlineLevel,
  lastUpdated,
  now,
}: WallboardHeaderProps) {
  const hero = shopStateSentence(totals, offShift);

  return (
    // Side tracks are content-sized and the hero owns the shrinkable middle
    // (minmax(0,1fr) + truncate) — the offline chip can never overlap it.
    <header className="grid h-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-[1.5rem] border-b-[0.0625rem] border-[#243042] px-[0.5rem]">
      {/* Left cluster: wordmark + title-cased dept chip */}
      <div className="flex min-w-0 items-center gap-[1rem]">
        <span className="whitespace-nowrap text-[1.25rem] font-bold uppercase tracking-widest text-[#f0f4f9]">
          Werco<span className="text-[#C8352B]">·</span>Floor
        </span>
        {dept && (
          <span
            data-testid="dept-label"
            className="whitespace-nowrap border-[0.0625rem] border-[#243042] px-[0.75rem] py-[0.125rem] text-[1.5rem] font-semibold leading-tight text-[#f0f4f9]"
          >
            {titleCaseDept(dept)}
          </span>
        )}
      </div>

      {/* Center: the shop-state hero + heartbeat dot */}
      {hasData ? (
        <div data-testid="shop-state-headline" className="flex min-w-0 items-center justify-center gap-[1.25rem]">
          <span
            aria-hidden="true"
            className={`wb-heartbeat h-[1.5rem] w-[1.5rem] shrink-0 rounded-full ${
              offline ? 'wb-heartbeat-frozen' : ''
            }`}
            style={{ backgroundColor: hero.color }}
          />
          <span
            className="wb-num truncate whitespace-nowrap text-[4.5rem] font-bold uppercase leading-none tracking-tight tabular-nums"
            style={{ color: hero.color }}
          >
            {hero.text}
          </span>
        </div>
      ) : (
        <div />
      )}

      {/* Right cluster: offline chip · updated · wall clock */}
      <div className="flex min-w-0 items-center justify-end gap-[1.5rem]">
        {offlineLevel > 0 && (
          <span
            data-testid="offline-banner"
            data-offline-level={offlineLevel}
            className={`whitespace-nowrap px-[1rem] py-[0.375rem] text-[1.25rem] font-bold uppercase tracking-widest text-black ${
              offlineLevel >= 2 ? 'bg-[#f04438]' : 'bg-[#d29922]'
            }`}
          >
            {lastUpdated ? `Offline · ${formatCentralTime(lastUpdated)}` : 'Offline'}
          </span>
        )}
        {/* The chip already carries the as-of time — don't repeat it. */}
        {offlineLevel === 0 && (
          <span data-testid="last-updated" className="whitespace-nowrap text-[1.25rem] tabular-nums text-[#8b98a9]">
            {lastUpdated ? `Updated ${formatCentralTime(lastUpdated)}` : 'Loading…'}
          </span>
        )}
        <span
          data-testid="wall-clock"
          className="whitespace-nowrap text-[2.5rem] font-bold leading-none tabular-nums text-[#f0f4f9]"
        >
          {formatCentralTime(now)}
        </span>
      </div>
    </header>
  );
}
