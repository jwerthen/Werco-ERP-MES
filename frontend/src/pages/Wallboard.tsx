/**
 * /wallboard — full-screen, read-only shop-floor TV board (Foundry design
 * handoff 2026-07-22): HUD command bar, 4×3 work-order grid + overflow
 * strip, right rail (SHIP TODAY / LATE / BLOCKED·DOWN / NCRs·holds), and
 * the TODAY KPI bar. Blueprint-grid canvas, JetBrains Mono throughout,
 * tabular numerals everywhere.
 *
 * Designed for an unattended TV at ~3–6m viewing distance:
 *  - NO Layout chrome, NO PrivateRoute. Auth comes from a scoped display
 *    token passed once as #token=<jwt> (captured to sessionStorage and
 *    scrubbed from the URL) or a logged-in user's session token.
 *  - All requests go through services/wallboardClient — the display token is
 *    never placed in the global axios client.
 *  - 30s polling (deliberately no WebSocket — reliability first). On fetch
 *    failure the last good data stays up; the HUD sync chip steps SYNC OK →
 *    SYNC STALE (1 failed poll) → SYNC LOST (>=4, ~2 min), steady, never
 *    flashing. A revoked/expired token gets its own full-screen state and
 *    polling stops.
 *  - ?dept=<work_center_type> narrows the board to one department.
 *  - The ONLY animation on the board is fdPulse on DOWN dots. Nothing else
 *    animates, nothing scrolls, and every zone keeps its slot at all data
 *    values (fixed geography).
 *
 * Scaling: the root sets fontSize calc(100vh / 67.5) → 1rem = 16px @1080p,
 * 32px @4K (identical angular size), so the handoff's px values render
 * exactly at 1080p as px/16 rem. EVERY size in this tree is rem — inline
 * styles included. NOTE: rem resolves against the <html> element, not this
 * container, so a mount effect mirrors the same calc() onto
 * document.documentElement (restored on unmount).
 *
 * Display settings (clock24h / clockSeconds / nightDim, all default false)
 * persist per display in localStorage; URL params clock24 / seconds / dim
 * (each 1/0) override AND re-persist them.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import BlockedDownPanel from '../components/wallboard/BlockedDownPanel';
import HudBar from '../components/wallboard/HudBar';
import LatePanel from '../components/wallboard/LatePanel';
import QualitySplitRow from '../components/wallboard/QualitySplitRow';
import ShipTodayPanel from '../components/wallboard/ShipTodayPanel';
import TodayKpiBar from '../components/wallboard/TodayKpiBar';
import WoGrid from '../components/wallboard/WoGrid';
import { FD } from '../components/wallboard/wallboardTokens';
import {
  captureWallboardTokenFromUrl,
  clearWallboardToken,
  fetchWallboard,
  getWallboardToken,
} from '../services/wallboardClient';
import type { WallboardResponse } from '../types/wallboard';
import { getCentralMinutesOfDay } from '../utils/centralTime';

const POLL_INTERVAL_MS = 30_000;
/** Failed polls before the sync chip escalates STALE → LOST (~2 min). */
const OFFLINE_RED_THRESHOLD = 4;
const ROOT_FONT_SIZE = 'calc(100vh / 67.5)';
/** localStorage key for the per-display clock/dim settings. */
const SETTINGS_STORAGE_KEY = 'wallboard_display_settings';

/**
 * Motion budget: fdPulse on DOWN dots (header chip when down > 0, DOWN card
 * chips) is the ONLY animation on the board. Nothing else animates — no
 * heartbeat, no new-event flash, no payload-swap fade (design rule: no
 * ambient motion on data).
 */
const WALLBOARD_CSS = `
  @keyframes fdPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
`;

interface DisplaySettings {
  clock24h: boolean;
  clockSeconds: boolean;
  nightDim: boolean;
}

/**
 * URL params (clock24 / seconds / dim, each "1"/"0") override the stored
 * settings; anything the URL doesn't mention loads from localStorage;
 * everything defaults false.
 */
