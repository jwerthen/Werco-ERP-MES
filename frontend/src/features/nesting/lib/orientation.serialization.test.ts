import { rect, validateJob, type Job, type Part } from './nesting';
import { compareSheets, createBlankQuote, quoteFromFile, quoteToFile, type Quote } from './quoting';
import { createBlankProject, projectFromFile, projectToFile } from './quote-project';
import { jobFromFile, jobToFile } from './units';
import { geometryHash } from './provenance';
import { buildRunManifest } from './run-manifest';
import { catalogQuoteFixture } from '../../../test-utils/nestingCatalogFixtures';

const plate = (): Part => ({
  id: 'plate',
  name: 'Synthetic plate',
  quantity: 1,
  rotate: true,
  color: 0,
  loops: [rect(25.4, 12.7)],
});
function constrainedQuote(): Quote {
  return { ...createBlankQuote(), grainAxis: 'x', parts: [{ ...plate(), rotationMode: 'half-turn', grainAxis: 'x' }] };
}
const readJSON = (value: unknown): unknown => JSON.parse(JSON.stringify(value));

describe('orientation restrictions in saved estimates and draft evidence', () => {
  it.each([false, true])(
    'keeps legacy project version and boolean rotation semantics with catalog binding=%s',
    catalog => {
      const quote = catalog ? catalogQuoteFixture().quote : createBlankQuote();
      quote.parts = [plate(), { ...plate(), id: 'locked', rotate: false }];
      const saved = projectToFile(createBlankProject(quote));
      expect(saved.version).toBe(catalog ? 5 : 4);
      expect(saved.groups[0].quote.version).toBe(3);
      const reopened = projectFromFile(readJSON(saved)).groups[0].quote;
      expect(reopened.parts.map(part => part.rotate)).toEqual([true, false]);
      expect(reopened.parts.every(part => part.rotationMode === undefined && part.grainAxis === undefined)).toBe(true);
      expect(reopened.grainAxis).toBeUndefined();
      expect(reopened.materialBinding).toEqual(quote.materialBinding);
    }
  );

  it('roundtrips source axes and explicit rotations in project6 and standalone quote7 without scaling direction metadata', async () => {
    const quote = constrainedQuote();
    const savedQuote = quoteToFile(quote);
    expect(savedQuote.version).toBe(7);
    expect(savedQuote.grainAxis).toBe('x');
    expect(savedQuote.parts[0]).toMatchObject({ rotationMode: 'half-turn', grainAxis: 'x' });
    const saved = projectToFile(createBlankProject(quote));
    expect(saved.version).toBe(6);
    expect(saved.groups[0].quote.version).toBe(7);
    for (const reopened of [quoteFromFile(readJSON(savedQuote)), projectFromFile(readJSON(saved)).groups[0].quote]) {
      expect(reopened.grainAxis).toBe('x');
      expect(reopened.parts[0]).toMatchObject({ rotationMode: 'half-turn', grainAxis: 'x' });
      expect(await geometryHash(reopened.parts[0])).toBe(await geometryHash(quote.parts[0]));
    }
    expect(await geometryHash(quote.parts[0])).toBe(await geometryHash(plate()));
  });

  it('roundtrips a mixed constrained/unconstrained project without applying grain to the other material group', () => {
    const project = createBlankProject(constrainedQuote());
    project.groups.push({
      id: 'group-2',
      quote: { ...createBlankQuote(), material: 'Aluminum', parts: [{ ...plate(), id: 'other' }] },
    });
    const saved = projectToFile(project);
    expect(saved.version).toBe(6);
    expect(saved.groups.map(group => group.quote.version)).toEqual([7, 3]);
    const reopened = projectFromFile(readJSON(saved));
    expect(reopened.groups[0].quote.grainAxis).toBe('x');
    expect(reopened.groups[1].quote.grainAxis).toBeUndefined();
    expect(reopened.groups[1].quote.parts[0].grainAxis).toBeUndefined();
  });

  it('dispatches constrained job8 separately and retains sheet grain when opening it as an estimate', () => {
    const quote = constrainedQuote();
    const job: Job = {
      version: 1,
      name: quote.name,
      material: quote.material,
      thickness: quote.thickness,
      parts: quote.parts,
      stock: {
        width: 254,
        height: 127,
        bedWidth: 254,
        bedHeight: 127,
        gap: 0,
        margin: 0,
        maxSheets: 1,
        grainAxis: 'x',
      },
      bedConfirmed: false,
    };
    const saved = jobToFile(job);
    expect(saved.version).toBe(8);
    expect(saved.stock).toMatchObject({ width: 10, height: 5, grainAxis: 'x' });
    const reopened = validateJob(jobFromFile(readJSON(saved)));
    expect(reopened.stock).toMatchObject({ width: 254, height: 127, grainAxis: 'x' });
    expect(reopened.parts[0]).toMatchObject({ rotationMode: 'half-turn', grainAxis: 'x' });
    const estimate = quoteFromFile(readJSON(saved));
    expect(estimate.grainAxis).toBe('x');
    expect(estimate.parts[0]).toMatchObject({ rotationMode: 'half-turn', grainAxis: 'x' });
  });

  it('retains a sheet-only restriction and rejects corrupt saved part or sheet axes', () => {
    const quote: Quote = { ...createBlankQuote(), grainAxis: 'y', parts: [plate()] };
    const saved = quoteToFile(quote);
    expect(saved.version).toBe(7);
    expect(quoteFromFile(readJSON(saved)).grainAxis).toBe('y');
    expect(() => quoteFromFile({ ...saved, grainAxis: 'unknown' })).toThrow(/grain/i);
    expect(() => quoteFromFile({ ...saved, parts: [{ ...saved.parts[0], rotationMode: 'free' }] })).toThrow(
      /rotation/i
    );
    expect(() => quoteFromFile({ ...saved, parts: [{ ...saved.parts[0], grainAxis: 0 }] })).toThrow(/grain/i);
  });

  it('invalidates cached draft evidence when a rotation policy or sheet axis changes', async () => {
    for (const change of ['policy', 'sheet'] as const) {
      const quote = constrainedQuote();
      const project = createBlankProject(quote);
      const snapshots = { 'group-1': { signature: JSON.stringify(quote), comparison: compareSheets(quote) } };
      if (change === 'policy') project.groups[0].quote.parts[0].rotationMode = 'fixed';
      else project.groups[0].quote.grainAxis = 'y';
      await expect(buildRunManifest(project, snapshots, { companyId: 1, estimatorId: 2 })).rejects.toThrow(
        /input has changed/i
      );
    }
  });

  it('refuses new constraints disguised as an older quote or job version', () => {
    const quote = constrainedQuote();
    expect(() => quoteFromFile({ ...quoteToFile(quote), version: 3 })).toThrow(/version/i);
    const job: Job = {
      version: 1,
      name: quote.name,
      material: quote.material,
      thickness: quote.thickness,
      parts: quote.parts,
      stock: {
        width: 254,
        height: 127,
        bedWidth: 254,
        bedHeight: 127,
        gap: 0,
        margin: 0,
        maxSheets: 1,
        grainAxis: 'x',
      },
      bedConfirmed: false,
    };
    expect(() => jobFromFile({ ...jobToFile(job), version: 2 })).toThrow(/version/i);
  });

  it('loads genuine legacy v1 millimeter jobs but refuses every new constraint field relabeled as v1', () => {
    const legacy: Job = {
      version: 1,
      name: 'Synthetic legacy job',
      material: 'Carbon steel',
      thickness: 3.175,
      parts: [{ ...plate(), rotate: false }],
      bedConfirmed: false,
      stock: { width: 254, height: 127, bedWidth: 254, bedHeight: 127, gap: 0, margin: 0, maxSheets: 1 },
    };
    expect(validateJob(jobFromFile(readJSON(legacy)))).toEqual(legacy);
    const oldQuote = quoteFromFile(readJSON(legacy));
    expect(oldQuote.options[0]).toMatchObject({ width: 254, height: 127 });
    expect(oldQuote.parts[0].rotate).toBe(false);
    for (const disguised of [
      { ...legacy, parts: [{ ...legacy.parts[0], rotationMode: 'half-turn' }] },
      { ...legacy, parts: [{ ...legacy.parts[0], grainAxis: 'x' }] },
      { ...legacy, stock: { ...legacy.stock, grainAxis: 'y' } },
    ]) {
      expect(() => jobFromFile(readJSON(disguised))).toThrow(/constraints require a version/i);
      expect(() => quoteFromFile(readJSON(disguised))).toThrow(/constraints require a version/i);
    }
  });

  it('explains the actual source-to-sheet grain intersection in draft evidence without claiming approval', async () => {
    const quote: Quote = {
      ...constrainedQuote(),
      grainAxis: 'y',
      parts: [{ ...plate(), rotationMode: 'quarter-turn', grainAxis: 'x' }],
    };
    const project = createBlankProject(quote);
    const snapshots = { 'group-1': { signature: JSON.stringify(quote), comparison: compareSheets(quote) } };
    const record = await buildRunManifest(project, snapshots, { companyId: 1, estimatorId: 2 });
    expect(record.content.results[0]).toMatchObject({ sheetGrainAxis: 'y', sheetGrainDirection: 'Along sheet width' });
    expect(record.content.results[0].parts[0]).toMatchObject({
      rotationMode: 'quarter-turn',
      rotationModeSource: 'explicit_selection',
      sourceGrainAxis: 'x',
      sourceGrainDirection: 'Horizontal in source drawing',
      allowedRotations: [90, 270],
      mirrorAllowed: false,
    });
    for (const alternative of record.content.results[0].alternatives) {
      expect(alternative.status).toBe('complete_valid_layout');
      expect([90, 270]).toContain(alternative.placements[0].rotationDegrees);
    }
    expect(record.content.solver).toMatchObject({
      version: 'werco-contour-v4',
      orientationPolicy: 'werco-orientation-v1',
    });
    expect(record.content.authoritativeApproval).toBe(false);
  });

  it('records unknown sheet grain as a blocking review issue with no completed cost estimate', async () => {
    const quote: Quote = { ...constrainedQuote(), grainAxis: undefined };
    quote.options = quote.options.map(option => ({ ...option, price: 100 }));
    const project = createBlankProject(quote);
    const snapshots = { 'group-1': { signature: JSON.stringify(quote), comparison: compareSheets(quote) } };
    const record = await buildRunManifest(project, snapshots, { companyId: 1, estimatorId: 2 });
    const group = record.content.results[0];
    expect(group.sheetGrainAxis).toBeNull();
    expect(group.parts[0].allowedRotations).toEqual([]);
    expect(group.parts[0].reviewFlags).toContainEqual(expect.stringMatching(/sheet grain is unknown/i));
    expect(group.recommendedOptionId).toBeNull();
    expect(
      group.alternatives.every(
        option => option.status === 'partial_valid_layout' && option.estimatedMaterialCostUSD === null
      )
    ).toBe(true);
    expect(record.content.authoritativeApproval).toBe(false);
  });
});
