/**
 * operationNumberText — the BARE identifier, and its contract with the label.
 *
 * Background: `operation_number` is an IDENTIFIER column, but `WorkOrderNew`
 * used to mint a display LABEL into it (`Op 10`). The mint is fixed forward and
 * there is deliberately NO backfill, so the column is permanently mixed —
 * rows written before the fix hold `Op 10`, rows written after hold `10`. Screens
 * that print the raw value under their own `Op #` / `Seq` header therefore had to
 * choose between showing the prefix twice on old rows or showing two different
 * strings for the same operation number.
 *
 * The thing actually worth pinning is not either function on its own — it is
 * that they share ONE definition of "what counts as a prefix". Two regexes is how
 * the office and floor spellings drifted apart to begin with, so the agreement
 * property below is the load-bearing test in this file; the per-input cases are
 * documentation.
 */
import {
  OPERATION_LABEL_FALLBACK,
  formatOperationLabel,
  hasOperationNumber,
  operationNumberText,
} from './operationLabel';

/** Every stored spelling of operation 10 this system has actually produced. */
const SPELLINGS_OF_TEN = [
  '10',
  'OP10',
  'Op 10',
  'op-10',
  'op 10',
  'OP.10',
  'Op #10',
  'Operation 10',
  '  Op 10  ',
];

/** Values that name no operation at all. */
const NAMES_NOTHING: Array<string | number | null | undefined> = [
  null,
  undefined,
  '',
  '   ',
  'Op',
  'OPERATION',
  'OP-',
];

/** Values with no prefix to absorb — they must survive byte-for-byte. */
const PASSES_THROUGH: Array<[string, string]> = [
  ['A10', 'A10'],
  ['FINAL', 'FINAL'],
  ['OPTICAL', 'OPTICAL'],
  ['010', '010'],
  ['10.5', '10.5'],
];

describe('operationNumberText', () => {
  it.each(SPELLINGS_OF_TEN)('renders every stored spelling of operation 10 as a bare "10" (%s)', (stored) => {
    expect(operationNumberText(stored)).toBe('10');
  });

  it('is what makes a legacy row and a new row render identically', () => {
    // The whole point of the helper: these two are the SAME operation, written on
    // either side of the mint fix, and a shared column must not show them apart.
    expect(operationNumberText('Op 10')).toBe(operationNumberText('10'));
  });

  it.each(NAMES_NOTHING.map((v) => [String(v), v] as const))(
    'is null-safe for %s — empty string, not a sentinel',
    (_label, stored) => {
      expect(operationNumberText(stored)).toBe('');
    }
  );

  it('returns EMPTY STRING rather than an em-dash, so each caller keeps its own fallback', () => {
    // The traveler falls back to the numeric `sequence`; the routing tables fall
    // back to an empty cell. A `||` chain gives them both that for free, which an
    // em-dash sentinel would silently break.
    expect(operationNumberText(null) || 'seq-30').toBe('seq-30');
    expect(operationNumberText('Op 10') || 'seq-30').toBe('10');
  });

  it.each(PASSES_THROUGH)('passes %s through untouched — no prefix to absorb', (stored, expected) => {
    expect(operationNumberText(stored)).toBe(expected);
  });

  it('accepts a number, because one call site types the column as one', () => {
    // ShopFloor's local `WorkOrderDetails` declares operation_number as a number
    // even though the column is free text; the helper must not care.
    expect(operationNumberText(10)).toBe('10');
  });

  it('is idempotent — re-running it on its own output changes nothing', () => {
    [...SPELLINGS_OF_TEN, 'A10', 'FINAL', '010'].forEach((stored) => {
      const once = operationNumberText(stored);
      expect(operationNumberText(once)).toBe(once);
    });
  });

  it('never leaks the prefix into a cell that sits under an "Op #" header', () => {
    SPELLINGS_OF_TEN.forEach((stored) => {
      expect(operationNumberText(stored)).not.toMatch(/^op/i);
    });
  });
});

describe('operationNumberText and formatOperationLabel share ONE prefix definition', () => {
  const EVERY_INPUT: Array<string | number | null | undefined> = [
    ...SPELLINGS_OF_TEN,
    ...NAMES_NOTHING,
    ...PASSES_THROUGH.map(([stored]) => stored),
    'Op A10',
    'Operation A10',
    'Op FINAL',
    '110',
    10,
    0,
  ];

  // This is the guard that keeps the two from drifting: whatever either one
  // decides a prefix is, the other decided the same thing. A second regex would
  // break this long before it reached a screen.
  it.each(EVERY_INPUT.map((v) => [JSON.stringify(v) ?? String(v), v] as const))(
    'label === "Op " + bare text, for %s',
    (_label, input) => {
      if (hasOperationNumber(input)) {
        expect(formatOperationLabel(input)).toBe(`Op ${operationNumberText(input)}`);
        expect(operationNumberText(input)).not.toBe('');
      } else {
        expect(formatOperationLabel(input)).toBe(OPERATION_LABEL_FALLBACK);
        expect(operationNumberText(input)).toBe('');
      }
    }
  );

  it('agrees with hasOperationNumber on what is blank — including a lone em-dash', () => {
    // A stored '—' formats to exactly the fallback, so both helpers call it blank.
    // Documented rather than fixed: an em-dash is not an identifier either.
    expect(hasOperationNumber('—')).toBe(false);
    expect(operationNumberText('—')).toBe('');
  });

  it('treats a zero sequence as a real identifier, which truthiness would not', () => {
    expect(operationNumberText(0)).toBe('0');
    expect(formatOperationLabel(0)).toBe('Op 0');
  });
});
