/**
 * OPEN NCRS / ON HOLD split row — two half panels, no top accent (design
 * handoff 2026-07-22, zone 3, panel 4).
 *
 * Left: OPEN NCRS with a NEWEST {n}D AGO sub-line (only when the age is
 * known) and an amber count (mute at zero). Right: ON HOLD with an ink
 * count. A null quality block renders em-dashes — both half panels keep
 * their slots at all data values.
 */

import React from 'react';
import type { WallboardQuality } from '../../types/wallboard';
import { FD } from './wallboardTokens';

export default function QualitySplitRow({ quality }: { quality: WallboardQuality | null }) {
  const ncr = quality?.open_ncr_count ?? null;
  const hold = quality?.wos_on_hold ?? null;

  return (
    <div className="flex flex-none gap-[0.8125rem]" data-testid="quality-row">
      <div
        className="flex flex-1 items-center justify-between gap-[0.625rem] rounded-[0.25rem] px-[1.25rem] py-[0.875rem]"
        style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}` }}
      >
        <div className="flex min-w-0 flex-col gap-[0.3125rem]">
          <span className="text-[0.875rem] font-semibold tracking-[0.16em]" style={{ color: FD.mute }}>
            OPEN NCRS
          </span>
          {quality !== null && quality.newest_ncr_age_days !== null && (
            <span className="text-[0.8125rem] tracking-[0.08em]" style={{ color: FD.faint }}>
              NEWEST {quality.newest_ncr_age_days}D AGO
            </span>
          )}
        </div>
        <span
          className="text-[2.375rem] font-extrabold leading-none"
          style={{ color: ncr !== null && ncr > 0 ? FD.amber : FD.mute }}
        >
          {ncr ?? '—'}
        </span>
      </div>
      <div
        className="flex flex-1 items-center justify-between gap-[0.625rem] rounded-[0.25rem] px-[1.25rem] py-[0.875rem]"
        style={{ background: FD.panel, border: `0.0625rem solid ${FD.line}` }}
      >
        <span className="text-[0.875rem] font-semibold tracking-[0.16em]" style={{ color: FD.mute }}>
          ON HOLD
        </span>
        <span
          className="text-[2.375rem] font-extrabold leading-none"
          style={{ color: hold !== null ? FD.ink : FD.mute }}
        >
          {hold ?? '—'}
        </span>
      </div>
    </div>
  );
}
