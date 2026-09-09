import { remnantPlanningFixture } from '../../../test-utils/remnantPlanningFixtures';
import { buildBuyerPdfReport, getBuyerPdfChoices } from './buyer-pdf';
import type { BuyerPdfInputs } from './buyer-pdf-types';
import { rect, transformLoops } from './nesting';
import { canonicalJSON, sha256 } from './provenance';
import { createBlankProject, projectFromFile } from './quote-project';
import { compareSheets, createBlankQuote, quoteToFile } from './quoting';
import { buildRemnantPlan, remnantEvidenceHash } from './remnant-evidence';
import { calculateRemnantProject, type RemnantStageMessage } from './remnant-planning';

const metadata = { projectName: 'JOB-123 <A&B>', notes: 'Buy material only.' };
function fixture(): BuyerPdfInputs {
  const quote = {
    ...createBlankQuote(),
    parts: [
      {
        id: 'plate',
        name: 'Actual L plate',
        revision: 'B',
        quantity: 2,
        rotate: true,
        color: 0,
        loops: [
          {
            type: 'poly' as const,
            points: [
              { x: 0, y: 0 },
              { x: 100, y: 0 },
              { x: 100, y: 30 },
              { x: 30, y: 30 },
              { x: 30, y: 80 },
              { x: 0, y: 80 },
            ],
          },
          { type: 'circle' as const, cx: 15, cy: 15, r: 5 },
        ],
      },
    ],
  };
  return {
    project: createBlankProject(quote),
    comparisons: { 'group-1': { comparison: compareSheets(quote), signature: JSON.stringify(quote) } },
    remnantInput: null,
    stages: [],
    companyId: 2,
  };
}
function selections(input: BuyerPdfInputs) {
  return Object.fromEntries(getBuyerPdfChoices(input).map(group => [group.groupId, group.choices[0]?.id]));
}
async function conditionalFixture(quantity = 10): Promise<BuyerPdfInputs> {
  const fixture = await remnantPlanningFixture();
  fixture.snapshot.evidence.geometry = { kind: 'rectangle', width: '3', height: '3' };
  const bytes = canonicalJSON(fixture.snapshot.evidence);
  fixture.snapshot.payloadSha256 = await sha256(bytes);
  fixture.snapshot.payloadBytes = new TextEncoder().encode(bytes).length;
  fixture.resolution.snapshot_sha256 = await remnantEvidenceHash(fixture.snapshot);
  const quote = quoteToFile({
    ...createBlankQuote(),
    margin: 0.125 * 25.4,
    gap: 0.125 * 25.4,
    parts: [{ id: 'plate', name: 'Plate', quantity, rotate: true, color: 0, loops: [rect(2 * 25.4, 2 * 25.4)] }],
    options: [{ id: 'eight', width: 8 * 25.4, height: 8 * 25.4, enabled: true, price: null }],
  });
  const remnantPlan = await buildRemnantPlan({
    resolution: fixture.resolution,
    companyId: 2,
    groupId: 'g1',
    quote,
    family: 'Carbon steel',
    requiredGrade: 'A36',
    reason: 'Synthetic measured piece',
    zoneClearanceIn: '0.125',
  });
  const raw = {
    version: 18,
    units: 'in',
    currency: 'USD',
    name: 'Synthetic buyer report',
    activeGroupId: 'g1',
    groups: [{ id: 'g1', quote }],
    remnantPlan,
  };
  const stages: RemnantStageMessage[] = [];
  for await (const frame of calculateRemnantProject(raw, await sha256(canonicalJSON(raw))))
    if (frame.type === 'stage') stages.push(frame);
  return { project: projectFromFile(raw), comparisons: {}, stages, remnantInput: raw, companyId: 2 };
}

test('one selected option preserves concave contours, holes, inches and exact quantities without summing alternatives', async () => {
  const input = fixture(),
    selected = selections(input);
  const report = await buildBuyerPdfReport(input, selected, metadata);
  expect(input.comparisons['group-1'].comparison.results).toHaveLength(3);
  expect(report.groups).toHaveLength(1);
  expect(report.groups[0].sheets).toHaveLength(1);
  expect(report.groups[0].sheets[0]).toMatchObject({ source: 'purchase' });
  expect(report.groups[0].sheets[0].widthIn).toBeCloseTo(48, 10);
  expect(report.groups[0].sheets[0].lengthIn).toBeCloseTo(96, 10);
  expect(report.groups[0].partRequirements).toEqual([
    { id: 'plate', label: 'P1', name: 'Actual L plate', revision: 'B', quantity: 2 },
  ]);
  const loops = report.groups[0].sheets[0].placements[0].loops;
  expect(loops[0].type === 'poly' && loops[0].points).toHaveLength(6);
  const original = transformLoops(
    input.project.groups[0].quote.parts[0],
    input.comparisons['group-1'].comparison.results[0].nest!.placements[0]
  );
  expect(loops[1]).toMatchObject({ type: 'circle', r: 5 / 25.4 });
  expect(loops[0].type === 'poly' && loops[0].points[2].x).toBe(
    original[0].type === 'poly' ? original[0].points[2].x / 25.4 : null
  );
  expect(report.projectName).toBe(metadata.projectName);
  expect(report.groups[0].materialDescription).toContain('Not specified');
  expect(report).not.toHaveProperty('cost');
});

