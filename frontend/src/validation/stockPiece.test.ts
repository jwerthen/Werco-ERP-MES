import { canonicalInches, emptyEvidence, stockPieceEvidenceSchema } from './stockPiece';

describe('reported observation inches', () => {
  it.each([
    ['1 1/8', '1.125'],
    ['.125', '0.125'],
    ['-.5', '-0.5'],
    ['006.000', '6'],
    ['-0.000', '0'],
    ['1/64', '0.015625'],
    ['0.000000001', '0.000000001'],
  ])('canonicalizes %s exactly', (input, expected) => {
    expect(canonicalInches(input)).toBe(expected);
  });
  it.each(['1/3', '1/0', '1e2', 'NaN', '0.0000000001', '100000.000000001'])(
    'refuses unrepresentable or out-of-budget %s',
    input => {
      expect(() => canonicalInches(input)).toThrow();
    }
  );
  it('preserves negative coordinates while refusing nonpositive dimensions', () => {
    expect(canonicalInches('-1 1/8')).toBe('-1.125');
    expect(() => canonicalInches('0', true)).toThrow();
    expect(() => canonicalInches('-1', true)).toThrow();
  });
  it('does not infer unknown evidence and normalizes reported decimal strings without a float conversion', () => {
    const input = {
      ...emptyEvidence(),
      measurement_method: ' Tape ',
      geometry: { kind: 'rectangle', width: '12.000', height: '1 1/8' },
      thickness: '.125',
    };
    expect(stockPieceEvidenceSchema.parse(input)).toEqual({
      ...emptyEvidence(),
      measurement_method: 'Tape',
      geometry: { kind: 'rectangle', width: '12', height: '1.125' },
      thickness: '0.125',
    });
    expect(input.thickness).toBe('.125');
  });
  it('enforces aggregate point budget and requires a known outline before positioning zones', () => {
    const zone = {
      id: 'defect',
      label: 'Reported defect',
      reason: 'Measured during review',
      outline: { kind: 'circle', cx: '1', cy: '1', r: '0.5' },
    };
    expect(
      stockPieceEvidenceSchema.safeParse({ ...emptyEvidence(), measurement_method: 'Tape', unavailable_zones: [zone] })
        .success
    ).toBe(false);
    expect(
      stockPieceEvidenceSchema.safeParse({
        ...emptyEvidence(),
        measurement_method: 'Tape',
        geometry: { kind: 'polygon', outer: Array.from({ length: 2000 }, () => ({ x: '0', y: '0' })), holes: [] },
        unavailable_zones: [zone],
      }).success
    ).toBe(false);
  });
});
