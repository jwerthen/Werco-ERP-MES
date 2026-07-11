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

/**
 * FastAPI **422** (validation) responses return `detail` as an ARRAY of
 * `{loc, msg, type, ctx}` objects. Rendering that array as a React child throws
 * "Objects are not valid as a React child"; when it happens inside a toast — which
 * renders above the router's error boundary — it unmounts the ENTIRE SPA to a blank
 * page (the user loses all form input). `normalizeAxiosErrorDetail` collapses that
 * array into one readable string so every consumer that displays
 * `error.response.data.detail` gets a string for free.
 *
 * Only the ARRAY shape is a validation error. Structured refusals — 409s with an
 * OBJECT `detail` carrying a `code` (e.g. Process Sheets `OUT_OF_TOLERANCE`,
 * `STEPS_INCOMPLETE`; see processSheetErrors.ts) — and plain-string details are left
 * untouched so their parsers keep working.
 */

export interface ApiValidationErrorItem {
  loc?: Array<string | number>;
  msg?: string;
  type?: string;
}

// Leading location segments that name the request part, not the field.
const LOC_NOISE = new Set(['body', 'query', 'path', 'header', 'cookie']);

/**
 * Join a FastAPI 422 `detail` array into a single human line, e.g.
 * `[{loc:['body','required_date'],msg:'invalid date'}]` -> `"required_date: invalid date"`.
 * Never returns an empty string.
 */
export function formatValidationErrorArray(items: readonly unknown[]): string {
  const parts = items
    .map((raw): string | null => {
      if (typeof raw === 'string') return raw.trim() || null;
      if (!raw || typeof raw !== 'object') return null;
      const item = raw as ApiValidationErrorItem;
      if (typeof item.msg !== 'string' || !item.msg.trim()) return null;
      const loc = Array.isArray(item.loc) ? item.loc.filter((seg) => !LOC_NOISE.has(String(seg))) : [];
      const field = loc.join('.');
      return field ? `${field}: ${item.msg}` : item.msg;
    })
    .filter((p): p is string => typeof p === 'string' && p.length > 0);

  return parts.length > 0 ? parts.join('; ') : 'Validation failed. Please check your input and try again.';
}

/**
 * Normalize an axios-style error's `response.data.detail` IN PLACE: when it is the
 * FastAPI 422 array, replace it with a readable string and stash the raw array under
 * `response.data.detailItems` for any caller that wants field-level errors. No-op for
 * string / object / missing detail. Returns the same error for chaining.
 */
export function normalizeAxiosErrorDetail<E>(error: E): E {
  const data = (error as { response?: { data?: Record<string, unknown> } } | null | undefined)?.response?.data;
  if (data && Array.isArray(data.detail)) {
    const items = data.detail;
    data.detail = formatValidationErrorArray(items);
    if (data.detailItems === undefined) {
      data.detailItems = items;
    }
  }
  return error;
}

/**
 * Coerce any value into something safe to render as text (for toasts, inline error
 * strings, etc.). Strings pass through; 422 arrays are joined; objects prefer a
 * `msg`/`message`/`detail` field, else JSON. Guarantees a React-renderable string so a
 * mis-typed caller can never crash a component that renders the result.
 */
export function toDisplayString(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  if (Array.isArray(value)) return formatValidationErrorArray(value);
  if (typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    const preferred = obj.msg ?? obj.message ?? obj.detail;
    if (typeof preferred === 'string') return preferred;
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}
