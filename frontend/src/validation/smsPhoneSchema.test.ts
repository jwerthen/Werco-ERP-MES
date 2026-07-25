/**
 * smsPhoneSchema / normalizePhoneInput — the client-side half of the E.164 rule
 * for the My Settings mobile number.
 *
 * The SERVER is the source of truth (it re-validates and normalizes with
 * `phonenumbers`); this layer only strips human formatting, applies the US
 * shorthands, and rejects input that obviously can't be a phone number so the
 * user gets an inline error instead of a round-trip 422.
 */

import { isValidPhoneInput, normalizePhoneInput, smsPhoneSchema } from './schemas';

describe('normalizePhoneInput', () => {
  it('strips human formatting and assumes +1 for a bare 10-digit US number', () => {
    expect(normalizePhoneInput('(512) 555-0142')).toBe('+15125550142');
    expect(normalizePhoneInput('512.555.0142')).toBe('+15125550142');
    expect(normalizePhoneInput('512 555 0142')).toBe('+15125550142');
  });

  it('adds the leading + to an 11-digit US number that already carries the 1', () => {
    expect(normalizePhoneInput('1 (512) 555-0142')).toBe('+15125550142');
  });

  it('leaves an already-E.164 number alone (including non-US country codes)', () => {
    expect(normalizePhoneInput('+15125550142')).toBe('+15125550142');
    expect(normalizePhoneInput('+44 20 7946 0958')).toBe('+442079460958');
  });

  it('returns an empty string for blank input (meaning "clear my number")', () => {
    expect(normalizePhoneInput('')).toBe('');
    expect(normalizePhoneInput('   ')).toBe('');
    expect(normalizePhoneInput(null)).toBe('');
    expect(normalizePhoneInput(undefined)).toBe('');
  });
});

describe('isValidPhoneInput', () => {
  it.each(['(512) 555-0142', '+15125550142', '+44 20 7946 0958', '1-512-555-0142'])(
    'accepts %s',
    (value) => {
      expect(isValidPhoneInput(value)).toBe(true);
    },
  );

  it.each(['12345', 'not-a-number', '+0123456789', '+1', '555-0142'])('rejects %s', (value) => {
    expect(isValidPhoneInput(value)).toBe(false);
  });
});

describe('smsPhoneSchema', () => {
  it('accepts a blank value so a user can remove their number', () => {
    expect(smsPhoneSchema.safeParse({ phone: '' }).success).toBe(true);
    expect(smsPhoneSchema.safeParse({ phone: '   ' }).success).toBe(true);
  });

  it('accepts a formatted US number', () => {
    expect(smsPhoneSchema.safeParse({ phone: '(512) 555-0142' }).success).toBe(true);
  });

  it('rejects an implausible number with a country-code hint', () => {
    const result = smsPhoneSchema.safeParse({ phone: '12345' });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues[0].message).toMatch(/country code/i);
    }
  });
});
