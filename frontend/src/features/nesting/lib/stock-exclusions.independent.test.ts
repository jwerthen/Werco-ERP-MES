import { envelopesOverlap } from './guarded-geometry';
import { createBlankProject, projectFromFile, projectToFile } from './quote-project';
import { createBlankQuote, quoteFromFile, quoteToFile } from './quoting';
import { policySnapshot } from '../../../test-utils/nestingPolicyFixtures';
import { inToMm } from './units';
import type { StockExclusion } from './stock-exclusions';

test('sub-grid triangular intersection remains blocked at a large coordinate offset', () => {
  const n = 100_000_000;
  // Independent analytic area: (N-1)/(2N), positive but below half an integer
  // grid square. Rounded polygon intersection alone can lose this overlap.
  expect((n - 1) / (2 * n)).toBeGreaterThan(0);
  expect((n - 1) / (2 * n)).toBeLessThan(0.5);
  for (const origin of [0, 100_000_000]) {
    const point = (x: number, y: number) => ({ X: x + origin, Y: y + origin });
    const a = [[point(0, 0), point(n, 0), point(n, 1)]];
    const b = [[point(n - 1, 0), point(n, 1), point(n - 1, 1)]];
    expect(envelopesOverlap(a, b)).toBe(true);
  }
});

test('pathological exact envelope comparisons fail visibly instead of treating budget exhaustion as clear stock', () => {
  const ring = (offset: number) =>
    Array.from({ length: 3000 }, (_, index) => ({
      X: offset + Math.round(100_000 * Math.cos((index * Math.PI * 2) / 3000)),
      Y: offset + Math.round(100_000 * Math.sin((index * Math.PI * 2) / 3000)),
    }));
  // Separate overall bounds require no edge comparisons. Diagonally separated
  // circles have intersecting bounds but disjoint interiors (sqrt(2)*150k > 200k).
  expect(envelopesOverlap([ring(0)], [ring(1_000_000)])).toBe(false);
  expect(Math.hypot(150_000, 150_000)).toBeGreaterThan(200_000);
  expect(() => envelopesOverlap([ring(0)], [ring(150_000)])).toThrow(/edge-pair budget/);
});

test('raw envelope comparisons observe changed coordinates rather than reuse stale cached bounds', () => {
  const square = (x: number) => [
    { X: x, Y: 0 },
    { X: x + 10, Y: 0 },
    { X: x + 10, Y: 10 },
    { X: x, Y: 10 },
  ];
  const a = [square(0)],
    b = [square(20)];
  expect(envelopesOverlap(a, b)).toBe(false);
  b[0].forEach(point => {
    point.X -= 20;
  });
  expect(envelopesOverlap(a, b)).toBe(true);
  b[0].forEach(point => {
    point.X += 40;
  });
  expect(envelopesOverlap(a, b)).toBe(false);
});

test('an exclusion-bearing estimate retains an applied policy and deep-clones exact source geometry', () => {
  const region: StockExclusion = {
    id: 'source',
    label: 'Synthetic unavailable triangle',
    reason: 'Estimator report',
    clearance: 0.1,
    outline: {
      type: 'poly',
      points: [
        { x: 15.123456789, y: 1 },
        { x: 20, y: 1 },
        { x: 16, y: 8 },
      ],
    },
  };
  const quote = {
    ...createBlankQuote(),
    spacingMode: 'policy' as const,
    spacingPolicy: policySnapshot,
    gap: inToMm(0.125),
    margin: inToMm(0.375),
    options: [{ id: 'stock', enabled: true, width: 100, height: 100, price: null, exclusions: [region] }],
  };
  const first = createBlankProject(quote),
    second = createBlankProject(quote);
  const saved = projectToFile(first);
  expect(saved.version).toBe(12);
  expect(saved.groups[0].quote.version).toBe(11);
  const reopened = projectFromFile(saved);
  expect(reopened.groups[0].quote.spacingPolicy).toEqual(policySnapshot);
  expect(reopened.groups[0].quote.spacingMode).toBe('policy');
  const copied = first.groups[0].quote.options[0].exclusions![0].outline;
  if (copied.type !== 'poly') throw new Error('Expected the exact source polygon');
  copied.points[0].x = 15.5;
  expect(second.groups[0].quote.options[0].exclusions![0]).toEqual(region);
  expect(quote.options[0].exclusions[0]).toBe(region);
  // Declaring an older quote must never suppress its policy/exclusion fields.
  const file = quoteToFile(second.groups[0].quote);
  expect(() => quoteFromFile({ ...file, version: 9 })).toThrow(/version 11/);
});
