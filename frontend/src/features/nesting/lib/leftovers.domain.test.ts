import type { ObservedShape, StockPieceEvidence } from '../../../types/stockPiece';
import { emptyEvidence } from '../../../validation/stockPiece';
import { CURRENT_GEOMETRY_PROFILE } from './geometry-profile';
import { stockForRecordedPiece } from './remnant-domain';
import { REMNANT_DOMAIN_RULES } from './remnant-domain-profile';
import {
  analyzeLeftovers,
  leftoversToFile,
  DOMAIN_AREA_DEFINITIONS,
  LEFTOVER_DOMAIN_PROFILE,
  type LeftoverAnalysis,
} from './leftovers';
import { bounds, rect, type Nest, type Part, type Point } from './nesting';

const inches2 = 25.4 ** 2;
const points = (pairs: number[][]) => pairs.map(([x, y]) => ({ x: String(x), y: String(y) }));
function fixture(geometry: ObservedShape = { kind: 'rectangle', width: '10', height: '10' }, x = 10, y = 10) {
  const evidence: StockPieceEvidence = {
    ...emptyEvidence(),
    geometry,
    thickness: '0.125',
    grade: 'A36',
  };
  const parts: Part[] = [
    {
      id: 'ring-plate',
      name: 'Synthetic part with reserved hole',
      quantity: 1,
      color: 0,
      rotate: false,
      loops: [rect(20, 10), { type: 'circle', cx: 10, cy: 5, r: 2 }],
    },
  ];
  const stock = stockForRecordedPiece(evidence, {
    geometryProfile: CURRENT_GEOMETRY_PROFILE,
    gap: 3.175,
    margin: 3.175,
    zoneClearanceIn: '0',
  });
  const nest: Nest = {
    placements: [{ partId: parts[0].id, instance: 0, sheet: 0, x, y, width: 20, height: 10, rotation: 0 }],
    unplaced: [],
    sheets: 1,
    area: 200 - Math.PI * 4,
    utilization: 1,
    method: 'Independent synthetic placement',
  };
  return { evidence, parts, stock, nest };
}
function balanced(analysis: LeftoverAnalysis) {
  expect(analysis).toMatchObject({ version: 'werco-leftovers-v4', status: 'potential_review_only', creditUSD: 0 });
  expect(analysis.assumptions.profile).toEqual(LEFTOVER_DOMAIN_PROFILE);
  expect(analysis.assumptions.areaDefinitions).toEqual(DOMAIN_AREA_DEFINITIONS);
  for (const row of analysis.sheets) {
    expect(row.protectedArea).toBeDefined();
    expect(row.grossArea).toBeCloseTo(row.edgeMarginArea + row.protectedArea!, 8);
    expect(row.protectedArea).toBeCloseTo(row.excludedArea! + row.usableArea, 8);
    expect(row.grossArea).toBeCloseTo(
      row.edgeMarginArea +
        row.excludedArea! +
        row.nominalPartArea +
        row.reservedCutoutArea +
        row.clearanceAndProtectionArea +
        row.remainingArea,
      8
    );
    expect(row.clearanceAndProtectionArea).toBeGreaterThanOrEqual(0);
    expect(row.remainingArea).toBeCloseTo(
      row.regions.reduce((sum, region) => sum + region.area, 0),
      8
    );
    expect(Math.abs(row.reconciliationResidualArea)).toBeLessThan(1e-7);
    for (const region of row.regions) expect(region).toMatchObject({ classification: 'review', creditUSD: 0 });
  }
}

test('concave reported material and physical holes have an analytical gross ledger; part holes remain reserved', () => {
  const value = fixture({
    kind: 'polygon',
    outer: points([
      [0, 0],
      [10, 0],
      [10, 4],
      [4, 4],
      [4, 10],
      [0, 10],
    ]),
    holes: [
      points([
        [1, 1],
        [2, 1],
        [2, 2],
        [1, 2],
      ]),
    ],
  });
  const before = JSON.stringify(value);
  const result = analyzeLeftovers(value.parts, value.stock, value.nest);
  balanced(result);
  expect(result.sheets[0].grossArea).toBeCloseTo(63 * inches2, 8);
  expect(result.sheets[0].nominalPartArea).toBeCloseTo(200 - Math.PI * 4, 10);
  expect(result.sheets[0].reservedCutoutArea).toBeCloseTo(Math.PI * 4, 10);
  expect(result.sheets[0].excludedArea).toBe(0);
  const saved = leftoversToFile(result, value);
  expect(saved.sheets[0]).toHaveProperty('grossAreaIn2', result.sheets[0].grossArea / inches2);
  expect(saved.sheets[0]).toHaveProperty('excludedAreaIn2', 0);
  expect(saved.sheets[0]).toHaveProperty('protectedAreaIn2', result.sheets[0].protectedArea! / inches2);
  expect(saved.sheets[0].regions.flatMap(region => region.holes).length).toBeGreaterThan(0);
  expect(JSON.stringify(value)).toBe(before);
});

test('a circular piece keeps its actual analytical area and a conservatively inset vector remainder', () => {
  const value = fixture({ kind: 'circle', cx: '-12', cy: '4', r: '5' }, 120, 120);
  const result = analyzeLeftovers(value.parts, value.stock, value.nest);
  balanced(result);
  const row = result.sheets[0];
  expect(row.grossArea).toBeCloseTo(Math.PI * 25 * inches2, 8);
  const radius = 5 * 25.4 - value.stock.margin;
  const guard =
    REMNANT_DOMAIN_RULES.numerics.boundaryProtectionMm + REMNANT_DOMAIN_RULES.numerics.circleRadialErrorMm + 0.0002;
  expect(row.protectedArea!).toBeLessThan(Math.PI * radius ** 2);
  expect(row.protectedArea!).toBeGreaterThan(Math.PI * (radius - guard) ** 2);
  expect(leftoversToFile(result, value).inputBinding).toBe('matched_current_layout');
});

