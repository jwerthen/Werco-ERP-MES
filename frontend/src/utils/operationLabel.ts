/**
 * "Op 10" — the ONE operation-number label for every screen in the app.
 *
 * `WorkOrderOperation.operation_number` is FREE TEXT the office types (and that
 * WorkOrderNew mints as `Op ${seq}` on the create form), so the stored value is
 * any of `10`, `OP10`, `Op 10`, `op-10`. Screens used to hard-code a literal
 * `Op ` prefix and interpolate the raw value, which read "Op Op 10" on
 * WO-20260807-006 — so the prefix is normalized here instead, at DISPLAY time
 * only. Nothing about the stored value is ever rewritten.
 *
 * This lives in `utils/` rather than the kiosk barrel because the defect was
 * never kiosk-specific: the shop-floor queue, the dispatch board, the manager
 * dashboard and the material-tie panels all read the same free-text column, and
 * two of them read it from the SAME endpoint the kiosk queue does
 * (GET /shop-floor/work-center-queue/{id}). One column, one label.
 *
 * An existing `op`/`operation` prefix is absorbed only when a SEPARATOR or a
 * DIGIT follows it — `Op 10`, `OP10`, `op-10`, `Operation 10`, and `Op A10` all
 * collapse to one prefix, while a value that merely starts with those letters
 * (`OPTICAL`) passes through untouched. The separator arm is what makes the
 * function idempotent on alphanumeric sequences: without it `Op A10` came back
 * out as `Op Op A10`, which is the very bug this closes.
 *
 * NOT for a numeric `sequence`/counter. `WorkOrderDetail`'s `Op {sequence}` and
 * `Scheduling`'s `Op {n}/{total}` progress counter render an integer this
 * function has no business touching — they are correct as they stand.
 */

/**
 * What an operation with no usable number renders as. Callers that need a
 * DIFFERENT fallback (a `Seq 3`, an `Operation #91`, or nothing at all) gate on
 * `hasOperationNumber` rather than string-matching this.
 */
export const OPERATION_LABEL_FALLBACK = 'Op —';

/**
 * The literal the label carries in front of the identifier. Named because
 * `operationNumberText` peels it back off — a hard-coded `'Op '.length` in two
 * places is the same drift this module exists to prevent.
 */
const OPERATION_LABEL_PREFIX = 'Op ';

export function formatOperationLabel(operationNumber: string | number | null | undefined): string {
  if (operationNumber == null) return OPERATION_LABEL_FALLBACK;
  const raw = String(operationNumber).trim();
  if (!raw) return OPERATION_LABEL_FALLBACK;
  // A value that is NOTHING but the prefix ("Op", "OPERATION", "OP-") carries no
  // identifier, and the strip below deliberately refuses to fire without something
  // following it -- so without this guard "Op" would render "Op Op", the exact bug
  // this function exists to close. Falls back to the em-dash shown for a missing
  // number, because that is what this value amounts to.
  if (/^op(?:eration)?[\s._\-#:]*$/i.test(raw)) return OPERATION_LABEL_FALLBACK;
  const stripped = raw.replace(/^op(?:eration)?(?:[\s._\-#:]+(?=\S)|(?=\d))/i, '').trim();
  return `${OPERATION_LABEL_PREFIX}${stripped}`;
}

/**
 * Does this stored value name an operation at all?
 *
 * Exists because the call sites do NOT share one empty-state. The kiosk shows
 * `Op —`; the dispatch board falls back to the word "Operation"; the manager
 * dashboard omits the segment (and its separator) entirely; a material tie names
 * the operation by id; the tie modal falls back to `Seq {n}`. Each of those is a
 * table or a toast someone reads all day, so the fallback is preserved per site
 * and only the LABEL is unified.
 *
 * Deliberately delegates to `formatOperationLabel` instead of re-testing the
 * regexes: a second copy of "what counts as blank" is exactly how the office and
 * floor spellings drifted apart in the first place. In particular this is TRUE
 * for `0` and FALSE for a prefix-only `"Op"` / `"  "`, neither of which a bare
 * truthiness check gets right.
 */
export function hasOperationNumber(operationNumber: string | number | null | undefined): boolean {
  return formatOperationLabel(operationNumber) !== OPERATION_LABEL_FALLBACK;
}

/**
 * The BARE identifier — `10` — with any legacy `Op`/`OP`/`Operation` prefix and
 * separator stripped. The column-cell twin of `formatOperationLabel`.
 *
 * Why it exists: `operation_number` is an IDENTIFIER column, but the create form
 * used to mint a display LABEL into it (`Op 10`), so a screen that renders the
 * raw value under its own `Op #` / `Seq` header showed the prefix twice — once in
 * the header, once in every cell. The mint is fixed forward (WorkOrderNew now
 * stores `10`), and there is deliberately NO backfill, so the table is
 * permanently mixed: rows written before the fix hold `Op 10`, rows written after
 * hold `10`. Without this helper those two render as different strings in the
 * same column, which is the same defect wearing a different hat.
 *
 * Blank in, EMPTY STRING out — not an em-dash. The call sites do not share an
 * empty-state (the traveler falls back to the numeric `sequence`, the routing
 * tables to an empty cell), so returning `''` lets each keep its own via a plain
 * `||` chain instead of string-matching a sentinel. `hasOperationNumber` is still
 * the gate for anything richer.
 *
 * Defined by PEELING `formatOperationLabel`'s output rather than re-running its
 * regexes. That is the point: one definition of "what counts as a prefix" for the
 * label and the bare text both, so they cannot drift the way the office and floor
 * spellings did. The invariant the tests pin is
 * `formatOperationLabel(x) === 'Op ' + operationNumberText(x)` for every value
 * that names an operation at all.
 *
 * NOT for a numeric `sequence`/counter — same caveat as `formatOperationLabel`.
 */
export function operationNumberText(operationNumber: string | number | null | undefined): string {
  const label = formatOperationLabel(operationNumber);
  if (label === OPERATION_LABEL_FALLBACK) return '';
  return label.slice(OPERATION_LABEL_PREFIX.length);
}
