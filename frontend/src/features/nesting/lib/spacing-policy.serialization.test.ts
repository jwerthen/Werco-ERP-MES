import { createBlankQuote, quoteFromFile, quoteToFile } from './quoting';
import { createBlankProject, projectFromFile, projectToFile } from './quote-project';
import { inToMm } from './units';
import { resolveSpacingBand, type SpacingBand, type SpacingPolicySnapshot } from './spacing-policy';

const band: SpacingBand = {
  id: 'fixture',
  material: 'Carbon steel',
  thickness_min_in: '0',
  thickness_max_in: '4',
  minimum_gap_in: '0.125',
  gap_thickness_multiplier: '1',
  minimum_margin_in: '0.375',
  margin_thickness_multiplier: '2',
};
const snapshot: SpacingPolicySnapshot = {
  schema_version: 1,
  company_id: 1,
  policy_id: 1,
  publication_id: 1,
  revision_id: 1,
  revision_number: 1,
  content_sha256: 'a'.repeat(64),
  band,
  ...resolveSpacingBand(band, 'Carbon steel', 0.125),
  resolved_at: '2026-09-08T20:00:00Z',
};
const legacyQuote = () => {
  const quote = createBlankQuote();
  delete quote.geometryProfile;
  return quote;
};
const policyQuote = () => ({ ...legacyQuote(), spacingMode: 'policy' as const, spacingPolicy: snapshot });

test('policy-bearing files use distinct versions and retain the exact snapshot', () => {
  const quote = policyQuote();
  const saved = quoteToFile(quote);
  expect(saved.version).toBe(9);
  expect(quoteFromFile(saved).spacingPolicy).toEqual(snapshot);
  const project = projectToFile(createBlankProject(quote));
  expect(project.version).toBe(10);
  expect(projectToFile(projectFromFile(project))).toEqual(project);
  expect(() => projectFromFile({ ...project, version: 6 })).toThrow();
  expect(() => quoteFromFile({ ...saved, version: 7 })).toThrow(/version 9/);
});

test('legacy files retain their old discriminators and round-trip without policy metadata', () => {
  const quote = quoteToFile(legacyQuote());
  const project = projectToFile(createBlankProject(legacyQuote()));
  expect(quote.version).toBe(3);
  expect(project.version).toBe(4);
  expect(quoteToFile(quoteFromFile(quote))).toEqual(quote);
  expect(projectToFile(projectFromFile(project))).toEqual(project);
  expect(quote).not.toHaveProperty('spacingPolicy');
});

test('policy allowances cannot be understated even below the decimal quantum', () => {
  const quote = policyQuote();
  expect(() => quoteToFile({ ...quote, gap: inToMm(0.1249999999) })).toThrow(/differs/);
  expect(quoteToFile(quote).gap).toBe(Number(snapshot.gap_in));
});

test('a recorded custom override is manual and never implies policy conformance', () => {
  const quote = {
    ...legacyQuote(),
    spacingMode: 'manual' as const,
    spacingOverride: {
      schema_version: 1 as const,
      reason: 'Quoted from reviewed customer stock constraint',
      changed_at: '2026-09-08T20:00:00Z',
    },
  };
  expect(quoteToFile(quote).version).toBe(9);
  expect(quoteFromFile(quoteToFile(quote)).spacingOverride).toEqual(quote.spacingOverride);
  expect(() => quoteToFile({ ...quote, spacingMode: 'auto' })).toThrow(/manual/);
  expect(() => quoteToFile({ ...policyQuote(), spacingOverride: quote.spacingOverride })).toThrow(/cannot both/);
});
