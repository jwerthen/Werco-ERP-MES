import { remnantPlanningFixture } from '../../../test-utils/remnantPlanningFixtures';
import type { ObservedShape } from '../../../types/stockPiece';
import { buildRemnantPlan, remnantEvidenceHash } from './remnant-evidence';
import { canonicalJSON, sha256 } from './provenance';
import { createBlankQuote, quoteToFile, quoteFromFile, comparisonFromResults, compareSheets } from './quoting';
import { rect } from './nesting';
import {
  buildStagePlan,
  calculateRemnantProject,
  deriveResidual,
  type RemnantStageMessage,
  type RemnantSummaryMessage,
  type RecordedPieceStageMessage,
} from './remnant-planning';

const digest = 'c'.repeat(64);
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
async function input(quantity = 10, geometry: ObservedShape = { kind: 'rectangle', width: '3', height: '3' }) {
  const fixture = await remnantPlanningFixture();
  const snapshot = fixture.snapshot;
  snapshot.evidence.geometry = geometry;
  const bytes = canonicalJSON(snapshot.evidence);
  snapshot.payloadSha256 = await sha256(bytes);
  snapshot.payloadBytes = new TextEncoder().encode(bytes).length;
  fixture.resolution.snapshot_sha256 = await remnantEvidenceHash(snapshot);
  const quote = clone(
    quoteToFile({
      ...createBlankQuote(),
      margin: 0.125 * 25.4,
      gap: 0.125 * 25.4,
      parts: [{ id: 'plate', name: 'Plate', quantity, rotate: true, color: 0, loops: [rect(2 * 25.4, 2 * 25.4)] }],
      options: [
        { id: 'eight', width: 8 * 25.4, height: 8 * 25.4, enabled: true, price: null },
        { id: 'ten', width: 10 * 25.4, height: 10 * 25.4, enabled: true, price: null },
      ],
    })
  );
  const remnantPlan = await buildRemnantPlan({
    resolution: fixture.resolution,
    companyId: 2,
    groupId: 'g1',
    quote,
    family: 'Carbon steel',
    requiredGrade: 'A36',
    reason: 'Use the measured piece conditionally',
    zoneClearanceIn: '0.125',
  });
  return {
    version: 18,
    units: 'in',
    currency: 'USD',
    name: 'Synthetic comparison',
    activeGroupId: 'g1',
    groups: [{ id: 'g1', quote }],
    remnantPlan,
  };
}
async function collect(value: unknown) {
  const frames: (RemnantStageMessage | RemnantSummaryMessage)[] = [];
  for await (const frame of calculateRemnantProject(value, digest)) frames.push(frame);
  return frames;
}

test('ordered full-sheet baselines precede one recorded piece and residual alternatives', async () => {
  const raw = await input(),
    before = JSON.stringify(raw);
  expect(buildStagePlan(raw).map(p => [p.stage_id, p.stage_kind, p.option_id, p.depends_on])).toEqual([
    ['stage-01', 'baseline', 'eight', null],
    ['stage-02', 'baseline', 'ten', null],
    ['stage-03', 'recorded_piece', null, null],
    ['stage-04', 'residual', 'eight', 'stage-03'],
    ['stage-05', 'residual', 'ten', 'stage-03'],
  ]);
  const frames = await collect(raw);
  const piece = frames[2] as RecordedPieceStageMessage;
  expect(piece.result.nest?.placements).toHaveLength(1);
  expect(piece.stock?.domain.outer.type).toBe('poly');
  expect(piece.result.leftovers?.version).toBe('werco-leftovers-v4');
  expect(Object.keys(piece.result).sort()).toEqual(['area', 'complete', 'error', 'leftovers', 'nest']);
  expect(frames[0]).toMatchObject({ requested: 10, result: { complete: true, nest: { sheets: 2 } } });
  expect(frames[3]).toMatchObject({
    requested: 9,
    instance_map: [{ part_id: 'plate', originals: [1, 2, 3, 4, 5, 6, 7, 8, 9] }],
    result: { complete: true, nest: { sheets: 1 } },
  });
  expect(frames[5]).toMatchObject({
    type: 'summary',
    evaluated_count: 5,
    complete_option_count: 4,
    total_options: 5,
    stop_reason: 'completed',
  });
  expect(JSON.stringify(raw)).toBe(before);
  expect(await collect(raw)).toEqual(frames);
});

test('all parts on the recorded piece emit explicit zero-sheet residuals without fake stock', async () => {
  const frames = await collect(await input(1));
  expect(frames[2]).toMatchObject({ result: { complete: true, nest: { sheets: 1 } } });
  for (const frame of frames.slice(3, 5))
    expect(frame).toMatchObject({ requested: 0, instance_map: [], stock: null, result: null });
  expect(frames[5]).toMatchObject({ complete_option_count: 4 }); // the piece itself is never an alternative completion
});

test('domain reconstruction failure preserves completed baselines and leaves every original instance for full sheets', async () => {
  const frames = await collect(await input(10, { kind: 'rectangle', width: '1000', height: '10' }));
  expect(frames[0]).toMatchObject({ stage_kind: 'baseline', result: { complete: true } });
  expect(frames[2]).toMatchObject({
    stage_kind: 'recorded_piece',
    stock: null,
    result: { nest: null, complete: false, area: 0, error: expect.any(String) },
  });
  expect(frames[3]).toMatchObject({
    requested: 10,
    instance_map: [{ part_id: 'plate', originals: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9] }],
    result: { nest: { sheets: 2 } },
  });
});

