import { rect, validateJob, type Job, type Part } from './nesting';
import { createBlankQuote, editSheetOption, quoteFromFile, quoteToFile, type Quote } from './quoting';
import { addImportedParts, createBlankProject, projectFromFile, projectToFile, validateProject } from './quote-project';
import { inToMm, jobFromFile, jobToFile } from './units';
import { type StockExclusion } from './stock-exclusions';
import { exclusionsToFile } from './stock-exclusion-files';

const regions = (): StockExclusion[] => [
  {
    id: 'edge',
    label: 'Edge defect',
    reason: 'Estimator-reported damaged material',
    clearance: inToMm(0.125),
    outline: {
      type: 'poly',
      points: [
        { x: 0, y: 0 },
        { x: 50.8, y: 0 },
        { x: 25.4, y: 25.4 },
      ],
    },
  },
  {
    id: 'spot',
    label: 'Surface defect',
    reason: 'Customer supplied unavailable area',
    clearance: 0,
    outline: { type: 'circle', cx: 127, cy: 127, r: 12.7 },
  },
];
const plate = (): Part => ({
  id: 'plate',
  name: 'Synthetic plate',
  quantity: 1,
  rotate: true,
  color: 0,
  loops: [rect(25.4, 12.7)],
});
function fixture(): Quote {
  return {
    ...createBlankQuote(),
    parts: [plate()],
    options: [{ id: 'sample', width: 254, height: 254, enabled: true, price: 100, exclusions: regions() }],
  };
}
const readJSON = (value: unknown): unknown => JSON.parse(JSON.stringify(value));

