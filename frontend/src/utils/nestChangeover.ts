/**
 * Laser-nest sequencing helpers for the Dispatch Board.
 *
 * A planner batches nests of the same material and thickness together because
 * every change of either costs a real setup on the machine — a sheet swap, an
 * assist-gas change, a nozzle/lens change. These helpers turn a column's current
 * order into the two things that make that cost visible: the per-boundary
 * changeover between two adjacent nests, and the per-column count.
 *
 * Pure and DOM-free on purpose so the comparison rules can be unit-tested
 * directly rather than through a rendered board.
 */

import type { DispatchNestInfo } from '../types';

/** What changes between two adjacent nests. `null` = nothing the board claims. */
export type NestChangeover = 'material' | 'thickness' | 'both';

/**
 * Trimmed + lower-cased, with blank treated as UNKNOWN (`null`).
 *
 * `A36`, `a36 ` and ` A36` are one material — a planner typed them, not a
 * machine, so casing and stray whitespace are not evidence of a changeover.
 */
const normalize = (value: string | null | undefined): string | null => {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim().toLowerCase();
  return trimmed || null;
};

/**
 * True only when BOTH values are known and they differ.
 *
 * An absent value is not evidence of a change: unknown↔known must never be
 * reported as a changeover, or the board invents setups that aren't there.
 */
const differs = (a: string | null | undefined, b: string | null | undefined): boolean => {
  const left = normalize(a);
  const right = normalize(b);
  return left != null && right != null && left !== right;
};

/**
 * The changeover between the nest ABOVE (`prev`) and the nest BELOW (`next`).
 *
 * Returns `null` when either side has no nest at all — a nest sitting next to a
 * non-nest job is not a material/thickness changeover, it's a different kind of
 * job entirely, and the board does not claim to know what that costs.
 */
export function nestChangeover(
  prev: DispatchNestInfo | null | undefined,
  next: DispatchNestInfo | null | undefined
): NestChangeover | null {
  if (!prev || !next) return null;
  const materialChanged = differs(prev.material, next.material);
  const thicknessChanged = differs(prev.thickness, next.thickness);
  if (materialChanged && thicknessChanged) return 'both';
  if (materialChanged) return 'material';
  if (thicknessChanged) return 'thickness';
  return null;
}

/** Human label for a changeover marker (the glyph is added by the renderer). */
export function changeoverLabel(kind: NestChangeover): string {
  if (kind === 'both') return 'material + thickness change';
  return `${kind} change`;
}

/**
 * How many nests a column holds and how many changeovers the CURRENT order
 * costs — the feedback loop that makes reordering purposeful ("4 nests ·
 * 2 changeovers"). Counts boundaries between adjacent rows only, so a non-nest
 * job between two nests breaks the comparison rather than being sequenced
 * through.
 */
export function nestQueueSummary<T extends { laser_nest?: DispatchNestInfo | null }>(
  rows: readonly T[]
): { nests: number; changeovers: number } {
  let nests = 0;
  let changeovers = 0;
  rows.forEach((row, index) => {
    if (row.laser_nest) nests += 1;
    if (index > 0 && nestChangeover(rows[index - 1].laser_nest, row.laser_nest)) changeovers += 1;
  });
  return { nests, changeovers };
}

/** One segment of the card's dense nest line, tagged so it can be styled. */
export interface NestDetailSegment {
  key: 'material' | 'thickness' | 'sheet_size' | 'runs';
  text: string;
}

const finite = (value: number | null | undefined): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

/**
 * Sheet counts arrive as floats (`completed_runs` is the operation's completed
 * quantity), so a partial sheet is real and must not be rounded away — but
 * 2.9000000000000004 is float noise, not information. Two decimals, trailing
 * zeros stripped: 3 -> "3", 2.5 -> "2.5".
 */
const formatSheets = (value: number): string => String(Number(value.toFixed(2)));

/**
 * The card's detail line, as segments — material, thickness, sheet size, and
 * sheets remaining. Missing pieces are OMITTED rather than rendered as empty
 * separators, so a half-populated nest reads as `A36 · 48x96`, never `A36 ·  · `.
 */
export function nestDetailSegments(nest: DispatchNestInfo | null | undefined): NestDetailSegment[] {
  if (!nest) return [];
  const segments: NestDetailSegment[] = [];
  const material = typeof nest.material === 'string' ? nest.material.trim() : '';
  const thickness = typeof nest.thickness === 'string' ? nest.thickness.trim() : '';
  const sheetSize = typeof nest.sheet_size === 'string' ? nest.sheet_size.trim() : '';
  if (material) segments.push({ key: 'material', text: material });
  if (thickness) segments.push({ key: 'thickness', text: thickness });
  if (sheetSize) segments.push({ key: 'sheet_size', text: sheetSize });

  const planned = finite(nest.planned_runs);
  // `remaining_runs` is server-derived; fall back to planned - completed only
  // when the server didn't send it, and never report a negative remainder.
  const remaining =
    finite(nest.remaining_runs) ?? (planned != null ? planned - (finite(nest.completed_runs) ?? 0) : null);
  if (planned != null && planned > 0 && remaining != null) {
    segments.push({
      key: 'runs',
      text: `${formatSheets(Math.max(0, remaining))} of ${formatSheets(planned)} sheets left`,
    });
  }
  return segments;
}

/** The same detail line as a plain string (labels, tooltips, tests). */
export const formatNestDetail = (nest: DispatchNestInfo | null | undefined): string =>
  nestDetailSegments(nest)
    .map((segment) => segment.text)
    .join(' · ');
