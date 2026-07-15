/**
 * /wallboard — full-screen, read-only shop-floor TV board ("ANDON WALL").
 *
 * Designed for an unattended TV at ~4–5m viewing distance:
 *  - NO Layout chrome, NO PrivateRoute. Auth comes from a scoped display
 *    token passed once as #token=<jwt> (captured to sessionStorage and
 *    scrubbed from the URL) or a logged-in user's session token.
 *  - All requests go through services/wallboardClient — the display token is
 *    never placed in the global axios client.
 *  - 30s polling (deliberately no WebSocket — reliability first).
 *  - On fetch failure: keep showing the last good data; a STEADY amber chip
 *    after 1 failed poll escalates to a steady red fill chip after 4
 *    (~2 min). Never flashing — flashing is reserved for newly-raised events.
 *  - ?dept=<work_center_type> narrows the board to one department's centers.
 *
 * Layout (spec): Z1 header 9% / Z2 floor wall 72.5%w + Z3 exception rail
 * 27.5%w at 82%h / Z4 today+30d band 9%. NO scroll containers anywhere —
 * every zone has computed capacity, a "+N more" overflow, and a designed
 * empty state. Fixed geography: panels keep their slots at all data values.
 *
 * Scaling: the root sets fontSize calc(100vh / 67.5) → 1rem = 16px @1080p,
 * 32px @4K (identical angular size). EVERY size in this tree is rem. NOTE:
 * rem resolves against the <html> element, not this container, so a mount
 * effect mirrors the same calc() onto document.documentElement (restored on
 * unmount) — the inline container fontSize alone would not scale rem units.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import ExceptionRail from '../components/wallboard/ExceptionRail';
import FloorGrid from '../components/wallboard/FloorGrid';
import TodayBand from '../components/wallboard/TodayBand';
import WallboardHeader from '../components/wallboard/WallboardHeader';
import { useNewEventFlash } from '../hooks/useNewEventFlash';
import {
  captureWallboardTokenFromUrl,
  clearWallboardToken,
  fetchWallboard,
  getWallboardToken,
} from '../services/wallboardClient';
import type { WallboardResponse } from '../types/wallboard';
import { getCentralMinutesOfDay } from '../utils/centralTime';

const POLL_INTERVAL_MS = 30_000;
/** Failed polls before the offline chip escalates amber → red fill (~2 min). */
const OFFLINE_RED_THRESHOLD = 4;
const ROOT_FONT_SIZE = 'calc(100vh / 67.5)';

/**
 * Motion budget (spec §7) — the exhaustive list; anything not here does not move:
 * 1s wall clock · minute counters between polls · 2s heartbeat (frozen offline)
 * · 600ms numeral/bar transitions + 200ms payload-swap fade at poll boundaries
 * · ~10s new-event flash (1.2s steps ×8) · instant tile resort on class change.
 * No marquees, no tickers, no rotation, no ambient motion.
 */
const WALLBOARD_CSS = `
  @keyframes wb-heartbeat { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }
  .wb-heartbeat { animation: wb-heartbeat 2s ease-in-out infinite; }
  .wb-heartbeat-frozen { animation-play-state: paused; }
  @keyframes wb-flash-new { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .wb-flash-new { animation: wb-flash-new 1.2s steps(1, end) 8; }
  @keyframes wb-swap { from { opacity: 0.6; } to { opacity: 1; } }
  .wb-swap { animation: wb-swap 200ms ease; }
  .wb-num { transition: color 600ms ease; }
`;

