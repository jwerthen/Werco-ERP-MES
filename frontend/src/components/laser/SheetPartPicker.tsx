import React, { useMemo, useState } from 'react';
import { ComboBox, ComboBoxOption } from '../ui/ComboBox';
import { formatTieQty } from '../../utils/materialTie';
import { isSheetLikePart } from '../../utils/sheetPart';
import { Part } from '../../types';

/**
 * The sheet-part picker used by the laser-nest import wizard.
 *
 * Wraps the generic `<ComboBox>` with the two things that are policy rather
 * than mechanics: which stock parts belong in a *sheet* picker, and how a
 * part's on-hand quantity is phrased.
 *
 * ---------------------------------------------------------------------------
 * THE FILTER IS A DEFAULT, NOT A RESTRICTION
 * ---------------------------------------------------------------------------
 * `/materials` serves every purchased / raw-material / hardware / consumable
 * part in the tenant, so an unfiltered picker offers bolts, nuts, beams, angle,
 * round bar and tube to someone tying a laser nest to a sheet. `isSheetLikePart`
 * narrows that to flat stock by default.
 *
 * It is a HEURISTIC over the part number and name (see `utils/sheetPart.ts`),
 * so it can be wrong, so it must always be escapable: the popup carries a "Show
 * all materials" toggle.
 *
 * Separately from that toggle, the CURRENT SELECTION is always pinned into the
 * list even when the filter would hide it. A tie the planner already made —
 * pre-filled from an existing nest, or picked deliberately through the escape
 * hatch — can never be filtered out from under them and silently re-render as
 * blank, because a blank picker on a re-import is exactly how a work order gets
 * quietly untied. Pinning one row is deliberately preferred over auto-flipping
 * `showAll`: it keeps the tie visible without dumping 400 bolts and beams into
 * a sheet picker the planner did not ask to widen.
 */

export interface SheetPartPickerProps {
  /** Every material part loaded for the wizard, unfiltered. */
  parts: Part[];
  /**
   * part_id -> on-hand. `null` when the stock read did not land — the hint is
   * then omitted entirely rather than fabricating a zero.
   */
  onHandByPart: Record<number, number> | null;
  /**
   * Parts a pre-filled tie names that `parts` does not contain (capped list,
   * deactivated part, failed read). Kept selectable so an existing tie survives.
   */
  extraOptions?: ComboBoxOption[];
  /** Selected part id as a string; `''` is untied. */
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
  id?: string;
  ariaLabel?: string;
  ariaLabelledBy?: string;
}

/**
 * One option's label.
 *
 * The stock figure is deliberately called **on hand**, never "available":
 * `quantity_allocated` is never written by anything, so an "available" number
 * would just be on-hand under a name that goes false the day reservations ship.
 */
export function sheetPartOptionLabel(part: Part): string {
  return part.part_number ? `${part.part_number} — ${part.name}` : part.name;
}

function stockHint(part: Part, onHandByPart: Record<number, number> | null): string | undefined {
  if (!onHandByPart) return undefined;
  const onHand = onHandByPart[part.id] ?? 0;
  return `${formatTieQty(onHand)} ${part.unit_of_measure} on hand`;
}

/**
 * Module-scope empty default. A `= []` default parameter allocates a fresh
 * array every render, which changes the `useMemo` dependency identity on every
 * render and defeats the memo entirely for any caller that omits the prop.
 */
const NO_EXTRA_OPTIONS: ComboBoxOption[] = [];

export function SheetPartPicker({
  parts,
  onHandByPart,
  extraOptions = NO_EXTRA_OPTIONS,
  value,
  onChange,
  disabled = false,
  className,
  id,
  ariaLabel,
  ariaLabelledBy,
}: SheetPartPickerProps) {
  const [showAll, setShowAll] = useState(false);

  const { options, hiddenCount } = useMemo(() => {
    const sheetParts = parts.filter(isSheetLikePart);
    const otherParts = parts.filter((part) => !isSheetLikePart(part));

    // The current selection is pinned in regardless of the filter. Without this
    // a tie to off-convention stock renders as an empty picker, and an empty
    // picker on a re-import silently drops the tie.
    const selectedIsHidden =
      value !== '' && !showAll && otherParts.some((part) => String(part.id) === value);

    const toOption = (part: Part, group: string): ComboBoxOption => ({
      value: String(part.id),
      label: sheetPartOptionLabel(part),
      hint: stockHint(part, onHandByPart),
      group,
    });

    const visible: ComboBoxOption[] = sheetParts.map((part) => toOption(part, 'Sheet & plate'));
    if (showAll) {
      visible.push(...otherParts.map((part) => toOption(part, 'Other materials')));
    } else if (selectedIsHidden) {
      const selectedPart = otherParts.find((part) => String(part.id) === value);
      if (selectedPart) visible.push(toOption(selectedPart, 'Other materials'));
    }

    // Ties pre-filled from parts the material load never returned. Appended
    // rather than merged so they cannot displace a real catalog row. `known`
    // GROWS as we go: two nests tied to the same missing part would otherwise
    // push it twice, rendering a duplicate row and tripping React's
    // duplicate-key warning. The producer dedupes as well — this is the half
    // that does not require trusting the caller.
    const known = new Set(visible.map((option) => option.value));
    for (const extra of extraOptions) {
      if (known.has(extra.value)) continue;
      known.add(extra.value);
      visible.push({ ...extra, group: extra.group ?? 'Other materials' });
    }

    // What the toggle would actually REVEAL — not `otherParts.length`. A pinned
    // selection is already on screen, so counting it advertises "1 more" that
    // does not exist.
    const hiddenCount = showAll ? 0 : otherParts.filter((part) => !known.has(String(part.id))).length;

    return { options: visible, hiddenCount };
  }, [parts, onHandByPart, extraOptions, showAll, value]);

  return (
    <ComboBox
      options={options}
      value={value}
      onChange={onChange}
      emptyOptionLabel="(none — untied)"
      placeholder="Search sheet stock…"
      disabled={disabled}
      className={className}
      id={id}
      ariaLabel={ariaLabel}
      ariaLabelledBy={ariaLabelledBy}
      noResultsLabel={hiddenCount > 0 ? 'No sheet stock matches — try "Show all materials"' : 'No matches'}
      footer={
        hiddenCount > 0 || showAll ? (
          <button
            type="button"
            onClick={() => setShowAll((prev) => !prev)}
            className="text-xs font-medium text-fd-blue hover:underline"
          >
            {showAll ? 'Show sheet & plate only' : `Show all materials (${hiddenCount} more)`}
          </button>
        ) : null
      }
    />
  );
}

export default SheetPartPicker;
