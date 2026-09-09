import { z } from 'zod';
import type { ObservedShape, StockPieceEvidence } from '../types/stockPiece';

/** Exact decimal/fraction parsing. Never round a reported measurement through a float. */
export function canonicalInches(input: string, positive = false): string {
  const value = input.trim().replace(/^(-?)\./, '$10.');
  if (value.length > 80) throw new Error('Measurement is too long.');
  const scale = BigInt(1_000_000_000);
  let units: bigint;
  const decimal = /^(-?)(\d+)(?:\.(\d{1,9}))?$/.exec(value);
  if (decimal) {
    units =
      (BigInt(decimal[2]) * scale + BigInt((decimal[3] ?? '').padEnd(9, '0'))) * (decimal[1] ? BigInt(-1) : BigInt(1));
  } else {
    const fraction = /^(-?)(?:(\d+)\s+)?(\d+)\/(\d+)$/.exec(value);
    if (!fraction) throw new Error('Use inches as a decimal (up to 9 places) or an exact fraction such as 1 1/8.');
    const denominator = BigInt(fraction[4]);
    const numerator = BigInt(fraction[3]);
    if (denominator === BigInt(0) || (numerator * scale) % denominator !== BigInt(0))
      throw new Error('This fraction cannot be represented exactly within 9 decimal places.');
    units =
      (BigInt(fraction[2] ?? '0') * scale + (numerator * scale) / denominator) * (fraction[1] ? BigInt(-1) : BigInt(1));
  }
  if (units > BigInt(100000) * scale || units < BigInt(-100000) * scale || (positive && units <= BigInt(0)))
    throw new Error('Dimensions must be positive; coordinates must be within ±100,000 inches.');
  const magnitude = units < BigInt(0) ? -units : units;
  const digits = (magnitude % scale).toString().padStart(9, '0').replace(/0+$/, '');
  return `${units < BigInt(0) ? '-' : ''}${magnitude / scale}${digits ? `.${digits}` : ''}`;
}
const decimal = (positive = false) =>
  z.string().transform((value, ctx) => {
    try {
      return canonicalInches(value, positive);
    } catch (error) {
      ctx.addIssue({ code: 'custom', message: error instanceof Error ? error.message : 'Invalid inches.' });
      return z.NEVER;
    }
  });
const text = (max: number) => z.string().trim().min(1, 'This field is required.').max(max);
const point = z.object({ x: decimal(), y: decimal() }).strict();
const ring = z.array(point).min(3, 'A polygon needs at least three points.').max(2000);
const circle = z.object({ kind: z.literal('circle'), cx: decimal(), cy: decimal(), r: decimal(true) }).strict();
export const observedShapeSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('unknown') }).strict(),
  z.object({ kind: z.literal('rectangle'), width: decimal(true), height: decimal(true) }).strict(),
  circle,
  z.object({ kind: z.literal('polygon'), outer: ring, holes: z.array(ring).max(16) }).strict(),
]);
const loop = z.discriminatedUnion('kind', [circle, z.object({ kind: z.literal('polygon'), pts: ring }).strict()]);
export const stockPieceEvidenceSchema = z
  .object({
    version: z.literal(1),
    unit: z.literal('in'),
    measurement_method: text(120),
    source_units: z.enum(['in', 'mm', 'unknown']),
    geometry: observedShapeSchema,
    unavailable_zones: z
      .array(
        z
          .object({
            id: z.string().regex(/^[A-Za-z0-9_-]{1,64}$/),
            label: text(120),
            reason: text(1000),
            outline: loop,
          })
          .strict()
      )
      .max(16),
    thickness: decimal(true).nullable(),
    grade: text(120).nullable(),
    grain_axis: z.enum(['x', 'y']).nullable(),
    location_note: text(1000).nullable(),
    ownership_note: text(1000).nullable(),
    certification_note: text(1000).nullable(),
  })
  .strict()
  .superRefine((value, ctx) => {
    let count =
      value.geometry.kind === 'polygon'
        ? value.geometry.outer.length + value.geometry.holes.reduce((n, points) => n + points.length, 0)
        : value.geometry.kind === 'rectangle'
          ? 4
          : value.geometry.kind === 'circle'
            ? 1
            : 0;
    count += value.unavailable_zones.reduce(
      (n, zone) => n + (zone.outline.kind === 'circle' ? 1 : zone.outline.pts.length),
      0
    );
    if (count > 2000) ctx.addIssue({ code: 'custom', message: 'Shape, holes and zones exceed 2,000 source vertices.' });
    if (new Set(value.unavailable_zones.map(zone => zone.id)).size !== value.unavailable_zones.length)
      ctx.addIssue({ code: 'custom', message: 'Zone IDs must be unique.' });
    if (value.geometry.kind === 'unknown' && value.unavailable_zones.length)
      ctx.addIssue({ code: 'custom', message: 'Unknown shape cannot position unavailable zones.' });
    if (new TextEncoder().encode(JSON.stringify(value)).length > 131072)
      ctx.addIssue({ code: 'custom', message: 'Observation evidence exceeds 128 KiB.' });
  });
export const observationFieldsSchema = z.object({
  label: text(120),
  reason: text(1000),
  observer_name: text(120),
  observed_at: text(80),
});
export function emptyEvidence(): StockPieceEvidence {
  return {
    version: 1,
    unit: 'in',
    measurement_method: '',
    source_units: 'unknown',
    geometry: { kind: 'unknown' },
    unavailable_zones: [],
    thickness: null,
    grade: null,
    grain_axis: null,
    location_note: null,
    ownership_note: null,
    certification_note: null,
  };
}
export function shapeSummary(shape: ObservedShape): string {
  if (shape.kind === 'rectangle') return `${shape.width} × ${shape.height} in rectangle`;
  if (shape.kind === 'circle') return `Circle · radius ${shape.r} in`;
  if (shape.kind === 'polygon') return `Polygon · ${shape.outer.length} outer points · ${shape.holes.length} holes`;
  return 'Shape unknown';
}
