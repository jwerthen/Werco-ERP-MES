/**
 * Sheet/plate stock-part recognition and dimension parsing.
 *
 * Two questions the laser-nest import wizard asks about a material part, both
 * answered from what the part record already carries (`part_number` / `name` /
 * `description`) rather than from new columns:
 *
 *   1. `isSheetLikePart` — is this flat stock a laser nest could be cut from?
 *      Drives the sheet-part picker's default filter, so a planner tying a nest
 *      is not scrolling past bolts, beams, angle and round bar.
 *   2. `deriveSheetSpec` — what thickness and sheet size does it represent?
 *      Drives the thickness / sheet-size pull-through: picking a sheet part
 *      stamps its real dimensions onto the nest row, replacing the AI's read of
 *      the nest report.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS IS A HEURISTIC AND NOT A COLUMN
 * ---------------------------------------------------------------------------
 * `Part` has no thickness / width / length / "is flat stock" fields, and the
 * shop's stocking convention already encodes all four in the part number:
 *
 *     0.188-72X144-A36      0.188" plate, 72 x 144
 *     0.06X60X144-304SS     0.060" stainless sheet, 60 x 144
 *     10GA-72X120-CS        10 ga carbon sheet, 72 x 120
 *
 * Adding real columns would mean a migration plus re-keying every stock part by
 * hand before the feature does anything. Reading the convention costs nothing
 * and is correct for the data that exists today. The trade is that it can be
 * WRONG, so every consumer treats it as a suggestion:
 *
 *  - the filter is a DEFAULT with a "show all materials" escape hatch, never a
 *    restriction — a sheet named off-convention is one toggle away, not lost;
 *  - the derived spec lands in EDITABLE fields the planner can overwrite, and
 *    the wizard marks a value it replaced rather than swallowing it.
 *
 * ---------------------------------------------------------------------------
 * FAIL CLOSED, IN BOTH DIRECTIONS
 * ---------------------------------------------------------------------------
 * Both functions return "no answer" rather than a guess, because both wrong
 * answers are worse than silence:
 *
 *  - `deriveSheetSpec` returning a bogus thickness would overwrite a correct AI
 *    read with a fabricated one, and thickness is what an operator loads the
 *    machine from. So the dimension grammar is ANCHORED (a sheet part number
 *    starts with its thickness) rather than "find three numbers separated by
 *    x anywhere" — `ANG-A36-1.5X1.5X.25` and `1.50" x 1.50" x 0.250" THK A36
 *    Angle` both contain a matching triple and are both angle, not sheet.
 *  - `isSheetLikePart` returning true for a beam would put it back in the list
 *    this exists to clean up, so recognition needs a POSITIVE signal: the
 *    anchored part-number grammar, or the words "sheet"/"plate"/"coil" in the
 *    part's own text. Nothing is inferred from what a part is *not*.
 *
 * The wizard additionally only pulls a spec through for a part that passes
 * `isSheetLikePart`, so the one case where the anchored grammar could still
 * mis-read — a planner deliberately tying a nest to non-sheet stock through the
 * "show all" escape hatch — stamps nothing instead of stamping nonsense.
 */

/** The fields of a `Part` this module reads. Structural so tests can pass literals. */
export interface SheetPartLike {
  part_number?: string | null;
  name?: string | null;
  description?: string | null;
}

/** Thickness and sheet size as the wizard's two free-text fields want them. */
export interface SheetSpec {
  /** `"0.188"`, `"10 ga"` — verbatim from the source, never rounded. */
  thickness: string | null;
  /** `"72x144"` — matches the `96x48` shape the extractor already emits. */
  sheetSize: string | null;
}

const EMPTY_SPEC: SheetSpec = { thickness: null, sheetSize: null };

