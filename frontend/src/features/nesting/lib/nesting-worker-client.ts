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
