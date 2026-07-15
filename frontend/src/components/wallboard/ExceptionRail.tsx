/**
 * Z3 EXCEPTION RAIL — four FIXED panels (SHIP 22% / LATE 30% / BLOCKED·DOWN
 * 28% / QUALITY 20%), replacing the old rotating ticker outright. Nothing in
 * the rail rotates, ever: rows are pinned worst-first with a "+N more" count,
 * totals are the TRUE uncapped totals (fallback to list lengths against an
 * old backend), and zero panels dim in place with a green zero-line — every
 * panel keeps its exact slot at all data values (fixed geography).
 *
 * Every exception row leads with a fixed-width 5ch magnitude column (bold,
 * tabular, state-colored) so severity scans vertically at 4m.
 */

import React from 'react';
import type {
  WallboardBlockedWorkOrder,
  WallboardLateWorkOrder,
  WallboardQuality,
  WallboardShip,
  WallboardWorkCenter,
} from '../../types/wallboard';
import { blockerLabel, formatAgeHours, formatDownDuration } from '../../utils/wallboardLayout';
import { WB } from './wallboardTokens';

const LATE_ROW_CAP = 6;
const BLOCKED_DOWN_ROW_CAP = 5;
const SHIP_ROW_CAP = 2;
/** SHIP fraction escalates to red when due-today WOs remain past 12:00 Central. */
const NOON_CENTRAL_MINUTES = 12 * 60;

function PlantTag() {
  return (
    <span className="shrink-0 whitespace-nowrap border-[0.0625rem] border-[#243042] px-[0.5rem] text-[1.25rem] uppercase leading-snug tracking-widest text-[#8b98a9]">
      Plant
    </span>
  );
}

/** Dim-green zero state — the board visibly rewards a clean day, in place. */
function ZeroLine({ text, large }: { text: string; large: boolean }) {
  return (
    <div className="flex flex-1 items-center justify-center overflow-hidden">
      <p
        data-testid="all-clear-line"
        // Wrap, never truncate — a clean-day line that reads "NOTHING BLO…"
        // defeats its own purpose.
        className={`text-center font-bold uppercase leading-tight tracking-wide text-[#3fb950] ${
          large ? 'text-[2.5rem]' : 'text-[1.5rem]'
        }`}
      >
        {text}
      </p>
    </div>
  );
}

function MagnitudeCell({ text, color }: { text: string; color: string }) {
  return (
    <span className="w-[5ch] shrink-0 font-bold tabular-nums" style={{ color }}>
      {text}
    </span>
  );
}

function PanelLabel({ text }: { text: string }) {
  return (
    <span className="whitespace-nowrap text-[1.375rem] uppercase leading-snug tracking-widest text-[#8b98a9]">
      {text}
    </span>
  );
}

// ---- P1 SHIP ----------------------------------------------------------------

