import type { ObservedShape, StockPieceEvidence } from '../../../types/stockPiece';
import { emptyEvidence } from '../../../validation/stockPiece';
import { CURRENT_GEOMETRY_PROFILE } from './geometry-profile';
import { prepareStockDomain, stockForRecordedPiece, type DomainStock } from './remnant-domain';
import { nestParts, rect, validateNest, type Part, type Loop, type Nest } from './nesting';
import { nominalDomainContains } from './nominal-domain';
import { prepareCompensatedGeometry } from './compensated-geometry';
import { prepareDomainCandidates } from './domain-candidates';
import { REMNANT_DOMAIN_RULES } from './remnant-domain-profile';

const inch = 25.4;
const points = (values: number[][]) => values.map(([x, y]) => ({ x: String(x), y: String(y) }));
const evidence = (geometry: ObservedShape): StockPieceEvidence => ({
  ...emptyEvidence(),
  geometry,
  thickness: '0.125',
  grade: 'A36',
});
const stock = (source: StockPieceEvidence, margin = 0.125, gap = 0.125) =>
  stockForRecordedPiece(source, {
    geometryProfile: CURRENT_GEOMETRY_PROFILE,
    margin: margin * inch,
    gap: gap * inch,
    zoneClearanceIn: '0',
  });
const rectangular = (width = '10', height = '8') => evidence({ kind: 'rectangle', width, height });
const part = (id: string, outer: Loop, quantity = 1): Part => ({
  id,
  name: id,
  quantity,
  rotate: true,
  color: 0,
  loops: [outer],
});
const plate = (id: string, width: number, height: number, quantity = 1) =>
  part(id, rect(width * inch, height * inch), quantity);
function account(parts: Part[], result: Nest) {
  expect(result.sheets).toBeLessThanOrEqual(1);
  expect(new Set(result.placements.map(p => `${p.partId}:${p.instance}`)).size).toBe(result.placements.length);
  for (const p of parts) {
    const placed = result.placements.filter(value => value.partId === p.id);
    expect(
      placed.length + result.unplaced.filter(value => value.partId === p.id).reduce((n, value) => n + value.count, 0)
    ).toBe(p.quantity);
    for (const value of placed) expect(value.instance).toBeLessThan(p.quantity);
  }
}
const minimumEdge = (s: DomainStock, p: Part) => s.margin + s.gap / 2 + (p.geometryToleranceMm ?? 0) + 0.0008;

test('analytic circle boundaries retain required margins for polygon and circle parts on one measured source', () => {
  const s = stock(evidence({ kind: 'circle', cx: '-50', cy: '20', r: '5' }));
  const parts = [
    plate('square', 1.5, 1, 5),
    part('disc', { type: 'circle', cx: inch / 2, cy: inch / 2, r: inch / 2 }, 6),
  ];
  const result = nestParts(parts, s),
    radius = 5 * inch;
  account(parts, result);
  expect(result.unplaced).toHaveLength(0);
  for (const placement of result.placements) {
    const p = parts.find(value => value.id === placement.partId)!;
    const limit = radius - minimumEdge(s, p);
    if (p.loops[0].type === 'circle') {
      const r = p.loops[0].r;
      expect(Math.hypot(placement.x + r - radius, placement.y + r - radius) + r).toBeLessThanOrEqual(limit + 1e-7);
    } else
      for (const x of [placement.x, placement.x + placement.width])
        for (const y of [placement.y, placement.y + placement.height])
          expect(Math.hypot(x - radius, y - radius)).toBeLessThanOrEqual(limit + 1e-7);
  }
  expect(() => validateNest(parts, s, result)).not.toThrow();
});

test('diagonal actual boundaries enforce perpendicular nominal distance, not an axis-aligned box', () => {
  const s = stock(
    evidence({
      kind: 'polygon',
      outer: points([
        [0, 0],
        [10, 0],
        [0, 10],
      ]),
      holes: [],
    })
  );
  const p = { ...plate('diagonal', 1.2, 0.8, 8), geometryToleranceMm: 0.0254 };
  const result = nestParts([p], s),
    minimum = minimumEdge(s, p);
  expect(result.unplaced).toHaveLength(0);
  for (const placement of result.placements) {
    expect(placement.x).toBeGreaterThanOrEqual(minimum - 1e-7);
    expect(placement.y).toBeGreaterThanOrEqual(minimum - 1e-7);
    expect(
      (10 * inch - placement.x - placement.width - placement.y - placement.height) / Math.SQRT2
    ).toBeGreaterThanOrEqual(minimum - 1e-7);
  }
  const forged = JSON.parse(JSON.stringify(result)) as Nest;
  forged.placements[0].x = 8 * inch;
  forged.placements[0].y = inch;
  expect(() => validateNest([p], s, forged)).toThrow(/envelope/);
});

