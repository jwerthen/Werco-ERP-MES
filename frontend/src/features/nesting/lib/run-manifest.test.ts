import { buildRunManifest } from './run-manifest';
import { canonicalJSON, sha256 } from './provenance';
import { createBlankProject } from './quote-project';
import { compareSheets, createBlankQuote } from './quoting';
import { rect, validateNest } from './nesting';

function fixture() {
  const quote = {
    ...createBlankQuote(),
    parts: [{ id: 'plate', name: 'Plate', revision: 'A', loops: [rect(100, 50)], quantity: 2, rotate: true, color: 0 }],
  };
  const project = createBlankProject(quote);
  return { project, snapshots: { 'group-1': { comparison: compareSheets(quote), signature: JSON.stringify(quote) } } };
}
const identity = { companyId: 7, estimatorId: 9 };

describe('Draft nesting review records', () => {
  it('reproduces content digests, records all alternatives and preserves tenant context', async () => {
    const { project, snapshots } = fixture();
    const first = await buildRunManifest(project, snapshots, identity);
    const second = await buildRunManifest(project, snapshots, identity);
    expect(first.contentSha256).toBe(second.contentSha256);
    expect(first.contentSha256).toBe(await sha256(canonicalJSON(first.content)));
    expect(first.content.inputProject.units).toBe('in');
    expect(first.content.authoritativeApproval).toBe(false);
    expect(first.content.results[0].alternatives).toHaveLength(3);
    expect(first.content.results[0].parts[0]).toMatchObject({ revision: 'A', quantity: 2, mirrorAllowed: false });
    expect(
      first.content.results[0].alternatives.every(
        option => option.status === 'complete_valid_layout' && option.remnantCreditUSD === 0
      )
    ).toBe(true);
    expect((await buildRunManifest(project, snapshots, { ...identity, companyId: 8 })).contentSha256).not.toBe(
      first.contentSha256
    );
  });

  it('refuses a stale revision, omitted option or changed price', async () => {
    const { project, snapshots } = fixture();
    project.groups[0].quote.parts[0].revision = 'B';
    await expect(buildRunManifest(project, snapshots, identity)).rejects.toThrow('input has changed');
    const missing = fixture();
    missing.snapshots['group-1'].comparison.results.pop();
    await expect(buildRunManifest(missing.project, missing.snapshots, identity)).rejects.toThrow('every enabled');
    const price = fixture();
    price.snapshots['group-1'].comparison.results[0].option = {
      ...price.snapshots['group-1'].comparison.results[0].option,
      price: 999,
    };
    await expect(buildRunManifest(price.project, price.snapshots, identity)).rejects.toThrow('prices differ');
  });

  it('revalidates geometry rather than trusting a cached success flag', async () => {
    const { project, snapshots } = fixture();
    const nest = snapshots['group-1'].comparison.results[0].nest!;
    nest.placements[1] = { ...nest.placements[1], x: nest.placements[0].x, y: nest.placements[0].y };
    await expect(buildRunManifest(project, snapshots, identity)).rejects.toThrow('Compensated part envelopes overlap');
  });

  it('checks unplaced identity/counts and empty-sheet conservation', () => {
    const stock = { width: 300, height: 300, margin: 0, gap: 0, maxSheets: 2, bedWidth: 300, bedHeight: 300 };
    const nest = {
      placements: [],
      unplaced: [{ partId: 'unknown', count: 1, reason: 'missing' }],
      sheets: 0,
      area: 0,
      utilization: 0,
      method: 'test',
    };
    expect(() => validateNest([], stock, nest)).toThrow('unplaced');
    expect(() => validateNest([], stock, { ...nest, unplaced: [], sheets: 1 })).toThrow('empty or missing');
    expect(() => validateNest([], stock, { ...nest, unplaced: [], sheets: -1 })).toThrow('sheet count');
  });
});
