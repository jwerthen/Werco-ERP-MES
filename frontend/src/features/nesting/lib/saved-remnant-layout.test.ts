import { remnantStageFixture, jsonCopy } from '../../../test-utils/remnantStageFixtures';
import { validateSavedPlanningStage } from './saved-remnant-layout';
import { buildRemnantReview, validateRemnantReport } from './remnant-review';

test('saved stage validation binds original source, predecessor and residual original indices', async () => {
  const f = await remnantStageFixture();
  const checked = await validateSavedPlanningStage(f.detail, f.raw, f.checkpoints[2], f.checkpoints[1]);
  expect(checked.instanceMap).toEqual([{ part_id: 'plate', originals: [1] }]);
  expect(checked.quote.parts[0].quantity).toBe(1);
  await expect(validateSavedPlanningStage(f.detail, f.raw, f.checkpoints[2])).rejects.toThrow(/predecessor/i);
  const changed = jsonCopy(f.checkpoints[2]);
  if (changed.result.type === 'stage' && changed.result.stage_kind === 'residual')
    changed.result.instance_map[0].originals = [0];
  await expect(validateSavedPlanningStage(f.detail, f.raw, changed, f.checkpoints[1])).rejects.toThrow(/partition/i);
});
test('rejects profile/runtime downgrade, modified source stock, and mismatched checkpoint receipt', async () => {
  const f = await remnantStageFixture();
  await expect(
    validateSavedPlanningStage(
      { ...f.detail, settings: { ...f.detail.settings, protocol: 1 } },
      f.raw,
      f.checkpoints[1]
    )
  ).rejects.toThrow(/runtime|protocol/i);
  const stock = jsonCopy(f.checkpoints[1]);
  if (stock.result.type === 'stage' && stock.result.stock) stock.result.stock.width += 1;
  await expect(validateSavedPlanningStage(f.detail, f.raw, stock)).rejects.toThrow(/material|observation/i);
  await expect(
    validateSavedPlanningStage(f.detail, f.raw, { ...f.checkpoints[1], content_sha256: 'f'.repeat(64) })
  ).rejects.toThrow(/receipt/i);
});
test('zero residual is explicit and cannot carry fabricated stock', async () => {
  const f = await remnantStageFixture(1);
  expect((await validateSavedPlanningStage(f.detail, f.raw, f.checkpoints[2], f.checkpoints[1])).instanceMap).toEqual(
    []
  );
  const changed = jsonCopy(f.checkpoints[2]);
  if (changed.result.type === 'stage' && changed.result.stage_kind === 'residual')
    changed.result.stock = f.stages[0].stock;
  await expect(validateSavedPlanningStage(f.detail, f.raw, changed, f.checkpoints[1])).rejects.toThrow(
    /zero residual/i
  );
});
test('local review exports actual-domain and original placements in inches, and marks partial prefixes', async () => {
  const f = await remnantStageFixture();
  const report = await buildRemnantReview(f.raw, f.stages, { companyId: 2, estimatorId: 7 }, { searchFinished: true });
  expect(report.content.searchFinished).toBe(true);
  expect(report.content.alternatives[1].stock?.width).toBeCloseTo(3);
  expect(report.content.alternatives[1].leftovers?.version).toBe('werco-leftovers-v4');
  expect(report.content.alternatives[2].placements[0].originalInstance).toBe(1);
  expect(report.content.creditUSD).toBe(0);
  expect(
    (await buildRemnantReview(f.raw, f.stages.slice(0, 1), { companyId: 2, estimatorId: 7 })).content.searchFinished
  ).toBe(false);
  await expect(
    validateRemnantReport({
      schema_version: 1,
      status: 'UNAPPROVED',
      run: f.detail,
      estimate: f.raw,
      checkpoints: f.checkpoints,
      content_sha256: 'd'.repeat(64),
    })
  ).resolves.toBeUndefined();
});