test('an eroded neck retains both usable components but never creates a second physical piece', () => {
  const source = evidence({
    kind: 'polygon',
    outer: points([
      [0, 0],
      [4, 0],
      [4, 1.9],
      [6, 1.9],
      [6, 0],
      [10, 0],
      [10, 4],
      [6, 4],
      [6, 2.1],
      [4, 2.1],
      [4, 4],
      [0, 4],
    ]),
    holes: [],
  });
  const s = stock(source),
    p = plate('one-per-lobe', 3, 3, 3);
  expect(prepareStockDomain(s).usable).toHaveLength(2);
  const result = nestParts([p], s);
  expect(result.placements).toHaveLength(2);
  expect(result.placements.some(value => value.x < 4 * inch)).toBe(true);
  expect(result.placements.some(value => value.x > 6 * inch)).toBe(true);
  expect(result.unplaced).toEqual([
    expect.objectContaining({ partId: p.id, count: 1, reason: expect.stringMatching(/not proof/) }),
  ]);
  account([p], result);
});

test('a source-spanning unavailable zone leaves independent regions that share one physical capacity', () => {
  const source = rectangular('10', '6');
  source.unavailable_zones = [
    {
      id: 'damage',
      label: 'Reported strip',
      reason: 'Measured damage',
      outline: {
        kind: 'polygon',
        pts: points([
          [4, 0],
          [6, 0],
          [6, 6],
          [4, 6],
        ]),
      },
    },
  ];
  const s = stock(source),
    p = plate('plate', 3, 2, 5),
    result = nestParts([p], s);
  expect(result.placements).toHaveLength(4);
  expect(result.placements.every(value => value.x + value.width < 4 * inch || value.x > 6 * inch)).toBe(true);
  account([p], result);
});

test('part cutouts cannot silently reserve or reuse a physical source hole', () => {
  const source = evidence({
    kind: 'polygon',
    outer: points([
      [0, 0],
      [10, 0],
      [10, 10],
      [0, 10],
    ]),
    holes: [
      points([
        [4, 4],
        [6, 4],
        [6, 6],
        [4, 6],
      ]),
    ],
  });
  const p = plate('frame', 8, 8);
  p.loops.push({
    type: 'poly',
    points: [
      [2, 2],
      [6, 2],
      [6, 6],
      [2, 6],
    ].map(([x, y]) => ({ x: x * inch, y: y * inch })),
  });
  const result = nestParts([p], stock(source));
  expect(result.placements).toHaveLength(0);
  expect(result.unplaced[0]).toMatchObject({ count: 1, reason: expect.stringMatching(/bounded search/) });
});

test('zero requested edge margin keeps numerical protection and empty erosion stays an explicit incomplete result', () => {
  const p = plate('small', 0.2, 0.2, 2),
    s = stock(rectangular('1', '1'), 0, 0),
    result = nestParts([p], s);
  expect(result.unplaced).toHaveLength(0);
  for (const value of result.placements) {
    expect(value.x).toBeGreaterThanOrEqual(0.0008 - 1e-7);
    expect(value.y).toBeGreaterThanOrEqual(0.0008 - 1e-7);
  }
  const empty = nestParts([p], stock(rectangular('1', '1'), 2, 0));
  expect(empty.sheets).toBe(0);
  expect(empty.placements).toEqual([]);
  expect(empty.unplaced).toEqual([expect.objectContaining({ count: 2, reason: expect.stringMatching(/not proof/) })]);
});

test('measured source grain controls allowed rotation and cannot be replaced with a guessed full-sheet axis', () => {
  const source = rectangular();
  source.grain_axis = 'x';
  const p = { ...plate('grain', 1, 2, 3), grainAxis: 'y' as const, rotationMode: 'quarter-turn' as const };
  const result = nestParts([p], stock(source));
  expect(result.unplaced).toHaveLength(0);
  expect(result.placements.every(value => value.rotation === 90 || value.rotation === 270)).toBe(true);
  const locked = nestParts([{ ...p, rotationMode: 'fixed' }], stock(source));
  expect(locked.placements).toHaveLength(0);
  expect(locked.unplaced[0].reason).toMatch(/grain/);
  source.grain_axis = null;
  expect(nestParts([p], stock(source)).placements).toHaveLength(0);
});

