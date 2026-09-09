import * as Clipper from 'clipper-lib';
import type { StockPieceEvidence, ObservedLoop, InchPoint } from '../../../types/stockPiece';
import { bounds, loopArea, validatePart, validateLoop, type Loop, type Stock } from './nesting';
import { requireCurrentGeometryProfile, type GeometryProfileRef } from './geometry-profile';
import {
  canonicalPath,
  circleSegments,
  filledArea,
  guardedOuter,
  integerOuter,
  pointPath,
  SCALE,
  sortedPaths,
} from './guarded-geometry';
import { prepareDomainBoundary } from './domain-containment';
import { assertReportedZonesContained } from './recorded-source-containment';
import { REMNANT_DOMAIN_PROFILE, REMNANT_DOMAIN_RULES, requireRemnantDomainProfile } from './remnant-domain-profile';

export type StockDomain = {
  version: 1;
  profile: GeometryProfileRef;
  outer: Loop;
  holes: Loop[];
  sourceOriginIn: { x: string; y: string };
};
export type DomainStock = Stock & { domain: StockDomain };
const check = (ok: unknown, message: string): void => {
  if (!ok) throw new Error(`Recorded-piece geometry: ${message}`);
};
const maxVertices = REMNANT_DOMAIN_RULES.budgets.maxBooleanVertices;
const count = (paths: Clipper.Paths) => paths.reduce((n, path) => n + path.length, 0);
const loopCount = (loop: Loop) => (loop.type === 'circle' ? 1 : loop.points.length);
function ownedFrozen<T>(value: T): T {
  if (Array.isArray(value)) return Object.freeze(value.map(ownedFrozen)) as T;
  if (value && typeof value === 'object')
    return Object.freeze(
      Object.fromEntries(Object.entries(value).map(([key, child]) => [key, ownedFrozen(child)]))
    ) as T;
  return value;
}

/** Exact source arithmetic occurs before conversion into the millimeter kernel. */
function nanoinches(value: string, maximum = 100_000): bigint {
  check(
    typeof value === 'string' &&
      /^-?(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,9})?$/.test(value) &&
      value !== '-0' &&
      !(value.includes('.') && value.endsWith('0')),
    'invalid canonical source inches.'
  );
  const negative = value.startsWith('-'),
    text = negative ? value.slice(1) : value;
  const [whole, fraction = ''] = text.split('.');
  const integer = BigInt(whole) * BigInt(1_000_000_000) + BigInt(fraction.padEnd(9, '0'));
  check(integer <= BigInt(maximum) * BigInt(1_000_000_000), 'source coordinates exceed their bounds.');
  return negative ? -integer : integer;
}
function inchString(value: bigint): string {
  const negative = value < BigInt(0),
    absolute = negative ? -value : value;
  const whole = absolute / BigInt(1_000_000_000);
  const fraction = (absolute % BigInt(1_000_000_000)).toString().padStart(9, '0').replace(/0+$/, '');
  return `${negative ? '-' : ''}${whole}${fraction ? '.' + fraction : ''}`;
}
const millimeters = (value: bigint) => (Number(value) / 1_000_000_000) * 25.4;

export function stockForRecordedPiece(
  evidence: StockPieceEvidence,
  settings: { geometryProfile: GeometryProfileRef; margin: number; gap: number; zoneClearanceIn: string }
): DomainStock {
  check(evidence?.version === 1 && evidence.unit === 'in', 'unsupported reported evidence units.');
  const shape = evidence.geometry;
  check(shape && shape.kind !== 'unknown', 'record a measured shape before planning this piece.');
  assertReportedZonesContained(evidence);
  let originX = BigInt(0),
    originY = BigInt(0);
  if (shape.kind === 'circle') {
    originX = nanoinches(shape.cx) - nanoinches(shape.r);
    originY = nanoinches(shape.cy) - nanoinches(shape.r);
  } else if (shape.kind === 'polygon') {
    check(
      Array.isArray(shape.outer) && shape.outer.length >= 3 && shape.outer.length <= 2000,
      'invalid reported outer profile.'
    );
    originX = shape.outer.reduce(
      (minimum, p) => (nanoinches(p.x) < minimum ? nanoinches(p.x) : minimum),
      nanoinches(shape.outer[0].x)
    );
    originY = shape.outer.reduce(
      (minimum, p) => (nanoinches(p.y) < minimum ? nanoinches(p.y) : minimum),
      nanoinches(shape.outer[0].y)
    );
  }
  const point = (p: InchPoint) => ({
    x: millimeters(nanoinches(p.x) - originX),
    y: millimeters(nanoinches(p.y) - originY),
  });
  const observedLoop = (loop: ObservedLoop): Loop =>
    loop.kind === 'circle'
      ? {
          type: 'circle',
          cx: millimeters(nanoinches(loop.cx) - originX),
          cy: millimeters(nanoinches(loop.cy) - originY),
          r: millimeters(nanoinches(loop.r)),
        }
      : { type: 'poly', points: loop.pts.map(point) };
  let outer: Loop;
  let holes: Loop[] = [];
  if (shape.kind === 'rectangle') {
    const width = millimeters(nanoinches(shape.width)),
      height = millimeters(nanoinches(shape.height));
    outer = {
      type: 'poly',
      points: [
        { x: 0, y: 0 },
        { x: width, y: 0 },
        { x: width, y: height },
        { x: 0, y: height },
      ],
    };
  } else if (shape.kind === 'circle') outer = observedLoop(shape);
  else if (shape.kind === 'polygon') {
    outer = { type: 'poly', points: shape.outer.map(point) };
    holes = shape.holes.map(ring => ({ type: 'poly', points: ring.map(point) }));
  } else throw new Error('Recorded-piece geometry: unknown source shape.');
  const zoneNano = nanoinches(settings.zoneClearanceIn);
  check(zoneNano >= BigInt(0) && zoneNano <= BigInt(100_000_000_000), 'zone clearance must be 0–100 inches.');
  const box = bounds(outer);
  const stock: DomainStock = {
    geometryProfile: settings.geometryProfile,
    width: box.width,
    height: box.height,
    bedWidth: box.width,
    bedHeight: box.height,
    margin: settings.margin,
    gap: settings.gap,
    maxSheets: 1,
    ...(evidence.grain_axis ? { grainAxis: evidence.grain_axis } : {}),
    exclusions: evidence.unavailable_zones.map(zone => ({
      id: zone.id,
      label: zone.label,
      reason: zone.reason,
      outline: observedLoop(zone.outline),
      clearance: millimeters(zoneNano),
    })),
    domain: {
      version: 1,
      profile: { ...REMNANT_DOMAIN_PROFILE },
      outer,
      holes,
      sourceOriginIn: { x: inchString(originX), y: inchString(originY) },
    },
  };
  validateStockDomain(stock);
  return stock;
}

