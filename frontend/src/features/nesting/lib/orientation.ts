import type { Placement } from './nesting';

export type RotationMode = 'fixed' | 'half-turn' | 'quarter-turn';
export type GrainAxis = 'x' | 'y';
type PartOrientation = { rotate: boolean; rotationMode?: RotationMode; grainAxis?: GrainAxis };
type SheetOrientation = { grainAxis?: GrainAxis };

/** Legacy rotate=false means fixed; an explicit mode always takes precedence. */
export function effectiveRotationMode(part: PartOrientation): RotationMode {
  return part.rotationMode ?? (part.rotate ? 'quarter-turn' : 'fixed');
}

/** Grain is an undirected axis: half turns preserve it, quarter turns exchange X/Y. */
export function allowedRotations(part: PartOrientation, stock: SheetOrientation): Placement['rotation'][] {
  const mode = effectiveRotationMode(part);
  const rotations: Placement['rotation'][] =
    mode === 'fixed' ? [0] : mode === 'half-turn' ? [0, 180] : mode === 'quarter-turn' ? [0, 90, 180, 270] : [];
  if (part.grainAxis === undefined) return rotations;
  if (stock.grainAxis === undefined) return [];
  return rotations.filter(rotation =>
    rotation % 180 === 0 ? part.grainAxis === stock.grainAxis : part.grainAxis !== stock.grainAxis
  );
}

/** A blocking reason only; having an allowed orientation does not establish sheet fit. */
export function orientationExplanation(part: PartOrientation, stock: SheetOrientation): string | null {
  if (allowedRotations(part, stock).length) return null;
  if (part.grainAxis !== undefined && stock.grainAxis === undefined)
    return 'Part grain is required, but sheet grain is unknown. Assign the sheet grain direction before nesting.';
  return `Permitted rotations cannot align the part's ${part.grainAxis?.toUpperCase() ?? 'required'}-axis grain with the sheet's ${stock.grainAxis?.toUpperCase() ?? 'required'}-axis grain.`;
}

export function hasOrientationConstraints(
  parts: readonly { rotationMode?: unknown; grainAxis?: unknown }[],
  stock: SheetOrientation
): boolean {
  return (
    stock.grainAxis !== undefined || parts.some(part => part.rotationMode !== undefined || part.grainAxis !== undefined)
  );
}
