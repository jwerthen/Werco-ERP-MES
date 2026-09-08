/** At most 75 batch write starts/minute, leaving headroom under the existing 100/minute API limit. */
export function createSourcePacer() {
  let nextAt = 0;
  return async (signal: AbortSignal) => {
    if (signal.aborted) throw new Error('Attachment request stopped.');
    const wait = Math.max(0, nextAt - Date.now());
    if (wait)
      await new Promise<void>((resolve, reject) => {
        const abort = () => {
          clearTimeout(timer);
          reject(new Error('Attachment request stopped.'));
        };
        const timer = setTimeout(() => {
          signal.removeEventListener('abort', abort);
          resolve();
        }, wait);
        signal.addEventListener('abort', abort, { once: true });
      });
    if (signal.aborted) throw new Error('Attachment request stopped.');
    nextAt = Date.now() + 800;
  };
}

/** The timestamp controls an explicit resume button, never an automatic network retry. */
export function sourceRetryAt(cause: unknown, now = Date.now()): number | null {
  const response = (cause as { response?: { status?: number; headers?: Record<string, unknown> } })?.response;
  if (response?.status !== 429) return null;
  const value = response.headers?.['retry-after'];
  const text = typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const seconds = /^\d+(?:\.\d+)?$/.test(text) ? Number(text) : NaN;
  const date = seconds >= 0 ? now + seconds * 1000 : Date.parse(text);
  return Number.isFinite(date) && date <= 8.64e15 ? Math.max(now + 1000, date) : now + 60_000;
}
