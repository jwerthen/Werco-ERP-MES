/**
 * Laser-dispatch work-center ordering.
 *
 * Nest packages default onto the Ermaksan fiber laser — never the HSG tube
 * laser (owner decision). Candidates rank in tiers:
 *   0. name/code contains "ermaksan" or "fiber"
 *   1. other laser-ish entries (name/code/type contains "laser")
 *   2. everything else (management may deliberately dispatch elsewhere)
 *   3. entries containing "tube" — always last, never a default
 * Deterministic within a tier: name (case-insensitive), then id.
 */
import { WorkCenter } from '../types';

const haystack = (wc: WorkCenter) =>
  `${wc.name ?? ''} ${wc.code ?? ''} ${wc.work_center_type ?? ''}`.toLowerCase();

/** Tier for laser-dispatch ordering; lower ranks first. */
export function laserDispatchTier(wc: WorkCenter): number {
  const text = haystack(wc);
  if (text.includes('tube')) return 3;
  if (text.includes('ermaksan') || text.includes('fiber')) return 0;
  if (text.includes('laser')) return 1;
  return 2;
}

/** Pure sort (never mutates): dispatch tiers, then name, then id. */
export function sortWorkCentersForLaserDispatch(workCenters: WorkCenter[]): WorkCenter[] {
  return [...workCenters].sort((a, b) => {
    const tierDiff = laserDispatchTier(a) - laserDispatchTier(b);
    if (tierDiff !== 0) return tierDiff;
    const nameDiff = (a.name ?? '').toLowerCase().localeCompare((b.name ?? '').toLowerCase());
    if (nameDiff !== 0) return nameDiff;
    return a.id - b.id;
  });
}

/**
 * The work center a nest package should default to: the top-ranked concrete
 * laser (tier 0–1). Tube lasers and non-laser centers never default —
 * undefined means "leave the pick on (auto-detect)".
 */
export function defaultLaserWorkCenter(workCenters: WorkCenter[]): WorkCenter | undefined {
  const sorted = sortWorkCentersForLaserDispatch(workCenters);
  return sorted.find((wc) => laserDispatchTier(wc) <= 1);
}
