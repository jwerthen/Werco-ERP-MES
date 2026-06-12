/**
 * Shared translation of Axios timeout failures into actionable guidance.
 *
 * When a request exceeds its timeout, Axios rejects with code ECONNABORTED (or
 * ETIMEDOUT) and a raw "timeout of Nms exceeded" message — that string should
 * never reach the user. The Import Center flows (ImportCenter.tsx and the Users
 * page CSV import) share this helper so both phases of preview-before-commit
 * get an honest, phase-appropriate message:
 *
 * - validate (dry run): the parse is bounded server-side, so a timeout usually
 *   means a slow-to-read file — suggest trimming it down.
 * - commit: the server may STILL be importing after the client gives up, so a
 *   blind retry risks duplicates — steer the user back through a fresh dry run.
 */

/** Which half of the preview-before-commit flow the request belonged to. */
export type ImportPhase = 'validate' | 'commit';

export function isTimeoutError(err: unknown): boolean {
  const code = (err as { code?: string } | null | undefined)?.code;
  return code === 'ECONNABORTED' || code === 'ETIMEDOUT';
}

/**
 * Returns a friendly, phase-aware message when `err` is an Axios timeout, or
 * null so callers fall through to their existing error handling (backend
 * `detail` strings must keep passing through untouched).
 */
export function importTimeoutMessage(err: unknown, phase?: ImportPhase): string | null {
  if (!isTimeoutError(err)) return null;
  if (phase === 'validate') {
    return (
      'The server took too long to read this file. Heavily formatted spreadsheets can be slow — ' +
      'trim empty rows/columns or re-save as CSV, then try again.'
    );
  }
  if (phase === 'commit') {
    return (
      'The server took too long to respond, but the import may still be processing. ' +
      'Wait a moment, then re-run "Validate file (dry run)" before retrying — ' +
      'rows that already imported will show as "already exists".'
    );
  }
  return 'The server took too long to respond. Please try again.';
}
