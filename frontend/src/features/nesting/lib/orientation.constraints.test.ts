import {
  nestParts,
  rect,
  validateNest,
  validatePart,
  validateStock,
  type Loop,
  type Part,
  type Stock,
} from './nesting';
import { compareSheets, createBlankQuote } from './quoting';

const elbow: Loop = {
  type: 'poly',
  points: [
    { x: 0, y: 0 },
    { x: 60, y: 0 },
    { x: 60, y: 10 },
    { x: 20, y: 10 },
    { x: 20, y: 50 },
    { x: 0, y: 50 },
  ],
};
const part = (outer: Loop = rect(20, 10)): Part => ({
  id: 'plate',
  name: 'Test plate',
  loops: [outer],
  quantity: 1,
  rotate: true,
  color: 0,
});
const stock = (width = 200, height = 200): Stock => ({
  width,
  height,
  bedWidth: width,
  bedHeight: height,
  gap: 0,
  margin: 0,
  maxSheets: 1,
});

describe('physical rotation and grain constraints', () => {
  it('uses half turns to interlock asymmetric profiles where fixed orientation cannot cover the order', () => {
    const fixed = { ...part(elbow), quantity: 2, rotationMode: 'fixed' as const };
    const sheet = { ...stock(87.5, 57.5), margin: 2.5, gap: 2.5 };
    const locked = nestParts([fixed], sheet);
    expect(locked.placements).toHaveLength(1);
    expect(locked.unplaced).toEqual([{ partId: fixed.id, count: 1, reason: expect.any(String) }]);
    // The 20 mm vertical legs sit on opposite sides after a half turn. Their
    // 60 x 50 envelopes overlap, while the actual profiles have a 2.5 mm gap.
    const halfTurn = { ...fixed, rotate: false, rotationMode: 'half-turn' as const };
    const result = nestParts([halfTurn], sheet);
    expect(result.unplaced).toEqual([]);
    expect(result.placements).toHaveLength(2);
    expect(result.placements.map(placement => placement.rotation).sort((a, b) => a - b)).toEqual([0, 180]);
    const [a, b] = result.placements;
    expect(a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height).toBe(true);
    expect(result.area).toBeCloseTo(2800);
    expect(() => validateNest([halfTurn], sheet, result)).not.toThrow();
  });

  it.each([
    ['square', rect(20, 20)],
    ['analytic circle', { type: 'circle', cx: 10, cy: 10, r: 10 }],
  ] as const)('keeps cross-grain parity for a %s despite symmetric geometry', (_name, outer) => {
    const plate: Part = { ...part(outer), rotate: false, rotationMode: 'quarter-turn', grainAxis: 'x' };
    const sheet: Stock = { ...stock(25, 25), grainAxis: 'y' };
    const result = nestParts([plate], sheet);
    expect(result.unplaced).toEqual([]);
    expect(result.placements).toHaveLength(1);
    expect([90, 270]).toContain(result.placements[0].rotation);
    expect(result.placements[0]).toMatchObject({ width: 20, height: 20 });
    expect(() => validateNest([plate], sheet, result)).not.toThrow();
  });

  it.each([
    ['rectangular fast path', rect(20, 10)],
    ['concave placement', elbow],
  ] as const)('accounts for blocked grain instances while placing unrestricted parts in %s', (_name, outer) => {
    const required: Part = { ...part(outer), grainAxis: 'x', quantity: 2 };
    const unrestricted = { ...part(), id: 'unrestricted' };
    const result = nestParts([required, unrestricted], stock());
    expect(result.placements.map(placement => placement.partId)).toEqual(['unrestricted']);
    expect(result.unplaced).toEqual([
      { partId: required.id, count: 2, reason: expect.stringMatching(/sheet.*grain|grain.*sheet/i) },
    ]);
    expect(result.sheets).toBe(1);
    expect(() => validateNest([required, unrestricted], stock(), result)).not.toThrow();
  });

  it.each(['fixed', 'half-turn'] as const)('cannot satisfy perpendicular grain under %s policy', rotationMode => {
    const plate: Part = { ...part(), rotationMode, grainAxis: 'x' };
    const result = nestParts([plate], { ...stock(), grainAxis: 'y' });
    expect(result.placements).toEqual([]);
    expect(result.unplaced).toEqual([{ partId: plate.id, count: 1, reason: expect.stringMatching(/grain|rotation/i) }]);
    expect(result.sheets).toBe(0);
    const comparison = compareSheets({ ...createBlankQuote(), grainAxis: 'y', parts: [plate] });
    expect(comparison.recommendedId).toBeNull();
    expect(comparison.reason).toMatch(/permitted rotations cannot align/i);
  });

  it('preserves legacy boolean rotation behavior while an explicit policy takes precedence', () => {
    const sidewaysSheet = stock(10, 20);
    expect(nestParts([part()], sidewaysSheet).unplaced).toEqual([]);
    expect(nestParts([{ ...part(), rotate: false }], sidewaysSheet).placements).toEqual([]);
    expect(nestParts([{ ...part(), rotationMode: 'fixed' }], sidewaysSheet).placements).toEqual([]);
    const explicit = nestParts([{ ...part(), rotate: false, rotationMode: 'quarter-turn' }], sidewaysSheet);
    expect(explicit.unplaced).toEqual([]);
    expect([90, 270]).toContain(explicit.placements[0].rotation);
  });

  it.each([
    ['fixed policy', { rotationMode: 'fixed' }, {}, 180],
    ['half-turn policy', { rotationMode: 'half-turn' }, {}, 90],
    ['grain parity', { grainAxis: 'x' }, { grainAxis: 'y' }, 0],
    ['unspecified sheet grain', { grainAxis: 'x' }, {}, 0],
  ] as const)(
    'rejects a forged placement that violates %s even when all geometry fits',
    (_name, restriction, sheetGrain, rotation) => {
      const plate = part(rect(20, 20));
      const sheet = stock(30, 30);
      const cached = nestParts([plate], sheet);
      cached.placements[0].rotation = rotation;
      expect(() => validateNest([{ ...plate, ...restriction }], { ...sheet, ...sheetGrain }, cached)).toThrow(
        /grain|rotation/i
      );
    }
  );

  it('never recommends a complete quote when required sheet grain is unknown', () => {
    const quote = { ...createBlankQuote(), parts: [{ ...part(), grainAxis: 'x' as const }] };
    const result = compareSheets(quote);
    expect(result.recommendedId).toBeNull();
    expect(result.reason).toMatch(/sheet grain is unknown/i);
    expect(result.results.every(option => !option.complete && option.nest?.placements.length === 0)).toBe(true);
    for (const option of result.results) expect(option.nest?.unplaced[0].reason).toMatch(/sheet.*grain|grain.*sheet/i);
  });

  it('rejects unsupported policies and grain axes instead of silently allowing unrestricted placement', () => {
    expect(() => validatePart({ ...part(), rotationMode: 'any-angle' } as unknown as Part)).toThrow(/rotation/i);
    expect(() => validatePart({ ...part(), grainAxis: 'diagonal' } as unknown as Part)).toThrow(/grain/i);
    expect(() => validateStock({ ...stock(), grainAxis: 'diagonal' } as unknown as Stock)).toThrow(/grain/i);
  });
});
