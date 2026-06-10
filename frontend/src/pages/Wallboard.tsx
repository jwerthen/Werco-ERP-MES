/**
 * /wallboard — full-screen, read-only shop-floor TV board (A0.5).
 *
 * Designed for an unattended TV at ~5m viewing distance:
 *  - NO Layout chrome, NO PrivateRoute. Auth comes from a scoped display
 *    token passed once as ?token=<jwt> (captured to sessionStorage and
 *    scrubbed from the URL) or a logged-in user's session token.
 *  - All requests go through services/wallboardClient — the display token is
 *    never placed in the global axios client.
 *  - 30s polling (deliberately no WebSocket in v1 — reliability first).
 *  - On fetch failure: OFFLINE banner, keep showing the last good data.
 *  - ?dept=<work_center_type> narrows the board to one department's centers.
 *
 * Visual language: dark instrument panel, hairline borders, sharp corners.
 * Red flash = blocked or down. Amber = running a late work order.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  captureWallboardTokenFromUrl,
  fetchWallboard,
  getWallboardToken,
} from '../services/wallboardClient';
import type {
  WallboardResponse,
  WallboardWorkCenter,
} from '../types/wallboard';

const POLL_INTERVAL_MS = 30_000;
const TICKER_ROTATE_MS = 6_000;

function formatElapsed(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}h ${String(m).padStart(2, '0')}m`;
}

function formatClock(d: Date): string {
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

function blockerLabel(category: string): string {
  return category.replace(/_/g, ' ');
}

export default function Wallboard() {
  const [searchParams] = useSearchParams();
  const dept = searchParams.get('dept');

  const [data, setData] = useState<WallboardResponse | null>(null);
  const [offline, setOffline] = useState(false);
  const [noToken, setNoToken] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [now, setNow] = useState<Date>(new Date());
  const [tickerIndex, setTickerIndex] = useState(0);
  const mountedRef = useRef(true);

  // Capture ?token= BEFORE the first fetch (and scrub it from the URL).
  useEffect(() => {
    captureWallboardTokenFromUrl();
    if (!getWallboardToken()) setNoToken(true);
  }, []);

  const load = useCallback(async () => {
    try {
      const payload = await fetchWallboard(dept);
      if (!mountedRef.current) return;
      setData(payload);
      setLastUpdated(new Date());
      setOffline(false);
      setNoToken(false);
    } catch (err: any) {
      if (!mountedRef.current) return;
      if (err?.message === 'NO_TOKEN') {
        setNoToken(true);
      } else {
        // Keep the last good board on screen; just flag it.
        setOffline(true);
      }
    }
  }, [dept]);

  // Poll every 30s.
  useEffect(() => {
    mountedRef.current = true;
    load();
    const id = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [load]);

  // Wall clock — 1s tick.
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1_000);
    return () => clearInterval(id);
  }, []);

  // Ticker rotation.
  const tickerItems = useMemo(() => {
    if (!data) return [];
    return [
      ...data.late_wos.map((wo) => ({
        kind: 'late' as const,
        text: `LATE  ${wo.wo_number}  ${wo.part_number ?? ''}  — ${wo.days_late}d past due`,
      })),
      ...data.blocked_wos.map((wo) => ({
        kind: 'blocked' as const,
        text: `BLOCKED  ${wo.wo_number}  — ${blockerLabel(wo.category)} (${Math.round(wo.age_hours)}h)`,
      })),
    ];
  }, [data]);

  useEffect(() => {
    if (tickerItems.length < 2) return;
    const id = setInterval(() => setTickerIndex((i) => i + 1), TICKER_ROTATE_MS);
    return () => clearInterval(id);
  }, [tickerItems.length]);

  const lateWoNumbers = useMemo(
    () => new Set((data?.late_wos ?? []).map((wo) => wo.wo_number)),
    [data],
  );

  const tickerItem = tickerItems.length > 0 ? tickerItems[tickerIndex % tickerItems.length] : null;

  return (
    <div className="fixed inset-0 flex flex-col bg-[#070a0f] text-[#f0f4f9] overflow-hidden font-sans">
      <style>{`
        @keyframes wallboard-flash {
          0%, 100% { border-color: #f04438; box-shadow: inset 0 0 0 1px #f04438; }
          50% { border-color: #7a1d16; box-shadow: inset 0 0 0 1px transparent; }
        }
        .wallboard-flash { animation: wallboard-flash 1.2s steps(1, end) infinite; }
      `}</style>

      {/* Header: title / dept, clock, last-updated */}
      <header className="flex items-center justify-between px-8 py-4 border-b border-[#243042] shrink-0">
        <div className="flex items-baseline gap-4">
          <span className="text-3xl font-bold tracking-widest text-white uppercase">
            Werco<span className="text-[#C8352B]">.</span> Floor
          </span>
          {dept && (
            <span className="text-2xl uppercase tracking-wider text-[#8b98a9]" data-testid="dept-label">
              {dept}
            </span>
          )}
        </div>
        <div className="flex items-center gap-8">
          {offline && (
            <span
              data-testid="offline-banner"
              className="px-4 py-1.5 bg-[#C8352B] text-white text-xl font-bold uppercase tracking-widest wallboard-flash border"
            >
              Offline — showing last data
            </span>
          )}
          <span className="text-xl text-[#8b98a9] tabular-nums" data-testid="last-updated">
            {lastUpdated ? `Updated ${formatClock(lastUpdated)}` : 'Loading…'}
          </span>
          <span className="text-5xl font-bold tabular-nums" data-testid="wall-clock">
            {formatClock(now)}
          </span>
        </div>
      </header>

      {/* Body */}
      <main className="flex-1 overflow-hidden p-6">
        {noToken && !data ? (
          <div className="h-full flex flex-col items-center justify-center text-center gap-4">
            <p className="text-4xl font-bold">No display token</p>
            <p className="text-2xl text-[#8b98a9] max-w-3xl">
              Open this screen using the wallboard link from Admin Settings → Wallboard Displays
              (it includes a one-time ?token=… parameter), or sign in first.
            </p>
          </div>
        ) : !data ? (
          <div className="h-full flex items-center justify-center">
            <p className="text-3xl text-[#8b98a9]" data-testid="wallboard-loading">Loading board…</p>
          </div>
        ) : data.work_centers.length === 0 ? (
          <div className="h-full flex items-center justify-center">
            <p className="text-3xl text-[#8b98a9]">No active work centers{dept ? ` for "${dept}"` : ''}</p>
          </div>
        ) : (
          <div
            className="grid gap-4 h-full content-start overflow-y-auto"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(420px, 1fr))' }}
            data-testid="wallboard-grid"
          >
            {data.work_centers.map((wc) => (
              <WorkCenterCard key={wc.id} wc={wc} lateWoNumbers={lateWoNumbers} />
            ))}
          </div>
        )}
      </main>

      {/* Bottom ticker: cycles late + blocked WOs */}
      <footer className="shrink-0 border-t border-[#243042] bg-[#0d1117] px-8 py-3 flex items-center gap-6 min-h-[64px]">
        {tickerItem ? (
          <p
            data-testid="ticker"
            className={`text-3xl font-bold tracking-wide uppercase tabular-nums truncate ${
              tickerItem.kind === 'late' ? 'text-[#d29922]' : 'text-[#f04438]'
            }`}
          >
            {tickerItem.text}
          </p>
        ) : (
          <p className="text-3xl font-bold tracking-wide uppercase text-[#3fb950]" data-testid="ticker">
            All clear — nothing late or blocked
          </p>
        )}
        {tickerItems.length > 1 && (
          <span className="ml-auto text-xl text-[#8b98a9] tabular-nums shrink-0">
            {(tickerIndex % tickerItems.length) + 1}/{tickerItems.length}
          </span>
        )}
      </footer>
    </div>
  );
}

