/**
 * Idle strip at the foot of the floor wall: idle work centers (no active job,
 * no downtime, nothing blocked) leave the tile grid and collapse into dim
 * slate chips with their queue counts — quiet when normal. Hidden when no
 * center is idle. Fixed chip capacity + "+N" (no scrolling anywhere).
 */

import React from 'react';
import type { WallboardWorkCenter } from '../../types/wallboard';

const CHIP_CAP = 8;

export default function IdleStrip({ idle }: { idle: WallboardWorkCenter[] }) {
  if (idle.length === 0) return null;
  const shown = idle.slice(0, CHIP_CAP);
  const hidden = idle.length - shown.length;

  return (
    <div
      data-testid="idle-strip"
      className="mt-[0.5rem] flex h-[3.5rem] shrink-0 items-center gap-[1rem] overflow-hidden border-[0.0625rem] border-[#243042] bg-[#10151d] px-[1rem]"
    >
      <span className="shrink-0 whitespace-nowrap text-[1.25rem] uppercase tracking-widest text-[#8b98a9]">
        Idle {idle.length}
      </span>
      <span aria-hidden="true" className="shrink-0 text-[1.25rem] text-[#243042]">
        —
      </span>
      <div className="flex min-w-0 items-center gap-[1.5rem] overflow-hidden">
        {shown.map(wc => (
          <span key={wc.id} className="whitespace-nowrap text-[1.5rem] leading-none text-[#5b6878]">
            {wc.code ?? wc.name} <span className="tabular-nums">Q{wc.queued_count}</span>
          </span>
        ))}
        {hidden > 0 && <span className="shrink-0 whitespace-nowrap text-[1.25rem] text-[#5b6878]">+{hidden} more</span>}
      </div>
    </div>
  );
}
