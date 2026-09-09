import { remnantStageInput, jsonCopy } from '../../../test-utils/remnantStageFixtures';
import { projectFromFile, projectToFile } from './quote-project';
import { validateRemnantFile } from './remnant-evidence';

test('project18 preserves the exact observation and requires fresh hash validation on open', async () => {
  const raw = await remnantStageInput(),
    opened = projectFromFile(raw),
    saved = jsonCopy(projectToFile(opened));
  expect(saved.version).toBe(18);
  expect(saved.remnantPlan).toEqual(raw.remnantPlan);
  await expect(validateRemnantFile(raw, 2)).resolves.toBeUndefined();
  await expect(validateRemnantFile(raw, 99)).rejects.toThrow(/company/i);
  const changed = jsonCopy(raw);
  changed.groups[0].quote.parts[0].quantity++;
  expect(() => projectFromFile(changed)).not.toThrow(); // editable structural parse, never an execution authorization
  await expect(validateRemnantFile(changed, 2)).rejects.toThrow(/changed|fingerprint/i);
});
test('omitted selection downgrades only through the writer; null and legacy field presence reject', async () => {
  const raw = await remnantStageInput();
  const { remnantPlan, ...ordinary } = raw;
  void remnantPlan;
  expect(projectToFile(projectFromFile(ordinary)).version).toBe(15);
  expect(() => projectFromFile({ ...raw, remnantPlan: null })).toThrow();
  for (const version of [4, 5, 6, 10, 12, 15])
    expect(() => projectFromFile({ ...raw, version })).toThrow(/version|format|recorded/i);
});
test('selection target and staged work count cannot be silently dropped', async () => {
  const raw = await remnantStageInput();
  expect(() => projectFromFile({ ...raw, remnantPlan: { ...raw.remnantPlan, groupId: 'missing' } })).toThrow(/group/i);
  raw.groups[0].quote.options = Array.from({ length: 12 }, (_, i) => ({
    ...raw.groups[0].quote.options[0],
    id: `s${i}`,
  }));
  raw.groups.push({
    id: 'g2',
    quote: {
      ...jsonCopy(raw.groups[0].quote),
      material: 'Aluminum',
      parts: [{ ...jsonCopy(raw.groups[0].quote.parts[0]), id: 'other' }],
    },
  });
  expect(() => projectFromFile(raw)).toThrow(/36|stage|evaluation/i);
});
