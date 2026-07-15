/**
 * Pure helpers for the PO/Quote upload review screen (pages/POUpload.tsx).
 *
 * A real PO frequently repeats the same part number across multiple line
 * items. The backend creates each new part once (matching part numbers
 * case-insensitively, ignoring surrounding whitespace) and attaches every
 * line to it, so the review UI must treat lines that share a part-number
 * key as covered by a single "create new part" choice rather than as
 * independent unmatched lines.
 *
 * These helpers use minimal structural input types (rather than importing
 * the page's LineItem) to keep the dependency direction page -> utils.
 */

/** Minimal shape needed to derive a line's part-number identity. */
export interface PartNumberSource {
  part_number?: string | null;
  suggested_part_number?: string | null;
}

/**
 * The line's effective part number: the entered part number, falling back to
 * the AI-suggested Werco number. Trimming happens BEFORE the fallback so a
 * whitespace-only entry doesn't shadow a usable suggested number.
 * Returns '' when the line has no usable part number.
 */
export function effectivePartNumber(item: PartNumberSource): string {
  return (item.part_number || '').trim() || (item.suggested_part_number || '').trim();
}

/**
 * Canonical identity key for a line's part number, lowercased so it matches
 * the backend's case/whitespace-insensitive part matching.
 * Returns '' when the line has no usable part number.
 */
export function partNumberKey(item: PartNumberSource): string {
  return effectivePartNumber(item).toLowerCase();
}

/**
 * Keys of all lines the user marked "create new part". A line without its
 * own part assignment is still submittable when its key is in this set —
 * the backend creates the part once and attaches both lines. Empty keys are
 * never included (a blank part number can't cover anything).
 */
export function newPartCoverageKeys(
  lineItems: Array<PartNumberSource & { create_new_part?: boolean }>
): Set<string> {
  const keys = new Set<string>();
  for (const item of lineItems) {
    if (item.create_new_part !== true) continue;
    const key = partNumberKey(item);
    if (key) keys.add(key);
  }
  return keys;
}

export interface NewPartPayload<TPartType extends string = string> {
  part_number: string;
  description: string;
  part_type: TPartType;
}

/**
 * Builds the `create_parts` payload: one entry per distinct part-number key
 * among the lines marked "create new part" (first occurrence wins), with
 * entries lacking any part number dropped. Part numbers are trimmed but
 * keep their entered casing.
 */
export function dedupePartsToCreate<TPartType extends string>(
  lineItems: Array<
    PartNumberSource & { create_new_part?: boolean; description: string; new_part_type: TPartType }
  >
): Array<NewPartPayload<TPartType>> {
  const seen = new Set<string>();
  const parts: Array<NewPartPayload<TPartType>> = [];
  for (const item of lineItems) {
    if (item.create_new_part !== true) continue;
    const partNumber = effectivePartNumber(item);
    if (!partNumber) continue;
    const key = partNumber.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    parts.push({
      part_number: partNumber,
      description: item.description,
      part_type: item.new_part_type,
    });
  }
  return parts;
}

export interface LineItemPayload {
  part_id: number;
  part_number: string;
  description: string;
  quantity_ordered: number;
  unit_price: number;
  line_total: number;
}

/**
 * Builds the `line_items` payload for create-from-upload. Lines without a
 * selected part send `part_id: 0` — the backend resolves them against
 * `create_parts` (or its own matching) by part number.
 */
export function buildLineItemsPayload(
  lineItems: Array<
    PartNumberSource & {
      description: string;
      qty_ordered: number;
      unit_price: number;
      line_total: number;
      selected_part_id?: number | null;
    }
  >
): LineItemPayload[] {
  return lineItems.map((item) => ({
    part_id: item.selected_part_id || 0,
    part_number: effectivePartNumber(item),
    description: item.description,
    quantity_ordered: item.qty_ordered,
    unit_price: item.unit_price,
    line_total: item.line_total,
  }));
}