/**
 * Uppercase, drop inch marks and the `THK` noise word, collapse whitespace.
 *
 * `THK` has to go before the dimension match: `0.188" THK x 60 x 120` would
 * otherwise break the thickness away from the two dimensions that follow it.
 *
 * The whole quote family is stripped, not just the forms that matter today.
 * Excel autocorrects an inch mark after a digit to the CLOSING `”`, so that is
 * the one this data actually contains — but a part number retyped or pasted from
 * elsewhere can carry the opening `“`, the straight `'` or the true prime `″`,
 * and a dimension that fails to parse is invisible (the picker just stamps
 * nothing). Covering the whole family costs nothing and removes a failure mode
 * nobody would think to look for.
 *
 * THIS CHARACTER CLASS MUST STAY IN LOCK-STEP with `_QUOTES` in
 * `backend/app/services/sheet_stock_spec.py`. `'` and `″` were missing here
 * until 2026-08-10, and the divergence failed in the dangerous direction: the
 * server parsed `0.188″-60X120-A36` as real sheet stock and could pre-fill it,
 * while this function returned false for the same part so the picker's default
 * filter HID it — leaving the planner a pre-filled tie with nothing in the list
 * to check it against. `tests/fixtures/sheet_part_cases.json` now carries a row
 * for each, read by both test suites, so the two ports cannot drift again.
 */
