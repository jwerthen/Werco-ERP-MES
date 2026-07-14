/**
 * Z2 FLOOR WALL — deterministic grid of NON-IDLE work-center tiles.
 *
 * Grid shape always exactly fills (rows = round(sqrt(N/1.6)), cols = ceil(N/rows));
 * trailing empty cells render as plain background, always bottom-right. Tiles
 * sort alarm-first (DOWN → BLOCKED → RUNNING-LATE → RUNNING, alphabetical
 * within class) so a habitual glance lands on the worst thing first. Density
 * tier (per-tile job-row budget) is damped with 2-poll hysteresis.
 *
 * >20 active centers: the documented answer is a ?dept= TV per screen. The
 * spec's last-resort 20s pagination of RUNNING tiles (DOWN/BLOCKED never page
 * out) is deliberately NOT implemented — unreachable at current shop size.
 */

import React, { useRef } from 'react';
import type { WallboardWorkCenter } from '../../types/wallboard';
import type { TierHysteresisState } from '../../utils/wallboardLayout';
import { computeGridShape, nextTierState, partitionWorkCenters, titleCaseDept } from '../../utils/wallboardLayout';
import IdleStrip from './IdleStrip';
import WorkCenterTile from './WorkCenterTile';

const IDLE_PANEL_CHIP_CAP = 12;

export default function FloorGrid({
  workCenters,
  pollKey,
  dept,
  flashKeys,
  extraMinutes,
  lateDaysByWo,
}: {
  workCenters: WallboardWorkCenter[];
  /** Changes once per successful poll (generated_at) — drives tier hysteresis. */
  pollKey: string;
  dept: string | null;
  flashKeys: Set<string>;
  extraMinutes: number;
  lateDaysByWo: Map<string, number>;
}) {
  const { active, idle } = partitionWorkCenters(workCenters);

  // Density-tier hysteresis: advance the state machine exactly once per poll.
  // Render-time ref mutation is guarded by pollKey, so StrictMode double
  // renders and clock-tick re-renders never double-advance it.
  const hysteresisRef = useRef<{ key: string | null; state: TierHysteresisState | null }>({
    key: null,
    state: null,
  });
  if (hysteresisRef.current.key !== pollKey) {
    hysteresisRef.current = {
      key: pollKey,
      state: nextTierState(hysteresisRef.current.state, active.length),
    };
  }
  const tier = hysteresisRef.current.state?.tier ?? 'roomy';

  // Zero work centers (bad dept value): full-zone empty state, kept verbatim.
  if (workCenters.length === 0) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-[1.875rem] text-[#8b98a9]">
          No active work centers{dept ? ` for "${titleCaseDept(dept)}"` : ''}
        </p>
      </div>
    );
  }

  // All centers idle: one large dim FLOOR IDLE panel, chips enlarged.
  // A dark, calm screen is the designed reward — not an error state.
  if (active.length === 0) {
    const chips = idle.slice(0, IDLE_PANEL_CHIP_CAP);
    const hidden = idle.length - chips.length;
    return (
      <div
        data-testid="floor-idle"
        className="flex h-full flex-col items-center justify-center gap-[2rem] border-[0.0625rem] border-[#243042] bg-[#10151d]"
      >
        <p className="text-[4rem] font-bold uppercase leading-none tracking-widest text-[#5b6878]">Floor Idle</p>
        <div className="flex flex-wrap items-center justify-center gap-x-[2rem] gap-y-[1rem] px-[2rem]">
          {chips.map(wc => (
            <span key={wc.id} className="whitespace-nowrap text-[1.75rem] leading-none text-[#5b6878]">
              {wc.code ?? wc.name} <span className="tabular-nums">Q{wc.queued_count}</span>
            </span>
          ))}
          {hidden > 0 && <span className="text-[1.5rem] text-[#5b6878]">+{hidden} more</span>}
        </div>
      </div>
    );
  }

  const { rows, cols } = computeGridShape(active.length);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        data-testid="wallboard-grid"
        className={`grid min-h-0 flex-1 gap-[0.5rem] ${active.length === 1 ? 'justify-items-center' : ''}`}
        style={{
          gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
          gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        }}
      >
        {active.map(wc => (
          <WorkCenterTile
            key={wc.id}
            wc={wc}
            tier={tier}
            flash={flashKeys.has(`wc-down:${wc.id}`) || flashKeys.has(`wc-blocked:${wc.id}`)}
            extraMinutes={extraMinutes}
            lateDaysByWo={lateDaysByWo}
            widthCap={active.length === 1}
          />
        ))}
      </div>
      <IdleStrip idle={idle} />
    </div>
  );
}
