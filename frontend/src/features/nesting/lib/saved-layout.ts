import type { NestingRunDetail } from '../../../types/nestingRun';
import { CURRENT_GEOMETRY_PROFILE, requireCurrentGeometryProfile } from './geometry-profile';
import { leftoversToFile } from './leftovers';
import { validateNest } from './nesting';
import { canonicalJSON } from './provenance';
import type { QuoteProject } from './quote-project';
import { stockFor } from './quoting';
import type { ServerOptionMessage } from './server-run';

const object = (value: unknown): Record<string, unknown> | undefined =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;
const check = (ok: unknown, message: string): void => {
  if (!ok) throw new Error(`Saved geometry: ${message}`);
};

/** Choose historical rules only from mutually bound immutable run and source identities. */
export type SavedGeometryRun = Pick<
  NestingRunDetail,
  'solver_version' | 'bundle_sha256' | 'node_version' | 'release_identity' | 'settings'
>;
export function validateSavedLayout(run: SavedGeometryRun, project: QuoteProject, output: ServerOptionMessage): string {
  const settings = run.settings;
  const runtime = object(settings.runtime);
  check(
    ['werco-contour-v4', 'werco-contour-v5', 'werco-contour-v6'].includes(run.solver_version ?? '') &&
      settings.solver_version === run.solver_version &&
      settings.protocol === 1 &&
      settings.units === 'mm' &&
      runtime?.protocol === 1 &&
      runtime.solver_version === run.solver_version &&
      typeof run.bundle_sha256 === 'string' &&
      /^[a-f0-9]{64}$/.test(run.bundle_sha256) &&
      runtime.bundle_sha256 === run.bundle_sha256 &&
      typeof run.node_version === 'string' &&
      /^v22\.\d+\.\d+$/.test(run.node_version) &&
      runtime.node_version === run.node_version &&
      typeof run.release_identity === 'string' &&
      run.release_identity.length > 0 &&
      runtime.release === run.release_identity,
    'solver and recorded runtime identity do not agree.'
  );
  const quote = project.groups.find(group => group.id === output.group_id)?.quote;
  const option = quote?.options.find(item => item.id === output.option_id && item.enabled);
  check(quote && option, 'the selected material group or stock option is missing.');
  if (!quote || !option) throw new Error('Saved geometry input is missing.');
  check(
    canonicalJSON(option) === canonicalJSON(output.result.option) &&
      canonicalJSON(stockFor(quote, option)) === canonicalJSON(output.stock),
    'stock geometry differs from the input revision.'
  );
  let expectedLeftovers: string;
  let label: string;
  if (run.solver_version === 'werco-contour-v6') {
    project.groups
      .filter(group => group.quote.parts.length)
      .forEach(group => requireCurrentGeometryProfile(group.quote.geometryProfile));
    requireCurrentGeometryProfile(quote.geometryProfile);
    requireCurrentGeometryProfile(settings.geometry_profile);
    check(
      canonicalJSON(settings.geometry_profile) === canonicalJSON(CURRENT_GEOMETRY_PROFILE),
      'unknown recorded geometry profile.'
    );
    expectedLeftovers = 'werco-leftovers-v3';
    label = 'Compensated clearance envelopes';
  } else {
    check(
      project.groups.every(group => group.quote.geometryProfile === undefined) &&
        !Object.prototype.hasOwnProperty.call(settings, 'geometry_profile'),
      'a historical solver cannot validate a current-profile input.'
    );
    if (run.solver_version === 'werco-contour-v4')
      check(
        project.groups.every(group => group.quote.options.every(stock => stock.exclusions === undefined)),
        'the recorded v4 solver did not support stock exclusions.'
      );
    expectedLeftovers = output.stock.exclusions?.length ? 'werco-leftovers-v2' : 'werco-leftovers-v1';
    label =
      run.solver_version === 'werco-contour-v4'
        ? 'Historical nominal clearance rules (v4)'
        : 'Historical nominal clearance rules with exclusions (v5)';
  }
  const { result } = output;
  if (result.nest) validateNest(quote.parts, output.stock, result.nest);
  const requested = quote.parts.reduce((count, part) => count + part.quantity, 0);
  const complete = Boolean(result.nest && result.nest.placements.length === requested && !result.nest.unplaced.length);
  check(
    output.requested === requested && result.complete === complete,
    'required quantities or completion state differ.'
  );
  check(!result.leftovers || (result.nest && !result.leftoverError), 'leftover analysis conflicts with its result.');
  if (result.leftovers && result.nest) {
    check(
      result.leftovers.version === expectedLeftovers,
      'leftover version does not match the recorded solver and source.'
    );
    leftoversToFile(result.leftovers, { parts: quote.parts, stock: output.stock, nest: result.nest });
  }
  return label;
}
