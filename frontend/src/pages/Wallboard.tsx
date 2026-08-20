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
 *    animates and nothing scrolls. Every zone keeps its slot at all data values
 *    (fixed geography) — with ONE scoped exception: zone 2's FIELD (grid rows
 *    2-3) rotates through the delivered work orders on a 22s dwell so every open
 *    WO reaches the wall. That is a discrete React state derivation, NOT motion:
 *    no fade, no slide, no transform, no CSS transition, no new @keyframes. It
 *    has to be, because the global prefers-reduced-motion block in
 *    styles/accessibility.css forces animation-duration: 0.01ms !important on
 *    `*`, so a CSS-animation-driven carousel would freeze on page 0 forever on
 *    any TV reporting reduced motion. Zone 2's ANCHOR row (grid row 1) is pinned
 *    and live, every other zone's geography is untouched, and the 12 grid cells
 *    keep their coordinates at every data value.
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

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { usePageTitle } from '../hooks/usePageTitle';
import { useWakeLock } from '../hooks/useWakeLock';
import BlockedDownPanel from '../components/wallboard/BlockedDownPanel';
import HudBar from '../components/wallboard/HudBar';
import LatePanel from '../components/wallboard/LatePanel';
import QualitySplitRow from '../components/wallboard/QualitySplitRow';
import ShipTodayPanel from '../components/wallboard/ShipTodayPanel';
import TodayKpiBar from '../components/wallboard/TodayKpiBar';
import WoGrid from '../components/wallboard/WoGrid';
import { useWoCycle } from '../components/wallboard/useWoCycle';
import { FD } from '../components/wallboard/wallboardTokens';
import {
  captureWallboardTokenFromUrl,
  clearWallboardToken,
  fetchWallboard,
  getWallboardToken,
} from '../services/wallboardClient';
import type { WallboardResponse } from '../types/wallboard';
import { getCentralMinutesOfDay } from '../utils/centralTime';
import { HELD_WO_STATUS } from '../utils/wallboardLayout';

const POLL_INTERVAL_MS = 30_000;
/**
 * Zone 2 field dwell. Whole seconds so the flip cannot jitter against the 1s
 * clock tick it is derived from. Deliberately NOT 20 or 30: LCM(22, 30) = 330s
 * against LCM(20, 30) = 60s, so a 20s or 30s dwell would coincide with the 30s
 * poll once a minute and teach viewers to attribute every data change to the
 * flip. Worst-case wait for a specific job is (pages - 1) x 22s — 22s at 16
 * delivered work orders, 44s at the 24 payload cap.
 */
const CYCLE_DWELL_MS = 22_000;
/** Failed polls before the sync chip escalates STALE → LOST (~2 min). */
const OFFLINE_RED_THRESHOLD = 4;
const ROOT_FONT_SIZE = 'calc(100vh / 67.5)';
/** localStorage key for the per-display clock/dim settings. */
const SETTINGS_STORAGE_KEY = 'wallboard_display_settings';

