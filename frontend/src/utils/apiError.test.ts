/**
 * Shared Axios-timeout translation used by the Import Center and the Users page
 * CSV import: timeouts become phase-aware guidance; everything else returns null
 * so callers fall through to their own error handling (backend `detail` strings
 * keep passing through untouched).
 */

import {
  formatValidationErrorArray,
  importTimeoutMessage,
  isTimeoutError,
  normalizeAxiosErrorDetail,
  toDisplayString,
} from './apiError';

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

describe('formatValidationErrorArray', () => {
  it('joins a FastAPI 422 detail array as "field: message"', () => {
    const detail = [
      { type: 'date_from_datetime_parsing', loc: ['body', 'required_date'], msg: 'Input should be a valid date' },
      { type: 'greater_than', loc: ['body', 'lines', 0, 'part_id'], msg: 'Input should be greater than 0' },
    ];
    expect(formatValidationErrorArray(detail)).toBe(
      'required_date: Input should be a valid date; lines.0.part_id: Input should be greater than 0',
    );
  });

  it('drops the body/query/path noise segment and uses the bare msg when no field remains', () => {
    expect(formatValidationErrorArray([{ loc: ['body'], msg: 'Field required' }])).toBe('Field required');
  });

  it('tolerates malformed items and never returns an empty string', () => {
    expect(formatValidationErrorArray([null, {}, { msg: '' }, 42])).toMatch(/check your input/i);
    expect(formatValidationErrorArray([])).toMatch(/check your input/i);
  });
});

describe('normalizeAxiosErrorDetail', () => {
  it('replaces a 422 array detail with a string and preserves the raw items', () => {
    const err = {
      response: { status: 422, data: { detail: [{ loc: ['body', 'name'], msg: 'Field required' }] } },
    };
    const items = err.response.data.detail;
    normalizeAxiosErrorDetail(err);
    expect(typeof err.response.data.detail).toBe('string');
    expect(err.response.data.detail).toBe('name: Field required');
    // Raw structured items are stashed for any field-level consumer.
    expect((err.response.data as any).detailItems).toBe(items);
  });

  it('leaves a plain-string detail untouched (e.g. a 500 or 404)', () => {
    const err = { response: { status: 500, data: { detail: 'Internal server error' } } };
    normalizeAxiosErrorDetail(err);
    expect(err.response.data.detail).toBe('Internal server error');
    expect((err.response.data as any).detailItems).toBeUndefined();
  });

  it('leaves a structured OBJECT detail untouched (Process Sheets 409 refusals)', () => {
    const detail = { code: 'OUT_OF_TOLERANCE', detail: 'Measured 10.12 is outside tolerance', measured: 10.12 };
    const err = { response: { status: 409, data: { detail } } };
    normalizeAxiosErrorDetail(err);
    expect(err.response.data.detail).toBe(detail);
  });

  it('no-ops when there is no response (network error)', () => {
    const err = { message: 'Network Error' };
    expect(() => normalizeAxiosErrorDetail(err)).not.toThrow();
    expect(normalizeAxiosErrorDetail(err)).toBe(err);
  });
});

describe('toDisplayString', () => {
  it('passes strings through and joins arrays', () => {
    expect(toDisplayString('hi')).toBe('hi');
    expect(toDisplayString([{ loc: ['body', 'qty'], msg: 'must be > 0' }])).toBe('qty: must be > 0');
  });

  it('prefers a msg/message/detail field on objects, else JSON', () => {
    expect(toDisplayString({ message: 'boom' })).toBe('boom');
    expect(toDisplayString({ code: 'X' })).toBe('{"code":"X"}');
  });

  it('handles null/number without throwing', () => {
    expect(toDisplayString(null)).toBe('');
    expect(toDisplayString(7)).toBe('7');
  });
});