/** "2026-07-16" → "Thu, Jul 16" without any timezone shifting (date-only value). */
function formatPromiseDay(iso: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return iso;
  const utc = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return utc.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

function ShipPanel({
  ship,
  hasDept,
  centralMinutes,
}: {
  ship: WallboardShip | null;
  hasDept: boolean;
  centralMinutes: number;
}) {
  const due = ship?.due_today ?? null;
  const shipped = ship?.shipped_today ?? null;
  const behind = ship !== null && due !== null && shipped !== null && due > 0 && shipped < due;
  const pastNoon = centralMinutes >= NOON_CENTRAL_MINUTES;
  const fractionColor = behind && pastNoon ? WB.red : WB.text;

  let subline: { text: string; color: string } | null = null;
  if (ship !== null && due !== null && shipped !== null) {
    if (due === 0) subline = { text: 'NONE DUE', color: WB.muted };
    else if (shipped >= due) subline = { text: 'COMPLETE', color: WB.green };
    else subline = { text: `${due - shipped} TO GO`, color: WB.amber };
  }

  const rows = ship?.due_today_rows.slice(0, SHIP_ROW_CAP) ?? [];
  const hiddenToday = ship !== null && due !== null && shipped !== null ? Math.max(0, due - shipped - rows.length) : 0;

  return (
    <section
      data-testid="ship-panel"
      className="flex h-[22%] flex-col gap-[0.375rem] overflow-hidden border-b-[0.0625rem] border-[#243042] bg-[#141b26] px-[1rem] py-[0.625rem]"
      style={{ borderTop: `0.1875rem solid ${WB.navy}` }}
    >
      <div className="flex items-baseline justify-between gap-[1rem]">
        <span className="flex items-baseline gap-[0.75rem]">
          <PanelLabel text="Ship Today" />
          {hasDept && <PlantTag />}
        </span>
        <span
          className="wb-num whitespace-nowrap text-[4.5rem] font-bold leading-none tabular-nums"
          style={{ color: fractionColor }}
        >
          {shipped ?? '—'} / {due ?? '—'}
        </span>
      </div>
      <p className="truncate text-[1.625rem] leading-tight">
        {subline && (
          <span className="font-bold uppercase" style={{ color: subline.color }}>
            {subline.text}
          </span>
        )}
        <span className="uppercase text-[#8b98a9]">
          {subline ? ' · ' : ''}This Week{' '}
          <span className="font-bold tabular-nums text-[#f0f4f9]">{ship?.due_this_week ?? '—'}</span>
        </span>
      </p>
      {due === 0 && ship?.next_due_date ? (
        <p className="truncate text-[1.375rem] leading-tight text-[#8b98a9]">
          Next due: {formatPromiseDay(ship.next_due_date)} ({ship.next_due_count} WOs)
        </p>
      ) : (
        <>
          {rows.map(row => (
            <p key={row.wo_number} className="truncate text-[1.375rem] leading-tight tabular-nums text-[#f0f4f9]">
              {row.wo_number}
              {row.part_number ? ` · ${row.part_number}` : ''}
            </p>
          ))}
          {hiddenToday > 0 && <p className="text-[1.25rem] leading-tight text-[#8b98a9]">+{hiddenToday} more today</p>}
        </>
      )}
    </section>
  );
}

// ---- P2 LATE ----------------------------------------------------------------

function LatePanel({
  lateWos,
  lateTotal,
  cleanDay,
  flashKeys,
}: {
  lateWos: WallboardLateWorkOrder[];
  lateTotal: number;
  cleanDay: boolean;
  flashKeys: Set<string>;
}) {
  const zero = lateTotal === 0;
  const rows = lateWos.slice(0, LATE_ROW_CAP);
  const hidden = Math.max(0, lateTotal - rows.length);

  return (
    <section
      data-testid="attention-late"
      className={`flex h-[30%] flex-col overflow-hidden border-b-[0.0625rem] border-[#243042] px-[1rem] py-[0.625rem] ${
        zero ? 'bg-[#10151d]' : 'bg-[#141b26]'
      }`}
    >
      {zero ? (
        <ZeroLine text="LATE 0 — ON TIME" large={cleanDay} />
      ) : (
        <>
          <div className="flex items-baseline justify-between gap-[1rem]">
            <PanelLabel text="Late" />
            <span
              data-testid="late-total"
              className="wb-num whitespace-nowrap text-[4rem] font-bold leading-none tabular-nums text-[#d29922]"
            >
              {lateTotal}
            </span>
          </div>
          <div className="mt-[0.375rem] flex min-h-0 flex-col gap-[0.375rem] overflow-hidden">
            {rows.map(wo => (
              <p
                key={wo.wo_number}
                className={`flex items-baseline gap-[0.75rem] truncate text-[1.5rem] leading-tight text-[#f0f4f9] ${
                  flashKeys.has(`late:${wo.wo_number}`) ? 'wb-flash-new' : ''
                }`}
              >
                <MagnitudeCell text={`${wo.days_late}d`} color={WB.amber} />
                <span className="shrink-0 tabular-nums">{wo.wo_number}</span>
                <span className="min-w-0 truncate text-[#8b98a9]">{wo.part_number ?? ''}</span>
              </p>
            ))}
          </div>
          {hidden > 0 && (
            <p className="mt-auto pt-[0.25rem] text-[1.25rem] leading-tight text-[#8b98a9]">+{hidden} more</p>
          )}
        </>
      )}
    </section>
  );
}

// ---- P3 BLOCKED · DOWN -------------------------------------------------------

interface DownRow {
  key: string;
  magnitude: string;
  label: string;
}

function BlockedDownPanel({
  workCenters,
  blockedWos,
  blockedTotal,
  downTotal,
  cleanDay,
  flashKeys,
  extraMinutes,
}: {
  workCenters: WallboardWorkCenter[];
  blockedWos: WallboardBlockedWorkOrder[];
  blockedTotal: number;
  downTotal: number;
  cleanDay: boolean;
  flashKeys: Set<string>;
  extraMinutes: number;
}) {
  const zero = blockedTotal === 0 && downTotal === 0;

  // Down rows first (worst-first by minutes), then blocked WOs (worst-first by age).
  const downRows: (DownRow & { flash: boolean })[] = workCenters
    .filter(wc => wc.down !== null)
    .sort((a, b) => b.down!.minutes - a.down!.minutes)
    .map(wc => ({
      key: `down:${wc.id}:${wc.down!.category}`,
      magnitude: formatDownDuration(wc.down!.minutes + extraMinutes),
      label: `${wc.code ?? wc.name} · ${blockerLabel(wc.down!.category)}`,
      flash: flashKeys.has(`down:${wc.id}:${wc.down!.category}`),
    }));
  const blockedRows = [...blockedWos]
    .sort((a, b) => b.age_hours - a.age_hours)
    .map(wo => ({
      key: `blocked:${wo.wo_number}`,
      magnitude: formatAgeHours(wo.age_hours),
      label: `${wo.wo_number} · ${blockerLabel(wo.category)}`,
      flash: flashKeys.has(`blocked:${wo.wo_number}`),
    }));

  const rows = [
    ...downRows.map(row => ({ ...row, color: WB.red })),
    ...blockedRows.map(row => ({ ...row, color: WB.orange })),
  ].slice(0, BLOCKED_DOWN_ROW_CAP);
  const hidden = Math.max(0, downTotal + blockedTotal - rows.length);

  return (
    <section
      data-testid="attention-blocked-down"
      className={`flex h-[28%] flex-col overflow-hidden border-b-[0.0625rem] border-[#243042] px-[1rem] py-[0.625rem] ${
        zero ? 'bg-[#10151d]' : 'bg-[#141b26]'
      }`}
    >
      {zero ? (
        <ZeroLine text="NOTHING BLOCKED OR DOWN" large={cleanDay} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-[1rem]">
            <div className="flex items-baseline justify-between gap-[0.75rem] overflow-hidden">
              <PanelLabel text="Blocked" />
              <span
                data-testid="blocked-total"
                className="wb-num whitespace-nowrap text-[3.5rem] font-bold leading-none tabular-nums text-[#f0883e]"
              >
                {blockedTotal}
              </span>
            </div>
            {/* The DOWN half never disappears — it dims to slate at zero. */}
            <div className="flex items-baseline justify-between gap-[0.75rem] overflow-hidden border-l-[0.0625rem] border-[#243042] pl-[1rem]">
              <PanelLabel text="Down" />
              <span
                data-testid="down-total"
                className={`wb-num whitespace-nowrap text-[3.5rem] font-bold leading-none tabular-nums ${
                  downTotal > 0 ? 'text-[#f04438]' : 'text-[#5b6878]'
                }`}
              >
                {downTotal}
              </span>
            </div>
          </div>
          <div className="mt-[0.375rem] flex min-h-0 flex-col gap-[0.375rem] overflow-hidden">
            {rows.map(row => (
              <p
                key={row.key}
                className={`flex items-baseline gap-[0.75rem] truncate text-[1.5rem] uppercase leading-tight ${
                  row.flash ? 'wb-flash-new' : ''
                }`}
                style={{ color: row.color }}
              >
                <MagnitudeCell text={row.magnitude} color={row.color} />
                <span className="min-w-0 truncate">{row.label}</span>
              </p>
            ))}
          </div>
          {hidden > 0 && (
            <p className="mt-auto pt-[0.25rem] text-[1.25rem] leading-tight text-[#8b98a9]">+{hidden} more</p>
          )}
        </>
      )}
    </section>
  );
}

// ---- P4 QUALITY ---------------------------------------------------------------

function QualityValue({ value }: { value: number | null }) {
  if (value === null) {
    return <span className="text-[2.75rem] font-bold leading-none tabular-nums text-[#8b98a9]">—</span>;
  }
  if (value === 0) {
    return <span className="text-[2.75rem] font-bold leading-none tabular-nums text-[#5b6878]">0</span>;
  }
  return (
    <span className="bg-[#d29922] px-[0.75rem] text-[2.75rem] font-bold leading-none tabular-nums text-black">
      {value}
    </span>
  );
}

function QualityPanel({ quality, hasDept }: { quality: WallboardQuality | null; hasDept: boolean }) {
  return (
    <section
      data-testid="quality-panel"
      className={`flex h-[20%] flex-col justify-between overflow-hidden px-[1rem] py-[0.625rem] ${
        quality === null || (quality.open_ncr_count === 0 && quality.wos_on_hold === 0)
          ? 'bg-[#10151d]'
          : 'bg-[#141b26]'
      }`}
    >
      <div className="flex items-baseline justify-between gap-[1rem]">
        <span className="flex items-baseline gap-[0.75rem]">
          <PanelLabel text="Open NCRs" />
          {hasDept && <PlantTag />}
        </span>
        <span className="flex items-baseline gap-[0.75rem]">
          {quality !== null && quality.open_ncr_count > 0 && quality.newest_ncr_age_days !== null && (
            <span className="whitespace-nowrap text-[1.25rem] tabular-nums text-[#8b98a9]">
              newest {quality.newest_ncr_age_days}d
            </span>
          )}
          <QualityValue value={quality?.open_ncr_count ?? null} />
        </span>
      </div>
      <div className="flex items-baseline justify-between gap-[1rem]">
        <PanelLabel text="On Hold" />
        <QualityValue value={quality?.wos_on_hold ?? null} />
      </div>
    </section>
  );
}

// ---- Rail --------------------------------------------------------------------

export default function ExceptionRail({
  workCenters,
  lateWos,
  blockedWos,
  ship,
  quality,
  lateTotal,
  blockedTotal,
  downTotal,
  hasDept,
  centralMinutes,
  flashKeys,
  extraMinutes,
}: {
  workCenters: WallboardWorkCenter[];
  lateWos: WallboardLateWorkOrder[];
  blockedWos: WallboardBlockedWorkOrder[];
  ship: WallboardShip | null;
  quality: WallboardQuality | null;
  lateTotal: number;
  blockedTotal: number;
  downTotal: number;
  hasDept: boolean;
  /** Minutes since Central midnight — drives the SHIP past-noon escalation. */
  centralMinutes: number;
  flashKeys: Set<string>;
  extraMinutes: number;
}) {
  // Clean day: P2 + P3 both zero AND no down centers → zero-lines render LARGE.
  const cleanDay = lateTotal === 0 && blockedTotal === 0 && downTotal === 0;

  return (
    <div className="flex h-full flex-col overflow-hidden border-[0.0625rem] border-[#243042]">
      <ShipPanel ship={ship} hasDept={hasDept} centralMinutes={centralMinutes} />
      <LatePanel lateWos={lateWos} lateTotal={lateTotal} cleanDay={cleanDay} flashKeys={flashKeys} />
      <BlockedDownPanel
        workCenters={workCenters}
        blockedWos={blockedWos}
        blockedTotal={blockedTotal}
        downTotal={downTotal}
        cleanDay={cleanDay}
        flashKeys={flashKeys}
        extraMinutes={extraMinutes}
      />
      <QualityPanel quality={quality} hasDept={hasDept} />
    </div>
  );
}
