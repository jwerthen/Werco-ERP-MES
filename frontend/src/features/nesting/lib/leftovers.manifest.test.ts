import * as leftovers from './leftovers';
import { rect, type Nest, type Part, type Stock } from './nesting';
import { createBlankProject } from './quote-project';
import { compareSheets, createBlankQuote } from './quoting';
import { buildRunManifest } from './run-manifest';

const identity = { companyId: 1, estimatorId: 2 };
function fixture() {
  const quote = {
    ...createBlankQuote(),
    gap: 0,
    margin: 6.35,
    parts: [{ id: 'plate', name: 'Synthetic plate', loops: [rect(25.4, 25.4)], quantity: 1, rotate: false, color: 0 }],
    options: [{ id: 'synthetic-stock', width: 254, height: 127, price: 100, enabled: true }],
  };
  const comparison = compareSheets(quote);
  const project = createBlankProject(quote);
  return { quote, comparison, project, snapshots: { 'group-1': { comparison, signature: JSON.stringify(quote) } } };
}

describe('leftover evidence and bounded analysis failures', () => {
  afterEach(() => jest.restoreAllMocks());

  it('exports real polygon coordinates and the complete area ledger in inches without approving reuse or credit', async () => {
    const value = fixture();
    const raw = value.comparison.results[0].leftovers!;
    const record = await buildRunManifest(value.project, value.snapshots, identity);
    const alternative = record.content.results[0].alternatives[0];
    const output = alternative.leftovers!;
    expect(output).toMatchObject({ units: 'in', areaUnits: 'in2', status: 'potential_review_only', creditUSD: 0 });
    expect(output.sheets[0]).toMatchObject({ grossAreaIn2: 50, nominalPartAreaIn2: 1, reservedCutoutAreaIn2: 0 });
    for (const key of [
      'grossArea',
      'usableArea',
      'edgeMarginArea',
      'nominalPartArea',
      'reservedCutoutArea',
      'clearanceAndProtectionArea',
      'remainingArea',
    ] as const) {
      expect(output.sheets[0]).toHaveProperty(`${key}In2`, raw.sheets[0][key] / 25.4 ** 2);
      expect(output.sheets[0]).not.toHaveProperty(key);
    }
    raw.sheets[0].regions.forEach((region, index) => {
      const saved = output.sheets[0].regions[index];
      expect(saved).toMatchObject({
        id: region.id,
        classification: 'review',
        creditUSD: 0,
        areaIn2: region.area / 25.4 ** 2,
      });
      expect(saved.outer).toEqual(region.outer.map(point => ({ x: point.x / 25.4, y: point.y / 25.4 })));
      expect(saved.holes).toEqual(
        region.holes.map(ring => ring.map(point => ({ x: point.x / 25.4, y: point.y / 25.4 })))
      );
      expect(saved.bounds).toEqual(
        Object.fromEntries(Object.entries(region.bounds).map(([key, distance]) => [key, distance / 25.4]))
      );
    });
    expect(alternative).toMatchObject({
      status: 'complete_valid_layout',
      estimatedMaterialCostUSD: 100,
      remnantCreditUSD: 0,
      leftoverError: null,
    });
    expect(record.content.authoritativeApproval).toBe(false);
  });

  it.each([
    'eligibility',
    'rectangle eligibility',
    'reserved cutouts',
    'numeric profile',
    'other stock ledger',
    'conflicting error',
  ] as const)('refuses counterfeit cached %s evidence', async corruption => {
    const value = fixture();
    const result = value.comparison.results[0];
    const report = result.leftovers!;
    if (corruption === 'eligibility') Object.assign(report.assumptions, { eligibilityVerified: true });
    if (corruption === 'rectangle eligibility') Object.assign(report.assumptions, { boundsAreUsableRectangles: true });
    if (corruption === 'reserved cutouts') Object.assign(report.assumptions, { internalHolesReserved: false });
    if (corruption === 'numeric profile')
      Object.assign(report.assumptions, { profile: { ...report.assumptions.profile, integerGridMm: 1 } });
    if (corruption === 'other stock ledger') {
      // It still balances internally, but it describes 100 mm² more sheet than was actually quoted.
      report.sheets[0].grossArea += 100;
      report.sheets[0].edgeMarginArea += 100;
    }
    if (corruption === 'conflicting error') result.leftoverError = 'Analysis did not complete.';
    await expect(buildRunManifest(value.project, value.snapshots, identity)).rejects.toThrow(/leftover/i);
  });

  it('keeps the validated nest and cost when the analyzer exceeds a budget, and exports only the error', async () => {
    jest.spyOn(leftovers, 'analyzeLeftovers').mockImplementationOnce(() => {
      throw new Error('Leftover analysis: synthetic output-vertex budget exceeded.');
    });
    const value = fixture();
    const result = value.comparison.results[0];
    expect(result.complete).toBe(true);
    expect(result.cost).toBe(100);
    expect(result.nest!.placements).toHaveLength(1);
    expect(result.leftovers).toBeUndefined();
    expect(result.leftoverError).toContain('output-vertex budget exceeded');
    const record = await buildRunManifest(value.project, value.snapshots, identity);
    expect(record.content.results[0].alternatives[0]).toMatchObject({
      status: 'complete_valid_layout',
      estimatedMaterialCostUSD: 100,
      remnantCreditUSD: 0,
      leftovers: null,
      leftoverError: 'Leftover analysis: synthetic output-vertex budget exceeded.',
    });
  });

  it('refuses translated cached regions outside the selected sheet even when their ledger and shape areas still match', async () => {
    const value = fixture();
    const report = value.comparison.results[0].leftovers!;
    for (const region of report.sheets[0].regions) {
      region.outer = region.outer.map(point => ({ ...point, x: point.x + 1000 }));
      region.holes = region.holes.map(ring => ring.map(point => ({ ...point, x: point.x + 1000 })));
      region.bounds.x += 1000;
    }
    await expect(buildRunManifest(value.project, value.snapshots, identity)).rejects.toThrow(/leftover/i);
  });

  it('refuses a previous leftover report after a valid same-area placement moves within the sheet', async () => {
    const value = fixture();
    value.comparison.results[0].nest!.placements[0].x += 10;
    await expect(buildRunManifest(value.project, value.snapshots, identity)).rejects.toThrow(/leftover/i);
  });

  it('enforces the real aggregate input budget before constructing 300 large circular exclusions', () => {
    const disk: Part = {
      id: 'disk',
      name: 'Synthetic disk',
      quantity: 300,
      rotate: false,
      color: 0,
      loops: [{ type: 'circle', cx: 1000, cy: 1000, r: 1000 }],
    };
    const stock: Stock = {
      width: 2000,
      height: 2000,
      bedWidth: 2000,
      bedHeight: 2000,
      margin: 0,
      gap: 0,
      maxSheets: 300,
    };
    const nest: Nest = {
      placements: Array.from({ length: 300 }, (_, instance) => ({
        partId: disk.id,
        instance,
        sheet: instance,
        rotation: 0,
        x: 0,
        y: 0,
        width: 2000,
        height: 2000,
      })),
      unplaced: [],
      sheets: 300,
      area: 0,
      utilization: 0,
      method: 'Synthetic per-sheet circles',
    };
    expect(() => leftovers.analyzeLeftovers([disk], stock, nest)).toThrow(/input.vertex budget/i);
  });

  it('roundtrips a very narrow region near the maximum sheet coordinate without area cancellation', () => {
    const plate: Part = {
      id: 'large',
      name: 'Synthetic large plate',
      loops: [rect(19999.998, 20000)],
      quantity: 1,
      rotate: false,
      color: 0,
    };
    const stock: Stock = {
      width: 20000,
      height: 20000,
      bedWidth: 20000,
      bedHeight: 20000,
      margin: 0,
      gap: 0,
      maxSheets: 1,
    };
    const nest: Nest = {
      placements: [
        { partId: plate.id, instance: 0, sheet: 0, rotation: 0, x: 0, y: 0, width: 19999.998, height: 20000 },
      ],
      unplaced: [],
      sheets: 1,
      area: 0,
      utilization: 0,
      method: 'Synthetic thin region',
    };
    const analysis = leftovers.analyzeLeftovers([plate], stock, nest);
    const region = analysis.sheets[0].regions[0];
    expect(region.bounds.x).toBeGreaterThan(19999.99);
    expect(region.bounds.width).toBeLessThan(0.002);
    expect(region.area).toBeCloseTo(0.0012 * 19999.9992, 6);
    const saved = leftovers.leftoversToFile(analysis, { parts: [plate], stock, nest });
    expect(saved.sheets[0].regions[0].areaIn2).toBeCloseTo(region.area / 25.4 ** 2, 12);
  });
});