test('a usable but nonfitting recorded piece is never counted as consumed material', async () => {
  const frames = await collect(await input(10, { kind: 'rectangle', width: '1', height: '1' }));
  expect(frames[2]).toMatchObject({
    result: { nest: { sheets: 0, placements: [] }, area: 0, complete: false, error: null },
  });
  expect(frames[3]).toMatchObject({ requested: 10, result: { nest: { sheets: 2 } } });
});

test('residual mapping tracks non-prefix original indices and rejects duplicate or invalid predecessor instances', async () => {
  const raw = await input(),
    frames = await collect(raw);
  const piece = clone(frames[2] as RecordedPieceStageMessage);
  piece.result.nest!.placements[0].instance = 6;
  const residual = deriveResidual(raw.groups[0].quote, piece);
  expect(residual.instanceMap).toEqual([{ part_id: 'plate', originals: [0, 1, 2, 3, 4, 5, 7, 8, 9] }]);
  expect(residual.quote.parts[0].quantity).toBe(9);
  expect(residual.quote.parts[0].loops).toEqual(raw.groups[0].quote.parts[0].loops);
  piece.result.nest!.placements[0].instance = 10;
  expect(() => deriveResidual(raw.groups[0].quote, piece)).toThrow(/instance/);
});

test('an input or yielded-frame mutation cannot rewrite the owned later calculation', async () => {
  const raw = await input(),
    baseline = await collect(raw),
    iterator = calculateRemnantProject(raw, digest);
  const first = await iterator.next();
  raw.groups[0].quote.parts[0].quantity = 1;
  if (first.value?.type === 'stage' && first.value.stage_kind === 'baseline') first.value.result.option.width = 1;
  const rest = [];
  for await (const frame of iterator) rest.push(frame);
  expect(rest).toEqual(baseline.slice(1));
});

test('changed assignment fingerprints and non-JSON inputs fail before any stage is emitted', async () => {
  const raw = await input();
  raw.groups[0].quote.parts[0].quantity = 9;
  await expect(calculateRemnantProject(raw, digest).next()).rejects.toThrow(/changed|fingerprint/);
  await expect(calculateRemnantProject({ ...raw, extra: undefined }, digest).next()).rejects.toThrow();
  await expect(calculateRemnantProject(raw, 'invalid').next()).rejects.toThrow(/fingerprint/);
});

test('baseline ranking reuses validated results and rejects a false complete flag or changed stock', async () => {
  const raw = await input(),
    quote = quoteFromFile(raw.groups[0].quote),
    result = compareSheets(quote);
  expect(comparisonFromResults(quote, result.results)).toEqual(result);
  expect(comparisonFromResults(quote, [result.results[0]]).results).toHaveLength(1);
  const changed = clone(result.results);
  changed[0].option.width += 1;
  expect(() => comparisonFromResults(quote, changed)).toThrow(/differ/);
  const falseFlag = clone(result.results);
  falseFlag[0].complete = false;
  expect(() => comparisonFromResults(quote, falseFlag)).toThrow(/completion/);
});

test('other thickness groups retain every baseline and never enter the selected piece or its residuals', async () => {
  const raw = await input(1);
  const second = clone(raw.groups[0]);
  second.id = 'thicker';
  second.quote.thickness = 0.25;
  second.quote.parts[0].id = 'thicker-plate';
  second.quote.parts[0].quantity = 3;
  raw.groups.unshift(second);
  const frames = await collect(raw);
  expect(frames.filter(f => f.type === 'stage').map(f => [f.stage_kind, f.group_id])).toEqual([
    ['baseline', 'thicker'],
    ['baseline', 'thicker'],
    ['baseline', 'g1'],
    ['baseline', 'g1'],
    ['recorded_piece', 'g1'],
    ['residual', 'g1'],
    ['residual', 'g1'],
  ]);
  expect(frames[0]).toMatchObject({
    requested: 3,
    result: { complete: true, nest: { placements: expect.any(Array) } },
  });
  const piece = frames[4] as RecordedPieceStageMessage;
  expect(piece.result.nest?.placements.map(p => p.partId)).toEqual(['plate']);
  expect(frames[5]).toMatchObject({ requested: 0, instance_map: [], result: null });
  expect(frames[7]).toMatchObject({ evaluated_count: 7, complete_option_count: 6 });
});

test('the shared stage budget includes residual alternatives and rejects before emitting any baseline', async () => {
  const raw = await input(1);
  for (let g = 0; g < 3; g++) {
    const extra = clone(raw.groups[0]);
    extra.id = `group-${g}`;
    extra.quote.thickness = 0.25 + g * 0.125;
    extra.quote.parts[0].id = `extra-${g}`;
    extra.quote.options = Array.from({ length: g < 2 ? 12 : 7 }, (_, i) => ({
      ...extra.quote.options[0],
      id: `stock-${i}`,
      width: 8 + i,
    }));
    raw.groups.push(extra);
  }
  expect(buildStagePlan(raw)).toHaveLength(36);
  const last = raw.groups[3].quote.options;
  last.push({ ...last[0], id: 'one-too-many' });
  expect(() => buildStagePlan(raw)).toThrow(/36/);
  await expect(calculateRemnantProject(raw, digest).next()).rejects.toThrow(/36/);
});