/**
 * Motion budget: fdPulse on DOWN dots (header chip when down > 0, DOWN card
 * chips) is the ONLY animation on the board. Nothing else animates — no
 * heartbeat, no new-event flash, no payload-swap fade (design rule: no
 * ambient motion on data). The zone 2 field flip adds NOTHING here: it is a
 * hard swap of eight React children with no keyframes, transition or transform,
 * and no countdown/progress element (a bar whose whole semantic is "the view
 * changes in N seconds" IS ambient motion on data).
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
  // Renders outside Layout, so the tab title is set here; keep the TV awake.
  usePageTitle('Wallboard · Werco ERP');
  useWakeLock();

  const [searchParams] = useSearchParams();
  const dept = searchParams.get('dept');

  const [data, setData] = useState<WallboardResponse | null>(null);
  const [offline, setOffline] = useState(false);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [noToken, setNoToken] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [now, setNow] = useState<Date>(new Date());
  /**
   * Monotonic counter, bumped once per poll in which some wo_number is NEW to
   * the `(blocked || down)` set — the server's bucket-1 predicate exactly.
   * Handed to useWoCycle, where it forces an immediate plan rebuild and snaps
   * the field to page 0; because anchor + field page 0 IS jobs[0..11] and the
   * server sorts blocked/down first, the newly-alarmed job is on screen by
   * construction up to rank 12.
   *
   * EDGE-triggered, never level-triggered: a machine down for three hours fires
   * ONCE. Level-triggering would pin the board on page 0 all afternoon and
   * silently restore the exact complaint this feature was filed to fix, on the
   * busiest days, invisibly.
   *
   * LATE is deliberately EXCLUDED from the snap set: days_late steps in a batch
   * at Central midnight, so a late trigger would fire at 00:00 with nobody
   * watching and would train people to ignore snaps. Late is an hours-to-days
   * condition, not a right-now event.
   */
  const [alarmSnap, setAlarmSnap] = useState(0);
  /** Previous poll's alarm set, scoped to the dept it was measured on. */
  const prevAlarmRef = useRef<{ dept: string | null; wos: Set<string> }>({ dept, wos: new Set<string>() });
  /**
   * RACE GUARD. `stale()` only cancels a fetch whose dept changed or whose
   * component unmounted; two polls for the SAME dept can still resolve out of
   * order (a slow one landing after a fast one). Applying the older payload
   * would advance prevAlarmRef past an alarm the board never rendered, and the
   * next genuine alarm would then be silently swallowed. So the diff — and
   * setData with it — runs only for a strictly newer request than the last one
   * applied.
   */
  const requestSeqRef = useRef(0);
  const appliedSeqRef = useRef(0);

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
      const seq = (requestSeqRef.current += 1);
      try {
        const payload = await fetchWallboard(dept);
        if (stale()) return;
        // Out-of-order / superseded response: paint nothing, diff nothing.
        if (seq <= appliedSeqRef.current) return;
        appliedSeqRef.current = seq;

        // Alarm-set diff — runs here, where setData lands, AFTER the stale()
        // probe and behind the sequence check, so prevAlarmRef only ever
        // advances on a payload the board actually rendered.
        if (prevAlarmRef.current.dept !== dept) {
          prevAlarmRef.current = { dept, wos: new Set<string>() };
        }
        // The server's bucket-1 predicate EXACTLY — which, since ON_HOLD work
        // orders joined the wall, means `(blocked || down) AND NOT held`:
        // `_job_sort_key` prepends a held bucket that sorts held work strictly
        // LAST regardless of its blocked/down flags. Snapping on a held job
        // would yank every TV in the plant back to field page 0 to show
        // something that is two pages away — an unpredictable whole-zone lurch
        // bought for nothing, which is exactly what the snap's "on screen by
        // construction" justification exists to rule out. A hold is a known
        // stop somebody already authorized; it is not a right-now event.
        const alarmed = new Set<string>(
          (Array.isArray(payload.jobs) ? payload.jobs : [])
            .filter(job => job.status?.trim().toLowerCase() !== HELD_WO_STATUS && (job.blocked || job.down))
            .map(job => job.wo_number)
        );
        let newlyAlarmed = false;
        alarmed.forEach(wo => {
          if (!prevAlarmRef.current.wos.has(wo)) newlyAlarmed = true;
        });
        prevAlarmRef.current = { dept, wos: alarmed };
        if (newlyAlarmed) setAlarmSnap(count => count + 1);

        setData(payload);
        setLastUpdated(new Date());
        setOffline(false);
        setConsecutiveFailures(0);
        setNoToken(false);
      } catch (err: any) {
        // Same guard the success path carries: only the NEWEST resolved request
        // may report connection state, or a slow poll that finally times out
        // after a newer one already painted flips the sync chip to STALE over
        // data that is seconds old (and four such overlaps escalate it to LOST).
        if (stale() || seq <= appliedSeqRef.current) return;
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

  // Zone 2 cadence, derived from the SAME 1s tick as the wall clock — no second
  // interval, no extra dep array, nothing new to clean up. Deriving the slot
  // from epoch time rather than accumulating a counter self-corrects when a
  // throttled or occluded tab resumes (Chrome throttles setInterval to ~1/min in
  // hidden tabs) and keeps every TV in the building in phase with every other.
  const slot = Math.floor(now.getTime() / CYCLE_DWELL_MS);
  const cycle = useWoCycle({
    jobs: data?.jobs ?? null,
    dept,
    slot,
    alarmSnap,
    // nightDim: the board is explicitly declaring nobody is looking, so this is
    // the one place where spending the motion budget provably buys nothing — and
    // page 0 is today's board, so a dimmed board is the board people already know.
    frozen: settings.nightDim,
    revoked,
  });

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
            Create a new display link or setup code in Admin Settings → Wallboard Displays, then open /tv on this screen
            and enter the code.
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
              anchorJobs={cycle.anchorJobs}
              fieldJobs={cycle.fieldJobs}
              pageIndex={cycle.pageIndex}
              pages={cycle.pages}
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
