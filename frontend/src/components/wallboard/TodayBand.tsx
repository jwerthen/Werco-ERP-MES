/**
 * Z4 TODAY / 30-DAY BAND (9%h) — live today-so-far pulse (six hairline-divided
 * cells, Central-midnight reset) plus the relocated PLANT 30d KPI cluster
 * behind a heavier 2-hairline rule. The 30d cluster stays company-wide even on
 * a dept TV (honest labeling). Threshold banding is quarantined to 0.75rem
 * swatches beside each value — never full-value color fills. Missing blocks
 * (old backend) render em-dash values; the band itself never disappears.
 */

import React from 'react';
import type { WallboardKpiStrip, WallboardToday } from '../../types/wallboard';

const KPI_GREEN = '#3fb950';
const KPI_AMBER = '#d29922';
const KPI_RED = '#f04438';
const KPI_MUTE = '#8b98a9';

export function pctColor(value: number | null, goodHigh: boolean): string {
  if (value === null) return KPI_MUTE;
  if (goodHigh) return value >= 95 ? KPI_GREEN : value >= 85 ? KPI_AMBER : KPI_RED;
  // Lower-is-better (scrap %).
  return value <= 2 ? KPI_GREEN : value <= 5 ? KPI_AMBER : KPI_RED;
}

export function kpiPct(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(1)}%`;
}

export function kpiNum(value: number | null, digits = 0, suffix = ''): string {
  return value === null ? '—' : `${value.toFixed(digits)}${suffix}`;
}

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
      <span className="whitespace-nowrap text-[2.75rem] font-bold leading-none tabular-nums text-[#f0f4f9]">
        {value}
      </span>
    </div>
  );
}

function KpiCell({ label, value, swatchColor }: { label: string; value: string; swatchColor: string }) {
  return (
    <div className="flex min-w-0 items-center gap-[0.5rem] overflow-hidden">
      {/* 0.75rem threshold swatch — banding lives here, not on the value. */}
      <span aria-hidden="true" className="h-[0.75rem] w-[0.75rem] shrink-0" style={{ backgroundColor: swatchColor }} />
      <span className="whitespace-nowrap text-[1.25rem] uppercase leading-none tracking-widest text-[#8b98a9]">
        {label}
      </span>
      <span className="whitespace-nowrap text-[2.25rem] font-bold leading-none tabular-nums text-[#f0f4f9]">
        {value}
      </span>
    </div>
  );
}

export default function TodayBand({ today, kpis }: { today: WallboardToday | null; kpis: WallboardKpiStrip | null }) {
  const cell = (value: number | null | undefined, digits = 0): string =>
    value === null || value === undefined ? '—' : value.toFixed(digits);

  return (
    <div
      data-testid="today-band"
      className="flex h-full items-stretch overflow-hidden border-t-[0.0625rem] border-[#243042] bg-[#10151d]"
    >
      {/* TODAY — live, Central-midnight reset. */}
      <div className="flex min-w-0 basis-[60%] items-stretch overflow-hidden py-[0.5rem]">
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

      {/* Heavier 2-hairline vertical rule quarantines the 30d cluster. */}
      <div
        aria-hidden="true"
        className="w-[0.375rem] shrink-0 border-l-[0.0625rem] border-r-[0.0625rem] border-[#243042]"
      />

      {/* PLANT 30d — explicitly company-wide, even on a dept TV. */}
      <div
        data-testid="wallboard-kpi-strip"
        className="flex min-w-0 basis-[40%] items-center justify-evenly gap-[1rem] overflow-hidden px-[1rem] py-[0.5rem]"
      >
        <span className="shrink-0 whitespace-nowrap text-[1.25rem] font-bold uppercase leading-none tracking-widest text-[#8b98a9]">
          Plant 30d
        </span>
        <KpiCell
          label="OTD"
          value={kpiPct(kpis?.otd_ship_pct_30d ?? null)}
          swatchColor={pctColor(kpis?.otd_ship_pct_30d ?? null, true)}
        />
        <KpiCell
          label="FPY"
          value={kpiPct(kpis?.fpy_pct_30d ?? null)}
          swatchColor={pctColor(kpis?.fpy_pct_30d ?? null, true)}
        />
        <KpiCell
          label="Scrap"
          value={kpiPct(kpis?.scrap_pct_30d ?? null)}
          swatchColor={pctColor(kpis?.scrap_pct_30d ?? null, false)}
        />
        <span className="whitespace-nowrap text-[1.75rem] leading-none text-[#f0f4f9]">
          <span className="text-[1.25rem] uppercase tracking-widest text-[#8b98a9]">WIP </span>
          <span className="font-bold tabular-nums">{kpiNum(kpis?.open_wip_count ?? null)}</span>
          <span className="text-[#8b98a9]"> · </span>
          <span className="font-bold tabular-nums">{kpiNum(kpis?.avg_wip_age_days ?? null, 1, 'd')}</span>
          <span className="text-[1.25rem] uppercase tracking-widest text-[#8b98a9]"> avg</span>
        </span>
      </div>
    </div>
  );
}