/** An available circle is inscribed. The outward obstacle helper is used only for holes/zones. */
function availableOuter(loop: Loop): Clipper.Path {
  if (loop.type === 'poly') return integerOuter(loop);
  const segments = circleSegments(loop.r);
  return canonicalPath(
    Array.from({ length: segments }, (_, i) => {
      const angle = (2 * Math.PI * i) / segments;
      return {
        X: Math.round((loop.cx + loop.r * Math.cos(angle)) * SCALE),
        Y: Math.round((loop.cy + loop.r * Math.sin(angle)) * SCALE),
      };
    }),
    true
  );
}

function validateQuantized(paths: Clipper.Paths): void {
  for (const path of paths) {
    check(path.length >= 3 && Clipper.Clipper.Area(path) !== 0, 'source contour collapses at the integer grid.');
    check(
      new Set(path.map(p => `${p.X},${p.Y}`)).size === path.length,
      'source contour merges distinct features at the integer grid.'
    );
    // Generated circle rings can exceed the source-polyline limit. Their angular
    // construction is convex; only original polygon rings use this source test.
    if (path.length <= 2000) validateLoop({ type: 'poly', points: pointPath(path) });
  }
}

export function validateStockDomain(stock: DomainStock): void {
  const source = stock.domain;
  check(
    source &&
      source.version === 1 &&
      Object.keys(source).sort().join(',') === 'holes,outer,profile,sourceOriginIn,version',
    'invalid actual-stock domain.'
  );
  requireCurrentGeometryProfile(stock.geometryProfile);
  requireRemnantDomainProfile(source.profile);
  check(
    source.sourceOriginIn && Object.keys(source.sourceOriginIn).sort().join(',') === 'x,y',
    'invalid source origin.'
  );
  nanoinches(source.sourceOriginIn.x, 200_000);
  nanoinches(source.sourceOriginIn.y, 200_000);
  check(
    Array.isArray(source.holes) && source.holes.length <= REMNANT_DOMAIN_RULES.budgets.maxHoles,
    'too many physical holes.'
  );
  validatePart({
    id: 'domain',
    name: 'Recorded material',
    quantity: 1,
    rotate: false,
    color: 0,
    loops: [source.outer, ...source.holes],
  });
  const box = bounds(source.outer);
  check(
    box.x === 0 && box.y === 0 && box.width === stock.width && box.height === stock.height,
    'stock dimensions must match the translated actual outer profile.'
  );
  check(stock.maxSheets === 1, 'a recorded physical piece may be used at most once.');
  check(
    Number.isFinite(stock.margin) && stock.margin >= 0 && Number.isFinite(stock.gap) && stock.gap >= 0,
    'invalid physical-edge margin or part gap.'
  );
  check((stock.exclusions?.length ?? 0) <= REMNANT_DOMAIN_RULES.budgets.maxZones, 'too many unavailable zones.');
  check(
    loopCount(source.outer) +
      source.holes.reduce((n, l) => n + loopCount(l), 0) +
      (stock.exclusions ?? []).reduce((n, zone) => n + loopCount(zone.outline), 0) <=
      REMNANT_DOMAIN_RULES.budgets.maxSourceVertices,
    'reported source geometry exceeds 2,000 vertices.'
  );
  const paths = [availableOuter(source.outer), ...source.holes.map(hole => canonicalPath(integerOuter(hole), false))];
  validateQuantized(paths);
  // Polygon source topology must also survive grid conversion, including hole relations.
  if (source.outer.type === 'poly' && source.holes.every(hole => hole.type === 'poly'))
    validatePart({
      id: 'domain-grid',
      name: 'Grid source',
      quantity: 1,
      rotate: false,
      color: 0,
      loops: paths.map(path => ({ type: 'poly', points: pointPath(path) })),
    });
  const ids = new Set<string>();
  for (const zone of stock.exclusions ?? []) {
    check(/^[A-Za-z0-9_-]{1,64}$/.test(zone.id) && !ids.has(zone.id), 'invalid or duplicate zone identity.');
    ids.add(zone.id);
    check(
      typeof zone.label === 'string' &&
        zone.label.trim() === zone.label &&
        zone.label.length > 0 &&
        zone.label.length <= 120 &&
        typeof zone.reason === 'string' &&
        zone.reason.trim() === zone.reason &&
        zone.reason.length > 0 &&
        zone.reason.length <= 1000,
      'zone label and reason must be nonempty and trimmed.'
    );
    check(
      Number.isFinite(zone.clearance) && zone.clearance >= 0 && zone.clearance <= 2540,
      'invalid additional zone clearance.'
    );
    validateLoop(zone.outline);
    const zonePaths = [integerOuter(zone.outline)];
    validateQuantized(zonePaths);
    // Source containment is exact before unit conversion in stockForRecordedPiece.
    // The guarded zone may intentionally extend beyond material. Comparing an
    // outward circle approximation with an inward source would reject legal contact.
  }
}

