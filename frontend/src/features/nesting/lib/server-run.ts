import { validateNest, type Stock } from './nesting';
import { projectFromFile, requireCurrentProjectGeometry } from './quote-project';
import { calculateSheetOption, stockFor, type OptionResult } from './quoting';
import { SOLVER_VERSION } from './run-manifest';

export const SERVER_RUN_PROFILE = Object.freeze({
  protocol: 1 as const,
  solverVersion: SOLVER_VERSION,
  nodeMajor: 22,
  maxOptionEvaluations: 36,
  maxInputBytes: 5 * 1024 * 1024,
  maxMessageBytes: 8 * 1024 * 1024,
});

export type ServerOptionMessage = {
  type: 'option';
  protocol: 1;
  input_sha256: string;
  sequence: number;
  group_id: string;
  option_id: string;
  units: 'mm';
  requested: number;
  stock: Stock;
  result: OptionResult;
};
export type ServerSummaryMessage = {
  type: 'summary';
  protocol: 1;
  input_sha256: string;
  evaluated_keys: { group_id: string; option_id: string }[];
  evaluated_count: number;
  complete_option_count: number;
  total_options: number;
  stop_reason: 'completed' | 'work_limit';
};

/** Input/hash binding is verified by the coordinator; geometry is verified here.
 * No clock, randomness, browser storage, fetch, or user-selected executable.
 * Yield only complete option calculations, so the coordinator can retain them
 * even if a later option exceeds the external wall-time or memory budget.
 */
export function* calculateSavedProject(
  estimate: unknown,
  inputSha256: string
): Generator<ServerOptionMessage | ServerSummaryMessage> {
  if (!/^[a-f0-9]{64}$/.test(inputSha256)) throw new Error('Invalid input fingerprint.');
  const project = projectFromFile(estimate);
  requireCurrentProjectGeometry(project);
  const groups = project.groups.filter(group => group.quote.parts.length > 0);
  if (!groups.length) throw new Error('A saved server calculation requires at least one part.');
  const totalOptions = groups.reduce((count, group) => count + group.quote.options.filter(o => o.enabled).length, 0);
  const evaluated: { group_id: string; option_id: string }[] = [];
  let completeOptions = 0;
  for (const group of groups) {
    for (const option of group.quote.options.filter(o => o.enabled)) {
      if (evaluated.length >= SERVER_RUN_PROFILE.maxOptionEvaluations) break;
      const stock = stockFor(group.quote, option);
      const result = calculateSheetOption(group.quote, option);
      // This check uses the original imported contours, outside the calculation
      // function's error-to-option boundary. A bad result is never emitted.
      if (result.nest) validateNest(group.quote.parts, stock, result.nest);
      const requested = group.quote.parts.reduce((count, part) => count + part.quantity, 0);
      const complete =
        !!result.nest && result.nest.placements.length === requested && result.nest.unplaced.length === 0;
      if (complete !== result.complete) throw new Error('Invalid calculation completion state.');
      evaluated.push({ group_id: group.id, option_id: option.id });
      if (complete) completeOptions += 1;
      yield {
        type: 'option',
        protocol: 1,
        input_sha256: inputSha256,
        sequence: evaluated.length,
        group_id: group.id,
        option_id: option.id,
        units: 'mm',
        requested,
        stock,
        result,
      };
    }
    if (evaluated.length >= SERVER_RUN_PROFILE.maxOptionEvaluations) break;
  }
  yield {
    type: 'summary',
    protocol: 1,
    input_sha256: inputSha256,
    evaluated_keys: evaluated,
    evaluated_count: evaluated.length,
    complete_option_count: completeOptions,
    total_options: totalOptions,
    stop_reason: evaluated.length === totalOptions ? 'completed' : 'work_limit',
  };
}
