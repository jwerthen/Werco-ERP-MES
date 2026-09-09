import { buildBuyerPdfReport } from './buyer-pdf';
import type { BuyerPdfInputs } from './buyer-pdf-types';
import type { Nest, Part, Placement } from './nesting';
import { createBlankProject } from './quote-project';
import { createBlankQuote, type Quote } from './quoting';

const metadata = { projectName: '100-design material order', notes: 'Confirm grade before ordering.' };
const selection = { 'group-1': 'full:twenty-inch-sheet' };
const diameterMm = 25.4;

/** A roomy, analytical two-sheet layout; no optimizer or geometry mocks produce this fixture. */
function manualGrid(designCount = 100): BuyerPdfInputs {
  const parts: Part[] = Array.from({ length: designCount }, (_, index) => ({
    id: `design-${String(index).padStart(3, '0')}`,
    name: index % 3 === 0 ? 'Repeated DXF name' : 'Another repeated DXF name',
    revision: `R-${index}`,
    quantity: 2,
    rotate: false,
    rotationMode: 'fixed',
    color: index % 4,
    loops:
      index % 2 === 0
        ? [
            {
              type: 'poly',
              points: [
                { x: 0, y: 0 },
                { x: diameterMm, y: 0 },
                { x: diameterMm, y: diameterMm },
                { x: 0, y: diameterMm },
              ],
            },
          ]
        : [{ type: 'circle', cx: diameterMm / 2, cy: diameterMm / 2, r: diameterMm / 2 }],
  }));
  const quote: Quote = {
    ...createBlankQuote(),
    name: 'Mixed profiles with repeated drawing names',
    parts,
    margin: 3.175,
    gap: 3.175,
    spacingMode: 'manual',
    options: [{ id: 'twenty-inch-sheet', width: 508, height: 508, enabled: true, price: null }],
  };
  const placements: Placement[] = [0, 1].flatMap(sheet => {
    const row: Placement[] = parts.map((part, index) => ({
      partId: part.id,
      instance: sheet,
      sheet,
      x: 12 + (index % 10) * 45,
      y: 12 + Math.floor(index / 10) * 45,
      width: diameterMm,
      height: diameterMm,
      rotation: 0,
    }));
    // Order is deliberately different from the requirements, so an index-based join cannot pass.
    return sheet === 0 ? row.reverse() : row;
  });
  const area = parts.reduce(
    (sum, _, index) => sum + 2 * (index % 2 === 0 ? diameterMm ** 2 : Math.PI * (diameterMm / 2) ** 2),
    0
  );
  const nest: Nest = {
    placements,
    unplaced: [],
    sheets: 2,
    area,
    utilization: (100 * area) / (2 * 508 ** 2),
    method: 'Independent analytical grid',
  };
  return {
    project: createBlankProject(quote),
    comparisons: {
      'group-1': {
        signature: JSON.stringify(quote),
        comparison: {
          results: [{ option: quote.options[0], nest, complete: true, error: null, area: 2 * 508 ** 2, cost: null }],
          recommendedId: quote.options[0].id,
          requested: designCount * 2,
          reason: 'Independent known-valid placements',
        },
      },
    },
    remnantInput: null,
    stages: [],
    companyId: 2,
  };
}

test('100 distinct designs retain duplicate names, distinct revisions and all 200 original instances in an imperial buyer report', async () => {
  const input = manualGrid();
  const before = JSON.stringify(input);
  const report = await buildBuyerPdfReport(input, selection, metadata);
  expect(JSON.stringify(input)).toBe(before);
  expect(report).toMatchObject({ version: 1, units: 'in', expectedCompanyId: 2, projectName: metadata.projectName });
  expect(report.inputSha256).toMatch(/^[a-f0-9]{64}$/);
  expect(report.groups).toHaveLength(1);
  const group = report.groups[0];
  const parts = input.project.groups[0].quote.parts;
  expect(group.partRequirements).toEqual(
    parts.map((part, index) => ({
      id: part.id,
      label: `P${index + 1}`,
      name: part.name,
      revision: part.revision,
      quantity: 2,
    }))
  );
  expect(new Set(group.partRequirements.map(part => part.id)).size).toBe(100);
  expect(new Set(group.partRequirements.map(part => part.name)).size).toBe(2);
  expect(new Set(group.partRequirements.map(part => part.revision)).size).toBe(100);
  expect(group.sheets).toHaveLength(2);
  expect(group.baselinePurchaseSheets).toEqual([]);
  const identities: string[] = [];
  group.sheets.forEach((sheet, sheetIndex) => {
    expect(sheet).toMatchObject({ number: sheetIndex + 1, source: 'purchase', widthIn: 20, lengthIn: 20 });
    expect(sheet.placements).toHaveLength(100);
    for (const placement of sheet.placements) {
      const index = parts.findIndex(part => part.id === placement.partId);
      expect(index).toBeGreaterThanOrEqual(0);
      expect(placement.originalInstance).toBe(sheetIndex);
      identities.push(`${placement.partId}:${placement.originalInstance}`);
      expect(placement.loops).toHaveLength(1);
      const loop = placement.loops[0];
      const x = 12 + (index % 10) * 45;
      const y = 12 + Math.floor(index / 10) * 45;
      // Independent transform oracle, including analytic circles rather than a bounding-box substitute.
      if (index % 2 === 0) {
        expect(loop.type).toBe('poly');
        if (loop.type !== 'poly') throw new Error('The rectangular profile was replaced.');
        expect(loop.points).toHaveLength(4);
        const corners = [
          [x, y],
          [x + diameterMm, y],
          [x + diameterMm, y + diameterMm],
          [x, y + diameterMm],
        ];
        loop.points.forEach((point, corner) => {
          expect(point.x).toBeCloseTo(corners[corner][0] / 25.4, 12);
          expect(point.y).toBeCloseTo(corners[corner][1] / 25.4, 12);
        });
      } else {
        expect(loop.type).toBe('circle');
        if (loop.type !== 'circle') throw new Error('The circular profile was replaced.');
        expect(loop.cx).toBeCloseTo((x + diameterMm / 2) / 25.4, 12);
        expect(loop.cy).toBeCloseTo((y + diameterMm / 2) / 25.4, 12);
        expect(loop.r).toBe(0.5);
      }
    }
  });
  expect(identities.sort()).toEqual(parts.flatMap(part => [`${part.id}:0`, `${part.id}:1`]).sort());
  expect(new Set(identities).size).toBe(200);
});

test('a forged quarter-turn of a fixed square is rejected even though its placement bounds still match', async () => {
  const input = manualGrid(1);
  input.comparisons['group-1'].comparison.results[0].nest!.placements[0].rotation = 90;
  await expect(buildBuyerPdfReport(input, selection, metadata)).rejects.toThrow(/orientation|rotation/i);
});

test('repeating an original instance cannot masquerade as the correct total quantity', async () => {
  const input = manualGrid(1);
  input.comparisons['group-1'].comparison.results[0].nest!.placements[1].instance = 0;
  await expect(buildBuyerPdfReport(input, selection, metadata)).rejects.toThrow(/Duplicate or invalid part instance/);
});
