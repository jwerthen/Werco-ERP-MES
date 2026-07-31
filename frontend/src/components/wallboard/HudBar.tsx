/**
 * HUD command bar — the 86px (5.375rem) header of the Foundry TV wallboard
 * (design handoff 2026-07-22, zone 1).
 *
 * Grid 1fr/auto/1fr: logo + board identity left, the three alert chips
 * (DOWN / BLOCKED / LATE) center, sync status + Central wall clock right.
 * A zero-count chip keeps its exact geometry and dims in place (slate text,
 * hairline edge, no glow). The ONLY animation on the whole board is fdPulse
 * on DOWN dots, and only while down > 0. Sync escalation is steady, never
 * flashing: SYNC OK green → SYNC STALE amber (1+ failed polls) → SYNC LOST
 * red (>=4). All clock/updated times are Central, per the repo invariant.
 */

import React from 'react';
import { formatInCentralTime, getCentralMinutesOfDay } from '../../utils/centralTime';
import { titleCaseDept } from '../../utils/wallboardLayout';
import { FD } from './wallboardTokens';

const NOON_CENTRAL_MINUTES = 12 * 60;

/**
 * Central wall clock split into digits + meridiem so the meridiem can render
 * as its own smaller muted span (or not at all in 24h mode). The meridiem is
 * derived from Central minutes-of-day rather than parsed out of the Intl
 * string, which may join it with a narrow no-break space.
 */
