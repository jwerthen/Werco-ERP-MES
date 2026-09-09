import { inToMm } from './units';
import {
  normalizePolicyDecimal,
  policyThicknessIn,
  resolveSpacingBand,
  validatePolicySnapshot,
  validateSpacingContent,
  type SpacingBand,
  type SpacingPolicySnapshot,
} from './spacing-policy';

const band: SpacingBand = {
  id: 'steel-1',
  material: 'Carbon steel',
  thickness_min_in: '0',
  thickness_max_in: '1',
  minimum_gap_in: '0.125',
  gap_thickness_multiplier: '1',
  minimum_margin_in: '0.375',
  margin_thickness_multiplier: '2',
};

test('decimal input normalization and half-up thickness are explicit', () => {
  expect(normalizePolicyDecimal(' 001.2500 ')).toBe('1.25');
  expect(policyThicknessIn(0.1250000005)).toBe('0.125000001');
  expect(policyThicknessIn(0.12500000049)).toBe('0.125');
  expect(policyThicknessIn(1e-9)).toBe('0.000000001');
  for (const value of [0, -1, Infinity, NaN, 1e-12, 5]) expect(() => policyThicknessIn(value)).toThrow();
  for (const value of ['1e-3', '-1', 'Infinity', '0.1234567891']) expect(() => normalizePolicyDecimal(value)).toThrow();
});

test('policy multiplication rounds upward and interval upper bounds are excluded', () => {
  expect(resolveSpacingBand(band, 'Carbon steel', 0.25)).toEqual({
    thickness_in: '0.25',
    gap_in: '0.25',
    margin_in: '0.5',
  });
  const tiny = { ...band, minimum_gap_in: '0', gap_thickness_multiplier: '0.333333333' };
  expect(resolveSpacingBand(tiny, 'Carbon steel', 0.1).gap_in).toBe('0.033333334');
  expect(() => resolveSpacingBand(band, 'Carbon steel', 1)).toThrow(/does not match/);
  expect(() => resolveSpacingBand(band, 'Aluminum', 0.1)).toThrow(/does not match/);
});

test('same-family overlaps and noncanonical constants are rejected; adjacent bands and gaps are allowed', () => {
  const content = { schema_version: 1, units: 'in', name: 'Synthetic test policy', bands: [band] };
  expect(validateSpacingContent(content)).toEqual(content);
  expect(() =>
    validateSpacingContent({
      ...content,
      bands: [band, { ...band, id: 'second', thickness_min_in: '0.5', thickness_max_in: '2' }],
    })
  ).toThrow(/overlap/);
  expect(() => validateSpacingContent({ ...content, bands: [{ ...band, minimum_gap_in: '0.1250' }] })).toThrow(/zeros/);
  expect(() =>
    validateSpacingContent({ ...content, bands: [{ ...band, minimum_gap_in: '0', gap_thickness_multiplier: '0' }] })
  ).toThrow(/positive/);
  expect(() =>
    validateSpacingContent({
      ...content,
      bands: [band, { ...band, id: 'second', thickness_min_in: '1', thickness_max_in: '2' }],
    })
  ).not.toThrow();
});

test('saved policy snapshots must reproduce the current group and spacing', () => {
  const source: SpacingPolicySnapshot = {
    schema_version: 1,
    company_id: 1,
    policy_id: 1,
    publication_id: 3,
    revision_id: 2,
    revision_number: 2,
    content_sha256: 'a'.repeat(64),
    band,
    ...resolveSpacingBand(band, 'Carbon steel', 0.125),
    resolved_at: '2026-09-08T20:00:00Z',
  };
  const check = (value: unknown, gap = 0.125) =>
    validatePolicySnapshot(value, 'Carbon steel', inToMm(0.125), inToMm(gap), inToMm(0.375));
  expect(check(source)).toEqual(source);
  expect(() => check({ ...source, gap_in: '0.1' })).toThrow(/formula/);
  expect(() => check(source, 0.25)).toThrow(/differs/);
  expect(() => check({ ...source, approved: true })).toThrow(/Invalid saved/);
  expect(() => check({ ...source, band: { ...band, material: 'Aluminum' } })).toThrow(/does not match/);
});
