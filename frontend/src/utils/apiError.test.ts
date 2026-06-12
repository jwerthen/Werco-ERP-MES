/**
 * Shared Axios-timeout translation used by the Import Center and the Users page
 * CSV import: timeouts become phase-aware guidance; everything else returns null
 * so callers fall through to their own error handling (backend `detail` strings
 * keep passing through untouched).
 */

import { importTimeoutMessage, isTimeoutError } from './apiError';

const timeoutError = (code: string) => Object.assign(new Error('timeout of 120000ms exceeded'), { code });

describe('isTimeoutError', () => {
  it('recognizes the Axios timeout codes', () => {
    expect(isTimeoutError(timeoutError('ECONNABORTED'))).toBe(true);
    expect(isTimeoutError(timeoutError('ETIMEDOUT'))).toBe(true);
  });

  it('rejects non-timeout failures and non-errors', () => {
    expect(isTimeoutError({ response: { status: 400, data: { detail: 'Unsupported file type' } } })).toBe(false);
    expect(isTimeoutError(new Error('boom'))).toBe(false);
    expect(isTimeoutError(null)).toBe(false);
    expect(isTimeoutError(undefined)).toBe(false);
  });
});

describe('importTimeoutMessage', () => {
  it('gives slim-the-file guidance for validation (dry run) timeouts', () => {
    const message = importTimeoutMessage(timeoutError('ECONNABORTED'), 'validate');
    expect(message).toMatch(/took too long/i);
    expect(message).toMatch(/trim empty rows\/columns or re-save as CSV/i);
  });

  it('warns that a timed-out commit may still be processing server-side', () => {
    const message = importTimeoutMessage(timeoutError('ECONNABORTED'), 'commit');
    expect(message).toMatch(/took too long/i);
    expect(message).toMatch(/may still be processing/i);
    expect(message).toMatch(/re-run "Validate file \(dry run\)"/i);
    expect(message).toMatch(/already exists/i);
  });

  it('falls back to a generic timeout message when no phase is given', () => {
    expect(importTimeoutMessage(timeoutError('ETIMEDOUT'))).toMatch(/took too long/i);
  });

  it('returns null for non-timeout errors so callers fall through', () => {
    expect(importTimeoutMessage({ response: { data: { detail: 'Unsupported file type' } } }, 'validate')).toBeNull();
    expect(importTimeoutMessage(new Error('boom'), 'commit')).toBeNull();
  });
});