describe('stock exclusions in inch estimate files', () => {
  it('preserves exact polygon and circle geometry, labels and clearance through quote11 and project12', () => {
    const quote = fixture();
    const saved = quoteToFile(quote);
    expect(saved.version).toBe(11);
    expect(saved.options[0].exclusions).toEqual([
      {
        id: 'edge',
        label: 'Edge defect',
        reason: 'Estimator-reported damaged material',
        clearance: 0.125,
        outline: {
          type: 'poly',
          points: [
            { x: 0, y: 0 },
            { x: 2, y: 0 },
            { x: 1, y: 1 },
          ],
        },
      },
      {
        id: 'spot',
        label: 'Surface defect',
        reason: 'Customer supplied unavailable area',
        clearance: 0,
        outline: { type: 'circle', cx: 5, cy: 5, r: 0.5 },
      },
    ]);
    expect(quoteFromFile(readJSON(saved)).options[0].exclusions).toEqual(quote.options[0].exclusions);
    const project = projectToFile(createBlankProject(quote));
    expect(project.version).toBe(12);
    const reopened = projectFromFile(readJSON(project));
    expect(projectToFile(reopened).groups[0].quote.options).toEqual(project.groups[0].quote.options);
    expect(reopened.groups[0].quote.options[0].exclusions).toEqual(quote.options[0].exclusions);
  });

  it('preserves field absence for legacy files, explicit empty arrays for new files and rejects null', () => {
    const legacy = createBlankQuote();
    expect(projectToFile(createBlankProject(legacy)).version).toBe(4);
    expect(quoteToFile(legacy).version).toBe(3);
    expect(quoteFromFile(quoteToFile(legacy)).options[0]).not.toHaveProperty('exclusions');
    const quote = fixture();
    quote.options[0].exclusions = [];
    const saved = quoteToFile(quote);
    expect(saved.version).toBe(11);
    expect(quoteFromFile(saved).options[0].exclusions).toEqual([]);
    expect(projectToFile(createBlankProject(quote)).version).toBe(12);
    expect(() => quoteFromFile({ ...saved, options: [{ ...saved.options[0], exclusions: null }] })).toThrow();
    expect(() => quoteFromFile({ ...saved, version: 9 })).toThrow(/version 11/);
    expect(() => projectFromFile({ ...projectToFile(createBlankProject(quote)), version: 10 })).toThrow();
  });

  it('retains orientation and explicit spacing override in a mixed-version project', () => {
    const quote = fixture();
    quote.grainAxis = 'x';
    quote.parts[0].rotationMode = 'half-turn';
    quote.parts[0].grainAxis = 'x';
    quote.spacingMode = 'manual';
    quote.spacingOverride = {
      schema_version: 1,
      reason: 'Reviewed estimate allowance',
      changed_at: '2026-09-08T20:00:00Z',
    };
    const project = createBlankProject(quote);
    project.groups.push({ id: 'other', quote: { ...createBlankQuote(), material: 'Aluminum' } });
    const file = projectToFile(project);
    expect(file.groups.map(group => group.quote.version)).toEqual([11, 3]);
    const reopened = projectFromFile(readJSON(file));
    expect(reopened.groups[0].quote).toMatchObject({
      grainAxis: 'x',
      spacingMode: 'manual',
      spacingOverride: quote.spacingOverride,
      parts: [{ rotationMode: 'half-turn', grainAxis: 'x' }],
    });
    expect(reopened.groups[1].quote.options.every(option => option.exclusions === undefined)).toBe(true);
  });

  it('converts legacy standalone job13 stock geometry to inches and reopens it as an estimate without dropping regions', () => {
    const quote = fixture();
    const job: Job = {
      version: 1,
      name: quote.name,
      material: quote.material,
      thickness: quote.thickness,
      parts: quote.parts,
      bedConfirmed: false,
      stock: {
        width: 254,
        height: 254,
        bedWidth: 254,
        bedHeight: 254,
        gap: 3.175,
        margin: 9.525,
        maxSheets: 10,
        exclusions: regions(),
        grainAxis: 'x',
      },
    };
    const file = jobToFile(job);
    expect(file.version).toBe(13);
    expect(file.stock.exclusions).toEqual(exclusionsToFile(regions()));
    const reopenedJob = validateJob(jobFromFile(readJSON(file)));
    expect(reopenedJob.stock).toEqual(job.stock);
    expect(reopenedJob.parts[0].loops[0].type).toBe('poly');
    expect(reopenedJob.thickness).toBe(job.thickness);
    const reopened = quoteFromFile(readJSON(file));
    expect(reopened.options[0].exclusions).toEqual(regions());
    expect(reopened.options.slice(1).every(option => option.exclusions === undefined)).toBe(true);
    for (const version of [1, 2, 8]) expect(() => jobFromFile({ ...file, version })).toThrow(/version/);
  });

  it('rejects geometry-changing stock shrink atomically and keeps compatible stock edits', () => {
    const original = fixture().options[0];
    expect(() => editSheetOption(original, { width: 127 })).toThrow(/inside the gross sheet/);
    expect(original.width).toBe(254);
    expect(original.price).toBe(100);
    expect(original.exclusions).toEqual(regions());
    const changed = editSheetOption(original, { width: 300 });
    expect(changed.price).toBeNull();
    expect(changed.exclusions).toEqual(regions());
  });

  it('does not copy unavailable coordinates to a newly imported material/thickness group', () => {
    const project = createBlankProject(fixture());
    const part = { ...plate(), id: 'imported' };
    const changed = addImportedParts(project, [{ partIds: [part.id], material: 'Aluminum', thickness: 3.175 }], [part]);
    expect(changed.groups[0].quote.options[0].exclusions).toEqual(regions());
    expect(changed.groups[1].quote.options[0]).toMatchObject({ width: 254, height: 254, price: null });
    expect(changed.groups[1].quote.options[0]).not.toHaveProperty('exclusions');
  });

  it('counts disabled stock areas within the global source-vertex budget before topology work', () => {
    const project = createBlankProject(fixture());
    // Structural budget rejects these before their intentionally repetitive topology is examined.
    project.groups[0].quote.options = Array.from({ length: 11 }, (_, index) => ({
      id: `stock-${index}`,
      width: 254,
      height: 254,
      enabled: index === 0,
      price: null,
      exclusions: [
        {
          ...regions()[0],
          outline: { type: 'poly' as const, points: Array.from({ length: 2000 }, () => ({ x: 25.4, y: 25.4 })) },
        },
      ],
    }));
    expect(() => validateProject(project)).toThrow(/20,000 source vertices/);
  });

  it.each(['extra', 'units', 'machineRegion'])(
    'rejects unsupported exclusion metadata %s rather than interpreting it',
    field => {
      const file = quoteToFile(fixture());
      const option = file.options[0];
      expect(() =>
        quoteFromFile({
          ...file,
          options: [{ ...option, exclusions: [{ ...option.exclusions![0], [field]: 'unknown' }] }],
        })
      ).toThrow(/unsupported fields/);
    }
  );
});
