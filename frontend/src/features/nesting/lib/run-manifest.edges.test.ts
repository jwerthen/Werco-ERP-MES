import { rect, type Nest } from './nesting';
import { buildRunManifest } from './run-manifest';
import { createBlankProject } from './quote-project';
import { compareSheets, createBlankQuote } from './quoting';
import { GEOMETRY_VERSION, IMPORTER_VERSION, geometryHash } from './provenance';

function fixture() {
  const quote = {
    ...createBlankQuote(),
    parts: [{ id: 'plate', name: 'Plate', quantity: 2, rotate: false, color: 0, loops: [rect(100, 50)] }],
  };
  const project = createBlankProject(quote);
  const comparison = compareSheets(quote);
  return { quote, project, comparison, snapshots: { 'group-1': { comparison, signature: JSON.stringify(quote) } } };
}
const identity = { companyId: 1, estimatorId: 2 };

describe('review export validates cached solver evidence', () => {
  it.each([
    [
      'outside stock',
      (nest: Nest) => {
        nest.placements[0].x = -1;
      },
      /margins/,
    ],
    [
      'unknown unplaced part',
      (nest: Nest) => {
        nest.unplaced.push({ partId: 'foreign', count: 1, reason: 'test' });
      },
      /unplaced/,
    ],
    [
      'duplicate instance',
      (nest: Nest) => {
        nest.placements[1].instance = nest.placements[0].instance;
      },
      /instance/,
    ],
    [
      'forbidden rotation',
      (nest: Nest) => {
        nest.placements[0].rotation = 180;
      },
      /grain/,
    ],
    [
      'invalid coordinate',
      (nest: Nest) => {
        nest.placements[0].x = NaN;
      },
      /placement/,
    ],
  ] as const)('refuses %s even with a cached complete flag', async (_label, corrupt, message) => {
    const value = fixture();
    corrupt(value.comparison.results[0].nest!);
    await expect(buildRunManifest(value.project, value.snapshots, identity)).rejects.toThrow(message);
  });

  it('rejects a false completion status and an unknown recommended stock', async () => {
    const status = fixture();
    status.comparison.results[0].complete = false;
    await expect(buildRunManifest(status.project, status.snapshots, identity)).rejects.toThrow('completion status');
    const recommended = fixture();
    recommended.comparison.recommendedId = 'not-a-stock';
    await expect(buildRunManifest(recommended.project, recommended.snapshots, identity)).rejects.toThrow('recommended');
  });

  it('marks changed source geometry for review and never promotes the exported file to approval', async () => {
    const value = fixture();
    const part = value.project.groups[0].quote.parts[0];
    part.provenance = {
      version: 1,
      sourceName: 'plate.dxf',
      sourceSha256: 'a'.repeat(64),
      sourceHashBasis: 'original-bytes',
      geometrySha256: await geometryHash({ loops: [rect(90, 50)] }),
      geometryVersion: GEOMETRY_VERSION,
      sourceUnits: 'unitless',
      resolvedUnits: 'in',
      unitDecision: 'assigned',
      importerVersion: IMPORTER_VERSION,
      warnings: ['Review the open contour repair.'],
    };
    value.snapshots['group-1'].signature = JSON.stringify(value.project.groups[0].quote);
    const manifest = await buildRunManifest(value.project, value.snapshots, identity);
    expect(manifest.content.results[0].parts[0].reviewFlags).toEqual(
      expect.arrayContaining([
        expect.stringContaining('differs'),
        expect.stringContaining('unitless'),
        expect.stringContaining('revision'),
        'Review the open contour repair.',
      ])
    );
    expect(manifest.content).toMatchObject({ status: 'draft_estimator_review', authoritativeApproval: false });
    expect(manifest.content.results[0].alternatives.every(result => result.remnantCreditUSD === 0)).toBe(true);
    expect(manifest.content.limitations).toContain(
      'This client-generated review record is not a signed approval or immutable server audit event.'
    );
  });
});