function normalize(text: string): string {
  return text
    .toUpperCase()
    .replace(/["'‘’“”„″]/g, '')
    .replace(/\bTHK\b\.?/g, ' ')
    .replace(/\bINCHES?\b\.?/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// A sheet's three dimensions, ANCHORED at the start of the normalized string:
// thickness first, then width x length. The anchor is the whole safeguard —
// unanchored, every angle and tube part number in the catalog matches.
//
// Separator between thickness and width is `X` or `-` (`0.06X60X144` and
// `0.188-72X144` are both in use); between width and length it is always `X`.
const DECIMAL_TRIPLE = /^(\d*\.\d+|\d+)\s*[X-]\s*(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)/;
// The gauge form of the same shape: `10GA-72X120`, `16 GA X 60 X 144`.
const GAUGE_TRIPLE = /^(\d+)\s*GA\b\.?\s*[X-]?\s*(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)/;

/** Words that make a part flat stock regardless of its numbering convention. */
const SHEET_WORDS = /\b(SHEET|SHEETS|PLATE|PLATES|COIL)\b/;

function matchTriple(normalized: string): SheetSpec | null {
  const gauge = GAUGE_TRIPLE.exec(normalized);
  if (gauge) {
    return { thickness: `${gauge[1]} ga`, sheetSize: `${gauge[2]}x${gauge[3]}` };
  }
  const decimal = DECIMAL_TRIPLE.exec(normalized);
  if (decimal) {
    return { thickness: decimal[1], sheetSize: `${decimal[2]}x${decimal[3]}` };
  }
  return null;
}

/**
 * Thickness + sheet size for a stock part, or `{null, null}` when the part does
 * not state them in a shape this understands.
 *
 * The part NUMBER is tried first and wins: it is the shop's canonical
 * identifier, keyed once and reused, while the name is prose that drifts (the
 * catalog contains at least one part whose number says 144 and whose name says
 * 120). The name is a fallback for parts numbered off-convention, and it is
 * only consulted for its own anchored triple — the same grammar, not a looser
 * one, so a name like `A36 Angle 1.5 x 1.5 x .25` still yields nothing.
 *
 * Values come back exactly as written. `0.250` does not become `0.25`: these
 * land in the nest's free-text thickness field, which an operator compares
 * against a tag on a rack.
 */
export function deriveSheetSpec(part: SheetPartLike | null | undefined): SheetSpec {
  if (!part) return EMPTY_SPEC;
  const fromNumber = matchTriple(normalize(part.part_number || ''));
  if (fromNumber) return fromNumber;
  const fromName = matchTriple(normalize(part.name || ''));
  if (fromName) return fromName;
  return EMPTY_SPEC;
}

/**
 * Gauge → decimal inches. Mirrors the backend `GAUGE_TO_INCHES`
 * (`sheet_metal_costing_service.py`), which is what the server-side matcher's
 * thickness gate runs on — two implementations of the same table would be a
 * silent disagreement about which sheet is which.
 *
 * Only the gauges this shop stocks are listed. A gauge that is not here (9ga,
 * 13ga) reads as UNREADABLE rather than being interpolated, for the same reason
 * a bare `16` is not silently treated as 16 ga: this codebase does not infer
 * units it was not given.
 */
const GAUGE_TO_INCHES: Record<string, number> = {
  '24': 0.0239,
  '22': 0.0299,
  '20': 0.0359,
  '18': 0.0478,
  '16': 0.0598,
  '14': 0.0747,
  '12': 0.1046,
  '11': 0.1196,
  '10': 0.1345,
  '7': 0.1793,
};

/**
 * Bounds a parsed thickness has to sit inside to count as read at all, matching
 * `sheet_stock_spec.MIN/MAX_PLAUSIBLE_THICKNESS_IN`.
 *
 * This exists for one specific failure: the decimal branch reads a bare `16` as
 * 16 inches. Nobody lasers 16 inches of plate, and letting it through would put
 * a nonsense number into a numeric comparison. Bounding turns it into a clean
 * "unreadable", which every caller here already fails closed on.
 */
export const MIN_PLAUSIBLE_THICKNESS_IN = 0.005;
export const MAX_PLAUSIBLE_THICKNESS_IN = 4.0;

/**
 * A thickness string as inches, or `null` when it cannot be read.
 *
 * Reads the same grammar as the backend, in the same order: gauge, mixed
 * fraction, fraction, millimetres, decimal inches.
 *
 * ---------------------------------------------------------------------------
 * THIS IS FOR COMPARING TWO THICKNESSES, NEVER FOR WRITING ONE
 * ---------------------------------------------------------------------------
 * The sheet-part pull-through stays STRING-VERBATIM (`deriveSheetSpec`): what it
 * stamps onto a nest row is what an operator compares against a tag on a rack,
 * so `0.250` must not become `0.25` and `10 ga` must not become `0.1345`. The
 * only caller of this function is the wizard's "the tied part disagrees with the
 * nest report" marker, which needs to know that `0.1875` and `0.188` are the
 * same sheet and `0.125` and `0.188` are not.
 *
 * `null` means UNREADABLE and must never be coerced to 0: a 0 compares equal to
 * nothing while looking like a real number. A caller comparing two values falls
 * back to a string compare when either side is null.
 */
export function thicknessInches(value: string | null | undefined): number | null {
  if (value == null) return null;
  const text = String(value).trim().toLowerCase().replace(/,/g, '');
  if (!text) return null;

  const parsed = parseThickness(text);
  if (parsed == null || !Number.isFinite(parsed)) return null;
  if (parsed < MIN_PLAUSIBLE_THICKNESS_IN || parsed > MAX_PLAUSIBLE_THICKNESS_IN) return null;
  return parsed;
}

/** The grammar half of `thicknessInches`, pre-normalized and unbounded. */
function parseThickness(text: string): number | null {
  const gauge = /\b(\d{1,2})\s*(?:ga|gauge)\b/.exec(text);
  // A gauge NUMBER that is not stocked returns null rather than falling through
  // to the decimal branch, which would read "9ga" as 9 inches.
  if (gauge) return GAUGE_TO_INCHES[gauge[1]] ?? null;

  const mixed = /\b(\d+)\s+(\d+)\s*\/\s*(\d+)\b/.exec(text);
  if (mixed && Number(mixed[3]) !== 0) return Number(mixed[1]) + Number(mixed[2]) / Number(mixed[3]);

  const fraction = /\b(\d+)\s*\/\s*(\d+)\b/.exec(text);
  if (fraction && Number(fraction[2]) !== 0) return Number(fraction[1]) / Number(fraction[2]);

  const mm = /(\d*\.?\d+)\s*mm\b/.exec(text);
  if (mm) return Number(mm[1]) / 25.4;

  const inches = /(\d*\.?\d+)\s*(?:in|inch|inches|")?\b/.exec(text);
  if (inches) return Number(inches[1]);

  return null;
}

/**
 * Is this material part flat stock a laser nest is cut from?
 *
 * True when EITHER signal is present:
 *  - the part number parses as an anchored thickness + width x length, or
 *  - the part number, name or description says "sheet", "plate" or "coil".
 *
 * Both are positive tests. Nothing is excluded by keyword — a veto list ("not
 * a beam, not a tube, not a bolt…") is unbounded and silently drops the stock
 * it forgot to name, which is the failure that hides a real sheet from the
 * planner who needs it.
 *
 * A false negative is cheap here (one toggle away in the picker); a false
 * positive just leaves an odd row in a list the planner is reading anyway.
 */
export function isSheetLikePart(part: SheetPartLike | null | undefined): boolean {
  if (!part) return false;
  if (matchTriple(normalize(part.part_number || ''))) return true;
  const text = normalize(`${part.part_number || ''} ${part.name || ''} ${part.description || ''}`);
  return SHEET_WORDS.test(text);
}
