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
 * Not every row in this column is an "Op", either: the laser path mints
 * `Nest 3`, and prefixing that read `Op Nest 3` — the same doubled-noun defect
 * one noun over. See `SELF_LABELED`.
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

/**
 * Identifiers that already carry their own noun, and so must NOT be prefixed.
 *
 * `operation_number` does not hold op numbers exclusively: `laser_nest_service`
 * writes `Nest {index}` into it for every nest task it creates, and those rows
 * reach the same kiosk cards, dispatch board and duplicate-skip notice as a
 * routed `10`. Prefixing them produced `Op Nest 3`, which is the doubled-noun
 * defect this module exists to close wearing a different noun.
 *
 * The set is deliberately CLOSED to what the app actually writes — `Nest` is the
 * only self-labeled shape any mint produces (see the `operation_number=` sites
 * under `backend/app/`; every other one writes a bare sequence or copies free
 * text through). Guessing at `Setup`/`Insp`/`Weld` would start silently dropping
 * the `Op ` off numbers the office typed by hand, which is the worse failure:
 * this way an unknown value is over-labeled, never mis-labeled.
 *
 * `\b` rather than the separator/digit rule used for the `op` prefix, because
 * this matches a whole WORD rather than stripping one: `Nest 3`, `NEST-3` and a
 * bare `Nest` all name a nest, while `Nesting fixture` — a plausible hand-typed
 * operation name — still reads `Op Nesting fixture`.
 */
const SELF_LABELED = /^nest\b/i;

/**
 * The stored value with any `op`/`operation` prefix removed, and `''` for
 * anything that names no operation at all.
 *
 * The ONE place the parsing lives. Both exported renderings are defined in terms
 * of it, so "what counts as a prefix" cannot drift between the label and the
 * bare cell — which is the drift that produced this whole thread.
 */
function bareIdentifier(operationNumber: string | number | null | undefined): string {
  if (operationNumber == null) return '';
  const raw = String(operationNumber).trim();
  if (!raw) return '';
  // A value that is NOTHING but the prefix ("Op", "OPERATION", "OP-") carries no
  // identifier, and the strip below deliberately refuses to fire without something
  // following it -- so without this guard "Op" would render "Op Op", the exact bug
  // this module exists to close.
  if (/^op(?:eration)?[\s._\-#:]*$/i.test(raw)) return '';
  return raw.replace(/^op(?:eration)?(?:[\s._\-#:]+(?=\S)|(?=\d))/i, '').trim();
}

export function formatOperationLabel(operationNumber: string | number | null | undefined): string {
  const identifier = bareIdentifier(operationNumber);
  // Falls back to the em-dash shown for a missing number, because a value that
  // parses to nothing amounts to exactly that.
  if (!identifier) return OPERATION_LABEL_FALLBACK;
  // Matched against the BARE identifier, not the raw value, so a stored
  // `Op Nest 3` -- the doubled form that copy-pasting the old rendering back into
  // the field would produce -- heals to `Nest 3` rather than surviving.
  if (SELF_LABELED.test(identifier)) return identifier;
  return `${OPERATION_LABEL_PREFIX}${identifier}`;
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
 * Defined by PEELING `formatOperationLabel`'s output rather than by calling
 * `bareIdentifier` directly, even though the two agree on every input today.
 * That keeps this function and `hasOperationNumber` deciding "blank" the same
 * way: a stored `—` parses to a non-empty identifier but FORMATS to exactly the
 * fallback, so peeling the label reports it blank on both, where the parser
 * alone would have them disagree.
 *
 * The invariant the tests pin is `formatOperationLabel(x) === 'Op ' +
 * operationNumberText(x)` for every value that names an operation — EXCEPT a
 * self-labeled one (`Nest 3`), where the label is the identifier itself and the
 * two are equal. Hence the conditional peel below: a blind `.slice(3)` would
 * turn `Nest 3` into `t 3`.
 *
 * NOT for a numeric `sequence`/counter — same caveat as `formatOperationLabel`.
 */
export function operationNumberText(operationNumber: string | number | null | undefined): string {
  const label = formatOperationLabel(operationNumber);
  if (label === OPERATION_LABEL_FALLBACK) return '';
  return label.startsWith(OPERATION_LABEL_PREFIX) ? label.slice(OPERATION_LABEL_PREFIX.length) : label;
}