export default function Wallboard() {
  const [searchParams] = useSearchParams();
  const dept = searchParams.get('dept');

  const [data, setData] = useState<WallboardResponse | null>(null);
  const [offline, setOffline] = useState(false);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [noToken, setNoToken] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [now, setNow] = useState<Date>(new Date());
  const [swapping, setSwapping] = useState(false);

  // rem units resolve against <html>, so the vh-based scale must live there
  // for the whole tree's rem sizing to track the TV's resolution.
  useEffect(() => {
    const el = document.documentElement;
    const previous = el.style.fontSize;
    el.style.fontSize = ROOT_FONT_SIZE;
    return () => {
      el.style.fontSize = previous;
    };
  }, []);

  // Capture ?token= / #token= BEFORE the first fetch (and scrub it from the URL).
  useEffect(() => {
    captureWallboardTokenFromUrl();
    if (!getWallboardToken()) setNoToken(true);
  }, []);

  // `stale` is the owning effect's cancellation probe — a fetch that resolves
  // after a dept change (or unmount) must not paint the old dept's data.
  const load = useCallback(async (stale: () => boolean = () => false) => {
    try {
      const payload = await fetchWallboard(dept);
      if (stale()) return;
      setData(payload);
      setLastUpdated(new Date());
      setOffline(false);
      setConsecutiveFailures(0);
      setNoToken(false);
    } catch (err: any) {
      if (stale()) return;
      if (err?.message === 'NO_TOKEN') {
        setNoToken(true);
      } else if (err?.message === 'UNAUTHORIZED') {
        // Revoked or expired display token: stale data + an "offline" badge
        // would lie forever on an unattended TV. Drop the dead credential,
        // stop polling, and show the distinct full-screen state.
        clearWallboardToken();
        setRevoked(true);
        setOffline(false);
      } else {
        // Keep the last good board on screen; just flag it (steady chip,
        // amber → red fill after OFFLINE_RED_THRESHOLD consecutive misses).
        setOffline(true);
        setConsecutiveFailures(count => count + 1);
      }
    }
  }, [dept]);

  // Poll every 30s (suspended once the token is known-dead — every further
  // poll would just 401 again until someone provisions a new link).
  useEffect(() => {
    if (revoked) return undefined;
    let cancelled = false;
    const run = () => load(() => cancelled);
    run();
    const id = setInterval(run, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [load, revoked]);

  // Wall clock — 1s tick.
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1_000);
    return () => clearInterval(id);
  }, []);

  // 200ms opacity fade at each payload swap (motion budget item 4).
  useEffect(() => {
    if (!data) return undefined;
    setSwapping(true);
    const id = setTimeout(() => setSwapping(false), 250);
    return () => clearTimeout(id);
  }, [data]);

  // New-event flash: diffed by stable ids, suppressed on first paint and on
  // ?dept= change (a token re-mint is a fresh page load = first paint).
  const flashKeys = useNewEventFlash(data, dept ?? '');

  // Minute counters tick client-side between polls (downtime, job elapsed).
  // Derived directly from lastUpdated (not a ref) so the render where a fresh
  // payload lands can never pair new server minutes with a stale baseline.
  const extraMinutes = lastUpdated ? Math.max(0, Math.floor((now.getTime() - lastUpdated.getTime()) / 60_000)) : 0;

  // True uncapped totals for the hero + rail; fallback to list lengths /
  // derived counts against an old backend (degraded but rendering).
  const totals = useMemo(() => {
    if (!data) return { down: 0, blocked: 0, late: 0 };
    const downFromCenters = data.work_centers.filter(wc => wc.down !== null).length;
    return {
      down: data.down_total ?? downFromCenters,
      blocked: data.blocked_total ?? data.blocked_wos.length,
      late: data.late_total ?? data.late_wos.length,
    };
  }, [data]);

  const offShift = useMemo(() => {
    if (!data) return false;
    return data.today?.operators_on_clock === 0 && data.work_centers.every(wc => wc.active_jobs.length === 0);
  }, [data]);

  // days_late by WO number, for the tile job rows' "LATE Nd" suffix chips.
  const lateDaysByWo = useMemo(() => {
    const map = new Map<string, number>();
    for (const wo of data?.late_wos ?? []) map.set(wo.wo_number, wo.days_late);
    return map;
  }, [data]);

  const offlineLevel: 0 | 1 | 2 = !offline ? 0 : consecutiveFailures >= OFFLINE_RED_THRESHOLD ? 2 : 1;

  return (
    <div
      className="fixed inset-0 flex flex-col overflow-hidden bg-[#070a0f] font-sans text-[#f0f4f9]"
      style={{ fontSize: ROOT_FONT_SIZE, padding: '2%' }}
    >
      <style>{WALLBOARD_CSS}</style>

      {/* Z1 HEADER — 9%h */}
      <div className="min-h-0 shrink-0 grow-0 basis-[9%]">
        <WallboardHeader
          dept={dept}
          totals={totals}
          offShift={offShift}
          hasData={data !== null}
          offline={offline}
          offlineLevel={offlineLevel}
          lastUpdated={lastUpdated}
          now={now}
        />
      </div>

      {revoked ? (
        <div
          className="flex flex-1 flex-col items-center justify-center gap-[1rem] text-center"
          data-testid="revoked-screen"
        >
          <p className="text-[2.5rem] font-bold text-[#f04438]">Display access revoked or expired</p>
          <p className="max-w-[48rem] text-[1.5rem] text-[#8b98a9]">
            Create a new display link or setup code in Admin Settings → Wallboard Displays, then open /tv on this
            screen and enter the code.
          </p>
        </div>
      ) : noToken && !data ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-[1rem] text-center">
          <p className="text-[2.5rem] font-bold">No display token</p>
          <p className="max-w-[48rem] text-[1.5rem] text-[#8b98a9]">
            Get a setup code from Admin Settings → Wallboard Displays and enter it at /tv on this screen. (Or use the
            one-time wallboard link from the same page, or sign in first.)
          </p>
        </div>
      ) : !data ? (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-[1.875rem] text-[#8b98a9]" data-testid="wallboard-loading">
            Loading board…
          </p>
        </div>
      ) : (
        <>
          {/* Z2 FLOOR WALL 72.5%w + Z3 EXCEPTION RAIL 27.5%w — 82%h */}
          <div
            className={`flex min-h-0 shrink-0 grow-0 basis-[82%] gap-[0.75rem] py-[0.75rem] ${
              swapping ? 'wb-swap' : ''
            }`}
          >
            <div className="min-w-0 basis-[72.5%]">
              <FloorGrid
                key={dept ?? 'all'}
                workCenters={data.work_centers}
                pollKey={data.generated_at}
                dept={dept}
                flashKeys={flashKeys}
                extraMinutes={extraMinutes}
                lateDaysByWo={lateDaysByWo}
              />
            </div>
            <div className="min-w-0 basis-[27.5%]">
              <ExceptionRail
                workCenters={data.work_centers}
                lateWos={data.late_wos}
                blockedWos={data.blocked_wos}
                ship={data.ship ?? null}
                quality={data.quality ?? null}
                lateTotal={totals.late}
                blockedTotal={totals.blocked}
                downTotal={totals.down}
                hasDept={!!dept}
                centralMinutes={getCentralMinutesOfDay(now)}
                flashKeys={flashKeys}
                extraMinutes={extraMinutes}
              />
            </div>
          </div>

          {/* Z4 TODAY / 30-DAY BAND — 9%h */}
          <div className={`min-h-0 shrink-0 grow-0 basis-[9%] ${swapping ? 'wb-swap' : ''}`}>
            <TodayBand today={data.today ?? null} kpis={data.kpi_strip ?? null} />
          </div>
        </>
      )}
    </div>
  );
}