test('overlapping unavailable zones are unioned once and never subtracted twice from final usable area', () => {
  const value = fixture();
  const zone = {
    id: 'damage-1',
    label: 'Reported damage',
    reason: 'Synthetic observation',
    outline: {
      kind: 'polygon' as const,
      pts: points([
        [4, 4],
        [6, 4],
        [6, 6],
        [4, 6],
      ]),
    },
  };
  const analyze = (duplicate: boolean) => {
    const stock = stockForRecordedPiece(
      { ...value.evidence, unavailable_zones: duplicate ? [zone, { ...zone, id: 'damage-2' }] : [zone] },
      {
        geometryProfile: CURRENT_GEOMETRY_PROFILE,
        gap: value.stock.gap,
        margin: value.stock.margin,
        zoneClearanceIn: '0',
      }
    );
    return { stock, report: analyzeLeftovers(value.parts, stock, value.nest) };
  };
  const one = analyze(false),
    two = analyze(true);
  balanced(two.report);
  expect(two.report.sheets[0]).toEqual(one.report.sheets[0]);
  expect(two.report.sheets[0].excludedArea!).toBeGreaterThan(4 * inches2);
  expect(two.report.sheets[0].excludedArea!).toBeLessThan(4.01 * inches2);
  expect(leftoversToFile(two.report, { ...value, stock: two.stock }).sheets[0]).toHaveProperty(
    'excludedAreaIn2',
    two.report.sheets[0].excludedArea! / inches2
  );
});

test('an edge-to-edge unavailable divider retains disconnected actual leftover components', () => {
  const value = fixture();
  value.stock = stockForRecordedPiece(
    {
      ...value.evidence,
      unavailable_zones: [
        {
          id: 'divider',
          label: 'Damaged strip',
          reason: 'Synthetic full-span observation',
          outline: {
            kind: 'polygon',
            pts: points([
              [4, 0],
              [5, 0],
              [5, 10],
              [4, 10],
            ]),
          },
        },
      ],
    },
    {
      geometryProfile: CURRENT_GEOMETRY_PROFILE,
      gap: value.stock.gap,
      margin: value.stock.margin,
      zoneClearanceIn: '0',
    }
  );
  const report = analyzeLeftovers(value.parts, value.stock, value.nest);
  balanced(report);
  expect(report.sheets[0].regions).toHaveLength(2);
  expect(leftoversToFile(report, value).sheets[0].regions).toHaveLength(2);
});

test('v4 requires original context and rejects a downgrade, altered assumptions or source', () => {
  const value = fixture();
  const report = analyzeLeftovers(value.parts, value.stock, value.nest);
  expect(() => leftoversToFile(report)).toThrow(/context/);
  expect(() => leftoversToFile({ ...report, version: 'werco-leftovers-v3' }, value)).toThrow();
  const altered = JSON.parse(JSON.stringify(report)) as LeftoverAnalysis;
  Object.assign(altered.assumptions.areaDefinitions!, { usableArea: 'Bounding rectangle' });
  expect(() => leftoversToFile(altered, value)).toThrow(/assumptions/);
  const stock = stockForRecordedPiece(
    { ...value.evidence, geometry: { kind: 'rectangle', width: '12.5', height: '8' } },
    {
      geometryProfile: CURRENT_GEOMETRY_PROFILE,
      gap: value.stock.gap,
      margin: value.stock.margin,
      zoneClearanceIn: '0',
    }
  );
  expect(() => leftoversToFile(report, { ...value, stock })).toThrow(/binding/);
});

test('export rejects a rewritten in-domain region covering a part even when area and bounds stay consistent', () => {
  const value = fixture();
  const report = analyzeLeftovers(value.parts, value.stock, value.nest);
  const bad = JSON.parse(JSON.stringify(report)) as LeftoverAnalysis;
  // The inner empty ring is the part envelope. Move it to another in-domain spot;
  // all areas and gross/usable ledger fields still match, but the part is now covered.
  const region = bad.sheets[0].regions.find(item => item.holes.length > 0)!;
  region.holes = region.holes.map(hole => hole.map(p => ({ x: p.x + 60, y: p.y + 60 })));
  expect(() => leftoversToFile(bad, value)).toThrow(/regenerated remainder/);
});

test('export rejects outside/concavity geometry and ledger substitutions without repairing evidence', () => {
  const value = fixture();
  const report = analyzeLeftovers(value.parts, value.stock, value.nest);
  const bad = JSON.parse(JSON.stringify(report)) as LeftoverAnalysis;
  const translate = (p: Point) => ({ x: p.x + 1000, y: p.y });
  for (const region of bad.sheets[0].regions) {
    region.outer = region.outer.map(translate);
    region.holes = region.holes.map(hole => hole.map(translate));
    region.bounds = bounds({ type: 'poly', points: region.outer });
  }
  expect(() => leftoversToFile(bad, value)).toThrow(/regenerated remainder/);
  bad.sheets[0].protectedArea = undefined;
  expect(() => leftoversToFile(bad, value)).toThrow();
});

test('fully unplaced work does not manufacture a used physical piece or leftover region', () => {
  const value = fixture();
  value.nest = {
    ...value.nest,
    sheets: 0,
    placements: [],
    unplaced: [{ partId: value.parts[0].id, count: 1, reason: 'Search did not place instance' }],
  };
  const report = analyzeLeftovers(value.parts, value.stock, value.nest);
  expect(report).toMatchObject({ version: 'werco-leftovers-v4', sheets: [] });
  expect(leftoversToFile(report, value).sheets).toEqual([]);
});
