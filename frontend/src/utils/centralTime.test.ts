import { formatCentralTime } from './centralTime';

describe('centralTime', () => {
  it('treats timezone-less backend datetimes as UTC before formatting for Tulsa', () => {
    expect(formatCentralTime('2026-05-01T18:17:00', { timeZoneName: 'short' })).toBe(
      '1:17 PM CDT'
    );
  });

  it('formats explicit offsets in Tulsa time', () => {
    expect(formatCentralTime('2026-05-01T13:17:00-05:00', { timeZoneName: 'short' })).toBe(
      '1:17 PM CDT'
    );
  });
});
