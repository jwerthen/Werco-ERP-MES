import * as Clipper from 'clipper-lib';
import { convexPieces } from './convex-pieces';

const path = (coordinates: number[][]): Clipper.Path => coordinates.map(([X, Y]) => ({ X, Y }));
const area = (paths: Clipper.Paths) =>
  paths.reduce((total, polygon) => total + Math.abs(Clipper.Clipper.Area(polygon)), 0);
function clip(a: Clipper.Paths, b: Clipper.Paths, operation: Clipper.ClipType): Clipper.Paths {
  const engine = new Clipper.Clipper();
  engine.AddPaths(a, Clipper.PolyType.ptSubject, true);
  engine.AddPaths(b, Clipper.PolyType.ptClip, true);
  const result: Clipper.Paths = [];
  engine.Execute(operation, result, Clipper.PolyFillType.pftNonZero, Clipper.PolyFillType.pftNonZero);
  return result;
}
function verify(original: Clipper.Path, pieces: Clipper.Paths) {
  expect(area(clip(pieces, [original], Clipper.ClipType.ctXor))).toBe(0);
  const originalCoordinates = new Set(original.map(point => `${point.X},${point.Y}`));
  for (let i = 0; i < pieces.length; i++) {
    expect(Clipper.Clipper.Area(pieces[i])).toBeGreaterThan(0);
    for (let j = 0; j < pieces[i].length; j++) {
      const a = pieces[i][j],
        b = pieces[i][(j + 1) % pieces[i].length],
        c = pieces[i][(j + 2) % pieces[i].length];
      const side = BigInt(b.X - a.X) * BigInt(c.Y - b.Y) - BigInt(b.Y - a.Y) * BigInt(c.X - b.X);
      expect(side >= BigInt(0)).toBe(true);
      expect(originalCoordinates.has(`${a.X},${a.Y}`)).toBe(true);
    }
    for (let j = i + 1; j < pieces.length; j++) {
      expect(area(clip([pieces[i]], [pieces[j]], Clipper.ClipType.ctIntersection))).toBe(0);
    }
  }
}

const elbow = path([
  [0, 0],
  [100, 0],
  [100, 30],
  [30, 30],
  [30, 100],
  [0, 100],
]);
const cShape = path([
  [0, 0],
  [100, 0],
  [100, 20],
  [20, 20],
  [20, 80],
  [100, 80],
  [100, 100],
  [0, 100],
]);
function star(count: number): Clipper.Path {
  return Array.from({ length: count * 2 }, (_, i) => {
    const angle = (Math.PI * i) / count;
    const radius = i % 2 ? 350 + (i % 3) * 12 : 1000;
    return { X: Math.round(radius * Math.cos(angle)), Y: Math.round(radius * Math.sin(angle)) };
  });
}

describe('exact convex decomposition', () => {
  it.each([
    ['L', elbow, 2],
    ['C', cShape, 3],
  ] as const)('preserves a concave %s profile with few merged pieces', (_name, original, maximum) => {
    const before = JSON.stringify(original);
    const pieces = convexPieces(original);
    expect(pieces.length).toBeGreaterThan(1);
    expect(pieces.length).toBeLessThanOrEqual(maximum);
    verify(original, pieces);
    expect(JSON.stringify(original)).toBe(before);
  });

  it.each([5, 8, 17, 30])('preserves a %s-point star without overlapping interiors', count => {
    const original = star(count);
    const pieces = convexPieces(original);
    expect(pieces.length).toBeGreaterThan(1);
    verify(original, pieces);
  });

  it('returns an already convex polygon directly, including many rounded integer vertices', () => {
    const original = Array.from({ length: 1000 }, (_, index) => ({
      X: Math.round(100_000_000 * Math.cos((2 * Math.PI * index) / 1000)),
      Y: Math.round(100_000_000 * Math.sin((2 * Math.PI * index) / 1000)),
    }));
    const pieces = convexPieces(original);
    expect(pieces).toHaveLength(1);
    expect(pieces[0]).toBe(original);
    verify(original, pieces);
  });

  it('retains a positive turn with determinant one at large coordinates', () => {
    const original = path([
      [0, 0],
      [199999999, 199999998],
      [200000000, 199999999],
      [0, 200000000],
    ]);
    const pieces = convexPieces(original);
    expect(pieces[0]).toBe(original);
    expect(pieces[0]).toHaveLength(4);
    verify(original, pieces);
  });

  it('returns a convex path with collinear boundary points directly', () => {
    const original = path([
      [0, 0],
      [50, 0],
      [100, 0],
      [100, 100],
      [0, 100],
    ]);
    const pieces = convexPieces(original);
    expect(pieces[0]).toBe(original);
    verify(original, pieces);
  });

  it('cleans redundant boundary vertices without changing a concave polygon', () => {
    const original = path([
      [0, 0],
      [50, 0],
      [100, 0],
      [100, 0],
      [100, 30],
      [30, 30],
      [30, 100],
      [0, 100],
      [0, 0],
    ]);
    const before = JSON.stringify(original);
    const pieces = convexPieces(original);
    verify(original, pieces);
    expect(JSON.stringify(original)).toBe(before);
  });

  it('handles translated and rotated integer profiles', () => {
    const original = cShape.map(point => ({ X: 150_000_000 - point.Y * 300, Y: -150_000_000 + point.X * 300 }));
    verify(original, convexPieces(original));
  });

  it.each(
    [
      path([
        [0, 0],
        [10, 0],
        [20, 0],
      ]),
      path([
        [0, 0],
        [10, 0],
        [5, 0],
        [5, 10],
        [0, 10],
      ]),
      path([
        [0, 0],
        [10, 10],
        [0, 10],
        [10, 0],
      ]),
      path([
        [0, 0],
        [20, 0],
        [20, 20],
        [10, 0],
        [0, 20],
      ]),
      [...elbow].reverse(),
    ].map(original => [original] as const)
  )('rejects degenerate, self-touching, or negative boundary %#', original => {
    expect(() => convexPieces(original)).toThrow();
  });

  it('rejects fractional, nonfinite, oversized, and unbounded input', () => {
    expect(() =>
      convexPieces(
        path([
          [0, 0],
          [1.5, 0],
          [0, 1],
        ])
      )
    ).toThrow(/integer/);
    expect(() =>
      convexPieces(
        path([
          [0, 0],
          [NaN, 0],
          [0, 1],
        ])
      )
    ).toThrow(/integer/);
    expect(() =>
      convexPieces(
        path([
          [0, 0],
          [200000001, 0],
          [0, 1],
        ])
      )
    ).toThrow(/integer/);
    expect(() => convexPieces(Array.from({ length: 2001 }, (_, i) => ({ X: i, Y: i })))).toThrow(/2,000/);
  });
});