test('a valid complete selection remains exportable when another enabled alternative has not returned', async () => {
  const input = fixture();
  input.comparisons['group-1'].comparison.results.splice(1);
  expect((await buildBuyerPdfReport(input, selections(input), metadata)).groups[0].sheets).toHaveLength(1);
});

test('the report owns a frozen snapshot during asynchronous hashing and carries sheet exclusions', async () => {
  const input = fixture();
  const quote = input.project.groups[0].quote;
  quote.options = [
    {
      ...quote.options[0],
      exclusions: [
        {
          id: 'damage',
          label: 'Damage',
          reason: 'Observed',
          clearance: 6.35,
          outline: {
            type: 'poly',
            points: [
              { x: 1000, y: 1000 },
              { x: 1100, y: 1000 },
              { x: 1100, y: 1100 },
              { x: 1000, y: 1100 },
            ],
          },
        },
      ],
    },
  ];
  input.comparisons['group-1'] = { comparison: compareSheets(quote), signature: JSON.stringify(quote) };
  const fields = { ...metadata };
  const pending = buildBuyerPdfReport(input, selections(input), fields);
  fields.projectName = 'Changed after request';
  quote.parts[0].name = 'Changed after request';
  const report = await pending;
  expect(report.projectName).toBe(metadata.projectName);
  expect(report.groups[0].partRequirements[0].name).toBe('Actual L plate');
  expect(report.groups[0].sheets[0].exclusions[0]).toMatchObject({ clearanceIn: 0.25, outline: { type: 'poly' } });
});

test('missing, extra, stale, incomplete or overlapping selections cannot create a buyer order', async () => {
  const input = fixture();
  await expect(buildBuyerPdfReport(input, {}, metadata)).rejects.toThrow('exactly one');
  await expect(buildBuyerPdfReport(input, { ...selections(input), unknown: 'full:fake' }, metadata)).rejects.toThrow(
    'exactly one'
  );
  const selected = selections(input);
  input.project.groups[0].quote.parts[0].quantity = 3;
  await expect(buildBuyerPdfReport(input, selected, metadata)).rejects.toThrow('stale');
  const overlap = fixture(),
    nest = overlap.comparisons['group-1'].comparison.results[0].nest!;
  nest.placements[1] = { ...nest.placements[1], x: nest.placements[0].x, y: nest.placements[0].y };
  await expect(buildBuyerPdfReport(overlap, selections(overlap), metadata)).rejects.toThrow('overlap');
  const incomplete = fixture();
  incomplete.comparisons['group-1'].comparison.results[0].complete = false;
  await expect(buildBuyerPdfReport(incomplete, selected, metadata)).rejects.toThrow('incomplete');
});

test('each material/thickness group must be covered, even with duplicate part names', async () => {
  const input = fixture();
  const quote = {
    ...input.project.groups[0].quote,
    material: 'Aluminum',
    thickness: 6.35,
    parts: input.project.groups[0].quote.parts.map(part => ({ ...part, id: 'aluminum-plate' })),
  };
  input.project.groups.push({ id: 'aluminum', quote });
  input.comparisons.aluminum = { comparison: compareSheets(quote), signature: JSON.stringify(quote) };
  const report = await buildBuyerPdfReport(input, selections(input), metadata);
  expect(report.groups.map(group => [group.material, group.thicknessIn, group.partRequirements[0].quantity])).toEqual([
    ['Carbon steel', 0.125, 2],
    ['Aluminum', 0.25, 2],
  ]);
  expect(report.groups.flatMap(group => group.sheets)).toHaveLength(2);
});

test('recorded piece and residual partition original instances exactly and retain a full-sheet fallback', async () => {
  const input = await conditionalFixture();
  const selected = { g1: getBuyerPdfChoices(input)[0].choices.find(choice => choice.conditional)!.id };
  const report = await buildBuyerPdfReport(input, selected, metadata);
  expect(report.groups[0].sheets.map(sheet => [sheet.source, sheet.placements.length])).toEqual([
    ['recorded_piece', 1],
    ['purchase', 9],
  ]);
  expect(report.groups[0].baselinePurchaseSheets).toEqual([{ widthIn: 8, lengthIn: 8, quantity: 2 }]);
  expect(
    report.groups[0].sheets.flatMap(sheet => sheet.placements.map(p => p.originalInstance)).sort((a, b) => a - b)
  ).toEqual(Array.from({ length: 10 }, (_, i) => i));
  const residual = input.stages.find(stage => stage.stage_kind === 'residual')!;
  if (residual.stage_kind === 'residual') residual.instance_map[0].originals[0] = 0;
  await expect(buildBuyerPdfReport(input, selected, metadata)).rejects.toThrow('partition');
});

test('a complete recorded-piece plan buys zero sheets and cannot export after source, tenant or stage changes', async () => {
  const input = await conditionalFixture(1);
  const selected = { g1: getBuyerPdfChoices(input)[0].choices.find(choice => choice.conditional)!.id };
  const report = await buildBuyerPdfReport(input, selected, metadata);
  expect(report.groups[0].sheets.filter(sheet => sheet.source === 'purchase')).toHaveLength(0);
  expect(report.groups[0].baselinePurchaseSheets[0].quantity).toBe(1);
  await expect(buildBuyerPdfReport({ ...input, companyId: 9 }, selected, metadata)).rejects.toThrow();
  input.project.groups[0].quote.parts[0].revision = 'changed';
  await expect(buildBuyerPdfReport(input, selected, metadata)).rejects.toThrow('inputs changed');
});
