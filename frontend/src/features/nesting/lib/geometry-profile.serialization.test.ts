import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import generated from './geometry-profile.generated.json';
import { CURRENT_GEOMETRY_PROFILE, requireCurrentGeometryProfile, resolveGeometryProfile } from './geometry-profile';
import { canonicalJSON, sha256 } from './provenance';
import { createBlankQuote, quoteFromFile, quoteToFile, stockFor } from './quoting';
import {
  createBlankProject,
  projectFromFile,
  projectToFile,
  requireCurrentProjectGeometry,
  upgradeProjectGeometry,
} from './quote-project';
import { bounds, demoJob, rect } from './nesting';
import { jobFromFile, jobToFile } from './units';
import { calculateSavedProject } from './server-run';

const legacyQuote = () => {
  const quote = createBlankQuote();
  delete quote.geometryProfile;
  quote.parts = [{ id: 'part', name: 'Part', quantity: 1, color: 0, rotate: false, loops: [rect(10, 20)] }];
  quote.options = [{ id: 'sheet', enabled: true, width: 100, height: 100, price: 25 }];
  quote.margin = 2;
  quote.gap = 1;
  return quote;
};

test('the generated browser profile and WebCrypto digest match the one normative source', async () => {
  const source = JSON.parse(
    readFileSync(resolve(process.cwd(), '../backend/app/data/nesting_profiles/werco-compensated-v1.json'), 'ascii')
  );
  expect(generated).toEqual(source);
  expect(await sha256(canonicalJSON({ id: generated.identity.id, profile: generated.profile }))).toBe(
    '21e8689fb2ce80c72befbc5866f658cd74fe8ed336d1b5c070e182f3081aa55a'
  );
  expect(CURRENT_GEOMETRY_PROFILE.sha256).toBe('21e8689fb2ce80c72befbc5866f658cd74fe8ed336d1b5c070e182f3081aa55a');
});

test('missing historical profiles remain absent while current, malformed and unknown references are distinguished', () => {
  expect(resolveGeometryProfile(undefined)).toBeUndefined();
  expect(() => requireCurrentGeometryProfile(undefined)).toThrow(/earlier clearance rules/);
  for (const ref of [
    null,
    {},
    [],
    { ...CURRENT_GEOMETRY_PROFILE, extra: true },
    { ...CURRENT_GEOMETRY_PROFILE, sha256: 'f'.repeat(64) },
  ])
    expect(() => resolveGeometryProfile(ref)).toThrow(/profile/);
  expect(Object.isFrozen(resolveGeometryProfile(CURRENT_GEOMETRY_PROFILE))).toBe(true);
});

test('fresh estimates use profile-bearing imperial forms; old forms never acquire a profile on read', () => {
  const quote = createBlankQuote();
  expect(quote.parts).toEqual([]);
  expect(quoteToFile(quote).version).toBe(14);
  expect(projectToFile(createBlankProject()).version).toBe(15);
  const currentJob = jobToFile({
    ...demoJob,
    stock: { ...demoJob.stock, geometryProfile: { ...CURRENT_GEOMETRY_PROFILE } },
  });
  expect(currentJob.version).toBe(16);
  expect(jobFromFile(currentJob)).toEqual(
    expect.objectContaining({ stock: expect.objectContaining({ geometryProfile: CURRENT_GEOMETRY_PROFILE }) })
  );
  const original = quoteToFile(legacyQuote());
  expect(original.version).toBe(3);
  const immutableBeforeRead = JSON.stringify(original);
  const reopened = quoteFromFile(original);
  expect(reopened.geometryProfile).toBeUndefined();
  expect(quoteToFile(reopened).version).toBe(3);
  expect(quoteToFile(reopened)).not.toHaveProperty('geometryProfile');
  expect(bounds(reopened.parts[0].loops[0]).width).toBeCloseTo(10, 12);
  expect(bounds(reopened.parts[0].loops[0]).height).toBeCloseTo(20, 12);
  expect(JSON.stringify(original)).toBe(immutableBeforeRead);
  for (const version of [3, 7, 9, 11])
    expect(() => quoteFromFile({ ...original, version, geometryProfile: CURRENT_GEOMETRY_PROFILE })).toThrow(
      /version 14/
    );
  expect(() => quoteFromFile({ ...original, version: 14 })).toThrow(/earlier clearance rules/);
  expect(() => quoteFromFile({ ...original, version: 14, geometryProfile: null })).toThrow(/profile/);
  expect(() => jobFromFile({ ...currentJob, version: 13 })).toThrow(/version 16/);
  expect(() => jobFromFile({ ...currentJob, stock: { ...currentJob.stock, geometryProfile: null } })).toThrow(
    /profile/
  );
});

test('explicit upgrade preserves exact geometry, spacing and business inputs without mutating the old project', () => {
  const quote = legacyQuote();
  quote.parts[0].revision = 'B';
  quote.options[0].exclusions = [
    {
      id: 'corner',
      label: 'Unavailable',
      reason: 'Measured for this quote',
      clearance: 0.1,
      outline: { type: 'circle', cx: 80.123456789, cy: 80, r: 3.123456789 },
    },
  ];
  const original = createBlankProject(quote);
  const before = JSON.stringify(original);
  const upgraded = upgradeProjectGeometry(original);
  const { geometryProfile, ...retained } = upgraded.groups[0].quote;
  expect(geometryProfile).toEqual(CURRENT_GEOMETRY_PROFILE);
  expect(retained).toEqual(original.groups[0].quote);
  expect(upgraded.groups[0].quote.parts).toBe(original.groups[0].quote.parts);
  expect(upgraded.groups[0].quote.options).toBe(original.groups[0].quote.options);
  expect(JSON.stringify(original)).toBe(before);
  expect(projectToFile(original).version).toBe(12);
  expect(projectToFile(upgraded).version).toBe(15);
  expect(quoteToFile(upgraded.groups[0].quote).version).toBe(14);
  expect(stockFor(upgraded.groups[0].quote, upgraded.groups[0].quote.options[0]).geometryProfile).toEqual(
    CURRENT_GEOMETRY_PROFILE
  );
});

test('all populated groups are checked before any new checkpoint while empty legacy groups stay preservable', () => {
  const project = upgradeProjectGeometry(createBlankProject(legacyQuote()));
  const other = legacyQuote();
  other.material = 'Aluminum';
  other.parts[0] = { ...other.parts[0], id: 'second-part' };
  project.groups.push({ id: 'old-group', quote: other });
  const saved = projectToFile(project);
  expect(saved.groups.map(group => group.quote.version)).toEqual([14, 3]);
  expect(() => calculateSavedProject(saved, 'a'.repeat(64)).next()).toThrow(/earlier clearance rules/);
  expect(() => requireCurrentProjectGeometry(project)).toThrow(/earlier clearance rules/);
  other.parts = [];
  expect(() => requireCurrentProjectGeometry(project)).not.toThrow();
  const reopened = projectFromFile(projectToFile(project));
  expect(reopened.groups[1].quote.geometryProfile).toBeUndefined();
  expect(() => projectFromFile({ ...projectToFile(project), version: 12 })).toThrow(/version/);
});
