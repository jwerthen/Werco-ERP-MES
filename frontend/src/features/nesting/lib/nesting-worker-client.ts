import { buildStagePlan, type RemnantStageMessage, type RemnantSummaryMessage } from './remnant-planning';
import NestingWorker from './nesting.worker?worker';
import type { Comparison, Quote } from './quoting';

/** Keep geometry search off the UI thread. A cancelled request cannot publish stale results. */
export function compareSheetsInWorker(quote: Quote, options: { signal?: AbortSignal } = {}): Promise<Comparison> {
  return new Promise((resolve, reject) => {
    if (options.signal?.aborted) {
      reject(new DOMException('Nesting cancelled.', 'AbortError'));
      return;
    }
    const worker = new NestingWorker();
    let settled = false;
    const finish = (comparison?: Comparison, error?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      options.signal?.removeEventListener('abort', cancel);
      worker.terminate();
      if (error) reject(error);
      else resolve(comparison!);
    };
    const cancel = () => finish(undefined, new DOMException('Nesting cancelled.', 'AbortError'));
    const deadline = setTimeout(
      () =>
        finish(
          undefined,
          new Error(
            'This nest exceeded the calculation time limit. Split the job into separate estimates or simplify unnecessary drawing detail.'
          )
        ),
      120_000
    );
    options.signal?.addEventListener('abort', cancel, { once: true });
    worker.onmessage = (event: MessageEvent<{ ok: boolean; comparison?: Comparison; error?: string }>) => {
      if (event.data.ok && event.data.comparison) finish(event.data.comparison);
      else finish(undefined, new Error(event.data.error || 'Could not compare sheet sizes.'));
    };
    worker.onerror = () =>
      finish(undefined, new Error('The nesting worker could not finish. Please retry the comparison.'));
    try {
      worker.postMessage({ quote });
    } catch (error) {
      finish(undefined, error instanceof Error ? error : new Error('Could not start nesting.'));
    }
  });
}

/** One worker/deadline for the whole conditional plan; callers retain verified stage callbacks on cancellation. */
export function compareProjectStagesInWorker(
  rawEstimate: unknown,
  inputSha256: string,
  options: { signal?: AbortSignal; onStage: (stage: RemnantStageMessage) => void }
): Promise<RemnantSummaryMessage> {
  return new Promise((resolve, reject) => {
    if (options.signal?.aborted) return reject(new DOMException('Nesting cancelled.', 'AbortError'));
    if (!/^[a-f0-9]{64}$/.test(inputSha256)) return reject(new Error('Invalid planning input fingerprint.'));
    if (new TextEncoder().encode(JSON.stringify(rawEstimate)).length > 5 * 1024 * 1024)
      return reject(new Error('The planning input exceeds its 5 MiB transport limit.'));
    const plan = buildStagePlan(rawEstimate);
    const worker = new NestingWorker();
    let settled = false,
      received = 0,
      complete = 0,
      retainedBytes = 0;
    const finish = (summary?: RemnantSummaryMessage, error?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      options.signal?.removeEventListener('abort', cancel);
      worker.terminate();
      if (error) reject(error);
      else resolve(summary!);
    };
    const cancel = () =>
      finish(undefined, new DOMException('Nesting cancelled. Completed stages remain available.', 'AbortError'));
    const deadline = setTimeout(
      () =>
        finish(
          undefined,
          new Error(
            'The staged nest reached its 120-second limit. Completed full-sheet and conditional stages remain available for review.'
          )
        ),
      120_000
    );
    options.signal?.addEventListener('abort', cancel, { once: true });
    worker.onmessage = (
      event: MessageEvent<RemnantStageMessage | RemnantSummaryMessage | { ok: false; error: string }>
    ) => {
      if (settled) return;
      try {
        const message = event.data;
        const bytes = new TextEncoder().encode(JSON.stringify(message)).length + 1;
        if (bytes > 8 * 1024 * 1024)
          throw new Error('The planning worker exceeded its per-stage output limit. Earlier stages remain available.');
        if ('ok' in message) throw new Error(message.error || 'The staged nesting worker stopped.');
        if (message.protocol !== 2 || message.input_sha256 !== inputSha256)
          throw new Error('The planning worker returned another input.');
        if (message.type === 'stage') {
          if (retainedBytes + bytes > 24 * 1024 * 1024)
            throw new Error('The planning worker reached its retained-output limit. Earlier stages remain available.');
          retainedBytes += bytes;
          const expected = plan[received];
          if (
            !expected ||
            ['sequence', 'stage_id', 'stage_kind', 'group_id', 'option_id', 'depends_on'].some(
              key => message[key as keyof typeof expected] !== expected[key as keyof typeof expected]
            ) ||
            message.units !== 'mm'
          )
            throw new Error('The planning worker returned an unexpected stage.');
          options.onStage(message);
          received++;
          if (
            message.stage_kind !== 'recorded_piece' &&
            (message.stage_kind === 'residual' && message.requested === 0
              ? message.result === null && message.stock === null && message.instance_map.length === 0
              : message.result?.complete)
          )
            complete++;
        } else {
          if (
            message.type !== 'summary' ||
            message.evaluated_count !== received ||
            message.complete_option_count !== complete ||
            received !== plan.length ||
            message.total_options !== plan.length ||
            message.stop_reason !== 'completed' ||
            JSON.stringify(message.evaluated_keys) !==
              JSON.stringify(plan.map(stage => ({ stage_id: stage.stage_id, group_id: stage.group_id })))
          )
            throw new Error('The planning worker summary is incomplete or inconsistent.');
          finish(message);
        }
      } catch (error) {
        finish(undefined, error instanceof Error ? error : new Error('Invalid planning worker response.'));
      }
    };
    worker.onerror = () =>
      finish(undefined, new Error('The planning worker stopped. Completed stages remain available.'));
    try {
      worker.postMessage({ kind: 'project-stages', estimate: rawEstimate, inputSha256 });
    } catch (error) {
      finish(undefined, error instanceof Error ? error : new Error('Could not start planning.'));
    }
  });
}
