/**
 * TODAY KPI bar — the 102px (6.375rem) footer panel (design handoff
 * 2026-07-22, zone 4).
 *
 * Lead cell: blue TODAY eyebrow over the live Central date. Six equal
 * centered cells split by hairline dividers: OPS DONE · PIECES · ON CLOCK
 * (green) · LABOR HRS (1 decimal) · RECEIPTS · SCRAP EVENTS (amber when
 * > 0). A null today block renders em-dash values — the bar never
 * disappears (fixed geography).
 */

import React from 'react';
import type { WallboardToday } from '../../types/wallboard';
import { formatInCentralTime } from '../../utils/centralTime';
import { FD } from './wallboardTokens';

function KpiCell({ label, value, color, last }: { label: string; value: string; color: string; last?: boolean }) {
  return (
    <div
      className="flex flex-1 flex-col items-center justify-center gap-[0.4375rem]"
      style={last ? undefined : { borderRight: `0.0625rem solid ${FD.line}` }}
    >
      <span className="text-[0.875rem] font-semibold tracking-[0.18em]" style={{ color: FD.mute }}>
        {label}
      </span>
      <span className="text-[2.625rem] font-extrabold leading-none" style={{ color }}>
        {value}
      </span>
    </div>
  );
}

export default function TodayKpiBar({ today, now }: { today: WallboardToday | null; now: Date }) {
  const dateStr = formatInCentralTime(now, { weekday: 'short', month: 'short', day: 'numeric' })
    .replace(/,/g, '')
    .toUpperCase();
  const value = (n: number | null | undefined): string => (n === null || n === undefined ? '—' : String(n));
  const scrap = today?.scrap_events ?? null;

  return (
    <footer
      data-testid="today-kpis"
      className="flex h-[6.375rem] flex-none items-stretch rounded-[0.25rem]"
      style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}` }}
    >
      <div
        className="flex w-[11.875rem] flex-none flex-col justify-center gap-[0.375rem] px-[1.625rem]"
        style={{ borderRight: `0.0625rem solid ${FD.line}` }}
      >
        <span className="text-[1.0625rem] font-bold tracking-[0.22em]" style={{ color: FD.blue }}>
          TODAY
        </span>
        <span className="text-[0.8125rem] font-medium tracking-[0.12em]" style={{ color: FD.mute }}>
          {dateStr}
        </span>
      </div>
      <KpiCell label="OPS DONE" value={value(today?.ops_completed)} color={FD.ink} />
      <KpiCell label="PIECES" value={value(today?.pieces_completed)} color={FD.ink} />
      <KpiCell label="ON CLOCK" value={value(today?.operators_on_clock)} color={today !== null ? FD.green : FD.ink} />
      <KpiCell
        label="LABOR HRS"
        value={today !== null && today.hours_logged !== null ? today.hours_logged.toFixed(1) : '—'}
        color={FD.ink}
      />
      <KpiCell label="RECEIPTS" value={value(today?.receipts)} color={FD.ink} />
      <KpiCell
        label="SCRAP EVENTS"
        value={value(scrap)}
        color={scrap !== null && scrap > 0 ? FD.amber : FD.ink}
        last
      />
    </footer>
  );
}