function WorkCenterCard({
  wc,
  lateWoNumbers,
}: {
  wc: WallboardWorkCenter;
  lateWoNumbers: Set<string>;
}) {
  const isDown = wc.down !== null;
  const isBlocked = wc.blocked_count > 0;
  const runningLate = wc.active_jobs.some((j) => j.wo_number && lateWoNumbers.has(j.wo_number));
  const alarm = isDown || isBlocked;

  return (
    <section
      data-testid={`wc-card-${wc.code ?? wc.id}`}
      className={`border bg-[#141b26] flex flex-col ${
        alarm
          ? 'wallboard-flash border-[#f04438]'
          : runningLate
            ? 'border-[#d29922]'
            : 'border-[#243042]'
      }`}
    >
      {/* Card header */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-[#243042]">
        <h2 className="text-3xl font-bold uppercase tracking-wide truncate">{wc.name}</h2>
        <div className="flex items-center gap-3 shrink-0">
          {isDown && (
            <span className="px-3 py-1 bg-[#f04438] text-black text-xl font-bold uppercase">
              Down · {blockerLabel(wc.down!.category)}
            </span>
          )}
          {isBlocked && (
            <span className="px-3 py-1 bg-[#7a1d16] text-[#ffb4ab] text-xl font-bold uppercase">
              {wc.blocked_count} blocked
            </span>
          )}
          {runningLate && !alarm && (
            <span className="px-3 py-1 bg-[#d29922] text-black text-xl font-bold uppercase">Late</span>
          )}
          <span className="text-xl text-[#8b98a9] tabular-nums uppercase">Queue {wc.queued_count}</span>
        </div>
      </div>

      {/* Jobs */}
      <div className="flex-1 px-5 py-3 space-y-3">
        {wc.active_jobs.length === 0 ? (
          <p className="text-2xl text-[#5b6878] uppercase tracking-wider py-2">Idle</p>
        ) : (
          wc.active_jobs.map((job, idx) => (
            <div key={`${job.wo_number}-${idx}`} className="flex items-baseline justify-between gap-4">
              <div className="min-w-0">
                <p className="text-3xl font-bold tabular-nums truncate">
                  {job.wo_number}
                  <span className="text-[#8b98a9] font-normal"> · {job.part_number}</span>
                </p>
                <p className="text-2xl text-[#aab6c5] truncate">
                  {job.op_name}
                  {job.operator_name ? ` — ${job.operator_name}` : ''}
                </p>
              </div>
              <div className="text-right shrink-0">
                <p className="text-3xl font-bold tabular-nums">{formatElapsed(job.elapsed_minutes)}</p>
                <p className="text-2xl text-[#8b98a9] tabular-nums">
                  {job.qty_done}/{job.qty_target}
                </p>
              </div>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