function booleanDifference(a: Clipper.Paths, b: Clipper.Paths): Clipper.Paths {
  if (!a.length) return [];
  if (!b.length) return a;
  const clip = new Clipper.Clipper(),
    output: Clipper.Paths = [];
  clip.StrictlySimple = true;
  check(
    clip.AddPaths(a, Clipper.PolyType.ptSubject, true) && clip.AddPaths(b, Clipper.PolyType.ptClip, true),
    'invalid domain Boolean input.'
  );
  check(
    clip.Execute(
      Clipper.ClipType.ctDifference,
      output,
      Clipper.PolyFillType.pftNonZero,
      Clipper.PolyFillType.pftNonZero
    ),
    'domain difference failed.'
  );
  check(count(output) <= maxVertices, 'domain difference exceeds the vertex budget.');
  return sortedPaths(output);
}

export function prepareStockDomain(stock: DomainStock) {
  validateStockDomain(stock);
  const source = ownedFrozen(stock.domain);
  const material = [
    availableOuter(source.outer),
    ...source.holes.map(hole => canonicalPath(integerOuter(hole), false)),
  ];
  const offset = new Clipper.ClipperOffset();
  offset.AddPaths(material, Clipper.JoinType.jtSquare, Clipper.EndType.etClosedPolygon);
  const inward: Clipper.Paths = [];
  offset.Execute(inward, -Math.ceil((stock.margin + REMNANT_DOMAIN_RULES.numerics.boundaryProtectionMm) * SCALE));
  check(count(inward) <= REMNANT_DOMAIN_RULES.budgets.maxDomainVertices, 'inward domain exceeds its vertex budget.');
  const guardedZones = (stock.exclusions ?? []).flatMap(zone => guardedOuter(zone.outline, zone.clearance));
  check(count(guardedZones) <= maxVertices, 'guarded unavailable zones exceed their vertex budget.');
  const usable = booleanDifference(inward, guardedZones);
  check(count(usable) <= REMNANT_DOMAIN_RULES.budgets.maxDomainVertices, 'usable domain exceeds its vertex budget.');
  const prepared = prepareDomainBoundary(usable);
  const grossArea = loopArea(source.outer) - source.holes.reduce((n, hole) => n + loopArea(hole), 0);
  const protectedArea = filledArea(inward),
    usableArea = filledArea(usable);
  const tolerance = Math.max(
    REMNANT_DOMAIN_RULES.numerics.absoluteAreaToleranceMm2,
    grossArea * REMNANT_DOMAIN_RULES.numerics.relativeAreaTolerance
  );
  check(
    grossArea > 0 && protectedArea <= grossArea + tolerance && usableArea <= protectedArea + tolerance,
    'derived domain would overstate actual material.'
  );
  return Object.freeze({
    source,
    prepared,
    grossArea,
    protectedArea,
    usableArea,
    edgeAndProtectionArea: Math.max(0, grossArea - protectedArea),
    unavailableArea: Math.max(0, protectedArea - usableArea),
    inward: prepareDomainBoundary(inward).original.paths,
    usable: prepared.original.paths,
    guardedZones: prepareDomainBoundary(guardedZones).original.paths,
  });
}
export type PreparedStockDomain = ReturnType<typeof prepareStockDomain>;