function resolveDisplaySettings(params: URLSearchParams): DisplaySettings {
  let stored: Partial<DisplaySettings> = {};
  try {
    const raw = localStorage.getItem(SETTINGS_STORAGE_KEY);
    // "null" is valid JSON, so a null parse would escape the catch and crash
    // the field reads below — guard the shape, not just the parse.
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (parsed !== null && typeof parsed === 'object') stored = parsed as Partial<DisplaySettings>;
  } catch {
    stored = {};
  }
  const read = (param: string, fallback: boolean | undefined): boolean => {
    const raw = params.get(param);
    if (raw === '1') return true;
    if (raw === '0') return false;
    // Strict comparison: hand-edited storage may hold non-boolean junk.
    return fallback === true;
  };
  return {
    clock24h: read('clock24', stored.clock24h),
    clockSeconds: read('seconds', stored.clockSeconds),
    nightDim: read('dim', stored.nightDim),
  };
}

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

  const settings = useMemo(() => resolveDisplaySettings(searchParams), [searchParams]);

  // A URL that mentions any display setting also persists the resolved set,
  // so the next unparameterized boot keeps the same behavior.
  useEffect(() => {
    if (!searchParams.has('clock24') && !searchParams.has('seconds') && !searchParams.has('dim')) return;
    try {
      localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(settings));
    } catch {
      // Storage unavailable — the settings still apply for this page load.
    }
  }, [searchParams, settings]);

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
  const load = useCallback(
    async (stale: () => boolean = () => false) => {
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
          // Keep the last good board on screen; just step the sync chip
          // (STALE → LOST after OFFLINE_RED_THRESHOLD consecutive misses).
          setOffline(true);
          setConsecutiveFailures(count => count + 1);
        }
      }
    },
    [dept]
  );

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

  // Minute counters tick client-side between polls (downtime, job elapsed).
  // Derived directly from lastUpdated (not a ref) so the render where a fresh
  // payload lands can never pair new server minutes with a stale baseline.
  const extraMinutes = lastUpdated ? Math.max(0, Math.floor((now.getTime() - lastUpdated.getTime()) / 60_000)) : 0;

  // True uncapped totals for the HUD chips + rail; fallback to list lengths /
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

  const offlineLevel: 0 | 1 | 2 = !offline ? 0 : consecutiveFailures >= OFFLINE_RED_THRESHOLD ? 2 : 1;

  return (
    <div
      className="fixed inset-0 flex flex-col overflow-hidden font-mono tabular-nums"
      style={{
        fontSize: ROOT_FONT_SIZE,
        gap: '0.875rem',
        padding: '1.375rem 1.5rem',
        color: FD.ink,
        backgroundColor: FD.canvas,
        // Blueprint texture: two hairline grids at 28px spacing + a soft
        // radial glow top-right, all in rem so 4K doubles with the type.
        backgroundImage:
          'linear-gradient(rgba(47,129,247,0.03) 0.0625rem, transparent 0.0625rem),' +
          'linear-gradient(90deg, rgba(47,129,247,0.03) 0.0625rem, transparent 0.0625rem),' +
          'radial-gradient(43.75rem 31.25rem at 88% 0%, rgba(47,129,247,0.06), transparent 65%)',
        backgroundSize: '1.75rem 1.75rem, 1.75rem 1.75rem, auto',
      }}
    >
      <style>{WALLBOARD_CSS}</style>

      {revoked ? (
        <div
          className="flex flex-1 flex-col items-center justify-center gap-[1rem] text-center"
          data-testid="revoked-screen"
        >
          <p className="text-[2.5rem] font-bold tracking-[0.04em]" style={{ color: FD.red }}>
            Display access revoked or expired
          </p>
          <p className="max-w-[48rem] text-[1.5rem]" style={{ color: FD.mute }}>
            Create a new display link or setup code in Admin Settings → Wallboard Displays, then open /tv on this
            screen and enter the code.
          </p>
        </div>
      ) : noToken && !data ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-[1rem] text-center">
          <p className="text-[2.5rem] font-bold" style={{ color: FD.ink }}>
            No display token
          </p>
          <p className="max-w-[48rem] text-[1.5rem]" style={{ color: FD.mute }}>
            Get a setup code from Admin Settings → Wallboard Displays and enter it at /tv on this screen. (Or use the
            one-time wallboard link from the same page, or sign in first.)
          </p>
        </div>
      ) : !data ? (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-[1.875rem]" style={{ color: FD.mute }} data-testid="wallboard-loading">
            Loading board…
          </p>
        </div>
      ) : (
        <>
          <HudBar
            dept={dept}
            downCount={totals.down}
            blockedCount={totals.blocked}
            lateCount={totals.late}
            offlineLevel={offlineLevel}
            lastUpdated={lastUpdated}
            now={now}
            clock24h={settings.clock24h}
            clockSeconds={settings.clockSeconds}
          />

          <div className="flex min-h-0 flex-1 gap-[0.875rem]">
            <WoGrid
              jobs={data.jobs ?? null}
              jobsTotal={data.jobs_total ?? null}
              workCenters={data.work_centers}
              blockedWos={data.blocked_wos}
              extraMinutes={extraMinutes}
            />
            <aside className="flex min-h-0 w-[26.875rem] flex-none flex-col gap-[0.8125rem]">
              <ShipTodayPanel ship={data.ship ?? null} centralMinutes={getCentralMinutesOfDay(now)} />
              <LatePanel lateWos={data.late_wos} lateTotal={totals.late} />
              <BlockedDownPanel
                workCenters={data.work_centers}
                blockedWos={data.blocked_wos}
                blockedTotal={totals.blocked}
                downTotal={totals.down}
                extraMinutes={extraMinutes}
              />
              <QualitySplitRow quality={data.quality ?? null} />
            </aside>
          </div>

          <TodayKpiBar today={data.today ?? null} now={now} />
        </>
      )}

      {settings.nightDim && (
        <div
          className="pointer-events-none absolute inset-0 z-50"
          data-testid="night-dim-overlay"
          style={{ background: 'rgba(0,0,0,0.38)' }}
        />
      )}
    </div>
  );
}
