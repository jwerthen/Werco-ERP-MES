import v4 from '../../../test-utils/fixtures/nesting-history-v4.json';
import v5 from '../../../test-utils/fixtures/nesting-history-v5.json';
import { CURRENT_GEOMETRY_PROFILE } from './geometry-profile';
import { validateNest } from './nesting';
import { canonicalJSON, sha256 } from './provenance';
import { projectFromFile, upgradeProjectGeometry } from './quote-project';
import { stockFor } from './quoting';
import { validateSavedLayout, type SavedGeometryRun } from './saved-layout';
import type { ServerOptionMessage } from './server-run';

for (const fixture of [v4, v5]) {
  const solver = fixture.run.solver_version;
  test(`frozen ${solver} input and actual original-kernel checkpoint remain valid and byte-stable`, async () => {
    const before = JSON.stringify(fixture);
    expect(await sha256(canonicalJSON(fixture.estimate))).toBe(fixture.input_sha256);
    expect(await sha256(canonicalJSON(fixture.checkpoint))).toBe(fixture.checkpoint_sha256);
    const project = projectFromFile(fixture.estimate);
    const output = fixture.checkpoint as unknown as ServerOptionMessage;
    expect(validateSavedLayout(fixture.run, project, output)).toMatch(/Historical nominal clearance/);
    const upgraded = upgradeProjectGeometry(project).groups[0].quote;
    expect(() => validateNest(upgraded.parts, stockFor(upgraded, upgraded.options[0]), output.result.nest!)).toThrow();
    expect(JSON.stringify(fixture)).toBe(before);
    expect(validateSavedLayout(fixture.run, project, output)).toMatch(/Historical/);
  });
}

test('history cannot select weaker validation from contradictory source, solver, runtime or report fields', () => {
  const project = projectFromFile(v5.estimate);
  const output = v5.checkpoint as unknown as ServerOptionMessage;
  const run: SavedGeometryRun = JSON.parse(JSON.stringify(v5.run));
  expect(() => validateSavedLayout({ ...run, solver_version: 'werco-contour-v4' }, project, output)).toThrow(
    /runtime identity/
  );
  expect(() => validateSavedLayout({ ...run, bundle_sha256: 'f'.repeat(64) }, project, output)).toThrow(
    /runtime identity/
  );
  expect(() => validateSavedLayout({ ...run, settings: {} }, project, output)).toThrow(/runtime identity/);
  expect(() => validateSavedLayout(run, upgradeProjectGeometry(project), output)).toThrow();
  expect(() =>
    validateSavedLayout(
      { ...run, settings: { ...run.settings, geometry_profile: CURRENT_GEOMETRY_PROFILE } },
      project,
      output
    )
  ).toThrow(/historical solver/);
  const badVersion = JSON.parse(JSON.stringify(output)) as ServerOptionMessage;
  badVersion.result.leftovers!.version = 'werco-leftovers-v1';
  expect(() => validateSavedLayout(run, project, badVersion)).toThrow(/leftover version/);
  const badCount = { ...output, requested: output.requested + 1 };
  expect(() => validateSavedLayout(run, project, badCount)).toThrow(/quantities/);
});
