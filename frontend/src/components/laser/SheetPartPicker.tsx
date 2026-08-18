import React, { useMemo, useState } from 'react';
import { ComboBox, ComboBoxOption } from '../ui/ComboBox';
import { formatTieQty } from '../../utils/materialTie';
import { partitionMaterialTiers } from '../../utils/catalogGroups';
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
 *
 * ---------------------------------------------------------------------------
 * THREE TIERS, NOT TWO: RAW STOCK, OTHER MATERIALS, AND NEVER
 * ---------------------------------------------------------------------------
 * The sheet heuristic alone was not enough, because "material" here means
 * "everything the shop buys" — three of the four material-supply part types
 * (`purchased`, `hardware`, `consumable`) are bought COMPONENTS, and the seeded
 * catalog types bolts and nuts as `purchased`. `isSheetLikePart` is text-only,
 * so "Sheet metal screw #8", "Plate nut" and "Abrasive sheets 9x11" all pass it
 * and landed in the DEFAULT view. The default tier is therefore
 * `isRawStockPartType` AND `isSheetLikePart`; everything else in `parts` sits
 * behind the same "Show all materials" toggle as before.
 *
 * The third tier is an EXCLUSION with no escape hatch: a part the shop
 * PRODUCES (`manufactured` / `assembly`) is never offered at either tier. That
 * one is not a default — tying a work order to its own output as material, and
 * depleting it from stock at completion, is not a preference.
 *
 * All three of those rules — plus the pinned selection above and the reveal
 * count — come from `partitionMaterialTiers` in `utils/catalogGroups.ts`, which
 * the two tie modals share. Only the sheet-like narrowing of the DEFAULT tier
 * is this picker's own.
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
  /**
   * The server's shortlist for this nest, rendered FIRST under its own heading.
   *
   * This is the whole "pick from 2, not 500" promise. Appended like
   * `extraOptions` they would sort in behind the entire material catalog (and be
   * skipped outright when the catalog already contains them), so the ranking the
   * matcher and the AI leg computed would be inert — the planner would still be
   * scrolling. Pinning them to the top is what makes an ambiguous row a
   * two-option decision.
   *
   * Order is significant and preserved: `<ComboBox>` emits a group header when
   * the group value changes, so the caller controls layout by array order.
   */
  priorityOptions?: ComboBoxOption[];
  /** Selected part id as a string; `''` is untied. */
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
  id?: string;
  ariaLabel?: string;
  ariaLabelledBy?: string;
  /** Id of an inline notice describing this picker's current state (e.g. "the
   *  tie this nest carried could not be offered here"). */
  ariaDescribedBy?: string;
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
  priorityOptions = NO_EXTRA_OPTIONS,
  value,
  onChange,
  disabled = false,
  className,
  id,
  ariaLabel,
  ariaLabelledBy,
  ariaDescribedBy,
}: SheetPartPickerProps) {
  const [showAll, setShowAll] = useState(false);

  const { options, hiddenCount } = useMemo(() => {
    // The tiering itself — the production exclusion, the raw-stock default, the
    // pinned selection, and a reveal count that never counts a pinned row — is
    // `partitionMaterialTiers`, shared with the two tie modals so a fix to any
    // of those four lands in one place. What stays here is this picker's own
    // policy: the sheet-like narrowing of the default tier, the group headings,
    // and the server's shortlist.
    const {
      defaultTier: sheetParts,
      hiddenTier: otherParts,
      pinned: pinnedParts,
      hiddenCount,
    } = partitionMaterialTiers(parts, {
      showAll,
      // The current selection is pinned in regardless of the filter. Without
      // this a tie to off-convention stock renders as an empty picker, and an
      // empty picker on a re-import silently drops the tie.
      pinnedIds: [value],
      // The shortlist and the pre-filled extras are rendered below from their
      // own arrays, so the toggle would not reveal them and they must not be
      // counted as hidden.
      alsoShownIds: [
        ...priorityOptions.map((option) => option.value),
        ...extraOptions.map((option) => option.value),
      ],
      defaultTierAlso: isSheetLikePart,
    });

    const toOption = (part: Part, group: string): ComboBoxOption => ({
      value: String(part.id),
      label: sheetPartOptionLabel(part),
      hint: stockHint(part, onHandByPart),
      group,
    });

    // The server's shortlist goes FIRST, under its own heading, and everything
    // below dedupes against it — so a shortlisted part appears once, at the top,
    // instead of once in catalog order where the ranking is invisible.
    const visible: ComboBoxOption[] = priorityOptions.map((option) => ({
      ...option,
      group: option.group ?? 'Suggested for this nest',
    }));
    const shortlisted = new Set(visible.map((option) => option.value));
    const notShortlisted = (part: Part) => !shortlisted.has(String(part.id));

    visible.push(...sheetParts.filter(notShortlisted).map((part) => toOption(part, 'Sheet & plate')));
    visible.push(
      ...(showAll ? otherParts : pinnedParts).filter(notShortlisted).map((part) => toOption(part, 'Other materials'))
    );

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

    return { options: visible, hiddenCount };
  }, [parts, onHandByPart, extraOptions, priorityOptions, showAll, value]);

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
      ariaDescribedBy={ariaDescribedBy}
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