export function formatWallClock(
  value: Date,
  clock24h: boolean,
  withSeconds: boolean
): { time: string; meridiem: string | null } {
  const options: Intl.DateTimeFormatOptions = clock24h
    ? { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
    : { hour: 'numeric', minute: '2-digit', hour12: true };
  if (withSeconds) options.second = '2-digit';
  const time = formatInCentralTime(value, options).replace(/\s*[AP]M$/i, '');
  const meridiem = clock24h ? null : getCentralMinutesOfDay(value) >= NOON_CENTRAL_MINUTES ? 'PM' : 'AM';
  return { time, meridiem };
}

/** Exact chip tints from the handoff (status color at ~10% bg / ~40% edge). */
const CHIP_SPECS = {
  down: {
    color: FD.red,
    bg: 'rgba(240,68,56,0.10)',
    edge: 'rgba(240,68,56,0.40)',
    glow: '0 0 0.625rem rgba(240,68,56,0.9)',
  },
  blocked: {
    color: FD.blockedOrange,
    bg: 'rgba(234,125,44,0.09)',
    edge: 'rgba(234,125,44,0.38)',
    glow: '0 0 0.5rem rgba(234,125,44,0.7)',
  },
  late: {
    color: FD.amber,
    bg: 'rgba(210,153,34,0.09)',
    edge: 'rgba(210,153,34,0.38)',
    glow: '0 0 0.5rem rgba(210,153,34,0.7)',
  },
} as const;

const SYNC_SPECS = [
  { label: 'SYNC OK', color: FD.green, glow: '0 0 0.5rem rgba(63,185,80,0.8)' },
  { label: 'SYNC STALE', color: FD.amber, glow: '0 0 0.5rem rgba(210,153,34,0.8)' },
  { label: 'SYNC LOST', color: FD.red, glow: '0 0 0.5rem rgba(240,68,56,0.8)' },
] as const;

function AlertChip({ tone, label, count, pulse }: { tone: keyof typeof CHIP_SPECS; label: string; count: number; pulse?: boolean }) {
  const spec = CHIP_SPECS[tone];
  const active = count > 0;
  const color = active ? spec.color : FD.mute;
  return (
    <div
      data-testid={`hud-chip-${tone}`}
      className="flex items-center gap-[0.75rem] rounded-[0.1875rem] px-[1.25rem] py-[0.6875rem]"
      style={{
        background: active ? spec.bg : 'transparent',
        border: `0.0625rem solid ${active ? spec.edge : FD.line}`,
      }}
    >
      <span
        className="h-[0.5625rem] w-[0.5625rem] shrink-0 rounded-full"
        style={{
          background: color,
          boxShadow: active ? spec.glow : 'none',
          animation: active && pulse ? 'fdPulse 1.6s ease-in-out infinite' : 'none',
        }}
      />
      <span className="text-[2rem] font-extrabold leading-none" style={{ color }}>
        {count}
      </span>
      <span className="text-[1rem] font-semibold tracking-[0.14em]" style={{ color }}>
        {label}
      </span>
    </div>
  );
}

export default function HudBar({
  dept,
  downCount,
  blockedCount,
  lateCount,
  offlineLevel,
  lastUpdated,
  now,
  clock24h,
  clockSeconds,
}: {
  dept: string | null;
  downCount: number;
  blockedCount: number;
  lateCount: number;
  /** 0 healthy · 1 = 1+ failed polls (STALE) · 2 = >=4 failed polls (LOST). */
  offlineLevel: 0 | 1 | 2;
  lastUpdated: Date | null;
  now: Date;
  clock24h: boolean;
  clockSeconds: boolean;
}) {
  const sync = SYNC_SPECS[offlineLevel];
  const clock = formatWallClock(now, clock24h, clockSeconds);
  const updated = lastUpdated ? formatWallClock(lastUpdated, clock24h, false) : null;
  const scope = dept ? titleCaseDept(dept).toUpperCase() : 'ALL WORK CENTERS';

  return (
    <header
      className="grid h-[5.375rem] flex-none grid-cols-[1fr_auto_1fr] items-center gap-[1.25rem] rounded-[0.25rem] px-[1.625rem]"
      style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}` }}
    >
      <div className="flex min-w-0 items-center gap-[1.25rem]">
        <img src="/Werco_Logo_white.png" alt="Werco Manufacturing" className="block h-[2.375rem] w-auto" />
        <div className="h-[2.75rem] w-[0.0625rem] shrink-0" style={{ background: FD.line }} />
        <div className="flex min-w-0 flex-col gap-[0.3125rem]">
          <span className="text-[1.0625rem] font-semibold tracking-[0.2em]" style={{ color: FD.ink }}>
            SHOP FLOOR
          </span>
          <span
            data-testid="hud-scope"
            className="truncate text-[0.75rem] font-medium tracking-[0.18em]"
            style={{ color: FD.mute }}
          >
            LIVE WALLBOARD // {scope}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-[0.75rem]">
        <AlertChip tone="down" label="DOWN" count={downCount} pulse />
        <AlertChip tone="blocked" label="BLOCKED" count={blockedCount} />
        <AlertChip tone="late" label="LATE" count={lateCount} />
      </div>

      <div className="flex items-center justify-end gap-[1.375rem]">
        <div className="flex flex-col items-end gap-[0.375rem]">
          <span
            data-testid="sync-status"
            data-offline-level={offlineLevel}
            className="flex items-center gap-[0.5rem] text-[0.8125rem] font-semibold tracking-[0.14em]"
            style={{ color: sync.color }}
          >
            <span
              className="h-[0.4375rem] w-[0.4375rem] rounded-full"
              style={{ background: sync.color, boxShadow: sync.glow }}
            />
            {sync.label}
          </span>
          <span className="text-[0.8125rem] font-medium tracking-[0.12em]" style={{ color: FD.mute }}>
            UPDATED {updated ? `${updated.time}${updated.meridiem ? ` ${updated.meridiem}` : ''}` : '—'}
          </span>
        </div>
        <div className="h-[2.75rem] w-[0.0625rem] shrink-0" style={{ background: FD.line }} />
        <div className="flex items-baseline gap-[0.5rem]">
          <span
            className="text-[2.875rem] font-bold leading-none tracking-[-0.01em]"
            style={{ color: FD.ink }}
            data-testid="hud-clock"
          >
            {clock.time}
          </span>
          {clock.meridiem && (
            <span className="text-[1.25rem] font-semibold" style={{ color: FD.mute }}>
              {clock.meridiem}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
