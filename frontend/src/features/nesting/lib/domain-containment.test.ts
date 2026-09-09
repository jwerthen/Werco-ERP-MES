import type * as Clipper from 'clipper-lib';
import { canonicalPath } from './guarded-geometry';
import { domainContains, prepareDomainBoundary } from './domain-containment';

const path = (points: number[][], positive = true): Clipper.Path =>
  canonicalPath(
    points.map(([X, Y]) => ({ X, Y })),
    positive
  );
const rect = (x: number, y: number, width: number, height: number, positive = true) =>
  path(
    [
      [x, y],
      [x + width, y],
      [x + width, y + height],
      [x, y + height],
    ],
    positive
  );
const contains = (p: Clipper.Paths, u: Clipper.Paths, dx = 0, dy = 0) =>
  domainContains(prepareDomainBoundary(p), prepareDomainBoundary(u), dx, dy);

test('permits exact boundary contact and rejects one grid unit outside', () => {
  const p = [rect(0, 0, 4, 4)],
    u = [rect(0, 0, 10, 10)];
  expect(contains(p, u)).toBe(true);
  expect(contains(p, u, 6, 6)).toBe(true);
  expect(contains(p, u, 7, 6)).toBe(false);
  expect(contains(p, u, -1, 0)).toBe(false);
  expect(contains([], u)).toBe(true);
  expect(contains(p, [])).toBe(false);
});

test('a covered triangular hole is rejected even when all hole vertices touch the part boundary', () => {
  const u = [
    rect(0, 0, 10, 10),
    path(
      [
        [4, 4],
        [6, 4],
        [5, 6],
      ],
      false
    ),
  ];
  expect(contains([rect(2, 4, 6, 2)], u)).toBe(false);
  expect(contains([rect(0, 0, 4, 4)], u)).toBe(true);
});

test('split boundary intervals catch a bridge between disconnected usable components', () => {
  const u = [rect(0, 0, 2, 4), rect(3, 0, 7, 4)];
  // All four P vertices are in U; both long-edge whole midpoints are also in U.
  expect(contains([rect(0, 0, 10, 4)], u)).toBe(false);
  expect(contains([rect(3, 0, 7, 4)], u)).toBe(true);
});

test('an offset hole remains a hole, and can surround an unavailable inner region', () => {
  const u = [rect(0, 0, 20, 20), rect(8, 8, 4, 4, false)];
  expect(contains(u, u)).toBe(true);
  expect(contains([rect(2, 2, 16, 16), rect(6, 6, 8, 8, false)], u)).toBe(true);
  expect(contains([rect(2, 2, 16, 16), rect(9, 9, 2, 2, false)], u)).toBe(false);
});

test('detects an analytically positive sub-grid sliver even at large coordinate offsets', () => {
  const n = 100_000_000;
  expect((n - 1) / (2 * n)).toBeGreaterThan(0);
  expect((n - 1) / (2 * n)).toBeLessThan(0.5);
  for (const origin of [0, 100_000_000]) {
    const shifted = (points: number[][], positive = true) =>
      path(
        points.map(([x, y]) => [x + origin, y + origin]),
        positive
      );
    const a = shifted([
      [0, 0],
      [n, 0],
      [n, 1],
    ]);
    const hole = shifted(
      [
        [n - 1, 0],
        [n, 1],
        [n - 1, 1],
      ],
      false
    );
    expect(contains([a], [rect(origin - 10, origin - 10, n + 20, 21), hole])).toBe(false);
  }
});

test('reflex-vertex contact cannot allow a rectangle to span the missing corner', () => {
  const l = path([
    [0, 0],
    [10, 0],
    [10, 4],
    [4, 4],
    [4, 10],
    [0, 10],
  ]);
  expect(contains([rect(0, 0, 4, 10)], [l])).toBe(true);
  expect(contains([rect(0, 0, 10, 10)], [l])).toBe(false);
  expect(
    contains(
      [
        path([
          [0, 0],
          [10, 0],
          [0, 10],
        ]),
      ],
      [l]
    )
  ).toBe(false);
});

test('prepared boundaries own their coordinates and support exact translated origins', () => {
  const p = [rect(0, 0, 3, 3)],
    u = [rect(100, 200, 10, 10)];
  const preparedP = prepareDomainBoundary(p),
    preparedU = prepareDomainBoundary(u);
  p[0][0].X = 999;
  expect(domainContains(preparedP, preparedU, 100, 200)).toBe(true);
  expect(() => domainContains(preparedP, preparedU, 100.5, 200)).toThrow(/grid integers/);
  expect(() => prepareDomainBoundary([rect(2_000_000_000, 0, 10, 10)])).toThrow(/precision/);
});

test('budget exhaustion is explicit and cannot approve a placement', () => {
  const p = prepareDomainBoundary([rect(1, 1, 4, 4)]),
    u = prepareDomainBoundary([rect(0, 0, 10, 10)]);
  expect(() => domainContains(p, u, 0, 0, 2)).toThrow(/work budget/);
});
