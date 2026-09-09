import { readFileSync } from 'fs';
import { resolve } from 'path';
import { remnantPlanningFixture } from '../../../test-utils/remnantPlanningFixtures';
import {
  buildRemnantPlan,
  canonicalRemnantEvidence,
  defaultZoneClearance,
  normalizedZoneClearance,
  remnantEvidenceHash,
  validateRemnantPlan,
  validateRemnantResolution,
  validateRemnantSnapshot,
} from './remnant-evidence';

const vectors: { name: string; value: unknown; canonical: string; sha256: string }[] = JSON.parse(
  readFileSync(resolve(process.cwd(), '../backend/tests/fixtures/remnant_evidence_golden.json'), 'utf8')
);
test.each(vectors)('independent Python golden parity: $name', async vector => {
  expect(canonicalRemnantEvidence(vector.value)).toBe(vector.canonical);
  expect(await remnantEvidenceHash(vector.value)).toBe(vector.sha256);
});
test('rejects non-JSON values, unsafe integers, accessors and sparse arrays', () => {
  const circular: unknown[] = [];
  circular.push(circular);
  const getter = Object.defineProperty({}, 'value', {
    enumerable: true,
    get() {
      throw new Error('Accessor executed');
    },
  });
  for (const value of [
    undefined,
    NaN,
    Infinity,
    1e100,
    Number.MAX_SAFE_INTEGER + 1,
    new Date(),
    new Map(),
    BigInt(2),
    Symbol('s'),
    circular,
    [undefined],
    Array(2),
    getter,
  ])
    expect(() => canonicalRemnantEvidence(value)).toThrow();
  let deep: unknown = null;
  for (let i = 0; i < 33; i++) deep = [deep];
  expect(() => canonicalRemnantEvidence(deep)).toThrow(/structural/);
  const shared = { value: 2 };
  expect(canonicalRemnantEvidence([shared, shared])).toBe(canonicalRemnantEvidence([{ value: 2 }, { value: 2 }]));
});
test('snapshot source/evidence bindings reject hidden edits and noncanonical repairs', async () => {
  const { snapshot } = await remnantPlanningFixture();
  await expect(validateRemnantSnapshot(snapshot)).resolves.toEqual(snapshot);
  for (const changed of [
    { ...snapshot, payloadSha256: 'b'.repeat(64) },
    { ...snapshot, payloadBytes: snapshot.payloadBytes + 1 },
    { ...snapshot, sourcePartId: 99 },
    {
      ...snapshot,
      sourceEvidence: { ...snapshot.sourceEvidence, part: { ...snapshot.sourceEvidence.part, company_id: 3 } },
    },
    { ...snapshot, evidence: { ...snapshot.evidence, thickness: '0.1250' } },
    { ...snapshot, evidence: { ...snapshot.evidence, grade: null } },
    {
      ...snapshot,
      sourceEvidence: {
        ...snapshot.sourceEvidence,
        item: { ...snapshot.sourceEvidence.item, quantity_available: '1' },
      },
    },
  ])
    await expect(validateRemnantSnapshot(changed)).rejects.toThrow();
});
test('resolver checks exact request identity, latestness and typed snapshot digest', async () => {
  const { snapshot, resolution } = await remnantPlanningFixture();
  const expected = {
    expected_company_id: 2,
    pieceId: 41,
    observationNumber: 2,
    expected_payload_sha256: snapshot.payloadSha256,
    expected_source_sha256: snapshot.sourceSha256,
  };
  await expect(validateRemnantResolution(resolution, expected)).resolves.toEqual(resolution);
  for (const patch of [
    { company_id: 3 },
    { latest_observation_number: 3 },
    { source_status: 'changed' },
    { current_source_sha256: 'b'.repeat(64) },
    { snapshot_sha256: 'b'.repeat(64) },
    { checked_at: '2026-02-30T00:00:00Z' },
  ])
    await expect(validateRemnantResolution({ ...resolution, ...patch }, expected)).rejects.toThrow();
});
test('exact known specification and every raw target field bind a one-piece selection', async () => {
  const fixture = await remnantPlanningFixture();
  const input = {
    resolution: fixture.resolution,
    companyId: 2,
    groupId: 'g1',
    quote: fixture.quote,
    family: 'Carbon steel',
    requiredGrade: 'A36',
    reason: 'Reviewed job grade and source record',
    zoneClearanceIn: '.375',
  };
  const plan = await buildRemnantPlan(input);
  expect(plan).toMatchObject({
    capacity: 1,
    planningOnly: true,
    eligibilityVerified: false,
    availabilityVerified: false,
    zoneClearanceIn: '0.375',
  });
  expect(plan.snapshot.evidence.geometry).toEqual(fixture.snapshot.evidence.geometry);
  for (const patch of [
    { requiredGrade: 'a36' },
    { family: 'Aluminum' },
    { reason: '' },
    { quote: { ...fixture.quote, thickness: 0.25 } },
  ])
    await expect(buildRemnantPlan({ ...input, ...patch })).rejects.toThrow();
  for (const patch of [
    { name: 'A changed name' },
    { margin: 0.5 },
    { parts: fixture.quote.parts.map(p => ({ ...p, quantity: 3 })) },
  ])
    await expect(validateRemnantPlan(plan, { ...input, quote: { ...fixture.quote, ...patch } })).rejects.toThrow(
      /changed/
    );
  for (const patch of [
    { capacity: 2 },
    { availabilityVerified: true },
    { planningOnly: false },
    { geometryProfile: null },
  ])
    await expect(validateRemnantPlan({ ...plan, ...patch }, input)).rejects.toThrow();
});
test('default zone clearance uses an explicit upward nanoinch bridge without changing the group', async () => {
  const { quote } = await remnantPlanningFixture();
  expect(defaultZoneClearance(quote)).toBe('0.375');
  const margin = 0.37500000000000006;
  const changed = { ...quote, margin };
  expect(defaultZoneClearance(changed)).toBe('0.375000001');
  expect(changed.margin).toBe(margin);
  expect(normalizedZoneClearance('1 1/8')).toBe('1.125');
  for (const input of ['-0.1', '100.000000001', '1/3']) expect(() => normalizedZoneClearance(input)).toThrow();
});