test('deterministic repeated placement conserves every design/instance and preserves supplied source inputs', () => {
  const source = rectangular(),
    s = stock(source),
    parts = [plate('a', 1, 1, 12), plate('b', 2, 1, 4)];
  const before = JSON.stringify({ source, s, parts });
  const first = nestParts(parts, s),
    second = nestParts(parts, s);
  expect(second).toEqual(first);
  account(parts, first);
  expect(JSON.stringify({ source, s, parts })).toBe(before);
  const forged = JSON.parse(JSON.stringify(first)) as Nest;
  forged.placements[1].partId = forged.placements[0].partId;
  forged.placements[1].instance = forged.placements[0].instance;
  expect(() => validateNest(parts, s, forged)).toThrow(/instance/);
});

test('original-curve validator distinguishes real minimum distance from slightly undersized separation', () => {
  const outer: Loop = { type: 'circle', cx: 0, cy: 0, r: 10 };
  const inside: Loop = { type: 'circle', cx: 3, cy: 4, r: 4 };
  expect(nominalDomainContains(inside, outer, [], 1)).toBe(true);
  expect(nominalDomainContains(inside, outer, [], 1.00001)).toBe(false);
  const triangular: Loop = {
    type: 'poly',
    points: [
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 0, y: 10 },
    ],
  };
  expect(nominalDomainContains({ type: 'circle', cx: 2, cy: 2, r: 1 }, triangular, [], 1)).toBe(true);
  expect(nominalDomainContains({ type: 'circle', cx: 2, cy: 2, r: 1 }, triangular, [], 1.00001)).toBe(false);
});

test('candidate cache reuse is bounded and excessive distinct contexts fail visibly instead of inventing infeasibility', () => {
  const s = stock(rectangular()),
    p = plate('cache', 1, 1),
    prepared = prepareCompensatedGeometry([p], s)!;
  const shape = prepared.get(p, 0),
    range = prepared.originRange(shape)!,
    generator = prepareDomainCandidates(prepareStockDomain(s));
  const result = generator.generate(shape, range);
  for (let i = 0; i < 1500; i++) expect(generator.generate(shape, range)).toBe(result);
  expect(() => {
    for (let i = 0; i <= REMNANT_DOMAIN_RULES.budgets.maxCandidateCacheEntries; i++)
      generator.generate({ ...shape }, range);
  }).toThrow(/candidate cache exceeded its memory budget/);
});

test('complex interacting boundary sweeps report their resource limit instead of returning an unplaced claim', () => {
  const top = Array.from({ length: 601 }, (_, i) => ({
    x: String(Number((30 - i / 20).toFixed(2))),
    y: i % 2 ? '19.8' : '20',
  }));
  const source = evidence({ kind: 'polygon', outer: [{ x: '0', y: '0' }, { x: '30', y: '0' }, ...top], holes: [] });
  const s = stock(source, 0, 0),
    p = part('disc', { type: 'circle', cx: 2 * inch, cy: 2 * inch, r: 2 * inch });
  const geometry = prepareCompensatedGeometry([p], s)!,
    moving = geometry.get(p, 0),
    range = geometry.originRange(moving)!;
  expect(range).not.toBeNull();
  const candidates = prepareDomainCandidates(prepareStockDomain(s));
  expect(() => candidates.generate(moving, range)).toThrow(/boundary sweeps exceeded their input budget/);
});

test('synthetic benchmark: 1000-vertex measured outline and 30 repeated instances stay within the shared calculation budget', () => {
  const outer = Array.from({ length: 1000 }, (_, i) => {
    const angle = (2 * Math.PI * i) / 1000;
    return {
      x: String(Number((12 * Math.cos(angle)).toFixed(9))),
      y: String(Number((12 * Math.sin(angle)).toFixed(9))),
    };
  });
  const source = evidence({ kind: 'polygon', outer, holes: [] }),
    p = plate('repeated', 1, 1, 30);
  const start = performance.now(),
    s = stock(source),
    result = nestParts([p], s),
    elapsed = performance.now() - start;
  process.stdout.write(
    `\nRemnant synthetic benchmark: ${elapsed.toFixed(1)} ms, ${result.placements.length}/30 placed, 1000 source vertices.\n`
  );
  expect(elapsed).toBeLessThan(120000);
  account([p], result);
  expect(result.placements).toHaveLength(30);
}, 125000);
