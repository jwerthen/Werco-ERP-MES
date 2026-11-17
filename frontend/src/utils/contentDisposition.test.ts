import { decodeExtValue, downloadFilename, filenameFromContentDisposition } from './contentDisposition';

describe('filenameFromContentDisposition', () => {
  it('prefers the RFC 5987 filename* over the ASCII fallback (non-ASCII name)', () => {
    const header =
      'attachment; filename="Uberstunden_Ruckstand.csv"; filename*=UTF-8\'\'%C3%9Cberstunden%20R%C3%BCckstand%20%E2%82%AC.csv';
    expect(filenameFromContentDisposition(header)).toBe('Überstunden Rückstand €.csv');
  });

  it('decodes a name containing quotes and an apostrophe from filename*', () => {
    const header = 'attachment; filename="Bob_s _best_.csv"; filename*=UTF-8\'\'Bob%27s%20%22best%22%20jobs.csv';
    expect(filenameFromContentDisposition(header)).toBe('Bob\'s "best" jobs.csv');
  });

  it('unescapes a quoted filename containing an escaped quote and a semicolon', () => {
    const header = 'attachment; filename="Weekly \\"hot\\" list; v2.csv"';
    expect(filenameFromContentDisposition(header)).toBe('Weekly "hot" list; v2.csv');
  });

  it('falls back to filename when filename* is malformed or in an unknown charset', () => {
    expect(filenameFromContentDisposition('attachment; filename="plain.csv"; filename*=UTF-8\'\'%E2%82')).toBe(
      'plain.csv'
    );
    expect(filenameFromContentDisposition('attachment; filename="plain.csv"; filename*=KOI8-R\'\'%C1.csv')).toBe(
      'plain.csv'
    );
    expect(filenameFromContentDisposition('attachment; filename="plain.csv"; filename*=no-quotes.csv')).toBe(
      'plain.csv'
    );
  });

  it('reads an unquoted token filename and is case-insensitive about parameter names', () => {
    expect(filenameFromContentDisposition('attachment; FILENAME=report.csv')).toBe('report.csv');
    expect(filenameFromContentDisposition("attachment; FileName*=utf-8'en'caf%C3%A9.csv")).toBe('café.csv');
  });

  it('decodes ISO-8859-1 ext-values byte by byte', () => {
    expect(decodeExtValue("ISO-8859-1''%A3%20rates.csv")).toBe('£ rates.csv');
  });

  it('never returns a path: separators and control characters are neutralized', () => {
    expect(filenameFromContentDisposition("attachment; filename*=UTF-8''..%2F..%2Fetc%2Fpasswd")).toBe(
      '.._.._etc_passwd'
    );
    expect(filenameFromContentDisposition('attachment; filename="a\\\\b.csv"')).toBe('a_b.csv');
    expect(filenameFromContentDisposition("attachment; filename*=UTF-8''line%0Abreak.csv")).toBe('linebreak.csv');
  });

  it('returns null when no filename is named', () => {
    expect(filenameFromContentDisposition(undefined)).toBeNull();
    expect(filenameFromContentDisposition('')).toBeNull();
    expect(filenameFromContentDisposition('attachment')).toBeNull();
    expect(filenameFromContentDisposition('attachment; filename=""')).toBeNull();
    expect(filenameFromContentDisposition("attachment; filename*=UTF-8''..")).toBeNull();
  });
});

describe('downloadFilename', () => {
  it('uses the header name, else the fallback', () => {
    expect(downloadFilename("attachment; filename*=UTF-8''%C3%A9t%C3%A9.csv", 'x.csv')).toBe('été.csv');
    expect(downloadFilename(undefined, 'custom_report.csv')).toBe('custom_report.csv');
    expect(downloadFilename('inline', 'custom_report.csv')).toBe('custom_report.csv');
  });
});
